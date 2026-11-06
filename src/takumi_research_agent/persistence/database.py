from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import Session, sessionmaker

from .models import Base


def create_database(database_url: str):
    options = {"pool_pre_ping": True}
    if database_url.startswith("sqlite"):
        options["connect_args"] = {"check_same_thread": False}
        if ":memory:" in database_url:
            options["poolclass"] = StaticPool
    else:
        options.update(pool_size=5, max_overflow=10)
    engine = create_engine(database_url, **options)
    sessions = sessionmaker(engine, expire_on_commit=False, class_=Session)
    return engine, sessions


def initialize_for_development(engine) -> None:
    """Convenience only for local prototypes; deployments use Alembic migrations."""
    Base.metadata.create_all(engine)


def session_dependency(sessions: sessionmaker) -> Generator[Session, None, None]:
    with sessions() as session:
        yield session
