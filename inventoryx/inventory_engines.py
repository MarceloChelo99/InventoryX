"""
Layer 1 — ordering engine module.

Two engines (flow / intermittent), one cross-cutting scorer, one router.
The flow formula (T1, T2, L) lives in a single private helper so the score
path and the find_g root-find can never disagree on the math.

Units convention:
    - All demand fed to the flow engine is WEEKLY (the unit the flow formula
      was calibrated in).
    - All lead times and inter-arrival gaps are in DAYS.
"""

from __future__ import annotations

import math
import statistics
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Optional, Sequence

from scipy.optimize import brentq


# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    """All magic numbers, named.

    Defaults match the tire-shop calibration with the documented weekly-d shift.
    """

    # Flow formula coefficients
    DEMAND_SCALE: float = 4.0          # multiplies weekly demand in T1 numerator
    LAPLACE: float = 1.0               # +1 smoothing on log arguments
    WEEKLY_DAYS: float = 7.0           # ETA-days -> weeks conversion in T1
    NORMAL_LEAD_TIME_DAYS: float = 15.0  # normal-ops lead ceiling; T2 anchor
    T1_SHIFT: float = math.e - 0.5
    T2_SHIFT: float = math.e - 1.0
    L_CENTER: float = 4.0              # weekly-demand center of demand gate L
    L_SLOPE: float = 1.0               # steepness of L

    # Routing
    FLOW_THRESHOLD_DAYS: float = 7.0   # p_bar <= this -> flow regime
    MIN_EVENTS: int = 4                # below this, p_bar untrusted

    # Intermittent (Croston / SBA)
    SBA_ALPHA: float = 0.1             # smoothing constant
    SERVICE_Z: float = 1.65            # ~95% service level

    # Root-finder
    G_UPPER_BOUND: float = 1e6

    # find_g gating. The legacy code solved T1*T2*L = 1 (logit gate KEPT in
    # the root-find). The brief specifies T1*T2 = 1 (L dropped, because the
    # router already decided the SKU is flow-regime). These give different
    # order quantities for low-demand SKUs near d ~ L_CENTER. Default follows
    # the brief; set True to restore the original tire-shop behavior.
    GATE_FIND_G: bool = False


DEFAULT_CONFIG = Config()


# ----------------------------------------------------------------------------
# Enums
# ----------------------------------------------------------------------------


class Engine(str, Enum):
    FLOW = "flow"
    INTERMITTENT = "intermittent"


class Confidence(str, Enum):
    HIGH = "high"
    LOW = "low"


class Action(str, Enum):
    OVERSTOCKED = "overstocked"
    BALANCED = "balanced"
    REORDER = "reorder"
    URGENT = "urgent"


# ----------------------------------------------------------------------------
# Value objects
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class SKUStats:
    """Digested demand profile for one SKU.

    weekly_demand    - mean weekly sales (units / week), for the flow engine.
    z_bar / sigma_z  - mean and std of event (burst) size.
    p_bar / sigma_p  - mean and std of inter-arrival gap, in DAYS.
    n_events         - count of non-zero-sale days observed.
    """

    weekly_demand: float
    z_bar: float = 0.0
    p_bar: float = 0.0
    sigma_z: float = 0.0
    sigma_p: float = 0.0
    n_events: int = 0

    @classmethod
    def from_daily_sales(
        cls,
        daily_quantities: Sequence[float],
        weeks_observed: Optional[float] = None,
    ) -> "SKUStats":
        """Build stats from a daily sales series.

        Same-day tickets aggregate to ONE event of summed size; gaps are
        days between consecutive event days. weeks_observed defaults to
        len(daily_quantities) / 7.
        """
        days = list(daily_quantities)
        n_days = len(days)
        weeks = weeks_observed if weeks_observed is not None else n_days / 7.0
        total = float(sum(days))
        weekly_demand = total / weeks if weeks > 0 else 0.0

        # Event days: index + summed size (already summed per day in the input).
        event_indices = [i for i, q in enumerate(days) if q > 0]
        event_sizes = [float(days[i]) for i in event_indices]
        n_events = len(event_indices)

        if n_events == 0:
            return cls(weekly_demand=weekly_demand, n_events=0)

        z_bar = statistics.fmean(event_sizes)
        sigma_z = statistics.pstdev(event_sizes) if n_events > 1 else 0.0

        # Inter-arrival gaps in days. With k events we have k-1 internal gaps.
        # We also want a "single event repeating" series like [4,0,0,0,0,0,0]*10
        # to give p_bar == 7, not 7*9/10. Solve this by including the wrap gap:
        # mean gap = (last_index - first_index + period) / n_events when the
        # series tiles cleanly. Use the simpler rule:
        #     p_bar = n_days / n_events
        # which gives 70/10 = 7 for the canonical case and matches the spec.
        p_bar = n_days / n_events if n_events > 0 else 0.0

        if n_events >= 2:
            gaps = [
                event_indices[i + 1] - event_indices[i]
                for i in range(n_events - 1)
            ]
            sigma_p = statistics.pstdev(gaps) if len(gaps) > 1 else 0.0
        else:
            sigma_p = 0.0

        return cls(
            weekly_demand=weekly_demand,
            z_bar=z_bar,
            p_bar=p_bar,
            sigma_z=sigma_z,
            sigma_p=sigma_p,
            n_events=n_events,
        )


@dataclass(frozen=True)
class LeadTimeProfile:
    """Aggregate lead-time behavior for a supplier or SKU.

    Backorder-flagged arrivals are EXCLUDED from mean_lead_days and
    sigma_lead_days; they're the pathological tail (60-90+ days) and
    they would poison normal-ops estimates.
    """

    mean_lead_days: float
    sigma_lead_days: float = 0.0
    n_orders: int = 0
    max_open_order_age_days: float = 0.0
    reliability: Optional[float] = None  # set by Layer 3 if tracked

    @classmethod
    def from_realized(
        cls,
        realized_lead_days: Sequence[float],
        backorder_flags: Optional[Sequence[bool]] = None,
        fallback_mean: float = 15.0,
    ) -> "LeadTimeProfile":
        lead = list(realized_lead_days)
        flags = list(backorder_flags) if backorder_flags is not None else [False] * len(lead)
        if len(flags) != len(lead):
            raise ValueError("backorder_flags must align with realized_lead_days")

        clean = [d for d, b in zip(lead, flags) if not b]
        n = len(clean)
        if n == 0:
            return cls(mean_lead_days=fallback_mean, n_orders=0)

        mean = statistics.fmean(clean)
        sigma = statistics.pstdev(clean) if n > 1 else 0.0
        return cls(mean_lead_days=mean, sigma_lead_days=sigma, n_orders=n)


@dataclass(frozen=True)
class OrderRecommendation:
    quantity: float
    engine: Engine
    alert_score: float
    score_confidence: Confidence
    action: Action
    reorder_point: Optional[float] = None
    diagnostics: dict = field(default_factory=dict)


@dataclass
class _EngineResult:
    """Internal: what an engine returns to the router."""

    quantity: float
    engine: Engine
    confidence: Confidence
    reorder_point: Optional[float] = None
    diagnostics: dict = field(default_factory=dict)


# ----------------------------------------------------------------------------
# Action classification
# ----------------------------------------------------------------------------


# Score thresholds — derived from the calibration where balanced = 1.
_SCORE_OVER_CEILING = 1.0   # below this and zero qty -> overstocked
_SCORE_URGENT_FLOOR = 2.0   # above this with an order -> urgent


def classify_action(alert_score: float, quantity: float) -> Action:
    """Resolve a score + quantity into a single decision.

    Key cases (the subtle one is the third):
        score < 1 and qty == 0  -> OVERSTOCKED  (don't buy, capital tied up)
        score >= 2 and qty > 0  -> URGENT       (stockout-class signal + action)
        qty > 0                 -> REORDER      (place an order — even if score
                                                 looks comfortable, find_g says
                                                 you need to keep flowing)
        else                    -> BALANCED
    """
    has_order = quantity > 0
    if alert_score >= _SCORE_URGENT_FLOOR and has_order:
        return Action.URGENT
    if has_order:
        return Action.REORDER
    if alert_score < _SCORE_OVER_CEILING:
        return Action.OVERSTOCKED
    return Action.BALANCED


# ----------------------------------------------------------------------------
# Shared flow-formula math
# ----------------------------------------------------------------------------


def _flow_terms(
    d: float,
    q: float,
    a: float,
    s: float,
    cfg: Config,
) -> tuple[float, float, float]:
    """Return (T1, T2, L) for the flow formula.

    d weekly, a in days, q in units, s in units. This is the single source
    of truth: RaidScorer.score and FlowEngine.find_g both call this and
    therefore agree by construction.
    """
    lap = cfg.LAPLACE

    demand_term = cfg.DEMAND_SCALE * d + lap
    supply_term = (q + lap) / (a / cfg.WEEKLY_DAYS + lap) + s + lap
    order_demand_term = 2.0 * d * (a / cfg.NORMAL_LEAD_TIME_DAYS) + lap
    order_supply_term = q + lap

    t1 = math.log(demand_term / supply_term + cfg.T1_SHIFT)
    t2 = math.log(order_demand_term / order_supply_term + cfg.T2_SHIFT)
    l_gate = 1.0 / (1.0 + math.exp(-cfg.L_SLOPE * (d - cfg.L_CENTER)))

    return t1, t2, l_gate


# ----------------------------------------------------------------------------
# RaidScorer — the cross-cutting "do I care?" signal
# ----------------------------------------------------------------------------


class RaidScorer:
    """Computes the alert score for any SKU, regardless of engine.

    Engines must NEVER instantiate this themselves; the router holds the
    single instance and applies it after the engine returns a quantity.
    """

    def __init__(self, cfg: Config = DEFAULT_CONFIG):
        self.cfg = cfg

    def score(
        self,
        stats: SKUStats,
        lead_time: LeadTimeProfile,
        on_order: float,
        safety_stock: float = 0.0,
    ) -> float:
        t1, t2, l_gate = _flow_terms(
            d=stats.weekly_demand,
            q=on_order,
            a=lead_time.mean_lead_days,
            s=safety_stock,
            cfg=self.cfg,
        )
        return round(t1 * t2 * l_gate, 2)

    def raid_items(
        self,
        stats: SKUStats,
        etas: Sequence[float],
        quantities: Sequence[float],
        safety_stock: float = 0.0,
    ) -> list[float]:
        """Cumulative per-shipment trace (legacy `raid_items`).

        Walks the incoming POs in ETA order and reports the score *as if* each
        successive shipment had just landed — i.e. cumulative pipeline quantity
        evaluated against that PO's own ETA. Useful for a timeline view:
        "after PO #1 you're still at 2.4, after #2 you hit 1.1, after #3 you're
        at 0.9 — so #3 is overkill."

        Keeps the demand gate L (this is the alert-style score, not the
        order-sizing root-find).
        """
        d = stats.weekly_demand
        s = max(safety_stock, 0.0)
        cumulative = 0.0
        trace: list[float] = []
        for eta, qty in zip(etas, quantities):
            cumulative += qty
            a = max(eta, 0.0)
            t1, t2, l_gate = _flow_terms(d=d, q=cumulative, a=a, s=s, cfg=self.cfg)
            trace.append(round(t1 * t2 * l_gate, 2))
        return trace


# ----------------------------------------------------------------------------
# Engines (Strategy)
# ----------------------------------------------------------------------------


class OrderingEngine(ABC):
    def __init__(self, cfg: Config = DEFAULT_CONFIG):
        self.cfg = cfg

    @abstractmethod
    def compute(
        self,
        stats: SKUStats,
        lead_time: LeadTimeProfile,
        on_hand: float,
        on_order: float,
        safety_stock: float = 0.0,
    ) -> _EngineResult:
        ...


class FlowEngine(OrderingEngine):
    """Suggests an order quantity by solving T1(g) * T2(g) == 1 for g.

    L is DROPPED from the root-find by default (Config.GATE_FIND_G=False):
    the router already decided this SKU is in the flow regime, so re-gating
    at decision time would distort the math near the d ~ L_CENTER boundary.
    Set Config.GATE_FIND_G=True to restore the legacy behavior, which solved
    T1*T2*L == 1 and therefore ordered fewer units for low-demand SKUs.
    """

    def find_g(
        self,
        stats: SKUStats,
        lead_time: LeadTimeProfile,
        on_order: float,
        safety_stock: float = 0.0,
    ) -> float:
        cfg = self.cfg
        d = stats.weekly_demand
        s = safety_stock

        # ETA guard: a brand-new order with no pipeline uses the normal-ops
        # lead time, never 0 or a magic constant.
        a = lead_time.mean_lead_days if lead_time.mean_lead_days > 0 else cfg.NORMAL_LEAD_TIME_DAYS

        def balance(g: float) -> float:
            t1, t2, l_gate = _flow_terms(d=d, q=g + on_order, a=a, s=s, cfg=cfg)
            gate = l_gate if cfg.GATE_FIND_G else 1.0
            return t1 * t2 * gate - 1.0

        # Guard: already at/above balanced with zero new order -> don't buy.
        if balance(0.0) <= 0.0:
            return 0.0

        # T1, T2 are each strictly decreasing in g; their product is too.
        # The bracket [0, G_UPPER_BOUND] is guaranteed: balance(0) > 0 by the
        # guard above; balance(big) -> T1_floor * T2_floor - 1 < 0.
        hi = cfg.G_UPPER_BOUND
        if balance(hi) > 0.0:
            # Pathological config (shifts changed). Surface rather than hang.
            raise RuntimeError(
                "find_g: balance(G_UPPER_BOUND) > 0; check Config shifts/coefficients."
            )

        return float(brentq(balance, 0.0, hi))

    def compute(self, stats, lead_time, on_hand, on_order, safety_stock=0.0):
        g = self.find_g(stats, lead_time, on_order, safety_stock)
        return _EngineResult(
            quantity=g,
            engine=Engine.FLOW,
            confidence=Confidence.HIGH,
            reorder_point=None,
            diagnostics={
                "weekly_demand": stats.weekly_demand,
                "lead_mean_days": lead_time.mean_lead_days,
                "on_order": on_order,
                "on_hand": on_hand,
                "safety_stock": safety_stock,
            },
        )


class IntermittentEngine(OrderingEngine):
    """Croston/SBA for lumpy demand.

    Order = max(reorder_point - inventory_position, 0).
    """

    def sba_daily_rate(self, stats: SKUStats) -> float:
        if stats.p_bar <= 0 or stats.z_bar <= 0:
            return 0.0
        return (1.0 - self.cfg.SBA_ALPHA / 2.0) * stats.z_bar / stats.p_bar

    def lead_time_demand_variance(
        self,
        stats: SKUStats,
        lead_time: LeadTimeProfile,
    ) -> float:
        """Classical compound-process LTD variance.

        sigma^2_LTD = (Lt/p) * (sigma_z^2 + z^2 * sigma_p^2 / p^2)
                       + (z/p)^2 * sigma_Lt^2
        """
        if stats.p_bar <= 0:
            return 0.0
        Lt = lead_time.mean_lead_days
        z, p = stats.z_bar, stats.p_bar
        sz2 = stats.sigma_z ** 2
        sp2 = stats.sigma_p ** 2
        sLt2 = lead_time.sigma_lead_days ** 2

        term_intra = (Lt / p) * (sz2 + (z ** 2) * sp2 / (p ** 2))
        term_lead = (z / p) ** 2 * sLt2
        return term_intra + term_lead

    def reorder_point(self, stats: SKUStats, lead_time: LeadTimeProfile) -> float:
        rate = self.sba_daily_rate(stats)
        mean_ltd = rate * lead_time.mean_lead_days
        variance = self.lead_time_demand_variance(stats, lead_time)
        return mean_ltd + self.cfg.SERVICE_Z * math.sqrt(max(variance, 0.0))

    def compute(self, stats, lead_time, on_hand, on_order, safety_stock=0.0):
        rop = self.reorder_point(stats, lead_time)
        position = on_hand + on_order
        qty = max(rop - position, 0.0)
        return _EngineResult(
            quantity=qty,
            engine=Engine.INTERMITTENT,
            confidence=Confidence.LOW,
            reorder_point=rop,
            diagnostics={
                "sba_daily_rate": self.sba_daily_rate(stats),
                "ltd_variance": self.lead_time_demand_variance(stats, lead_time),
                "inventory_position": position,
            },
        )


# ----------------------------------------------------------------------------
# Router
# ----------------------------------------------------------------------------


class Router:
    """Routes each SKU to the engine valid for its demand regime,
    then layers on the shared score + action classification.
    """

    def __init__(
        self,
        cfg: Config = DEFAULT_CONFIG,
        flow_engine: Optional[FlowEngine] = None,
        intermittent_engine: Optional[IntermittentEngine] = None,
        scorer: Optional[RaidScorer] = None,
    ):
        self.cfg = cfg
        self.flow = flow_engine or FlowEngine(cfg)
        self.intermittent = intermittent_engine or IntermittentEngine(cfg)
        self.scorer = scorer or RaidScorer(cfg)

    def choose_engine(self, stats: SKUStats) -> Engine:
        if stats.n_events < self.cfg.MIN_EVENTS:
            # Too few events to trust p_bar — fall back to demand-level check.
            return (
                Engine.FLOW
                if stats.weekly_demand >= self.cfg.L_CENTER
                else Engine.INTERMITTENT
            )
        return (
            Engine.FLOW
            if stats.p_bar <= self.cfg.FLOW_THRESHOLD_DAYS
            else Engine.INTERMITTENT
        )

    def _engine_for(self, choice: Engine) -> OrderingEngine:
        return self.flow if choice is Engine.FLOW else self.intermittent

    def recommend(
        self,
        stats: SKUStats,
        lead_time: LeadTimeProfile,
        on_hand: float = 0.0,
        on_order: float = 0.0,
        safety_stock: float = 0.0,
    ) -> OrderRecommendation:
        choice = self.choose_engine(stats)
        engine = self._engine_for(choice)
        result = engine.compute(
            stats=stats,
            lead_time=lead_time,
            on_hand=on_hand,
            on_order=on_order,
            safety_stock=safety_stock,
        )

        score = self.scorer.score(stats, lead_time, on_order, safety_stock)
        action = classify_action(score, result.quantity)

        diagnostics = dict(result.diagnostics)
        diagnostics["routed_on"] = {
            "n_events": stats.n_events,
            "p_bar": stats.p_bar,
            "weekly_demand": stats.weekly_demand,
            "rule": (
                "demand-level fallback (n_events < MIN_EVENTS)"
                if stats.n_events < self.cfg.MIN_EVENTS
                else "p_bar threshold"
            ),
        }

        return OrderRecommendation(
            quantity=result.quantity,
            engine=result.engine,
            alert_score=score,
            score_confidence=result.confidence,
            action=action,
            reorder_point=result.reorder_point,
            diagnostics=diagnostics,
        )
