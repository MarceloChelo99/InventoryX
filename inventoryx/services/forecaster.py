"""
Production forecaster — the real-data analogue of the simulation's
``DemandService``.

The simulation's ``DemandService`` (simulation/demand_service.py) is fed one
realized-sales value per simulated day via ``observe_sale`` and turns the
rolling series into an ``SKUStats`` for the engines. In production we don't get
a tidy one-value-per-tick stream; we get append-only, timestamped ``SaleEvent``
rows (per the design doc's data model). This module bridges that gap.

The bridge is deliberate: both the sim and production funnel through the *same*
canonical aggregation — ``SKUStats.from_daily_sales`` — so the engines see an
identical input shape whether they're being validated in the harness or run on
a real company's history. The only difference is how the daily series is built.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import List, Sequence

from inventoryx.inventory_engines import SKUStats


@dataclass(frozen=True)
class SaleEvent:
    """One append-only sale record (design doc: SaleEvent).

    ``quantity`` is units sold; ``occurred_at`` is the calendar day. Multiple
    events on the same day aggregate to one event of summed size, matching
    ``SKUStats.from_daily_sales`` semantics.
    """

    sku_id: str
    quantity: float
    occurred_at: date


class Forecaster:
    """Turns timestamped sales history into the inputs the engines consume.

    Two outputs:
      * ``profile(events, as_of)`` -> ``SKUStats`` for ``Router.recommend``.
        This is the path the engines actually use.
      * ``predict(events, as_of)`` -> float daily demand rate via the design
        doc's exponentially weighted moving average. Useful for display and
        as the simple ``d`` the legacy scoring formula expects.
    """

    def __init__(self, window_days: int = 90, tau_days: float = 14.0):
        if window_days <= 0:
            raise ValueError("window_days must be positive")
        if tau_days <= 0:
            raise ValueError("tau_days must be positive")
        self.window_days = window_days
        self.tau_days = tau_days

    # --- series construction ----------------------------------------------

    def _daily_series(
        self, events: Sequence[SaleEvent], as_of: date
    ) -> List[float]:
        """Bucket events into per-day sums ending at ``as_of``.

        The series spans ``[start, as_of]`` where ``start`` is the later of
        the rolling-window start and the SKU's first-ever sale. That mirrors
        the sim's deque: an established SKU fills the full window (quiet
        stretches dilute weekly_demand with trailing zeros, exactly as the
        sim's maxlen deque does), while a brand-new SKU is scored on its
        short observed span rather than being diluted by 90 days of zeros it
        was never alive for.
        """
        past = [e for e in events if e.occurred_at <= as_of]
        if not past:
            return []

        window_start = as_of - timedelta(days=self.window_days - 1)
        first_ever = min(e.occurred_at for e in past)
        start = max(window_start, first_ever)
        n_days = (as_of - start).days + 1

        series = [0.0] * n_days
        for e in past:
            idx = (e.occurred_at - start).days
            if 0 <= idx < n_days:
                series[idx] += float(e.quantity)
        return series

    # --- engine input ------------------------------------------------------

    def profile(self, events: Sequence[SaleEvent], as_of: date) -> SKUStats:
        """Build the ``SKUStats`` the router/engines consume.

        Reuses ``SKUStats.from_daily_sales`` so the digest is byte-for-byte
        identical to what the simulation produces for the same daily series.
        """
        series = self._daily_series(events, as_of)
        if not series:
            return SKUStats(weekly_demand=0.0)
        return SKUStats.from_daily_sales(series)

    # --- simple daily rate (design doc weighted MA) ------------------------

    def predict(self, events: Sequence[SaleEvent], as_of: date) -> float:
        """Exponentially weighted moving average daily demand rate.

        ``d = sum(sales[i] * w[i]) / sum(w[i])`` with ``w[i] = exp(-i / tau)``,
        where ``i`` counts days back from ``as_of`` (newer days weighted more).
        Long zero-sale stretches naturally pull the rate toward 0.
        """
        series = self._daily_series(events, as_of)
        if not series:
            return 0.0

        n = len(series)
        num = 0.0
        den = 0.0
        for offset, qty in enumerate(series):
            days_back = (n - 1) - offset  # series[-1] is as_of -> i = 0
            w = math.exp(-days_back / self.tau_days)
            num += qty * w
            den += w
        return num / den if den > 0 else 0.0
