from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from alexacart.app import templates
from alexacart.db import get_db
from alexacart.matching.matcher import (
    add_alias,
    add_preferred_product,
    create_grocery_item,
    promote_product,
)
from alexacart.models import Alias, GroceryItem, PreferredProduct

router = APIRouter(prefix="/preferences", tags=["preferences"])


@router.get("/")
async def preferences_page(request: Request, db: Session = Depends(get_db)):
    items = db.query(GroceryItem).order_by(GroceryItem.name).all()
    return templates.TemplateResponse(
        "preferences.html", {"request": request, "items": items}
    )


@router.post("/items", response_class=HTMLResponse)
async def create_item(request: Request, name: str = Form(...), db: Session = Depends(get_db)):
    """Create a new grocery item (with its name as initial alias)."""
    create_grocery_item(db, name)
    items = db.query(GroceryItem).order_by(GroceryItem.name).all()
    # Return the full items list HTML for htmx swap
    parts = []
    for item in items:
        parts.append(
            templates.get_template("partials/preference_item.html").render(
                {"request": request, "item": item}
            )
        )
    return HTMLResponse("\n".join(parts))


@router.get("/items/{item_id}/fragment", response_class=HTMLResponse)
async def item_fragment(request: Request, item_id: int, db: Session = Depends(get_db)):
    """Return a single preference item card (used by htmx after updates)."""
    item = db.query(GroceryItem).get(item_id)
    if not item:
        return HTMLResponse("")
    return HTMLResponse(
        templates.get_template("partials/preference_item.html").render(
            {"request": request, "item": item}
        )
    )


@router.delete("/items/{item_id}", response_class=HTMLResponse)
async def delete_item(item_id: int, db: Session = Depends(get_db)):
    """Delete a grocery item and all its aliases/products."""
    item = db.query(GroceryItem).get(item_id)
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
    except ValueError:
        pass  # Alias already exists elsewhere
    item = db.query(GroceryItem).get(item_id)
    return HTMLResponse(
        templates.get_template("partials/preference_item.html").render(
            {"request": request, "item": item}
        )
    )


@router.delete("/aliases/{alias_id}", response_class=HTMLResponse)
async def delete_alias(alias_id: int, db: Session = Depends(get_db)):
    """Delete an alias."""
    alias = db.query(Alias).get(alias_id)
    if alias:
        db.delete(alias)
        db.commit()
    return HTMLResponse("")


@router.post("/items/{item_id}/products", response_class=HTMLResponse)
async def add_item_product(
    request: Request,
    item_id: int,
    product_name: str = Form(...),
    brand: str = Form(""),
    db: Session = Depends(get_db),
):
    """Add a preferred product to a grocery item."""
    add_preferred_product(db, item_id, product_name, brand=brand or None)
    item = db.query(GroceryItem).get(item_id)
    return HTMLResponse(
        templates.get_template("partials/preference_item.html").render(
            {"request": request, "item": item}
        )
    )


@router.post("/products/{product_id}/move-up", response_class=HTMLResponse)
async def move_product_up(
    request: Request, product_id: int, db: Session = Depends(get_db)
):
    """Move a preferred product up one rank."""
    product = db.query(PreferredProduct).get(product_id)
    if product:
        promote_product(db, product_id)
        item = db.query(GroceryItem).get(product.grocery_item_id)
        return HTMLResponse(
            templates.get_template("partials/preference_item.html").render(
                {"request": request, "item": item}
            )
        )
    return HTMLResponse("")


@router.delete("/products/{product_id}", response_class=HTMLResponse)
async def delete_product(product_id: int, db: Session = Depends(get_db)):
    """Delete a preferred product."""
    product = db.query(PreferredProduct).get(product_id)
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

    source = db.query(GroceryItem).get(source_id)
    target = db.query(GroceryItem).get(target_id)

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

    items = db.query(GroceryItem).order_by(GroceryItem.name).all()
    parts = []
    for item in items:
        parts.append(
            templates.get_template("partials/preference_item.html").render(
                {"request": request, "item": item}
            )
        )
    return HTMLResponse("\n".join(parts))
