"""
Alexa cookie-based authentication.

Cookies are extracted automatically from the browser-use persistent Chrome
session during the order flow (ensure_amazon_logged_in → get_amazon_cookies).

On-demand refresh: called automatically when cookies expire (401 from Alexa API).
Uses the Node.js alexa-cookie2 sidecar if available.
"""

import json
import logging
import subprocess
from pathlib import Path

from alexacart.config import settings

logger = logging.getLogger(__name__)


def _cookies_path() -> Path:
    return settings.cookies_path


def load_cookies() -> dict | None:
    """Load saved cookies from disk. Returns dict with 'cookies' key or None."""
    path = _cookies_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        if "cookies" in data and data["cookies"]:
            return data
    except (json.JSONDecodeError, KeyError):
        pass
    return None


def save_cookies(data: dict) -> None:
    """Save cookies to disk."""
    path = _cookies_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))
    logger.info("Cookies saved to %s", path)


def get_cookie_header(data: dict) -> dict[str, str]:
    """Build HTTP cookie header from saved cookie data."""
    cookies = data.get("cookies", {})
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
    return {"Cookie": cookie_str}


def try_refresh_via_sidecar() -> dict | None:
    """
    Try to refresh cookies using the Node.js alexa-cookie2 sidecar.
    Returns cookie data if successful, None if sidecar unavailable or refresh failed.
    """
    sidecar_path = settings.base_dir / "cookie_refresh" / "refresh.js"
    if not sidecar_path.exists():
        logger.info("Cookie refresh sidecar not found at %s", sidecar_path)
        return None

    existing = load_cookies()
    if not existing:
        logger.info("No existing cookies to refresh")
        return None

    try:
        result = subprocess.run(
            ["node", str(sidecar_path)],
            input=json.dumps(existing),
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(sidecar_path.parent),
        )
        if result.returncode == 0:
            new_data = json.loads(result.stdout)
            save_cookies(new_data)
            logger.info("Cookies refreshed via sidecar")
            return new_data
        else:
            logger.warning("Sidecar refresh failed: %s", result.stderr)
    except FileNotFoundError:
        logger.warning("Node.js not found, cannot run cookie refresh sidecar")
    except subprocess.TimeoutExpired:
        logger.warning("Cookie refresh sidecar timed out")
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Cookie refresh sidecar error: %s", e)

    return None


async def ensure_valid_cookies() -> dict:
    """
    Ensure we have valid cookies. Try loading, then refreshing, then prompt for login.
    Returns cookie data dict.
    Raises RuntimeError if no valid cookies can be obtained.
    """
    data = load_cookies()
    if data:
        return data

    # Try refreshing
    import asyncio
    data = await asyncio.to_thread(try_refresh_via_sidecar)
    if data:
        return data

    raise RuntimeError(
        "No valid Alexa cookies found. Start an order to log in via the browser."
    )
