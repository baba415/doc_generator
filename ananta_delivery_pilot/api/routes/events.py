"""GET /api/v1/events/{entity_id} — event trace for an entity."""
import json

from fastapi import APIRouter, Depends, Request

from api.auth import verify_api_key

router = APIRouter(dependencies=[Depends(verify_api_key)])


@router.get("/api/v1/events/{entity_id}")
async def get_events(entity_id: str, request: Request) -> dict:
    repo = request.app.state.repo
    meta = request.app.state.meta

    conn = repo._connect()
    try:
        rows = conn.execute(
            "SELECT * FROM event_log WHERE entity_id = ? ORDER BY created_at ASC",
            (entity_id,),
        ).fetchall()
    finally:
        conn.close()

    events = []
    for row in rows:
        payload_raw = row["payload_json"]
        try:
            payload = json.loads(payload_raw) if payload_raw else None
        except (json.JSONDecodeError, TypeError):
            payload = None

        events.append({
            "event_id": row["event_id"],
            "event_type": row["event_type"],
            "entity_type": row["entity_type"],
            "entity_id": row["entity_id"],
            "content_hash": row["content_hash"],
            "idempotency_key": row["idempotency_key"],
            "schema_ok": row["schema_ok"],
            "validated_against_ref": row["validated_against_ref"],
            "payload": payload,
            "created_at": row["created_at"],
            "rails_trade_id": row["rails_trade_id"],
        })

    return {
        "schema_version": meta["schema_version"],
        "core_requirements_ref": meta["core_requirements_ref"],
        "core_event_requirements_hash": meta["core_event_requirements_hash"],
        "entity_id": entity_id,
        "events": events,
        "total": len(events),
    }
