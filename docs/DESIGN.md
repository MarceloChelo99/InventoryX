# InventoryX — Design Doc (v1 draft)

## Goal

A local Python app that lets any company **dealing in non-perishable, storable goods with a reorder cycle** track inventory + sales and get a per-SKU **order-health score** — generalizing the formula used in the tire-shop system. Ships a REST API for ingestion and a lightweight dashboard for insights.

## Target customer (ICP)

Businesses that:
- Sell discrete, countable units (SKUs).
- Restock from suppliers on a lead-time cycle (not made-to-order, not produced in-house just-in-time).
- Hold goods that don't expire or spoil within the reorder horizon — tires, auto parts, hardware, electronics, apparel, packaged consumer goods, industrial supplies.

Explicitly out of scope: perishables (groceries, flowers), services, made-to-order manufacturing, consignment. The scoring formula assumes stock-and-flow dynamics that break for these.

## Non-goals (v1)

- Multi-tenant SaaS, auth, billing.
- POS-specific integrations (Shopify, Square, etc.). The REST API is the integration surface; adapters come later.
- Sophisticated ML forecasting. v1 uses a simple weighted moving average — same as the original.
- Purchase-order workflow (approvals, receiving). v1 only *records* orders; it doesn't manage them.

## Architecture

Single-process Python app, runs locally.

- **API:** FastAPI. REST + auto OpenAPI docs.
- **Storage:** SQLite via SQLAlchemy. One file, zero setup. Postgres later if needed.
- **Models:** Pydantic for I/O, SQLAlchemy for persistence.
- **Analytics:** numpy + pandas. Pure functions; no framework lock-in.
- **Dashboard:** deferred to phase 3. Likely Streamlit (fastest path) or a small React SPA against the API. Picked when phase 2 lands.

```
inventoryx/
  api/          # FastAPI routers
  db/           # SQLAlchemy models, migrations
  domain/       # Pure logic: scoring, forecasting
  services/     # Orchestration: ingest, snapshot
  cli/          # `inventoryx run`, `inventoryx ingest`
  tests/
```

## Data model

Entities (SQLite tables):

- **Company** — single row in local mode, but modeled so multi-tenancy is a future migration, not a rewrite.
- **Sku** — `id`, `company_id`, `code`, `name`, `category`, `supplier_id`, `unit_cost`, `safety_stock`, `lead_time_days`. The last two have company-level defaults so new SKUs aren't blockers.
- **Supplier** — `id`, `name`, `default_lead_time_days`.
- **SaleEvent** — `sku_id`, `quantity`, `occurred_at`, `unit_price`, `source` (e.g. "shopify", "manual", "csv"). Append-only.
- **StockSnapshot** — `sku_id`, `on_hand`, `on_order`, `observed_at`. Append-only; current state is the latest row per SKU.
- **PurchaseOrder** — `sku_id`, `quantity`, `ordered_at`, `expected_at`, `received_at`. Drives `on_order` projection.

Why append-only sales + snapshots: scoring and forecasting need history, not just current state. Mutating-in-place loses that.

## Scoring engine — generalized

The tire-shop formula, with variables renamed for clarity:

```
d   = demand_rate           # units / day, from forecast
g   = on_hand               # current stock
o   = on_order              # pending POs not yet received
a   = lead_time_days        # SKU or supplier default
s   = safety_stock          # buffer units
```

The formula itself is preserved (it's the working IP — generalization is about *feeding* it cleanly, not changing it):

```python
demand        = 4*d + 1
supply        = ((g + o + 1) / (a/7 + 1)) + s + 1
order_demand  = (12 * d * a) / 90 + 1
order_supply  = g + o + 1

term1     = log(demand / supply + e - 0.5)
term2     = log(order_demand / order_supply + e - 1)
logit     = 1 / (1 + exp(-d + 4))      # high-demand items weighted more

score = term1 * term2 * logit - 1
```

**Interpretation** (carried over from the tire system):
- `score ≈ 1` → balanced
- `score ≥ 2` → underordered (action: reorder)
- `score < 1` → overordered (action: hold / discount)

The score is computed per-SKU on demand and cached on `StockSnapshot` writes.

### What generalization changes

1. **`d` (demand_rate) is now pluggable.** v1 ships a weighted moving average over the last N days (default 30, configurable per SKU). The interface is `Forecaster.predict(sku, as_of) -> float`. Future: seasonal, ARIMA, ML — same interface.
2. **`a` (lead time) cascades.** SKU-level → supplier default → company default. The tire system hardcoded this; here it's resolved at score time.
3. **`s` (safety stock) cascades the same way.** Optional: derive from observed demand variance instead of a manual number (phase 2).
4. **Units are explicit.** The formula assumes `d` is daily and `a` is days. The data model stores them that way; conversions happen at ingest, not in the formula.
5. **Constants are named.** Magic numbers in the original (`4`, `12`, `90`, `0.25`, etc.) get named constants in `domain/scoring.py` with the tire-shop values as defaults. Tunable per company later, not in v1.

### What we do NOT change

The functional form. It's hand-tuned and works. Refitting needs labeled data we don't have yet.

## Forecasting (v1)

Weighted moving average:

```
d = sum(sales[i] * w[i]) / sum(w[i])   over last N days
w[i] = exp(-i / tau)                    # newer days weighted more
```

Defaults: `N = 30`, `tau = 14`. Configurable per SKU.

Edge cases:
- SKU with < 7 days of history → fall back to category average; flag in response.
- Long zero-sale stretches → return 0 (the logit term in scoring handles low-demand items).

## REST API surface (v1)

```
POST /sales              # ingest one or many SaleEvents
POST /stock/snapshot     # ingest one or many StockSnapshots
POST /purchase-orders    # record a PO
PATCH /purchase-orders/{id}/receive

GET  /skus                       # list with current score + state
GET  /skus/{code}/score          # detailed breakdown (term1, term2, logit, d, etc.)
GET  /skus/{code}/history        # sales + snapshots over time
GET  /insights/reorder           # SKUs with score >= 2, sorted
GET  /insights/overstock         # SKUs with score < 1, sorted
```

All POST endpoints accept arrays — bulk ingest is the common case for REST integrations.

## Analytics / insights (beyond the score)

These are what "any company" wants on top of the score:

- **Reorder list** — SKUs ranked by score, with suggested order qty = `order_demand - order_supply`.
- **Overstock list** — same but inverted; flags capital tied up.
- **Forecast vs. actual** — rolling MAPE per SKU. Tells the user when the forecast is failing and which SKUs need attention.
- **Lead-time drift** — actual vs. expected (`received_at - ordered_at` vs. supplier default). Catches suppliers slipping.
- **Stockout events** — derived from snapshots where `on_hand` hit 0; correlated with score to validate the model.

The dashboard renders these. The API exposes them as `GET /insights/*` JSON.

## Milestones

1. **Phase 1 — Core (target: working CLI + API)**
   - Data model, migrations, FastAPI skeleton.
   - Ingest endpoints + CSV bulk import via CLI.
   - Scoring engine + weighted-MA forecaster.
   - `GET /skus`, `GET /skus/{code}/score`, `GET /insights/reorder|overstock`.
   - Unit tests for scoring against tire-shop reference values (regression guard).

2. **Phase 2 — Analytics depth**
   - Forecast accuracy tracking.
   - Lead-time drift, stockout correlation.
   - Variance-derived safety stock (opt-in).

3. **Phase 3 — Dashboard**
   - Pick Streamlit vs. React based on whether the user wants to ship this to other companies (React) or just run it themselves (Streamlit).

## Open questions

1. **Multi-currency / multi-warehouse** — out of scope for v1, but the data model should not preclude it. Recommend: add `warehouse_id` to `StockSnapshot` now (nullable), defer aggregation logic to phase 2.
2. **Score caching strategy** — recompute on every snapshot write, or lazily on read? Lean toward eager (cheap; keeps `GET /skus` fast).
3. **Original constants tunable per company?** Not in v1. Revisit when there's evidence the tire-shop defaults underperform on a different domain.
4. ~~What does "any company" actually look like?~~ **Resolved:** non-perishable storable goods with a reorder cycle. See ICP section above.

---

*Next step after sign-off: scaffold phase 1 — repo layout, deps, data model, and the scoring module with reference tests.*
