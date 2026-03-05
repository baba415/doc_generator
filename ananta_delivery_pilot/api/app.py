"""Merchant Pilot API — FastAPI application.

Governed by contracts/pilot_event_contract.md v0.1.
All state mutations go through apply_transition / apply_prep_evidence / apply_prep_note.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from api.config import load_catalog_meta, settings
from api.routes import actions, events, evidence, health, notes, work_items


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: verify DB, init repo+validator, ensure evidence dir."""
    db_path = Path(settings.db_path)
    if not db_path.exists():
        raise RuntimeError(
            f"Database not found at {db_path}. "
            "Run init_db first (e.g. via scripts/csv_import.py)."
        )

    from adapters.sqlite_repo import SQLiteRepo
    from domain.event_ledger import EnvelopeValidator

    repo = SQLiteRepo(db_path)
    config_dir = Path(settings.config_dir)
    req_path = config_dir / "core_event_requirements.json"
    repo._validator = EnvelopeValidator(req_path)

    evidence_dir = Path(settings.evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)

    meta = load_catalog_meta(settings.config_dir)

    app.state.repo = repo
    app.state.meta = meta
    app.state.evidence_dir = str(evidence_dir)

    yield
    # shutdown — nothing to clean up for SQLite


app = FastAPI(
    title="Merchant Pilot API",
    description=(
        "Governed by contracts/pilot_event_contract.md v0.1. "
        "All state mutations go through apply_transition / apply_prep_evidence / apply_prep_note."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(health.router)
app.include_router(work_items.router)
app.include_router(actions.router)
app.include_router(evidence.router)
app.include_router(notes.router)
app.include_router(events.router)
