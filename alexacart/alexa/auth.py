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

# Virtual Alexa device constants (mimics the Alexa iOS app).
# If Amazon starts rejecting device registration, check the latest Alexa iOS
# app version in the App Store and update APP_VERSION / OS_VERSION here.
DEVICE_TYPE = "A2IVLV5VM2W81"
APP_NAME = "Amazon Alexa"
APP_VERSION = "2.2.658990.0"
OS_VERSION = "18.3.1"
SOFTWARE_VERSION = "1"


def _is_on_maplanding(url: str) -> bool:
    """Check if URL path is the OAuth maplanding redirect (not just a query param match)."""
    from urllib.parse import urlparse
    return "/ap/maplanding" in urlparse(url).path


def _cookies_path() -> Path:
    return settings.cookies_path


def load_cookies() -> dict | None:
    """Load saved cookies from disk. Returns dict with 'cookies' key or None."""
    path = _cookies_path()
    if not path.exists():
        logger.info("load_cookies: no cookies file at %s", path)
        return None
    try:
        data = json.loads(path.read_text())
        if "cookies" in data and data["cookies"]:
            reg = data.get("registration", {})
            has_refresh = bool(reg.get("refresh_token"))
            logger.info(
                "load_cookies: loaded %d cookies, source=%s, has_refresh_token=%s, registered_at=%s",
                len(data["cookies"]),
                data.get("source", "unknown"),
                has_refresh,
                reg.get("registered_at", "n/a"),
            )
            return data
        logger.info("load_cookies: file exists but no cookies in it")
    except (json.JSONDecodeError, KeyError) as e:
        logger.warning("load_cookies: failed to parse %s: %s", path, e)
    return None


def save_cookies(data: dict) -> None:
    """Save cookies to disk."""
    path = _cookies_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    reg = data.get("registration", {})
    has_refresh = bool(reg.get("refresh_token"))
    logger.info(
        "save_cookies: saving %d cookies, source=%s, has_refresh_token=%s, registered_at=%s → %s",
        len(data.get("cookies", {})),
        data.get("source", "unknown"),
        has_refresh,
        reg.get("registered_at", "n/a"),
        path,
    )
    if not has_refresh:
        # Check if we're about to overwrite a file that HAD a refresh token
        existing = None
        if path.exists():
            try:
                existing = json.loads(path.read_text())
            except Exception:
                pass
        if existing and existing.get("registration", {}).get("refresh_token"):
            logger.warning(
                "save_cookies: ⚠️  OVERWRITING existing refresh token! "
                "Old source=%s, new source=%s. This will lose the refresh token.",
                existing.get("source", "unknown"),
                data.get("source", "unknown"),
            )
    path.write_text(json.dumps(data, indent=2))


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


def _build_oauth_url(
    code_challenge: str,
    device_serial: str,
    force_fresh_auth: bool = True,
    immediate: bool = False,
) -> str:
    """Build the Amazon OAuth URL that the Alexa app uses to initiate login.

    Args:
        force_fresh_auth: If True, include PAPE max_auth_age=0 which forces
            Amazon to require fresh authentication (ignore existing sessions).
        immediate: If True, use checkid_immediate mode which forces Amazon to
            respond instantly (no login form). Used for retry after login — the
            user is already authenticated so Amazon should auto-complete with
            the full OAuth2 assertion including the authorization code.
    """
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
        "openid.mode": "checkid_immediate" if immediate else "checkid_setup",
        "openid.ns.oa2": "http://www.amazon.com/ap/ext/oauth/2",
        "openid.oa2.client_id": f"device:{client_id}",
        "openid.oa2.scope": "device_auth_access",
        "openid.ns": "http://specs.openid.net/auth/2.0",
        # serial is required — Amazon echoes it back in the maplanding redirect and
        # uses it to include openid.oa2.authorization_code in the response.
        # Without it, Amazon completes OpenID auth but skips the OAuth2 extension.
        "serial": device_serial,
    }
    if force_fresh_auth:
        params["openid.ns.pape"] = "http://specs.openid.net/extensions/pape/1.0"
        params["openid.pape.max_auth_age"] = "0"
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
            "use_global_authentication": "true",
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
            # Log the top-level structure to diagnose registration issues
            response_keys = list(data.get("response", {}).keys())
            logger.info("Device registration response keys: %s", response_keys)
            if "error" in data.get("response", {}):
                logger.warning(
                    "Device registration error in response: %s",
                    json.dumps(data["response"]["error"], indent=2)[:1000],
                )
            response_data = data.get("response", {}).get("success", {})
            tokens = response_data.get("tokens", {})
            token_types = list(tokens.keys())
            bearer = tokens.get("bearer", {})
            bearer_keys = list(bearer.keys())
            logger.info(
                "Device registration tokens: types=%s, bearer_keys=%s",
                token_types, bearer_keys,
            )

            refresh_token = bearer.get("refresh_token")
            access_token = bearer.get("access_token")

            if not refresh_token:
                logger.warning(
                    "Device registration response missing refresh_token. "
                    "bearer keys: %s, full bearer (truncated): %s",
                    bearer_keys,
                    str({k: v[:20] + "..." if isinstance(v, str) and len(v) > 20 else v for k, v in bearer.items()})[:500],
                )
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
        logger.info(
            "No refresh token in cookies file — token refresh unavailable. "
            "source=%s, registration_keys=%s",
            existing.get("source", "unknown"),
            list(registration.keys()),
        )
        return None

    logger.info(
        "Attempting token refresh (registered_at=%s, refresh_token=%s...)",
        registration.get("registered_at", "n/a"),
        refresh_token[:20] if refresh_token else "None",
    )
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
                    "Token-to-cookie exchange failed: HTTP %d — %s",
                    resp.status_code, resp.text[:500],
                )
                return None

            data = resp.json()
            response = data.get("response", {})
            response_keys = list(response.keys())
            tokens = response.get("tokens", {})
            tokens_keys = list(tokens.keys())
            cookie_domains = list(tokens.get("cookies", {}).keys())
            cookie_list = tokens.get("cookies", {}).get(".amazon.com", [])
            logger.info(
                "Token exchange response: response_keys=%s, tokens_keys=%s, "
                "cookie_domains=%s, .amazon.com_cookies=%d",
                response_keys, tokens_keys, cookie_domains, len(cookie_list),
            )

            if not cookie_list:
                logger.warning(
                    "Token exchange response has no cookies. Full response (truncated): %s",
                    json.dumps(data, indent=2)[:1000],
                )
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
    """Extract the authorization_code from an OAuth redirect URL.

    Amazon primarily uses openid.oa2.authorization_code, but some account
    configurations return a plain 'code' parameter instead.
    """
    from urllib.parse import parse_qs, urlparse

    parsed = urlparse(url)
    if "maplanding" in parsed.path:
        params = parse_qs(parsed.query)
        code = params.get("openid.oa2.authorization_code", [None])[0]
        if code:
            return code
        # Fallback: some account types return 'code' instead
        code = params.get("code", [None])[0]
        if code:
            logger.info("Auth code found via fallback 'code' parameter (not openid.oa2.authorization_code)")
            return code
    return None


async def _try_extract_auth_code(page) -> str | None:
    """Check the current page URL for an OAuth authorization code (non-blocking)."""
    url = page.url or ""
    return _extract_auth_code_from_url(url)


async def _setup_auth_code_interceptor(page) -> list[str]:
    """
    Set up a CDP network interceptor to capture OAuth auth codes from redirects.

    The maplanding redirect URL may only contain the auth code briefly before
    client-side processing strips it. This interceptor captures every request URL
    containing the auth code in real-time, regardless of how quickly the page changes.

    Listens to both RequestWillBeSent and ResponseReceived (for 3xx Location
    headers) to catch the auth code regardless of how Amazon delivers it.

    Returns a list that gets populated with captured auth codes.
    """
    try:
        import nodriver.cdp.network as network

        captured_codes: list[str] = []

        def _check_url_for_code(url: str, source: str) -> None:
            """Check a URL for an auth code and log maplanding hits."""
            if _is_on_maplanding(url) or "authorization_code" in url:
                code = _extract_auth_code_from_url(url)
                if code and code not in captured_codes:
                    logger.info(
                        "Intercepted OAuth auth code via %s: ...authorization_code=<captured>",
                        source,
                    )
                    captured_codes.append(code)
                elif not code and _is_on_maplanding(url):
                    logger.info(
                        "Maplanding URL seen via %s but NO auth code present. URL: %s",
                        source,
                        url,
                    )

        async def on_request(event: network.RequestWillBeSent):
            _check_url_for_code(event.request.url, "RequestWillBeSent")

        async def on_response(event: network.ResponseReceived):
            status = event.response.status
            if 300 <= status < 400:
                # Check the Location header on redirects
                location = event.response.headers.get("location", "") if event.response.headers else ""
                if location:
                    _check_url_for_code(location, f"ResponseReceived(Location, {status})")
                # Also check the response URL itself
                _check_url_for_code(event.response.url, f"ResponseReceived({status})")

        page.add_handler(network.RequestWillBeSent, on_request)
        page.add_handler(network.ResponseReceived, on_response)
        # Enable the Network domain so CDP fires the events
        await page.send(network.enable())
        logger.debug("OAuth redirect interceptor active (RequestWillBeSent + ResponseReceived)")
        return captured_codes
    except Exception as e:
        logger.warning("Failed to set up network interceptor: %s", e)
        return []


async def _wait_for_oauth_redirect(
    page, timeout_polls: int = 100, captured_codes: list[str] | None = None
) -> tuple[str | None, bool]:
    """
    Poll the browser page URL waiting for the OAuth redirect to maplanding.
    Also checks captured_codes from the network interceptor.

    Returns (authorization_code, user_logged_in):
      - (code, True)  — got auth code, user is logged in
      - (None, True)  — no auth code, but user is logged in (maplanding seen or on amazon.com)
      - (None, False) — timed out, user may not be logged in yet
    """
    maplanding_polls = 0
    for _ in range(timeout_polls):  # Up to ~5 minutes
        await page.sleep(3)

        # Check network interceptor first (most reliable)
        if captured_codes:
            logger.info("Captured OAuth authorization code via network interceptor")
            return captured_codes[0], True

        code = await _try_extract_auth_code(page)
        if code:
            logger.info("Captured OAuth authorization code from page URL")
            return code, True
        # Also check if user navigated away from the login flow
        url = page.url or ""
        if _is_on_maplanding(url):
            # We're on the maplanding page but no auth code in the URL.
            # Give it a couple polls in case the page is still loading/redirecting,
            # then bail — the user sees a blank/404 page otherwise.
            maplanding_polls += 1
            if maplanding_polls >= 2:
                # Check interceptor one more time
                if captured_codes:
                    logger.info("Captured OAuth authorization code via network interceptor (late)")
                    return captured_codes[0], True
                logger.info("On maplanding page but no auth code captured — moving on")
                logger.info("Maplanding URL: %s", url)
                return None, True  # User IS logged in, just no auth code
        elif "amazon.com" in url and "/ap/" not in url:
            # User is on amazon.com but not in the auth flow — login succeeded
            # but we missed the redirect (e.g. 2FA or CAPTCHA changed the flow)
            if captured_codes:
                logger.info("Captured OAuth authorization code via network interceptor (post-redirect)")
                return captured_codes[0], True
            logger.info("User appears logged in but OAuth redirect not captured")
            return None, True  # User IS logged in, just no auth code
    return None, False  # Timed out — user may not be logged in


async def _retry_oauth_for_device_registration(browser, _status) -> dict | None:
    """
    Retry the OAuth flow via page navigation to capture an auth code.

    Called when the initial browser login succeeded but the auth code wasn't
    captured (e.g. maplanding was reached but lacked openid.oa2.authorization_code).

    Since the user is already authenticated, navigating to the OAuth URL again
    should auto-redirect to maplanding — this time with the auth code, because
    the serial param is included in the URL.

    NOTE: fetch() does NOT work here — Amazon checks Sec-Fetch-Mode and returns
    the login page HTML for non-navigation requests.

    Returns cookie data dict with registration info, or None if retry didn't work.
    """
    logger.info("Retrying OAuth for device registration via page navigation...")
    _status("Retrying OAuth for device registration...")

    page = browser.main_tab

    code_verifier, code_challenge, device_serial = _generate_pkce()
    oauth_url = _build_oauth_url(code_challenge, device_serial, force_fresh_auth=False)

    # Set up interceptor BEFORE navigation so we catch the immediate redirect
    captured_codes = await _setup_auth_code_interceptor(page)
    await page.get(oauth_url)

    # Poll up to 10 seconds — user is already logged in so redirect should be fast
    for _ in range(5):
        await page.sleep(2)
        if captured_codes:
            break
        url = page.url or ""
        if _is_on_maplanding(url) or ("amazon.com" in url and "/ap/" not in url):
            break

    auth_code = captured_codes[0] if captured_codes else await _try_extract_auth_code(page)
    logger.info(
        "OAuth retry navigation: auth_code=%s, captured=%d, url=%s",
        "yes" if auth_code else "no",
        len(captured_codes),
        (page.url or "")[:200],
    )

    if auth_code:
        logger.info("OAuth retry: captured auth code via page navigation")
        reg_result = await _register_device(auth_code, code_verifier, device_serial)
        if reg_result:
            _status("Device registered — refresh token saved")
            return reg_result
        logger.warning("OAuth retry: device registration failed despite auth code")
    else:
        logger.info("OAuth retry: no auth code captured")

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
    """Extract Amazon cookies from a nodriver browser and save to disk.

    NOTE: This is a fallback path — saves cookies WITHOUT a refresh token.
    The save_cookies() call will log a warning if this overwrites an existing
    refresh token.
    """
    import traceback

    _status("Extracting Amazon cookies...")
    logger.info(
        "_extract_and_save_cookies called (fallback path — no refresh token). "
        "Caller: %s",
        " → ".join(
            f"{f.name}:{f.lineno}" for f in traceback.extract_stack()[-4:-1]
        ),
    )
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


def _kill_chrome_for_profile(profile_dir: Path) -> bool:
    """Kill any lingering Chrome processes using the specified profile directory."""
    import subprocess

    try:
        result = subprocess.run(
            ["pgrep", "-f", f"--user-data-dir={profile_dir}"],
            capture_output=True,
            text=True,
        )
        pids = [p.strip() for p in result.stdout.strip().split("\n") if p.strip()]
        if not pids:
            return False
        logger.info("Killing %d lingering Chrome process(es): %s", len(pids), pids)
        subprocess.run(["kill", "-9"] + pids, capture_output=True)
        return True
    except Exception as e:
        logger.debug("Chrome cleanup: %s", e)
        return False


def _clean_profile_locks(profile_dir: Path) -> bool:
    """Remove Chrome profile lock files that prevent new instances from starting."""
    cleaned = False
    for lock_name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        lock_path = profile_dir / lock_name
        if lock_path.exists() or lock_path.is_symlink():
            try:
                lock_path.unlink()
                logger.info("Removed stale lock file: %s", lock_name)
                cleaned = True
            except OSError as e:
                logger.debug("Could not remove %s: %s", lock_name, e)
    return cleaned


async def _start_browser(
    profile_dir: Path, headless: bool = True, start_url: str | None = None
):
    """Start nodriver with retry and zombie Chrome cleanup.

    Args:
        start_url: URL to open when Chrome launches. Passed as a Chrome
            command-line argument so the page starts loading immediately
            (no blank window). Only used for visible browsers.
    """
    import nodriver as uc

    from alexacart.nodriver_patch import patch as _patch_nodriver
    _patch_nodriver()

    extra_args = [start_url] if start_url and not headless else []

    for attempt in range(3):
        killed = _kill_chrome_for_profile(profile_dir)
        locks_cleaned = _clean_profile_locks(profile_dir)
        if killed or attempt > 0:
            await asyncio.sleep(2)
        elif locks_cleaned:
            await asyncio.sleep(1)
        try:
            browser = await uc.start(
                user_data_dir=str(profile_dir),
                headless=headless,
                browser_args=extra_args,

            )
            return browser
        except Exception as e:
            logger.warning(
                "nodriver start (attempt %d/3, headless=%s): %s",
                attempt + 1,
                headless,
                e,
            )
            if attempt == 2:
                raise


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

    def _status(msg):
        logger.info(msg)
        if on_status:
            on_status(msg)

    profile_dir = settings.resolved_local_data_dir / "nodriver-amazon"
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

    # Generate PKCE + OAuth URL for device registration.
    # force_fresh_auth=False omits max_auth_age=0, which avoids forcing 2FA.
    # When 2FA is triggered, Amazon drops the OAuth2 extension (authorization_code)
    # from the redirect. Without max_auth_age=0, warm Chrome profiles can
    # auto-redirect with the auth code intact.
    code_verifier, code_challenge, device_serial = _generate_pkce()
    oauth_url = _build_oauth_url(code_challenge, device_serial, force_fresh_auth=False)

    if force_relogin:
        # Skip headless check — open visible browser and force a fresh login
        _status("Session expired — opening Amazon for re-login...")
        browser = await _start_browser(
            profile_dir, headless=False,
            start_url="https://www.amazon.com/gp/flex/sign-out.html",
        )
        try:
            # Sign out first to clear the stale session
            page = browser.main_tab
            await page.sleep(3)

            # Set up interceptor BEFORE navigating to OAuth URL so we catch instant redirects
            _status("Please log into Amazon in the browser window...")
            captured_codes = await _setup_auth_code_interceptor(page)
            await page.get(oauth_url)
            await page.sleep(2)

            auth_code, login_seen = await _wait_for_oauth_redirect(page, captured_codes=captured_codes)
            if auth_code:
                result = await _register_device(auth_code, code_verifier, device_serial)
                if result:
                    _status("Device registered — refresh token saved")
                    return result
                logger.warning("Device registration failed after re-login, falling back to cookie extraction")
            elif not login_seen:
                # Timed out waiting for login — user may not have logged in yet
                raise RuntimeError("Timed out waiting for Amazon re-login")
            else:
                # Maplanding seen (user logged in) but no auth code — retry OAuth
                retry_result = await _retry_oauth_for_device_registration(browser, _status)
                if retry_result:
                    return retry_result

            # User is logged in — extract cookies directly (no need to poll)
            page = await browser.get("https://www.amazon.com")
            await page.sleep(2)
            return await _extract_and_save_cookies(browser, _status)
        finally:
            try:
                browser.stop()
            except Exception:
                pass
            _kill_chrome_for_profile(profile_dir)

    # Normal mode: start headless — only open a visible window if login is actually needed
    _status("Checking Amazon login...")
    browser = await _start_browser(profile_dir, headless=True)

    try:
        # Set up interceptor BEFORE navigating to OAuth URL so we catch instant redirects
        page = await browser.get("about:blank")
        captured_codes = await _setup_auth_code_interceptor(page)
        await page.get(oauth_url)

        # Poll up to 12 seconds for the OAuth redirect.
        # Chrome can take 8-12s to complete the maplanding redirect even for
        # an already-logged-in session. A flat 2s sleep misses the redirect and
        # incorrectly triggers a visible browser.
        for _ in range(6):
            await page.sleep(2)
            if captured_codes:
                break
            url = page.url or ""
            if _is_on_maplanding(url) or ("amazon.com" in url and "/ap/" not in url):
                break

        # Check if the OAuth redirected (user already logged in)
        auth_code = captured_codes[0] if captured_codes else await _try_extract_auth_code(page)
        page_url_now = page.url or ""
        logger.info(
            "Headless OAuth check: auth_code=%s, captured_codes=%d, page_url=%s",
            "yes" if auth_code else "no",
            len(captured_codes),
            page_url_now[:200],
        )
        if auth_code:
            _status("Already logged in — registering device...")
            result = await _register_device(auth_code, code_verifier, device_serial)
            if result:
                _status("Device registered — refresh token saved")
                return result
            logger.warning("Device registration failed (had auth code), falling back to cookie extraction")
            # Fall through to extract cookies directly
            page = await browser.get("https://www.amazon.com")
            await page.sleep(2)
            return await _extract_and_save_cookies(browser, _status)

        # Check if we ended up on maplanding (user was already logged in,
        # OAuth redirected but auth code wasn't in the URL)
        page_url = page.url or ""
        if _is_on_maplanding(page_url):
            # Check interceptor — may have captured code from a redirect we missed
            if captured_codes:
                auth_code = captured_codes[0]
                logger.info("Captured OAuth auth code via interceptor on maplanding")
                _status("Already logged in — registering device...")
                result = await _register_device(auth_code, code_verifier, device_serial)
                if result:
                    _status("Device registered — refresh token saved")
                    return result
                logger.warning("Device registration failed, falling back to cookie extraction")
            else:
                logger.info("On maplanding but no auth code (URL: %s)", page_url)
                # Retry OAuth — user is authenticated, second attempt should auto-redirect with auth code
                retry_result = await _retry_oauth_for_device_registration(browser, _status)
                if retry_result:
                    return retry_result
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
        # Generate fresh PKCE to avoid server-side state issues from the headless attempt
        code_verifier, code_challenge, device_serial = _generate_pkce()
        oauth_url = _build_oauth_url(code_challenge, device_serial, force_fresh_auth=False)
        browser = await _start_browser(profile_dir, headless=False, start_url=oauth_url)
        page = browser.main_tab
        captured_codes = await _setup_auth_code_interceptor(page)
        await page.sleep(2)

        _status("Waiting for Amazon login — please log in via the browser window...")
        auth_code, login_seen = await _wait_for_oauth_redirect(page, captured_codes=captured_codes)
        if auth_code:
            result = await _register_device(auth_code, code_verifier, device_serial)
            if result:
                _status("Device registered — refresh token saved")
                return result
            logger.warning("Device registration failed after login, falling back to cookie extraction")
        elif not login_seen:
            # Timed out — user never completed login
            raise RuntimeError("Timed out waiting for Amazon login")
        else:
            # Maplanding seen (user logged in) but no auth code — retry OAuth
            retry_result = await _retry_oauth_for_device_registration(browser, _status)
            if retry_result:
                return retry_result

        # User is logged in — extract cookies directly (no need to poll)
        page = await browser.get("https://www.amazon.com")
        await page.sleep(2)

        return await _extract_and_save_cookies(browser, _status)

    finally:
        try:
            browser.stop()
        except Exception:
            pass
        _kill_chrome_for_profile(profile_dir)


async def validate_alexa_cookies(cookie_data: dict) -> bool:
    """
    Quick check: hit the Alexa API with the given cookies and return True if 200.

    Used to detect stale cookies upfront instead of discovering it mid-flow
    after wasting time on 401 retries and nodriver launches.
    """
    headers = {
        **get_cookie_header(cookie_data),
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 13_5_1 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 "
            "PitanguiBridge/2.2.345247.0-[HARDWARE=iPhone10_4][SOFTWARE=13.5.1]"
        ),
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://www.amazon.com/alexashoppinglists/api/getlistitems",
                headers=headers,
            )
            if resp.status_code == 200:
                logger.info("Cookie validation: OK (200)")
                return True
            logger.info("Cookie validation: failed (%d)", resp.status_code)
            return False
    except Exception as e:
        logger.warning("Cookie validation error: %s", e)
        return False


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
