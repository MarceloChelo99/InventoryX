"""Layer 1 acceptance tests — one test per bullet in §1.6."""

from __future__ import annotations

import math

import pytest

from inventoryx.inventory_engines import (
    Action,
    Confidence,
    Config,
    DEFAULT_CONFIG,
    Engine,
    FlowEngine,
    IntermittentEngine,
    LeadTimeProfile,
    RaidScorer,
    Router,
    SKUStats,
    _flow_terms,
    classify_action,
)


# --- 1.6 #1 -----------------------------------------------------------------

def test_from_daily_sales_canonical_lumpy():
    """[4,0,0,0,0,0,0]*10 -> z_bar==4, p_bar==7."""
    series = [4, 0, 0, 0, 0, 0, 0] * 10
    stats = SKUStats.from_daily_sales(series)
    assert stats.z_bar == pytest.approx(4.0)
    assert stats.p_bar == pytest.approx(7.0)
    assert stats.n_events == 10


def test_from_daily_sales_dense_flow():
    """Dense daily series -> small p_bar."""
    series = [3] * 60
    stats = SKUStats.from_daily_sales(series)
    assert stats.p_bar == pytest.approx(1.0)
    assert stats.weekly_demand == pytest.approx(21.0)


# --- 1.6 #2 -----------------------------------------------------------------

def test_routing_flow_vs_intermittent():
    router = Router()
    dense = SKUStats.from_daily_sales([5] * 60)
    # p_bar = 10 > FLOW_THRESHOLD_DAYS (7) -> intermittent.
    lumpy = SKUStats.from_daily_sales([4] + [0] * 9 + ([4] + [0] * 9) * 9)
    assert lumpy.p_bar > DEFAULT_CONFIG.FLOW_THRESHOLD_DAYS
    assert router.choose_engine(dense) is Engine.FLOW
    assert router.choose_engine(lumpy) is Engine.INTERMITTENT


def test_routing_low_event_fallback():
    """n_events < MIN_EVENTS -> demand-level fallback."""
    # 2 events in 60 days — below MIN_EVENTS=4. weekly_demand=8/(60/7) ~ 0.93 < L_CENTER.
    series = [0] * 30 + [4] + [0] * 14 + [4] + [0] * 14
    stats = SKUStats.from_daily_sales(series)
    assert stats.n_events < DEFAULT_CONFIG.MIN_EVENTS
    assert Router().choose_engine(stats) is Engine.INTERMITTENT


# --- 1.6 #3 -----------------------------------------------------------------

def test_find_g_sits_on_balance():
    """find_g result zeroes the flow balance (gate-aware) and is >= 0."""
    engine = FlowEngine()
    stats = SKUStats(weekly_demand=20.0, n_events=20, p_bar=1.0, z_bar=3.0)
    lt = LeadTimeProfile(mean_lead_days=14.0)
    g = engine.find_g(stats, lt, on_order=0.0)
    assert g >= 0.0
    t1, t2, l_gate = _flow_terms(
        d=stats.weekly_demand, q=g, a=lt.mean_lead_days, s=0.0, cfg=DEFAULT_CONFIG
    )
    # Default keeps the logit gate on; assert the contract the engine solves.
    gate = l_gate if DEFAULT_CONFIG.GATE_FIND_G else 1.0
    assert t1 * t2 * gate == pytest.approx(1.0, abs=1e-6)


# --- 1.6 #4 -----------------------------------------------------------------

def test_find_g_monotone_in_on_order():
    """More already on order -> recommendation does not increase."""
    engine = FlowEngine()
    stats = SKUStats(weekly_demand=20.0, n_events=20, p_bar=1.0, z_bar=3.0)
    lt = LeadTimeProfile(mean_lead_days=14.0)
    g0 = engine.find_g(stats, lt, on_order=0.0)
    g1 = engine.find_g(stats, lt, on_order=10.0)
    g2 = engine.find_g(stats, lt, on_order=50.0)
    assert g0 >= g1 >= g2 >= 0.0


# --- 1.6 #5 -----------------------------------------------------------------

def test_find_g_zero_guard():
    """balance(0) <= 0 -> find_g returns 0 exactly."""
    engine = FlowEngine()
    # Tiny demand, huge pipeline already -> balance(0) will be deeply negative.
    stats = SKUStats(weekly_demand=0.1, n_events=10, p_bar=1.0, z_bar=0.1)
    lt = LeadTimeProfile(mean_lead_days=14.0)
    g = engine.find_g(stats, lt, on_order=10000.0)
    assert g == 0.0


# --- 1.6 #6 -----------------------------------------------------------------

def test_intermittent_reorder_point_grows_with_lead_sigma():
    engine = IntermittentEngine()
    stats = SKUStats(
        weekly_demand=2.0, n_events=10, p_bar=10.0, z_bar=4.0,
        sigma_z=0.5, sigma_p=2.0,
    )
    lt_low = LeadTimeProfile(mean_lead_days=14.0, sigma_lead_days=1.0, n_orders=5)
    lt_high = LeadTimeProfile(mean_lead_days=14.0, sigma_lead_days=5.0, n_orders=5)
    rop_low = engine.reorder_point(stats, lt_low)
    rop_high = engine.reorder_point(stats, lt_high)
    assert rop_high > rop_low


# --- 1.6 #7 -----------------------------------------------------------------

def test_lead_time_profile_excludes_backorders():
    """Backorder-flagged orders do not move the learned mean."""
    realized = [10.0, 12.0, 11.0, 90.0, 85.0]
    flags = [False, False, False, True, True]
    profile = LeadTimeProfile.from_realized(realized, flags)
    assert profile.n_orders == 3
    assert profile.mean_lead_days == pytest.approx((10 + 12 + 11) / 3)


def test_lead_time_profile_empty_uses_fallback():
    profile = LeadTimeProfile.from_realized([], [], fallback_mean=15.0)
    assert profile.mean_lead_days == 15.0
    assert profile.n_orders == 0


# --- 1.6 #8 -----------------------------------------------------------------

def test_score_path_equivalence():
    """The score reported in the OrderRecommendation equals a direct
    RaidScorer.score(...) call, regardless of which engine was routed to.
    """
    cfg = DEFAULT_CONFIG
    router = Router(cfg)
    scorer = RaidScorer(cfg)

    # An intermittent SKU (p_bar = 10 > 7).
    stats = SKUStats.from_daily_sales([4] + [0] * 9 + ([4] + [0] * 9) * 9)
    lt = LeadTimeProfile(mean_lead_days=14.0, sigma_lead_days=2.0, n_orders=5)

    rec = router.recommend(stats, lt, on_hand=5.0, on_order=0.0)
    direct = scorer.score(stats, lt, on_order=0.0, safety_stock=0.0)

    assert rec.engine is Engine.INTERMITTENT
    assert rec.alert_score == direct


# --- 1.6 #9 -----------------------------------------------------------------

def test_classify_action_reorder_when_score_comfortable_but_qty_nonzero():
    """The subtle case: score < urgent floor but quantity > 0 -> REORDER."""
    assert classify_action(alert_score=0.8, quantity=50.0) is Action.REORDER
    assert classify_action(alert_score=1.2, quantity=10.0) is Action.REORDER


def test_classify_action_urgent():
    assert classify_action(alert_score=3.0, quantity=20.0) is Action.URGENT


def test_classify_action_overstocked():
    assert classify_action(alert_score=0.2, quantity=0.0) is Action.OVERSTOCKED


def test_classify_action_balanced():
    assert classify_action(alert_score=1.0, quantity=0.0) is Action.BALANCED


# --- Sanity: score is bounded as expected at edges ---------------------------

def test_logit_gate_suppresses_low_demand():
    """L should crush the score for very low weekly demand."""
    cfg = DEFAULT_CONFIG
    scorer = RaidScorer(cfg)
    stats = SKUStats(weekly_demand=0.1, n_events=10)
    lt = LeadTimeProfile(mean_lead_days=14.0)
    s = scorer.score(stats, lt, on_order=0.0)
    assert s < 0.2  # heavily suppressed


# --- raid_items cumulative trace -------------------------------------------

def test_raid_items_trace_decreases_as_shipments_land():
    """Each successive incoming PO should move the score toward / past balance,
    i.e. the cumulative trace is non-increasing as pipeline accumulates."""
    scorer = RaidScorer()
    stats = SKUStats(weekly_demand=20.0, n_events=20)
    etas = [7.0, 7.0, 7.0, 7.0]
    qtys = [10.0, 10.0, 10.0, 10.0]
    trace = scorer.raid_items(stats, etas, qtys)
    assert len(trace) == 4
    # More cumulative pipeline at the same ETA -> lower score.
    assert trace == sorted(trace, reverse=True)


def test_raid_items_empty():
    scorer = RaidScorer()
    stats = SKUStats(weekly_demand=20.0, n_events=20)
    assert scorer.raid_items(stats, [], []) == []


# --- GATE_FIND_G flag (legacy vs brief order sizing) -----------------------

def test_gate_find_g_orders_fewer_for_low_demand():
    """Legacy/default behavior (gate ON) solves T1*T2*L=1; for a low-demand SKU
    near L_CENTER that yields a SMALLER order than the ungated mode (gate OFF)."""
    stats = SKUStats(weekly_demand=4.0, n_events=20)  # d == L_CENTER -> L=0.5
    lt = LeadTimeProfile(mean_lead_days=14.0)

    brief_engine = FlowEngine(Config(GATE_FIND_G=False))
    legacy_engine = FlowEngine(Config(GATE_FIND_G=True))

    g_brief = brief_engine.find_g(stats, lt, on_order=0.0)
    g_legacy = legacy_engine.find_g(stats, lt, on_order=0.0)

    assert g_legacy < g_brief  # the gate suppresses the order for low demand


def test_gate_find_g_converges_for_high_demand():
    """For high demand (L ~ 1) the two modes should agree closely."""
    stats = SKUStats(weekly_demand=30.0, n_events=20)
    lt = LeadTimeProfile(mean_lead_days=14.0)
    g_brief = FlowEngine(Config(GATE_FIND_G=False)).find_g(stats, lt, on_order=0.0)
    g_legacy = FlowEngine(Config(GATE_FIND_G=True)).find_g(stats, lt, on_order=0.0)
    assert g_brief == pytest.approx(g_legacy, rel=0.02)
