"""Catalog endpoints: create companies, suppliers, and SKUs."""

from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from inventoryx.api.deps import get_session, resolve_company
from inventoryx.api.schemas import (
    CompanyCreate,
    CompanyOut,
    SkuCreate,
    SkuOut,
    SupplierCreate,
    SupplierOut,
)
from inventoryx.db import Repository, models

router = APIRouter(tags=["catalog"])


@router.post("/companies", response_model=CompanyOut, status_code=201)
def create_company(body: CompanyCreate, session: Session = Depends(get_session)):
    company = Repository(session).create_company(
        name=body.name,
        default_lead_time_days=body.default_lead_time_days,
        default_safety_stock=body.default_safety_stock,
    )
    return company


@router.get("/companies", response_model=List[CompanyOut])
def list_companies(session: Session = Depends(get_session)):
    return list(session.scalars(select(models.Company).order_by(models.Company.id)))


@router.post("/suppliers", response_model=SupplierOut, status_code=201)
def create_supplier(body: SupplierCreate, session: Session = Depends(get_session)):
    # company_id flows through resolve_company semantics via explicit body field.
    company = _company_from_body(session, body.company_id)
    supplier = Repository(session).create_supplier(
        company=company,
        name=body.name,
        default_lead_time_days=body.default_lead_time_days,
    )
    return supplier


@router.post("/skus", response_model=SkuOut, status_code=201)
def create_sku(body: SkuCreate, session: Session = Depends(get_session)):
    company = _company_from_body(session, body.company_id)
    supplier = None
    if body.supplier_id is not None:
        supplier = session.get(models.Supplier, body.supplier_id)
        if supplier is None or supplier.company_id != company.id:
            raise HTTPException(400, f"supplier {body.supplier_id} not in company")
    try:
        sku = Repository(session).create_sku(
            company=company,
            code=body.code,
            name=body.name,
            category=body.category,
            supplier=supplier,
            unit_cost=body.unit_cost,
            safety_stock=body.safety_stock,
            lead_time_days=body.lead_time_days,
        )
        session.flush()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, f"SKU code {body.code!r} already exists in company")
    return sku


def _company_from_body(session: Session, company_id):
    """Resolve a company from an optional body field (single-tenant default)."""
    from inventoryx.api.deps import resolve_company as _resolve

    # Reuse the same resolution rule used by query-param endpoints.
    return _resolve(session=session, company_id=company_id)
