# InventoryX — Services Concept Map

A map of the "services" in InventoryX and how they relate to one another.
InventoryX is a pure-logic Python library organized in **three independently
testable layers**:

1. **Engines** (`inventoryx/inventory_engines.py`) — stateless ordering math.
2. **Domain** (`inventoryx/entities.py`, `inventoryx/pipeline.py`) — mutable
   inventory objects: SKUs, vendors, and the purchase-order pipeline.
3. **Simulation** (`inventoryx/simulation/`) — a discrete-event harness that
   drives the domain through the engines, against a hidden ground truth.

A deliberate isolation boundary runs through the simulation layer: the
**forecaster** (`DemandService`) observes only realized *sales*, while the
hidden **truth** (`DemandProcess`, `VendorLeadDistribution`) generates the
real demand and lead times. The forecaster never imports truth — a separation
enforced by a test — so engines must react to lagged, honest signals.

---

## 1. High-level concept map

```mermaid
graph TD
    subgraph L3["Layer 3 — Simulation harness"]
        World["World<br/><i>daily-tick orchestrator</i>"]
        Datex["Datex<br/><i>clock / decision days</i>"]
        Schedule["Schedule<br/><i>event registry</i>"]
        DemandService["DemandService<br/><i>honest forecaster</i>"]
        RunMetrics["RunMetrics<br/><i>analytics accumulator</i>"]
        subgraph Truth["Hidden ground truth (walled off)"]
            DemandProcess["DemandProcess<br/><i>true demand</i>"]
            VLD["VendorLeadDistribution<br/><i>true lead times</i>"]
        end
    end

    subgraph L1["Layer 1 — Ordering engines (stateless)"]
        Router["Router<br/><i>regime dispatcher</i>"]
        FlowEngine["FlowEngine"]
        IntermittentEngine["IntermittentEngine"]
        RaidScorer["RaidScorer<br/><i>alert score</i>"]
        Classify["classify_action()"]
    end

    subgraph L2["Layer 2 — Domain & pipeline"]
        SKU["SKU<br/><i>aggregate root</i>"]
        Vendor["Vendor<br/><i>lead-time + shocks</i>"]
        Order["Order<br/><i>PO pipeline</i>"]
    end

    %% Orchestration edges
    World -->|advances| Datex
    World -->|reads events| Schedule
    World -->|observe_sale| DemandService
    World -->|records| RunMetrics
    World -->|owns / ticks| SKU
    World -->|applies shocks| Vendor
    World -->|realize_day| DemandProcess
    World -->|maybe_backorder| VLD
    World -->|recommend| Router

    %% Decision path
    DemandService -.->|SKUStats| Router
    Vendor -.->|LeadTimeProfile| Router
    Router --> FlowEngine
    Router --> IntermittentEngine
    Router --> RaidScorer
    Router --> Classify

    %% Domain wiring
    SKU -->|owns| Order
    SKU -->|belongs to| Vendor
    Order -.->|Arrival| Vendor

    classDef truth fill:#fde,stroke:#c39,stroke-width:1px;
    class DemandProcess,VLD truth;
    classDef engine fill:#def,stroke:#39c,stroke-width:1px;
    class Router,FlowEngine,IntermittentEngine,RaidScorer,Classify engine;
```

---

## 2. Service catalog

| Service | Layer | File | Role |
|---|---|---|---|
| **Router** | Engines | `inventory_engines.py` | Picks an engine per SKU on inter-arrival time (`p_bar`); assembles the recommendation. |
| **FlowEngine** | Engines | `inventory_engines.py` | Order quantity for high-frequency demand (root-find `find_g` via `scipy.brentq`). |
| **IntermittentEngine** | Engines | `inventory_engines.py` | Croston/SBA reorder point + safety stock for lumpy demand. |
| **RaidScorer** | Engines | `inventory_engines.py` | Cross-cutting alert score; per-shipment pipeline-health trace (`raid_items`). |
| **classify_action** | Engines | `inventory_engines.py` | Maps (score, qty) → `OVERSTOCKED / BALANCED / REORDER / URGENT`. |
| **SKU** | Domain | `entities.py` | Aggregate root: on-hand stock, safety stock, owns its `Order`, places POs. |
| **Vendor** | Domain | `entities.py` | Lead-time distribution + shock state; learns realized lead times. |
| **Order** | Pipeline | `pipeline.py` | Per-SKU PO pipeline; advances time; emits `Arrival` records. |
| **DemandService** | Simulation | `simulation/demand_service.py` | Honest forecaster over a rolling window of *sold* units → `SKUStats`. |
| **World** | Simulation | `simulation/world.py` | Daily-tick orchestrator wiring every other service together. |
| **Schedule** | Simulation | `simulation/events.py` | Day-indexed registry of vendor shocks and demand spikes. |
| **RunMetrics** | Simulation | `simulation/metrics.py` | Per-SKU + per-engine fill-rate / holding-cost accumulator. |
| **Datex** | Simulation | `simulation/clock.py` | Time authority; flags weekly decision days. |
| **DemandProcess** | Truth | `simulation/truth.py` | Hidden true per-SKU demand generator. |
| **VendorLeadDistribution** | Truth | `simulation/truth.py` | Hidden true per-vendor lead-time generator / backorder injector. |

---

## 3. The Router → engines relationship

The `Router` is the single decision service. It is **stateless** and composes
the two engines plus the scorer. Wiring is constructor injection (no DI
container), so any part can be swapped for a test double.

```mermaid
graph LR
    Stats["SKUStats<br/>(from DemandService)"] --> Router
    LT["LeadTimeProfile<br/>(from Vendor)"] --> Router
    OnHand["on_hand / on_order / safety_stock<br/>(from SKU)"] --> Router

    Router -->|p_bar < threshold| FlowEngine
    Router -->|otherwise| IntermittentEngine
    Router --> RaidScorer
    FlowEngine --> Rec
    IntermittentEngine --> Rec
    RaidScorer -->|alert_score| Rec
    Router --> Classify["classify_action()"]
    Classify -->|action| Rec["OrderRecommendation<br/>(quantity, engine, score, action)"]
```

---

## 4. World orchestration — who calls whom each tick

`World` is the hub. Per day it advances the pipeline, realizes hidden demand,
feeds the forecaster only what *sold*, and weekly asks the `Router` what to
order. The truth services sit behind the dashed isolation line.

```mermaid
sequenceDiagram
    participant World
    participant Schedule
    participant Vendor
    participant SKU
    participant Order
    participant Truth as DemandProcess
    participant DS as DemandService
    participant Router
    participant Metrics as RunMetrics

    World->>Schedule: events for today
    World->>Vendor: apply_shock / recover
    World->>Order: next_day() → arrivals
    World->>SKU: receive_arrivals()
    SKU->>Vendor: record_arrival(lead, was_backorder)
    World->>Truth: realize_day(rng)  [hidden]
    World->>SKU: fill_sale(demanded)
    World->>DS: observe_sale(sold)   [honest signal]
    World->>Metrics: record_day()

    Note over World: weekly decision day
    World->>DS: profile_for(sku) → SKUStats
    World->>Vendor: lead_time_profile()
    World->>Router: recommend(...)
    Router-->>World: OrderRecommendation
    World->>Vendor: sample_lead_time() / backorder?
    World->>SKU: place_po(qty, lead, status)
    SKU->>Order: add(Po)
    World->>Metrics: record_order()
```

---

## 5. The isolation boundary (key invariant)

```mermaid
graph LR
    subgraph Hidden["Hidden truth — generates reality"]
        DemandProcess
        VLD["VendorLeadDistribution"]
    end
    subgraph Observable["Observable — what the business sees"]
        DemandService
    end

    DemandProcess -->|real demand| World
    World -->|only what SOLD| DemandService
    DemandProcess -. "never imported" .-x DemandService

    style Hidden fill:#fde,stroke:#c39
    style Observable fill:#dfd,stroke:#3a3
```

`DemandService` records **sold** units, not **demanded** units — an upstream
POS can't see lost sales it never rang up. During stockouts and shocks the
forecaster therefore lags the truth, and the engines must cope. The boundary
is enforced by `test_demand_service_does_not_import_truth`.

---

## 6. Data objects passed between services

| Object | Produced by | Consumed by |
|---|---|---|
| `SKUStats` | `DemandService.profile_for` | `Router`, `FlowEngine`, `IntermittentEngine`, `RaidScorer` |
| `LeadTimeProfile` | `Vendor.lead_time_profile` | `Router` and engines |
| `OrderRecommendation` | `Router.recommend` | `World._decide` |
| `Po` | `SKU.place_po` → `Order.add` | `Order` pipeline |
| `Arrival` | `Order.next_day` | `SKU.receive_arrivals` → `Vendor.record_arrival` |
| `Shock` | `World._apply_events_for_today` | `Vendor.apply_shock` |

---

*Generated for branch `claude/services-concept-map-xXdNH`. Source of truth:
`inventoryx/` — see `docs/DESIGN.md` for architectural rationale.*
