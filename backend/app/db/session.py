from __future__ import annotations

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass

from fastapi import Request
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def normalize_database_url(url: str) -> str:
    normalized = (url or "").strip()
    if not normalized:
        normalized = "postgresql+asyncpg://reportx:reportx@localhost:5432/reportx"
    if normalized.startswith("postgres://"):
        return normalized.replace("postgres://", "postgresql+asyncpg://", 1)
    if normalized.startswith("postgresql://") and "+asyncpg" not in normalized:
        return normalized.replace("postgresql://", "postgresql+asyncpg://", 1)
    return normalized


@dataclass(frozen=True)
class DatabaseSettings:
    url: str
    echo: bool = False

    @classmethod
    def from_env(cls) -> DatabaseSettings:
        return cls(
            url=normalize_database_url(os.getenv("DATABASE_URL", "")),
            echo=os.getenv("SQLALCHEMY_ECHO", "0").strip().lower() in {"1", "true", "yes"},
        )


class DatabaseManager:
    def __init__(self, settings: DatabaseSettings) -> None:
        self.settings = settings
        self.engine: AsyncEngine = create_async_engine(
            settings.url,
            echo=settings.echo,
            future=True,
        )
        self.session_factory = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )

    async def dispose(self) -> None:
        await self.engine.dispose()


def build_database_manager(settings: DatabaseSettings | None = None) -> DatabaseManager:
    return DatabaseManager(settings or DatabaseSettings.from_env())


async def get_db_session(request: Request) -> AsyncIterator[AsyncSession]:
    database: DatabaseManager = request.app.state.database
    async with database.session_factory() as session:
        yield session
