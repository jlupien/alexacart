"""
Alexa cookie-based authentication.

Amazon's bot detection flags browser-use/Playwright sessions and issues
limited-scope session tokens that can't access the Shopping List API.
We use nodriver (undetectable Chrome) for Amazon login + cookie extraction.

To avoid cookies expiring after ~1 hour, we register a virtual Alexa device
with Amazon (mimicking the Alexa mobile app) to obtain a long-lived OAuth
refresh token. This token can be exchanged for fresh cookies via a simple
HTTP POST — no browser needed.

On-demand refresh: called automatically when cookies expire (401 from Alexa API).
"""

import asyncio
import base64
import hashlib
import json
import logging
import secrets
from datetime import UTC, datetime
from pathlib import Path

import httpx

from alexacart.config import settings

logger = logging.getLogger(__name__)

# Virtual Alexa device constants (mimics the Alexa iOS app)
DEVICE_TYPE = "A2IVLV5VM2W81"
APP_NAME = "Amazon Alexa"
APP_VERSION = "2.2.651540.0"
OS_VERSION = "18.3.1"
SOFTWARE_VERSION = "1"


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


def _generate_pkce() -> tuple[str, str, str]:
    """Generate PKCE code_verifier, code_challenge, and device_serial for OAuth."""
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    code_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    device_serial = secrets.token_hex(16)
    return code_verifier, code_challenge, device_serial


def _build_oauth_url(code_challenge: str, device_serial: str) -> str:
    """Build the Amazon OAuth URL that the Alexa app uses to initiate login."""
    from urllib.parse import urlencode

    client_id = f"{device_serial}#{DEVICE_TYPE}"
    params = {
        "openid.oa2.response_type": "code",
        "openid.oa2.code_challenge_method": "S256",
        "openid.oa2.code_challenge": code_challenge,
        "openid.return_to": "https://www.amazon.com/ap/maplanding",
        "openid.assoc_handle": "amzn_dp_project_dee_ios",
        "openid.identity": "http://specs.openid.net/auth/2.0/identifier_select",
        "openid.claimed_id": "http://specs.openid.net/auth/2.0/identifier_select",
        "openid.mode": "checkid_setup",
        "openid.ns.oa2": "http://www.amazon.com/ap/ext/oauth/2",
        "openid.oa2.client_id": f"device:{client_id}",
        "openid.ns.pape": "http://specs.openid.net/extensions/pape/1.0",
        "openid.oa2.scope": "device_auth_access",
        "openid.ns": "http://specs.openid.net/auth/2.0",
        "openid.pape.max_auth_age": "0",
    }
    return f"https://www.amazon.com/ap/signin?{urlencode(params)}"


async def _register_device(
    authorization_code: str, code_verifier: str, device_serial: str
) -> dict | None:
    """
    Register a virtual Alexa device with Amazon to obtain a long-lived refresh token.

    This mimics what the Alexa mobile app does after the user logs in via OAuth.
    The refresh token can later be exchanged for fresh cookies without a browser.
    """
    client_id = f"{device_serial}#{DEVICE_TYPE}"
    body = {
        "requested_extensions": ["device_info", "customer_info"],
        "cookies": {"website_cookies": [], "domain": ".amazon.com"},
        "registration_data": {
            "domain": "Device",
            "app_version": APP_VERSION,
            "device_type": DEVICE_TYPE,
            "device_name": f"%FIRST_NAME%'s%DUPE_STRATEGY_1ST%Alexa App",
            "os_version": OS_VERSION,
            "device_serial": device_serial,
            "device_model": "iPhone",
            "app_name": APP_NAME,
            "software_version": SOFTWARE_VERSION,
        },
        "auth_data": {
            "client_id": client_id,
            "authorization_code": authorization_code,
            "code_verifier": code_verifier,
            "code_algorithm": "SHA-256",
            "client_domain": "DeviceLegacy",
        },
        "user_context_map": {"frc": ""},
        "requested_token_type": [
            "bearer",
            "mac_dms",
            "website_cookies",
        ],
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://api.amazon.com/auth/register",
                json=body,
                headers={
                    "Content-Type": "application/json",
                    "Accept-Language": "en-US",
                },
            )
            if resp.status_code != 200:
                logger.warning(
                    "Device registration failed: %d %s", resp.status_code, resp.text[:500]
                )
                return None

            data = resp.json()
            response_data = data.get("response", {}).get("success", {})
            tokens = response_data.get("tokens", {})
            bearer = tokens.get("bearer", {})

            refresh_token = bearer.get("refresh_token")
            access_token = bearer.get("access_token")

            if not refresh_token:
                logger.warning("Device registration response missing refresh_token")
                return None

            # Extract website cookies from registration response
            cookies = {}
            website_cookies = tokens.get("website_cookies", [])
            for cookie in website_cookies:
                name = cookie.get("Name", "")
                value = cookie.get("Value", "")
                if name and value:
                    cookies[name] = value

            result = {
                "cookies": cookies,
                "registration": {
                    "refresh_token": refresh_token,
                    "access_token": access_token,
                    "device_serial": device_serial,
                    "device_type": DEVICE_TYPE,
                    "registered_at": datetime.now(UTC).isoformat(),
                },
                "source": "nodriver",
            }

            save_cookies(result)
            logger.info("Device registered successfully, refresh token saved")
            return result

    except Exception as e:
        logger.warning("Device registration error: %s", e)
        return None


async def refresh_cookies_via_token() -> dict | None:
    """
    Exchange the stored OAuth refresh token for fresh Amazon cookies.

    This is a pure HTTP call — no browser needed. Takes ~1 second.
    Returns cookie data dict, or None if no refresh token or exchange fails.
    """
    existing = load_cookies()
    if not existing:
        logger.info("No existing cookies file for token refresh")
        return None

    registration = existing.get("registration", {})
    refresh_token = registration.get("refresh_token")
    if not refresh_token:
        logger.info("No refresh token in cookies file — token refresh unavailable")
        return None

    device_serial = registration.get("device_serial", "")

    try:
        form_data = {
            "app_name": APP_NAME,
            "app_version": APP_VERSION,
            "di.sdk.version": "6.12.4",
            "source_token": refresh_token,
            "package_name": "com.amazon.echo",
            "di.hw.version": "iPhone",
            "platform": "iOS",
            "requested_token_type": "auth_cookies",
            "source_token_type": "refresh_token",
            "di.os.name": "iOS",
            "di.os.version": OS_VERSION,
            "current_version": "6.12.4",
            "previous_token": "cookie",
            "domain": ".amazon.com",
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://www.amazon.com/ap/exchangetoken/cookies",
                data=form_data,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept-Language": "en-US",
                },
            )

            if resp.status_code != 200:
                logger.warning(
                    "Token-to-cookie exchange failed: %d %s",
                    resp.status_code, resp.text[:500],
                )
                return None

            data = resp.json()
            response = data.get("response", {})
            tokens = response.get("tokens", {})
            cookie_list = tokens.get("cookies", {}).get(".amazon.com", [])

            if not cookie_list:
                logger.warning("Token exchange response has no cookies")
                return None

            cookies = {}
            for cookie in cookie_list:
                name = cookie.get("Name", "")
                value = cookie.get("Value", "")
                if name and value:
                    cookies[name] = value

            logger.info(
                "Token refresh got %d cookies: %s", len(cookies), sorted(cookies.keys())
            )

            # Preserve registration data, update cookies
            result = {
                "cookies": cookies,
                "registration": registration,
                "source": "token_refresh",
            }
            save_cookies(result)
            return result

    except Exception as e:
        logger.warning("Token-to-cookie exchange error: %s", e)
        return None


def _extract_auth_code_from_url(url: str) -> str | None:
    """Extract the authorization_code from an OAuth redirect URL."""
    from urllib.parse import parse_qs, urlparse

    parsed = urlparse(url)
    if "maplanding" in parsed.path:
        params = parse_qs(parsed.query)
        code = params.get("openid.oa2.authorization_code", [None])[0]
        if code:
            return code
    return None


async def _try_extract_auth_code(page) -> str | None:
    """Check the current page URL for an OAuth authorization code (non-blocking)."""
    url = page.url or ""
    return _extract_auth_code_from_url(url)


async def _wait_for_oauth_redirect(page, timeout_polls: int = 100) -> str | None:
    """
    Poll the browser page URL waiting for the OAuth redirect to maplanding.
    Returns the authorization_code if captured, or None on timeout.
    """
    maplanding_polls = 0
    for _ in range(timeout_polls):  # Up to ~5 minutes
        await page.sleep(3)
        code = await _try_extract_auth_code(page)
        if code:
            logger.info("Captured OAuth authorization code")
            return code
        # Also check if user navigated away from the login flow
        url = page.url or ""
        if "maplanding" in url:
            # We're on the maplanding page but no auth code in the URL.
            # Give it a couple polls in case the page is still loading/redirecting,
            # then bail — the user sees a blank/404 page otherwise.
            maplanding_polls += 1
            if maplanding_polls >= 2:
                logger.info("On maplanding page but no auth code captured — moving on")
                return None
        elif "amazon.com" in url and "/ap/" not in url:
            # User is on amazon.com but not in the auth flow — login succeeded
            # but we missed the redirect (e.g. 2FA or CAPTCHA changed the flow)
            logger.info("User appears logged in but OAuth redirect not captured")
            return None
    return None


async def _check_amazon_login(page) -> bool:
    """Check if the current Amazon page shows a logged-in state."""
    try:
        el = await page.query_selector("#nav-link-accountList")
        if el:
            text = el.text or ""
            if "sign in" not in text.lower():
                return True
    except Exception:
        pass
    return False


async def _extract_and_save_cookies(browser, _status) -> dict:
    """Extract Amazon cookies from a nodriver browser and save to disk."""
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


async def extract_cookies_via_nodriver(on_status=None, force_relogin=False) -> dict:
    """
    Open an undetectable Chrome instance via nodriver, ensure the user is
    logged into Amazon, and extract session cookies.

    nodriver bypasses Amazon's bot detection that flags browser-use/Playwright
    sessions and limits their API access.

    Args:
        on_status: Optional callback(str) for progress messages.
        force_relogin: If True, skip headless check — open a visible browser,
            sign out of Amazon, and wait for fresh login. Used when the
            automatic headless refresh gets stale cookies that still 401.

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

    # Generate PKCE + OAuth URL for device registration
    code_verifier, code_challenge, device_serial = _generate_pkce()
    oauth_url = _build_oauth_url(code_challenge, device_serial)

    if force_relogin:
        # Skip headless check — open visible browser and force a fresh login
        _status("Session expired — opening Amazon for re-login...")
        browser = await uc.start(
            user_data_dir=str(profile_dir),
            headless=False,
        )
        try:
            # Sign out first to clear the stale session
            page = await browser.get("https://www.amazon.com/gp/flex/sign-out.html")
            await page.sleep(3)

            # Navigate to OAuth URL for device registration
            _status("Please log into Amazon in the browser window...")
            page = await browser.get(oauth_url)
            await page.sleep(2)

            auth_code = await _wait_for_oauth_redirect(page)
            if auth_code:
                result = await _register_device(auth_code, code_verifier, device_serial)
                if result:
                    _status("Device registered — refresh token saved")
                    return result
                logger.warning("Device registration failed after re-login, falling back to cookie extraction")

            # Fallback: wait for login on amazon.com and extract cookies directly
            page = await browser.get("https://www.amazon.com")
            await page.sleep(2)
            logged_in = False
            for _ in range(100):  # Up to ~5 minutes
                await page.sleep(3)
                if await _check_amazon_login(page):
                    logged_in = True
                    break

            if not logged_in:
                raise RuntimeError("Timed out waiting for Amazon re-login")

            return await _extract_and_save_cookies(browser, _status)
        finally:
            try:
                browser.stop()
            except Exception:
                pass

    # Normal mode: start headless — only open a visible window if login is actually needed
    _status("Checking Amazon login...")
    browser = await uc.start(
        user_data_dir=str(profile_dir),
        headless=True,
    )

    try:
        # Try OAuth flow first — navigate to OAuth URL
        page = await browser.get(oauth_url)
        await page.sleep(2)

        # Check if the OAuth redirected immediately (user already logged in)
        auth_code = await _try_extract_auth_code(page)
        if auth_code:
            _status("Already logged in — registering device...")
            result = await _register_device(auth_code, code_verifier, device_serial)
            if result:
                _status("Device registered — refresh token saved")
                return result
            logger.warning("Device registration failed, falling back to cookie extraction")
            # Fall through to extract cookies directly
            page = await browser.get("https://www.amazon.com")
            await page.sleep(2)
            return await _extract_and_save_cookies(browser, _status)

        # Check if we ended up on maplanding (user was already logged in,
        # OAuth redirected but auth code wasn't in the URL)
        page_url = page.url or ""
        if "maplanding" in page_url:
            _status("Already logged into Amazon")
            page = await browser.get("https://www.amazon.com")
            await page.sleep(2)
            return await _extract_and_save_cookies(browser, _status)

        # Check if we ended up on a login page or on amazon.com
        logged_in = False
        if "amazon.com" in page_url and "/ap/" not in page_url:
            # We're on amazon.com, not a login page — check if logged in
            logged_in = await _check_amazon_login(page)

        if logged_in:
            _status("Already logged into Amazon")
            return await _extract_and_save_cookies(browser, _status)

        # Need manual login — restart with a visible browser window.
        # Add delay + retry: the old Chrome process may still hold the profile
        # lock, and concurrent browser-use launches can cause resource contention.
        browser.stop()
        await asyncio.sleep(2)
        _status("Login needed — opening Amazon login window...")
        for attempt in range(3):
            try:
                browser = await uc.start(
                    user_data_dir=str(profile_dir),
                    headless=False,
                )
                break
            except Exception as e:
                if attempt < 2:
                    logger.warning("nodriver start attempt %d failed: %s, retrying...", attempt + 1, e)
                    await asyncio.sleep(3)
                else:
                    raise
        page = await browser.get(oauth_url)
        await page.sleep(2)

        _status("Waiting for Amazon login — please log in via the browser window...")
        auth_code = await _wait_for_oauth_redirect(page)
        if auth_code:
            result = await _register_device(auth_code, code_verifier, device_serial)
            if result:
                _status("Device registered — refresh token saved")
                return result
            logger.warning("Device registration failed after login, falling back to cookie extraction")

        # Fallback: check if we're logged in and just extract cookies
        page = await browser.get("https://www.amazon.com")
        await page.sleep(2)
        logged_in = await _check_amazon_login(page)
        if not logged_in:
            for _ in range(100):
                await page.sleep(3)
                if await _check_amazon_login(page):
                    logged_in = True
                    break

        if not logged_in:
            raise RuntimeError("Timed out waiting for Amazon login")

        return await _extract_and_save_cookies(browser, _status)

    finally:
        try:
            browser.stop()
        except Exception:
            pass


async def ensure_valid_cookies() -> dict:
    """
    Ensure we have valid cookies. Try loading, then token refresh, then prompt for login.
    Returns cookie data dict.
    Raises RuntimeError if no valid cookies can be obtained.
    """
    data = load_cookies()
    if data:
        return data

    # Try token refresh
    data = await refresh_cookies_via_token()
    if data:
        return data

    raise RuntimeError(
        "No valid Alexa cookies found. Start an order to log in via the browser."
    )
