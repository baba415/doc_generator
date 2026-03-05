"""POST /api/v1/notes — add an operator note (PREP_NOTE)."""
from fastapi import APIRouter, Depends, HTTPException, Request

from api.auth import verify_api_key
from api.models.requests import NoteRequest

router = APIRouter(dependencies=[Depends(verify_api_key)])


def _entity_exists(repo, entity_type: str, entity_id: str) -> bool:
    _TABLE_MAP = {
        "trade": ("contracts", "contract_id"),
        "contract": ("contracts", "contract_id"),
        "delivery": ("deliveries", "delivery_id"),
    }
    entry = _TABLE_MAP.get(entity_type)
    if entry is None:
        return False
    table, pk = entry
    conn = repo._connect()
    try:
        return conn.execute(
            f"SELECT 1 FROM {table} WHERE {pk} = ?", (entity_id,)
        ).fetchone() is not None
    finally:
        conn.close()


def _build_receipt(repo, event_id: str, deduped: bool, meta: dict) -> dict:
    conn = repo._connect()
    try:
        row = conn.execute(
            "SELECT * FROM event_log WHERE event_id = ?", (event_id,)
        ).fetchone()
    finally:
        conn.close()
    return {
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
        "applied": True,
        "deduped": deduped,
        "data_source": "PILOT",
        "new_state": None,
        "schema_version": meta["schema_version"],
        "core_requirements_ref": meta["core_requirements_ref"],
        "core_event_requirements_hash": meta["core_event_requirements_hash"],
    }


@router.post("/api/v1/notes")
async def add_note(body: NoteRequest, request: Request) -> dict:
    from domain.event_ledger import IdempotencyConflictError, SchemaValidationError

    repo = request.app.state.repo
    meta = request.app.state.meta

    # FIX 4: check entity exists before processing
    if not _entity_exists(repo, body.entity_type, body.entity_id):
        raise HTTPException(
            status_code=404,
            detail=f"Entity {body.entity_type}:{body.entity_id!r} not found",
        )

    payload = {
        "entity_type": body.entity_type,
        "entity_id": body.entity_id,
        "note": body.note,
        "source_system": "api",
    }

    try:
        result = repo.apply_prep_note(
            event_type="pilot:note:added",
            entity_type=body.entity_type,
            entity_id=body.entity_id,
            payload=payload,
            idempotency_key=body.idempotency_key,
        )
    except SchemaValidationError as exc:
        raise HTTPException(status_code=422, detail={"errors": exc.errors})
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
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return _build_receipt(repo, result["event_id"], result["deduped"], meta)
