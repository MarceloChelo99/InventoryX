"""SQLite/SQLAlchemy persistence: ORM models + a SQL-backed data source.

The design doc's data model (Company, Supplier, Sku, SaleEvent, StockSnapshot,
PurchaseOrder). ``SqlInventoryDataSource`` implements the ``InventoryDataSource``
protocol so ``ScoringService`` runs on a real database unchanged.
"""

from inventoryx.db.models import (
    Base,
    Company,
    PurchaseOrder,
    SaleEvent,
    Sku,
    StockSnapshot,
    Supplier,
)
from inventoryx.db.repository import Repository
from inventoryx.db.session import (
    DEFAULT_URL,
    init_db,
    make_engine,
    make_session_factory,
)
from inventoryx.db.source import SqlInventoryDataSource

__all__ = [
    "Base",
    "Company",
    "Supplier",
    "Sku",
    "SaleEvent",
    "StockSnapshot",
    "PurchaseOrder",
    "Repository",
    "SqlInventoryDataSource",
    "make_engine",
    "make_session_factory",
    "init_db",
    "DEFAULT_URL",
]
