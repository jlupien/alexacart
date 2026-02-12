from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from alexacart.config import settings
from alexacart.models import Base

engine = create_engine(settings.database_url, echo=False)
SessionLocal = sessionmaker(bind=engine)


def init_db() -> None:
    settings.resolved_data_dir.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine)


def get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
