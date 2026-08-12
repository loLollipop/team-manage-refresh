import unittest
from unittest.mock import AsyncMock, patch

from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from app import database
from app import main
from app import db_migrations


class _AsyncContext:
    def __init__(self, value=None, error=None):
        self.value = value
        self.error = error

    async def __aenter__(self):
        if self.error is not None:
            raise self.error
        return self.value

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeConnection:
    def __init__(self):
        self.execute = AsyncMock()
        self.run_sync = AsyncMock()


class _FakeEngine:
    def __init__(self, connection=None, error=None):
        self.connection = connection
        self.error = error

    def begin(self):
        return _AsyncContext(self.connection, self.error)

    def connect(self):
        return _AsyncContext(self.connection, self.error)


class DatabaseCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_init_db_does_not_run_sqlite_pragma_for_non_sqlite_database(self):
        connection = _FakeConnection()
        engine = _FakeEngine(connection=connection)

        with patch("app.database._is_sqlite", False), patch("app.database.engine", engine):
            await database.init_db()

        connection.execute.assert_not_awaited()
        connection.run_sync.assert_awaited_once()


class MigrationCompatibilityTests(unittest.TestCase):
    def test_get_db_path_returns_none_for_non_sqlite_database(self):
        with patch(
            "app.config.settings.database_url",
            "postgresql+asyncpg://user:password@db.example.com/app",
        ):
            self.assertIsNone(db_migrations.get_db_path())

    def test_get_db_path_returns_none_for_in_memory_sqlite_database(self):
        with patch(
            "app.config.settings.database_url",
            "sqlite+aiosqlite:///:memory:",
        ):
            self.assertIsNone(db_migrations.get_db_path())


class StartupAndHealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_lifespan_fails_fast_when_database_initialization_fails(self):
        with patch(
            "app.main.init_db",
            new=AsyncMock(side_effect=RuntimeError("database unavailable")),
        ):
            with self.assertRaisesRegex(RuntimeError, "数据库初始化失败"):
                async with main.lifespan(main.app):
                    pass

    async def test_health_check_returns_503_when_database_cannot_be_reached(self):
        engine = _FakeEngine(error=SQLAlchemyError("database unavailable"))

        with patch("app.main.engine", engine, create=True):
            response = await main.health_check()

        self.assertIsInstance(response, JSONResponse)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.body, b'{"status":"unhealthy"}')
