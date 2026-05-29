"""Tests for the SQLAlchemy persistence layer + SQL-backed scoring."""

from datetime import date, timedelta

import pytest

from inventoryx.db import (
    Repository,
    SqlInventoryDataSource,
    init_db,
    make_engine,
    make_session_factory,
)
from inventoryx.services import InMemorySource, ScoringService

AS_OF = date(2026, 5, 29)


@pytest.fixture()
def session():
    engine = make_engine("sqlite://")  # in-memory
    init_db(engine)
    Session = make_session_factory(engine)
    with Session() as s:
        yield s


def test_init_db_creates_all_six_tables(session):
    from sqlalchemy import inspect

    names = set(inspect(session.get_bind()).get_table_names())
    assert {
        "company",
        "supplier",
        "sku",
        "sale_event",
        "stock_snapshot",
        "purchase_order",
    } <= names


def test_sql_source_round_trips_inputs(session):
    repo = Repository(session)
    co = repo.create_company("Acme", default_lead_time_days=15.0)
    sup = repo.create_supplier(co, "VendorA", default_lead_time_days=12.0)
    sku = repo.create_sku(co, "TIRE-1", "Tire", supplier=sup, safety_stock=5)
    for i in range(30):
        repo.record_sale(sku, 4, AS_OF - timedelta(days=i))
    repo.record_snapshot(sku, on_hand=20, on_order=10, observed_at=AS_OF)
    repo.record_purchase_order(
        sku, 50, ordered_at=AS_OF - timedelta(days=20),
        received_at=AS_OF - timedelta(days=6),  # 14-day lead
    )
    session.commit()

    src = SqlInventoryDataSource(session, company_id=co.id)
    assert src.sku_ids() == ["TIRE-1"]
    assert len(src.sales_history("TIRE-1", AS_OF)) == 30
    state = src.stock_state("TIRE-1", AS_OF)
    assert state.on_hand == 20 and state.on_order == 10 and state.safety_stock == 5
    leads = src.lead_time_history("TIRE-1")
    assert len(leads) == 1 and leads[0].lead_days == 14.0


def test_sales_history_respects_as_of(session):
    repo = Repository(session)
    co = repo.create_company("Acme")
    sku = repo.create_sku(co, "X", "X")
    repo.record_sale(sku, 1, AS_OF - timedelta(days=1))
    repo.record_sale(sku, 99, AS_OF + timedelta(days=1))  # future
    session.commit()

    src = SqlInventoryDataSource(session, company_id=co.id)
    hist = src.sales_history("X", AS_OF)
    assert [e.quantity for e in hist] == [1]


def test_lead_fallback_cascade(session):
    """SKU lead None -> supplier default; both None -> company default."""
    repo = Repository(session)
    co = repo.create_company("Acme", default_lead_time_days=20.0)
    sup = repo.create_supplier(co, "S", default_lead_time_days=9.0)
    sku_sup = repo.create_sku(co, "VIA-SUP", "n", supplier=sup)  # no sku lead
    sku_co = repo.create_sku(co, "VIA-CO", "n")  # no supplier, no sku lead
    sku_own = repo.create_sku(co, "OWN", "n", lead_time_days=3.0)
    session.commit()

    src = SqlInventoryDataSource(session, company_id=co.id)
    assert src.stock_state("VIA-SUP", AS_OF).fallback_lead_days == 9.0
    assert src.stock_state("VIA-CO", AS_OF).fallback_lead_days == 20.0
    assert src.stock_state("OWN", AS_OF).fallback_lead_days == 3.0


def test_backorder_excluded_from_learned_lead(session):
    repo = Repository(session)
    co = repo.create_company("Acme")
    sku = repo.create_sku(co, "X", "X")
    # One normal 14-day PO and one 90-day backorder.
    repo.record_purchase_order(
        sku, 10, ordered_at=AS_OF - timedelta(days=20),
        received_at=AS_OF - timedelta(days=6),
    )
    repo.record_purchase_order(
        sku, 10, ordered_at=AS_OF - timedelta(days=100),
        received_at=AS_OF - timedelta(days=10), is_backorder=True,
    )
    session.commit()

    from inventoryx.inventory_engines import LeadTimeProfile

    src = SqlInventoryDataSource(session, company_id=co.id)
    leads = src.lead_time_history("X")
    profile = LeadTimeProfile.from_realized(
        [o.lead_days for o in leads], [o.was_backorder for o in leads]
    )
    # The 90-day backorder must not drag the learned mean off 14.
    assert profile.mean_lead_days == 14.0
    assert profile.n_orders == 1


def test_sql_and_inmemory_sources_score_identically(session):
    """The headline guarantee: same data via two sources -> same recommendation.

    Proves the SQL source faithfully implements the protocol that scoring was
    validated against.
    """
    repo = Repository(session)
    co = repo.create_company("Acme", default_lead_time_days=14.0)
    sku = repo.create_sku(co, "FLOW-1", "Flow item", safety_stock=5)

    mem = InMemorySource()
    for i in range(60):
        d = AS_OF - timedelta(days=i)
        repo.record_sale(sku, 8, d)
        mem.add_sale("FLOW-1", 8, d)
    repo.record_snapshot(sku, on_hand=3, on_order=0, observed_at=AS_OF)
    mem.set_stock_state(
        "FLOW-1", on_hand=3, on_order=0, safety_stock=5, fallback_lead_days=14.0
    )
    repo.record_purchase_order(
        sku, 40, ordered_at=AS_OF - timedelta(days=20),
        received_at=AS_OF - timedelta(days=6),  # 14-day lead
    )
    mem.add_lead_observation("FLOW-1", 14)
    session.commit()

    sql_src = SqlInventoryDataSource(session, company_id=co.id)
    sql_rec = ScoringService(sql_src).score_sku("FLOW-1", AS_OF)
    mem_rec = ScoringService(mem).score_sku("FLOW-1", AS_OF)

    assert sql_rec.engine == mem_rec.engine
    assert sql_rec.quantity == mem_rec.quantity
    assert sql_rec.alert_score == mem_rec.alert_score
    assert sql_rec.action == mem_rec.action


def test_company_scoping_isolates_skus(session):
    repo = Repository(session)
    co_a = repo.create_company("A")
    co_b = repo.create_company("B")
    repo.create_sku(co_a, "SHARED", "in A")
    repo.create_sku(co_b, "SHARED", "in B")  # same code, different company
    session.commit()

    src_a = SqlInventoryDataSource(session, company_id=co_a.id)
    assert src_a.sku_ids() == ["SHARED"]
    # Unscoped source sees both rows.
    assert SqlInventoryDataSource(session).sku_ids().count("SHARED") == 2
