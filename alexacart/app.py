import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from alexacart.config import settings
from alexacart.db import init_db

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["instacart_store"] = settings.instacart_store


class _JudgeWarningFilter(logging.Filter):
    """Suppress noisy 'Simple judge failed' warnings from browser-use."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "Simple judge failed" not in record.getMessage()


def create_app() -> FastAPI:
    logging.getLogger("Agent").addFilter(_JudgeWarningFilter())

    app = FastAPI(title="AlexaCart")

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    init_db()

    from alexacart.routes.order import router as order_router
    from alexacart.routes.preferences import router as preferences_router

    app.include_router(order_router)
    app.include_router(preferences_router)

    @app.get("/")
    async def root():
        return RedirectResponse(url="/order/")

    return app
