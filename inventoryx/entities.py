"""
Layer 2 — domain entities: Vendor and SKU.

Vendor owns lead-time behavior and shock state — supply shocks are
correlated across all SKUs from the same vendor, which is the single most
important thing the engine cannot see directly.

SKU is the mutable aggregate root. It holds on_hand and delegates pipeline
queries to its Order.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import List, Optional

from inventoryx.inventory_engines import LeadTimeProfile
from inventoryx.pipeline import Arrival, Order, Po, POStatus


# ----------------------------------------------------------------------------
# Shock — vendor-level perturbation
# ----------------------------------------------------------------------------


@dataclass
class Shock:
    """A vendor perturbation: scales lead time mean/sigma and can flip
    new orders to backorder status. Defined in events.py (Layer 3) and
    applied here.
    """

    lead_mean_multiplier: float = 1.0
    lead_sigma_multiplier: float = 1.0
    force_backorder: bool = False
    expires_at_day: Optional[int] = None  # set by the scheduler


# ----------------------------------------------------------------------------
# Vendor
# ----------------------------------------------------------------------------


class Vendor:
    """A supplier with a baseline lead-time distribution and shock state."""

    def __init__(
        self,
        vendor_id: str,
        country: str = "",
        base_lead_mean: float = 12.0,
        base_lead_sigma: float = 3.0,
    ) -> None:
        self.vendor_id = vendor_id
        self.country = country
        self.base_lead_mean = base_lead_mean
        self.base_lead_sigma = base_lead_sigma
        self._shock: Optional[Shock] = None
        self._completed_leads: List[float] = []
        self._backorder_flags: List[bool] = []

    # --- shock state -------------------------------------------------------

    def apply_shock(self, shock: Shock) -> None:
        self._shock = shock

    def recover(self) -> None:
        self._shock = None

    def is_shocked(self) -> bool:
        return self._shock is not None

    # --- sampling ----------------------------------------------------------

    def sample_lead_time(self, rng: random.Random) -> float:
        """Draw a lead-time-days sample. Shock-aware; never returns < 1."""
        mean = self.base_lead_mean
        sigma = self.base_lead_sigma
        if self._shock is not None:
            mean *= self._shock.lead_mean_multiplier
            sigma *= self._shock.lead_sigma_multiplier
        sample = rng.gauss(mean, sigma)
        return max(1.0, sample)

    def should_force_backorder(self) -> bool:
        return self._shock is not None and self._shock.force_backorder

    # --- arrival recording -------------------------------------------------

    def record_arrival(self, lead_days: float, was_backorder: bool) -> None:
        self._completed_leads.append(lead_days)
        self._backorder_flags.append(was_backorder)

    # --- aggregate readout -------------------------------------------------

    def lead_time_profile(self, fallback_mean: Optional[float] = None) -> LeadTimeProfile:
        """Backorder-excluded learned lead-time profile.

        Empty clean history -> falls back to base_lead_mean (or the override).
        """
        fb = fallback_mean if fallback_mean is not None else self.base_lead_mean
        return LeadTimeProfile.from_realized(
            self._completed_leads,
            self._backorder_flags,
            fallback_mean=fb,
        )


# ----------------------------------------------------------------------------
# SKU
# ----------------------------------------------------------------------------


class SKU:
    """A stockable item. Mutable; owns on_hand and the per-SKU Order pipeline."""

    def __init__(
        self,
        sku_id: str,
        vendor: Vendor,
        unit_cost: float,
        on_hand: float = 0.0,
        safety_stock: float = 0.0,
    ) -> None:
        self.sku_id = sku_id
        self.vendor = vendor
        self.unit_cost = unit_cost
        self.on_hand = on_hand
        self.safety_stock = safety_stock
        self.order = Order()

    def on_order(self) -> float:
        return self.order.total_open_quantity()

    def inventory_position(self) -> float:
        return self.on_hand + self.on_order()

    def place_po(
        self,
        quantity: float,
        lead_days: float,
        status: POStatus = POStatus.CONFIRMED,
    ) -> Po:
        po = Po(
            quantity=quantity,
            eta_days=lead_days,
            vendor_id=self.vendor.vendor_id,
            declared_status=status,
        )
        self.order.add(po)
        return po

    def fill_sale(self, demanded: float) -> tuple[float, float]:
        """Consume up to `demanded` units from on_hand.

        Returns (sold_units, unfilled_units) where unfilled is the
        stockout amount (lost sales).
        """
        sold = min(self.on_hand, demanded)
        self.on_hand -= sold
        return sold, demanded - sold

    def receive_arrivals(self, arrivals: list[Arrival]) -> None:
        for rec in arrivals:
            self.on_hand += rec.quantity
            self.vendor.record_arrival(rec.realized_lead_days, rec.was_backorder)
