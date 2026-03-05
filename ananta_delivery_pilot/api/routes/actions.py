"""POST /api/v1/actions/apply — core mutation endpoint.

All state mutations go through apply_transition / apply_prep_evidence / apply_prep_note.
"""
from __future__ import annotations

import json as _json

from fastapi import APIRouter, Depends, HTTPException, Request

from api.auth import verify_api_key
from api.enrichment import (
    build_enrichment_report,
    enrich_common_fields,
    enrich_event_specific_fields,
    validate_required_after_enrichment,
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
    from domain.event_ledger import IdempotencyConflictError, SchemaValidationError, canonical_hash

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
        # FIX 3: Pre-enrichment idempotency check using caller's raw payload hash.
        # This ensures retries dedup correctly even when entity data has changed
        # (which would otherwise produce a different enriched hash).
        raw_hash = canonical_hash(body.payload)
        conn = repo._connect()
        try:
            existing_row = conn.execute(
                "SELECT event_id, payload_json FROM event_log WHERE idempotency_key = ?",
                (body.idempotency_key,),
            ).fetchone()
        finally:
            conn.close()

        if existing_row is not None:
            stored = _json.loads(existing_row["payload_json"]) if existing_row["payload_json"] else {}
            stored_caller_hash = stored.get("_caller_payload_hash")
            if stored_caller_hash is not None:
                if stored_caller_hash == raw_hash:
                    # Same caller intent → DEDUP (skip enrichment + apply_transition)
                    return _build_receipt(
                        repo, existing_row["event_id"], True, meta,
                        new_state=body.new_state,
                    )
                else:
                    # Different caller payload for same key → CONFLICT
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "message": "Idempotency conflict",
                            "idempotency_key": body.idempotency_key,
                        },
                    )
            # stored_caller_hash is None (old event, pre-FIX3) → fall through to apply_transition

        payload_after_common, common_added = enrich_common_fields(body.payload, entity_row)
        payload_after_specific, specific_added = enrich_event_specific_fields(
            payload_after_common, entity_row, body.event_type
        )
        enriched_payload = payload_after_specific

        # FIX 2: Populate missing_after_enrichment for observable error reporting
        missing_after = validate_required_after_enrichment(
            enriched_payload, body.event_type, repo._validator
        )
        enrichment_report = build_enrichment_report(common_added, specific_added, missing=missing_after)

        # FIX 3: Embed caller's raw hash in stored payload so retries can dedup
        # by caller intent rather than enriched content (entity data may change).
        enriched_payload["_caller_payload_hash"] = raw_hash
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
        # FIX 2: include enrichment report in 422 detail so caller can see
        # what was filled and what is still missing after enrichment
        detail: dict = {"errors": exc.errors, "message": str(exc)}
        if enrichment_report is not None:
            detail["enrichment"] = {
                "enrichment_version": enrichment_report.enrichment_version,
                "fields_added": enrichment_report.fields_added,
                "missing_after_enrichment": enrichment_report.missing_after_enrichment,
            }
        raise HTTPException(status_code=422, detail=detail)
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
