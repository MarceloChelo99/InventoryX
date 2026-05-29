"""
World — the daily-tick orchestrator.

Order of operations per tick (per the brief):
    1. Advance time           — pipeline POs move one day closer.
    2. Receive arrivals       — due POs convert to on_hand; record on Vendor.
    3. Realize demand         — draw true demand; fill from on_hand;
                                 record stockout units; tell the demand service.
    4. (weekly) Update stats  — demand_service.profile_for each SKU.
    5. (weekly) Decide        — router.recommend(...) using profiles.
    6. (weekly) Place PO      — qty -> SKU.place_po, lead drawn from Vendor.
    7. Record metrics.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from inventoryx.entities import SKU, Shock, Vendor
from inventoryx.inventory_engines import (
    DEFAULT_CONFIG,
    Config,
    Engine,
    OrderRecommendation,
    Router,
)
from inventoryx.simulation.clock import Datex
from inventoryx.simulation.demand_service import DemandService
from inventoryx.simulation.events import (
    DemandSpikeEvent,
    Schedule,
    VendorShockEvent,
)
from inventoryx.pipeline import POStatus
from inventoryx.simulation.metrics import RunMetrics
from inventoryx.simulation.truth import DemandProcess, VendorLeadDistribution


@dataclass
class SimSKU:
    """Wraps an SKU + its hidden truth process."""

    sku: SKU
    process: DemandProcess


@dataclass
class World:
    vendors: Dict[str, Vendor]
    skus: Dict[str, SimSKU]
    schedule: Schedule
    rng: random.Random
    decision_cadence_days: int = 7
    cfg: Config = field(default_factory=lambda: DEFAULT_CONFIG)
    vendor_lead_distributions: Dict[str, VendorLeadDistribution] = field(
        default_factory=dict
    )
    metrics: RunMetrics = field(default_factory=RunMetrics)
    demand_service: DemandService = field(
        default_factory=lambda: DemandService(window_days=90)
    )
    clock: Datex = field(default_factory=Datex)
    router: Optional[Router] = None
    # Tracks the rate_multiplier baseline so a spike can be reverted cleanly.
    _baseline_rate_multipliers: Dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.router is None:
            self.router = Router(self.cfg)
        # Snapshot baseline rates so spike teardown restores them.
        for sku_id, sim in self.skus.items():
            self._baseline_rate_multipliers[sku_id] = sim.process.rate_multiplier

    # --- event application -------------------------------------------------

    def _apply_events_for_today(self) -> None:
        day = self.clock.day
        # Start: apply shocks.
        for ev in self.schedule.vendor_shocks_starting(day):
            vendor = self.vendors.get(ev.target_vendor_id)
            if vendor is not None:
                vendor.apply_shock(
                    Shock(
                        lead_mean_multiplier=ev.lead_mean_multiplier,
                        lead_sigma_multiplier=ev.lead_sigma_multiplier,
                        force_backorder=ev.force_backorder,
                    )
                )
        for ev in self.schedule.demand_spikes_starting(day):
            sim = self.skus.get(ev.target_sku_id)
            if sim is not None:
                sim.process.set_rate_multiplier(
                    self._baseline_rate_multipliers[ev.target_sku_id]
                    * ev.rate_multiplier
                )
        # End: revert.
        for ev in self.schedule.vendor_shocks_ending(day):
            vendor = self.vendors.get(ev.target_vendor_id)
            if vendor is not None:
                vendor.recover()
        for ev in self.schedule.demand_spikes_ending(day):
            sim = self.skus.get(ev.target_sku_id)
            if sim is not None:
                sim.process.set_rate_multiplier(
                    self._baseline_rate_multipliers[ev.target_sku_id]
                )

    # --- daily tick --------------------------------------------------------

    def tick(self) -> None:
        self._apply_events_for_today()

        # Snapshot which SKUs route to which engine — used by metrics.
        for sku_id, sim in self.skus.items():
            sku = sim.sku
            # 1 + 2: advance pipeline and collect arrivals.
            arrivals = sku.order.next_day()
            sku.receive_arrivals(arrivals)

            # 3: realize today's true demand and fill from on_hand.
            demanded = sim.process.realize_day(self.rng)
            sold, _unfilled = sku.fill_sale(demanded)
            self.demand_service.observe_sale(sku_id, sold)
            # NOTE: demand_service sees SOLD, not DEMANDED. That's honest —
            # an upstream POS system can't see lost sales it never recorded.
            # The truth/forecaster lag during stockouts is part of the test.

            # 7: record day metrics (engine tag filled below on decision day).
            # Use a provisional engine tag if first time we see this SKU.
            provisional_engine = (
                self.metrics.per_sku[sku_id].routed_engine
                if sku_id in self.metrics.per_sku
                else Engine.INTERMITTENT
            )
            m = self.metrics.ensure(sku_id, provisional_engine, sku.unit_cost)
            m.record_day(demanded=demanded, sold=sold, on_hand=sku.on_hand)

        # 4-6: weekly decision step.
        if self.clock.is_decision_day(self.decision_cadence_days):
            self._decide()

        self.clock.next_day()

    def _decide(self) -> None:
        for sku_id, sim in self.skus.items():
            sku = sim.sku
            stats = self.demand_service.profile_for(sku_id)
            lt_profile = sku.vendor.lead_time_profile()

            rec: OrderRecommendation = self.router.recommend(
                stats=stats,
                lead_time=lt_profile,
                on_hand=sku.on_hand,
                on_order=sku.on_order(),
                safety_stock=sku.safety_stock,
            )

            # Update routing tag in metrics.
            m = self.metrics.ensure(sku_id, rec.engine, sku.unit_cost)

            if rec.quantity > 0:
                lead = sku.vendor.sample_lead_time(self.rng)
                backorder = sku.vendor.should_force_backorder()
                # Optional injection: vendor_lead_distributions can flip to a
                # backorder draw with probability backorder_chance.
                vld = self.vendor_lead_distributions.get(sku.vendor.vendor_id)
                if vld is not None and not backorder:
                    maybe = vld.maybe_backorder(self.rng)
                    if maybe is not None:
                        lead = maybe
                        backorder = True
                status = POStatus.BACKORDERED if backorder else POStatus.CONFIRMED
                sku.place_po(quantity=rec.quantity, lead_days=lead, status=status)
                m.record_order()

    # --- run loop ----------------------------------------------------------

    def run(self, days: int) -> RunMetrics:
        for _ in range(days):
            self.tick()
        return self.metrics
