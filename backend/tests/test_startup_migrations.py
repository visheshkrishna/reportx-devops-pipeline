from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import main as main_mod


@pytest.mark.asyncio
async def test_run_alembic_migrations_uses_active_interpreter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_process = SimpleNamespace(
        communicate=AsyncMock(return_value=(b"ok", b"")),
        returncode=0,
    )

    called_args: dict[str, object] = {}

    async def fake_exec(*args, **kwargs):
        called_args["args"] = args
        called_args["kwargs"] = kwargs
        return fake_process

    monkeypatch.setattr(main_mod.asyncio, "create_subprocess_exec", fake_exec)

    await main_mod._run_alembic_migrations(".", timeout=60)

    args = called_args["args"]
    assert args[0] == sys.executable
    assert args[1:5] == ("-m", "alembic", "upgrade", "head")


@pytest.mark.asyncio
async def test_app_lifespan_defaults_cleanup_interval_to_five_minutes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeScheduler:
        def add_job(self, _func, _trigger, *, minutes: int, **_kwargs):
            captured["minutes"] = minutes

        def start(self):
            captured["started"] = True

        def shutdown(self):
            captured["shutdown"] = True

    class FakeDatabase:
        def session_factory(self):
            raise AssertionError("session_factory should not be called in this test")

        async def dispose(self):
            captured["disposed"] = True

    async def fake_run_alembic_migrations(_project_root: str, timeout: int):
        captured["timeout"] = timeout

    fake_app = SimpleNamespace(state=SimpleNamespace(database=FakeDatabase()))

    monkeypatch.delenv("CLEANUP_INTERVAL_MINUTES", raising=False)
    monkeypatch.setattr(main_mod, "AsyncIOScheduler", lambda: FakeScheduler())
    monkeypatch.setattr(main_mod, "_run_alembic_migrations", fake_run_alembic_migrations)

    async with main_mod.app_lifespan(fake_app):
        pass

    assert captured["minutes"] == 5
    assert captured["started"] is True
    assert captured["shutdown"] is True
    assert captured["disposed"] is True
