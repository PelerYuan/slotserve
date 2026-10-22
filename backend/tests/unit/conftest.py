"""L1 unit tests: pure logic, no app, no network, no subprocesses; the whole layer stays under 5 s.

Everything under ``tests/unit/`` gets the ``unit`` marker; when the full suite is collected, the other
tests get ``integration``. Code that reads the database gets a private in-memory SQLite session (``db``),
never the app's engine.
"""

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from slotserve import orm
from slotserve.config import Settings
from slotserve.db import Base

UNIT_DIR = Path(__file__).resolve().parent
SAMPLES_DIR = UNIT_DIR.parent / "samples"
# Fixtures that start the app or talk to it; a unit test that needs them belongs in tests/<feature>/.
APP_FIXTURES = {"app", "make_client"}


def pytest_collection_modifyitems(config, items):
    for item in items:
        if UNIT_DIR in Path(item.path).resolve().parents:
            if APP_FIXTURES & set(getattr(item, "fixturenames", ())):
                raise pytest.UsageError(f"{item.nodeid}: unit tests must not use the app fixtures {APP_FIXTURES}")
            item.add_marker(pytest.mark.unit)
        else:
            item.add_marker(pytest.mark.integration)


@pytest.fixture
def settings():
    """Settings with defaults only (no environment variables, no .env), overridable per test."""

    def make(**overrides) -> Settings:
        return Settings.model_construct(**overrides)

    return make


@pytest.fixture
def samples_dir() -> Path:
    return SAMPLES_DIR


def _schema_script() -> str:
    """The full schema as one SQL script, built once (create_all per test would dominate the layer's time)."""
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    raw = engine.raw_connection()
    try:
        conn: sqlite3.Connection = raw.driver_connection
        return "\n".join(stmt for stmt in conn.iterdump() if stmt.startswith("CREATE"))
    finally:
        raw.close()
        engine.dispose()


SCHEMA = _schema_script()


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        raw = await conn.get_raw_connection()
        await raw.driver_connection.executescript(SCHEMA)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        yield session
    await engine.dispose()


class Rows:
    """Adds rows with only the columns a test cares about (and flushes, so ids are set)."""

    def __init__(self, db: AsyncSession):
        self.db = db
        self._n = 0

    async def _add(self, row):
        self.db.add(row)
        await self.db.flush()
        return row

    def _name(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}{self._n}"

    async def user(self, username: str | None = None, is_admin: bool = False) -> orm.User:
        return await self._add(orm.User(username=username or self._name("u"), password_hash="x", is_admin=is_admin))

    async def lane(self, name: str | None = None, gpus: str = "", **kw) -> orm.Lane:
        return await self._add(orm.Lane(name=name or self._name("lane"), gpu_indices=gpus, **kw))

    async def model(self, name: str | None = None, size_bytes: int = 1_500_000_000, **kw) -> orm.LlmModel:
        kw.setdefault("status", "ready")
        name = name or self._name("m")
        return await self._add(orm.LlmModel(name=name, file_path=f"/models/{name}.gguf", size_bytes=size_bytes, **kw))

    async def preset(self, model: orm.LlmModel, name: str = "p") -> orm.ModelPreset:
        return await self._add(orm.ModelPreset(model_id=model.id, name=name))

    async def booking(self, user, start, end, lane=None, model=None, **kw) -> orm.Reservation:
        return await self._add(
            orm.Reservation(
                user_id=user.id,
                lane_id=lane.id if lane is not None else None,
                model_id=model.id if model is not None else None,
                start_at=start,
                end_at=end,
                **kw,
            )
        )

    async def sample(self, start, lane, kind: str, user=None, gpu: int = 0, seconds: int = 60, busy: int = 0, **kw):
        return await self._add(
            orm.UsageSample(
                start=start,
                seconds=seconds,
                lane_id=lane.id,
                gpu_index=gpu,
                kind=kind,
                user_id=user.id if user is not None else None,
                metered_seconds=kw.pop("metered", seconds),
                busy_seconds=busy,
                util_seconds=kw.pop("util", 0.0),
                energy_wh=kw.pop("wh", 0.0),
                **kw,
            )
        )


@pytest.fixture
def rows(db) -> Rows:
    return Rows(db)
