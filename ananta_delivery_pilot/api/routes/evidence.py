"""POST /api/v1/evidence         — multipart upload (PREP_EVIDENCE).
GET  /api/v1/evidence           — list evidence items for an entity (?entity_id=X).
GET  /api/v1/evidence/{id}/link — short-lived evidence URL (pilot-only, §5.2).

§1.4 compliance: no direct SQL for operational data. apply_prep_evidence()
writes the event atomically; file is stored on disk as a binary blob.

§3.2 compliance: idempotency is checked BEFORE any file or DB write. Evidence_id
is derived deterministically from idempotency_key so the payload hash is stable
across retries — enabling true dedup instead of conflict.

Event_log IS the truth for evidence metadata. evidence_originals is not used
by this API.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile

from api.auth import verify_api_key

router = APIRouter(dependencies=[Depends(verify_api_key)])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _deterministic_uuid(key: str) -> str:
    """Derive a stable UUID from a string (SHA-256 → first 16 bytes).

    Same key always produces the same UUID — required so that evidence_id is
    stable across retries and the payload canonical hash doesn't drift.
    """
    h = hashlib.sha256(key.encode()).digest()[:16]
    return str(uuid.UUID(bytes=h))


def _entity_exists(repo, entity_type: str, entity_id: str) -> bool:
    """Return True if the entity exists in the appropriate truth table."""
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


# ---------------------------------------------------------------------------
# GET /api/v1/evidence
# ---------------------------------------------------------------------------

@router.get("/api/v1/evidence")
async def list_evidence(
    entity_id: str = Query(..., description="Entity ID to fetch evidence for"),
    request: Request = None,
) -> dict:
    """Return all evidence items for an entity, sourced from event_log.

    Evidence metadata lives in PREP_EVIDENCE event payloads — the event IS
    the authoritative record (§1.3, §1.4).
    """
    repo = request.app.state.repo
    meta = request.app.state.meta

    conn = repo._connect()
    try:
        event_rows = conn.execute(
            "SELECT payload_json, created_at FROM event_log "
            "WHERE entity_id = ? AND event_type LIKE 'pilot:evidence:%' "
            "ORDER BY created_at ASC",
            (entity_id,),
        ).fetchall()
    finally:
        conn.close()

    items = []
    for er in event_rows:
        try:
            p = json.loads(er["payload_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        items.append({
            "evidence_id": p.get("evidence_id", ""),
            "evidence_kind": p.get("evidence_kind", ""),
            "status": "UNLINKED",
            "content_hash": p.get("content_hash"),
            "filename": p.get("filename"),
            "submitted_at": er["created_at"],
            "verified_at": None,
            "notes": p.get("note"),
        })

    return {
        "schema_version": meta["schema_version"],
        "core_requirements_ref": meta["core_requirements_ref"],
        "core_event_requirements_hash": meta["core_event_requirements_hash"],
        "entity_id": entity_id,
        "items": items,
        "total": len(items),
    }


# ---------------------------------------------------------------------------
# POST /api/v1/evidence
# ---------------------------------------------------------------------------

@router.post("/api/v1/evidence")
async def upload_evidence(
    request: Request,
    file: UploadFile = File(...),
    entity_type: str = Form(...),
    entity_id: str = Form(...),
    evidence_kind: str = Form(...),
    idempotency_key: str = Form(...),
    note: str = Form(""),
) -> dict:
    from domain.event_ledger import (
        IdempotencyConflictError,
        SchemaValidationError,
        canonical_hash,
    )

    repo = request.app.state.repo
    meta = request.app.state.meta

    # FIX 4: check entity exists before any processing
    if not _entity_exists(repo, entity_type, entity_id):
        raise HTTPException(
            status_code=404,
            detail=f"Entity {entity_type}:{entity_id!r} not found",
        )

    content = await file.read()
    file_hash = _sha256_bytes(content)
    size_bytes = len(content)
    original_filename = file.filename or "upload"

    # FIX 2: deterministic evidence_id — same idempotency_key → same UUID
    evidence_id = _deterministic_uuid(idempotency_key)
    evidence_dir = Path(request.app.state.evidence_dir)
    stored_path = evidence_dir / f"{evidence_id}_{original_filename}"

    # Build payload deterministically before any I/O
    payload = {
        "evidence_id": evidence_id,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "evidence_kind": evidence_kind,
        "filename": original_filename,
        "content_hash": file_hash,
        "size_bytes": size_bytes,
        "storage_ref": str(stored_path),
        "note": note or None,
        "source_system": "api",
    }

    # FIX 2: idempotency check BEFORE any file or DB write
    c_hash = canonical_hash(payload)
    idem_status, idem_val = repo._check_idempotency(idempotency_key, c_hash)

    if idem_status == "dedup":
        # Return original receipt with no file write and no DB write (§3.2)
        receipt = _build_receipt(repo, idem_val, True, meta)
        receipt["evidence_ref"] = {
            "evidence_id": evidence_id,
            "storage_ref": str(stored_path),
            "filename": original_filename,
            "content_hash": file_hash,
            "size_bytes": size_bytes,
        }
        return receipt

    if idem_status == "conflict":
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Idempotency conflict",
                "idempotency_key": idempotency_key,
                "existing_hash": idem_val,
                "new_hash": c_hash,
            },
        )

    # NEW path: write file, then record event atomically (FIX 1 — no direct SQL)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    stored_path.write_bytes(content)

    try:
        # FIX 1: apply_prep_evidence with evidence_updates={} records the event
        # atomically without any direct SQL INSERT/UPDATE on truth tables.
        # The event payload IS the authoritative evidence record.
        result = repo.apply_prep_evidence(
            event_type="pilot:evidence:uploaded",
            entity_type=entity_type,
            entity_id=entity_id,
            payload=payload,
            evidence_table="evidence_originals",
            evidence_pk_column="evidence_id",
            evidence_id=evidence_id,
            evidence_updates={},
            idempotency_key=idempotency_key,
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

    receipt = _build_receipt(repo, result["event_id"], result["deduped"], meta)
    receipt["evidence_ref"] = {
        "evidence_id": evidence_id,
        "storage_ref": str(stored_path),
        "filename": original_filename,
        "content_hash": file_hash,
        "size_bytes": size_bytes,
    }
    return receipt


# ---------------------------------------------------------------------------
# GET /api/v1/evidence/{evidence_id}/link
# ---------------------------------------------------------------------------

@router.get("/api/v1/evidence/{evidence_id}/link")
async def evidence_link(evidence_id: str, request: Request) -> dict:
    """Return file URL for evidence — looked up from event_log payload (§5.2)."""
    repo = request.app.state.repo
    meta = request.app.state.meta

    conn = repo._connect()
    try:
        row = conn.execute(
            "SELECT payload_json FROM event_log "
            "WHERE event_type LIKE 'pilot:evidence:%' "
            "AND json_extract(payload_json, '$.evidence_id') = ? "
            "LIMIT 1",
            (evidence_id,),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        raise HTTPException(status_code=404, detail=f"Evidence {evidence_id!r} not found")

    try:
        storage_ref = json.loads(row["payload_json"] or "{}").get("storage_ref", "")
    except (json.JSONDecodeError, TypeError):
        storage_ref = ""

    return {
        "evidence_id": evidence_id,
        "url": f"file://{storage_ref}",
        "expires_at": None,
        # FIX 3: canonical trio on every response (§6.3)
        "schema_version": meta["schema_version"],
        "core_requirements_ref": meta["core_requirements_ref"],
        "core_event_requirements_hash": meta["core_event_requirements_hash"],
    }
