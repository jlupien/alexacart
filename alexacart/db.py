import logging

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from alexacart.config import settings
from alexacart.models import Base

logger = logging.getLogger(__name__)

engine = create_engine(settings.database_url, echo=False)
SessionLocal = sessionmaker(bind=engine)


def init_db() -> None:
    settings.resolved_data_dir.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine)
    _migrate_order_log()


def _migrate_order_log() -> None:
    """Add columns introduced after initial schema."""
    insp = inspect(engine)
    if "order_log" not in insp.get_table_names():
        return
    columns = {c["name"] for c in insp.get_columns("order_log")}
    with engine.begin() as conn:
        if "skipped" not in columns:
            logger.info("Migrating order_log: adding 'skipped' column")
            conn.execute(text("ALTER TABLE order_log ADD COLUMN skipped BOOLEAN DEFAULT 0"))


def get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
