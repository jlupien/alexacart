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
    settings.resolved_local_data_dir.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine)
    _migrate_order_log()
    _migrate_preferred_products()
    _cleanup_urlless_preferences()


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


def _migrate_preferred_products() -> None:
    """Add columns introduced after initial schema."""
    insp = inspect(engine)
    if "preferred_products" not in insp.get_table_names():
        return
    columns = {c["name"] for c in insp.get_columns("preferred_products")}
    with engine.begin() as conn:
        if "size" not in columns:
            logger.info("Migrating preferred_products: adding 'size' column")
            conn.execute(text("ALTER TABLE preferred_products ADD COLUMN size TEXT"))


def _cleanup_urlless_preferences() -> None:
    """Delete preferred products with no URL and re-compact ranks."""
    with Session(engine) as db:
        from alexacart.models import PreferredProduct

        urlless = (
            db.query(PreferredProduct)
            .filter(
                (PreferredProduct.product_url.is_(None))
                | (PreferredProduct.product_url == "")
            )
            .all()
        )
        if not urlless:
            return

        affected_item_ids = {p.grocery_item_id for p in urlless}
        for p in urlless:
            db.delete(p)
        db.flush()

        # Re-compact ranks for affected grocery items
        for item_id in affected_item_ids:
            remaining = (
                db.query(PreferredProduct)
                .filter(PreferredProduct.grocery_item_id == item_id)
                .order_by(PreferredProduct.rank)
                .all()
            )
            for i, p in enumerate(remaining, 1):
                p.rank = i

        db.commit()
        logger.info(
            "Cleaned up %d URL-less preferred products for %d grocery items",
            len(urlless),
            len(affected_item_ids),
        )


def get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
