import json
import sqlite3
import time
from contextlib import closing, contextmanager

ACTIVE = ("queued", "draft", "approved", "deferred", "unknown", "questions", "error", "sending")
HISTORY = ("reported", "rejected", "filtered", "duplicate", "sent")
LABELS = {
    "queued": "Ожидает анализа",
    "draft": "Подготовлен черновик",
    "approved": "Письмо готово",
    "deferred": "Отложена",
    "unknown": "Результат неизвестен",
    "reported": "Отправка отмечена владельцем",
    "filtered": "Не подходит",
    "rejected": "Отклонена владельцем",
    "questions": "Нужно проверить форму HH",
    "error": "Ошибка анализа",
    "duplicate": "Отклик уже есть на HH",
    "sending": "Отправляется через HH",
    "sent": "Отправка подтверждена площадкой",
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS vacancies (
    id TEXT PRIMARY KEY, details TEXT NOT NULL, status TEXT NOT NULL,
    found_at INTEGER NOT NULL, analyzed_at INTEGER, result TEXT, letter TEXT,
    attempts INTEGER NOT NULL DEFAULT 0, retry_at INTEGER NOT NULL DEFAULT 0,
    sent_day TEXT, sent_at INTEGER, error TEXT
);
CREATE INDEX IF NOT EXISTS vacancy_queue ON vacancies(status, retry_at, found_at);
CREATE INDEX IF NOT EXISTS vacancy_day ON vacancies(sent_day);
CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, result TEXT NOT NULL, at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, vacancy TEXT, action TEXT NOT NULL, at INTEGER NOT NULL
);
"""


class Database:
    def __init__(self, directory):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = directory / "autoapply.sqlite3"
        with self.connection() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > 1:
                raise RuntimeError("Версия базы новее этой версии приложения.")
            db.execute("PRAGMA journal_mode=WAL")
            if not version:
                db.executescript("BEGIN IMMEDIATE;" + SCHEMA + "PRAGMA user_version=1; COMMIT;")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=2)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, key, fallback=None):
        with self.connection() as db:
            row = db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else fallback

    def set(self, key, value):
        self.set_many({key: value})

    def set_many(self, items):
        with self.connection() as db:
            db.executemany(
                "INSERT OR REPLACE INTO state VALUES (?,?)",
                ((key, json.dumps(value)) for key, value in items.items()),
            )

    def settings(self, config):
        return config.defaults | self.get("settings", {})

    def add(self, vacancy_id, details, review=False, *, deferred=False):
        status = "deferred" if deferred else "queued"
        with self.connection() as db:
            inserted = db.execute(
                "INSERT OR IGNORE INTO vacancies(id,details,status,found_at) VALUES(?,?,?,?)",
                (vacancy_id, json.dumps(details), status, int(time.time())),
            ).rowcount
            if inserted:
                self.event(db, vacancy_id, status)
                return True
            if review:
                changed = db.execute(
                    """UPDATE vacancies SET details=?, status='queued', result=NULL, letter=NULL,
                       attempts=0, retry_at=0, error=NULL, analyzed_at=NULL
                       WHERE id=? AND status IN ('error','filtered')""",
                    (json.dumps(details), vacancy_id),
                ).rowcount
                if changed:
                    self.event(db, vacancy_id, "queued")
                return bool(changed)
            return False

    def add_discovered(self, vacancy_id, details):
        details = details | {"auto_discovered": True}
        with self.connection() as db:
            inserted = db.execute(
                "INSERT OR IGNORE INTO vacancies(id,details,status,found_at) VALUES(?,?,?,?)",
                (vacancy_id, json.dumps(details), "queued", int(time.time())),
            ).rowcount
            if inserted:
                self.event(db, vacancy_id, "queued")
                return True
            if details["source"] == "browser":
                # An earlier Mini App search may have used different thresholds. Let the
                # recurring HH search re-evaluate untouched results under its own settings.
                promoted = db.execute(
                    """UPDATE vacancies SET details=?,status='queued',result=NULL,letter=NULL,
                       analyzed_at=NULL,error=NULL,attempts=0,retry_at=0
                       WHERE id=? AND status IN ('queued','draft','filtered','error')
                       AND sent_day IS NULL
                       AND (status<>'draft' OR letter=json_extract(result, '$.letter'))
                       AND json_extract(details, '$.auto_discovered')=1
                       AND json_extract(details, '$.source')='mini_hh'""",
                    (json.dumps(details), vacancy_id),
                ).rowcount
                if promoted:
                    self.event(db, vacancy_id, "queued")
                    return True
            return False

    def add_vacancy_description(self, vacancy_id, details):
        with self.connection() as db:
            changed = db.execute(
                """UPDATE vacancies SET details=?,status='queued',attempts=0,retry_at=0,error=NULL
                   WHERE id=? AND status IN ('deferred','queued')
                   AND COALESCE(json_extract(details, '$.description'), '')=''
                   AND result IS NULL AND letter IS NULL AND sent_day IS NULL""",
                (json.dumps(details), vacancy_id),
            ).rowcount
            if changed:
                self.event(db, vacancy_id, "queued")
            return bool(changed)

    @staticmethod
    def event(db, vacancy_id, action):
        db.execute(
            "INSERT INTO events(vacancy,action,at) VALUES(?,?,?)",
            (vacancy_id, action, int(time.time())),
        )

    def vacancy(self, vacancy_id):
        with self.connection() as db:
            return db.execute("SELECT * FROM vacancies WHERE id=?", (vacancy_id,)).fetchone()

    def vacancies(self, identifiers):
        if not identifiers:
            return {}
        values = ",".join("?" for _ in identifiers)
        with self.connection() as db:
            rows = db.execute(f"SELECT * FROM vacancies WHERE id IN ({values})", tuple(identifiers))
            return {row["id"]: row for row in rows}

    def page(self, statuses, page, size=5):
        values = ",".join("?" for _ in statuses)
        where = f"WHERE status IN ({values})" if statuses else ""
        with self.connection() as db:
            return db.execute(
                f"SELECT * FROM vacancies {where} ORDER BY found_at DESC,id DESC LIMIT ? OFFSET ?",
                (*statuses, size + 1, page * size),
            ).fetchall()

    def pending(self, *, source=None):
        with self.connection() as db:
            return db.execute(
                """SELECT * FROM vacancies WHERE status='queued' AND retry_at<=?
                   AND (? IS NULL OR json_extract(details, '$.source')=?)
                   ORDER BY found_at,id LIMIT 1""",
                (int(time.time()), source, source),
            ).fetchone()

    def finish_analysis(self, vacancy_id, status, result, letter, *, expected_details=None):
        with self.connection() as db:
            changed = db.execute(
                """UPDATE vacancies SET status=?,result=?,letter=?,analyzed_at=?,error=NULL
                   WHERE id=? AND status='queued' AND (? IS NULL OR details=?)""",
                (
                    status,
                    json.dumps(result),
                    letter,
                    int(time.time()),
                    vacancy_id,
                    expected_details,
                    expected_details,
                ),
            ).rowcount
            if changed:
                self.event(db, vacancy_id, status)
            return bool(changed)

    def failure(self, vacancy_id, attempt, message, delay, *, expected_details=None):
        with self.connection() as db:
            status = "queued" if delay else "error"
            changed = db.execute(
                """UPDATE vacancies SET status=?,attempts=?,retry_at=?,error=?
                   WHERE id=? AND status='queued' AND (? IS NULL OR details=?)""",
                (
                    status,
                    attempt,
                    int(time.time()) + delay,
                    message,
                    vacancy_id,
                    expected_details,
                    expected_details,
                ),
            ).rowcount
            if changed:
                self.event(db, vacancy_id, "analysis_error")

    def transition(
        self, vacancy_id, previous, status, *, day=None, limit=None, letter=None, error=None
    ):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if (
                day
                and db.execute(
                    "SELECT count(*) FROM vacancies WHERE sent_day=?",
                    (day,),
                ).fetchone()[0]
                >= limit
            ):
                return "limit"
            states = ",".join("?" for _ in previous)
            changed = db.execute(
                f"""UPDATE vacancies SET status=?,letter=COALESCE(?,letter),sent_day=?,sent_at=?,
                    error=COALESCE(?,error)
                    WHERE id=? AND status IN ({states})""",
                (
                    status,
                    letter,
                    day,
                    int(time.time()) if day else None,
                    error,
                    vacancy_id,
                    *previous,
                ),
            ).rowcount
            if changed:
                self.event(db, vacancy_id, status)
            return "ok" if changed else "stale"

    def browser_candidates(self, *, policy, resume_hash, min_score, min_skills, source="browser"):
        with self.connection() as db:
            yield from db.execute(
                """SELECT * FROM vacancies WHERE status IN ('draft', 'approved')
                   AND sent_day IS NULL AND json_extract(details, '$.source')=?
                   AND json_extract(result, '$.auto_policy')=?
                   AND json_extract(result, '$.resume_hash')=?
                   AND json_type(result, '$.score')='integer'
                   AND json_type(result, '$.skills_score')='integer'
                   AND json_extract(result, '$.score')>=?
                   AND json_extract(result, '$.skills_score')>=?
                   AND letter=json_extract(result, '$.letter')
                   ORDER BY found_at,id""",
                (source, policy, resume_hash, min_score, min_skills),
            )

    def next_browser_application(self, *, policy=None, resume_hash=None, min_score=0, min_skills=0):
        if policy is not None:
            candidates = self.browser_candidates(
                policy=policy, resume_hash=resume_hash, min_score=min_score, min_skills=min_skills
            )
            with closing(candidates) as rows:
                return next(rows, None)
        with self.connection() as db:
            return db.execute(
                """SELECT * FROM vacancies WHERE status='approved'
                   AND json_extract(details, '$.source')='browser'
                   ORDER BY found_at,id LIMIT 1"""
            ).fetchone()

    def refresh_auto_drafts(self, policy, resume_hash, *, min_score=None, source="browser"):
        with self.connection() as db:
            rows = db.execute(
                """SELECT id FROM vacancies WHERE status IN ('draft', 'approved', 'filtered')
                   AND json_extract(details, '$.source')=:source
                   AND (:source='browser' OR json_extract(details, '$.auto_discovered')=1)
                   AND found_at>=:recent AND sent_day IS NULL
                   AND (
                       (status IN ('draft','approved') AND letter=json_extract(result, '$.letter')
                        AND (COALESCE(json_extract(result, '$.analysis_policy'), '')<>:policy
                             OR COALESCE(json_extract(result, '$.resume_hash'), '')<>:digest))
                       OR (status='filtered' AND :min_score IS NOT NULL
                           AND json_type(result, '$.score')='integer'
                           AND (COALESCE(json_extract(result, '$.resume_hash'), '')<>:digest
                                OR (json_extract(result, '$.score')>=:min_score
                                    AND json_extract(result, '$.filter_reason') IS NULL
                                    AND COALESCE(json_extract(result, '$.analysis_threshold'),101)
                                        > :min_score)))
                   )
                   ORDER BY found_at DESC,id LIMIT 25""",
                {
                    "recent": int(time.time()) - 7 * 86400,
                    "policy": policy,
                    "digest": resume_hash,
                    "min_score": min_score,
                    "source": source,
                },
            ).fetchall()
            for row in rows:
                db.execute(
                    """UPDATE vacancies SET status='queued',attempts=0,retry_at=0,error=NULL
                       WHERE id=? AND status IN ('draft', 'approved', 'filtered')""",
                    (row["id"],),
                )
                self.event(db, row["id"], "queued")

    def hh_application_overview(self, day):
        hh_sources = "json_extract(details, '$.source') IN ('browser','mini_hh','hh')"
        with self.connection() as db:
            totals = db.execute(
                f"""SELECT count(*) FILTER(WHERE status='sent') AS confirmed_total,
                           count(*) FILTER(WHERE status='sent' AND sent_day=?) AS confirmed_today,
                           count(*) FILTER(WHERE sent_day=? AND status<>'reported') AS attempted_today,
                           count(*) FILTER(WHERE status='queued') AS pending,
                           count(*) FILTER(WHERE status='unknown') AS unknown
                    FROM vacancies WHERE {hh_sources}""",
                (day, day),
            ).fetchone()
            used = db.execute("SELECT count(*) FROM vacancies WHERE sent_day=?", (day,)).fetchone()[
                0
            ]
            recent = db.execute(
                f"""SELECT id, json_extract(details, '$.title') AS title, status,
                           COALESCE(sent_at, analyzed_at, found_at) AS at
                    FROM vacancies WHERE {hh_sources}
                    AND status IN ('sent','unknown','questions','duplicate')
                    ORDER BY at DESC,id DESC LIMIT 6"""
            ).fetchall()
            examples = db.execute(
                f"""SELECT * FROM vacancies WHERE {hh_sources}
                    AND status NOT IN ('sent','reported','duplicate','rejected','sending')
                    ORDER BY found_at DESC,id DESC LIMIT 201"""
            ).fetchall()
        return dict(totals) | {"used_today": used, "recent": list(map(dict, recent))}, examples

    def complete_browser_application(self, vacancy_id, status, *, previous="sending"):
        with self.connection() as db:
            changed = db.execute(
                "UPDATE vacancies SET status=? WHERE id=? AND status=?",
                (status, vacancy_id, previous),
            ).rowcount
            if changed:
                self.event(db, vacancy_id, status)

    def recover_browser_applications(self):
        with self.connection() as db:
            rows = db.execute(
                """SELECT id,json_extract(details, '$.source') AS source
                   FROM vacancies WHERE status='sending'"""
            ).fetchall()
            db.execute("UPDATE vacancies SET status='unknown' WHERE status='sending'")
            for row in rows:
                self.event(db, row["id"], "unknown")
            if any(row["source"] in {"browser", "mini_hh"} for row in rows):
                db.executemany(
                    "INSERT OR REPLACE INTO state VALUES (?,?)",
                    (
                        ("browser_auto_enabled", json.dumps(False)),
                        (
                            "browser_notice",
                            json.dumps(
                                "После перезапуска проверьте результат незавершённого отклика HH."
                            ),
                        ),
                    ),
                )
            if any(row["source"] == "superjob" for row in rows):
                db.executemany(
                    "INSERT OR REPLACE INTO state VALUES (?,?)",
                    (
                        ("superjob_auto_enabled", json.dumps(False)),
                        (
                            "superjob_auto_notice",
                            json.dumps(
                                "Проверьте незавершённый отклик в личном кабинете SuperJob."
                            ),
                        ),
                    ),
                )

    def cached(self, key):
        with self.connection() as db:
            row = db.execute("SELECT result FROM cache WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else None

    def cache(self, key, result):
        with self.connection() as db:
            db.execute(
                "INSERT OR REPLACE INTO cache VALUES(?,?,?)",
                (key, json.dumps(result), int(time.time())),
            )
            db.execute("""DELETE FROM cache WHERE key IN
                          (SELECT key FROM cache ORDER BY at DESC,rowid DESC LIMIT -1 OFFSET 250)""")

    def counts(self, day):
        with self.connection() as db:
            counts = dict(db.execute("SELECT status,count(*) FROM vacancies GROUP BY status"))
            today = db.execute(
                "SELECT count(*) FROM vacancies WHERE sent_day=?", (day,)
            ).fetchone()[0]
            return counts, today

    def errors(self):
        with self.connection() as db:
            return db.execute(
                "SELECT id,error FROM vacancies WHERE error IS NOT NULL ORDER BY retry_at DESC LIMIT 5"
            ).fetchall()
