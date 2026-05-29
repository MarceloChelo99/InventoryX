"""
Per-SKU + regime-split accumulators.

We report the stockout / holding tradeoff; neither alone judges quality.
Regime split prevents a flow engine's blowup from being hidden inside a
combined aggregate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

from inventoryx.inventory_engines import Engine


@dataclass
class SKUMetrics:
    sku_id: str
    routed_engine: Engine
    days_observed: int = 0
    demand_units: float = 0.0
    sold_units: float = 0.0
    stockout_units: float = 0.0
    orders_placed: int = 0
    on_hand_sum: float = 0.0
    unit_cost: float = 0.0

    def record_day(self, demanded: float, sold: float, on_hand: float) -> None:
        self.days_observed += 1
        self.demand_units += demanded
        self.sold_units += sold
        self.stockout_units += demanded - sold
        self.on_hand_sum += on_hand

    def record_order(self) -> None:
        self.orders_placed += 1

    @property
    def mean_on_hand(self) -> float:
        return self.on_hand_sum / self.days_observed if self.days_observed else 0.0

    @property
    def fill_rate(self) -> float:
        return self.sold_units / self.demand_units if self.demand_units > 0 else 1.0

    @property
    def holding_cost_proxy(self) -> float:
        # mean_on_hand * unit_cost * weeks_observed
        weeks = self.days_observed / 7.0
        return self.mean_on_hand * self.unit_cost * weeks


@dataclass
class RunMetrics:
    per_sku: Dict[str, SKUMetrics] = field(default_factory=dict)

    def ensure(self, sku_id: str, engine: Engine, unit_cost: float) -> SKUMetrics:
        if sku_id not in self.per_sku:
            self.per_sku[sku_id] = SKUMetrics(
                sku_id=sku_id, routed_engine=engine, unit_cost=unit_cost
            )
        else:
            # Engine routing can shift over the run (e.g. as more events accrue
            # and the router crosses the FLOW_THRESHOLD). Keep the latest.
            self.per_sku[sku_id].routed_engine = engine
        return self.per_sku[sku_id]

    # --- regime split ------------------------------------------------------

    def split_by_engine(self) -> Dict[Engine, List[SKUMetrics]]:
        out: Dict[Engine, List[SKUMetrics]] = {Engine.FLOW: [], Engine.INTERMITTENT: []}
        for m in self.per_sku.values():
            out[m.routed_engine].append(m)
        return out

    def summary(self) -> dict:
        split = self.split_by_engine()
        out = {"overall": self._aggregate(list(self.per_sku.values()))}
        for eng, metrics in split.items():
            out[eng.value] = self._aggregate(metrics)
        return out

    @staticmethod
    def _aggregate(metrics: List[SKUMetrics]) -> dict:
        if not metrics:
            return {"skus": 0}
        total_demand = sum(m.demand_units for m in metrics)
        total_sold = sum(m.sold_units for m in metrics)
        total_stockout = sum(m.stockout_units for m in metrics)
        total_holding = sum(m.holding_cost_proxy for m in metrics)
        fill = total_sold / total_demand if total_demand > 0 else 1.0
        return {
            "skus": len(metrics),
            "demand_units": round(total_demand, 2),
            "sold_units": round(total_sold, 2),
            "stockout_units": round(total_stockout, 2),
            "fill_rate": round(fill, 4),
            "holding_cost_proxy": round(total_holding, 2),
            "orders_placed": sum(m.orders_placed for m in metrics),
        }
