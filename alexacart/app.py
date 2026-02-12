from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from alexacart.db import init_db

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def create_app() -> FastAPI:
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
