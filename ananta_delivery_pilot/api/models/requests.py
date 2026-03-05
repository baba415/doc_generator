"""Pydantic request models for the Merchant Pilot API."""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel


class ApplyActionRequest(BaseModel):
    work_item_id: str
    action_type: str  # TRANSITION | PREP_EVIDENCE | PREP_NOTE
    event_type: str
    payload: dict[str, Any]
    idempotency_key: str
    new_state: Optional[str] = None  # required for TRANSITION


class NoteRequest(BaseModel):
    entity_type: str
    entity_id: str
    note: str
    idempotency_key: str
