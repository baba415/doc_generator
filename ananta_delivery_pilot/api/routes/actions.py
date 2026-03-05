"""POST /api/v1/actions/apply — core mutation endpoint.

All state mutations go through apply_transition / apply_prep_evidence / apply_prep_note.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from api.auth import verify_api_key
from api.enrichment import (
    build_enrichment_report,
    enrich_common_fields,
    enrich_event_specific_fields,
)
from api.models.requests import ApplyActionRequest

router = APIRouter(dependencies=[Depends(verify_api_key)])


# Entity resolution: contract or delivery
_ENTITY_MAP = {
    "trade": ("contracts", "contract_id", "lpo_state"),
    "contract": ("contracts", "contract_id", "lpo_state"),
    "delivery": ("deliveries", "delivery_id", "status"),
}


def _resolve_entity(repo, work_item_id: str) -> tuple:
    """Return (entity_type, table, pk_column, state_column) or None."""
    conn = repo._connect()
    try:
        row = conn.execute(
            "SELECT contract_id FROM contracts WHERE contract_id = ?",
            (work_item_id,),
        ).fetchone()
        if row:
            return "trade", "contracts", "contract_id", "lpo_state"

        row = conn.execute(
            "SELECT delivery_id FROM deliveries WHERE delivery_id = ?",
            (work_item_id,),
        ).fetchone()
        if row:
            return "delivery", "deliveries", "delivery_id", "status"
    finally:
        conn.close()
    return None


def _fetch_entity_row(repo, table: str, pk_column: str, entity_id: str) -> dict:
    """Fetch the full entity row as a dict (column_name → value).

    For contracts: joins with parties to include operator_uuid (UUID from core_uuid),
    so enrichment can fill actor_org_id with a valid UUID.
    """
    conn = repo._connect()
    try:
        if table == "contracts":
            cursor = conn.execute(
                """
                SELECT c.*, p.core_uuid AS operator_uuid
                FROM contracts c
                LEFT JOIN parties p ON p.party_id = c.operator_id
                WHERE c.{} = ?
                """.format(pk_column),
                (entity_id,),
            )
        else:
            cursor = conn.execute(
                "SELECT * FROM {} WHERE {} = ?".format(table, pk_column),
                (entity_id,),
            )
        columns = [desc[0] for desc in cursor.description]
        row = cursor.fetchone()
        return dict(zip(columns, row)) if row else {}
    finally:
        conn.close()


def _build_receipt(
    repo, event_id: str, deduped: bool, meta: dict, new_state=None
) -> dict:
    """Fetch event row and build full API receipt (§2.2 + enrichments)."""
    conn = repo._connect()
    try:
        row = conn.execute(
            "SELECT * FROM event_log WHERE event_id = ?", (event_id,)
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        raise ValueError(f"event_id {event_id!r} not found in event_log")

    return {
        # §2.2 fields
        "event_id": row["event_id"],
        "event_type": row["event_type"],
        "entity_type": row["entity_type"],
        "entity_id": row["entity_id"],
        "content_hash": row["content_hash"],
        "idempotency_key": row["idempotency_key"],
        "schema_ok": row["schema_ok"],
        "validated_against_ref": row["validated_against_ref"],
        "validated_against_hash": row["validated_against_hash"],
        "created_at": row["created_at"],
        # API enrichments
        "applied": True,
        "deduped": deduped,
        "data_source": "PILOT",
        "new_state": new_state,
        # canonical trio
        "schema_version": meta["schema_version"],
        "core_requirements_ref": meta["core_requirements_ref"],
        "core_event_requirements_hash": meta["core_event_requirements_hash"],
    }


@router.post("/api/v1/actions/apply")
async def apply_action(body: ApplyActionRequest, request: Request) -> dict:
    from domain.event_ledger import IdempotencyConflictError, SchemaValidationError

    repo = request.app.state.repo
    meta = request.app.state.meta

    resolved = _resolve_entity(repo, body.work_item_id)
    if resolved is None:
        raise HTTPException(
            status_code=404,
            detail=f"Entity {body.work_item_id!r} not found",
        )

    entity_type, table, pk_column, state_column = resolved

    # Fetch full entity row for enrichment
    entity_row = _fetch_entity_row(repo, table, pk_column, body.work_item_id)

    # Three-step enrichment (TRANSITION actions only)
    enrichment_report = None
    if body.action_type == "TRANSITION":
        payload_after_common, common_added = enrich_common_fields(body.payload, entity_row)
        payload_after_specific, specific_added = enrich_event_specific_fields(
            payload_after_common, entity_row, body.event_type
        )
        enriched_payload = payload_after_specific
        enrichment_report = build_enrichment_report(common_added, specific_added, missing=[])
    else:
        enriched_payload = body.payload

    try:
        if body.action_type == "TRANSITION":
            if body.new_state is None:
                raise HTTPException(
                    status_code=422,
                    detail="new_state is required for TRANSITION actions",
                )
            result = repo.apply_transition(
                event_type=body.event_type,
                entity_type=entity_type,
                entity_id=body.work_item_id,
                payload=enriched_payload,
                table=table,
                pk_column=pk_column,
                state_column=state_column,
                new_state=body.new_state,
                idempotency_key=body.idempotency_key,
            )

        elif body.action_type == "PREP_EVIDENCE":
            result = repo.apply_prep_evidence(
                event_type=body.event_type,
                entity_type=entity_type,
                entity_id=body.work_item_id,
                payload=body.payload,
                evidence_table="evidence_originals",
                evidence_pk_column="evidence_id",
                evidence_id=body.work_item_id,
                evidence_updates={},
                idempotency_key=body.idempotency_key,
            )

        elif body.action_type == "PREP_NOTE":
            result = repo.apply_prep_note(
                event_type=body.event_type,
                entity_type=entity_type,
                entity_id=body.work_item_id,
                payload=body.payload,
                idempotency_key=body.idempotency_key,
            )

        else:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown action_type: {body.action_type!r}",
            )

    except SchemaValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={"errors": exc.errors, "message": str(exc)},
        )
    except IdempotencyConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Idempotency conflict",
                "idempotency_key": exc.idempotency_key,
                "existing_hash": exc.existing_hash,
                "new_hash": exc.new_hash,
            },
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    receipt_new_state = body.new_state if body.action_type == "TRANSITION" else None
    receipt = _build_receipt(repo, result["event_id"], result["deduped"], meta,
                             new_state=receipt_new_state)
    if enrichment_report is not None:
        receipt["enrichment"] = {
            "enrichment_version": enrichment_report.enrichment_version,
            "fields_added": enrichment_report.fields_added,
            "missing_after_enrichment": enrichment_report.missing_after_enrichment,
        }
    return receipt
