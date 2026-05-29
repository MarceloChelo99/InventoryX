"""
The TRUTH layer — hidden generators for demand and lead times.

The engine must never see anything in this file directly. The only path
truth -> engine is: truth produces realized sales/arrivals -> demand_service
observes the realized series -> engine consumes the digested profile.

NOTE: No helpers from inventory_engines or demand_service are imported here,
on purpose. That isolation is what makes shock-response a real test rather
than a tautology.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Optional


# ----------------------------------------------------------------------------
# Per-SKU demand process — compound (inter-arrival x burst)
# ----------------------------------------------------------------------------


@dataclass
class DemandProcess:
    """Hidden daily demand process for one SKU.

    Sample-by-sample model:
        - Each day, with hazard 1/gap_mean, an event fires.
          (Gap-driven: countdown of days-until-next-event.)
        - When fired, burst size is drawn from a discrete distribution
          centered near `burst_typical`, with occasional 1 or 2 unit sales.

    regime_dial:
        - 1.0  : dense flow (events ~daily)
        - 7.0  : weekly-event lumpy
        - 14.0+: very lumpy intermittent

    Optional rate_multiplier lets external events (a demand spike) inflate
    demand without rebuilding the process. The engine never sees this — it
    only sees the realized sales that follow.
    """

    regime_dial: float = 1.0
    burst_typical: int = 4
    burst_p_typical: float = 0.7
    burst_p_one: float = 0.1
    burst_p_two: float = 0.2
    rate_multiplier: float = 1.0
    _days_until_next: int = 0

    def __post_init__(self) -> None:
        # Stagger initial fires so all SKUs don't sell on day 0.
        # Actual day count is set on first draw via the rng.
        self._days_until_next = -1  # sentinel: not yet primed

    # --- shock-style perturbations -----------------------------------------

    def set_rate_multiplier(self, x: float) -> None:
        self.rate_multiplier = max(x, 0.0)

    # --- daily draw --------------------------------------------------------

    def _effective_gap_days(self) -> float:
        # rate_multiplier > 1 -> events more frequent -> smaller gap.
        denom = self.rate_multiplier if self.rate_multiplier > 0 else 1e-9
        return max(self.regime_dial / denom, 1.0)

    def _prime(self, rng: random.Random) -> None:
        # Uniform stagger across the (rounded) gap.
        gap = self._effective_gap_days()
        self._days_until_next = rng.randint(0, max(int(round(gap)), 1))

    def _draw_burst(self, rng: random.Random) -> int:
        u = rng.random()
        if u < self.burst_p_one:
            return 1
        if u < self.burst_p_one + self.burst_p_two:
            return 2
        return self.burst_typical

    def realize_day(self, rng: random.Random) -> int:
        """Return today's realized true demand (units)."""
        if self._days_until_next < 0:
            self._prime(rng)
        if self._days_until_next > 0:
            self._days_until_next -= 1
            return 0
        # Fire today; schedule the next.
        size = self._draw_burst(rng)
        gap = self._effective_gap_days()
        # Geometric-ish: round next gap; clamp at >= 1 so we don't loop.
        # Add jitter so consecutive gaps aren't constant.
        jitter = rng.uniform(-0.3, 0.3)
        self._days_until_next = max(int(round(gap * (1.0 + jitter))) - 1, 0)
        return size


# ----------------------------------------------------------------------------
# Per-vendor lead time — drawn at order placement
# ----------------------------------------------------------------------------


@dataclass
class VendorLeadDistribution:
    """Hidden lead-time distribution for one vendor.

    Generates a normal-band sample by default; on shocked vendors, the
    Vendor object scales the parameters. We do NOT also re-implement
    shock scaling here — Vendor.sample_lead_time already handles it.

    This dataclass exists in case the sim wants to swap in a non-normal
    distribution; the default Vendor uses gauss(mean, sigma). We leave a
    backorder injection hook so a sim event can flip a single order into
    the backorder tail.
    """

    backorder_chance: float = 0.0
    backorder_min_days: float = 60.0
    backorder_max_days: float = 90.0

    def maybe_backorder(self, rng: random.Random) -> Optional[float]:
        """Return a backorder lead-time sample with probability backorder_chance.

        None means "use the vendor's normal sample."
        """
        if self.backorder_chance <= 0:
            return None
        if rng.random() < self.backorder_chance:
            return rng.uniform(self.backorder_min_days, self.backorder_max_days)
        return None
