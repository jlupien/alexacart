"""
Alexa Shopping List API client.

Uses undocumented Amazon Alexa v2 API endpoints:
- POST /alexashoppinglists/api/v2/lists/fetch — list all shopping lists
- POST /alexashoppinglists/api/v2/lists/{listId}/items/fetch — get items
- PUT  /alexashoppinglists/api/v2/lists/{listId}/items/{itemId}?version=N — update item
"""

import asyncio
import logging
from dataclasses import dataclass

import httpx

from alexacart.alexa.auth import ensure_valid_cookies, get_cookie_header, try_refresh_via_sidecar
from alexacart.config import settings

logger = logging.getLogger(__name__)

API_BASE = "https://www.amazon.com/alexashoppinglists/api/v2"

COMMON_HEADERS = {
    "Content-Type": "application/json; charset=utf-8",
    "Accept": "application/json; charset=utf-8",
    "Accept-Language": "en-US",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": "https://alexa.amazon.com/spa/index.html",
    "Origin": "https://alexa.amazon.com",
}


def _extract_csrf(cookie_data: dict) -> str:
    """Extract CSRF token from cookies (it's stored as a cookie named 'csrf')."""
    cookies = cookie_data.get("cookies", {})
    return cookies.get("csrf", "")


@dataclass
class AlexaListItem:
    item_id: str
    text: str
    list_id: str
    version: int = 1
    completed: bool = False


class AlexaClient:
    def __init__(self):
        self._cookies: dict | None = None
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._cookies is None:
            cookie_data = await ensure_valid_cookies()
            self._cookies = cookie_data
            csrf = _extract_csrf(cookie_data)
            headers = {**COMMON_HEADERS, **get_cookie_header(cookie_data)}
            if csrf:
                headers["csrf"] = csrf
            self._client = httpx.AsyncClient(headers=headers, timeout=30.0)
        return self._client

    async def _request_with_retry(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Make a request with retries for transient errors (503/502/429) and 401 cookie refresh."""
        client = await self._get_client()

        # Retry on transient server errors
        retryable_statuses = {502, 503, 429}
        max_retries = 3

        for attempt in range(max_retries + 1):
            resp = await client.request(method, url, **kwargs)

            if resp.status_code == 401:
                logger.info("Got 401, attempting cookie refresh...")
                refreshed = try_refresh_via_sidecar()
                if refreshed:
                    self._cookies = refreshed
                    csrf = _extract_csrf(refreshed)
                    headers = {**COMMON_HEADERS, **get_cookie_header(refreshed)}
                    if csrf:
                        headers["csrf"] = csrf
                    self._client = httpx.AsyncClient(headers=headers, timeout=30.0)
                    client = self._client
                    resp = await client.request(method, url, **kwargs)
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

    async def get_lists(self) -> list[dict]:
        """Fetch all Alexa shopping lists."""
        resp = await self._request_with_retry(
            "POST",
            f"{API_BASE}/lists/fetch",
            json={
                "listAttributesToAggregate": [
                    {"type": "totalActiveItemsCount"},
                ],
                "listOwnershipType": None,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("listInfoList", [])

    async def find_list_id(self, list_name: str | None = None) -> str:
        """Find the list ID for the configured list name."""
        target = (list_name or settings.alexa_list_name).lower()
        lists = await self.get_lists()

        for lst in lists:
            name = lst.get("listName", lst.get("name", "")).lower()
            if name == target:
                return lst.get("listId", lst.get("id", ""))

        available = [lst.get("listName", lst.get("name", "")) for lst in lists]
        raise ValueError(
            f"List '{target}' not found. Available lists: {available}"
        )

    async def get_items(self, list_id: str | None = None) -> list[AlexaListItem]:
        """Fetch active (uncompleted) items from the Alexa list."""
        if list_id is None:
            list_id = await self.find_list_id()

        resp = await self._request_with_retry(
            "POST",
            f"{API_BASE}/lists/{list_id}/items/fetch?limit=100",
            json={
                "itemAttributesToProject": ["quantity", "note"],
            },
        )
        resp.raise_for_status()
        data = resp.json()

        items = []
        raw_items = data.get("itemInfoList", [])
        for item in raw_items:
            status = item.get("itemStatus", "ACTIVE")
            if status == "ACTIVE":
                items.append(
                    AlexaListItem(
                        item_id=item.get("itemId", ""),
                        text=item.get("itemName", ""),
                        list_id=list_id,
                        version=item.get("version", 1),
                        completed=False,
                    )
                )

        logger.info("Found %d active items on Alexa list", len(items))
        return items

    async def mark_complete(self, item: AlexaListItem) -> bool:
        """Mark an item as complete (checked off) on the Alexa list."""
        try:
            resp = await self._request_with_retry(
                "PUT",
                f"{API_BASE}/lists/{item.list_id}/items/{item.item_id}?version={item.version}",
                json={
                    "itemAttributesToUpdate": [
                        {"type": "itemStatus", "value": "COMPLETE"},
                    ],
                    "itemAttributesToRemove": [],
                },
            )
            if resp.status_code in (200, 204):
                logger.info("Marked '%s' as complete on Alexa list", item.text)
                return True
            else:
                logger.warning(
                    "Failed to mark '%s' as complete: %s %s",
                    item.text,
                    resp.status_code,
                    resp.text,
                )
                return False
        except Exception as e:
            logger.error("Error marking '%s' as complete: %s", item.text, e)
            return False

    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None
