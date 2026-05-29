"""
Default scenario builder.

~200 SKUs, mixed regimes, 5 vendors. 104-week (728-day) horizon. 2 events
including at least one vendor supply shock. Seeded.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Dict, List, Tuple

from inventoryx.entities import SKU, Vendor
from inventoryx.simulation.events import (
    DemandSpikeEvent,
    Schedule,
    VendorShockEvent,
)
from inventoryx.simulation.truth import DemandProcess, VendorLeadDistribution
from inventoryx.simulation.world import SimSKU, World


def _build_vendors(n: int = 5) -> Dict[str, Vendor]:
    bands = [
        (10.0, 2.0),
        (12.0, 3.0),
        (15.0, 4.0),
        (8.0, 1.5),
        (14.0, 3.5),
    ]
    vendors: Dict[str, Vendor] = {}
    for i in range(n):
        mean, sigma = bands[i % len(bands)]
        vid = f"V{i+1}"
        vendors[vid] = Vendor(
            vendor_id=vid,
            country=["US", "CA", "MX", "DE", "JP"][i % 5],
            base_lead_mean=mean,
            base_lead_sigma=sigma,
        )
    return vendors


def _build_skus(
    n: int,
    vendors: Dict[str, Vendor],
    rng: random.Random,
) -> Dict[str, SimSKU]:
    """Mix regimes deliberately:
        - ~40% dense flow (regime_dial 1)
        - ~30% weekly-ish (regime_dial 5-7)
        - ~30% lumpy intermittent (regime_dial 10-20)
    """
    vendor_ids = list(vendors.keys())
    skus: Dict[str, SimSKU] = {}
    for i in range(n):
        u = rng.random()
        if u < 0.4:
            dial = rng.uniform(1.0, 1.5)
            burst = 4
        elif u < 0.7:
            dial = rng.uniform(4.0, 7.0)
            burst = 4
        else:
            dial = rng.uniform(10.0, 20.0)
            burst = 4
        proc = DemandProcess(regime_dial=dial, burst_typical=burst)
        vendor_id = vendor_ids[i % len(vendor_ids)]
        vendor = vendors[vendor_id]
        sku_id = f"S{i+1:03d}"
        sku = SKU(
            sku_id=sku_id,
            vendor=vendor,
            unit_cost=rng.uniform(40.0, 250.0),
            on_hand=rng.uniform(8.0, 40.0),
            safety_stock=0.0,
        )
        skus[sku_id] = SimSKU(sku=sku, process=proc)
    return skus


def build_default_scenario(seed: int = 1, n_skus: int = 200) -> World:
    rng = random.Random(seed)
    vendors = _build_vendors(5)
    skus = _build_skus(n_skus, vendors, rng)

    # Two events:
    # 1. Plant fire at V3 around mid-run for 60 days.
    # 2. Demand spike on one SKU served by a different vendor.
    spike_sku = next(iter(skus))
    schedule = Schedule(
        vendor_shocks=[
            VendorShockEvent(
                start_day=300,
                duration_days=60,
                target_vendor_id="V3",
                lead_mean_multiplier=6.0,
                lead_sigma_multiplier=2.0,
                force_backorder=True,
            ),
        ],
        demand_spikes=[
            DemandSpikeEvent(
                start_day=120,
                duration_days=45,
                target_sku_id=spike_sku,
                rate_multiplier=2.5,
            ),
        ],
    )

    # Optional backorder injection per vendor (very small base rate).
    vlds = {
        vid: VendorLeadDistribution(backorder_chance=0.01) for vid in vendors
    }

    return World(
        vendors=vendors,
        skus=skus,
        schedule=schedule,
        rng=rng,
        vendor_lead_distributions=vlds,
    )


def run_default(seed: int = 1, days: int = 728, n_skus: int = 200) -> Tuple[World, dict]:
    world = build_default_scenario(seed=seed, n_skus=n_skus)
    metrics = world.run(days)
    return world, metrics.summary()
