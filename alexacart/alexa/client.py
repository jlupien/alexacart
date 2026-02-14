"""
Alexa Shopping List API client.

Uses undocumented Amazon Alexa API endpoints (mobile app webview API):
- GET  /alexashoppinglists/api/getlistitems — get all shopping list items
- PUT  /alexashoppinglists/api/updatelistitem — update an item (e.g. mark complete)

These endpoints are accessed using the Alexa mobile app's User-Agent
(PitanguiBridge) which routes to the still-active mobile API backend.
The older v2 API (used by the now-defunct alexa.amazon.com SPA) returns 401.
"""

import asyncio
import logging
from dataclasses import dataclass, field

import httpx

from alexacart.alexa.auth import ensure_valid_cookies, get_cookie_header, save_cookies, try_refresh_via_sidecar
from alexacart.config import settings

logger = logging.getLogger(__name__)

API_BASE = "https://www.amazon.com/alexashoppinglists/api"

# Mimic the Alexa mobile app's webview (PitanguiBridge).
# Amazon routes requests to different backends based on User-Agent;
# the desktop browser UA hits the deprecated v2 backend while the
# mobile app UA hits the still-active mobile backend.
COMMON_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 13_5_1 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 "
        "PitanguiBridge/2.2.345247.0-[HARDWARE=iPhone10_4][SOFTWARE=13.5.1]"
    ),
    "Accept": "*/*",
    "Accept-Language": "*",
    "DNT": "1",
    "Upgrade-Insecure-Requests": "1",
}


@dataclass
class AlexaListItem:
    item_id: str
    text: str
    list_id: str = ""
    version: int = 1
    completed: bool = False
    # Store the raw item dict from the API so we can send it back for updates
    _raw: dict = field(default_factory=dict, repr=False)


class AlexaClient:
    def __init__(self, cookie_refresh_fn=None):
        """
        Args:
            cookie_refresh_fn: Optional async callable that returns fresh cookie data.
                Used to re-extract cookies from the browser session on 401.
        """
        self._cookies: dict | None = None
        self._client: httpx.AsyncClient | None = None
        self._cookie_refresh_fn = cookie_refresh_fn

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._cookies is None:
            cookie_data = await ensure_valid_cookies()
            self._cookies = cookie_data
            headers = {**COMMON_HEADERS, **get_cookie_header(cookie_data)}
            cookie_count = len(cookie_data.get("cookies", {}))
            logger.info(
                "AlexaClient initialized (cookies=%d, source=%s)",
                cookie_count, cookie_data.get("source", "disk"),
            )
            self._client = httpx.AsyncClient(headers=headers, timeout=30.0)
        return self._client

    async def _request_with_retry(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Make a request with retries for transient errors (503/502/429) and 401 cookie refresh."""
        client = await self._get_client()

        # Retry on transient server errors
        retryable_statuses = {502, 503, 429}
        max_retries = 3

        for attempt in range(max_retries + 1):
            if attempt == 0:
                # Log headers on first attempt (truncate cookie values)
                debug_headers = {}
                for k, v in client.headers.items():
                    if k.lower() == "cookie":
                        parts = v.split("; ")
                        debug_headers[k] = "; ".join(
                            p.split("=")[0] + "=..." if "=" in p else p
                            for p in parts
                        )
                    else:
                        debug_headers[k] = v
                logger.info("Alexa API request headers: %s", debug_headers)
            logger.info("Alexa API request: %s %s (attempt %d)", method, url, attempt + 1)
            resp = await client.request(method, url, **kwargs)
            logger.info("Alexa API response: %d for %s %s", resp.status_code, method, url)

            if resp.status_code not in (200, 204):
                body = resp.text[:500] if resp.text else "(empty)"
                logger.warning("Alexa API error body: %s", body)

            if resp.status_code == 401:
                logger.info("Got 401, attempting cookie refresh...")
                refreshed = None

                # Try browser-based refresh first (if available)
                if self._cookie_refresh_fn:
                    try:
                        refreshed = await self._cookie_refresh_fn()
                        if refreshed:
                            logger.info("Cookies refreshed via browser session")
                            save_cookies(refreshed)
                    except Exception as e:
                        logger.warning("Browser cookie refresh failed: %s", e)

                # Fall back to Node.js sidecar
                if not refreshed:
                    refreshed = await asyncio.to_thread(try_refresh_via_sidecar)

                if refreshed:
                    self._cookies = refreshed
                    headers = {**COMMON_HEADERS, **get_cookie_header(refreshed)}
                    old_client = self._client
                    self._client = httpx.AsyncClient(headers=headers, timeout=30.0)
                    client = self._client
                    if old_client:
                        await old_client.aclose()
                    logger.info("Retrying request after cookie refresh...")
                    resp = await client.request(method, url, **kwargs)
                    logger.info("Post-refresh response: %d for %s %s", resp.status_code, method, url)
                    if resp.status_code not in (200, 204):
                        body = resp.text[:500] if resp.text else "(empty)"
                        logger.warning("Post-refresh error body: %s", body)
                else:
                    logger.warning("No cookie refresh available — returning 401 as-is")
                return resp

            if resp.status_code in retryable_statuses and attempt < max_retries:
                delay = 2 ** attempt  # 1s, 2s, 4s
                logger.warning(
                    "Got %d from %s, retrying in %ds (attempt %d/%d)",
                    resp.status_code, url, delay, attempt + 1, max_retries,
                )
                await asyncio.sleep(delay)
                continue

            return resp

        return resp

    @staticmethod
    def _extract_list_items(data: dict) -> list[dict]:
        """Extract the list items from the API response (nested structure)."""
        # The response contains a nested dict with a 'listItems' key
        for key in data:
            val = data[key]
            if isinstance(val, dict) and "listItems" in val:
                return val["listItems"]
        # Fallback: try top-level listItems
        if "listItems" in data:
            return data["listItems"]
        logger.warning("Could not find listItems in response keys: %s", list(data.keys()))
        return []

    async def get_items(self, list_id: str | None = None) -> list[AlexaListItem]:
        """Fetch active (uncompleted) items from the Alexa shopping list."""
        resp = await self._request_with_retry("GET", f"{API_BASE}/getlistitems")
        resp.raise_for_status()
        data = resp.json()

        raw_items = self._extract_list_items(data)
        logger.info("API returned %d total items", len(raw_items))

        items = []
        for item in raw_items:
            if not item.get("completed", False):
                items.append(
                    AlexaListItem(
                        item_id=item.get("id", ""),
                        text=item.get("value", ""),
                        completed=False,
                        _raw=item,
                    )
                )

        logger.info("Found %d active items on Alexa list", len(items))
        return items

    async def mark_complete(self, item: AlexaListItem) -> bool:
        """Mark an item as complete (checked off) on the Alexa list."""
        try:
            # Build the update payload from the raw item data if available,
            # otherwise construct a minimal one
            if item._raw:
                update_data = {**item._raw, "completed": True}
            else:
                update_data = {
                    "id": item.item_id,
                    "value": item.text,
                    "completed": True,
                    "type": "TASK",
                }

            resp = await self._request_with_retry(
                "PUT",
                f"{API_BASE}/updatelistitem",
                json=update_data,
            )
            if resp.status_code in (200, 204):
                logger.info("Marked '%s' as complete on Alexa list", item.text)
                return True
            else:
                logger.warning(
                    "Failed to mark '%s' as complete: %s %s",
                    item.text,
                    resp.status_code,
                    resp.text[:200],
                )
                return False
        except Exception as e:
            logger.error("Error marking '%s' as complete: %s", item.text, e)
            return False

    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None
