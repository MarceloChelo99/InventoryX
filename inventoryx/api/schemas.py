"""Pydantic I/O schemas for the REST API.

Input models are what the integration POSTs; output models are JSON-safe
projections. Inventory math types (``OrderRecommendation`` etc.) never cross
the wire directly — they're mapped into these.
"""

from __future__ import annotations

from datetime import date
from typing import List, Optional

from pydantic import BaseModel, Field


# --- catalog ---------------------------------------------------------------


class CompanyCreate(BaseModel):
    name: str
    default_lead_time_days: float = 15.0
    default_safety_stock: float = 0.0


class CompanyOut(BaseModel):
    id: int
    name: str
    default_lead_time_days: float
    default_safety_stock: float


class SupplierCreate(BaseModel):
    name: str
    default_lead_time_days: Optional[float] = None
    company_id: Optional[int] = None


class SupplierOut(BaseModel):
    id: int
    company_id: int
    name: str
    default_lead_time_days: Optional[float]


class SkuCreate(BaseModel):
    code: str
    name: str
    category: Optional[str] = None
    supplier_id: Optional[int] = None
    unit_cost: float = 0.0
    safety_stock: Optional[float] = None
    lead_time_days: Optional[float] = None
    company_id: Optional[int] = None


class SkuOut(BaseModel):
    id: int
    company_id: int
    code: str
    name: str
    category: Optional[str]
    supplier_id: Optional[int]
    unit_cost: float
    safety_stock: Optional[float]
    lead_time_days: Optional[float]


# --- ingestion (all POSTs accept arrays — bulk is the common case) ---------


class SaleIn(BaseModel):
    sku_code: str
    quantity: float
    occurred_at: date
    unit_price: Optional[float] = None
    source: str = "manual"


class SnapshotIn(BaseModel):
    sku_code: str
    on_hand: float
    on_order: float = 0.0
    observed_at: date
    warehouse_id: Optional[int] = None


class PurchaseOrderIn(BaseModel):
    sku_code: str
    quantity: float
    ordered_at: date
    expected_at: Optional[date] = None
    received_at: Optional[date] = None
    is_backorder: bool = False


class PurchaseOrderReceive(BaseModel):
    received_at: date
    is_backorder: bool = False


class IngestResult(BaseModel):
    ingested: int


# --- scoring / insights ----------------------------------------------------


class RecommendationOut(BaseModel):
    sku_code: str
    action: str
    engine: str
    quantity: float
    alert_score: float
    score_confidence: str
    reorder_point: Optional[float] = None
    on_hand: float
    on_order: float


class ScoreBreakdown(RecommendationOut):
    """Detailed score, including the formula intermediates."""

    weekly_demand: float
    safety_stock: float
    mean_lead_days: float
    diagnostics: dict = Field(default_factory=dict)


class SaleOut(BaseModel):
    quantity: float
    occurred_at: date
    unit_price: Optional[float]
    source: str


class SnapshotOut(BaseModel):
    on_hand: float
    on_order: float
    observed_at: date
    warehouse_id: Optional[int]


class HistoryOut(BaseModel):
    sku_code: str
    sales: List[SaleOut]
    snapshots: List[SnapshotOut]
