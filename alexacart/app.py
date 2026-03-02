import logging
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from alexacart.config import settings
from alexacart.db import init_db

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

NYC_TZ = ZoneInfo("America/New_York")


def _to_nyc(dt: datetime) -> datetime:
    """Convert a naive-UTC or aware-UTC datetime to America/New_York."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(NYC_TZ)


templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["instacart_store"] = settings.instacart_store
templates.env.filters["to_nyc"] = _to_nyc


def create_app() -> FastAPI:

    app = FastAPI(title="AlexaCart")

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    init_db()

    from alexacart.routes.order import router as order_router
    from alexacart.routes.preferences import router as preferences_router
    from alexacart.routes.settings import router as settings_router

    app.include_router(order_router)
    app.include_router(preferences_router)
    app.include_router(settings_router)

    @app.get("/")
    async def root():
        return RedirectResponse(url="/order/")

    return app
