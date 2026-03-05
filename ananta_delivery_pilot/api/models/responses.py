"""Pydantic response models — governed by contracts/pilot_event_contract.md v0.1.

Receipt matches contract §2.2 exactly, with API-layer enrichments.
Every response includes the canonical trio: schema_version, core_requirements_ref,
core_event_requirements_hash.

Settlement fence: none of the §1.2 forbidden fields appear here.
Forbidden: release_ok, hold_reason, settlement_status, capital_release_at,
  waterfall_*, payment_release_*, netting_*, ledger_posting_*
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Canonical trio base (every response inherits this)
# ---------------------------------------------------------------------------

class CanonicalBase(BaseModel):
    schema_version: str
    core_requirements_ref: str
    core_event_requirements_hash: str


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

class HealthResponse(CanonicalBase):
    status: str
    version: str
    pilot_contract_ref: str


# ---------------------------------------------------------------------------
# Receipt — contract §2.2 + API enrichments
# ---------------------------------------------------------------------------

class Receipt(CanonicalBase):
    event_id: str
    event_type: str
    entity_type: str
    entity_id: str
    content_hash: Optional[str] = None
    idempotency_key: str
    schema_ok: int
    validated_against_ref: Optional[str] = None
    validated_against_hash: Optional[str] = None
    created_at: str
    # API enrichments
    applied: bool
    deduped: bool
    data_source: str = "PILOT"
    new_state: Optional[str] = None  # populated for TRANSITION actions


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------

class EvidenceRef(BaseModel):
    evidence_id: str
    storage_ref: str
    filename: str
    content_hash: str
    size_bytes: int


class EvidenceReceipt(Receipt):
    evidence_ref: EvidenceRef


class EvidenceLinkResponse(CanonicalBase):
    evidence_id: str
    url: str
    expires_at: Optional[str] = None


class EvidenceItem(BaseModel):
    evidence_id: str
    evidence_kind: str
    status: str
    content_hash: Optional[str] = None
    filename: Optional[str] = None
    submitted_at: Optional[str] = None
    verified_at: Optional[str] = None
    notes: Optional[str] = None


class EvidenceListResponse(CanonicalBase):
    entity_id: str
    items: list[EvidenceItem]
    total: int


# ---------------------------------------------------------------------------
# WorkItem — projected entity for workbench rendering
# §6.4: data_source + canonical trio required
# ---------------------------------------------------------------------------

class AvailableAction(BaseModel):
    id: str
    type: str  # TRANSITION | PREP_EVIDENCE | PREP_NOTE
    label: str
    description: str
    event_type: str
    current_state: str
    resulting_state: str
    irreversible: bool = False
    required_fields: list[str] = []


class WorkItem(CanonicalBase):
    id: str
    type: str           # Trade | Shipment | Exception
    title: str
    description: str
    status: str         # ready | blocked | pending | awaiting_approval
    priority: str       # high | medium | low
    org: str
    node: str
    sla_deadline: Optional[str] = None
    data_source: str = "PILOT"
    event_count: int
    available_actions: list[AvailableAction]
    amount: Optional[float] = None
    quantity: Optional[float] = None
    deliveries: list[Any] = []
    created_at: str
    updated_at: str


class WorkItemDetail(WorkItem):
    evidence_summary: dict[str, int] = {}


class WorkItemListResponse(CanonicalBase):
    items: list[WorkItem]
    total: int


# ---------------------------------------------------------------------------
# Event trace — for workbench Inspector Full Trace tab
# ---------------------------------------------------------------------------

class TraceEvent(BaseModel):
    event_id: str
    event_type: str
    entity_type: str
    entity_id: str
    content_hash: Optional[str] = None
    idempotency_key: Optional[str] = None
    schema_ok: int
    validated_against_ref: Optional[str] = None
    payload: Optional[Any] = None
    created_at: str
    rails_trade_id: Optional[str] = None


class EventTraceResponse(CanonicalBase):
    entity_id: str
    events: list[TraceEvent]
    total: int
