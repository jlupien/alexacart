"""
Alexa cookie-based authentication.

Initial setup: `python -m alexacart.alexa.auth login`
Opens a browser for manual Amazon login, then extracts and saves cookies.

On-demand refresh: called automatically when cookies expire (401 from Alexa API).
Uses the Node.js alexa-cookie2 sidecar if available, otherwise prompts for manual login.
"""

import asyncio
import json
import logging
import subprocess
import sys
from pathlib import Path

from alexacart.config import settings

logger = logging.getLogger(__name__)

AMAZON_BASE = "https://www.amazon.com"
ALEXA_API_BASE = "https://api.amazonalexa.com"


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


async def login_interactive() -> dict:
    """
    Open a browser for the user to log into Amazon manually.
    Extracts cookies after successful login.
    """
    import nodriver as uc

    browser = await uc.start(headless=False)
    page = await browser.get(AMAZON_BASE + "/ap/signin")

    print("\n=== AlexaCart Login ===")
    print("Please log into your Amazon account in the browser window.")
    print("After you're logged in and see the Amazon homepage, press Enter here.")
    print("(Waiting for login...)")

    # Wait for the user to log in - poll for Amazon homepage
    while True:
        try:
            current_url = page.url
            if "amazon.com" in current_url and "/ap/" not in current_url:
                # User appears to be past the login page
                await asyncio.sleep(2)  # Give cookies a moment to settle
                break
        except Exception:
            pass
        await asyncio.sleep(1)

    # Extract cookies
    all_cookies = await browser.cookies.get_all()
    cookies = {}
    for cookie in all_cookies:
        if "amazon" in cookie.domain:
            cookies[cookie.name] = cookie.value

    await browser.stop()

    if not cookies:
        raise RuntimeError("No Amazon cookies captured. Login may have failed.")

    cookie_data = {"cookies": cookies, "source": "interactive_login"}
    save_cookies(cookie_data)
    print(f"Login successful! {len(cookies)} cookies saved.")
    return cookie_data


def try_refresh_via_sidecar() -> dict | None:
    """
    Try to refresh cookies using the Node.js alexa-cookie2 sidecar.
    Returns cookie data if successful, None if sidecar unavailable or refresh failed.
    """
    sidecar_path = settings.base_dir / "cookie_refresh" / "refresh.js"
    if not sidecar_path.exists():
        logger.info("Cookie refresh sidecar not found at %s", sidecar_path)
        return None

    existing = load_cookies()
    if not existing:
        logger.info("No existing cookies to refresh")
        return None

    try:
        result = subprocess.run(
            ["node", str(sidecar_path)],
            input=json.dumps(existing),
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(sidecar_path.parent),
        )
        if result.returncode == 0:
            new_data = json.loads(result.stdout)
            save_cookies(new_data)
            logger.info("Cookies refreshed via sidecar")
            return new_data
        else:
            logger.warning("Sidecar refresh failed: %s", result.stderr)
    except FileNotFoundError:
        logger.warning("Node.js not found, cannot run cookie refresh sidecar")
    except subprocess.TimeoutExpired:
        logger.warning("Cookie refresh sidecar timed out")
    except (json.JSONDecodeError, Exception) as e:
        logger.warning("Cookie refresh sidecar error: %s", e)

    return None


async def ensure_valid_cookies() -> dict:
    """
    Ensure we have valid cookies. Try loading, then refreshing, then prompt for login.
    Returns cookie data dict.
    Raises RuntimeError if no valid cookies can be obtained.
    """
    data = load_cookies()
    if data:
        return data

    # Try refreshing
    data = try_refresh_via_sidecar()
    if data:
        return data

    raise RuntimeError(
        "No valid Alexa cookies found. Run 'python -m alexacart.alexa.auth login' to authenticate."
    )


# CLI entry point
if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "login":
        asyncio.run(login_interactive())
    else:
        print("Usage: python -m alexacart.alexa.auth login")
        print("  Opens a browser for Amazon login and saves cookies.")
