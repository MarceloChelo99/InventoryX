"""Layer 2 acceptance tests — §2.4."""

from __future__ import annotations

import random

import pytest

from inventoryx.entities import SKU, Shock, Vendor
from inventoryx.pipeline import (
    BACKORDER_GRACE_DAYS,
    Order,
    Po,
    POStatus,
    po_status_from_legacy,
)


# --- 2.4 #1: pipeline behavior unchanged -----------------------------------

def test_pipeline_sorts_by_eta():
    o = Order()
    o.add(Po(quantity=10, eta_days=14, vendor_id="V"))
    o.add(Po(quantity=5, eta_days=3, vendor_id="V"))
    o.add(Po(quantity=8, eta_days=21, vendor_id="V"))
    etas = [po.eta_days for po in o.open_pos()]
    assert etas == sorted(etas)
    assert o.next_eta_days() == 3


def test_pipeline_next_day_collects_arrivals():
    o = Order()
    o.add(Po(quantity=10, eta_days=2, vendor_id="V"))
    o.add(Po(quantity=5, eta_days=5, vendor_id="V"))
    day1 = o.next_day()
    assert day1 == []
    day2 = o.next_day()
    assert len(day2) == 1
    assert day2[0].quantity == 10
    assert day2[0].realized_lead_days == 2
    assert o.total_open_quantity() == 5


def test_pipeline_next_week_advances_seven_days():
    o = Order()
    o.add(Po(quantity=10, eta_days=3, vendor_id="V"))
    o.add(Po(quantity=5, eta_days=10, vendor_id="V"))
    arrivals = o.next_week()
    # Only the 3-day PO lands within the first week.
    assert len(arrivals) == 1
    assert arrivals[0].quantity == 10
    # The other PO has 3 days of ETA remaining (10 - 7).
    assert o.next_eta_days() == pytest.approx(3.0)


# --- 2.4 #2: no scoring math on Order --------------------------------------

def test_order_has_no_scoring_methods():
    o = Order()
    for forbidden in ("raid", "find_g", "raid_items", "get_inverse_raid",
                      "get_raid_agg", "get_raid_items", "get_order_suggestion"):
        assert not hasattr(o, forbidden), (
            f"Order should not expose scoring method {forbidden!r}"
        )


# --- 2.4 #3: SKU.inventory_position --------------------------------------

def test_inventory_position_sums_on_hand_and_open_pos():
    v = Vendor(vendor_id="V1")
    sku = SKU(sku_id="S1", vendor=v, unit_cost=50.0, on_hand=12.0)
    sku.place_po(quantity=8, lead_days=14)
    sku.place_po(quantity=4, lead_days=7)
    assert sku.on_order() == 12.0
    assert sku.inventory_position() == 24.0


# --- 2.4 #4: Vendor profile excludes backorders ---------------------------

def test_vendor_profile_excludes_backorders():
    v = Vendor(vendor_id="V1", base_lead_mean=12.0, base_lead_sigma=3.0)
    # Three normal arrivals, two backorder ones.
    v.record_arrival(10.0, False)
    v.record_arrival(12.0, False)
    v.record_arrival(11.0, False)
    v.record_arrival(80.0, True)
    v.record_arrival(90.0, True)
    profile = v.lead_time_profile()
    assert profile.n_orders == 3
    assert profile.mean_lead_days == pytest.approx(11.0)


def test_vendor_profile_falls_back_when_only_backorders():
    v = Vendor(vendor_id="V1", base_lead_mean=15.0)
    v.record_arrival(80.0, True)
    profile = v.lead_time_profile()
    assert profile.n_orders == 0
    assert profile.mean_lead_days == 15.0


# --- Shock semantics -------------------------------------------------------

def test_vendor_shock_scales_lead_time():
    rng = random.Random(0)
    v = Vendor(vendor_id="V1", base_lead_mean=10.0, base_lead_sigma=0.001)
    baseline = v.sample_lead_time(rng)
    v.apply_shock(Shock(lead_mean_multiplier=8.0))
    shocked = v.sample_lead_time(rng)
    assert shocked > 4 * baseline  # shocked sample clearly above baseline band
    v.recover()
    assert not v.is_shocked()


# --- SKU sale/arrival end-to-end ------------------------------------------

# --- legacy three-bucket status model + grace rule -------------------------

def test_backorder_grace_rule():
    """A backordered PO younger than the grace window reads as TENTATIVE;
    once it ages past the window it becomes BACKORDERED."""
    o = Order()
    po = Po(
        quantity=10,
        eta_days=30,
        vendor_id="V",
        declared_status=POStatus.BACKORDERED,
    )
    o.add(po)
    # Fresh: counts as tentative, not backordered.
    assert len(o.backordered) == 0
    assert len(o.tentative) == 1
    # Age it past the grace window.
    for _ in range(int(BACKORDER_GRACE_DAYS) + 1):
        o.next_day()
    assert len(o.backordered) == 1
    assert len(o.tentative) == 0


def test_buckets_partition_open_pos():
    o = Order()
    o.add(Po(quantity=5, eta_days=7, vendor_id="V", declared_status=POStatus.CONFIRMED))
    o.add(Po(quantity=3, eta_days=9, vendor_id="V", declared_status=POStatus.TENTATIVE))
    # Aged backorder.
    bo = Po(quantity=2, eta_days=40, vendor_id="V", declared_status=POStatus.BACKORDERED)
    bo.age_days = 10.0
    o.add(bo)
    total = len(o.confirmed) + len(o.tentative) + len(o.backordered)
    assert total == len(o.open_pos())
    # Backorders still count toward pipeline quantity (supply), per legacy.
    assert o.total_open_quantity() == 10.0


def test_legacy_status_mapping():
    assert po_status_from_legacy("In-Transit") is POStatus.CONFIRMED
    assert po_status_from_legacy("Unconfirmed") is POStatus.TENTATIVE
    assert po_status_from_legacy("Backordered") is POStatus.BACKORDERED
    assert po_status_from_legacy("Cntr BO") is POStatus.BACKORDERED
    assert po_status_from_legacy("???") is POStatus.CONFIRMED  # safe default


def test_sku_fill_sale_and_receive_arrival():
    v = Vendor(vendor_id="V1")
    sku = SKU(sku_id="S1", vendor=v, unit_cost=50.0, on_hand=4.0)
    sold, unfilled = sku.fill_sale(6.0)
    assert sold == 4.0 and unfilled == 2.0
    assert sku.on_hand == 0.0
    # Place a PO, age it to delivery, receive.
    sku.place_po(quantity=12.0, lead_days=2.0)
    for _ in range(2):
        arrivals = sku.order.next_day()
        sku.receive_arrivals(arrivals)
    assert sku.on_hand == 12.0
    # Vendor recorded a non-backorder arrival.
    profile = v.lead_time_profile()
    assert profile.n_orders == 1
    assert profile.mean_lead_days == pytest.approx(2.0)
