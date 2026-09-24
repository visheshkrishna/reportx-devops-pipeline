from app.db.session import DatabaseManager
from app.main import create_app


def test_create_app_bootstraps_database_manager(monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://reportx:reportx@localhost:5432/reportx",
    )

    app = create_app()

    assert isinstance(app.state.database, DatabaseManager)
    assert (
        app.state.database.settings.url
        == "postgresql+asyncpg://reportx:reportx@localhost:5432/reportx"
    )
    assert app.state.database.session_factory is not None
