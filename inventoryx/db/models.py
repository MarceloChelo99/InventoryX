"""
SQLAlchemy ORM models — the design doc's persistence schema.

Six tables (design doc, "Data model"):

  Company        - tenant root; single row in local mode, but modeled so
                   multi-tenancy is a future migration, not a rewrite. Holds
                   the company-level lead-time / safety-stock defaults that the
                   cascade falls back to.
  Supplier       - a vendor with a default lead time.
  Sku            - a stockable item. lead_time_days / safety_stock are nullable
                   so a new SKU isn't a blocker; they cascade
                   SKU -> supplier -> company at score time.
  SaleEvent      - append-only realized sales (drives the forecaster).
  StockSnapshot  - append-only inventory position; latest row per SKU is
                   "current". Carries a nullable warehouse_id now so
                   multi-warehouse is a later migration (design doc open Q1).
  PurchaseOrder  - placed orders; received rows yield realized lead times.

The ORM rows are the storage shape. The read-side adapter (``source.py``)
converts them into the plain dataclasses the scoring services already consume,
so nothing downstream of ``InventoryDataSource`` knows the DB exists.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import Boolean, Date, Float, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Company(Base):
    __tablename__ = "company"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    # Bottom of the cascade — used when neither SKU nor supplier specify.
    default_lead_time_days: Mapped[float] = mapped_column(Float, default=15.0)
    default_safety_stock: Mapped[float] = mapped_column(Float, default=0.0)

    suppliers: Mapped[list["Supplier"]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )
    skus: Mapped[list["Sku"]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )


class Supplier(Base):
    __tablename__ = "supplier"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("company.id"))
    name: Mapped[str] = mapped_column(String(200))
    default_lead_time_days: Mapped[float | None] = mapped_column(Float, nullable=True)

    company: Mapped["Company"] = relationship(back_populates="suppliers")
    skus: Mapped[list["Sku"]] = relationship(back_populates="supplier")


class Sku(Base):
    __tablename__ = "sku"
    __table_args__ = (UniqueConstraint("company_id", "code", name="uq_sku_company_code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("company.id"))
    code: Mapped[str] = mapped_column(String(100), index=True)
    name: Mapped[str] = mapped_column(String(200))
    category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    supplier_id: Mapped[int | None] = mapped_column(
        ForeignKey("supplier.id"), nullable=True
    )
    unit_cost: Mapped[float] = mapped_column(Float, default=0.0)
    # Nullable -> cascade to supplier/company default at score time.
    safety_stock: Mapped[float | None] = mapped_column(Float, nullable=True)
    lead_time_days: Mapped[float | None] = mapped_column(Float, nullable=True)

    company: Mapped["Company"] = relationship(back_populates="skus")
    supplier: Mapped["Supplier | None"] = relationship(back_populates="skus")
    sales: Mapped[list["SaleEvent"]] = relationship(
        back_populates="sku", cascade="all, delete-orphan"
    )
    snapshots: Mapped[list["StockSnapshot"]] = relationship(
        back_populates="sku", cascade="all, delete-orphan"
    )
    purchase_orders: Mapped[list["PurchaseOrder"]] = relationship(
        back_populates="sku", cascade="all, delete-orphan"
    )


class SaleEvent(Base):
    __tablename__ = "sale_event"

    id: Mapped[int] = mapped_column(primary_key=True)
    sku_id: Mapped[int] = mapped_column(ForeignKey("sku.id"), index=True)
    quantity: Mapped[float] = mapped_column(Float)
    occurred_at: Mapped[date] = mapped_column(Date, index=True)
    unit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(50), default="manual")

    sku: Mapped["Sku"] = relationship(back_populates="sales")


class StockSnapshot(Base):
    __tablename__ = "stock_snapshot"

    id: Mapped[int] = mapped_column(primary_key=True)
    sku_id: Mapped[int] = mapped_column(ForeignKey("sku.id"), index=True)
    on_hand: Mapped[float] = mapped_column(Float)
    on_order: Mapped[float] = mapped_column(Float, default=0.0)
    observed_at: Mapped[date] = mapped_column(Date, index=True)
    # Nullable now so multi-warehouse aggregation is a later migration.
    warehouse_id: Mapped[int | None] = mapped_column(nullable=True)

    sku: Mapped["Sku"] = relationship(back_populates="snapshots")


class PurchaseOrder(Base):
    __tablename__ = "purchase_order"

    id: Mapped[int] = mapped_column(primary_key=True)
    sku_id: Mapped[int] = mapped_column(ForeignKey("sku.id"), index=True)
    quantity: Mapped[float] = mapped_column(Float)
    ordered_at: Mapped[date] = mapped_column(Date)
    expected_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    received_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Recorded by the ingestion adapter (mirrors po_status_from_legacy): a
    # backordered arrival's realized lead time is excluded from the learned
    # normal-ops mean so one 90-day tail doesn't poison the estimate.
    is_backorder: Mapped[bool] = mapped_column(Boolean, default=False)

    sku: Mapped["Sku"] = relationship(back_populates="purchase_orders")
