"""
trading_engine/db/database.py
SQLAlchemy engine, session factory, and base model.
"""

from contextlib import contextmanager
from typing import Generator

import sqlalchemy as sa
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, Session, DeclarativeBase

from config.settings import DATABASE_URL


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
_engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
    echo=False,
)

SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Context manager helper
# ---------------------------------------------------------------------------
@contextmanager
def get_db() -> Generator[Session, None, None]:
    """Provide a transactional database session."""
    db: Session = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db() -> None:
    """Create all tables if they don't exist (applies SQLAlchemy models).
    For the raw SQL schema, run db/schema.sql separately."""
    Base.metadata.create_all(bind=_engine)


def health_check() -> bool:
    """Return True if database is reachable."""
    try:
        with _engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
