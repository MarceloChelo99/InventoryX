"""Engine / session helpers.

SQLite via SQLAlchemy — one file, zero setup (design doc). Use ``init_db`` for
quick local bootstrapping; production deployments run Alembic migrations
instead (see ``inventoryx/db/migrations``).
"""

from __future__ import annotations

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from inventoryx.db.models import Base

DEFAULT_URL = "sqlite:///inventoryx.db"


def make_engine(url: str = DEFAULT_URL, echo: bool = False) -> Engine:
    return create_engine(url, echo=echo, future=True)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, future=True, expire_on_commit=False)


def init_db(engine: Engine) -> None:
    """Create all tables. Convenience for tests / local; prefer migrations."""
    Base.metadata.create_all(engine)
