"""
Instacart authentication via nodriver.

Uses nodriver (undetectable Chrome) to log into Instacart and extract
session cookies + session parameters needed for API calls.

Session parameters (shopId, zoneId, postalCode, retailerLocationId,
retailerInventorySessionToken) are extracted by navigating to the store's
search page and intercepting the GraphQL request variables via the
Performance API.
"""

import asyncio
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path

from alexacart.config import settings

logger = logging.getLogger(__name__)

INSTACART_BASE = "https://www.instacart.com"


def _cookies_path() -> Path:
    return settings.resolved_data_dir / "instacart_cookies.json"


def load_instacart_cookies() -> dict | None:
    """Load saved Instacart session data from disk."""
    path = _cookies_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        if data.get("cookies"):
            return data
    except (json.JSONDecodeError, KeyError):
        pass
    return None


def save_instacart_cookies(data: dict) -> None:
    """Save Instacart session data to disk."""
    path = _cookies_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))
    logger.info("Instacart session saved to %s", path)


# JavaScript to extract session params from the Performance API.
# After a search page loads, the browser makes a SearchResultsPlacements
# GraphQL GET request with all session params in the query string.
_JS_EXTRACT_FROM_PERFORMANCE = """
(function() {
    var result = {};
    var entries = performance.getEntriesByType('resource');
    for (var i = 0; i < entries.length; i++) {
        var name = entries[i].name;
        if (name.indexOf('graphql') >= 0 && name.indexOf('SearchResultsPlacements') >= 0) {
            try {
                var url = new URL(name);
                var vars = JSON.parse(decodeURIComponent(url.searchParams.get('variables')));
                if (vars.shopId) result.shop_id = vars.shopId;
                if (vars.zoneId) result.zone_id = vars.zoneId;
                if (vars.postalCode) result.postal_code = vars.postalCode;
                if (vars.retailerInventorySessionToken) {
                    result.retailer_inventory_session_token = vars.retailerInventorySessionToken;
                }
                break;
            } catch(e) {}
        }
    }
    return JSON.stringify(result);
})()
"""

# Fallback: search __NEXT_DATA__ and page scripts for session params
_JS_EXTRACT_FROM_PAGE = """
(function() {
    var result = {};
    var sources = [];

    var nd = document.getElementById('__NEXT_DATA__');
    if (nd) sources.push(nd.textContent);

    var scripts = document.querySelectorAll('script');
    for (var i = 0; i < scripts.length; i++) {
        if (scripts[i].textContent.length > 100) sources.push(scripts[i].textContent);
    }

    var text = sources.join('\\n');

    var patterns = [
        [/"shopId"\\s*:\\s*"(\\d+)"/, 'shop_id'],
        [/"zoneId"\\s*:\\s*"(\\d+)"/, 'zone_id'],
        [/"postalCode"\\s*:\\s*"(\\d{5})"/, 'postal_code'],
        [/"retailerInventorySessionToken"\\s*:\\s*"([^"]+)"/, 'retailer_inventory_session_token'],
        [/items_(\\d+)-\\d+/, 'retailer_location_id'],
    ];

    for (var i = 0; i < patterns.length; i++) {
        var m = text.match(patterns[i][0]);
        if (m) result[patterns[i][1]] = m[1];
    }

    return JSON.stringify(result);
})()
"""


async def _run_js(page, js_code: str) -> str | None:
    """Execute JavaScript on a nodriver page and return the string result."""
    try:
        result = await page.evaluate(js_code)
        if isinstance(result, str):
            return result
        if hasattr(result, "value"):
            return result.value
        return str(result) if result is not None else None
    except Exception as e:
        logger.warning("JS eval failed: %s", e)
        return None


async def _extract_session_params(page, store_slug: str) -> dict:
    """
    Extract session params from the current page.

    Tries the Performance API first (reliable if a search was performed),
    then falls back to scanning page scripts.
    """
    params = {"retailer_slug": store_slug}

    # Strategy 1: Performance API (works after search page loads)
    raw = await _run_js(page, _JS_EXTRACT_FROM_PERFORMANCE)
    if raw:
        try:
            found = json.loads(raw)
            params.update({k: v for k, v in found.items() if v})
            logger.info("Performance API params: %s", list(found.keys()))
        except json.JSONDecodeError:
            pass

    # Strategy 2: Page script scanning (works on any store page)
    if not params.get("shop_id") or not params.get("zone_id"):
        raw = await _run_js(page, _JS_EXTRACT_FROM_PAGE)
        if raw:
            try:
                found = json.loads(raw)
                for k, v in found.items():
                    if v and k not in params:
                        params[k] = v
                logger.info("Page scan params: %s", list(found.keys()))
            except json.JSONDecodeError:
                pass

    # Extract location_id from inventory token if not found directly
    if not params.get("retailer_location_id") and params.get("retailer_inventory_session_token"):
        token = params["retailer_inventory_session_token"]
        # Token format: v1.{hash}.{userId}-{postal}-...-{retailerId}-{locationId}-...
        parts = token.split(".")
        if len(parts) >= 3:
            segments = parts[2].split("-")
            # Location ID is typically the 6th segment
            if len(segments) >= 7:
                params["retailer_location_id"] = segments[5]

    found_keys = [k for k in ("shop_id", "zone_id", "postal_code", "retailer_location_id",
                               "retailer_inventory_session_token") if params.get(k)]
    logger.info("Session params discovered: %s", found_keys)
    return params


async def extract_session_via_nodriver(on_status=None) -> dict:
    """
    Login to Instacart via nodriver and extract session cookies + params.

    1. Open headless browser to store page — check if logged in
    2. If not logged in, open visible browser for manual login
    3. Navigate to search page to trigger GraphQL call
    4. Extract session params from the GraphQL request
    5. Extract cookies
    6. Save everything to disk

    Returns dict with 'cookies' and 'session_params' keys.
    """
    import nodriver as uc

    def _status(msg):
        logger.info(msg)
        if on_status:
            on_status(msg)

    store_slug = settings.instacart_store.lower()
    profile_dir = settings.resolved_data_dir / "nodriver-instacart"
    profile_dir.mkdir(parents=True, exist_ok=True)

    if settings.debug_clear_instacart_cookies:
        _status("Debug: clearing Instacart cookies...")
        path = _cookies_path()
        if path.exists():
            path.unlink()

    store_url = f"{INSTACART_BASE}/store/{store_slug}"
    search_url = f"{INSTACART_BASE}/store/{store_slug}/s?k=milk"

    _status("Checking Instacart login...")
    browser = await uc.start(user_data_dir=str(profile_dir), headless=True)

    try:
        page = await browser.get(store_url)
        await page.sleep(3)

        current_url = page.url or ""
        logged_in = "/login" not in current_url and "/store/" in current_url

        if not logged_in:
            browser.stop()
            await asyncio.sleep(1)
            _status("Login needed — opening Instacart login window...")

            for attempt in range(3):
                try:
                    browser = await uc.start(user_data_dir=str(profile_dir), headless=False)
                    break
                except Exception as e:
                    if attempt < 2:
                        logger.warning("nodriver start attempt %d failed: %s", attempt + 1, e)
                        await asyncio.sleep(2)
                    else:
                        raise

            page = await browser.get(f"{INSTACART_BASE}/login")
            await page.sleep(2)

            _status("Waiting for Instacart login — please log in via the browser window...")
            for _ in range(120):
                await page.sleep(3)
                current_url = page.url or ""
                if "/login" not in current_url:
                    logged_in = True
                    break

            if not logged_in:
                raise RuntimeError("Timed out waiting for Instacart login")

            _status("Logged in! Extracting session data...")

        # Navigate to search page to trigger SearchResultsPlacements GraphQL call
        _status("Discovering session parameters...")
        page = await browser.get(search_url)
        await page.sleep(5)

        # Extract session params
        session_params = await _extract_session_params(page, store_slug)

        # Extract cookies
        all_cookies = await browser.cookies.get_all()
        cookies = {}
        for c in all_cookies:
            name = getattr(c, "name", "") or ""
            value = getattr(c, "value", "") or ""
            domain = getattr(c, "domain", "") or ""
            if "instacart" in domain and name and value:
                cookies[name] = value

        logger.info("Extracted %d Instacart cookies: %s", len(cookies), sorted(cookies.keys()))

        data = {
            "cookies": cookies,
            "session_params": session_params,
            "extracted_at": datetime.now(UTC).isoformat(),
        }
        save_instacart_cookies(data)
        _status("Instacart session ready")
        return data

    finally:
        try:
            browser.stop()
        except Exception:
            pass


async def ensure_valid_session() -> dict:
    """Load cached session or extract a new one via nodriver."""
    data = load_instacart_cookies()
    if data and data.get("cookies"):
        return data
    return await extract_session_via_nodriver()
