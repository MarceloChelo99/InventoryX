"""
ScoringService — the production orchestration that the simulation's ``World``
is to the harness: it pulls a SKU's history and state from a data source, runs
the (already-validated) engines via the ``Router``, and returns recommendations
plus the design doc's reorder / overstock insight lists.

It owns no inventory math. Every formula lives in ``inventory_engines``; this
layer is purely "fetch the inputs, call the router, shape the output."
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import List, Optional

from inventoryx.inventory_engines import (
    DEFAULT_CONFIG,
    Action,
    Config,
    LeadTimeProfile,
    OrderRecommendation,
    Router,
)
from inventoryx.services.forecaster import Forecaster
from inventoryx.services.sources import InventoryDataSource


@dataclass(frozen=True)
class ScoredSku:
    """A SKU paired with its recommendation, for insight lists."""

    sku_id: str
    recommendation: OrderRecommendation


class ScoringService:
    def __init__(
        self,
        source: InventoryDataSource,
        forecaster: Optional[Forecaster] = None,
        router: Optional[Router] = None,
        cfg: Config = DEFAULT_CONFIG,
    ):
        self.source = source
        self.cfg = cfg
        self.forecaster = forecaster or Forecaster()
        self.router = router or Router(cfg)

    # --- single SKU --------------------------------------------------------

    def score_sku(self, sku_id: str, as_of: date) -> OrderRecommendation:
        """Score one SKU as of a given day."""
        events = self.source.sales_history(sku_id, as_of)
        stats = self.forecaster.profile(events, as_of)

        state = self.source.stock_state(sku_id, as_of)
        leads = self.source.lead_time_history(sku_id)
        lead_profile = LeadTimeProfile.from_realized(
            [o.lead_days for o in leads],
            [o.was_backorder for o in leads],
            fallback_mean=state.fallback_lead_days,
        )

        return self.router.recommend(
            stats=stats,
            lead_time=lead_profile,
            on_hand=state.on_hand,
            on_order=state.on_order,
            safety_stock=state.safety_stock,
        )

    # --- whole catalog -----------------------------------------------------

    def score_all(self, as_of: date) -> List[ScoredSku]:
        return [
            ScoredSku(sku_id, self.score_sku(sku_id, as_of))
            for sku_id in self.source.sku_ids()
        ]

    def reorder_list(self, as_of: date) -> List[ScoredSku]:
        """SKUs that need ordering, most urgent first (design doc insight)."""
        scored = [
            s
            for s in self.score_all(as_of)
            if s.recommendation.action in (Action.REORDER, Action.URGENT)
        ]
        return sorted(
            scored, key=lambda s: s.recommendation.alert_score, reverse=True
        )

    def overstock_list(self, as_of: date) -> List[ScoredSku]:
        """SKUs with capital tied up in excess stock, worst first."""
        scored = [
            s
            for s in self.score_all(as_of)
            if s.recommendation.action is Action.OVERSTOCKED
        ]
        return sorted(scored, key=lambda s: s.recommendation.alert_score)
