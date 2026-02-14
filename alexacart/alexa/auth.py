"""
Alexa cookie-based authentication.

Amazon's bot detection flags browser-use/Playwright sessions and issues
limited-scope session tokens that can't access the Shopping List API.
We use nodriver (undetectable Chrome) for Amazon login + cookie extraction.

On-demand refresh: called automatically when cookies expire (401 from Alexa API).
"""

import asyncio
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


async def extract_cookies_via_nodriver(on_status=None) -> dict:
    """
    Open an undetectable Chrome instance via nodriver, ensure the user is
    logged into Amazon, and extract session cookies.

    nodriver bypasses Amazon's bot detection that flags browser-use/Playwright
    sessions and limits their API access.

    Args:
        on_status: Optional callback(str) for progress messages.

    Returns cookie data dict with 'cookies' key.
    """
    import shutil

    import nodriver as uc

    def _status(msg):
        logger.info(msg)
        if on_status:
            on_status(msg)

    profile_dir = settings.resolved_data_dir / "nodriver-amazon"
    profile_dir.mkdir(parents=True, exist_ok=True)

    if settings.debug_clear_amazon_cookies:
        _status("Debug: clearing Amazon cookies...")
        # Clear saved cookies file
        cookies_path = _cookies_path()
        if cookies_path.exists():
            cookies_path.unlink()
            logger.info("Cleared cookies file: %s", cookies_path)
        # Clear Chrome cookie storage in nodriver profile
        for rel in ("Default/Cookies", "Default/Cookies-journal",
                     "Default/Network/Cookies", "Default/Network/Cookies-journal"):
            p = profile_dir / rel
            if p.exists():
                p.unlink()
                logger.info("Cleared: %s", p)
        session_dir = profile_dir / "Default" / "Session Storage"
        if session_dir.exists():
            shutil.rmtree(session_dir, ignore_errors=True)
            logger.info("Cleared session storage: %s", session_dir)

    _status("Opening browser for Amazon login...")
    browser = await uc.start(
        user_data_dir=str(profile_dir),
        headless=False,
    )

    try:
        page = await browser.get("https://www.amazon.com")
        await page.sleep(2)

        # Check if already logged in by looking for the account nav
        logged_in = False
        try:
            el = await page.query_selector("#nav-link-accountList")
            if el:
                text = el.text or ""
                if "sign in" not in text.lower():
                    logged_in = True
        except Exception:
            pass

        if logged_in:
            _status("Already logged into Amazon")
        else:
            _status("Waiting for Amazon login — please log in via the browser window...")
            # Poll until logged in (up to 5 minutes)
            for _ in range(100):
                await page.sleep(3)
                try:
                    el = await page.query_selector("#nav-link-accountList")
                    if el:
                        text = el.text or ""
                        if "sign in" not in text.lower():
                            logged_in = True
                            break
                except Exception:
                    pass
                # Also check if we're on the main page (redirected after login)
                try:
                    if "amazon.com" in page.url and "/ap/" not in page.url:
                        el = await page.query_selector("#nav-link-accountList")
                        if el and "sign in" not in (el.text or "").lower():
                            logged_in = True
                            break
                except Exception:
                    pass

            if not logged_in:
                raise RuntimeError("Timed out waiting for Amazon login")

        _status("Extracting Amazon cookies...")
        all_cookies = await browser.cookies.get_all()

        cookies = {}
        for c in all_cookies:
            domain = getattr(c, "domain", "") or ""
            name = getattr(c, "name", "") or ""
            value = getattr(c, "value", "") or ""
            if "amazon" in domain and name and value:
                cookies[name] = value

        logger.info("Extracted %d Amazon cookies via nodriver", len(cookies))
        logger.info("Cookie names: %s", sorted(cookies.keys()))

        result = {"cookies": cookies, "source": "nodriver"}
        save_cookies(result)
        return result

    finally:
        try:
            browser.stop()
        except Exception:
            pass


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
