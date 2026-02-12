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
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request
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
class ProposalItem:
    index: int
    alexa_text: str
    alexa_item_id: str = ""
    alexa_list_id: str = ""
    grocery_item_id: int | None = None
    grocery_item_name: str | None = None
    product_name: str | None = None
    brand: str | None = None
    price: str | None = None
    image_url: str | None = None
    status: str = "Pending"  # Matched, Substituted, New item, Error
    status_class: str = "new"  # matched, substituted, new, error
    in_stock: bool = True


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
    """Fetch Alexa list and begin the search/match process."""
    session_id = str(uuid.uuid4())
    session = OrderSession(session_id=session_id)
    _sessions[session_id] = session

    try:
        from alexacart.alexa.client import AlexaClient

        client = AlexaClient()
        items = await client.get_items()
        await client.close()

        if not items:
            return HTMLResponse(
                '<div class="status-message status-warning">'
                "No items found on your Alexa Grocery List. "
                "Add some items via Alexa and try again.</div>"
            )

        session.total_items = len(items)
        session.status = "searching"

        for i, item in enumerate(items):
            session.proposals.append(
                ProposalItem(
                    index=i,
                    alexa_text=item.text,
                    alexa_item_id=item.item_id,
                    alexa_list_id=item.list_id,
                )
            )

        # Start background search
        asyncio.create_task(_search_items(session))

        return HTMLResponse(
            f'<div id="search-progress" '
            f'hx-ext="sse" '
            f'sse-connect="/order/progress/{session_id}" '
            f'sse-swap="progress" '
            f'hx-swap="innerHTML">'
            f'<div class="progress-container">'
            f'<div class="progress-bar"><div class="progress-fill" style="width: 0%"></div></div>'
            f'<p class="progress-text">Searching Instacart for {len(items)} items...</p>'
            f"</div></div>"
        )

    except RuntimeError as e:
        if "cookies" in str(e).lower():
            return HTMLResponse(
                '<div class="status-message status-error">'
                "<strong>Login Required</strong><br>"
                "Alexa cookies not found or expired. "
                "Run <code>python -m alexacart.alexa.auth login</code> to authenticate."
                "</div>"
            )
        return HTMLResponse(
            f'<div class="status-message status-error">'
            f"Error: {e}</div>"
        )
    except Exception as e:
        logger.exception("Error starting order")
        return HTMLResponse(
            f'<div class="status-message status-error">'
            f"Error fetching Alexa list: {e}</div>"
        )


async def _search_items(session: OrderSession):
    """Background task: search Instacart for each item."""
    from alexacart.instacart.agent import InstacartAgent

    db = SessionLocal()
    agent = InstacartAgent()

    try:
        for proposal in session.proposals:
            try:
                match = find_match(db, proposal.alexa_text)
                proposal.grocery_item_id = match.grocery_item_id
                proposal.grocery_item_name = match.grocery_item_name

                if match.is_known and match.preferred_products:
                    # Try preferred products in rank order
                    found = False
                    for pref in match.preferred_products:
                        result = await agent.search_specific_product(pref.product_name)
                        if result and result.in_stock:
                            proposal.product_name = result.product_name
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
                            proposal.brand = best.brand
                            proposal.price = best.price
                            proposal.image_url = best.image_url
                            proposal.status = "Substituted (usual out of stock)"
                            proposal.status_class = "substituted"
                            proposal.in_stock = best.in_stock
                        else:
                            proposal.status = "No results"
                            proposal.status_class = "error"
                else:
                    # Unknown item — general search
                    results = await agent.search_product(proposal.alexa_text)
                    if results:
                        best = results[0]
                        proposal.product_name = best.product_name
                        proposal.brand = best.brand
                        proposal.price = best.price
                        proposal.image_url = best.image_url
                        proposal.status = "New item"
                        proposal.status_class = "new"
                        proposal.in_stock = best.in_stock
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
        await agent.close()
        db.close()


@router.get("/progress/{session_id}")
async def progress_stream(session_id: str):
    """SSE stream for search progress updates."""

    async def generate():
        session = _sessions.get(session_id)
        if not session:
            yield {"event": "progress", "data": '<div class="status-message status-error">Session not found</div>'}
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

            # Add to Instacart cart
            added = await instacart_agent.add_to_cart(product_name)

            if added and alexa_item_id:
                # Check off Alexa list
                alexa_item = AlexaListItem(
                    item_id=alexa_item_id,
                    text=alexa_text,
                    list_id=proposal.alexa_list_id if proposal else "",
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
                _learn_from_result(
                    db,
                    alexa_text=alexa_text,
                    grocery_item_id=int(grocery_item_id) if grocery_item_id else None,
                    final_product=product_name,
                    brand=data.get("brand"),
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

    # Build results HTML
    html = '<div class="results-summary"><h3>Order Results</h3>'
    success_count = sum(1 for r in results if r["success"])
    html += f'<p>{success_count} of {len(results)} items added to cart</p>'

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
        '</div>'
        '<div style="margin-top: 1rem;">'
        '<a href="/order/" class="btn btn-primary">Start New Order</a>'
        "</div>"
    )

    # Clean up session
    del _sessions[session_id]

    return HTMLResponse(html)


def _learn_from_result(
    db: Session,
    alexa_text: str,
    grocery_item_id: int | None,
    final_product: str,
    brand: str | None,
    was_corrected: bool,
):
    """Learn from the user's choices to improve future proposals."""
    if grocery_item_id:
        # Known item
        if was_corrected:
            # User changed the proposal — make their choice the top preference
            make_product_top_choice(db, grocery_item_id, final_product, brand=brand)
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
            if not existing:
                add_preferred_product(db, grocery_item_id, final_product, brand=brand)
    else:
        # Unknown item — create new grocery item + alias + preferred product
        item = create_grocery_item(db, alexa_text)
        add_preferred_product(db, item.id, final_product, brand=brand, rank=1)


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
