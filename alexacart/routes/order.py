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
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse
from sse_starlette.sse import EventSourceResponse
from sqlalchemy.orm import Session

from alexacart.app import templates
from alexacart.db import SessionLocal, get_db
from alexacart.matching.matcher import (
    add_preferred_product,
    create_grocery_item,
    find_match,
    make_product_top_choice,
)
from alexacart.models import OrderLog

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


@dataclass
class OrderSession:
    session_id: str
    proposals: list[ProposalItem] = field(default_factory=list)
    total_items: int = 0
    searched_count: int = 0
    status: str = "starting"  # starting, searching, ready, committing, done
    error: str | None = None


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
        f'hx-ext="sse" '
        f'sse-connect="/order/progress/{session_id}" '
        f'sse-swap="progress" '
        f'hx-swap="innerHTML">'
        f'<div class="progress-container">'
        f'<div class="progress-bar"><div class="progress-fill" style="width: 0%"></div></div>'
        f'<p class="progress-text">Starting up...</p>'
        f"</div></div>"
    )


async def _run_order(session: OrderSession):
    """Background task: check logins, fetch Alexa list, then search Instacart."""
    from alexacart.alexa.auth import save_cookies
    from alexacart.alexa.client import AlexaClient
    from alexacart.instacart.agent import InstacartAgent

    agent = InstacartAgent()

    try:
        # Step 1: Ensure logged into both Amazon and Instacart
        session.status = "logging_in"

        amazon_ok = await agent.ensure_amazon_logged_in()
        if not amazon_ok:
            session.error = "Timed out waiting for Amazon login. Please try again."
            session.status = "error"
            return

        instacart_ok = await agent.ensure_logged_in()
        if not instacart_ok:
            session.error = "Timed out waiting for Instacart login. Please try again."
            session.status = "error"
            return

        # Step 2: Extract fresh Amazon cookies from the browser and save them
        session.status = "fetching_list"
        cookie_data = await agent.get_amazon_cookies()
        if not cookie_data.get("cookies"):
            session.error = "Could not extract Amazon cookies from browser. Please try again."
            session.status = "error"
            return
        save_cookies(cookie_data)

        # Step 3: Fetch Alexa shopping list using fresh cookies
        alexa_client = AlexaClient()
        try:
            items = await alexa_client.get_items()
        except Exception as e:
            await alexa_client.close()
            error_str = str(e)
            if "401" in error_str:
                session.error = "Amazon session expired. Please restart the app and log in again."
            elif "503" in error_str or "502" in error_str:
                session.error = "Amazon servers are temporarily unavailable. Please try again in a minute."
            else:
                session.error = f"Failed to fetch Alexa list: {error_str}"
            session.status = "error"
            return
        finally:
            await alexa_client.close()

        if not items:
            session.error = "No items found on your Alexa Grocery List. Add some items via Alexa and try again."
            session.status = "error"
            return

        session.total_items = len(items)
        for i, item in enumerate(items):
            session.proposals.append(
                ProposalItem(
                    index=i,
                    alexa_text=item.text,
                    alexa_item_id=item.item_id,
                    alexa_list_id=item.list_id,
                    alexa_item_version=item.version,
                )
            )

        # Step 4: Search Instacart for each item
        await _search_items(session, agent)

    except Exception as e:
        logger.exception("Order flow failed")
        session.error = str(e)
        session.status = "error"
    finally:
        await agent.close()


async def _search_items(session: OrderSession, agent):
    """Search Instacart for each item in the session."""
    db = SessionLocal()

    try:
        session.status = "searching"

        for proposal in session.proposals:
            try:
                match = find_match(db, proposal.alexa_text)
                proposal.grocery_item_id = match.grocery_item_id
                proposal.grocery_item_name = match.grocery_item_name

                if match.is_known and match.preferred_products:
                    # Try preferred products in rank order
                    found = False
                    for pref in match.preferred_products:
                        # Use direct URL if available, fall back to search
                        if pref.product_url:
                            result = await agent.check_product_by_url(pref.product_url)
                        else:
                            results = await agent.search_product(pref.product_name)
                            result = results[0] if results else None
                        if result and result.in_stock:
                            proposal.product_name = result.product_name
                            proposal.product_url = result.product_url or pref.product_url
                            proposal.brand = result.brand or pref.brand
                            proposal.price = result.price
                            proposal.image_url = result.image_url or pref.image_url
                            proposal.status = f"Matched (choice #{pref.rank})"
                            proposal.status_class = "matched"
                            proposal.in_stock = True
                            found = True

                            # Update last_seen_in_stock
                            pref.last_seen_in_stock = datetime.utcnow()
                            db.commit()
                            break

                    if not found:
                        # All preferred products out of stock, do general search
                        results = await agent.search_product(proposal.alexa_text)
                        if results:
                            best = results[0]
                            proposal.product_name = best.product_name
                            proposal.product_url = best.product_url
                            proposal.brand = best.brand
                            proposal.price = best.price
                            proposal.image_url = best.image_url
                            proposal.status = "Substituted (usual out of stock)"
                            proposal.status_class = "substituted"
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
                else:
                    # Unknown item — general search
                    results = await agent.search_product(proposal.alexa_text)
                    if results:
                        best = results[0]
                        proposal.product_name = best.product_name
                        proposal.product_url = best.product_url
                        proposal.brand = best.brand
                        proposal.price = best.price
                        proposal.image_url = best.image_url
                        proposal.status = "New item"
                        proposal.status_class = "new"
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

            except Exception as e:
                logger.error("Search failed for '%s': %s", proposal.alexa_text, e)
                proposal.status = f"Error: {e}"
                proposal.status_class = "error"

            session.searched_count += 1

        session.status = "ready"

    except Exception as e:
        logger.exception("Search task failed")
        session.error = str(e)
        session.status = "ready"
    finally:
        db.close()


@router.get("/progress/{session_id}")
async def progress_stream(session_id: str):
    """SSE stream for search progress updates."""

    async def generate():
        session = _sessions.get(session_id)
        if not session:
            yield {"event": "progress", "data": '<div class="status-message status-error">Session not found</div>'}
            return

        while session.status == "logging_in":
            yield {
                "event": "progress",
                "data": (
                    '<div class="progress-container">'
                    '<p class="progress-text">'
                    "Checking logins... "
                    "If a browser window opened, please log into Amazon and Instacart there."
                    "</p></div>"
                ),
            }
            await asyncio.sleep(3)

        if session.status == "error":
            yield {
                "event": "progress",
                "data": f'<div class="status-message status-error">{session.error}</div>',
            }
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
                "data": f'<div class="status-message status-error">{session.error}</div>',
            }
            return

        while session.status == "searching":
            pct = (
                int(session.searched_count / session.total_items * 100)
                if session.total_items > 0
                else 0
            )
            current_item = ""
            if session.searched_count < len(session.proposals):
                current_item = session.proposals[session.searched_count].alexa_text

            html = (
                f'<div class="progress-container">'
                f'<div class="progress-bar">'
                f'<div class="progress-fill" style="width: {pct}%"></div>'
                f"</div>"
                f'<p class="progress-text">'
                f"Searched {session.searched_count} of {session.total_items} items ({pct}%)"
                f"</p>"
            )
            if current_item:
                html += f'<p class="progress-text">Searching: {current_item}...</p>'
            html += "</div>"

            yield {"event": "progress", "data": html}
            await asyncio.sleep(2)

        # Search complete — redirect to review page
        if session.error:
            yield {
                "event": "progress",
                "data": (
                    f'<div class="status-message status-error">Search error: {session.error}</div>'
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

    agent = InstacartAgent()
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
            f'<div class="status-message status-error">Search failed: {e}</div>'
        )
    finally:
        await agent.close()


@router.post("/fetch-url")
async def fetch_product_url(request: Request, url: str = Form(...), index: int = Form(0)):
    """Fetch product details from a custom Instacart URL."""
    from alexacart.instacart.agent import InstacartAgent

    agent = InstacartAgent()
    try:
        result = await agent.check_product_by_url(url)
        if result:
            return HTMLResponse(
                f'<div class="status-message status-success" style="margin-top:0.5rem">'
                f'Found: <strong>{result.product_name}</strong>'
                f'{" — " + result.brand if result.brand else ""}'
                f'{" — " + result.price if result.price else ""}'
                f'</div>'
                f'<script>'
                f'selectProduct({index}, {json.dumps(result.product_name)}, '
                f'{json.dumps(result.price or "")}, {json.dumps(result.image_url or "")}, '
                f'{json.dumps(url)})'
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
            f'Error: {e}</div>'
        )
    finally:
        await agent.close()


@router.post("/commit")
async def commit_order(request: Request):
    """Add accepted items to Instacart cart and check off Alexa list."""
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
        if key.startswith("items["):
            # Parse items[0][field_name]
            parts = key.replace("items[", "").replace("]", " ").split()
            if len(parts) == 2:
                idx, field = int(parts[0]), parts[1][1:]  # Remove leading [
                if idx not in items_data:
                    items_data[idx] = {}
                items_data[idx][field] = value

    from alexacart.alexa.client import AlexaClient, AlexaListItem
    from alexacart.instacart.agent import InstacartAgent

    alexa_client = AlexaClient()
    instacart_agent = InstacartAgent()
    db = SessionLocal()

    results = []

    try:
        for idx, data in sorted(items_data.items()):
            product_name = data.get("product_name", "")
            alexa_text = data.get("alexa_text", "")
            grocery_item_id = data.get("grocery_item_id", "")
            alexa_item_id = data.get("alexa_item_id", "")

            if not product_name:
                results.append({"text": alexa_text, "success": False, "reason": "No product selected"})
                continue

            # Find the original proposal
            proposal = None
            for p in session.proposals:
                if p.index == idx:
                    proposal = p
                    break

            # Add to Instacart cart — use URL if available
            product_url = data.get("product_url", "")
            added = await instacart_agent.add_to_cart(product_name, product_url=product_url or None)

            if added and alexa_item_id:
                # Check off Alexa list
                alexa_item = AlexaListItem(
                    item_id=alexa_item_id,
                    text=alexa_text,
                    list_id=proposal.alexa_list_id if proposal else "",
                    version=proposal.alexa_item_version if proposal else 1,
                )
                await alexa_client.mark_complete(alexa_item)

            # Determine if this was a correction
            was_corrected = False
            if proposal and proposal.product_name and proposal.product_name != product_name:
                was_corrected = True

            # Log to order_log
            log_entry = OrderLog(
                session_id=session_id,
                alexa_text=alexa_text,
                matched_grocery_item_id=int(grocery_item_id) if grocery_item_id else None,
                proposed_product=proposal.product_name if proposal else None,
                final_product=product_name,
                was_corrected=was_corrected,
                added_to_cart=added,
            )
            db.add(log_entry)

            # Learn from corrections
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

            results.append({
                "text": alexa_text,
                "product": product_name,
                "success": added,
                "reason": "" if added else "Failed to add to cart",
            })

        db.commit()

    except Exception as e:
        logger.exception("Error during commit")
        db.rollback()
        return HTMLResponse(
            f'<div class="status-message status-error">Error: {e}</div>'
        )
    finally:
        await alexa_client.close()
        await instacart_agent.close()
        db.close()

    # Build results HTML — hide the form and show results
    html = (
        '<script>document.getElementById("review-form").style.display="none";</script>'
        '<div class="results-summary card"><h3>Order Complete</h3>'
    )
    success_count = sum(1 for r in results if r["success"])
    html += f'<p class="muted" style="margin-bottom:1rem">{success_count} of {len(results)} items added to cart</p>'

    for r in results:
        icon = "&#10003;" if r["success"] else "&#10007;"
        cls = "status-success" if r["success"] else "status-error"
        html += (
            f'<div class="result-item">'
            f'<span class="result-icon {cls}">{icon}</span>'
            f'<span>{r["text"]}'
        )
        if r.get("product"):
            html += f' &rarr; {r["product"]}'
        if r.get("reason"):
            html += f' ({r["reason"]})'
        html += "</span></div>"

    html += (
        '<div style="margin-top: 1.5rem;">'
        '<a href="/order/" class="btn btn-primary">Start New Order</a>'
        "</div></div>"
    )

    # Clean up session
    del _sessions[session_id]

    return HTMLResponse(html)


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
            # User accepted — ensure product is in preferences
            from alexacart.models import PreferredProduct

            existing = (
                db.query(PreferredProduct)
                .filter(
                    PreferredProduct.grocery_item_id == grocery_item_id,
                    PreferredProduct.product_name == final_product,
                )
                .first()
            )
            if existing:
                # Update URL/image if we now have one
                if product_url and not existing.product_url:
                    existing.product_url = product_url
                if image_url and not existing.image_url:
                    existing.image_url = image_url
                db.commit()
            else:
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
        {"request": request, "sessions": sessions},
    )
