"""FastAPI application factory.

``create_app`` builds the app and wires a session factory onto ``app.state``.
Pass an ``engine`` to point at a specific database (tests inject an in-memory
SQLite engine); otherwise it reads ``INVENTORYX_DATABASE_URL`` (default
``sqlite:///inventoryx.db``) and creates tables if they're missing.
"""

from __future__ import annotations

import os
from typing import Optional

from fastapi import FastAPI
from sqlalchemy import Engine

from inventoryx.api.routers import catalog, ingest, insights
from inventoryx.db import init_db, make_engine, make_session_factory


def create_app(engine: Optional[Engine] = None, create_tables: bool = True) -> FastAPI:
    if engine is None:
        url = os.getenv("INVENTORYX_DATABASE_URL", "sqlite:///inventoryx.db")
        engine = make_engine(url)
    if create_tables:
        # Convenience for local/dev; production runs Alembic migrations.
        init_db(engine)

    app = FastAPI(
        title="InventoryX",
        version="0.1.0",
        summary="Order-health scoring for storable-goods distributors.",
    )
    app.state.session_factory = make_session_factory(engine)

    @app.get("/health", tags=["meta"])
    def health():
        return {"status": "ok"}

    app.include_router(catalog.router)
    app.include_router(ingest.router)
    app.include_router(insights.router)
    return app


def run() -> None:
    """Entry point for the ``inventoryx-api`` console script."""
    import uvicorn

    host = os.getenv("INVENTORYX_API_HOST", "127.0.0.1")
    port = int(os.getenv("INVENTORYX_API_PORT", "8000"))
    uvicorn.run(create_app(), host=host, port=port)
