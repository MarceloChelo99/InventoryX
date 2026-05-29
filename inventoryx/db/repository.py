"""
Write-side helpers — ingestion into the ORM tables.

The ``InventoryDataSource`` protocol is read-only by design (scoring never
writes). Ingestion is a separate concern, kept here so the future REST
endpoints (POST /sales, /stock/snapshot, /purchase-orders) and CSV importer
have one place to call.
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from sqlalchemy.orm import Session

from inventoryx.db import models


class Repository:
    def __init__(self, session: Session):
        self.session = session

    # --- setup ------------------------------------------------------------

    def create_company(
        self,
        name: str,
        default_lead_time_days: float = 15.0,
        default_safety_stock: float = 0.0,
    ) -> models.Company:
        company = models.Company(
            name=name,
            default_lead_time_days=default_lead_time_days,
            default_safety_stock=default_safety_stock,
        )
        self.session.add(company)
        self.session.flush()
        return company

    def create_supplier(
        self,
        company: models.Company,
        name: str,
        default_lead_time_days: Optional[float] = None,
    ) -> models.Supplier:
        supplier = models.Supplier(
            company_id=company.id,
            name=name,
            default_lead_time_days=default_lead_time_days,
        )
        self.session.add(supplier)
        self.session.flush()
        return supplier

    def create_sku(
        self,
        company: models.Company,
        code: str,
        name: str,
        *,
        category: Optional[str] = None,
        supplier: Optional[models.Supplier] = None,
        unit_cost: float = 0.0,
        safety_stock: Optional[float] = None,
        lead_time_days: Optional[float] = None,
    ) -> models.Sku:
        sku = models.Sku(
            company_id=company.id,
            code=code,
            name=name,
            category=category,
            supplier_id=supplier.id if supplier is not None else None,
            unit_cost=unit_cost,
            safety_stock=safety_stock,
            lead_time_days=lead_time_days,
        )
        self.session.add(sku)
        self.session.flush()
        return sku

    # --- append-only ingestion -------------------------------------------

    def record_sale(
        self,
        sku: models.Sku,
        quantity: float,
        occurred_at: date,
        *,
        unit_price: Optional[float] = None,
        source: str = "manual",
    ) -> models.SaleEvent:
        ev = models.SaleEvent(
            sku_id=sku.id,
            quantity=quantity,
            occurred_at=occurred_at,
            unit_price=unit_price,
            source=source,
        )
        self.session.add(ev)
        return ev

    def record_snapshot(
        self,
        sku: models.Sku,
        on_hand: float,
        observed_at: date,
        *,
        on_order: float = 0.0,
        warehouse_id: Optional[int] = None,
    ) -> models.StockSnapshot:
        snap = models.StockSnapshot(
            sku_id=sku.id,
            on_hand=on_hand,
            on_order=on_order,
            observed_at=observed_at,
            warehouse_id=warehouse_id,
        )
        self.session.add(snap)
        return snap

    def record_purchase_order(
        self,
        sku: models.Sku,
        quantity: float,
        ordered_at: date,
        *,
        expected_at: Optional[date] = None,
        received_at: Optional[date] = None,
        is_backorder: bool = False,
    ) -> models.PurchaseOrder:
        po = models.PurchaseOrder(
            sku_id=sku.id,
            quantity=quantity,
            ordered_at=ordered_at,
            expected_at=expected_at,
            received_at=received_at,
            is_backorder=is_backorder,
        )
        self.session.add(po)
        return po
