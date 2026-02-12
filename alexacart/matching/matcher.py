"""
Matching logic: resolve Alexa list text to known grocery items via aliases,
then propose preferred products in rank order.
"""

import logging
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from alexacart.models import Alias, GroceryItem, PreferredProduct

logger = logging.getLogger(__name__)


@dataclass
class MatchResult:
    alexa_text: str
    grocery_item_id: int | None = None
    grocery_item_name: str | None = None
    preferred_products: list[PreferredProduct] = field(default_factory=list)
    is_known: bool = False

    @property
    def status(self) -> str:
        if self.is_known and self.preferred_products:
            return "known"
        elif self.is_known:
            return "known_no_products"
        return "unknown"


def normalize_text(text: str) -> str:
    """Normalize Alexa list item text for matching."""
    return text.strip().lower()


def find_match(db: Session, alexa_text: str) -> MatchResult:
    """
    Look up an Alexa list item in the preference database.

    1. Normalize the text
    2. Search aliases for an exact match
    3. If found, return the grocery item and its preferred products (ranked)
    """
    normalized = normalize_text(alexa_text)

    alias = db.query(Alias).filter(Alias.alias == normalized).first()

    if alias:
        item = alias.grocery_item
        products = (
            db.query(PreferredProduct)
            .filter(PreferredProduct.grocery_item_id == item.id)
            .order_by(PreferredProduct.rank)
            .all()
        )
        return MatchResult(
            alexa_text=alexa_text,
            grocery_item_id=item.id,
            grocery_item_name=item.name,
            preferred_products=products,
            is_known=True,
        )

    return MatchResult(alexa_text=alexa_text)


def create_grocery_item(db: Session, name: str) -> GroceryItem:
    """Create a new grocery item with its name as the initial alias."""
    normalized = normalize_text(name)

    existing = db.query(GroceryItem).filter(GroceryItem.name == normalized).first()
    if existing:
        return existing

    item = GroceryItem(name=normalized)
    db.add(item)
    db.flush()

    alias = Alias(grocery_item_id=item.id, alias=normalized)
    db.add(alias)
    db.commit()
    db.refresh(item)

    logger.info("Created grocery item '%s' with id=%d", normalized, item.id)
    return item


def add_alias(db: Session, grocery_item_id: int, alias_text: str) -> Alias:
    """Add an alias for a grocery item."""
    normalized = normalize_text(alias_text)

    existing = db.query(Alias).filter(Alias.alias == normalized).first()
    if existing:
        if existing.grocery_item_id == grocery_item_id:
            return existing
        raise ValueError(
            f"Alias '{normalized}' already exists for a different item "
            f"(item id={existing.grocery_item_id})"
        )

    alias = Alias(grocery_item_id=grocery_item_id, alias=normalized)
    db.add(alias)
    db.commit()
    db.refresh(alias)
    return alias


def add_preferred_product(
    db: Session,
    grocery_item_id: int,
    product_name: str,
    brand: str | None = None,
    image_url: str | None = None,
    rank: int | None = None,
) -> PreferredProduct:
    """
    Add a preferred product for a grocery item.
    If rank is None, append at the end.
    If rank is specified, shift existing products down.
    """
    if rank is None:
        max_rank = (
            db.query(PreferredProduct.rank)
            .filter(PreferredProduct.grocery_item_id == grocery_item_id)
            .order_by(PreferredProduct.rank.desc())
            .first()
        )
        rank = (max_rank[0] + 1) if max_rank else 1

    # Shift existing products at this rank or below
    existing_at_rank = (
        db.query(PreferredProduct)
        .filter(
            PreferredProduct.grocery_item_id == grocery_item_id,
            PreferredProduct.rank >= rank,
        )
        .order_by(PreferredProduct.rank.desc())
        .all()
    )
    for p in existing_at_rank:
        p.rank += 1

    product = PreferredProduct(
        grocery_item_id=grocery_item_id,
        rank=rank,
        product_name=product_name,
        brand=brand,
        image_url=image_url,
    )
    db.add(product)
    db.commit()
    db.refresh(product)
    return product


def promote_product(db: Session, product_id: int) -> None:
    """Move a preferred product up one rank (lower number = higher priority)."""
    product = db.query(PreferredProduct).get(product_id)
    if not product or product.rank <= 1:
        return

    above = (
        db.query(PreferredProduct)
        .filter(
            PreferredProduct.grocery_item_id == product.grocery_item_id,
            PreferredProduct.rank == product.rank - 1,
        )
        .first()
    )

    if above:
        above.rank, product.rank = product.rank, above.rank
    else:
        product.rank -= 1

    db.commit()


def make_product_top_choice(db: Session, grocery_item_id: int, product_name: str, brand: str | None = None) -> PreferredProduct:
    """
    Make a product the #1 choice for a grocery item.
    If it already exists, move it to rank 1. Otherwise, add it at rank 1.
    Used when the user corrects a proposal during order review.
    """
    existing = (
        db.query(PreferredProduct)
        .filter(
            PreferredProduct.grocery_item_id == grocery_item_id,
            PreferredProduct.product_name == product_name,
        )
        .first()
    )

    if existing:
        if existing.rank == 1:
            return existing
        # Move to rank 1: shift everything above it up
        others = (
            db.query(PreferredProduct)
            .filter(
                PreferredProduct.grocery_item_id == grocery_item_id,
                PreferredProduct.rank < existing.rank,
            )
            .order_by(PreferredProduct.rank.desc())
            .all()
        )
        for p in others:
            p.rank += 1
        existing.rank = 1
        db.commit()
        db.refresh(existing)
        return existing
    else:
        return add_preferred_product(
            db, grocery_item_id, product_name, brand=brand, rank=1
        )
