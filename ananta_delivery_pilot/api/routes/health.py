"""GET /api/v1/health — static, no auth required."""
from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/api/v1/health")
async def health(request: Request) -> dict:
    meta = request.app.state.meta
    return {
        "status": "ok",
        "version": "0.1.0",
        "schema_version": meta["schema_version"],
        "core_requirements_ref": meta["core_requirements_ref"],
        "core_event_requirements_hash": meta["core_event_requirements_hash"],
        "pilot_contract_ref": meta["pilot_contract_ref"],
    }
