import json
import sqlite3
import time
from contextlib import contextmanager

ACTIVE = ("queued", "draft", "approved", "deferred", "unknown", "questions", "error")
HISTORY = ("reported", "rejected", "filtered", "duplicate")
LABELS = {
    "queued": "Ожидает анализа",
    "draft": "Подготовлен черновик",
    "approved": "Готов к ручной отправке",
    "deferred": "Отложена",
    "unknown": "Результат неизвестен",
    "reported": "Отправка отмечена владельцем",
    "filtered": "Не подходит",
    "rejected": "Отклонена владельцем",
    "questions": "Требуются ответы",
    "error": "Ошибка анализа",
    "duplicate": "Уже отправлялся по данным владельца",
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

    def add(self, vacancy_id, details, review=False):
        with self.connection() as db:
            inserted = db.execute(
                "INSERT OR IGNORE INTO vacancies(id,details,status,found_at) VALUES(?,?,?,?)",
                (vacancy_id, json.dumps(details), "queued", int(time.time())),
            ).rowcount
            if inserted:
                self.event(db, vacancy_id, "queued")
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

    @staticmethod
    def event(db, vacancy_id, action):
        db.execute(
            "INSERT INTO events(vacancy,action,at) VALUES(?,?,?)",
            (vacancy_id, action, int(time.time())),
        )

    def vacancy(self, vacancy_id):
        with self.connection() as db:
            return db.execute("SELECT * FROM vacancies WHERE id=?", (vacancy_id,)).fetchone()

    def page(self, statuses, page, size=5):
        values = ",".join("?" for _ in statuses)
        where = f"WHERE status IN ({values})" if statuses else ""
        with self.connection() as db:
            return db.execute(
                f"SELECT * FROM vacancies {where} ORDER BY found_at DESC,id DESC LIMIT ? OFFSET ?",
                (*statuses, size + 1, page * size),
            ).fetchall()

    def pending(self):
        with self.connection() as db:
            return db.execute(
                """SELECT * FROM vacancies WHERE status='queued' AND retry_at<=?
                   ORDER BY found_at,id LIMIT 1""",
                (int(time.time()),),
            ).fetchone()

    def finish_analysis(self, vacancy_id, status, result, letter):
        with self.connection() as db:
            changed = db.execute(
                """UPDATE vacancies SET status=?,result=?,letter=?,analyzed_at=?,error=NULL
                   WHERE id=? AND status='queued'""",
                (status, json.dumps(result), letter, int(time.time()), vacancy_id),
            ).rowcount
            if changed:
                self.event(db, vacancy_id, status)

    def failure(self, vacancy_id, attempt, message, delay):
        with self.connection() as db:
            status = "queued" if delay else "error"
            db.execute(
                """UPDATE vacancies SET status=?,attempts=?,retry_at=?,error=?
                   WHERE id=? AND status='queued'""",
                (status, attempt, int(time.time()) + delay, message, vacancy_id),
            )
            self.event(db, vacancy_id, "analysis_error")

    def transition(self, vacancy_id, previous, status, *, day=None, limit=None, letter=None):
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
                f"""UPDATE vacancies SET status=?,letter=COALESCE(?,letter),sent_day=?,sent_at=?
                    WHERE id=? AND status IN ({states})""",
                (status, letter, day, int(time.time()) if day else None, vacancy_id, *previous),
            ).rowcount
            if changed:
                self.event(db, vacancy_id, status)
            return "ok" if changed else "stale"

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
