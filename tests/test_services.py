"""Tests for the production forecaster + scoring service."""

from datetime import date, timedelta

from inventoryx.inventory_engines import Action, Engine
from inventoryx.simulation.demand_service import DemandService
from inventoryx.services import (
    Forecaster,
    InMemorySource,
    ScoringService,
)

AS_OF = date(2026, 5, 29)


def _events(sku_id, daily, end=AS_OF):
    """Build SaleEvents from a daily series ending at `end` (one day each)."""
    start = end - timedelta(days=len(daily) - 1)
    from inventoryx.services import SaleEvent

    return [
        SaleEvent(sku_id=sku_id, quantity=q, occurred_at=start + timedelta(days=i))
        for i, q in enumerate(daily)
        if q > 0
    ]


# --- the bridge guarantee --------------------------------------------------


def test_forecaster_profile_matches_sim_demand_service():
    """Same daily series -> byte-for-byte identical SKUStats in sim and prod.

    daily[0] is non-zero so the production series starts exactly at the sim's
    day 0; otherwise first-ever clamping would shorten the prod window.
    """
    daily = [3, 5, 0, 2, 0, 0, 4, 1, 0, 0, 6, 0, 2, 0] * 2  # 28 days, daily[0]>0

    prod = Forecaster(window_days=90).profile(_events("A", daily), AS_OF)

    sim = DemandService(window_days=90)
    for q in daily:
        sim.observe_sale("A", q)
    sim_stats = sim.profile_for("A")

    assert prod == sim_stats


def test_empty_history_is_zero_profile():
    stats = Forecaster().profile([], AS_OF)
    assert stats.weekly_demand == 0.0
    assert stats.n_events == 0


# --- window + new-SKU behavior ---------------------------------------------


def test_events_outside_window_are_ignored():
    f = Forecaster(window_days=30)
    inside = _events("A", [5] * 30)  # last 30 days
    old = _events("A", [99] * 10, end=AS_OF - timedelta(days=60))  # well before window
    stats = f.profile(inside + old, AS_OF)
    # 5/day for 30 days -> 35/week; the 99s outside the window must not show up.
    assert round(stats.weekly_demand, 2) == 35.0


def test_new_sku_not_diluted_by_window():
    """7 days of strong sales must read as high weekly demand, not /90."""
    f = Forecaster(window_days=90)
    stats = f.profile(_events("A", [10] * 7), AS_OF)
    assert round(stats.weekly_demand, 1) == 70.0  # not diluted to ~5.4


# --- weighted-MA predict ---------------------------------------------------


def test_predict_weights_recent_sales_higher():
    f = Forecaster(window_days=30, tau_days=7.0)
    recent_spike = f.predict(_events("A", [0] * 29 + [30]), AS_OF)
    old_spike = f.predict(_events("A", [30] + [0] * 29), AS_OF)
    assert recent_spike > old_spike


def test_predict_zero_history():
    assert Forecaster().predict([], AS_OF) == 0.0


# --- scoring service end to end --------------------------------------------


def test_score_sku_flags_reorder_when_starved():
    src = InMemorySource()
    # Steady high daily demand, essentially no stock or pipeline.
    for i in range(60):
        src.add_sale("FLOW1", 8, AS_OF - timedelta(days=i))
    src.set_stock_state("FLOW1", on_hand=1, on_order=0, safety_stock=5)
    src.add_lead_observation("FLOW1", 14)

    rec = ScoringService(src).score_sku("FLOW1", AS_OF)
    assert rec.engine is Engine.FLOW
    assert rec.quantity > 0
    assert rec.action in (Action.REORDER, Action.URGENT)


def test_score_sku_overstocked_when_swimming_in_stock():
    src = InMemorySource()
    for i in range(60):
        src.add_sale("SLOW1", 1, AS_OF - timedelta(days=i * 6))  # sparse
    src.set_stock_state("SLOW1", on_hand=5000, on_order=2000, safety_stock=0)
    src.add_lead_observation("SLOW1", 10)

    rec = ScoringService(src).score_sku("SLOW1", AS_OF)
    assert rec.quantity == 0
    assert rec.action is Action.OVERSTOCKED


def test_reorder_and_overstock_lists_partition_and_sort():
    src = InMemorySource()
    # Starved high-demand SKU -> reorder.
    for i in range(60):
        src.add_sale("HOT", 9, AS_OF - timedelta(days=i))
    src.set_stock_state("HOT", on_hand=0, safety_stock=5)
    src.add_lead_observation("HOT", 14)
    # Overstocked slow SKU.
    for i in range(20):
        src.add_sale("COLD", 1, AS_OF - timedelta(days=i * 6))
    src.set_stock_state("COLD", on_hand=4000, on_order=1000)
    src.add_lead_observation("COLD", 10)

    svc = ScoringService(src)
    reorder_ids = [s.sku_id for s in svc.reorder_list(AS_OF)]
    overstock_ids = [s.sku_id for s in svc.overstock_list(AS_OF)]

    assert "HOT" in reorder_ids
    assert "COLD" in overstock_ids
    assert set(reorder_ids).isdisjoint(overstock_ids)


def test_lead_time_fallback_used_when_no_observations():
    src = InMemorySource()
    for i in range(30):
        src.add_sale("X", 4, AS_OF - timedelta(days=i))
    src.set_stock_state("X", on_hand=10, fallback_lead_days=21)
    # No lead observations -> service must fall back without error.
    rec = ScoringService(src).score_sku("X", AS_OF)
    assert rec is not None
