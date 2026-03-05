"""GET /api/v1/work-items and GET /api/v1/work-items/{id}."""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from api.auth import verify_api_key
from api.projections import (
    project_delivery_detail,
    project_delivery_to_work_item,
    project_trade_detail,
    project_trade_to_work_item,
)

router = APIRouter(dependencies=[Depends(verify_api_key)])


def _get_events(repo, entity_type: str, entity_id: str) -> list[dict]:
    conn = repo._connect()
    try:
        rows = conn.execute(
            "SELECT * FROM event_log WHERE entity_type = ? AND entity_id = ? "
            "ORDER BY created_at ASC",
            (entity_type, entity_id),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _get_evidence(repo, entity_id: str) -> list[dict]:
    conn = repo._connect()
    try:
        rows = conn.execute(
            "SELECT * FROM evidence_originals "
            "WHERE contract_id = ? OR delivery_id = ?",
            (entity_id, entity_id),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/api/v1/work-items")
async def list_work_items(request: Request) -> dict:
    repo = request.app.state.repo
    meta = request.app.state.meta
    items: list[dict] = []

    conn = repo._connect()
    try:
        trades = conn.execute(
            "SELECT * FROM contracts ORDER BY created_at DESC LIMIT 200"
        ).fetchall()
        deliveries = conn.execute(
            "SELECT * FROM deliveries ORDER BY created_at DESC LIMIT 200"
        ).fetchall()
    finally:
        conn.close()

    for row in trades:
        events = _get_events(repo, "trade", row["contract_id"])
        evidence = _get_evidence(repo, row["contract_id"])
        items.append(project_trade_to_work_item(row, events, evidence, meta))

    for row in deliveries:
        events = _get_events(repo, "delivery", row["delivery_id"])
        evidence = _get_evidence(repo, row["delivery_id"])
        items.append(project_delivery_to_work_item(row, events, evidence, meta))

    return {
        "schema_version": meta["schema_version"],
        "core_requirements_ref": meta["core_requirements_ref"],
        "core_event_requirements_hash": meta["core_event_requirements_hash"],
        "items": items,
        "total": len(items),
    }


@router.get("/api/v1/work-items/{item_id}")
async def get_work_item(item_id: str, request: Request) -> dict:
    repo = request.app.state.repo
    meta = request.app.state.meta

    conn = repo._connect()
    try:
        trade_row = conn.execute(
            "SELECT * FROM contracts WHERE contract_id = ?", (item_id,)
        ).fetchone()
        if trade_row:
            events = _get_events(repo, "trade", item_id)
            evidence = _get_evidence(repo, item_id)
            return {
                "schema_version": meta["schema_version"],
                "core_requirements_ref": meta["core_requirements_ref"],
                "core_event_requirements_hash": meta["core_event_requirements_hash"],
                **project_trade_detail(trade_row, events, evidence, meta),
            }

        delivery_row = conn.execute(
            "SELECT * FROM deliveries WHERE delivery_id = ?", (item_id,)
        ).fetchone()
        if delivery_row:
            events = _get_events(repo, "delivery", item_id)
            evidence = _get_evidence(repo, item_id)
            return {
                "schema_version": meta["schema_version"],
                "core_requirements_ref": meta["core_requirements_ref"],
                "core_event_requirements_hash": meta["core_event_requirements_hash"],
                **project_delivery_detail(delivery_row, events, evidence, meta),
            }
    finally:
        conn.close()

    raise HTTPException(status_code=404, detail=f"Work item {item_id!r} not found")
