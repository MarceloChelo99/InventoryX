"""
Shock definitions + a day-indexed schedule.

A shock perturbs TRUTH. The demand service is never told a shock occurred;
it only sees the realized data downstream and lags accordingly. That lag
is the test.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class VendorShockEvent:
    """A perturbation applied to a vendor for a window of days.

    start_day                  : day the shock begins (inclusive)
    duration_days              : window length
    target_vendor_id           : which vendor's SKUs get hit
    lead_mean_multiplier       : e.g. 8.0 for plant fire (12 -> 96)
    lead_sigma_multiplier      : usually >= 1.0 during a shock
    force_backorder            : flag new POs during the window as backorder
    """

    start_day: int
    duration_days: int
    target_vendor_id: str
    lead_mean_multiplier: float = 1.0
    lead_sigma_multiplier: float = 1.0
    force_backorder: bool = False

    def active_on(self, day: int) -> bool:
        return self.start_day <= day < self.start_day + self.duration_days


@dataclass
class DemandSpikeEvent:
    """Inflates a single SKU's true demand rate for a window."""

    start_day: int
    duration_days: int
    target_sku_id: str
    rate_multiplier: float = 2.0

    def active_on(self, day: int) -> bool:
        return self.start_day <= day < self.start_day + self.duration_days


@dataclass
class Schedule:
    """A day-indexed collection of pending events."""

    vendor_shocks: List[VendorShockEvent] = field(default_factory=list)
    demand_spikes: List[DemandSpikeEvent] = field(default_factory=list)

    def vendor_shocks_starting(self, day: int) -> List[VendorShockEvent]:
        return [e for e in self.vendor_shocks if e.start_day == day]

    def vendor_shocks_ending(self, day: int) -> List[VendorShockEvent]:
        return [e for e in self.vendor_shocks if e.start_day + e.duration_days == day]

    def demand_spikes_starting(self, day: int) -> List[DemandSpikeEvent]:
        return [e for e in self.demand_spikes if e.start_day == day]

    def demand_spikes_ending(self, day: int) -> List[DemandSpikeEvent]:
        return [e for e in self.demand_spikes if e.start_day + e.duration_days == day]
