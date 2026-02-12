"""
Instacart browser-use agent.

Uses browser-use to:
1. Search for products on Instacart (scoped to configured store)
2. Extract product details (name, brand, price, image, stock status, URL)
3. Add products to the cart — by direct URL when available, search as fallback
"""

import logging
from dataclasses import dataclass
from urllib.parse import quote

from pydantic import BaseModel

from alexacart.config import settings

logger = logging.getLogger(__name__)

INSTACART_BASE = "https://www.instacart.com"

# Common instruction appended to all agent tasks to handle Instacart popups
DISMISS_MODALS = (
    "IMPORTANT: If a modal/popup appears asking you to rate a delivery driver, "
    "close it by clicking the X button (usually top-left). If you can't find the X, "
    "just click the rightmost star (5 stars) and submit. "
    "Dismiss any other popups or modals before proceeding with the task."
)


@dataclass
class ProductResult:
    product_name: str
    product_url: str | None = None
    brand: str | None = None
    price: str | None = None
    image_url: str | None = None
    in_stock: bool = True


class SearchResults(BaseModel):
    products: list[dict]


class InstacartAgent:
    def __init__(self):
        self._session = None

    async def _get_session(self):
        if self._session is None:
            from browser_use import BrowserSession

            profile_dir = settings.resolved_data_dir / "browser-profile"
            profile_dir.mkdir(parents=True, exist_ok=True)
            self._session = BrowserSession(
                headless=False,
                user_data_dir=str(profile_dir),
                keep_alive=True,
            )
        return self._session

    async def ensure_logged_in(self) -> bool:
        """
        Check if logged into Instacart. If not, navigate to login page
        and poll until the user signs in manually.
        Returns True once logged in.
        """
        import asyncio

        from browser_use import Agent, ChatBrowserUse

        session = await self._get_session()

        # First check: go to Instacart and see if we're logged in
        check_task = (
            f"Go to {INSTACART_BASE} . "
            f"Check if the user is logged into Instacart. "
            f"Look for signs of being logged in: a user/account icon in the header, "
            f"a cart icon with items, or a store page showing products. "
            f"If you see a 'Log in' or 'Sign up' button, the user is NOT logged in — "
            f"click 'Log in' to go to the login page and say 'NEEDS_LOGIN'. "
            f"If already logged in, say 'LOGGED_IN'. "
            f"{DISMISS_MODALS}"
        )

        agent = Agent(
            task=check_task,
            llm=ChatBrowserUse(model="bu-2-0"),
            browser_session=session,
            max_actions_per_step=5,
            use_vision=True,
            use_judge=False,
        )

        history = await agent.run(max_steps=8)
        raw = str(history.final_result() or "")

        if "LOGGED_IN" in raw.upper() and "NEEDS_LOGIN" not in raw.upper():
            logger.info("Already logged into Instacart")
            return True

        # Not logged in — browser should now be on the login page.
        # Poll until the user logs in.
        logger.info("Not logged into Instacart — waiting for manual login...")

        for attempt in range(60):  # Up to ~10 minutes
            await asyncio.sleep(10)

            poll_task = (
                f"Look at the current page. "
                f"If this is still a login/sign-up page, say 'NEEDS_LOGIN'. "
                f"If the user has logged in (you see a store page, account icon, "
                f"or home page with products), say 'LOGGED_IN'. "
                f"{DISMISS_MODALS}"
            )

            poll_agent = Agent(
                task=poll_task,
                llm=ChatBrowserUse(model="bu-2-0"),
                browser_session=session,
                max_actions_per_step=2,
                use_vision=True,
            )

            try:
                poll_history = await poll_agent.run(max_steps=3)
                poll_raw = str(poll_history.final_result() or "")
                if "LOGGED_IN" in poll_raw.upper() and "NEEDS_LOGIN" not in poll_raw.upper():
                    logger.info("User logged into Instacart successfully")
                    return True
            except Exception:
                pass  # Keep waiting

        logger.error("Timed out waiting for Instacart login")
        return False

    async def search_product(self, query: str, store: str | None = None) -> list[ProductResult]:
        """
        Search Instacart for a product and return top results.
        """
        from browser_use import Agent, ChatBrowserUse

        store_name = store or settings.instacart_store
        session = await self._get_session()

        task = (
            f"Go to {INSTACART_BASE}/store/{store_name.lower()}/search/{quote(query)} "
            f"and extract the product search results. "
            f"For each product visible on the page, extract: "
            f"1. The full product name "
            f"2. The brand name (if shown) "
            f"3. The price "
            f"4. The product page URL (the href of the link to the product detail page) "
            f"5. The image URL (from the img src attribute) "
            f"6. Whether it appears to be in stock (not showing 'out of stock' or similar) "
            f"Return the top 5 results. If there are no results, return an empty list. "
            f"{DISMISS_MODALS}"
        )

        agent = Agent(
            task=task,
            llm=ChatBrowserUse(model="bu-2-0"),
            browser_session=session,
            max_actions_per_step=5,
            use_vision=True,
            use_judge=False,
        )

        try:
            history = await agent.run(max_steps=20)
            raw = history.final_result()
            return self._parse_search_results(raw)
        except Exception as e:
            logger.error("Instacart search failed for '%s': %s", query, e)
            return []

    async def check_product_by_url(self, product_url: str) -> ProductResult | None:
        """
        Navigate directly to a product URL and check if it's in stock.
        Returns ProductResult if the page loads and the product exists, None otherwise.
        """
        from browser_use import Agent, ChatBrowserUse

        session = await self._get_session()

        # Ensure absolute URL
        if product_url.startswith("/"):
            product_url = INSTACART_BASE + product_url

        task = (
            f"Go to {product_url} . "
            f"This is an Instacart product page. "
            f"Extract: the full product name, brand, price, image URL, "
            f"and whether it is in stock (look for 'Add to cart' button vs 'out of stock' or '404' page). "
            f"If the page shows a 404 error or the product doesn't exist, say 'NOT FOUND'. "
            f"If the product is out of stock, say 'OUT OF STOCK' along with the product details. "
            f"{DISMISS_MODALS}"
        )

        agent = Agent(
            task=task,
            llm=ChatBrowserUse(model="bu-2-0"),
            browser_session=session,
            max_actions_per_step=5,
            use_vision=True,
            use_judge=False,
        )

        try:
            history = await agent.run(max_steps=10)
            raw = history.final_result()
            raw_str = str(raw or "")
            if "NOT FOUND" in raw_str.upper() or "404" in raw_str:
                return None
            results = self._parse_search_results(raw)
            if results:
                result = results[0]
                result.product_url = product_url
                result.in_stock = "OUT OF STOCK" not in raw_str.upper()
                return result
            return None
        except Exception as e:
            logger.error("Instacart URL check failed for '%s': %s", product_url, e)
            return None

    async def add_to_cart_by_url(self, product_url: str) -> bool:
        """
        Navigate directly to a product page and add it to the cart.
        Returns True if successful.
        """
        from browser_use import Agent, ChatBrowserUse

        session = await self._get_session()

        if product_url.startswith("/"):
            product_url = INSTACART_BASE + product_url

        task = (
            f"Go to {product_url} . "
            f"This is an Instacart product page. "
            f"Click the 'Add to cart' button. "
            f"Wait for confirmation that it was added to the cart. "
            f"If the product is out of stock or can't be added, say 'FAILED'. "
            f"If successfully added, say 'SUCCESS'. "
            f"{DISMISS_MODALS}"
        )

        agent = Agent(
            task=task,
            llm=ChatBrowserUse(model="bu-2-0"),
            browser_session=session,
            max_actions_per_step=5,
            use_vision=True,
            use_judge=False,
        )

        try:
            history = await agent.run(max_steps=15)
            raw = str(history.final_result() or "")
            success = "SUCCESS" in raw.upper() and "FAILED" not in raw.upper()
            if success:
                logger.info("Added product to Instacart cart via URL: %s", product_url)
            else:
                logger.warning("Failed to add product via URL %s: %s", product_url, raw)
            return success
        except Exception as e:
            logger.error("Instacart add-to-cart by URL failed for '%s': %s", product_url, e)
            return False

    async def add_to_cart(self, product_name: str, product_url: str | None = None, store: str | None = None) -> bool:
        """
        Add a product to the Instacart cart.
        Uses direct URL navigation if product_url is provided, falls back to search.
        Returns True if successful.
        """
        if product_url:
            result = await self.add_to_cart_by_url(product_url)
            if result:
                return True
            logger.warning("Direct URL add failed for '%s', falling back to search", product_name)

        # Fallback: search by name
        from browser_use import Agent, ChatBrowserUse

        store_name = store or settings.instacart_store
        session = await self._get_session()

        task = (
            f"Go to {INSTACART_BASE}/store/{store_name.lower()}/search "
            f"and search for: {product_name}. "
            f"Find the product that best matches this name. "
            f"Click the 'Add' or 'Add to cart' button for that product. "
            f"Wait for confirmation that it was added to the cart. "
            f"If the product is out of stock or can't be added, say 'FAILED'. "
            f"If successfully added, say 'SUCCESS'. "
            f"{DISMISS_MODALS}"
        )

        agent = Agent(
            task=task,
            llm=ChatBrowserUse(model="bu-2-0"),
            browser_session=session,
            max_actions_per_step=5,
            use_vision=True,
            use_judge=False,
        )

        try:
            history = await agent.run(max_steps=25)
            raw = str(history.final_result() or "")
            success = "SUCCESS" in raw.upper() and "FAILED" not in raw.upper()
            if success:
                logger.info("Added '%s' to Instacart cart via search", product_name)
            else:
                logger.warning("Failed to add '%s' to Instacart cart: %s", product_name, raw)
            return success
        except Exception as e:
            logger.error("Instacart add-to-cart failed for '%s': %s", product_name, e)
            return False

    def _parse_search_results(self, raw_result) -> list[ProductResult]:
        """Parse the raw agent output into ProductResult objects."""
        results = []

        if raw_result is None:
            return results

        raw = str(raw_result)

        if isinstance(raw_result, dict):
            products = raw_result.get("products", [raw_result])
            for p in products:
                results.append(
                    ProductResult(
                        product_name=p.get("product_name", p.get("name", "Unknown")),
                        product_url=p.get("product_url", p.get("url")),
                        brand=p.get("brand"),
                        price=p.get("price"),
                        image_url=p.get("image_url", p.get("image")),
                        in_stock=p.get("in_stock", True),
                    )
                )
        elif isinstance(raw_result, list):
            for p in raw_result:
                if isinstance(p, dict):
                    results.append(
                        ProductResult(
                            product_name=p.get("product_name", p.get("name", "Unknown")),
                            product_url=p.get("product_url", p.get("url")),
                            brand=p.get("brand"),
                            price=p.get("price"),
                            image_url=p.get("image_url", p.get("image")),
                            in_stock=p.get("in_stock", True),
                        )
                    )
        else:
            if raw.strip() and "NOT FOUND" not in raw.upper():
                results.append(
                    ProductResult(
                        product_name=raw.strip()[:200],
                        in_stock="out of stock" not in raw.lower(),
                    )
                )

        return results

    async def close(self):
        if self._session:
            await self._session.close()
            self._session = None
