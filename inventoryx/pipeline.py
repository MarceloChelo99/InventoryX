"""
Layer 2 — pipeline objects (Po / SpecificOrder / Order).

Generalized from the legacy tire-distributor module. Key faithful concepts
preserved from the original:

  * Po                — one purchase-order line.
  * SpecificOrder     — a SORTED BUCKET of POs for one status category
                        (NOT an arrival record — that's `Arrival`, below).
  * Order             — three buckets: confirmed / tentative / backordered,
                        with the legacy "a backorder younger than the grace
                        window is still treated as tentative" rule.

Deliberately dropped vs. the legacy file:
  * `from torch import logit_`, `from turtle import back`,
    `from os import supports_follow_symlinks` — accidental IDE auto-imports
    that pulled heavy / headless-unsafe deps for nothing (brief §4).
  * pandas CSV ingestion, the fixed 29-column tire header, ascii_table, and
    the hardcoded tire status strings. Those belong in an ingestion adapter,
    not the core. `po_status_from_legacy()` below is the seam: it maps the
    old C/U/B status strings onto the generalized POStatus enum.
  * Datex coupling — the pipeline now advances on integer day/week steps and
    has no dependency on a calendar library. An ingestion layer converts real
    dates to `eta_days` at load time.

All scoring math (`raid`, `find_g`, `raid_items`, `get_*`) has been removed
from `Order` and now lives in `inventory_engines` (brief §2.1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Tuple


# ----------------------------------------------------------------------------
# Status model — generalized from the tire C/U/B status strings
# ----------------------------------------------------------------------------


class POStatus(str, Enum):
    CONFIRMED = "confirmed"       # legacy C_STATUS: In-Transit, Confirmed, ...
    TENTATIVE = "tentative"       # legacy U_STATUS: Estimated, Unconfirmed, ...
    BACKORDERED = "backordered"   # legacy B_STATUS: Backordered, No Prod Sch, ...


# Default grace window: a freshly-declared backorder is treated as tentative
# until it has aged this many days (legacy rule was `days_since_order() < 3`).
BACKORDER_GRACE_DAYS = 3.0


# Legacy tire status strings -> generalized POStatus. This is the ONLY place
# the tire-specific vocabulary lives; ingestion code calls it, the core does not.
_LEGACY_CONFIRMED = {"In-Transit", "Confirmed", "Scheduled", "Re-Sched"}
_LEGACY_TENTATIVE = {"Estimated", "Container", "Unconfirmed", "Cntr Est", "Future"}
_LEGACY_BACKORDER = {"Backordered", "No Prod Sch", "Cntr BO"}


def po_status_from_legacy(status: str) -> POStatus:
    """Map a legacy tire status string onto POStatus (adapter seam)."""
    if status in _LEGACY_CONFIRMED:
        return POStatus.CONFIRMED
    if status in _LEGACY_TENTATIVE:
        return POStatus.TENTATIVE
    if status in _LEGACY_BACKORDER:
        return POStatus.BACKORDERED
    return POStatus.CONFIRMED  # conservative default for unknown codes


# ----------------------------------------------------------------------------
# Po — one purchase-order line
# ----------------------------------------------------------------------------


@dataclass
class Po:
    """A purchase order placed with a vendor.

    quantity         : units ordered
    eta_days         : days until expected arrival (decremented by next_day)
    vendor_id        : who fulfills it
    declared_status  : status as declared by the source system
    received         : flips True on arrival
    age_days         : days since placement (incremented by next_day)

    Optional legacy/metadata fields are carried through for reporting but the
    core logic never reads them.
    """

    quantity: float
    eta_days: float
    vendor_id: str
    declared_status: POStatus = POStatus.CONFIRMED
    received: bool = False
    age_days: float = 0.0
    # passthrough metadata (optional)
    po_number: int = -1
    item_number: str = ""
    unit_cost: float = 0.0

    def next_day(self) -> None:
        if self.received:
            return
        self.eta_days -= 1.0
        self.age_days += 1.0

    def next_week(self) -> None:
        for _ in range(7):
            self.next_day()

    def is_due(self) -> bool:
        return (not self.received) and self.eta_days <= 0.0

    def receive(self) -> None:
        self.received = True

    def effective_status(self, grace_days: float = BACKORDER_GRACE_DAYS) -> POStatus:
        """Backordered POs younger than the grace window read as TENTATIVE."""
        if self.declared_status is POStatus.BACKORDERED and self.age_days < grace_days:
            return POStatus.TENTATIVE
        return self.declared_status

    def is_backorder(self, grace_days: float = BACKORDER_GRACE_DAYS) -> bool:
        return self.effective_status(grace_days) is POStatus.BACKORDERED


# ----------------------------------------------------------------------------
# Arrival — a received-PO record (was the thing my first pass mis-named)
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Arrival:
    quantity: float
    realized_lead_days: float
    vendor_id: str
    was_backorder: bool


# ----------------------------------------------------------------------------
# SpecificOrder — a sorted bucket of POs (faithful to the legacy class)
# ----------------------------------------------------------------------------


@dataclass
class SpecificOrder:
    """A sorted-by-ETA bucket of open POs for one status category."""

    pos: List[Po] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.pos)

    def total_quantity(self) -> float:
        return sum(p.quantity for p in self.pos)

    def next_arrival(self) -> Optional[Po]:
        return self.pos[0] if self.pos else None

    def average_eta(self) -> float:
        """Quantity-weighted mean ETA (days). 0 if empty / zero quantity."""
        q = self.total_quantity()
        if q <= 0:
            return 0.0
        return sum(p.quantity * p.eta_days for p in self.pos) / q


# ----------------------------------------------------------------------------
# Order — the per-SKU pipeline, three status buckets over one master list
# ----------------------------------------------------------------------------


class Order:
    """Pipeline of open + historical POs for one SKU.

    A single master list is the source of truth; the confirmed / tentative /
    backordered buckets are computed views (so a PO ageing past the grace
    window flips buckets automatically — the legacy code bucketed once at
    add-time and could go stale).
    """

    def __init__(self, grace_days: float = BACKORDER_GRACE_DAYS) -> None:
        self.grace_days = grace_days
        self._pos: List[Po] = []
        self._history: List[Arrival] = []

    # --- mutations ---------------------------------------------------------

    def add(self, po: Po) -> None:
        self._pos.append(po)
        self._pos.sort(key=lambda p: p.eta_days)

    def next_day(self) -> List[Arrival]:
        """Advance one day; return arrivals that landed today."""
        for po in self._pos:
            po.next_day()
        arrived: List[Arrival] = []
        survivors: List[Po] = []
        for po in self._pos:
            if po.is_due():
                po.receive()
                rec = Arrival(
                    quantity=po.quantity,
                    realized_lead_days=po.age_days,
                    vendor_id=po.vendor_id,
                    # final status at arrival; a long backorder has long since
                    # aged past the grace window, so it's correctly excluded
                    # from learned lead times downstream.
                    was_backorder=po.is_backorder(self.grace_days),
                )
                arrived.append(rec)
                self._history.append(rec)
            else:
                survivors.append(po)
        self._pos = survivors
        return arrived

    def next_week(self) -> List[Arrival]:
        arrivals: List[Arrival] = []
        for _ in range(7):
            arrivals.extend(self.next_day())
        return arrivals

    # --- queries -----------------------------------------------------------

    def open_pos(self) -> List[Po]:
        return sorted(self._pos, key=lambda p: p.eta_days)

    def history(self) -> List[Arrival]:
        return list(self._history)

    def total_open_quantity(self) -> float:
        """All open pipeline, across every bucket (incl. backorders).

        Backorders count as supply here (they will eventually arrive) — they
        are only excluded from *lead-time learning*, not from pipeline qty.
        This matches the legacy `join_all_orders()` behavior.
        """
        return sum(p.quantity for p in self._pos)

    def next_eta_days(self) -> Optional[float]:
        if not self._pos:
            return None
        return min(p.eta_days for p in self._pos)

    def max_open_age_days(self) -> float:
        return max((p.age_days for p in self._pos), default=0.0)

    def timeline(self) -> Tuple[List[float], List[float]]:
        """(etas, quantities) sorted by ETA — feeds RaidScorer.raid_items."""
        ordered = self.open_pos()
        etas = [max(p.eta_days, 0.0) for p in ordered]
        qtys = [p.quantity for p in ordered]
        return etas, qtys

    # --- bucket views ------------------------------------------------------

    def _bucket(self, status: POStatus) -> SpecificOrder:
        return SpecificOrder(
            sorted(
                (p for p in self._pos if p.effective_status(self.grace_days) is status),
                key=lambda p: p.eta_days,
            )
        )

    @property
    def confirmed(self) -> SpecificOrder:
        return self._bucket(POStatus.CONFIRMED)

    @property
    def tentative(self) -> SpecificOrder:
        return self._bucket(POStatus.TENTATIVE)

    @property
    def backordered(self) -> SpecificOrder:
        return self._bucket(POStatus.BACKORDERED)
