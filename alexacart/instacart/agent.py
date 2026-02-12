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

            # Clean up stale Chrome lock file if no Chrome process is using the profile
            self._cleanup_stale_lock(profile_dir)

            self._session = BrowserSession(
                headless=False,
                user_data_dir=str(profile_dir),
                keep_alive=True,
            )
        return self._session

    @staticmethod
    def _cleanup_stale_lock(profile_dir):
        """Remove Chrome's SingletonLock if no Chrome process holds it."""
        import subprocess

        lock_file = profile_dir / "SingletonLock"
        if not lock_file.exists():
            return

        # Check if any Chrome process is using this profile directory
        try:
            result = subprocess.run(
                ["pgrep", "-f", str(profile_dir)],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode != 0:
                # No matching process found — lock is stale
                logger.info("Removing stale Chrome lock file: %s", lock_file)
                lock_file.unlink(missing_ok=True)
        except Exception as e:
            logger.debug("Could not check for stale lock: %s", e)

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
                use_judge=False,
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

    async def ensure_amazon_logged_in(self) -> bool:
        """
        Check if logged into Amazon in the persistent browser.
        If not, navigate to Amazon login and poll until the user signs in.
        Returns True once logged in.
        """
        import asyncio

        from browser_use import Agent, ChatBrowserUse

        session = await self._get_session()

        check_task = (
            "Go to https://www.amazon.com . "
            "Check if the user is logged into Amazon. "
            "Look for signs of being logged in: a greeting like 'Hello, [Name]' "
            "in the header, or an 'Account & Lists' dropdown showing a name. "
            "If you see a 'Sign in' link/button or 'Hello, sign in', the user is NOT logged in — "
            "click 'Sign in' to go to the login page and say 'NEEDS_LOGIN'. "
            "If already logged in, say 'LOGGED_IN'. "
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
            logger.info("Already logged into Amazon")
            return True

        logger.info("Not logged into Amazon — waiting for manual login...")

        for attempt in range(60):
            await asyncio.sleep(10)

            poll_task = (
                "Look at the current page. "
                "If this is still an Amazon login/sign-in page, say 'NEEDS_LOGIN'. "
                "If the user has logged in (you see 'Hello, [Name]' or an account page), "
                "say 'LOGGED_IN'."
            )

            poll_agent = Agent(
                task=poll_task,
                llm=ChatBrowserUse(model="bu-2-0"),
                browser_session=session,
                max_actions_per_step=2,
                use_vision=True,
                use_judge=False,
            )

            try:
                poll_history = await poll_agent.run(max_steps=3)
                poll_raw = str(poll_history.final_result() or "")
                if "LOGGED_IN" in poll_raw.upper() and "NEEDS_LOGIN" not in poll_raw.upper():
                    logger.info("User logged into Amazon successfully")
                    return True
            except Exception:
                pass

        logger.error("Timed out waiting for Amazon login")
        return False

    async def get_amazon_cookies(self) -> dict:
        """
        Extract Amazon cookies from the persistent browser session.
        Returns cookie data in the format expected by AlexaClient.
        """
        session = await self._get_session()

        # Ensure browser is initialized (agent.run() should have started it already)
        try:
            session.cdp_client  # property raises if not connected
        except (AssertionError, AttributeError):
            await session.start()

        all_cookies = await session._cdp_get_cookies()
        cookies = {}
        for cookie in all_cookies:
            domain = cookie.get("domain", "")
            if "amazon" in domain:
                cookies[cookie["name"]] = cookie["value"]

        logger.info("Extracted %d Amazon cookies from browser session", len(cookies))
        return {"cookies": cookies, "source": "browser_session"}

    async def search_product(self, query: str, store: str | None = None) -> list[ProductResult]:
        """
        Search Instacart for a product and return top results.
        """
        from browser_use import Agent, ChatBrowserUse

        store_name = store or settings.instacart_store
        session = await self._get_session()

        task = (
            f"Go to {INSTACART_BASE}/store/{store_name.lower()}/search/{quote(query)} . "
            f"Wait for results to load, then use the extract tool ONCE to get the top 3 results. "
            f"For each product extract: product name, brand, price, and product page URL. "
            f"Do NOT extract image URLs — skip them entirely. "
            f"Return results immediately after the first successful extraction. "
            f"If there are no results, return an empty list. "
            f"{DISMISS_MODALS}"
        )

        agent = Agent(
            task=task,
            llm=ChatBrowserUse(model="bu-2-0"),
            browser_session=session,
            max_actions_per_step=3,
            use_vision=True,
            use_judge=False,
        )

        try:
            history = await agent.run(max_steps=8)
            raw = history.final_result()
            results = self._parse_search_results(raw)
            # Backfill image URLs via fast JS extraction (no extra LLM call)
            await self._backfill_images(results)
            return results
        except Exception as e:
            logger.error("Instacart search failed for '%s': %s", query, e)
            return []

    async def _backfill_images(self, results: list[ProductResult]) -> None:
        """
        Extract product images from the current search page via JavaScript.
        Matches images to results by product URL. Fast — no LLM call needed.
        """
        if not results:
            return

        session = await self._get_session()
        try:
            cdp_session = await session.get_or_create_cdp_session(target_id=None)
            resp = await cdp_session.cdp_client.send.Runtime.evaluate(
                params={
                    "expression": """
                        (function() {
                            const cards = document.querySelectorAll('a[role="button"][href*="/products/"]');
                            const data = [];
                            for (let i = 0; i < cards.length && data.length < 10; i++) {
                                const href = cards[i].getAttribute('href') || '';
                                const img = cards[i].querySelector('img');
                                const src = img ? (img.getAttribute('src') || '') : '';
                                if (href && src) {
                                    data.push({href: href, src: src});
                                }
                            }
                            return JSON.stringify(data);
                        })()
                    """,
                    "returnByValue": True,
                },
                session_id=cdp_session.session_id,
            )

            raw_value = resp.get("result", {}).get("value", "[]")
            import json
            image_map = {}
            for item in json.loads(raw_value):
                # Normalize href to match product URLs
                href = item.get("href", "")
                src = item.get("src", "")
                if href and src:
                    # Store by the product ID portion of the URL
                    image_map[href] = src
                    # Also store with full base URL
                    if href.startswith("/"):
                        image_map[INSTACART_BASE + href] = src

            # Match images to results by URL
            for result in results:
                if result.product_url and not result.image_url:
                    result.image_url = image_map.get(result.product_url)
                    # Try matching by partial path
                    if not result.image_url and result.product_url.startswith(INSTACART_BASE):
                        path = result.product_url[len(INSTACART_BASE):]
                        result.image_url = image_map.get(path)

            logger.debug("Backfilled images: %d/%d results have images",
                         sum(1 for r in results if r.image_url), len(results))

        except Exception as e:
            logger.warning("Image backfill failed (non-critical): %s", e)

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
            # Try to parse structured text (markdown) from agent output
            results.extend(self._parse_text_results(raw))

        return results

    def _parse_text_results(self, text: str) -> list[ProductResult]:
        """Parse free-text/markdown agent output into ProductResult objects."""
        import re

        results = []
        if not text.strip() or "NOT FOUND" in text.upper():
            return results

        # Normalize literal \n sequences to actual newlines
        # (browser-use agent sometimes returns escaped newlines)
        text = text.replace("\\n", "\n")

        logger.debug("Parsing text results:\n%s", text[:500])

        # Split into numbered items (e.g. "1. **Product Name**" or "1. Product Name")
        # Handle newlines, semicolons, colons, or start-of-string before numbered items
        # e.g. "\n1. ", "; 2. ", ": 1. ", or text starting with "1. "
        items = re.split(r'(?:^|[\n;:])[\s]*\d+\.\s+', text)
        for item_text in items:
            if not item_text.strip():
                continue

            # Try multiple name extraction strategies:
            name = None

            # Strategy 1: Bold markdown — **Product Name**
            # But skip if it's a label like **Brand:** or **Price:**
            bold_match = re.search(r'\*\*([^*]+?)\*\*', item_text)
            if bold_match:
                candidate = bold_match.group(1).strip()
                # Skip if it's a field label (contains a colon at end)
                if not candidate.endswith(':') and 'Product Name' not in candidate:
                    name = candidate

            # Strategy 2: "Product Name: X" or "Full product name: X" or "Name: X"
            if not name:
                name_field = re.search(
                    r'(?:full\s+)?(?:product\s+)?name\s*:\s*\*?\*?(.+?)(?:\*\*|\n|$)',
                    item_text, re.IGNORECASE
                )
                if name_field:
                    name = name_field.group(1).strip().strip('*').strip()

            # Strategy 3: Inline format — "Product Name (size) - Brand: X, Price: $Y, URL: ..."
            # Take text before first " - Brand:" or " - Price:" or ", Brand:"
            if not name:
                inline_match = re.match(
                    r'(.+?)(?:\s*[-–]\s*[Bb]rand:|,\s*[Bb]rand:|,\s*[Pp]rice:)',
                    item_text.split('\n')[0],
                )
                if inline_match:
                    name = inline_match.group(1).strip().strip('*').strip()

            # Strategy 4: First non-empty line (fallback)
            if not name:
                first_line = item_text.split('\n')[0].strip().strip('*').strip()
                # Strip common prefixes
                first_line = re.sub(r'^(?:Product\s+Name|Name)\s*:\s*', '', first_line, flags=re.IGNORECASE)
                if first_line and len(first_line) < 200:
                    name = first_line

            if not name or len(name) > 200:
                continue

            # Extract price
            price_match = re.search(r'\$[\d.]+', item_text)
            price = price_match.group(0) if price_match else None

            # Extract brand — stop at **, newline, comma-before-field, or end
            brand_match = re.search(
                r'[Bb]rand(?:\s+[Nn]ame)?\s*:\s*\*?\*?(.+?)(?:\*\*|,\s*(?:[Pp]rice|URL|[Pp]roduct)|\n|$)',
                item_text,
            )
            brand = brand_match.group(1).strip().strip('*').strip() if brand_match else None

            # Extract URL
            url_match = re.search(r'(https?://\S*instacart\S*/products/\S+)', item_text)
            if not url_match:
                url_match = re.search(r'(/products/\S+)', item_text)
            product_url = url_match.group(1).rstrip('*);,.') if url_match else None
            if product_url and product_url.startswith('/'):
                product_url = INSTACART_BASE + product_url

            # Skip preamble fragments that have no product data
            if not product_url and not price and not brand:
                continue

            in_stock = "out of stock" not in item_text.lower()

            results.append(
                ProductResult(
                    product_name=name,
                    product_url=product_url,
                    brand=brand,
                    price=price,
                    in_stock=in_stock,
                )
            )

        logger.debug("Parsed %d results from text", len(results))
        return results

    async def close(self):
        if self._session:
            try:
                await self._session.stop()
            except Exception as e:
                logger.warning("Error stopping browser session: %s", e)
            self._session = None
