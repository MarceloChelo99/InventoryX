# InventoryX

Order-health scoring and discrete-event inventory simulation for distributors
of non-perishable, storable goods with reorder cycles.

## What's here

Three layers, each independently testable:

1. **`inventoryx/inventory_engines.py`** — the engine module.
   - `RaidScorer` — the cross-cutting alert score, plus `raid_items` (the
     cumulative per-shipment timeline trace from the legacy system).
   - `FlowEngine` — flow-regime suggested-order quantity via `find_g`
     (root-find on T1·T2 = 1 using `scipy.optimize.brentq`).
   - `IntermittentEngine` — Croston/SBA reorder point for lumpy demand.
   - `Router` — picks the right engine per SKU on `p_bar` (with a
     demand-level fallback when too few events have been observed).
   - `classify_action` — resolves score + quantity into one of
     `OVERSTOCKED / BALANCED / REORDER / URGENT`.

   **`find_g` gating (`Config.GATE_FIND_G`)** — the legacy code solved
   `T1·T2·L = 1` (logit gate kept in the order-sizing root-find); the brief
   specifies `T1·T2 = 1` (gate dropped, since the router already decided the
   SKU is flow-regime). These differ for low-demand SKUs near `L_CENTER`:
   the gated version orders fewer units. **Default is `False` (brief
   behavior); set `True` to restore the original tire-shop sizing.**

2. **`inventoryx/entities.py` + `inventoryx/pipeline.py`** — domain objects.
   - `Vendor` owns lead-time behavior and shock state. Correlated supply
     shocks across all SKUs from one vendor are first-class.
   - `SKU` holds `on_hand` and delegates pipeline queries to its `Order`.
   - `Po` — one purchase-order line. `SpecificOrder` — a sorted *bucket* of
     POs for one status category. `Order` — three buckets
     (confirmed / tentative / backordered) computed as views over one master
     list, advancing a day (or week) at a time. `Arrival` — a received-PO
     record.
   - **Backorder grace rule** (from the legacy code): a PO declared
     backordered is treated as *tentative* until it ages past
     `BACKORDER_GRACE_DAYS` (default 3) — fresh backorders get the benefit of
     the doubt before being declared a true bottleneck.
   - Backorders count toward pipeline *quantity* (they will eventually
     arrive) but are **excluded from learned lead-time statistics** — without
     this, one 90-day backorder poisons the SKU's normal-ops estimate forever.
   - The tire-specific status strings (`In-Transit`, `Cntr BO`, …) live only
     in `po_status_from_legacy()`, an ingestion-adapter seam — the core logic
     is status-string-agnostic.

3. **`inventoryx/simulation/`** — daily discrete-event simulation harness.
   - `truth.py` — the hidden demand and lead-time processes.
   - `demand_service.py` — the honest forecaster (sees only realized sales).
   - `world.py` — the daily-tick orchestrator with weekly decision cadence.
   - `events.py` — vendor supply shocks + demand spikes.
   - `metrics.py` — per-SKU + regime-split stockout / holding / fill-rate.
   - `scenario.py` — a 200-SKU, 5-vendor, 104-week default scenario.

### Truth-isolation caveat (important)

This sim is **Job 2**: validates that the routed engines behave correctly
across regimes and survive supply shocks. It is **not Job 3**: validating
real-world accuracy needs real backtest data — synthetic ground truth can
only tell you the engine responds to *its own* generator.

The simulation enforces a structural separation: `simulation/truth.py`
holds the hidden demand and lead-time processes, and
`simulation/demand_service.py` never imports from `truth.py`. A test
(`test_demand_service_does_not_import_truth`) asserts this. The engine only
ever sees what the forecaster digests from realized post-fact sales, with
the natural lag that implies during a shock.

## Install + test

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

All 39 tests should pass.

## Run the default scenario

```python
from inventoryx.simulation.scenario import run_default

world, summary = run_default(seed=1, days=728, n_skus=200)
print(summary)
# {
#   "overall":      {"skus": 200, "fill_rate": 0.97,  ...},
#   "flow":         {"skus": 129, "fill_rate": 0.988, ...},
#   "intermittent": {"skus":  71, "fill_rate": 0.687, ...},
# }
```

The regime split is the headline number — it tells you which engine is
failing, not just whether the aggregate is healthy.

## Public API (stable across simulation iterations)

```python
from inventoryx import (
    Router, RaidScorer, FlowEngine, IntermittentEngine,
    SKUStats, LeadTimeProfile, OrderRecommendation,
    Action, Engine, Confidence, Config, DEFAULT_CONFIG,
    classify_action,
)
```

Typical use in production code:

```python
router = Router()  # or Router(my_config) for tuned constants

rec = router.recommend(
    stats        = demand_service.profile_for(sku_id),
    lead_time    = sku.vendor.lead_time_profile(),
    on_hand      = sku.on_hand,
    on_order     = sku.on_order(),
    safety_stock = sku.safety_stock,
)

# rec.action       in {OVERSTOCKED, BALANCED, REORDER, URGENT}
# rec.quantity     units to order now (0 if no action)
# rec.alert_score  the cross-cutting score, in tire-shop units (1 = balanced)
# rec.engine       which engine produced the quantity
# rec.diagnostics  formula intermediates + routing rule
```

## Constants worth knowing

All in `Config` — tune via a custom `Config(...)` if needed.

| Name | Default | What it controls |
|---|---|---|
| `NORMAL_LEAD_TIME_DAYS` | 15.0 | T2 anchor (normal-ops lead ceiling) |
| `FLOW_THRESHOLD_DAYS` | 7.0 | `p_bar ≤ this` → flow regime |
| `MIN_EVENTS` | 4 | Below this, fall back to demand-level routing |
| `SBA_ALPHA` | 0.1 | Croston smoothing |
| `SERVICE_Z` | 1.65 | Service-level multiplier (~95%) |
| `L_CENTER`, `L_SLOPE` | 4.0, 1.0 | Demand-gate sigmoid (weekly demand) |

## Design notes

See `docs/DESIGN.md` for the architectural reasoning and the open questions
that drove this iteration.
