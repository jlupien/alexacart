"""
Instacart browser-use agent.

Uses browser-use to:
1. Search for products on Instacart (scoped to configured store)
2. Extract product details (name, brand, price, image, stock status, URL)
3. Add products to the cart — by direct URL when available, search as fallback
"""

import asyncio
import logging
from dataclasses import dataclass
from urllib.parse import quote

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


class InstacartAgent:
    def __init__(self, profile_suffix: str | None = None, headless: bool = False,
                 on_status=None):
        self._session = None
        self._profile_suffix = profile_suffix
        self._headless = headless
        self._on_status = on_status

    async def _get_session(self):
        if self._session is None:
            from browser_use import BrowserSession

            if self._profile_suffix:
                profile_dir = settings.resolved_data_dir / f"browser-profile-{self._profile_suffix}"
            else:
                profile_dir = settings.resolved_data_dir / "browser-profile"
            profile_dir.mkdir(parents=True, exist_ok=True)

            # Clean up stale Chrome lock file if no Chrome process is using the profile
            self._cleanup_stale_lock(profile_dir)

            self._session = BrowserSession(
                headless=self._headless,
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

    async def extract_all_cookies(self) -> list[dict]:
        """Extract all browser cookies via CDP (for sharing with worker sessions)."""
        session = await self._get_session()
        return await session._cdp_get_cookies()

    async def inject_cookies(self, cookies: list[dict]) -> None:
        """Inject cookies into this browser session via CDP."""
        session = await self._get_session()
        # Ensure browser is started
        try:
            session.cdp_client
        except (AssertionError, AttributeError):
            await session.start()

        # Navigate to Instacart so cookie domain context is established
        await session.navigate_to(f"{INSTACART_BASE}")
        await asyncio.sleep(1)

        cdp_session = await session.get_or_create_cdp_session(target_id=None)
        # Filter to Instacart-related cookies
        ic_cookies = [c for c in cookies if "instacart" in c.get("domain", "")]
        if ic_cookies:
            await cdp_session.cdp_client.send.Network.setCookies(
                params={"cookies": ic_cookies},
                session_id=cdp_session.session_id,
            )
        logger.info("Injected %d Instacart cookies into worker session", len(ic_cookies))

    async def create_search_pool(self, count: int) -> list["InstacartAgent"]:
        """
        Create a pool of headless worker agents with cookies from this (main) agent.
        Workers share the same Instacart auth but use separate browser instances.
        """
        cookies = await self.extract_all_cookies()
        workers = []

        async def _start_worker(i: int) -> "InstacartAgent":
            worker = InstacartAgent(profile_suffix=f"search-worker-{i}", headless=True)
            await worker.inject_cookies(cookies)
            return worker

        # Start all workers in parallel
        results = await asyncio.gather(
            *[_start_worker(i) for i in range(count)],
            return_exceptions=True,
        )
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.warning("Failed to start search worker %d: %s", i, result)
            else:
                workers.append(result)

        logger.info("Created %d search workers (requested %d)", len(workers), count)
        return workers

    async def _run_agent(self, task: str, max_actions_per_step: int = 5, max_steps: int = 8):
        """Create and run a browser-use agent with standard settings."""
        from browser_use import Agent, ChatBrowserUse

        session = await self._get_session()
        agent = Agent(
            task=task,
            llm=ChatBrowserUse(model="bu-2-0"),
            browser_session=session,
            max_actions_per_step=max_actions_per_step,
            use_vision=True,
            use_judge=False,
        )
        return await agent.run(max_steps=max_steps)

    async def _run_js(self, js_code: str) -> str:
        """Run JavaScript on the current page and return the string result."""
        session = await self._get_session()
        cdp_session = await session.get_or_create_cdp_session(target_id=None)
        resp = await cdp_session.cdp_client.send.Runtime.evaluate(
            params={"expression": js_code, "returnByValue": True},
            session_id=cdp_session.session_id,
        )
        return str(resp.get("result", {}).get("value", ""))

    async def _ensure_started(self):
        """Ensure the browser session is started."""
        session = await self._get_session()
        try:
            session.cdp_client
        except (AssertionError, AttributeError):
            await session.start()
        return session

    async def _restart_as(self, headless: bool):
        """Stop current session and restart with a different headless mode."""
        if self._session:
            try:
                await self._session.stop()
            except Exception as e:
                logger.warning("Error stopping session for restart: %s", e)
            self._session = None
        self._headless = headless
        await self._ensure_started()

    async def _ensure_service_logged_in(
        self,
        service_name: str,
        check_url: str,
        login_url: str,
        check_js: str,
        poll_js: str,
    ) -> bool:
        """
        Fast login check via JS (no LLM call).
        Navigate to check_url, run check_js. If not logged in, open a visible
        browser window for manual login and poll with poll_js every 3s.
        Returns to headless after login completes.
        """
        session = await self._ensure_started()

        await session.navigate_to(check_url)
        await asyncio.sleep(2)

        try:
            result = await self._run_js(check_js)
        except Exception as e:
            logger.warning("JS login check failed for %s: %s", service_name, e)
            result = ""

        if "LOGGED_IN" in result.upper() and "NEEDS_LOGIN" not in result.upper():
            logger.info("Already logged into %s (fast check)", service_name)
            return True

        # Not logged in — show a visible browser for manual login
        showed_browser = False
        if self._headless:
            logger.info("Login needed for %s — opening visible browser", service_name)
            if self._on_status:
                self._on_status(f"Login needed — opening {service_name} window...")
            await self._restart_as(headless=False)
            showed_browser = True

        if self._on_status:
            self._on_status(
                f"Waiting for {service_name} login — please log in via the browser window"
            )

        session = await self._get_session()
        logger.info("Not logged into %s — navigating to login page...", service_name)
        await session.navigate_to(login_url)

        # Poll with JS every 3s (much faster than LLM-based polling)
        logged_in = False
        for _ in range(200):  # Up to ~10 minutes
            await asyncio.sleep(3)
            try:
                poll_result = await self._run_js(poll_js)
                if "LOGGED_IN" in poll_result.upper() and "NEEDS_LOGIN" not in poll_result.upper():
                    logger.info("User logged into %s successfully", service_name)
                    logged_in = True
                    break
            except Exception:
                logger.debug("Poll attempt for %s login failed", service_name)

        if not logged_in:
            logger.error("Timed out waiting for %s login", service_name)

        # Return to headless now that login is done (or timed out)
        if showed_browser:
            await self._restart_as(headless=True)

        return logged_in

    async def ensure_logged_in(self) -> bool:
        """Check if logged into Instacart via fast JS check."""
        check_js = """
            (function() {
                var links = document.querySelectorAll('a, button');
                for (var i = 0; i < links.length; i++) {
                    var text = (links[i].textContent || '').trim();
                    if (text === 'Log in' || text === 'Sign up') return 'NEEDS_LOGIN';
                }
                return 'LOGGED_IN';
            })()
        """
        poll_js = """
            (function() {
                var url = window.location.href;
                if (/\\/login|\\/signup/i.test(url)) return 'NEEDS_LOGIN';
                return 'LOGGED_IN';
            })()
        """
        return await self._ensure_service_logged_in(
            "Instacart",
            INSTACART_BASE,
            f"{INSTACART_BASE}/login",
            check_js,
            poll_js,
        )

    async def ensure_amazon_logged_in(self) -> bool:
        """Check if logged into Amazon via fast JS check."""
        check_js = """
            (function() {
                var el = document.getElementById('nav-link-accountList');
                if (!el) return 'NEEDS_LOGIN';
                var text = el.innerText || el.textContent || '';
                if (/sign in/i.test(text)) return 'NEEDS_LOGIN';
                return 'LOGGED_IN';
            })()
        """
        # Poll using the same DOM check — URL-based polling is unreliable
        # because the sign-in URL can 404 or change across redirects.
        # On sign-in pages (no #nav-link-accountList), returns NEEDS_LOGIN.
        # After login redirect back to amazon.com, detects LOGGED_IN.
        poll_js = check_js
        return await self._ensure_service_logged_in(
            "Amazon",
            "https://www.amazon.com",
            "https://www.amazon.com",
            check_js,
            poll_js,
        )

    async def ensure_amazon_logged_in_and_get_cookies(self) -> dict | None:
        """
        Check Amazon login, then navigate to alexa.amazon.com to establish
        Alexa session cookies and extract them — all in one flow.
        Returns cookie data dict on success, None if login failed.
        """
        ok = await self.ensure_amazon_logged_in()
        if not ok:
            return None
        return await self.get_amazon_cookies()

    async def get_amazon_cookies(self) -> dict:
        """
        Extract Amazon cookies from the persistent browser session.
        Navigates to alexa.amazon.com first to establish Alexa session cookies
        (csrf token, etc.) which are separate from www.amazon.com cookies.
        Returns cookie data in the format expected by AlexaClient.
        """
        session = await self._ensure_started()

        # Visit alexa.amazon.com to establish Alexa session cookies (csrf, etc.)
        # These are on a different domain than www.amazon.com and won't exist
        # unless the browser actually visits the Alexa subdomain.
        # The SPA sets the csrf cookie via JavaScript, so we poll for it rather
        # than using a fixed sleep — page load times vary widely.
        try:
            await session.navigate_to("https://alexa.amazon.com/spa/index.html")
            all_cookies = None
            for _ in range(15):
                await asyncio.sleep(1)
                all_cookies = await session._cdp_get_cookies()
                if any(
                    c.get("name") == "csrf" and "amazon" in c.get("domain", "")
                    for c in all_cookies
                ):
                    logger.info("Alexa csrf cookie found")
                    break
            else:
                logger.warning("Alexa csrf cookie not found after 15s — extracting available cookies")
        except Exception as e:
            logger.warning("Failed to navigate to alexa.amazon.com: %s", e)
            all_cookies = None

        if all_cookies is None:
            all_cookies = await session._cdp_get_cookies()

        cookies = {}
        for cookie in all_cookies:
            domain = cookie.get("domain", "")
            if "amazon" in domain:
                cookies[cookie["name"]] = cookie["value"]

        logger.info("Extracted %d Amazon cookies from browser session", len(cookies))
        return {"cookies": cookies, "source": "browser_session"}

    async def search_product(self, query: str, store: str | None = None) -> list[ProductResult]:
        """Search Instacart for a product and return top results."""
        store_name = store or settings.instacart_store

        task = (
            f"Go to {INSTACART_BASE}/store/{store_name.lower()}/search/{quote(query)} . "
            f"Wait for results to load, then use the extract tool ONCE to get the top 3 results. "
            f"For each product extract: product name, brand, price, and product page URL. "
            f"Do NOT extract image URLs — skip them entirely. "
            f"Return results immediately after the first successful extraction. "
            f"If there are no results, return an empty list. "
            f"{DISMISS_MODALS}"
        )

        try:
            history = await self._run_agent(task, max_actions_per_step=3)
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

            import json
            raw_value = resp.get("result", {}).get("value", "[]")
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
        # Ensure absolute URL
        if product_url.startswith("/"):
            product_url = INSTACART_BASE + product_url

        task = (
            f"Go to {product_url} . "
            f"This is an Instacart product page. "
            f"Extract: the full product name, brand, and price. "
            f"Do NOT extract image URLs — skip them entirely. "
            f"Also check whether it is in stock (look for 'Add to cart' button vs 'out of stock' or '404' page). "
            f"If the page shows a 404 error or the product doesn't exist, say 'NOT FOUND'. "
            f"If the product is out of stock, say 'OUT OF STOCK' along with the product details. "
            f"{DISMISS_MODALS}"
        )

        try:
            history = await self._run_agent(task, max_steps=10)
            raw = history.final_result()
            raw_str = str(raw or "")
            if "NOT FOUND" in raw_str.upper() or "404" in raw_str:
                return None
            results = self._parse_search_results(raw)
            if results:
                result = results[0]
                result.product_url = product_url
                result.in_stock = "OUT OF STOCK" not in raw_str.upper()
                # Fast JS-based image extraction from product detail page
                result.image_url = await self._extract_product_page_image()
                return result
            return None
        except Exception as e:
            logger.error("Instacart URL check failed for '%s': %s", product_url, e)
            return None

    async def _extract_product_page_image(self) -> str | None:
        """
        Extract the main product image from the current product detail page via JavaScript.
        Fast — no LLM call needed.
        """
        session = await self._get_session()
        try:
            cdp_session = await session.get_or_create_cdp_session(target_id=None)
            resp = await cdp_session.cdp_client.send.Runtime.evaluate(
                params={
                    "expression": """
                        (function() {
                            // Strategy 1: find product images by CDN URL patterns
                            // Instacart product images are hosted on their image CDN
                            var candidates = Array.from(document.querySelectorAll('img'))
                                .filter(function(img) {
                                    var src = img.src || '';
                                    // Match Instacart CDN image URLs (product photos)
                                    return (src.indexOf('instacartassets') !== -1
                                         || src.indexOf('product-image') !== -1
                                         || src.indexOf('/image-server/') !== -1)
                                        && img.naturalWidth > 50
                                        && img.naturalHeight > 50;
                                });
                            if (candidates.length) {
                                // Pick the largest one (likely the hero image)
                                candidates.sort(function(a, b) {
                                    return (b.naturalWidth * b.naturalHeight)
                                         - (a.naturalWidth * a.naturalHeight);
                                });
                                return candidates[0].src;
                            }
                            // Strategy 2: any large image that isn't a logo/icon
                            var all = Array.from(document.querySelectorAll('img'))
                                .filter(function(img) {
                                    return img.naturalWidth >= 100 && img.naturalHeight >= 100;
                                });
                            if (all.length) {
                                all.sort(function(a, b) {
                                    return (b.naturalWidth * b.naturalHeight)
                                         - (a.naturalWidth * a.naturalHeight);
                                });
                                return all[0].src;
                            }
                            return '';
                        })()
                    """,
                    "returnByValue": True,
                },
                session_id=cdp_session.session_id,
            )
            value = resp.get("result", {}).get("value", "")
            return value if value else None
        except Exception as e:
            logger.warning("Product page image extraction failed (non-critical): %s", e)
            return None

    async def add_to_cart_by_url(self, product_url: str) -> bool:
        """
        Navigate directly to a product page and add it to the cart.
        Returns True if successful.
        """
        if product_url.startswith("/"):
            product_url = INSTACART_BASE + product_url

        task = (
            f"Go to {product_url} . "
            f"This is an Instacart product page. "
            f"First, check if this product is already in your cart — look for a quantity "
            f"counter or '1 in cart' indicator instead of an 'Add to cart' button. "
            f"If the product is already in the cart, do NOT add it again — just say 'SUCCESS'. "
            f"If it is not in the cart yet, click the 'Add to cart' button and wait for "
            f"confirmation that it was added. "
            f"If the product is out of stock or can't be added, say 'FAILED'. "
            f"If successfully added (or already in cart), say 'SUCCESS'. "
            f"{DISMISS_MODALS}"
        )

        try:
            history = await self._run_agent(task, max_steps=15)
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
        store_name = store or settings.instacart_store

        task = (
            f"Go to {INSTACART_BASE}/store/{store_name.lower()}/search "
            f"and search for: {product_name}. "
            f"Find the product that best matches this name. "
            f"Check if it already shows a quantity counter or '1 in cart' indicator — "
            f"if so, the product is already in the cart. Do NOT add it again; just say 'SUCCESS'. "
            f"If it is not in the cart yet, click the 'Add' or 'Add to cart' button for that product "
            f"and wait for confirmation that it was added. "
            f"If the product is out of stock or can't be added, say 'FAILED'. "
            f"If successfully added (or already in cart), say 'SUCCESS'. "
            f"{DISMISS_MODALS}"
        )

        try:
            history = await self._run_agent(task, max_steps=25)
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

    @staticmethod
    def _dict_to_product(p: dict) -> ProductResult:
        """Convert a dict (from structured agent output) to a ProductResult."""
        return ProductResult(
            product_name=p.get("product_name", p.get("name", "Unknown")),
            product_url=p.get("product_url", p.get("url")),
            brand=p.get("brand"),
            price=p.get("price"),
            image_url=p.get("image_url", p.get("image")),
            in_stock=p.get("in_stock", True),
        )

    def _parse_search_results(self, raw_result) -> list[ProductResult]:
        """Parse the raw agent output into ProductResult objects."""
        if raw_result is None:
            return []

        if isinstance(raw_result, dict):
            products = raw_result.get("products", [raw_result])
            return [self._dict_to_product(p) for p in products]
        elif isinstance(raw_result, list):
            return [self._dict_to_product(p) for p in raw_result if isinstance(p, dict)]
        else:
            # Try to parse structured text (markdown) from agent output
            return self._parse_text_results(str(raw_result))

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


class BrowserPool:
    """
    Persistent pool of InstacartAgent browser instances.

    - Runs Amazon + Instacart auth in parallel on 2 visible browsers
    - Spins up remaining headless workers concurrently
    - Injects Instacart cookies into all workers after auth
    - Persists across search and commit phases
    """

    def __init__(self, size: int):
        self._size = max(size, 2)
        self._agents: list[InstacartAgent] = []
        self._queue: asyncio.Queue[InstacartAgent] = asyncio.Queue()
        self._closed = False
        # Index of the Amazon auth agent (for cookie refresh)
        self._amazon_agent_idx = 0

    async def start_with_auth(self, on_status=None) -> tuple[bool, bool, dict | None]:
        """
        Start the pool with parallel auth:
        1. Create 2 headless browsers (pool-0=Amazon, pool-1=Instacart)
        2. Start N-2 headless workers in the background
        3. Run Amazon + Instacart auth in parallel
        4. Inject Instacart cookies from pool-1 into all other agents
        5. Populate the work queue

        Returns (amazon_ok, instacart_ok, cookie_data).
        on_status: optional callback(str) for progress messages.
        """
        def _status(msg):
            if on_status:
                on_status(msg)

        # Debug: wipe browser profile cookies to force re-login
        if settings.debug_clear_amazon_cookies:
            self._clear_profile_cookies(settings.resolved_data_dir / "browser-profile-pool-0")
        if settings.debug_clear_instacart_cookies:
            self._clear_profile_cookies(settings.resolved_data_dir / "browser-profile")

        _status("Checking logins...")

        # Create the two auth agents (headless — a visible window only opens if login is needed)
        # pool-0 (Amazon auth) gets its own profile for Amazon cookies
        # pool-1 (Instacart auth) uses the default profile to preserve existing cookies
        amazon_agent = InstacartAgent(profile_suffix="pool-0", headless=True, on_status=on_status)
        instacart_agent = InstacartAgent(headless=True, on_status=on_status)
        self._agents = [amazon_agent, instacart_agent]

        # Start headless workers in background
        worker_count = self._size - 2
        worker_task = None
        if worker_count > 0:
            worker_task = asyncio.create_task(self._start_workers(worker_count))

        # Run auth in parallel
        amazon_result, instacart_result = await asyncio.gather(
            amazon_agent.ensure_amazon_logged_in_and_get_cookies(),
            instacart_agent.ensure_logged_in(),
            return_exceptions=True,
        )

        # Process Amazon result
        amazon_ok = False
        cookie_data = None
        if isinstance(amazon_result, Exception):
            logger.error("Amazon auth failed: %s", amazon_result)
        elif amazon_result is not None:
            amazon_ok = True
            cookie_data = amazon_result

        # Process Instacart result
        instacart_ok = False
        if isinstance(instacart_result, Exception):
            logger.error("Instacart auth failed: %s", instacart_result)
        elif instacart_result:
            instacart_ok = True

        if not instacart_ok:
            return amazon_ok, instacart_ok, cookie_data

        # Wait for workers to finish starting
        if worker_task:
            _status(f"Starting {worker_count} browser workers...")
            try:
                await worker_task
            except Exception as e:
                logger.warning("Worker startup had errors: %s", e)

        # Extract Instacart cookies from pool-1 and inject into all others
        _status("Preparing browsers...")
        try:
            ic_cookies = await instacart_agent.extract_all_cookies()
            inject_tasks = []
            for i, agent in enumerate(self._agents):
                if i == 1:  # Skip pool-1 (already has cookies)
                    continue
                inject_tasks.append(agent.inject_cookies(ic_cookies))
            if inject_tasks:
                results = await asyncio.gather(*inject_tasks, return_exceptions=True)
                for i, r in enumerate(results):
                    if isinstance(r, Exception):
                        logger.warning("Cookie injection failed for agent: %s", r)
        except Exception as e:
            logger.warning("Failed to extract/inject Instacart cookies: %s", e)

        # Populate work queue with all agents
        for agent in self._agents:
            self._queue.put_nowait(agent)

        logger.info(
            "BrowserPool started: %d agents (%d auth + %d workers)",
            len(self._agents), 2, len(self._agents) - 2,
        )
        return amazon_ok, instacart_ok, cookie_data

    @staticmethod
    def _clear_profile_cookies(profile_dir):
        """Delete cookie files from a Chrome profile directory."""
        import shutil

        if not profile_dir.exists():
            return
        # Chrome stores cookies in Default/Cookies and Default/Network/Cookies
        for rel in ("Default/Cookies", "Default/Cookies-journal",
                     "Default/Network/Cookies", "Default/Network/Cookies-journal"):
            p = profile_dir / rel
            if p.exists():
                p.unlink()
                logger.info("Cleared cookies: %s", p)
        # Also clear session storage cookies
        session_dir = profile_dir / "Default" / "Session Storage"
        if session_dir.exists():
            shutil.rmtree(session_dir, ignore_errors=True)
            logger.info("Cleared session storage: %s", session_dir)

    async def _start_workers(self, count: int):
        """Start headless worker agents and add them to self._agents."""

        async def _make_worker(i: int) -> InstacartAgent:
            agent = InstacartAgent(profile_suffix=f"pool-{i + 2}", headless=True)
            await agent._ensure_started()
            return agent

        results = await asyncio.gather(
            *[_make_worker(i) for i in range(count)],
            return_exceptions=True,
        )
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.warning("Failed to start worker pool-%d: %s", i + 2, result)
            else:
                self._agents.append(result)

    async def acquire(self) -> InstacartAgent:
        """Get an agent from the pool (blocks until one is available)."""
        return await self._queue.get()

    def release(self, agent: InstacartAgent):
        """Return an agent to the pool."""
        if not self._closed:
            self._queue.put_nowait(agent)

    async def refresh_amazon_cookies(self) -> dict:
        """Get fresh Amazon cookies using the Amazon auth agent."""
        agent = self._agents[self._amazon_agent_idx]
        return await agent.get_amazon_cookies()

    async def close(self):
        """Close all browser sessions in the pool."""
        if self._closed:
            return
        self._closed = True
        # Drain the queue
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        # Close all agents
        close_tasks = []
        for agent in self._agents:
            close_tasks.append(agent.close())
        if close_tasks:
            results = await asyncio.gather(*close_tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, Exception):
                    logger.warning("Error closing pool agent: %s", r)
        self._agents.clear()
        logger.info("BrowserPool closed")
