"""Engine, session and create/drop/reset for reportfix.db (never touches llm_cache.db)."""
import os
from pathlib import Path

from sqlmodel import Session, SQLModel, create_engine

from app import models  # noqa: F401  (registers tables)

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = os.environ.get("REPORTFIX_DB") or str(ROOT / "reportfix.db")

engine = create_engine(
    f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False}
)


def get_session():
    with Session(engine) as session:
        yield session


def create_tables() -> None:
    SQLModel.metadata.create_all(engine)


def schema_is_current() -> bool:
    """False when an older reportfix.db lacks a column the models now have."""
    from sqlalchemy import inspect
    insp = inspect(engine)
    for name, table in SQLModel.metadata.tables.items():
        if not insp.has_table(name):
            return False
        have = {c["name"] for c in insp.get_columns(name)}
        if not {c.name for c in table.columns} <= have:
            return False
    return True


def drop_tables() -> None:
    SQLModel.metadata.drop_all(engine)


def reset_all() -> None:
    """Drop and reseed the app database. Instant: seeding makes no AI calls."""
    from app.seed import seed

    drop_tables()
    create_tables()
    with Session(engine) as session:
        seed(session)
