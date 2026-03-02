"""
Settings page routes:
- GET /settings/ — main settings page with session status
- POST /settings/check-amazon — validate Amazon cookies against API
- POST /settings/logout-amazon — clear Amazon session data
- POST /settings/logout-instacart — clear Instacart session data
"""

import json
import logging
import shutil

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from alexacart.app import templates
from alexacart.config import settings
from alexacart.db import get_db
from alexacart.models import Alias, GroceryItem, OrderLog, PreferredProduct

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/settings", tags=["settings"])


def _read_amazon_status() -> dict:
    """Read Amazon cookie file and extract status info (no API calls)."""
    path = settings.cookies_path
    info = {"logged_in": False, "cookie_count": 0}
    if not path.exists():
        return info
    try:
        data = json.loads(path.read_text())
        cookies = data.get("cookies", {})
        info["logged_in"] = bool(cookies)
        info["cookie_count"] = len(cookies)
        info["source"] = data.get("source", "unknown")

        reg = data.get("registration", {})
        if reg:
            info["has_refresh_token"] = bool(reg.get("refresh_token"))
            info["registered_at"] = reg.get("registered_at", "")
            serial = reg.get("device_serial", "")
            info["device_serial"] = f"{serial[:8]}..." if len(serial) > 8 else serial
        else:
            info["has_refresh_token"] = False
    except (json.JSONDecodeError, KeyError):
        pass
    return info


def _read_instacart_status() -> dict:
    """Read Instacart cookie file and extract status info (no API calls)."""
    path = settings.resolved_local_data_dir / "instacart_cookies.json"
    info = {"logged_in": False, "cookie_count": 0}
    if not path.exists():
        return info
    try:
        data = json.loads(path.read_text())
        cookies = data.get("cookies", {})
        info["logged_in"] = bool(cookies)
        info["cookie_count"] = len(cookies)
        info["extracted_at"] = data.get("extracted_at", "")
        info["cart_id"] = data.get("cart_id", "")

        params = data.get("session_params", {})
        info["store_slug"] = params.get("retailer_slug", "")
        info["postal_code"] = params.get("postal_code", "")
        info["has_shop_id"] = bool(params.get("shop_id"))
        info["has_zone_id"] = bool(params.get("zone_id"))
        info["has_inventory_token"] = bool(params.get("retailer_inventory_session_token"))
        info["has_location_id"] = bool(params.get("retailer_location_id"))
    except (json.JSONDecodeError, KeyError):
        pass
    return info


def _get_db_stats(db: Session) -> dict:
    """Get row counts for all tables."""
    return {
        "grocery_items": db.query(func.count(GroceryItem.id)).scalar() or 0,
        "aliases": db.query(func.count(Alias.id)).scalar() or 0,
        "preferred_products": db.query(func.count(PreferredProduct.id)).scalar() or 0,
        "order_log": db.query(func.count(OrderLog.id)).scalar() or 0,
    }


@router.get("/")
async def settings_page(request: Request, db: Session = Depends(get_db)):
    amazon = _read_amazon_status()
    instacart = _read_instacart_status()
    db_stats = _get_db_stats(db)
    return templates.TemplateResponse("settings.html", {
        "request": request,
        "amazon": amazon,
        "instacart": instacart,
        "db_stats": db_stats,
        "config": {
            "instacart_store": settings.instacart_store,
            "alexa_list_name": settings.alexa_list_name,
            "data_dir": str(settings.resolved_data_dir),
            "local_data_dir": str(settings.resolved_local_data_dir),
        },
    })


@router.post("/check-amazon")
async def check_amazon():
    """Validate Amazon cookies against the real Alexa API."""
    from alexacart.alexa.auth import load_cookies, validate_alexa_cookies

    cookie_data = load_cookies()
    if not cookie_data:
        return HTMLResponse(
            '<span class="badge badge-error">No cookies</span>'
        )

    valid = await validate_alexa_cookies(cookie_data)
    if valid:
        return HTMLResponse(
            '<span class="badge badge-matched">Valid</span>'
        )
    return HTMLResponse(
        '<span class="badge badge-error">Expired</span>'
    )


@router.post("/logout-amazon")
async def logout_amazon():
    """Clear Amazon session data (cookies + Chrome profile)."""
    cookies_path = settings.cookies_path
    if cookies_path.exists():
        cookies_path.unlink()
        logger.info("Deleted %s", cookies_path)

    profile_dir = settings.resolved_local_data_dir / "nodriver-amazon"
    if profile_dir.exists():
        shutil.rmtree(profile_dir, ignore_errors=True)
        logger.info("Deleted Amazon Chrome profile: %s", profile_dir)

    return HTMLResponse(
        '<div class="alert alert-success">'
        "Amazon session cleared. You'll be prompted to log in on your next order."
        "</div>"
    )


@router.post("/logout-instacart")
async def logout_instacart():
    """Clear Instacart session data (cookies + Chrome profile)."""
    from alexacart.instacart.auth import _cookies_path

    path = _cookies_path()
    if path.exists():
        path.unlink()
        logger.info("Deleted %s", path)

    profile_dir = settings.resolved_local_data_dir / "nodriver-instacart"
    if profile_dir.exists():
        shutil.rmtree(profile_dir, ignore_errors=True)
        logger.info("Deleted Instacart Chrome profile: %s", profile_dir)

    return HTMLResponse(
        '<div class="alert alert-success">'
        "Instacart session cleared. You'll be prompted to log in on your next order."
        "</div>"
    )
