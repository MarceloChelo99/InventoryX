"""Scoring + insight endpoints over the validated engines."""

from __future__ import annotations

from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from inventoryx.api.deps import get_session, get_sku_or_404, resolve_company
from inventoryx.api.schemas import (
    HistoryOut,
    RecommendationOut,
    SaleOut,
    ScoreBreakdown,
    SnapshotOut,
)
from inventoryx.db import SqlInventoryDataSource, models
from inventoryx.inventory_engines import LeadTimeProfile, OrderRecommendation
from inventoryx.services import ScoringService

router = APIRouter(tags=["insights"])


def _today_or(as_of: Optional[date]) -> date:
    return as_of or date.today()


def _service(session: Session, company: models.Company):
    source = SqlInventoryDataSource(session, company_id=company.id)
    return source, ScoringService(source)


def _rec_out(
    source: SqlInventoryDataSource,
    sku_code: str,
    rec: OrderRecommendation,
    as_of: date,
) -> RecommendationOut:
    state = source.stock_state(sku_code, as_of)
    return RecommendationOut(
        sku_code=sku_code,
        action=rec.action.value,
        engine=rec.engine.value,
        quantity=rec.quantity,
        alert_score=rec.alert_score,
        score_confidence=rec.score_confidence.value,
        reorder_point=rec.reorder_point,
        on_hand=state.on_hand,
        on_order=state.on_order,
    )


@router.get("/skus", response_model=List[RecommendationOut])
def list_skus(
    session: Session = Depends(get_session),
    company: models.Company = Depends(resolve_company),
    as_of: Optional[date] = Query(None),
):
    """Every SKU with its current recommendation + stock position."""
    day = _today_or(as_of)
    source, svc = _service(session, company)
    return [
        _rec_out(source, s.sku_id, s.recommendation, day)
        for s in svc.score_all(day)
    ]


@router.get("/skus/{code}/score", response_model=ScoreBreakdown)
def sku_score(
    code: str,
    session: Session = Depends(get_session),
    company: models.Company = Depends(resolve_company),
    as_of: Optional[date] = Query(None),
):
    """Detailed score for one SKU, including formula intermediates."""
    day = _today_or(as_of)
    get_sku_or_404(session, company, code)  # 404 if unknown
    source, svc = _service(session, company)

    rec = svc.score_sku(code, day)
    base = _rec_out(source, code, rec, day)

    stats = svc.forecaster.profile(source.sales_history(code, day), day)
    state = source.stock_state(code, day)
    leads = source.lead_time_history(code)
    lead_profile = LeadTimeProfile.from_realized(
        [o.lead_days for o in leads],
        [o.was_backorder for o in leads],
        fallback_mean=state.fallback_lead_days,
    )

    return ScoreBreakdown(
        **base.model_dump(),
        weekly_demand=stats.weekly_demand,
        safety_stock=state.safety_stock,
        mean_lead_days=lead_profile.mean_lead_days,
        diagnostics=rec.diagnostics,
    )


@router.get("/skus/{code}/history", response_model=HistoryOut)
def sku_history(
    code: str,
    session: Session = Depends(get_session),
    company: models.Company = Depends(resolve_company),
):
    """Raw sales + snapshot history for one SKU."""
    sku = get_sku_or_404(session, company, code)
    sales = session.scalars(
        select(models.SaleEvent)
        .where(models.SaleEvent.sku_id == sku.id)
        .order_by(models.SaleEvent.occurred_at)
    ).all()
    snaps = session.scalars(
        select(models.StockSnapshot)
        .where(models.StockSnapshot.sku_id == sku.id)
        .order_by(models.StockSnapshot.observed_at)
    ).all()
    return HistoryOut(
        sku_code=code,
        sales=[
            SaleOut(
                quantity=s.quantity,
                occurred_at=s.occurred_at,
                unit_price=s.unit_price,
                source=s.source,
            )
            for s in sales
        ],
        snapshots=[
            SnapshotOut(
                on_hand=sn.on_hand,
                on_order=sn.on_order,
                observed_at=sn.observed_at,
                warehouse_id=sn.warehouse_id,
            )
            for sn in snaps
        ],
    )


@router.get("/insights/reorder", response_model=List[RecommendationOut])
def insights_reorder(
    session: Session = Depends(get_session),
    company: models.Company = Depends(resolve_company),
    as_of: Optional[date] = Query(None),
):
    day = _today_or(as_of)
    source, svc = _service(session, company)
    return [
        _rec_out(source, s.sku_id, s.recommendation, day)
        for s in svc.reorder_list(day)
    ]


@router.get("/insights/overstock", response_model=List[RecommendationOut])
def insights_overstock(
    session: Session = Depends(get_session),
    company: models.Company = Depends(resolve_company),
    as_of: Optional[date] = Query(None),
):
    day = _today_or(as_of)
    source, svc = _service(session, company)
    return [
        _rec_out(source, s.sku_id, s.recommendation, day)
        for s in svc.overstock_list(day)
    ]
