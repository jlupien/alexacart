"""
Order flow routes:
1. GET /order/ — landing page
2. POST /order/start — fetch Alexa list, begin matching & searching
3. GET /order/progress/{session_id} — SSE stream for search progress
4. GET /order/review/{session_id} — review page with proposals
5. GET /order/search — search Instacart for a product (for product picker)
6. POST /order/commit — add items to Instacart cart and check off Alexa list
"""

import asyncio
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html import escape as html_escape

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse
from sse_starlette.sse import EventSourceResponse
from sqlalchemy.orm import Session

from alexacart.app import templates
from alexacart.config import settings
from alexacart.db import SessionLocal, get_db
from alexacart.matching.matcher import (
    add_preferred_product,
    create_grocery_item,
    find_match,
    make_product_top_choice,
    normalize_text,
)
from alexacart.models import Alias, OrderLog

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/order", tags=["order"])

# In-memory store for active order sessions
_sessions: dict[str, "OrderSession"] = {}


@dataclass
class ProductOption:
    product_name: str
    product_url: str | None = None
    brand: str | None = None
    price: str | None = None
    image_url: str | None = None
    in_stock: bool = True


@dataclass
class ProposalItem:
    index: int
    alexa_text: str
    alexa_item_id: str = ""
    alexa_list_id: str = ""
    alexa_item_version: int = 1
    grocery_item_id: int | None = None
    grocery_item_name: str | None = None
    product_name: str | None = None
    product_url: str | None = None
    brand: str | None = None
    price: str | None = None
    image_url: str | None = None
    status: str = "Pending"  # Matched, Substituted, New item, Error
    status_class: str = "new"  # matched, substituted, new, error
    in_stock: bool = True
    alternatives: list[ProductOption] = field(default_factory=list)
    extra_alexa_items: list[dict] = field(default_factory=list)
    auto_committed: bool = False  # True if added to cart during search (rank 1 match)


@dataclass
class OrderSession:
    session_id: str
    proposals: list[ProposalItem] = field(default_factory=list)
    total_items: int = 0
    searched_count: int = 0
    status: str = "starting"  # starting, searching, ready, committing, done
    error: str | None = None
    active_searches: set = field(default_factory=set)
    # Commit state (queue-based for reliable SSE delivery)
    commit_queue: asyncio.Queue | None = None
    commit_items_data: dict = field(default_factory=dict)
    active_commits: set = field(default_factory=set)
    # Persistent browser pool (shared between search and commit)
    browser_pool: object | None = None
    # Detailed status text shown during logging_in phase
    status_detail: str = ""
    auto_committed_count: int = 0


@router.get("/")
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@router.post("/start")
async def start_order(request: Request):
    """Start the order flow. Launches browser, checks logins, fetches Alexa list, searches."""
    session_id = str(uuid.uuid4())
    session = OrderSession(session_id=session_id)
    session.status = "logging_in"
    _sessions[session_id] = session

    # Everything runs in the background task — logins, Alexa fetch, Instacart search
    asyncio.create_task(_run_order(session))

    return HTMLResponse(
        f'<div id="search-progress" '
        f'sse-connect="/order/progress/{session_id}" '
        f'sse-swap="progress" '
        f'sse-close="close" '
        f'hx-swap="innerHTML">'
        f'<div class="progress-container">'
        f'<div class="progress-bar"><div class="progress-fill" style="width: 0%"></div></div>'
        f'<p class="progress-text">Starting up...</p>'
        f"</div></div>"
    )


async def _run_order(session: OrderSession):
    """Background task: check logins, fetch Alexa list, then search Instacart."""
    from alexacart.alexa.auth import extract_cookies_via_nodriver
    from alexacart.alexa.client import AlexaClient
    from alexacart.instacart.agent import BrowserPool

    pool = BrowserPool(settings.search_concurrency)
    session.browser_pool = pool

    try:
        # Step 1: Parallel auth — Instacart via browser-use, Amazon via nodriver.
        # nodriver is used for Amazon because browser-use/Playwright sessions get
        # flagged by Amazon's bot detection, resulting in limited-scope tokens
        # that can't access the Shopping List API.
        session.status = "logging_in"
        session.status_detail = "Checking logins..."

        on_status = lambda msg: setattr(session, "status_detail", msg)

        instacart_result, amazon_result = await asyncio.gather(
            pool.start_with_auth(on_status=on_status),
            extract_cookies_via_nodriver(on_status=on_status),
            return_exceptions=True,
        )

        # Process Instacart result
        if isinstance(instacart_result, Exception):
            logger.error("Instacart auth failed: %s", instacart_result, exc_info=True)
            session.error = f"Instacart login failed: {instacart_result}"
            session.status = "error"
            return
        if not instacart_result:
            session.error = "Timed out waiting for Instacart login. Please try again."
            session.status = "error"
            return

        # Process Amazon/nodriver result
        if isinstance(amazon_result, Exception):
            logger.error("nodriver cookie extraction failed: %s", amazon_result, exc_info=True)
            session.error = f"Amazon authentication failed: {amazon_result}"
            session.status = "error"
            return
        cookie_data = amazon_result
        if not cookie_data or not cookie_data.get("cookies"):
            session.error = "Could not extract Amazon cookies. Please try again."
            session.status = "error"
            return

        # Step 2: Fetch Alexa shopping list using nodriver cookies
        # AlexaClient stays open through search phase for auto-commit check-offs
        session.status_detail = "Fetching your Alexa shopping list..."
        alexa_client = AlexaClient(
            cookie_refresh_fn=lambda: extract_cookies_via_nodriver(),
            interactive_cookie_refresh_fn=lambda: extract_cookies_via_nodriver(
                on_status=on_status,
                force_relogin=True,
            ),
        )
        try:
            try:
                items = await alexa_client.get_items()
            except Exception as e:
                error_str = str(e)
                logger.error("Alexa list fetch failed: %s", error_str, exc_info=True)
                if "401" in error_str:
                    session.error = (
                        "Amazon session not authorized for Shopping List API. "
                        "Please log into amazon.com in the browser window and try again."
                    )
                elif "503" in error_str or "502" in error_str:
                    session.error = "Amazon servers are temporarily unavailable. Please try again in a minute."
                else:
                    session.error = f"Failed to fetch Alexa list: {error_str}"
                session.status = "error"
                return

            if not items:
                session.error = "No items found on your Alexa Grocery List. Add some items via Alexa and try again."
                session.status = "error"
                return

            # Deduplicate items: group by grocery_item_id (known) or normalized text (unknown)
            db = SessionLocal()
            try:
                groups: dict[str | int, list] = {}
                for item in items:
                    normalized = normalize_text(item.text)
                    alias = db.query(Alias).filter(Alias.alias == normalized).first()
                    if alias:
                        key = alias.grocery_item_id
                    else:
                        key = f"_unknown:{normalized}"
                    groups.setdefault(key, []).append(item)
            finally:
                db.close()

            for i, group_items in enumerate(groups.values()):
                primary = group_items[0]
                extras = group_items[1:]
                session.proposals.append(
                    ProposalItem(
                        index=i,
                        alexa_text=primary.text,
                        alexa_item_id=primary.item_id,
                        alexa_list_id=primary.list_id,
                        alexa_item_version=primary.version,
                        extra_alexa_items=[
                            {"item_id": e.item_id, "text": e.text, "list_id": e.list_id, "version": e.version}
                            for e in extras
                        ],
                    )
                )
            session.total_items = len(session.proposals)

            # Step 4: Search Instacart for each item (pool is already running)
            await _search_items(session, pool, alexa_client)
        finally:
            await alexa_client.close()

    except Exception as e:
        logger.exception("Order flow failed")
        session.error = str(e)
        session.status = "error"
        # Close pool on error (commit won't happen)
        await pool.close()
        session.browser_pool = None


async def _apply_search_results(
    proposal: ProposalItem, agent, query: str, status: str, status_class: str
):
    """Search Instacart and apply the best result to a proposal."""
    results = await agent.search_product(query)
    if results:
        best = results[0]
        proposal.product_name = best.product_name
        proposal.product_url = best.product_url
        proposal.brand = best.brand
        proposal.price = best.price
        proposal.image_url = best.image_url
        proposal.status = status
        proposal.status_class = status_class
        proposal.in_stock = best.in_stock
        proposal.alternatives = [
            ProductOption(
                product_name=r.product_name,
                product_url=r.product_url,
                brand=r.brand,
                price=r.price,
                image_url=r.image_url,
                in_stock=r.in_stock,
            )
            for r in results
        ]
    else:
        proposal.status = "No results"
        proposal.status_class = "error"


async def _auto_checkoff_alexa(alexa_client, proposal: ProposalItem):
    """Check off a proposal's primary + duplicate Alexa items. Returns True if all succeeded."""
    from alexacart.alexa.client import AlexaListItem

    if not proposal.alexa_item_id or settings.skip_alexa_checkoff:
        if settings.skip_alexa_checkoff:
            logger.info("Skipping Alexa check-off for '%s' (SKIP_ALEXA_CHECKOFF=true)", proposal.alexa_text)
        return True

    alexa_item = AlexaListItem(
        item_id=proposal.alexa_item_id,
        text=proposal.alexa_text,
        list_id=proposal.alexa_list_id,
        version=proposal.alexa_item_version,
    )
    checked_off = await alexa_client.mark_complete(alexa_item)
    if not checked_off:
        logger.warning("Could not check off '%s' on Alexa list", proposal.alexa_text)

    for extra in proposal.extra_alexa_items:
        extra_item = AlexaListItem(
            item_id=extra["item_id"],
            text=extra["text"],
            list_id=extra["list_id"],
            version=extra["version"],
        )
        extra_ok = await alexa_client.mark_complete(extra_item)
        if not extra_ok:
            logger.warning("Could not check off duplicate '%s' on Alexa list", extra["text"])

    return checked_off


async def _search_single_item(
    session: OrderSession, proposal: ProposalItem, pool, alexa_client,
):
    """Search Instacart for a single item using a worker from the pool."""
    worker = await pool.acquire()
    db = SessionLocal()

    session.active_searches.add(proposal.alexa_text)
    try:
        match = find_match(db, proposal.alexa_text)
        proposal.grocery_item_id = match.grocery_item_id
        proposal.grocery_item_name = match.grocery_item_name

        if match.is_known and match.preferred_products:
            # Try preferred products in rank order
            found = False
            for pref in match.preferred_products:
                if pref.product_url:
                    result = await worker.check_product_by_url(pref.product_url)
                else:
                    results = await worker.search_product(pref.product_name)
                    result = results[0] if results else None
                if result and result.in_stock:
                    proposal.product_name = result.product_name
                    proposal.product_url = result.product_url or pref.product_url
                    proposal.brand = result.brand or pref.brand
                    proposal.price = result.price
                    proposal.image_url = result.image_url or pref.image_url
                    proposal.in_stock = True
                    found = True

                    pref.last_seen_in_stock = datetime.now(UTC)
                    db.commit()

                    # Auto-commit rank-1 matches immediately
                    if pref.rank == 1:
                        try:
                            added = await worker.add_to_cart(
                                proposal.product_name,
                                product_url=proposal.product_url or None,
                            )
                        except Exception as add_err:
                            logger.warning(
                                "Auto-commit add_to_cart failed for '%s': %s",
                                proposal.alexa_text, add_err,
                            )
                            added = False

                        if added:
                            proposal.auto_committed = True
                            proposal.status = "Auto-added (choice #1)"
                            proposal.status_class = "matched"
                            session.auto_committed_count += 1

                            try:
                                await _auto_checkoff_alexa(alexa_client, proposal)
                            except Exception as chk_err:
                                logger.warning(
                                    "Auto-commit Alexa check-off failed for '%s': %s",
                                    proposal.alexa_text, chk_err,
                                )

                            _learn_from_result(
                                db,
                                alexa_text=proposal.alexa_text,
                                grocery_item_id=proposal.grocery_item_id,
                                final_product=proposal.product_name,
                                product_url=proposal.product_url,
                                brand=proposal.brand,
                                image_url=proposal.image_url,
                                was_corrected=False,
                            )

                            log_entry = OrderLog(
                                session_id=session.session_id,
                                alexa_text=proposal.alexa_text,
                                matched_grocery_item_id=proposal.grocery_item_id,
                                proposed_product=proposal.product_name,
                                final_product=proposal.product_name,
                                was_corrected=False,
                                added_to_cart=True,
                            )
                            db.add(log_entry)
                            db.commit()
                        else:
                            # Fallback: show normally on review page
                            proposal.status = f"Matched (choice #{pref.rank})"
                            proposal.status_class = "matched"
                    else:
                        proposal.status = f"Matched (choice #{pref.rank})"
                        proposal.status_class = "matched"

                    break

            if not found:
                await _apply_search_results(
                    proposal, worker, proposal.alexa_text,
                    "Substituted (usual out of stock)", "substituted",
                )
        else:
            await _apply_search_results(
                proposal, worker, proposal.alexa_text, "New item", "new",
            )

    except Exception as e:
        logger.error("Search failed for '%s': %s", proposal.alexa_text, e)
        proposal.status = f"Error: {e}"
        proposal.status_class = "error"
    finally:
        session.active_searches.discard(proposal.alexa_text)
        session.searched_count += 1
        db.close()
        pool.release(worker)


async def _search_items(session: OrderSession, pool, alexa_client):
    """Search Instacart for each item in the session — in parallel via BrowserPool."""
    session.status = "searching"

    try:
        logger.info(
            "Searching %d items with BrowserPool (%d agents)",
            len(session.proposals), pool._size,
        )

        # Launch all searches concurrently — pool limits actual parallelism
        results = await asyncio.gather(
            *[
                _search_single_item(session, proposal, pool, alexa_client)
                for proposal in session.proposals
            ],
            return_exceptions=True,
        )

        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error("Parallel search task %d failed: %s", i, result)

        session.status = "ready"

    except Exception as e:
        logger.exception("Search task failed")
        session.error = str(e)
        session.status = "ready"


@router.get("/progress/{session_id}")
async def progress_stream(session_id: str):
    """SSE stream for search progress updates."""

    async def generate():
        session = _sessions.get(session_id)
        if not session:
            yield {"event": "progress", "data": '<div class="status-message status-error">Session not found</div>'}
            yield {"event": "close", "data": ""}
            return

        while session.status == "logging_in":
            detail = html_escape(session.status_detail or "Starting up...")
            yield {
                "event": "progress",
                "data": (
                    '<div class="progress-container">'
                    f'<p class="progress-text">{detail}</p>'
                    '</div>'
                ),
            }
            await asyncio.sleep(2)

        if session.status == "error":
            yield {
                "event": "progress",
                "data": f'<div class="status-message status-error">{html_escape(session.error or "")}</div>',
            }
            yield {"event": "close", "data": ""}
            return

        while session.status == "fetching_list":
            yield {
                "event": "progress",
                "data": (
                    '<div class="progress-container">'
                    '<p class="progress-text">Fetching your Alexa shopping list...</p>'
                    '</div>'
                ),
            }
            await asyncio.sleep(2)

        if session.status == "error":
            yield {
                "event": "progress",
                "data": f'<div class="status-message status-error">{html_escape(session.error or "")}</div>',
            }
            yield {"event": "close", "data": ""}
            return

        while session.status == "searching":
            pct = (
                int(session.searched_count / session.total_items * 100)
                if session.total_items > 0
                else 0
            )
            active = list(session.active_searches)

            html = (
                f'<div class="progress-container">'
                f'<div class="progress-bar">'
                f'<div class="progress-fill" style="width: {pct}%"></div>'
                f"</div>"
                f'<p class="progress-text">'
                f"Searched {session.searched_count} of {session.total_items} items ({pct}%)"
                f"</p>"
            )
            if session.auto_committed_count > 0:
                html += (
                    f'<p class="progress-text">'
                    f"&#10003; {session.auto_committed_count} item(s) auto-added to cart"
                    f"</p>"
                )
            if active:
                items_str = ", ".join(html_escape(a) for a in active)
                html += f'<p class="progress-text">Searching: {items_str}...</p>'
            html += "</div>"

            yield {"event": "progress", "data": html}
            await asyncio.sleep(2)

        # Search complete — redirect to review page
        if session.error:
            yield {
                "event": "progress",
                "data": (
                    f'<div class="status-message status-error">Search error: {html_escape(session.error or "")}</div>'
                    f'<a href="/order/review/{session_id}" class="btn btn-primary">Review Partial Results</a>'
                ),
            }
        else:
            yield {
                "event": "progress",
                "data": (
                    f'<div class="status-message status-success">'
                    f"All {session.total_items} items searched!</div>"
                    f'<script>window.location.href="/order/review/{session_id}";</script>'
                ),
            }

        yield {"event": "close", "data": ""}

    return EventSourceResponse(generate())


@router.get("/review/{session_id}")
async def review_page(request: Request, session_id: str):
    """Display the order review page."""
    session = _sessions.get(session_id)
    if not session:
        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "error": "Session not found. Please start a new order.",
            },
        )

    return templates.TemplateResponse(
        "review.html",
        {
            "request": request,
            "session_id": session_id,
            "proposals": session.proposals,
        },
    )


@router.get("/search")
async def search_products(request: Request, q: str = Query(...), index: int = Query(0)):
    """Search Instacart for a product (used by the product picker)."""
    from alexacart.instacart.agent import InstacartAgent

    agent = InstacartAgent(headless=True)
    try:
        results = await agent.search_product(q)
        product_dicts = [
            {
                "product_name": r.product_name,
                "product_url": r.product_url,
                "brand": r.brand,
                "price": r.price,
                "image_url": r.image_url,
            }
            for r in results
        ]
        return HTMLResponse(
            templates.get_template("partials/product_picker.html").render(
                {"request": request, "query": q, "index": index, "results": product_dicts}
            )
        )
    except Exception as e:
        logger.error("Product search failed: %s", e)
        return HTMLResponse(
            f'<div class="status-message status-error">Search failed: {html_escape(str(e))}</div>'
        )
    finally:
        await agent.close()


@router.post("/fetch-url")
async def fetch_product_url(
    request: Request,
    url: str = Form(...),
    index: int = Form(0),
    session_id: str = Form(""),
):
    """Fetch product details from a custom Instacart URL."""
    from alexacart.instacart.agent import InstacartAgent

    # Reuse an agent from the active order's BrowserPool when available,
    # instead of launching a brand-new browser (which can timeout).
    pool = None
    agent = None
    borrowed = False

    session = _sessions.get(session_id) if session_id else None
    if session and session.browser_pool and not session.browser_pool._closed:
        pool = session.browser_pool
        try:
            agent = await asyncio.wait_for(pool.acquire(), timeout=5)
            borrowed = True
        except (asyncio.TimeoutError, Exception):
            agent = None

    if agent is None:
        agent = InstacartAgent(headless=True)

    try:
        result = await agent.check_product_by_url(url)
        if result:
            return HTMLResponse(
                f'<script>'
                f'selectProduct({index}, {json.dumps(result.product_name)}, '
                f'{json.dumps(result.price or "")}, {json.dumps(result.image_url or "")}, '
                f'{json.dumps(url)}, {json.dumps(result.brand or "")})'
                f'</script>'
            )
        return HTMLResponse(
            '<div class="status-message status-error" style="margin-top:0.5rem">'
            'Could not find a product at that URL.</div>'
        )
    except Exception as e:
        logger.error("URL fetch failed: %s", e)
        return HTMLResponse(
            f'<div class="status-message status-error" style="margin-top:0.5rem">'
            f'Error: {html_escape(str(e))}</div>'
        )
    finally:
        if borrowed and pool:
            pool.release(agent)
        elif not borrowed:
            await agent.close()


@router.post("/commit")
async def commit_order(request: Request):
    """Start the commit flow: parse form, launch background task, return SSE progress."""
    form = await request.form()
    session_id = form.get("session_id", "")

    session = _sessions.get(session_id)
    if not session:
        return HTMLResponse(
            '<div class="status-message status-error">Session expired. Please start a new order.</div>'
        )

    # Parse form data — items are sent as items[0][product_name], items[0][alexa_text], etc.
    items_data = {}
    for key, value in form.items():
        m = re.match(r"items\[(\d+)\]\[(\w+)\]", key)
        if m:
            idx, field_name = int(m.group(1)), m.group(2)
            if idx not in items_data:
                items_data[idx] = {}
            items_data[idx][field_name] = value

    session.commit_items_data = items_data
    session.commit_queue = asyncio.Queue()

    asyncio.create_task(_run_commit(session))

    return HTMLResponse(
        f'<div id="commit-progress">'
        f'<div class="progress-container">'
        f'<p class="progress-text">Starting...</p>'
        f"</div></div>"
        f"<script>"
        f"(function(){{"
        f'var form=document.getElementById("review-form");'
        f'if(form)form.querySelectorAll("input,button,details").forEach(function(x){{x.disabled=true}});'
        f'if(form)form.querySelectorAll(".skip-option,.custom-url-section").forEach(function(x){{x.style.display="none"}});'
        f'var src=new EventSource("/order/commit-progress/{session_id}");'
        f'var el=document.getElementById("commit-progress");'
        f'src.addEventListener("progress",function(e){{'
        f"el.innerHTML=e.data;"
        f'el.querySelectorAll("script").forEach(function(s){{eval(s.textContent)}});'
        f"}});"
        f'src.addEventListener("close",function(){{src.close()}});'
        f'src.addEventListener("error",function(){{src.close()}});'
        f"}})();"
        f"</script>"
    )


async def _commit_single_item(
    session: OrderSession,
    idx: int,
    data: dict,
    pool,
    alexa_client,
    q: asyncio.Queue,
    total: int,
    commit_counter: list,
) -> dict:
    """Add a single item to Instacart cart and check off Alexa list."""
    product_name = data.get("product_name", "")
    alexa_text = data.get("alexa_text", "")
    grocery_item_id = data.get("grocery_item_id", "")
    alexa_item_id = data.get("alexa_item_id", "")

    if data.get("skip") == "1":
        db = SessionLocal()
        try:
            proposal = None
            for p in session.proposals:
                if p.index == idx:
                    proposal = p
                    break
            log_entry = OrderLog(
                session_id=session.session_id,
                alexa_text=alexa_text,
                matched_grocery_item_id=int(grocery_item_id) if grocery_item_id else None,
                proposed_product=proposal.product_name if proposal else None,
                skipped=True,
            )
            db.add(log_entry)
            db.commit()
        finally:
            db.close()
        await q.put(("skip", idx, alexa_text, commit_counter[0], total))
        return {"text": alexa_text, "success": True, "reason": "Skipped", "skipped": True}

    if not product_name:
        commit_counter[0] += 1
        await q.put(("done", idx, alexa_text, False, "No product selected", commit_counter[0], total))
        return {"text": alexa_text, "success": False, "reason": "No product selected"}

    worker = await pool.acquire()
    db = SessionLocal()

    session.active_commits.add(alexa_text)
    try:
        logger.info("Commit: pushing 'active' for idx=%s text=%s", idx, alexa_text)
        await q.put(("active", idx, alexa_text, commit_counter[0], total))

        proposal = None
        for p in session.proposals:
            if p.index == idx:
                proposal = p
                break

        product_url = data.get("product_url", "")
        added = await worker.add_to_cart(product_name, product_url=product_url or None)
        checked_off = True

        if added and proposal:
            checked_off = await _auto_checkoff_alexa(alexa_client, proposal)

        was_corrected = False
        if proposal and proposal.product_name and proposal.product_name != product_name:
            was_corrected = True

        log_entry = OrderLog(
            session_id=session.session_id,
            alexa_text=alexa_text,
            matched_grocery_item_id=int(grocery_item_id) if grocery_item_id else None,
            proposed_product=proposal.product_name if proposal else None,
            final_product=product_name,
            was_corrected=was_corrected,
            added_to_cart=added,
        )
        db.add(log_entry)

        if added:
            image_url = data.get("image_url") or (proposal.image_url if proposal else None)
            _learn_from_result(
                db,
                alexa_text=alexa_text,
                grocery_item_id=int(grocery_item_id) if grocery_item_id else None,
                final_product=product_name,
                product_url=product_url or None,
                brand=data.get("brand"),
                image_url=image_url,
                was_corrected=was_corrected,
            )

        db.commit()

        reason = ""
        if not added:
            reason = "Failed to add to cart"
        elif not checked_off:
            reason = "Added but could not check off Alexa list"

        result = {
            "text": alexa_text,
            "product": product_name,
            "success": added,
            "reason": reason,
        }
        commit_counter[0] += 1
        logger.info("Commit: pushing 'done' for idx=%s text=%s added=%s", idx, alexa_text, added)
        await q.put(("done", idx, alexa_text, added, reason, commit_counter[0], total))
        return result

    except Exception as e:
        logger.error("Commit failed for '%s': %s", alexa_text, e)
        db.rollback()
        commit_counter[0] += 1
        await q.put(("done", idx, alexa_text, False, str(e), commit_counter[0], total))
        return {"text": alexa_text, "success": False, "reason": str(e)}
    finally:
        session.active_commits.discard(alexa_text)
        db.close()
        pool.release(worker)


async def _run_commit(session: OrderSession):
    """Background task: add items to Instacart cart and check off Alexa list — in parallel."""
    from alexacart.alexa.auth import extract_cookies_via_nodriver
    from alexacart.alexa.client import AlexaClient
    from alexacart.instacart.agent import BrowserPool

    pool = session.browser_pool
    if pool is None:
        # Edge case: pool lost — create a fresh one (no auth, just headless workers)
        logger.warning("Browser pool not found on session — creating fresh pool")
        pool = BrowserPool(settings.search_concurrency)
        session.browser_pool = pool
        await pool.start_with_auth()

    alexa_client = AlexaClient(
        cookie_refresh_fn=lambda: extract_cookies_via_nodriver(),
        interactive_cookie_refresh_fn=lambda: extract_cookies_via_nodriver(
            force_relogin=True,
        ),
    )
    q = session.commit_queue
    total = sum(1 for d in session.commit_items_data.values() if d.get("skip") != "1")
    commit_counter = [0]  # mutable counter shared across parallel tasks

    try:
        logger.info(
            "Committing %d items with BrowserPool (%d agents)",
            len(session.commit_items_data), pool._size,
        )

        # Launch all commits concurrently — pool limits actual parallelism
        results = await asyncio.gather(
            *[
                _commit_single_item(
                    session, idx, data, pool, alexa_client, q, total, commit_counter,
                )
                for idx, data in sorted(session.commit_items_data.items())
            ],
            return_exceptions=True,
        )

        commit_results = []
        for r in results:
            if isinstance(r, Exception):
                logger.error("Commit task failed: %s", r)
                commit_results.append({"text": "?", "success": False, "reason": str(r)})
            else:
                commit_results.append(r)

        added_count = sum(1 for r in commit_results if r["success"] and not r.get("skipped"))
        skipped_count = sum(1 for r in commit_results if r.get("skipped"))
        await q.put(("complete", added_count, skipped_count, len(commit_results)))

    except Exception as e:
        logger.exception("Error during commit")
        await q.put(("error", str(e)))
    finally:
        await alexa_client.close()
        # Close the pool — commit is the final phase
        await pool.close()
        session.browser_pool = None


@router.get("/commit-progress/{session_id}")
async def commit_progress_stream(session_id: str):
    """SSE stream for commit progress — uses queue for reliable event delivery."""

    async def generate():
        session = _sessions.get(session_id)
        if not session or not session.commit_queue:
            logger.warning("Commit SSE: session %s not found or no queue", session_id)
            yield {"event": "progress", "data": '<div class="status-message status-error">Session not found</div>'}
            yield {"event": "close", "data": ""}
            return

        q = session.commit_queue
        logger.info("Commit SSE: connected for session %s", session_id)

        while True:
            try:
                event = await asyncio.wait_for(q.get(), timeout=600)
            except asyncio.TimeoutError:
                yield {
                    "event": "progress",
                    "data": '<div class="status-message status-error">Commit timed out.</div>',
                }
                yield {"event": "close", "data": ""}
                _sessions.pop(session_id, None)
                return

            event_type = event[0]
            logger.info("Commit SSE: sending event type=%s", event_type)

            if event_type == "complete":
                _, added_count, skipped_count, total_count = event
                auto_count = session.auto_committed_count
                total_added = added_count + auto_count
                total_all = total_count + auto_count
                store_slug = settings.instacart_store.lower()
                summary = (
                    f'<div class="results-summary card">'
                    f"<h3>Order Complete</h3>"
                    f'<p class="muted" style="margin-bottom:1rem">'
                    f"{total_added} of {total_all} items added to cart"
                )
                if auto_count:
                    summary += f" ({auto_count} auto-added)"
                if skipped_count:
                    summary += f", {skipped_count} skipped"
                summary += (
                    f"</p>"
                    f'<div style="display:flex;gap:0.75rem;justify-content:center">'
                    f'<a href="https://www.instacart.com/store/{store_slug}/storefront" '
                    f'target="_blank" class="btn btn-primary">Review Cart on Instacart</a>'
                    f'<a href="/order/" class="btn btn-outline">Start New Order</a>'
                    f"</div></div>"
                )
                yield {"event": "progress", "data": summary}
                yield {"event": "close", "data": ""}
                _sessions.pop(session_id, None)
                return

            if event_type == "error":
                _, error_msg = event
                yield {
                    "event": "progress",
                    "data": f'<div class="status-message status-error">Error: {html_escape(error_msg)}</div>',
                }
                yield {"event": "close", "data": ""}
                _sessions.pop(session_id, None)
                return

            active = list(session.active_commits)

            if event_type == "skip":
                _, idx, alexa_text, count, total = event
                script = _row_update_script(idx, "badge-substituted", "&mdash; Skipped")
                yield {"event": "progress", "data": _commit_progress_bar(count, total, active) + script}

            elif event_type == "active":
                _, idx, alexa_text, count, total = event
                script = _row_update_script(idx, "badge-new commit-pulse", "Adding...")
                yield {"event": "progress", "data": _commit_progress_bar(count, total, active) + script}

            elif event_type == "done":
                _, idx, alexa_text, success, reason, count, total = event
                if success:
                    badge_html = "&#10003; Added"
                    extra = ""
                    if reason:
                        extra = f'<span class="muted" style="font-size:0.75rem;display:block">{html_escape(reason)}</span>'
                    script = _row_update_script(idx, "badge-matched", badge_html, extra)
                else:
                    badge_html = f"&#10007; {html_escape(reason or 'Failed')}"
                    script = _row_update_script(idx, "badge-error", badge_html)
                yield {"event": "progress", "data": _commit_progress_bar(count, total, active) + script}

    return EventSourceResponse(generate())


def _row_update_script(idx: int, badge_class: str, badge_html: str, extra: str = "") -> str:
    """Return a <script> tag that updates a row's status badge via JS."""
    return (
        f'<script>'
        f'(function(){{'
        f'var el=document.getElementById("status-{idx}");'
        f'if(el)el.innerHTML=\'<span class="badge {badge_class}">{badge_html}</span>{extra}\';'
        f'}})();'
        f'</script>'
    )


def _commit_progress_bar(count: int, total: int, active_items: list[str] | None = None) -> str:
    """Render the commit progress bar HTML, optionally showing active items."""
    pct = int(count / total * 100) if total > 0 else 0
    html = (
        f'<div class="progress-container">'
        f'<div class="progress-bar">'
        f'<div class="progress-fill" style="width: {pct}%"></div>'
        f"</div>"
        f'<p class="progress-text">Added {count} of {total}</p>'
    )
    if active_items:
        items_str = ", ".join(html_escape(a) for a in active_items[:4])
        if len(active_items) > 4:
            items_str += f" +{len(active_items) - 4} more"
        html += f'<p class="progress-text">Adding: {items_str}...</p>'
    html += "</div>"
    return html


def _learn_from_result(
    db: Session,
    alexa_text: str,
    grocery_item_id: int | None,
    final_product: str,
    product_url: str | None,
    brand: str | None,
    image_url: str | None,
    was_corrected: bool,
):
    """Learn from the user's choices to improve future proposals."""
    if grocery_item_id:
        # Known item
        if was_corrected:
            # User changed the proposal — make their choice the top preference
            make_product_top_choice(db, grocery_item_id, final_product, product_url=product_url, brand=brand, image_url=image_url)
        else:
            # User accepted — ensure product is in preferences (dedup by URL then name)
            add_preferred_product(db, grocery_item_id, final_product, product_url=product_url, brand=brand, image_url=image_url)
    else:
        # Unknown item — create new grocery item + alias + preferred product
        item = create_grocery_item(db, alexa_text)
        add_preferred_product(db, item.id, final_product, product_url=product_url, brand=brand, image_url=image_url, rank=1)


@router.delete("/history/{session_id}")
async def delete_history_session(session_id: str, db: Session = Depends(get_db)):
    """Delete all order log entries for a single session."""
    db.query(OrderLog).filter(OrderLog.session_id == session_id).delete()
    db.commit()
    return HTMLResponse("")


@router.delete("/history")
async def delete_all_history(db: Session = Depends(get_db)):
    """Delete all order history."""
    db.query(OrderLog).delete()
    db.commit()
    return HTMLResponse(
        '<div class="empty-state"><p>No order history yet. Complete an order to see it here.</p></div>'
    )


@router.get("/history")
async def order_history(request: Request, db: Session = Depends(get_db)):
    """View past orders."""
    logs = (
        db.query(OrderLog)
        .order_by(OrderLog.created_at.desc())
        .limit(500)
        .all()
    )

    # Group by session_id, preserving order
    sessions = []
    seen = set()
    for log in logs:
        if log.session_id not in seen:
            seen.add(log.session_id)
            session_logs = [l for l in logs if l.session_id == log.session_id]
            sessions.append((log.session_id, session_logs))

    return templates.TemplateResponse(
        "history.html",
        {
            "request": request,
            "sessions": sessions,
            "instacart_store": settings.instacart_store.lower(),
        },
    )
