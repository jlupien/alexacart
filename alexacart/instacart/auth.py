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


# Extract addressId from page data (for ActiveCartId fallback).
# Looks in Performance API entries and __NEXT_DATA__.
_JS_EXTRACT_ADDRESS_ID = """
(function() {
    var entries = performance.getEntriesByType('resource');
    for (var i = 0; i < entries.length; i++) {
        var name = entries[i].name;
        if (name.indexOf('ActiveCartId') >= 0) {
            try {
                var url = new URL(name);
                var vars = JSON.parse(decodeURIComponent(url.searchParams.get('variables')));
                if (vars.addressId) return vars.addressId;
            } catch(e) {}
        }
    }
    var sources = [];
    var nd = document.getElementById('__NEXT_DATA__');
    if (nd) sources.push(nd.textContent);
    var scripts = document.querySelectorAll('script');
    for (var i = 0; i < scripts.length; i++) {
        if (scripts[i].textContent.length > 100) sources.push(scripts[i].textContent);
    }
    var text = sources.join('\\n');
    var m = text.match(/"addressId"\\s*:\\s*"(\\d+)"/);
    if (m) return m[1];
    m = text.match(/"address_id"\\s*:\\s*"?(\\d+)"?/);
    if (m) return m[1];
    return null;
})()
"""

# Call ActiveCartId GraphQL query from browser context.
# This allocates/discovers the correct cart (family if in household) for
# a given address + shop. More reliable than PersonalActiveCarts which
# only returns non-empty carts.
_JS_CALL_ACTIVE_CART_ID = """
(async function() {
    var addressId = "%ADDRESS_ID%";
    var shopId = "%SHOP_ID%";
    var hash = "6803f97683d706ab6faa3c658a0d6766299dbe1ff55f78b720ca2ef77de7c5c7";
    try {
        var resp = await fetch(
            '/graphql?operationName=ActiveCartId'
            + '&variables=' + encodeURIComponent(JSON.stringify({addressId: addressId, shopId: shopId}))
            + '&extensions=' + encodeURIComponent(JSON.stringify({
                persistedQuery: {version: 1, sha256Hash: hash}
            })),
            {headers: {"x-client-identifier": "web"}}
        );
        var data = await resp.json();
        var basket = (data.data || {}).shopBasket || {};
        return JSON.stringify({cart_id: basket.cartId || null, cart_type: basket.cartType || null});
    } catch(e) {
        return JSON.stringify({error: e.message});
    }
})()
"""

_JS_DISCOVER_CART_ID = """
(async function() {
    var slug = "%STORE_SLUG%";
    try {
        var resp = await fetch(
            '/graphql?operationName=PersonalActiveCarts'
            + '&variables=' + encodeURIComponent('{}')
            + '&extensions=' + encodeURIComponent(JSON.stringify({
                persistedQuery: {version: 1, sha256Hash: "eac9d17bd45b099fbbdabca2e111acaf2a4fa486f2ce5bc4e8acbab2f31fd8c0"}
            })),
            {headers: {"x-client-identifier": "web"}}
        );
        var data = await resp.json();
        var carts = (data.data || {}).userCarts || {};
        var cartList = carts.carts || [];
        var fallback = null;
        for (var i = 0; i < cartList.length; i++) {
            var cart = cartList[i];
            var retailer = cart.retailer || {};
            if ((retailer.slug || '').toLowerCase() === slug) {
                // Prefer family/household carts, fall back to personal
                if (cart.householdId) {
                    return JSON.stringify({cart_id: cart.id, household_id: cart.householdId, all_carts: cartList.length});
                }
                if (!fallback) fallback = cart;
            }
        }
        if (fallback) {
            return JSON.stringify({cart_id: fallback.id, household_id: null, all_carts: cartList.length});
        }
        return JSON.stringify({cart_id: null, all_carts: cartList.length});
    } catch(e) {
        return JSON.stringify({error: e.message});
    }
})()
"""

# Create a cart by adding a temporary item from within the browser context.
# The browser has the correct household/family context, so the created cart
# will be a household cart (not an orphan personal cart).
# We then immediately remove the item, keeping just the cart ID.
_JS_CREATE_CART = """
(async function() {
    var itemId = "%ITEM_ID%";
    var mutHash = "7c2c63093a07a61b056c09be23eba6f5790059dca8179f7af7580c0456b1049f";

    // Add a temporary item to force cart creation
    var addResp = await fetch('/graphql?operationName=UpdateCartItemsMutation', {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'x-client-identifier': 'web'},
        body: JSON.stringify({
            operationName: 'UpdateCartItemsMutation',
            variables: {
                cartItemUpdates: [{itemId: itemId, quantity: 1, quantityType: 'each'}],
                cartType: 'grocery',
                requestTimestamp: Date.now()
            },
            extensions: {persistedQuery: {version: 1, sha256Hash: mutHash}}
        })
    });
    var addData = await addResp.json();
    var result = (addData.data || {}).updateCartItems || {};
    var cart = result.cart || {};
    var cartId = cart.id;
    if (!cartId) return JSON.stringify({error: 'no cart id in add response', raw: JSON.stringify(result).substring(0, 500)});

    // Remove the temporary item (set quantity to 0)
    await fetch('/graphql?operationName=UpdateCartItemsMutation', {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'x-client-identifier': 'web'},
        body: JSON.stringify({
            operationName: 'UpdateCartItemsMutation',
            variables: {
                cartItemUpdates: [{itemId: itemId, quantity: 0, quantityType: 'each'}],
                cartType: 'grocery',
                requestTimestamp: Date.now(),
                cartId: cartId
            },
            extensions: {persistedQuery: {version: 1, sha256Hash: mutHash}}
        })
    });

    return JSON.stringify({cart_id: cartId});
})()
"""

# Search for "milk" via the GraphQL API from within the browser and return
# the first item_id from the results. Uses fetch (not Performance API) for
# reliability — the Performance API entries may not include all XHR requests.
_JS_GET_FIRST_ITEM_ID = """
(async function() {
    var shopId = "%SHOP_ID%";
    var postalCode = "%POSTAL_CODE%";
    var zoneId = "%ZONE_ID%";
    var locationId = "%LOCATION_ID%";
    var searchHash = "521b48a91cb45d5ce14457b8548ae4592e947136cebef056b8b350fe078d92ec";
    var itemsHash = "5116339819ff07f207fd38f949a8a7f58e52cc62223b535405b087e3076ebf2f";

    // Search for a common product
    var searchVars = {
        query: "milk", shopId: shopId, postalCode: postalCode, zoneId: zoneId,
        first: 3, pageViewId: crypto.randomUUID(), searchSource: "search",
        orderBy: "bestMatch", filters: [], action: null, elevatedProductId: null,
        disableReformulation: false, disableLlm: false, forceInspiration: false,
        clusterId: null, includeDebugInfo: false, clusteringStrategy: null,
        contentManagementSearchParams: {itemGridColumnCount: 4}
    };
    var resp = await fetch(
        '/graphql?operationName=SearchResultsPlacements'
        + '&variables=' + encodeURIComponent(JSON.stringify(searchVars))
        + '&extensions=' + encodeURIComponent(JSON.stringify({persistedQuery: {version: 1, sha256Hash: searchHash}})),
        {headers: {"x-client-identifier": "web"}}
    );
    var data = await resp.json();
    var placements = ((data.data || {}).searchResultsPlacements || {}).placements || [];

    // Try to get item_ids from search results
    for (var i = 0; i < placements.length; i++) {
        var content = placements[i].content || {};
        var typename = content.__typename || '';
        if (typename.indexOf('Ads') === 0) continue;
        var itemIds = content.itemIds || [];
        if (itemIds.length > 0) return itemIds[0];
        var items = content.items || [];
        if (items.length > 0 && items[0].id) return items[0].id;
    }

    // Fallback: construct item_id from search results product IDs
    for (var i = 0; i < placements.length; i++) {
        var content = placements[i].content || {};
        var items = content.items || [];
        for (var j = 0; j < items.length; j++) {
            var pid = items[j].productId;
            if (pid && locationId) return 'items_' + locationId + '-' + pid;
        }
    }

    return null;
})()
"""


async def _discover_cart_id_from_browser(
    page, store_slug: str, session_params: dict,
) -> str | None:
    """Discover or create the cart ID from within the browser context.

    The browser has the correct household/session context, so any cart
    created here will be a household/family cart — not an orphan personal cart.

    Strategy (matches real browser behavior):
    1. Extract addressId, then try ActiveCartId — returns the correct cart
       tied to the user's delivery address (same as instacart.com uses)
    2. Fall back to PersonalActiveCarts if ActiveCartId fails
    3. If no matching cart, add+remove a temporary item to force cart creation,
       then extract the cart ID from the mutation response
    """
    # Strategy 1: ActiveCartId (needs addressId) — preferred, matches browser
    address_id = await _run_js(page, _JS_EXTRACT_ADDRESS_ID)
    if address_id:
        logger.info("Found addressId from page: %s", address_id)
        session_params["address_id"] = address_id
        active_js = (
            _JS_CALL_ACTIVE_CART_ID
            .replace("%ADDRESS_ID%", address_id)
            .replace("%SHOP_ID%", session_params.get("shop_id", ""))
        )
        raw = await _run_js(page, active_js, await_promise=True)
        if raw:
            try:
                result = json.loads(raw)
                if result.get("error"):
                    logger.warning("Browser ActiveCartId error: %s", result["error"])
                elif result.get("cart_id"):
                    logger.info("Browser ActiveCartId: cart_id=%s", result["cart_id"])
                    return result["cart_id"]
            except (json.JSONDecodeError, TypeError) as e:
                logger.warning("Browser ActiveCartId parse error: %s", e)
    else:
        logger.info("No addressId found on page — skipping ActiveCartId")

    # Strategy 2: PersonalActiveCarts (fallback)
    js = _JS_DISCOVER_CART_ID.replace("%STORE_SLUG%", store_slug)
    raw = await _run_js(page, js, await_promise=True)
    if raw:
        try:
            result = json.loads(raw)
            if result.get("error"):
                logger.warning("Browser PersonalActiveCarts error: %s", result["error"])
            else:
                household = result.get("household_id")
                cart_type = "family" if household else "personal"
                logger.info(
                    "Browser PersonalActiveCarts: cart_id=%s (%s), household=%s, total_carts=%s",
                    result.get("cart_id"), cart_type, household, result.get("all_carts"),
                )
                if result.get("cart_id"):
                    return result["cart_id"]
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning("Browser PersonalActiveCarts parse error: %s (raw=%s)", e, raw[:200])
    else:
        logger.warning("Browser PersonalActiveCarts returned no result")

    # Strategy 3: Create cart by adding+removing a temporary item
    logger.info("No existing cart for %s, creating via browser context...", store_slug)
    get_item_js = (
        _JS_GET_FIRST_ITEM_ID
        .replace("%SHOP_ID%", session_params.get("shop_id", ""))
        .replace("%POSTAL_CODE%", session_params.get("postal_code", ""))
        .replace("%ZONE_ID%", session_params.get("zone_id", ""))
        .replace("%LOCATION_ID%", session_params.get("retailer_location_id", ""))
    )
    item_id = await _run_js(page, get_item_js, await_promise=True)
    if not item_id:
        logger.warning("Could not find an item_id to create cart")
        return None

    logger.info("Creating family cart with temporary item: %s", item_id)
    create_js = _JS_CREATE_CART.replace("%ITEM_ID%", item_id)
    raw = await _run_js(page, create_js, await_promise=True)
    if not raw:
        logger.warning("Cart creation returned no result")
        return None
    try:
        result = json.loads(raw)
        if result.get("error"):
            logger.warning("Cart creation error: %s", result["error"])
            return None
        logger.info("Created family cart via browser: %s", result.get("cart_id"))
        return result.get("cart_id")
    except (json.JSONDecodeError, TypeError):
        return None


async def _run_js(page, js_code: str, await_promise: bool = False) -> str | None:
    """Execute JavaScript on a nodriver page and return the string result.

    Set await_promise=True for async JS functions (those using fetch() or
    other async operations). Without this, nodriver returns the unresolved
    Promise object instead of the resolved value.
    """
    try:
        result = await page.evaluate(js_code, await_promise=await_promise)
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

        # Discover cart ID from within the browser context.
        # The browser has the correct household/family context, so
        # PersonalActiveCarts will return household carts that may not
        # appear when queried via plain httpx.
        _status("Discovering cart ID...")
        cart_id = await _discover_cart_id_from_browser(page, store_slug, session_params)
        if cart_id:
            logger.info("Discovered cart ID from browser: %s", cart_id)
        else:
            logger.warning("No cart ID discovered from browser for %s", store_slug)

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
            "cart_id": cart_id or "",
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
    if data and data.get("cookies") and data.get("cart_id"):
        params = data.get("session_params", {})
        if params.get("address_id"):
            return data
        # Without address_id we can't validate/discover the correct cart
        # via ActiveCartId — re-extract to get it.
        logger.info("Cached session missing address_id — re-extracting via nodriver")
    elif data and data.get("cookies"):
        logger.info("Cached Instacart session has no cart_id — re-extracting via nodriver")
    return await extract_session_via_nodriver()
