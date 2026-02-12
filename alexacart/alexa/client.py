"""
Alexa Shopping List API client.

Uses undocumented Amazon Alexa API endpoints:
- GET /alexashoppinglists — list all shopping lists
- GET /alexashoppinglists/api/getlistitems?listId=... — get items from a list
- PUT /alexashoppinglists/api/updatelistitem — mark item as complete
"""

import logging
from dataclasses import dataclass

import httpx

from alexacart.alexa.auth import ensure_valid_cookies, get_cookie_header, try_refresh_via_sidecar
from alexacart.config import settings

logger = logging.getLogger(__name__)

ALEXA_API_BASE = "https://api.amazonalexa.com"
ALEXA_LISTS_URL = "https://www.amazon.com/alexashoppinglists"
ALEXA_LIST_ITEMS_URL = "https://www.amazon.com/alexashoppinglists/api/getlistitems"
ALEXA_UPDATE_ITEM_URL = "https://www.amazon.com/alexashoppinglists/api/updatelistitem"

COMMON_HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.amazon.com/alexashoppinglists",
    "Origin": "https://www.amazon.com",
}


@dataclass
class AlexaListItem:
    item_id: str
    text: str
    list_id: str
    completed: bool = False


class AlexaClient:
    def __init__(self):
        self._cookies: dict | None = None
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._cookies is None:
            cookie_data = await ensure_valid_cookies()
            self._cookies = cookie_data
            headers = {**COMMON_HEADERS, **get_cookie_header(cookie_data)}
            self._client = httpx.AsyncClient(headers=headers, timeout=30.0)
        return self._client

    async def _request_with_retry(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Make a request, retrying once with refreshed cookies on 401."""
        client = await self._get_client()
        resp = await client.request(method, url, **kwargs)

        if resp.status_code == 401:
            logger.info("Got 401, attempting cookie refresh...")
            refreshed = try_refresh_via_sidecar()
            if refreshed:
                self._cookies = refreshed
                headers = {**COMMON_HEADERS, **get_cookie_header(refreshed)}
                self._client = httpx.AsyncClient(headers=headers, timeout=30.0)
                client = self._client
                resp = await client.request(method, url, **kwargs)

        return resp

    async def get_lists(self) -> list[dict]:
        """Fetch all Alexa shopping lists."""
        resp = await self._request_with_retry("GET", ALEXA_LISTS_URL, params={"format": "json"})
        resp.raise_for_status()
        data = resp.json()
        lists = data.get("lists", data.get("shoppingLists", []))
        return lists

    async def find_list_id(self, list_name: str | None = None) -> str:
        """Find the list ID for the configured list name."""
        target = (list_name or settings.alexa_list_name).lower()
        lists = await self.get_lists()

        for lst in lists:
            name = lst.get("name", lst.get("listName", "")).lower()
            if name == target:
                return lst.get("listId", lst.get("id", ""))

        available = [lst.get("name", lst.get("listName", "")) for lst in lists]
        raise ValueError(
            f"List '{target}' not found. Available lists: {available}"
        )

    async def get_items(self, list_id: str | None = None) -> list[AlexaListItem]:
        """Fetch active (uncompleted) items from the Alexa list."""
        if list_id is None:
            list_id = await self.find_list_id()

        resp = await self._request_with_retry(
            "GET",
            ALEXA_LIST_ITEMS_URL,
            params={"listId": list_id},
        )
        resp.raise_for_status()
        data = resp.json()

        items = []
        raw_items = data.get("items", data.get("listItems", []))
        for item in raw_items:
            completed = item.get("completed", item.get("isCompleted", False))
            if not completed:
                items.append(
                    AlexaListItem(
                        item_id=item.get("itemId", item.get("id", "")),
                        text=item.get("value", item.get("text", item.get("name", ""))),
                        list_id=list_id,
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
                ALEXA_UPDATE_ITEM_URL,
                json={
                    "listId": item.list_id,
                    "itemId": item.item_id,
                    "completed": True,
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
