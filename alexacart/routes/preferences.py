import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session, selectinload

from alexacart.app import templates
from alexacart.db import get_db
from alexacart.matching.matcher import (
    add_alias,
    add_preferred_product,
    create_grocery_item,
    promote_product,
)
from alexacart.models import Alias, GroceryItem, PreferredProduct

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/preferences", tags=["preferences"])


def _render_item(request: Request, item: GroceryItem, **extra) -> str:
    """Render a single preference item card partial."""
    return templates.get_template("partials/preference_item.html").render(
        {"request": request, "item": item, **extra}
    )


def _render_all_items(request: Request, db: Session) -> str:
    """Render all preference item cards (used after create/merge)."""
    items = (
        db.query(GroceryItem)
        .options(selectinload(GroceryItem.aliases), selectinload(GroceryItem.preferred_products))
        .order_by(GroceryItem.name)
        .all()
    )
    return "\n".join(_render_item(request, item) for item in items)


@router.get("/")
async def preferences_page(request: Request, db: Session = Depends(get_db)):
    items = (
        db.query(GroceryItem)
        .options(selectinload(GroceryItem.aliases), selectinload(GroceryItem.preferred_products))
        .order_by(GroceryItem.name)
        .all()
    )
    return templates.TemplateResponse(
        "preferences.html", {"request": request, "items": items}
    )


@router.post("/items", response_class=HTMLResponse)
async def create_item(request: Request, name: str = Form(...), db: Session = Depends(get_db)):
    """Create a new grocery item (with its name as initial alias)."""
    create_grocery_item(db, name)
    db.commit()
    return HTMLResponse(_render_all_items(request, db))


@router.get("/items/{item_id}/fragment", response_class=HTMLResponse)
async def item_fragment(request: Request, item_id: int, db: Session = Depends(get_db)):
    """Return a single preference item card (used by htmx after updates)."""
    item = db.get(GroceryItem, item_id)
    if not item:
        return HTMLResponse("")
    return HTMLResponse(_render_item(request, item))


@router.delete("/items/{item_id}", response_class=HTMLResponse)
async def delete_item(item_id: int, db: Session = Depends(get_db)):
    """Delete a grocery item and all its aliases/products."""
    item = db.get(GroceryItem, item_id)
    if item:
        db.delete(item)
        db.commit()
    return HTMLResponse("")


@router.post("/items/{item_id}/aliases", response_class=HTMLResponse)
async def add_item_alias(
    request: Request,
    item_id: int,
    alias: str = Form(...),
    db: Session = Depends(get_db),
):
    """Add an alias to a grocery item."""
    try:
        add_alias(db, item_id, alias)
        db.commit()
    except ValueError:
        pass  # Alias already exists elsewhere
    item = db.get(GroceryItem, item_id)
    return HTMLResponse(_render_item(request, item))


@router.delete("/aliases/{alias_id}", response_class=HTMLResponse)
async def delete_alias(request: Request, alias_id: int, db: Session = Depends(get_db)):
    """Delete an alias."""
    alias = db.get(Alias, alias_id)
    if alias:
        grocery_item_id = alias.grocery_item_id
        db.delete(alias)
        db.commit()
        item = db.get(GroceryItem, grocery_item_id)
        if item:
            return HTMLResponse(_render_item(request, item))
    return HTMLResponse("")


@router.post("/items/{item_id}/products", response_class=HTMLResponse)
async def add_item_product(
    request: Request,
    item_id: int,
    product_name: str = Form(...),
    product_url: str = Form(""),
    brand: str = Form(""),
    db: Session = Depends(get_db),
):
    """Add a preferred product to a grocery item."""
    add_preferred_product(db, item_id, product_name, product_url=product_url or None, brand=brand or None)
    db.commit()
    item = db.get(GroceryItem, item_id)
    return HTMLResponse(_render_item(request, item))


@router.post("/items/{item_id}/products/from-url", response_class=HTMLResponse)
async def add_product_from_url(
    request: Request,
    item_id: int,
    url: str = Form(...),
    db: Session = Depends(get_db),
):
    """Add a preferred product by fetching details from an Instacart URL."""
    from alexacart.instacart.auth import ensure_valid_session
    from alexacart.instacart.client import InstacartClient

    item = db.get(GroceryItem, item_id)
    if not item:
        return HTMLResponse('<div class="status-message status-error">Item not found</div>', status_code=404)

    client = InstacartClient(await ensure_valid_session())
    try:
        await client.init_session()
        result = await client.get_product_details(url)
        if not result:
            return HTMLResponse(
                _render_item(request, item, url_error="Could not find a product at that URL.")
            )
        add_preferred_product(
            db, item_id, result.product_name,
            product_url=result.product_url or url,
            brand=result.brand,
            image_url=result.image_url,
            size=result.size,
        )
        db.commit()
        item = db.get(GroceryItem, item_id)
        return HTMLResponse(_render_item(request, item))
    except Exception as e:
        logger.error("URL fetch failed for preferences: %s", e)
        return HTMLResponse(
            _render_item(request, item, url_error=f"Error fetching URL: {e}")
        )
    finally:
        await client.close()


@router.post("/products/{product_id}/move-up", response_class=HTMLResponse)
async def move_product_up(
    request: Request, product_id: int, db: Session = Depends(get_db)
):
    """Move a preferred product up one rank."""
    product = db.get(PreferredProduct, product_id)
    if product:
        promote_product(db, product_id)
        db.commit()
        item = db.get(GroceryItem, product.grocery_item_id)
        return HTMLResponse(_render_item(request, item))
    return HTMLResponse("")


@router.delete("/products/{product_id}", response_class=HTMLResponse)
async def delete_product(request: Request, product_id: int, db: Session = Depends(get_db)):
    """Delete a preferred product."""
    product = db.get(PreferredProduct, product_id)
    if product:
        grocery_item_id = product.grocery_item_id
        db.delete(product)
        db.commit()
        # Re-number remaining products
        remaining = (
            db.query(PreferredProduct)
            .filter(PreferredProduct.grocery_item_id == grocery_item_id)
            .order_by(PreferredProduct.rank)
            .all()
        )
        for i, p in enumerate(remaining, 1):
            p.rank = i
        db.commit()
        item = db.get(GroceryItem, grocery_item_id)
        if item:
            return HTMLResponse(_render_item(request, item))
    return HTMLResponse("")


@router.post("/items/merge", response_class=HTMLResponse)
async def merge_items(
    request: Request,
    source_id: int = Form(...),
    target_id: int = Form(...),
    db: Session = Depends(get_db),
):
    """Merge source grocery item into target: move aliases and products, delete source."""
    if source_id == target_id:
        return HTMLResponse("Cannot merge an item with itself", status_code=400)

    source = db.get(GroceryItem, source_id)
    target = db.get(GroceryItem, target_id)

    if not source or not target:
        return HTMLResponse("Item not found", status_code=404)

    # Move aliases (skip duplicates)
    for alias in source.aliases:
        existing = db.query(Alias).filter(Alias.alias == alias.alias, Alias.grocery_item_id == target_id).first()
        if not existing:
            alias.grocery_item_id = target_id
        else:
            db.delete(alias)

    # Get max rank on target
    max_rank = (
        db.query(PreferredProduct.rank)
        .filter(PreferredProduct.grocery_item_id == target_id)
        .order_by(PreferredProduct.rank.desc())
        .first()
    )
    next_rank = (max_rank[0] + 1) if max_rank else 1

    # Move products
    for product in source.preferred_products:
        product.grocery_item_id = target_id
        product.rank = next_rank
        next_rank += 1

    db.delete(source)
    db.commit()

    return HTMLResponse(_render_all_items(request, db))
