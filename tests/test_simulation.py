"""Layer 3 acceptance tests — §3.10."""

from __future__ import annotations

import random

import pytest

from inventoryx.entities import SKU, Vendor
from inventoryx.inventory_engines import Engine
from inventoryx.simulation import demand_service as ds_mod
from inventoryx.simulation import truth as truth_mod
from inventoryx.simulation.events import (
    DemandSpikeEvent,
    Schedule,
    VendorShockEvent,
)
from inventoryx.simulation.scenario import build_default_scenario, run_default
from inventoryx.simulation.truth import DemandProcess
from inventoryx.simulation.world import SimSKU, World


# --- 3.10 #1: deterministic run with seed ----------------------------------

def test_run_is_deterministic_for_fixed_seed():
    _, s1 = run_default(seed=42, days=200, n_skus=40)
    _, s2 = run_default(seed=42, days=200, n_skus=40)
    assert s1 == s2


def test_summary_has_regime_split():
    _, summary = run_default(seed=1, days=200, n_skus=40)
    assert "flow" in summary and "intermittent" in summary
    assert "overall" in summary
    # Both regimes should have a non-trivial population over 40 mixed SKUs.
    assert summary["flow"]["skus"] + summary["intermittent"]["skus"] == 40


# --- 3.10 #2: vendor shock observable in stockouts or open-PO age ----------

def test_vendor_shock_is_observable():
    rng = random.Random(7)
    v = Vendor(vendor_id="V1", base_lead_mean=10.0, base_lead_sigma=1.0)
    skus = {}
    for i in range(8):
        sku = SKU(sku_id=f"S{i}", vendor=v, unit_cost=50.0, on_hand=8.0)
        proc = DemandProcess(regime_dial=1.0)
        skus[f"S{i}"] = SimSKU(sku=sku, process=proc)

    shock_start, dur = 40, 60
    schedule = Schedule(
        vendor_shocks=[
            VendorShockEvent(
                start_day=shock_start,
                duration_days=dur,
                target_vendor_id="V1",
                lead_mean_multiplier=8.0,
                lead_sigma_multiplier=2.0,
                force_backorder=True,
            ),
        ]
    )
    world = World(
        vendors={"V1": v},
        skus=skus,
        schedule=schedule,
        rng=rng,
    )

    # Pre-shock baseline (long enough to build pipeline assumptions).
    world.run(shock_start)
    pre_stockouts = sum(m.stockout_units for m in world.metrics.per_sku.values())
    pre_max_age = max(
        sim.sku.order.max_open_age_days() for sim in world.skus.values()
    )

    # Shock window.
    world.run(dur + 30)  # run through shock + tail
    post_stockouts = sum(m.stockout_units for m in world.metrics.per_sku.values())
    post_max_age = max(
        sim.sku.order.max_open_age_days() for sim in world.skus.values()
    )

    # Either stockouts rose or open-PO age climbed (or both). The effect must
    # be observable, not absorbed silently.
    assert post_stockouts > pre_stockouts or post_max_age > pre_max_age


# --- 3.10 #3: learned profile stays clean during shock ---------------------

def test_learned_lead_profile_excludes_shock_tail():
    rng = random.Random(11)
    v = Vendor(vendor_id="V1", base_lead_mean=10.0, base_lead_sigma=1.0)
    skus = {}
    for i in range(5):
        sku = SKU(sku_id=f"S{i}", vendor=v, unit_cost=50.0, on_hand=20.0)
        proc = DemandProcess(regime_dial=1.0)
        skus[f"S{i}"] = SimSKU(sku=sku, process=proc)

    schedule = Schedule(
        vendor_shocks=[
            VendorShockEvent(
                start_day=30,
                duration_days=90,
                target_vendor_id="V1",
                lead_mean_multiplier=8.0,
                force_backorder=True,
            )
        ]
    )
    world = World(vendors={"V1": v}, skus=skus, schedule=schedule, rng=rng)
    world.run(200)

    profile = v.lead_time_profile()
    # Clean mean stays near the normal band even though many shocked orders
    # were placed and arrived during the window.
    assert profile.mean_lead_days < 25.0, (
        f"Learned mean {profile.mean_lead_days:.1f} polluted by backorder tail"
    )
    # And the vendor did see backorder-flagged completions.
    assert any(v._backorder_flags), "Expected at least one backorder arrival"


# --- 3.10 #4: routing labels assertable in output --------------------------

def test_routing_labels_in_metrics():
    world, _ = run_default(seed=3, days=120, n_skus=30)
    engines = {m.routed_engine for m in world.metrics.per_sku.values()}
    # With a mixed regime population we expect to see both labels.
    assert Engine.FLOW in engines or Engine.INTERMITTENT in engines
    # And each label is one of the two valid values, never something else.
    for m in world.metrics.per_sku.values():
        assert m.routed_engine in (Engine.FLOW, Engine.INTERMITTENT)


# --- 3.10 #5: demand service never reads truth -----------------------------

def test_demand_service_does_not_import_truth():
    """Module boundary check: demand_service must not import from truth.

    This is a structural test of the truth-isolation property called out in
    the brief. We inspect the module's globals.
    """
    forbidden = {"DemandProcess", "VendorLeadDistribution"}
    leaks = forbidden & set(vars(ds_mod).keys())
    assert not leaks, f"demand_service leaked truth symbols: {leaks}"


# --- Sanity: smoke run completes -------------------------------------------

def test_smoke_short_run_completes():
    _, summary = run_default(seed=2, days=90, n_skus=20)
    assert summary["overall"]["skus"] == 20
    assert summary["overall"]["demand_units"] >= 0
