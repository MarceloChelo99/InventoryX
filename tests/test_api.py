"""End-to-end tests for the FastAPI surface."""

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from inventoryx.api import create_app

AS_OF = "2026-05-29"
AS_OF_DATE = date(2026, 5, 29)


@pytest.fixture()
def client():
    # Shared in-memory SQLite: StaticPool keeps one connection so the DB
    # persists across requests within the test.
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    app = create_app(engine=engine)
    with TestClient(app) as c:
        yield c


def _seed_company(client):
    r = client.post("/companies", json={"name": "Acme", "default_lead_time_days": 14})
    assert r.status_code == 201
    return r.json()["id"]


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_create_and_list_company(client):
    _seed_company(client)
    rows = client.get("/companies").json()
    assert len(rows) == 1 and rows[0]["name"] == "Acme"


def test_full_ingest_and_score_flow(client):
    _seed_company(client)
    # Create a SKU (single-tenant: no company_id needed).
    r = client.post("/skus", json={"code": "FLOW-1", "name": "Flow", "safety_stock": 5})
    assert r.status_code == 201

    # Bulk-ingest 60 days of steady demand.
    sales = [
        {"sku_code": "FLOW-1", "quantity": 8, "occurred_at": str(AS_OF_DATE - timedelta(days=i))}
        for i in range(60)
    ]
    r = client.post("/sales", json=sales)
    assert r.status_code == 201 and r.json()["ingested"] == 60

    # Starved stock position.
    r = client.post(
        "/stock/snapshot",
        json=[{"sku_code": "FLOW-1", "on_hand": 2, "on_order": 0, "observed_at": AS_OF}],
    )
    assert r.status_code == 201

    # A received PO -> learned 14-day lead time.
    client.post(
        "/purchase-orders",
        json=[{
            "sku_code": "FLOW-1",
            "quantity": 40,
            "ordered_at": str(AS_OF_DATE - timedelta(days=20)),
            "received_at": str(AS_OF_DATE - timedelta(days=6)),
        }],
    )

    # GET /skus reflects the recommendation.
    skus = client.get(f"/skus?as_of={AS_OF}").json()
    assert len(skus) == 1
    item = skus[0]
    assert item["sku_code"] == "FLOW-1"
    assert item["engine"] == "flow"
    assert item["quantity"] > 0
    assert item["action"] in ("reorder", "urgent")

    # Reorder insight includes it; overstock does not.
    reorder = client.get(f"/insights/reorder?as_of={AS_OF}").json()
    overstock = client.get(f"/insights/overstock?as_of={AS_OF}").json()
    assert "FLOW-1" in [s["sku_code"] for s in reorder]
    assert "FLOW-1" not in [s["sku_code"] for s in overstock]


def test_score_breakdown(client):
    _seed_company(client)
    client.post("/skus", json={"code": "X", "name": "X"})
    client.post(
        "/sales",
        json=[
            {"sku_code": "X", "quantity": 4, "occurred_at": str(AS_OF_DATE - timedelta(days=i))}
            for i in range(30)
        ],
    )
    client.post("/stock/snapshot", json=[{"sku_code": "X", "on_hand": 10, "observed_at": AS_OF}])

    body = client.get(f"/skus/X/score?as_of={AS_OF}").json()
    assert body["weekly_demand"] > 0
    assert body["mean_lead_days"] == 14.0  # cascades to company default (set to 14)
    assert "routed_on" in body["diagnostics"]


def test_history_endpoint(client):
    _seed_company(client)
    client.post("/skus", json={"code": "H", "name": "H"})
    client.post("/sales", json=[{"sku_code": "H", "quantity": 3, "occurred_at": AS_OF}])
    client.post("/stock/snapshot", json=[{"sku_code": "H", "on_hand": 7, "observed_at": AS_OF}])

    hist = client.get("/skus/H/history").json()
    assert hist["sku_code"] == "H"
    assert len(hist["sales"]) == 1 and hist["sales"][0]["quantity"] == 3
    assert len(hist["snapshots"]) == 1 and hist["snapshots"][0]["on_hand"] == 7


def test_receive_purchase_order(client):
    _seed_company(client)
    client.post("/skus", json={"code": "P", "name": "P"})
    client.post(
        "/purchase-orders",
        json=[{"sku_code": "P", "quantity": 5, "ordered_at": str(AS_OF_DATE - timedelta(days=10))}],
    )
    r = client.patch("/purchase-orders/1/receive", json={"received_at": AS_OF})
    assert r.status_code == 200 and r.json()["received_at"] == AS_OF


def test_unknown_sku_is_404(client):
    _seed_company(client)
    r = client.post("/sales", json=[{"sku_code": "NOPE", "quantity": 1, "occurred_at": AS_OF}])
    assert r.status_code == 404


def test_duplicate_sku_code_is_409(client):
    _seed_company(client)
    client.post("/skus", json={"code": "DUP", "name": "a"})
    r = client.post("/skus", json={"code": "DUP", "name": "b"})
    assert r.status_code == 409


def test_no_company_yet_is_400(client):
    r = client.post("/skus", json={"code": "X", "name": "X"})
    assert r.status_code == 400


def test_multiple_companies_requires_company_id(client):
    a = _seed_company(client)
    client.post("/companies", json={"name": "Beta"})
    # Ambiguous without company_id.
    assert client.post("/skus", json={"code": "X", "name": "X"}).status_code == 400
    # Explicit company_id works.
    assert client.post("/skus", json={"code": "X", "name": "X", "company_id": a}).status_code == 201
