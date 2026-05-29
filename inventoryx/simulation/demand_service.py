"""
The honest forecaster.

It maintains a per-SKU realized-daily-sales history and produces SKUStats
from it. It NEVER reads truth.py — only the realized series passed in via
observe_sale.

Critically: this module does not import from truth.py. The boundary is
enforced by convention, but verified in tests (test_simulation imports
both modules and asserts no shared symbols leak between them).
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Deque, Dict, Optional

from inventoryx.inventory_engines import SKUStats


class DemandService:
    """Per-SKU rolling realized-sales history -> SKUStats."""

    def __init__(self, window_days: int = 90):
        self.window_days = window_days
        self._series: Dict[str, Deque[float]] = defaultdict(
            lambda: deque(maxlen=window_days)
        )

    def observe_sale(self, sku_id: str, quantity: float) -> None:
        """Append today's realized sales for this SKU (call once per day)."""
        self._series[sku_id].append(float(quantity))

    def history(self, sku_id: str) -> list[float]:
        return list(self._series.get(sku_id, deque()))

    def profile_for(self, sku_id: str) -> SKUStats:
        """Build an SKUStats from the rolling realized series.

        Reuses SKUStats.from_daily_sales — the canonical aggregation logic.
        Lags truth naturally because it only sees post-fact data.
        """
        series = self.history(sku_id)
        if not series:
            return SKUStats(weekly_demand=0.0)
        return SKUStats.from_daily_sales(series)
