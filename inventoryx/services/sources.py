"""
Data source seam for the production scoring path.

``ScoringService`` reads everything it needs through the ``InventoryDataSource``
protocol: sales history, realized lead times, and current stock state. v1 ships
an in-memory implementation; the future SQLite/SQLAlchemy layer (design doc
phase 1) becomes just another implementation of this protocol — the service
above it doesn't change.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Protocol, Sequence, runtime_checkable

from inventoryx.services.forecaster import SaleEvent


@dataclass(frozen=True)
class LeadTimeObservation:
    """One realized arrival: how many days the PO actually took.

    ``was_backorder`` arrivals are excluded from the learned lead-time mean by
    ``LeadTimeProfile.from_realized`` (a single 90-day backorder would poison
    the normal-ops estimate forever).
    """

    sku_id: str
    lead_days: float
    was_backorder: bool = False


@dataclass(frozen=True)
class StockState:
    """Current inventory position for one SKU (latest snapshot + open POs)."""

    on_hand: float = 0.0
    on_order: float = 0.0
    safety_stock: float = 0.0
    # Used when the SKU has no clean realized lead times yet (cascades from
    # SKU -> supplier -> company default in the eventual data model).
    fallback_lead_days: float = 15.0


@runtime_checkable
class InventoryDataSource(Protocol):
    """What ``ScoringService`` needs to score a SKU. Back it with anything."""

    def sku_ids(self) -> Sequence[str]: ...

    def sales_history(
        self, sku_id: str, as_of: date
    ) -> Sequence[SaleEvent]: ...

    def lead_time_history(
        self, sku_id: str
    ) -> Sequence[LeadTimeObservation]: ...

    def stock_state(self, sku_id: str, as_of: date) -> StockState: ...


class InMemorySource:
    """In-memory ``InventoryDataSource`` — a stub for tests and local runs."""

    def __init__(self) -> None:
        self._sales: Dict[str, List[SaleEvent]] = defaultdict(list)
        self._leads: Dict[str, List[LeadTimeObservation]] = defaultdict(list)
        self._stock: Dict[str, StockState] = {}

    # --- ingestion --------------------------------------------------------

    def add_sale(self, sku_id: str, quantity: float, occurred_at: date) -> None:
        self._sales[sku_id].append(
            SaleEvent(sku_id=sku_id, quantity=float(quantity), occurred_at=occurred_at)
        )

    def add_lead_observation(
        self, sku_id: str, lead_days: float, was_backorder: bool = False
    ) -> None:
        self._leads[sku_id].append(
            LeadTimeObservation(
                sku_id=sku_id,
                lead_days=float(lead_days),
                was_backorder=was_backorder,
            )
        )

    def set_stock_state(
        self,
        sku_id: str,
        on_hand: float,
        on_order: float = 0.0,
        safety_stock: float = 0.0,
        fallback_lead_days: float = 15.0,
    ) -> None:
        self._stock[sku_id] = StockState(
            on_hand=float(on_hand),
            on_order=float(on_order),
            safety_stock=float(safety_stock),
            fallback_lead_days=float(fallback_lead_days),
        )

    # --- InventoryDataSource ----------------------------------------------

    def sku_ids(self) -> List[str]:
        return sorted(set(self._sales) | set(self._leads) | set(self._stock))

    def sales_history(self, sku_id: str, as_of: date) -> List[SaleEvent]:
        return [e for e in self._sales.get(sku_id, []) if e.occurred_at <= as_of]

    def lead_time_history(self, sku_id: str) -> List[LeadTimeObservation]:
        return list(self._leads.get(sku_id, []))

    def stock_state(self, sku_id: str, as_of: date) -> StockState:
        return self._stock.get(sku_id, StockState())
