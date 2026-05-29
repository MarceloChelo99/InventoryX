"""Request-scoped dependencies: DB session, company resolution, SKU lookup.

The session factory lives on ``app.state`` (set in ``create_app``) so tests can
inject an in-memory engine. ``get_session`` yields one session per request and
commits on success / rolls back on error.
"""

from __future__ import annotations

from typing import Iterator, Optional

from fastapi import Depends, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from inventoryx.db import models


def get_session(request: Request) -> Iterator[Session]:
    factory = request.app.state.session_factory
    session: Session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def resolve_company(
    session: Session = Depends(get_session),
    company_id: Optional[int] = Query(
        None,
        description="Target company. Optional when exactly one company exists "
        "(local single-tenant mode).",
    ),
) -> models.Company:
    """Resolve the working company.

    Explicit ``company_id`` wins. Otherwise, if there's exactly one company
    (the local-mode default), use it; if there are several, require the caller
    to disambiguate.
    """
    if company_id is not None:
        company = session.get(models.Company, company_id)
        if company is None:
            raise HTTPException(404, f"company {company_id} not found")
        return company

    count = session.scalar(select(func.count()).select_from(models.Company))
    if count == 0:
        raise HTTPException(400, "no company exists yet — POST /companies first")
    if count > 1:
        raise HTTPException(400, "multiple companies exist — pass ?company_id=")
    return session.scalars(select(models.Company)).one()


def get_sku_or_404(
    session: Session, company: models.Company, code: str
) -> models.Sku:
    sku = session.scalars(
        select(models.Sku)
        .where(models.Sku.company_id == company.id)
        .where(models.Sku.code == code)
    ).first()
    if sku is None:
        raise HTTPException(404, f"SKU {code!r} not found for company {company.id}")
    return sku
