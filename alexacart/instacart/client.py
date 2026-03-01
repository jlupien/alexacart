"""
Instacart GraphQL API client.

Uses direct HTTP calls to Instacart's internal GraphQL API (same endpoints
used by the web frontend). Authentication is cookie-based — cookies are
extracted from a browser session via nodriver.

This replaces the previous browser-use approach (LLM-powered browser automation)
with reliable, fast API calls.
"""

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass

import httpx

from alexacart.config import settings

logger = logging.getLogger(__name__)

GRAPHQL_URL = "https://www.instacart.com/graphql"

# Automatic Persisted Query (APQ) hashes — the server resolves the full
# GraphQL query from these hashes. No need to send the query text.
_APQ = {
    "SearchResultsPlacements": "521b48a91cb45d5ce14457b8548ae4592e947136cebef056b8b350fe078d92ec",
    "Items": "5116339819ff07f207fd38f949a8a7f58e52cc62223b535405b087e3076ebf2f",
    "UpdateCartItemsMutation": "7c2c63093a07a61b056c09be23eba6f5790059dca8179f7af7580c0456b1049f",
    "PersonalActiveCarts": "eac9d17bd45b099fbbdabca2e111acaf2a4fa486f2ce5bc4e8acbab2f31fd8c0",
    "LandingProductMeta": "0adb678fd5eae17020624e19c376b2e2eb18235b3d6c164ed458952cc2f8260c",
    "ItemPricesQuery": "b1f35040f89d7ebfeea056e00f759a430a09621545a855835c001fc0ed9ab4f7",
}

_COMMON_HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "Origin": "https://www.instacart.com",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/144.0.0.0 Safari/537.36"
    ),
    "x-client-identifier": "web",
    "x-ic-view-layer": "true",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}


@dataclass
class ProductResult:
    """A product from Instacart search or detail lookup."""

    product_name: str
    product_url: str | None = None
    brand: str | None = None
    price: str | None = None
    image_url: str | None = None
    in_stock: bool = True
    product_id: str | None = None
    item_id: str | None = None
    size: str | None = None


class InstacartClient:
    """Direct HTTP client for Instacart's GraphQL API."""

    def __init__(self, session_data: dict):
        cookies = session_data.get("cookies", {})
        params = session_data.get("session_params", {})

        self._shop_id = params.get("shop_id", "")
        self._zone_id = params.get("zone_id", "")
        self._postal_code = params.get("postal_code", "")
        self._location_id = params.get("retailer_location_id", "")
        self._inventory_token = params.get("retailer_inventory_session_token", "")
        self._retailer_slug = params.get("retailer_slug", settings.instacart_store.lower())
        self._cart_id = session_data.get("cart_id", "")

        cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
        self._client = httpx.AsyncClient(
            headers={**_COMMON_HEADERS, "Cookie": cookie_header},
            timeout=30.0,
        )

    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None

    async def init_session(self):
        """Discover cart ID and validate session."""
        if not self._cart_id:
            await self._discover_cart_id()

    # ------------------------------------------------------------------
    # GraphQL transport
    # ------------------------------------------------------------------

    def _apq_extensions(self, operation: str) -> dict:
        return {"persistedQuery": {"version": 1, "sha256Hash": _APQ[operation]}}

    async def _graphql_get(self, operation: str, variables: dict) -> dict:
        params = {
            "operationName": operation,
            "variables": json.dumps(variables, separators=(",", ":")),
            "extensions": json.dumps(self._apq_extensions(operation), separators=(",", ":")),
        }
        headers = {"x-page-view-id": str(uuid.uuid4())}
        resp = await self._client.get(GRAPHQL_URL, params=params, headers=headers)
        if resp.status_code == 401:
            raise InstacartAuthError("Instacart session expired (401)")
        resp.raise_for_status()
        return resp.json()

    async def _graphql_post(self, operation: str, variables: dict) -> dict:
        body = {
            "operationName": operation,
            "variables": variables,
            "extensions": self._apq_extensions(operation),
        }
        headers = {"x-page-view-id": str(uuid.uuid4())}
        resp = await self._client.post(
            f"{GRAPHQL_URL}?operationName={operation}",
            json=body,
            headers=headers,
        )
        if resp.status_code == 401:
            raise InstacartAuthError("Instacart session expired (401)")
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def search_products(self, query: str, limit: int = 10) -> list[ProductResult]:
        """Search for products on the configured store."""
        variables = {
            "query": query,
            "shopId": self._shop_id,
            "postalCode": self._postal_code,
            "zoneId": self._zone_id,
            "retailerInventorySessionToken": self._inventory_token or None,
            "first": limit,
            "pageViewId": str(uuid.uuid4()),
            "searchSource": "search",
            "orderBy": "bestMatch",
            "filters": [],
            "action": None,
            "elevatedProductId": None,
            "disableReformulation": False,
            "disableLlm": False,
            "forceInspiration": False,
            "clusterId": None,
            "includeDebugInfo": False,
            "clusteringStrategy": None,
            "contentManagementSearchParams": {"itemGridColumnCount": 4},
        }

        data = await self._graphql_get("SearchResultsPlacements", variables)

        placements = (
            data.get("data", {})
            .get("searchResultsPlacements", {})
            .get("placements", [])
        )

        results: list[ProductResult] = []
        unfetched_ids: list[str] = []

        for placement in placements:
            items, item_ids = self._extract_placement_items(placement)

            for item in items:
                pr = self._parse_item(item)
                if pr:
                    results.append(pr)

            if not items and item_ids:
                unfetched_ids.extend(item_ids)

        # Fetch remaining items by ID if we don't have enough inline
        if len(results) < limit and unfetched_ids:
            needed = limit - len(results)
            fetched = await self._fetch_items_by_id(unfetched_ids[:needed])
            results.extend(fetched)

        # Discover location_id from results if we don't have it yet
        if not self._location_id:
            for r in results:
                if r.item_id:
                    m = re.match(r"items_(\d+)-", r.item_id)
                    if m:
                        self._location_id = m.group(1)
                        logger.info("Discovered retailer_location_id: %s", self._location_id)
                        break

        return results[:limit]

    async def get_product_details(self, product_url: str) -> ProductResult | None:
        """Get product details from an Instacart product URL."""
        slug = self._extract_product_slug(product_url)
        if not slug:
            logger.warning("Could not extract product slug from URL: %s", product_url)
            return None

        variables = {
            "productId": slug,
            "retailerSlug": self._retailer_slug,
        }

        try:
            data = await self._graphql_get("LandingProductMeta", variables)
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (400, 404):
                return None
            raise

        products = data.get("data", {}).get("landingProducts", [])
        if not products:
            return None

        product = products[0]
        product_id = product.get("id", "")
        name = product.get("name", "")
        brand = product.get("brandName", "")
        size = product.get("size", "")

        image_url = ""
        image = product.get("image", {}) or {}
        image_vs = image.get("viewSection", {}) or {}
        product_image = image_vs.get("productImage", {}) or {}
        image_url = product_image.get("url", "")

        # Get price + availability via Items query
        price = None
        in_stock = True
        item_id = None

        if product_id and self._location_id:
            item_id = f"items_{self._location_id}-{product_id}"
            fetched = await self._fetch_items_by_id([item_id])
            if fetched:
                price = fetched[0].price
                in_stock = fetched[0].in_stock
                # Prefer Items image — LandingProductMeta often returns a placeholder
                if fetched[0].image_url:
                    image_url = fetched[0].image_url

        return ProductResult(
            product_name=name,
            product_url=product_url,
            brand=brand or None,
            price=price,
            image_url=image_url or None,
            in_stock=in_stock,
            product_id=product_id or None,
            item_id=item_id,
            size=size or None,
        )

    async def add_to_cart(self, item_id: str, quantity: int = 1) -> bool:
        """Add a product to the Instacart cart by item_id."""
        if not self._cart_id:
            await self._discover_cart_id()
        if not self._cart_id:
            logger.error("Cannot add to cart — no cart ID")
            return False

        variables = {
            "cartItemUpdates": [
                {
                    "itemId": item_id,
                    "quantity": quantity,
                    "quantityType": "each",
                }
            ],
            "cartType": "grocery",
            "requestTimestamp": int(time.time() * 1000),
            "cartId": self._cart_id,
        }

        try:
            data = await self._graphql_post("UpdateCartItemsMutation", variables)
            result = data.get("data", {}).get("updateCartItems", {})
            typename = result.get("__typename", "")
            if "Success" in typename:
                logger.info("Added %s to cart (qty=%d)", item_id, quantity)
                return True
            logger.warning("Add to cart response type: %s", typename)
            return False
        except Exception as e:
            logger.error("Add to cart failed for %s: %s", item_id, e)
            return False

    async def get_active_carts(self) -> list[dict]:
        """Get all active carts across retailers."""
        data = await self._graphql_get("PersonalActiveCarts", {})
        return data.get("data", {}).get("userCarts", {}).get("carts", [])

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_placement_items(placement: dict) -> tuple[list[dict], list[str]]:
        """Extract items and itemIds from a search placement, skipping ads.

        Items are nested at: placement.content.items / placement.content.itemIds.
        Ad/sponsored placements (content.__typename starting with "Ads") are
        skipped entirely so organic results come first.
        """
        content = placement.get("content") or {}

        # Skip ad/sponsored placements
        content_type = content.get("__typename") or ""
        if content_type.startswith("Ads"):
            return [], []

        # Regular product placements
        items = content.get("items") or []
        item_ids = content.get("itemIds") or []

        return items, item_ids

    async def _discover_cart_id(self):
        try:
            carts = await self.get_active_carts()
            for cart in carts:
                slug = cart.get("retailer", {}).get("slug", "").lower()
                if slug == self._retailer_slug:
                    self._cart_id = cart["id"]
                    logger.info("Discovered cart ID: %s for %s", self._cart_id, self._retailer_slug)
                    return
            logger.warning("No active cart found for %s", self._retailer_slug)
        except Exception as e:
            logger.warning("Failed to discover cart ID: %s", e)

    async def _fetch_items_by_id(self, item_ids: list[str]) -> list[ProductResult]:
        if not item_ids:
            return []

        variables = {
            "ids": item_ids,
            "shopId": self._shop_id,
            "zoneId": self._zone_id,
            "postalCode": self._postal_code,
        }

        data = await self._graphql_get("Items", variables)
        items = data.get("data", {}).get("items", [])
        return [pr for item in items if (pr := self._parse_item(item))]

    def _parse_item(self, item: dict) -> ProductResult | None:
        if not item:
            return None

        name = item.get("name", "")
        if not name:
            return None

        product_id = item.get("productId", "")
        item_id = item.get("id", "")
        brand = item.get("brandName", "")
        size = item.get("size", "")

        availability = item.get("availability", {}) or {}
        in_stock = availability.get("available", True)

        price_data = item.get("price", {}) or {}
        price_vs = price_data.get("viewSection", {}) or {}
        price = price_vs.get("priceString", "")

        vs = item.get("viewSection", {}) or {}
        item_image = vs.get("itemImage", {}) or {}
        image_url = item_image.get("url", "")

        evergreen_url = item.get("evergreenUrl", "")
        product_url = f"https://www.instacart.com/products/{evergreen_url}" if evergreen_url else None

        return ProductResult(
            product_name=name,
            product_url=product_url,
            brand=brand.title() if brand else None,
            price=price or None,
            image_url=image_url or None,
            in_stock=in_stock,
            product_id=product_id or None,
            item_id=item_id or None,
            size=size or None,
        )

    @staticmethod
    def _extract_product_slug(url: str) -> str | None:
        m = re.search(r"/products/([^?#]+)", url)
        return m.group(1) if m else None

    @staticmethod
    def _extract_product_id_from_url(url: str) -> str | None:
        m = re.search(r"/products/(\d+)", url)
        return m.group(1) if m else None


class InstacartAuthError(Exception):
    """Raised when Instacart session cookies are expired/invalid."""
