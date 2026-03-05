"""POST /api/v1/evidence — multipart upload.
GET  /api/v1/evidence/{evidence_id}/link — evidence URL (pilot-only).
"""
from __future__ import annotations

import hashlib
import sqlite3
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile

from api.auth import verify_api_key

router = APIRouter(dependencies=[Depends(verify_api_key)])


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


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
        "schema_version": meta["schema_version"],
        "core_requirements_ref": meta["core_requirements_ref"],
        "core_event_requirements_hash": meta["core_event_requirements_hash"],
    }


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
    from domain.event_ledger import IdempotencyConflictError, SchemaValidationError

    repo = request.app.state.repo
    meta = request.app.state.meta
    evidence_dir = Path(request.app.state.evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)

    content = await file.read()
    content_hash = _sha256_bytes(content)
    size_bytes = len(content)
    evidence_id = str(uuid.uuid4())
    original_filename = file.filename or "upload"
    stored_filename = f"{evidence_id}_{original_filename}"
    stored_path = evidence_dir / stored_filename
    stored_path.write_bytes(content)

    # Insert evidence_originals record (entity creation, not a state mutation)
    now_str = _now_iso()
    conn = repo._connect()
    try:
        contract_id_val = entity_id if entity_type in ("trade", "contract") else None
        delivery_id_val = entity_id if entity_type == "delivery" else None
        conn.execute(
            """
            INSERT INTO evidence_originals (
                evidence_id, contract_id, delivery_id,
                file_name, doc_type, link_status,
                source_path, stored_path, sha256,
                captured_at, created_at
            ) VALUES (?, ?, ?, ?, ?, 'UNLINKED', ?, ?, ?, ?, ?)
            """,
            (
                evidence_id,
                contract_id_val,
                delivery_id_val,
                original_filename,
                evidence_kind,
                str(stored_path),
                str(stored_path),
                content_hash,
                now_str,
                now_str,
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        pass  # duplicate evidence_id — tolerate
    finally:
        conn.close()

    # Record PREP_EVIDENCE event
    payload = {
        "evidence_id": evidence_id,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "evidence_kind": evidence_kind,
        "filename": original_filename,
        "content_hash": content_hash,
        "size_bytes": size_bytes,
        "storage_ref": str(stored_path),
        "note": note or None,
        "source_system": "api",
    }

    try:
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
        "content_hash": content_hash,
        "size_bytes": size_bytes,
    }
    return receipt


@router.get("/api/v1/evidence/{evidence_id}/link")
async def evidence_link(evidence_id: str, request: Request) -> dict:
    repo = request.app.state.repo
    evidence_dir = Path(request.app.state.evidence_dir)

    conn = repo._connect()
    try:
        row = conn.execute(
            "SELECT stored_path, file_name FROM evidence_originals WHERE evidence_id = ?",
            (evidence_id,),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        raise HTTPException(status_code=404, detail=f"Evidence {evidence_id!r} not found")

    stored_path = row["stored_path"]
    url = f"file://{stored_path}"
    return {
        "evidence_id": evidence_id,
        "url": url,
        "expires_at": None,
    }


def _now_iso() -> str:
    from core.time import utc_now_iso_z
    return utc_now_iso_z()
