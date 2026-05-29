"""FastAPI REST surface for InventoryX.

Ingestion (POST /sales, /stock/snapshot, /purchase-orders) writes through the
Repository; insights (GET /skus, /skus/{code}/score, /insights/reorder|overstock)
read through ScoringService. Both are thin wrappers — no inventory math lives
here.
"""

from inventoryx.api.app import create_app, run

__all__ = ["create_app", "run"]
