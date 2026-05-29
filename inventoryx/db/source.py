"""
SQL-backed ``InventoryDataSource``.

This is the payoff of the data-source seam built with ``ScoringService``: it
reads the ORM tables and hands back the exact plain dataclasses the scoring
layer already consumes (``SaleEvent``, ``LeadTimeObservation``, ``StockState``).
Swap an ``InMemorySource`` for this and scoring runs on a real database with no
other change.

SKUs are addressed by their external ``code`` (the stable business identifier),
not the integer primary key — so ``sku_id`` everywhere in the scoring layer is
a SKU code.
"""

from __future__ import annotations

from datetime import date
from typing import Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from inventoryx.db import models
from inventoryx.services.forecaster import SaleEvent
from inventoryx.services.sources import LeadTimeObservation, StockState


class SqlInventoryDataSource:
    """Reads scoring inputs from the SQLAlchemy models for one company."""

    def __init__(self, session: Session, company_id: Optional[int] = None):
        self.session = session
        self.company_id = company_id

    # --- helpers ----------------------------------------------------------

    def _sku_query(self):
        q = select(models.Sku)
        if self.company_id is not None:
            q = q.where(models.Sku.company_id == self.company_id)
        return q

    def _sku_by_code(self, code: str) -> Optional[models.Sku]:
        return self.session.scalars(
            self._sku_query().where(models.Sku.code == code)
        ).first()

    @staticmethod
    def _resolve_lead_fallback(sku: models.Sku) -> float:
        """SKU -> supplier -> company default."""
        if sku.lead_time_days is not None:
            return sku.lead_time_days
        if sku.supplier is not None and sku.supplier.default_lead_time_days is not None:
            return sku.supplier.default_lead_time_days
        return sku.company.default_lead_time_days

    @staticmethod
    def _resolve_safety_stock(sku: models.Sku) -> float:
        if sku.safety_stock is not None:
            return sku.safety_stock
        return sku.company.default_safety_stock

    # --- InventoryDataSource ----------------------------------------------

    def sku_ids(self) -> List[str]:
        codes = self.session.scalars(
            self._sku_query().order_by(models.Sku.code)
        ).all()
        return [s.code for s in codes]

    def sales_history(self, sku_id: str, as_of: date) -> List[SaleEvent]:
        sku = self._sku_by_code(sku_id)
        if sku is None:
            return []
        rows = self.session.scalars(
            select(models.SaleEvent)
            .where(models.SaleEvent.sku_id == sku.id)
            .where(models.SaleEvent.occurred_at <= as_of)
            .order_by(models.SaleEvent.occurred_at)
        ).all()
        return [
            SaleEvent(sku_id=sku_id, quantity=r.quantity, occurred_at=r.occurred_at)
            for r in rows
        ]

    def lead_time_history(self, sku_id: str) -> List[LeadTimeObservation]:
        sku = self._sku_by_code(sku_id)
        if sku is None:
            return []
        rows = self.session.scalars(
            select(models.PurchaseOrder)
            .where(models.PurchaseOrder.sku_id == sku.id)
            .where(models.PurchaseOrder.received_at.is_not(None))
        ).all()
        obs: List[LeadTimeObservation] = []
        for po in rows:
            lead_days = (po.received_at - po.ordered_at).days
            obs.append(
                LeadTimeObservation(
                    sku_id=sku_id,
                    lead_days=float(lead_days),
                    was_backorder=po.is_backorder,
                )
            )
        return obs

    def stock_state(self, sku_id: str, as_of: date) -> StockState:
        sku = self._sku_by_code(sku_id)
        if sku is None:
            return StockState()
        latest = self.session.scalars(
            select(models.StockSnapshot)
            .where(models.StockSnapshot.sku_id == sku.id)
            .where(models.StockSnapshot.observed_at <= as_of)
            .order_by(models.StockSnapshot.observed_at.desc())
        ).first()
        on_hand = latest.on_hand if latest is not None else 0.0
        on_order = latest.on_order if latest is not None else 0.0
        return StockState(
            on_hand=on_hand,
            on_order=on_order,
            safety_stock=self._resolve_safety_stock(sku),
            fallback_lead_days=self._resolve_lead_fallback(sku),
        )
