# AlexaCart

Automate your grocery ordering: read your Alexa shopping list, match items to Instacart products using your saved preferences, review the proposed cart, and add everything with one click.

## How It Works

1. **Fetch** — Reads your Alexa "Grocery List" via Amazon's mobile app API
2. **Match** — Looks up each item in your preference database (aliases + ranked products)
3. **Search** — Searches Instacart's API for products on your store. Rank-1 preferred products that are in stock are auto-added to cart immediately — no waiting for review.
4. **Review** — Shows a table of proposed matches; auto-added items appear read-only, remaining items can be accepted or swapped
5. **Commit** — Adds remaining items to your Instacart cart and checks them off your Alexa list
6. **Learn** — Saves your corrections so future orders get smarter (deduplicates by product URL)

## Setup

### Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) package manager (`brew install uv`)

### Install

```bash
git clone <repo-url> alexacart
cd alexacart
uv sync
```

### Configure

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

```
# Directory for database (preferences, order log) — default: ./data/
# Set to a synced folder (e.g. Dropbox) to share preferences across machines
DATA_DIR=

# Directory for login cookies and browser profiles — default: ./data/
# Always local per machine (do NOT sync this)
LOCAL_DATA_DIR=

ALEXA_LIST_NAME=Grocery List
INSTACART_STORE=Wegmans

# When debugging, you can skip checking off list items
#SKIP_ALEXA_CHECKOFF=true

# Debug: clear cookies on order start to test the login workflow
# Amazon: clears data/cookies.json + nodriver profile (data/nodriver-amazon/)
# Instacart: clears data/instacart_cookies.json + nodriver profile (data/nodriver-instacart/)
#DEBUG_CLEAR_AMAZON_COOKIES=true
#DEBUG_CLEAR_INSTACART_COOKIES=true
```

| Variable | Description | Default |
|----------|-------------|---------|
| `DATA_DIR` | Where the SQLite DB is stored (preferences, aliases, order log). Set to a synced folder (e.g. Dropbox) to share preferences across machines. | `./data/` |
| `LOCAL_DATA_DIR` | Where login cookies and nodriver browser profiles are stored. Always local per machine. | `./data/` |
| `ALEXA_LIST_NAME` | Name of your Alexa shopping list | `Grocery List` |
| `INSTACART_STORE` | Instacart store to search (must match the store name on Instacart) | `Wegmans` |
| `SKIP_ALEXA_CHECKOFF` | Skip checking off items on the Alexa list after commit (useful for debugging) | `false` |
| `DEBUG_CLEAR_AMAZON_COOKIES` | Clear Amazon cookies + Chrome profile on each order start (forces re-login) | `false` |
| `DEBUG_CLEAR_INSTACART_COOKIES` | Clear Instacart cookies + Chrome profile on each order start (forces re-login) | `false` |

### Run

```bash
uv run python run.py
```

Open http://127.0.0.1:8000 in your browser.

### Run at Login (macOS)

Install a LaunchAgent so the server starts automatically when you log in:

```bash
bash scripts/install-launchagent.sh
```

The script will prompt for your password to configure log rotation via `newsyslog` (caps `data/alexacart.log` at 5 MB). You can skip it — the server still works, logs just won't rotate.

The server runs in the background at http://127.0.0.1:8000.

**Managing the agent** (without removing it):

```bash
# Stop
launchctl unload ~/Library/LaunchAgents/com.alexacart.plist

# Start
launchctl load ~/Library/LaunchAgents/com.alexacart.plist

# Restart
launchctl unload ~/Library/LaunchAgents/com.alexacart.plist && launchctl load ~/Library/LaunchAgents/com.alexacart.plist

# Restart + clear logs (truncates the log file in-place; no need to reopen in editor)
bash scripts/restart-launchagent.sh
```

**Remove entirely:**

```bash
bash scripts/uninstall-launchagent.sh
```

## Usage

### Start an Order

1. Click "Start Order" on the home page
2. Chrome windows open — log into Amazon and Instacart if prompted (first time only; sessions persist across runs via nodriver Chrome profiles)
3. The app fetches your Alexa list and searches Instacart for each item. Your preferred products are at the top, if available.
4. Review the proposed matches — pick alternatives or paste a custom Instacart URL for the rest
5. Click "Add Remaining to Cart" — live progress updates show each item being added to your cart and checked off the Alexa list

### Manage Preferences

Visit `/preferences` to:

- Add grocery items with aliases (e.g., "skim milk" = "fat free milk")
- Rank preferred products per item (the app tries #1 first, falls back to #2, etc.)
- Merge duplicate items

### Order History

Visit `/order/history` to see past orders and which items were corrected. You can delete individual sessions or clear all history.

### Settings

Visit `/settings` to:

- Check Amazon and Instacart login status
- Validate Amazon cookies against the real API
- Log out of Amazon or Instacart (clears cookies + Chrome profile)
- View DB stats and current config
- Shut down the server

## Architecture

- **FastAPI** + **Jinja2** + **htmx** — server-rendered UI with dynamic updates
- **SQLite** via **SQLAlchemy** — preference database (aliases, ranked products, order log)
- **httpx** — direct HTTP calls to Instacart's GraphQL API (product search, add-to-cart) and Amazon's Alexa Shopping List API
- **nodriver** — undetectable Chrome automation for login/cookie extraction on both Amazon and Instacart (bypasses bot detection)
- **SSE** — real-time progress updates during search and commit phases

## File Structure

```
alexacart/
├── run.py                    # Entry point
├── alexacart/
│   ├── app.py                # FastAPI app factory
│   ├── config.py             # Settings from .env
│   ├── db.py                 # SQLAlchemy setup
│   ├── models.py             # ORM models
│   ├── nodriver_patch.py     # Compatibility patches for newer Chrome versions
│   ├── alexa/                # Amazon/Alexa integration
│   │   ├── auth.py           # Cookie management (OAuth device registration + nodriver)
│   │   └── client.py         # Alexa list API client (httpx)
│   ├── instacart/
│   │   ├── auth.py           # Cookie extraction via nodriver
│   │   └── client.py         # Instacart GraphQL API client (httpx)
│   ├── matching/
│   │   └── matcher.py        # Alias resolution + preference lookup
│   ├── routes/
│   │   ├── order.py          # Order flow endpoints
│   │   ├── preferences.py    # Preference CRUD
│   │   └── settings.py       # Settings + server shutdown
│   ├── templates/            # Jinja2 templates
│   └── static/               # CSS + JS
├── scripts/
│   ├── install-launchagent.sh   # Install macOS LaunchAgent (start at login)
│   ├── restart-launchagent.sh  # Restart agent + clear log (dev helper)
│   └── uninstall-launchagent.sh # Remove LaunchAgent
└── data/                     # Runtime data (gitignored)
```
