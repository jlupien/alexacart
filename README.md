# AlexaCart

Automate your grocery ordering: read your Alexa shopping list, match items to Instacart products using your saved preferences, review the proposed cart, and add everything with one click.

## How It Works

1. **Fetch** — Reads your Alexa "Grocery List" via Amazon's mobile app API
2. **Match** — Looks up each item in your preference database (aliases + ranked products)
3. **Search** — Uses browser-use AI agent to find products on Instacart. Rank-1 preferred products that are in stock are auto-added to cart immediately — no waiting for review.
4. **Review** — Shows a table of proposed matches; auto-added items appear read-only, remaining items can be accepted or swapped
5. **Commit** — Adds remaining items to your Instacart cart and checks them off your Alexa list
6. **Learn** — Saves your corrections so future orders get smarter (deduplicates by product URL)

## Setup

### Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) package manager (`brew install uv`)
- A [browser-use](https://browser-use.com) API key (for `bu-2-0` model)

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
# Directory for database and cookies (default: ./data/)
# Point to a Dropbox/synced folder to share preferences across machines
DATA_DIR=

BROWSER_USE_API_KEY=your-api-key-here
ALEXA_LIST_NAME=Grocery List
INSTACART_STORE=Wegmans
SEARCH_CONCURRENCY=8
```

| Variable | Description | Default |
|----------|-------------|---------|
| `DATA_DIR` | Where the DB and cookies are stored. Set to a synced folder (e.g. Dropbox) to share across machines. | `./data/` |
| `BROWSER_USE_API_KEY` | API key from [browser-use](https://browser-use.com) | (required) |
| `ALEXA_LIST_NAME` | Name of your Alexa shopping list | `Grocery List` |
| `INSTACART_STORE` | Instacart store to search | `Wegmans` |
| `SEARCH_CONCURRENCY` | Number of parallel browser agents for searching Instacart | `4` |

### Run

```bash
uv run python run.py
```

Open http://127.0.0.1:8000 in your browser.

## Usage

### Start an Order

1. Click "Start Order" on the home page
2. Chrome windows open — log into Amazon (via nodriver) and Instacart (via browser-use) if prompted (first time only; sessions persist across runs)
3. The app fetches your Alexa list and searches Instacart for each item. Your #1 preferred products are auto-added to cart during search.
4. Review the proposed matches — auto-added items are shown read-only; pick alternatives or paste a custom Instacart URL for the rest
5. Click "Add Remaining to Cart" — live progress updates show each item being added to your cart and checked off the Alexa list

### Manage Preferences

Visit `/preferences` to:

- Add grocery items with aliases (e.g., "skim milk" = "fat free milk")
- Rank preferred products per item (the app tries #1 first, falls back to #2, etc.)
- Merge duplicate items

### Order History

Visit `/order/history` to see past orders and which items were corrected. You can delete individual sessions or clear all history.

## Architecture

- **FastAPI** + **Jinja2** + **htmx** — server-rendered UI with dynamic updates
- **SQLite** via **SQLAlchemy** — preference database (aliases, ranked products, order log)
- **browser-use** — AI-powered browser automation for Instacart
- **nodriver** — undetectable Chrome automation for Amazon cookie extraction (bypasses bot detection that flags Playwright/browser-use sessions)
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
│   ├── alexa/                # Amazon/Alexa integration
│   │   ├── auth.py           # Cookie management (OAuth device registration + nodriver)
│   │   └── client.py         # Alexa list API client
│   ├── instacart/
│   │   └── agent.py          # browser-use agent
│   ├── matching/
│   │   └── matcher.py        # Alias resolution + preference lookup
│   ├── routes/
│   │   ├── order.py          # Order flow endpoints
│   │   └── preferences.py    # Preference CRUD
│   ├── templates/            # Jinja2 templates
│   └── static/               # CSS + JS
└── data/                     # Runtime data (gitignored)
```
