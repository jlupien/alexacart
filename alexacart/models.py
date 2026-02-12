from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class GroceryItem(Base):
    __tablename__ = "grocery_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    aliases: Mapped[list["Alias"]] = relationship(
        back_populates="grocery_item", cascade="all, delete-orphan"
    )
    preferred_products: Mapped[list["PreferredProduct"]] = relationship(
        back_populates="grocery_item", cascade="all, delete-orphan", order_by="PreferredProduct.rank"
    )


class Alias(Base):
    __tablename__ = "aliases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    grocery_item_id: Mapped[int] = mapped_column(Integer, ForeignKey("grocery_items.id"), nullable=False)
    alias: Mapped[str] = mapped_column(String, unique=True, nullable=False)

    grocery_item: Mapped["GroceryItem"] = relationship(back_populates="aliases")


class PreferredProduct(Base):
    __tablename__ = "preferred_products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    grocery_item_id: Mapped[int] = mapped_column(Integer, ForeignKey("grocery_items.id"), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    product_name: Mapped[str] = mapped_column(String, nullable=False)
    product_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    brand: Mapped[str | None] = mapped_column(String, nullable=True)
    image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_seen_in_stock: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    grocery_item: Mapped["GroceryItem"] = relationship(back_populates="preferred_products")

    __table_args__ = (UniqueConstraint("grocery_item_id", "rank"),)


class OrderLog(Base):
    __tablename__ = "order_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String, nullable=False)
    alexa_text: Mapped[str] = mapped_column(Text, nullable=False)
    matched_grocery_item_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("grocery_items.id"), nullable=True
    )
    proposed_product: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_product: Mapped[str | None] = mapped_column(Text, nullable=True)
    was_corrected: Mapped[bool] = mapped_column(Boolean, default=False)
    added_to_cart: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
