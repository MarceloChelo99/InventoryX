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
   proposed `T1·T2 = 1` (gate dropped, since the router already decided the
   SKU is flow-regime). These differ for low-demand SKUs near `L_CENTER`:
   the gated version orders fewer units. **Default is `True` — the original
   tire-shop sizing; set `False` for the brief's ungated mode.**

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

4. **`inventoryx/services/`** — the production scoring path (real data, not
   the harness). Runs the *same* validated engines on a company's append-only
   history.
   - `forecaster.py` — `Forecaster`: turns timestamped `SaleEvent`s into
     `SKUStats` via the **same** `SKUStats.from_daily_sales` the sim uses, so
     the engines get identical inputs in both worlds (a parity test pins this).
     Also exposes the design doc's weighted-MA `predict()` daily rate.
   - `sources.py` — `InventoryDataSource` protocol + an `InMemorySource` stub.
     The future SQLite layer is just another implementation of this seam.
   - `scoring_service.py` — `ScoringService`: fetch a SKU's history + state,
     call `Router.recommend`, and expose `reorder_list` / `overstock_list`.

5. **`inventoryx/db/`** — SQLite/SQLAlchemy persistence (the design doc's data
   model). The drop-in for the data-source seam above.
   - `models.py` — the six tables: `Company`, `Supplier`, `Sku`, `SaleEvent`,
     `StockSnapshot`, `PurchaseOrder`. `lead_time_days` / `safety_stock` are
     nullable and cascade SKU → supplier → company at score time.
   - `source.py` — `SqlInventoryDataSource`: implements `InventoryDataSource`
     by reading the tables and returning the same dataclasses scoring already
     consumes. A test asserts it scores **identically** to `InMemorySource` on
     the same data.
   - `repository.py` — write-side ingestion helpers (the seam the REST
     `POST /sales`, `/stock/snapshot`, `/purchase-orders` call).
   - `migrations/` — Alembic. `alembic upgrade head` builds the schema.

6. **`inventoryx/api/`** — FastAPI REST surface. A thin wrapper: ingestion
   writes through `Repository`, insights read through `ScoringService`, and no
   inventory math lives here. See [Run the REST API](#run-the-rest-api).

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

All 66 tests should pass.

## Set up the database

```bash
# Create the schema (defaults to sqlite:///inventoryx.db).
alembic upgrade head

# Point at another database without editing alembic.ini:
INVENTORYX_DATABASE_URL=sqlite:///prod.db alembic upgrade head
```

Then score real data — same `ScoringService`, just a SQL-backed source:

```python
from datetime import date
from inventoryx.db import make_engine, make_session_factory, Repository, SqlInventoryDataSource
from inventoryx.services import ScoringService

engine = make_engine("sqlite:///inventoryx.db")
Session = make_session_factory(engine)

with Session() as s:
    repo = Repository(s)
    co = repo.create_company("Acme Tires")
    sku = repo.create_sku(co, code="TIRE-205", name="205/55R16", safety_stock=10)
    repo.record_sale(sku, quantity=8, occurred_at=date(2026, 5, 28))
    repo.record_snapshot(sku, on_hand=12, on_order=0, observed_at=date(2026, 5, 29))
    s.commit()

    svc = ScoringService(SqlInventoryDataSource(s, company_id=co.id))
    for hot in svc.reorder_list(as_of=date(2026, 5, 29)):
        print(hot.sku_id, hot.recommendation.action, hot.recommendation.quantity)
```

## Run the REST API

```bash
# Defaults to sqlite:///inventoryx.db; override with INVENTORYX_DATABASE_URL.
inventoryx-api            # serves on 127.0.0.1:8000
# Interactive docs at http://127.0.0.1:8000/docs
```

`inventoryx/api/` is a thin FastAPI layer over the existing services — no
inventory math lives there. Ingestion writes through `Repository`; insights
read through `ScoringService`. In single-company (local) mode the `company_id`
is inferred; pass `?company_id=` once there's more than one.

| Method & path | Purpose |
|---|---|
| `POST /companies`, `/suppliers`, `/skus` | Set up the catalog |
| `POST /sales` | Bulk-ingest sale events |
| `POST /stock/snapshot` | Bulk-ingest stock snapshots |
| `POST /purchase-orders` | Record POs |
| `PATCH /purchase-orders/{id}/receive` | Mark a PO received (yields a lead time) |
| `GET /skus` | Every SKU with its current recommendation + position |
| `GET /skus/{code}/score` | Detailed score + formula intermediates |
| `GET /skus/{code}/history` | Raw sales + snapshots |
| `GET /insights/reorder` | SKUs to order, most urgent first |
| `GET /insights/overstock` | SKUs with capital tied up |

```bash
curl -X POST localhost:8000/companies -H 'content-type: application/json' \
  -d '{"name":"Acme Tires","default_lead_time_days":14}'
curl -X POST localhost:8000/skus -H 'content-type: application/json' \
  -d '{"code":"TIRE-205","name":"205/55R16","safety_stock":10}'
curl -X POST localhost:8000/sales -H 'content-type: application/json' \
  -d '[{"sku_code":"TIRE-205","quantity":8,"occurred_at":"2026-05-28"}]'
curl localhost:8000/insights/reorder
```

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

Or let `ScoringService` do the wiring from raw, append-only history:

```python
from datetime import date
from inventoryx import ScoringService, InMemorySource

src = InMemorySource()
src.add_sale("TIRE-205", quantity=8, occurred_at=date(2026, 5, 28))
# ... more SaleEvents ...
src.set_stock_state("TIRE-205", on_hand=12, on_order=0, safety_stock=10)
src.add_lead_observation("TIRE-205", lead_days=14)

svc = ScoringService(src)
rec = svc.score_sku("TIRE-205", as_of=date(2026, 5, 29))

for hot in svc.reorder_list(as_of=date(2026, 5, 29)):
    print(hot.sku_id, hot.recommendation.action, hot.recommendation.quantity)
```

`InMemorySource` is a stub; swap in a SQLite-backed `InventoryDataSource`
later without touching `ScoringService`.

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
