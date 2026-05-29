"""Ingestion endpoints. All POSTs accept arrays — bulk is the common case."""

from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from inventoryx.api.deps import get_session, get_sku_or_404, resolve_company
from inventoryx.api.schemas import (
    IngestResult,
    PurchaseOrderIn,
    PurchaseOrderReceive,
    SaleIn,
    SnapshotIn,
)
from inventoryx.db import Repository, models

router = APIRouter(tags=["ingest"])


@router.post("/sales", response_model=IngestResult, status_code=201)
def ingest_sales(
    body: List[SaleIn],
    session: Session = Depends(get_session),
    company: models.Company = Depends(resolve_company),
):
    repo = Repository(session)
    for s in body:
        sku = get_sku_or_404(session, company, s.sku_code)
        repo.record_sale(
            sku,
            quantity=s.quantity,
            occurred_at=s.occurred_at,
            unit_price=s.unit_price,
            source=s.source,
        )
    return IngestResult(ingested=len(body))


@router.post("/stock/snapshot", response_model=IngestResult, status_code=201)
def ingest_snapshots(
    body: List[SnapshotIn],
    session: Session = Depends(get_session),
    company: models.Company = Depends(resolve_company),
):
    repo = Repository(session)
    for snap in body:
        sku = get_sku_or_404(session, company, snap.sku_code)
        repo.record_snapshot(
            sku,
            on_hand=snap.on_hand,
            on_order=snap.on_order,
            observed_at=snap.observed_at,
            warehouse_id=snap.warehouse_id,
        )
    return IngestResult(ingested=len(body))


@router.post("/purchase-orders", response_model=IngestResult, status_code=201)
def ingest_purchase_orders(
    body: List[PurchaseOrderIn],
    session: Session = Depends(get_session),
    company: models.Company = Depends(resolve_company),
):
    repo = Repository(session)
    for po in body:
        sku = get_sku_or_404(session, company, po.sku_code)
        repo.record_purchase_order(
            sku,
            quantity=po.quantity,
            ordered_at=po.ordered_at,
            expected_at=po.expected_at,
            received_at=po.received_at,
            is_backorder=po.is_backorder,
        )
    return IngestResult(ingested=len(body))


@router.patch("/purchase-orders/{po_id}/receive", status_code=200)
def receive_purchase_order(
    po_id: int,
    body: PurchaseOrderReceive,
    session: Session = Depends(get_session),
):
    po = session.get(models.PurchaseOrder, po_id)
    if po is None:
        raise HTTPException(404, f"purchase order {po_id} not found")
    po.received_at = body.received_at
    po.is_backorder = body.is_backorder
    return {"id": po.id, "received_at": po.received_at.isoformat()}
