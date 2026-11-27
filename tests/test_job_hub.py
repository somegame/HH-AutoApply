import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.database import Database
from app.job_hub import JobHub
from app.services import ServiceError


def test_hub_processes_supported_sources_independently_and_persists_opt_in(tmp_path):
    async def run():
        config = SimpleNamespace(defaults={"interval": 30}, superjob_key="fixture")
        db = Database(tmp_path)
        db.set("browser_notice", "HH вернул HTTP 403. Операции остановлены.")
        hub = JobHub(config, db, None, None)
        with pytest.raises(ValueError):
            hub.configure("hh", True)
        hub.configure("trudvsem", True)
        hub.configure("superjob", True)

        async def provider(_settings, source):
            if source == "superjob":
                raise ServiceError("SuperJob временно недоступен.", 300)
            return [("trudvsem:fixture", {"source": source, "description": "Готовить бюджет."})]

        hub.sources.search = AsyncMock(side_effect=provider)
        await hub.tick(None)
        assert hub.sources.search.await_count == 2
        assert db.vacancy("trudvsem:fixture")["status"] == "queued"
        assert db.get("processing")
        restarted = JobHub(config, Database(tmp_path), None, None)
        loaded = restarted.state()
        assert loaded["trudvsem"]["enabled"] and loaded["trudvsem"]["found"] == 1
        assert loaded["superjob"]["enabled"] and "недоступен" in loaded["superjob"]["message"]
        assert loaded["superjob"]["next_at"] > loaded["superjob"]["last_at"]
        await hub.tick(None)
        assert hub.sources.search.await_count == 2
        restarted.configure("trudvsem", False)
        assert not hub.state()["trudvsem"]["enabled"]
        assert "403" in db.get("browser_notice")

    asyncio.run(run())
