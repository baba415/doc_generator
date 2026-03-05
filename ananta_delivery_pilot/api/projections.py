"""Entity → WorkItem projection logic.

Maps pilot entity table rows to the WorkItem shape expected by the workbench.
WorkItem shape is defined by this API per contract §6.4.
"""
from __future__ import annotations

import json
from typing import Any


# ---------------------------------------------------------------------------
# State → WorkItem status mapping
# ---------------------------------------------------------------------------

_CONTRACT_STATUS_MAP = {
    "DRAFT": "pending",
    "ACTIVE": "ready",
    "OPEN": "pending",
    "CLOSED": "blocked",
    "CANCELLED": "blocked",
    "EXPIRED": "blocked",
    "PENDING_APPROVAL": "awaiting_approval",
    "SUSPENDED": "blocked",
}

_DELIVERY_STATUS_MAP = {
    "PLANNED": "pending",
    "DISPATCHED": "ready",
    "IN_TRANSIT": "ready",
    "DELIVERED": "awaiting_approval",
    "INVOICED": "awaiting_approval",
    "CANCELLED": "blocked",
    "EXCEPTION": "blocked",
}


def _contract_status(lpo_state: str) -> str:
    return _CONTRACT_STATUS_MAP.get(lpo_state, "pending")


def _delivery_status(status: str) -> str:
    return _DELIVERY_STATUS_MAP.get(status, "pending")


# ---------------------------------------------------------------------------
# Available actions derived from entity state
# ---------------------------------------------------------------------------

_TRADE_ACTIONS: dict[str, list[dict]] = {
    "_any": [
        {
            "id": "apply_terms",
            "type": "TRANSITION",
            "label": "Submit Terms",
            "description": "Submit trade terms for this contract",
            "event_type": "TERMS_SUBMITTED",
            "current_state": "ACTIVE",
            "resulting_state": "ACTIVE",
            "irreversible": False,
            "required_fields": ["trade_id", "payload.actor_org_id", "payload.payment_terms",
                                 "payload.delivery_term", "payload.delivery_location"],
        },
        {
            "id": "add_note",
            "type": "PREP_NOTE",
            "label": "Add Note",
            "description": "Add an operator note to this trade",
            "event_type": "pilot:note:added",
            "current_state": "_any",
            "resulting_state": "_any",
            "irreversible": False,
            "required_fields": ["note"],
        },
        {
            "id": "upload_evidence",
            "type": "PREP_EVIDENCE",
            "label": "Upload Evidence",
            "description": "Attach evidence document to this trade",
            "event_type": "pilot:evidence:uploaded",
            "current_state": "_any",
            "resulting_state": "_any",
            "irreversible": False,
            "required_fields": ["file", "evidence_kind"],
        },
    ]
}

_DELIVERY_ACTIONS: dict[str, list[dict]] = {
    "_any": [
        {
            "id": "confirm_delivery",
            "type": "TRANSITION",
            "label": "Confirm Delivery",
            "description": "Confirm that the delivery was completed",
            "event_type": "DELIVERY_CONFIRMED",
            "current_state": "DISPATCHED",
            "resulting_state": "DELIVERED",
            "irreversible": False,
            "required_fields": ["shipment_id", "payload.actor_org_id"],
        },
        {
            "id": "add_note",
            "type": "PREP_NOTE",
            "label": "Add Note",
            "description": "Add an operator note to this delivery",
            "event_type": "pilot:note:added",
            "current_state": "_any",
            "resulting_state": "_any",
            "irreversible": False,
            "required_fields": ["note"],
        },
        {
            "id": "upload_evidence",
            "type": "PREP_EVIDENCE",
            "label": "Upload Evidence",
            "description": "Attach evidence document to this delivery",
            "event_type": "pilot:evidence:uploaded",
            "current_state": "_any",
            "resulting_state": "_any",
            "irreversible": False,
            "required_fields": ["file", "evidence_kind"],
        },
    ]
}


def _trade_actions(lpo_state: str) -> list[dict]:
    return _TRADE_ACTIONS["_any"]


def _delivery_actions(status: str) -> list[dict]:
    return _DELIVERY_ACTIONS["_any"]


# ---------------------------------------------------------------------------
# Projectors
# ---------------------------------------------------------------------------

def project_trade_to_work_item(
    trade_row: Any,
    events: list[dict],
    evidence: list[dict],
    meta: dict,
) -> dict:
    """Project a trade (contract) row + its events + evidence into a WorkItem."""
    row = dict(trade_row)
    title = f"{row.get('contract_ref', '')} — {row.get('lpo_no', '')}".strip(" —")
    return {
        "id": row["contract_id"],
        "type": "Trade",
        "title": title or row["contract_id"],
        "description": f"Contract {row.get('contract_ref', row['contract_id'])} "
                       f"| Lane {row.get('lane', '?')} | {row.get('currency', '?')}",
        "status": _contract_status(row.get("lpo_state", "ACTIVE")),
        "priority": "medium",
        "org": row.get("buyer_id", ""),
        "node": row.get("lane", ""),
        "sla_deadline": row.get("due_date"),
        "data_source": "PILOT",
        "schema_version": meta["schema_version"],
        "core_requirements_ref": meta["core_requirements_ref"],
        "core_event_requirements_hash": meta["core_event_requirements_hash"],
        "event_count": len(events),
        "available_actions": _trade_actions(row.get("lpo_state", "ACTIVE")),
        "amount": row.get("expected_total_value"),
        "quantity": row.get("expected_total_qty"),
        "deliveries": [],
        "created_at": row.get("created_at", ""),
        "updated_at": row.get("updated_at", ""),
    }


def project_delivery_to_work_item(
    delivery_row: Any,
    events: list[dict],
    evidence: list[dict],
    meta: dict,
) -> dict:
    """Project a delivery row + its events + evidence into a WorkItem."""
    row = dict(delivery_row)
    title = row.get("delivery_ref") or row.get("run_id") or row["delivery_id"]
    return {
        "id": row["delivery_id"],
        "type": "Shipment",
        "title": f"Delivery {title}",
        "description": (
            f"Run {row.get('run_id', '?')} Batch {row.get('batch_id', '?')} "
            f"| {row.get('delivered_qty', '?')} {row.get('unit', '')}"
        ),
        "status": _delivery_status(row.get("status", "PLANNED")),
        "priority": "medium",
        "org": row.get("contract_id", ""),
        "node": "",
        "sla_deadline": row.get("delivery_date"),
        "data_source": "PILOT",
        "schema_version": meta["schema_version"],
        "core_requirements_ref": meta["core_requirements_ref"],
        "core_event_requirements_hash": meta["core_event_requirements_hash"],
        "event_count": len(events),
        "available_actions": _delivery_actions(row.get("status", "PLANNED")),
        "amount": row.get("gross_amount"),
        "quantity": row.get("delivered_qty"),
        "deliveries": [],
        "created_at": row.get("created_at", ""),
        "updated_at": row.get("updated_at", ""),
    }


def project_trade_detail(
    trade_row: Any,
    events: list[dict],
    evidence: list[dict],
    meta: dict,
) -> dict:
    """Same as project_trade_to_work_item but with evidence summary."""
    base = project_trade_to_work_item(trade_row, events, evidence, meta)
    # Evidence summary: count by link_status (or by source if not linked)
    evidence_summary: dict[str, int] = {}
    for ev in evidence:
        status = ev.get("link_status", "UNLINKED")
        evidence_summary[status] = evidence_summary.get(status, 0) + 1
    base["evidence_summary"] = evidence_summary
    return base


def project_delivery_detail(
    delivery_row: Any,
    events: list[dict],
    evidence: list[dict],
    meta: dict,
) -> dict:
    """Same as project_delivery_to_work_item but with evidence summary."""
    base = project_delivery_to_work_item(delivery_row, events, evidence, meta)
    evidence_summary: dict[str, int] = {}
    for ev in evidence:
        status = ev.get("link_status", "UNLINKED")
        evidence_summary[status] = evidence_summary.get(status, 0) + 1
    base["evidence_summary"] = evidence_summary
    return base
