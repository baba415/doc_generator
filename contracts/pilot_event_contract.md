# Pilot Event Contract v0.1

**Contract ID:** `contracts/pilot_event_contract.md`
**Version:** 0.1
**Binding for:** Merchant Pilot API (Day 2), Workbench backend integration, CSV ingestion scripts, any CLI/operator tooling that emits events.

**Change control:** Any change to required fields, idempotency policy, event naming, or evidence schema MUST bump version and require review from arbiter + ChatGPT Plus.

---

## 1. Fences (Binding — Violations Are Contract Drift)

### 1.1 UI Never Writes Truth

Workbench is a projection + command surface only. All truth mutation happens via evented paths through the Pilot API or explicitly-marked bootstrap tooling. Workbench never executes SQL, never calls apply_transition directly, never modifies truth tables.

### 1.2 Settlement Is Out of Scope

Settlement is essential to the full Ananta system but is not implemented in the Pilot Spine. No Pilot API response, receipt, WorkItem projection, or UI element may include settlement-shaped fields. Explicitly forbidden fields:

- `release_ok`
- `hold_reason`
- `settlement_status`
- `capital_release_at`
- `waterfall_*`
- `payment_release_*`
- `netting_*`
- `ledger_posting_*`

If any of these appear in any response type, it is contract drift. Settlement boundary definition and trigger engine design will be addressed in a dedicated Phase when the first real trade completes end-to-end.

### 1.3 Evidence Is Attachment, Not State

A stored file is not proof of verification. Verification is represented only by a PREP_EVIDENCE event, not by file existence. Events are append-only — deleting or moving a file must not rewrite history.

If evidence deletion is ever added, it must be tombstoned and evented, never a silent removal.

### 1.4 Every Mutation Goes Through the Evented Path

All operational state changes must go through exactly one of:

- `apply_transition()` — state mutations
- `apply_prep_evidence()` — evidence linkage
- `apply_prep_note()` — operator annotations

No exceptions. No direct SQL INSERT/UPDATE on truth tables for operational data. Bootstrap tooling (csv_import.py) is explicitly marked as pre-evented and must migrate to these paths.

### 1.5 Dedup Must Not Mutate Truth Tables

An idempotent retry (same idempotency_key + same content_hash) must cause zero writes:

- No new event row
- No truth table UPDATE
- No timestamp changes (including updated_at)
- Return the original receipt unchanged

This is enforced by checking idempotency BEFORE entering any transaction.

---

## 2. As-Implemented Schemas (Binding — Matches Merged Code)

### 2.1 Event Log Columns

As implemented in `adapters/sqlite_repo.py` (PR #9, SHA 8090cf95):

```
event_log:
  id                      INTEGER PRIMARY KEY AUTOINCREMENT
  event_id                TEXT NOT NULL UNIQUE          -- UUID v4
  event_type              TEXT NOT NULL                 -- canonical type
  entity_type             TEXT NOT NULL                 -- trade, delivery, etc.
  entity_id               TEXT NOT NULL                 -- pilot ID
  rails_trade_id          TEXT                          -- parent trade cross-ref
  payload                 TEXT NOT NULL                 -- JSON envelope
  content_hash            TEXT NOT NULL                 -- SHA-256 canonical
  idempotency_key         TEXT NOT NULL                 -- caller-supplied
  schema_ok               INTEGER NOT NULL DEFAULT 1    -- 0 for unknown types
  validated_against_ref   TEXT                          -- catalog commit ref
  validated_against_hash  TEXT                          -- catalog file hash
  replay_obligations      TEXT                          -- JSON array
  created_at              TEXT NOT NULL DEFAULT (datetime('now'))
```

Unique index on `idempotency_key`. Indexes on `(entity_type, entity_id)` and `rails_trade_id`.

### 2.2 Receipt Shape (As Returned by apply_* Methods)

The current receipt returned by `apply_transition()`, `apply_prep_evidence()`, and `apply_prep_note()` contains all fields from the event_log row. At the API boundary, the receipt MUST include at minimum:

```json
{
  "event_id": "string",
  "event_type": "string",
  "entity_type": "string",
  "entity_id": "string",
  "content_hash": "string",
  "idempotency_key": "string",
  "schema_ok": 1,
  "validated_against_ref": "string",
  "validated_against_hash": "string",
  "created_at": "string"
}
```

**API-layer enrichments (add in Merchant API, not in ledger):**

- `applied: true` — the operation took effect
- `deduped: false` — true if this was a replay returning the original event
- `data_source: "PILOT"` — provenance badge for workbench rendering

These are derived from the code path (dedup vs new event) and do not require ledger schema changes.

### 2.3 Canonical Serialization

Content hash: `json.dumps(payload, sort_keys=True, separators=(',', ':'))` → SHA-256 hex digest. This is invariant. Same payload always produces same hash.

---

## 3. Idempotency Policy (Binding)

### 3.1 Caller-Supplied Key

All state-mutating methods accept a required `idempotency_key: str` parameter. The key MUST NOT include content_hash or any hash of the payload.

### 3.2 Semantics

For a given `idempotency_key`:

- **Key not found:** proceed to validate → mutate → append event (single transaction)
- **Key found, content_hash matches:** DEDUP — return existing receipt, zero writes
- **Key found, content_hash differs:** CONFLICT — raise error, write conflict JSONL outside transaction boundary

### 3.3 Allowed Key Formats

Keys are opaque strings. Each source system uses a distinct prefix:

- **Workbench → API:** `ui:{work_item_id}:{action}:{client_request_id}`
- **CSV ingestion:** `csv:{file_sha256}:{row_number}`
- **CLI / scripts:** `cli:{run_id}:{op}:{entity_id}:{attempt}`

Content hash MUST NOT appear in any key format.

### 3.4 Source Provenance

Events generated by any source SHOULD include source metadata in the payload:

- `source_system`: `"workbench"` | `"csv_import"` | `"cli"` | `"api"`
- `source_ref`: request_id | file_sha:row | run_id

This enables replay debugging and audit trail differentiation.

### 3.5 Logging Boundary

Rejection and conflict JSONL logs are written OUTSIDE the DB transaction boundary. The transaction does: validate → mutate → append event (or rollback). After the transaction exits: write rejection/conflict log if needed.

---

## 4. Event Type Naming (Binding)

### 4.1 Core-Aligned Types

If an event_type exists in `config/core_event_requirements.json`, use the exact canonical name (e.g., `TERMS_SUBMITTED`, `SHIPMENT_DISPATCHED`, `DELIVERY_CONFIRMED`). Do not alias, rename, or prefix these.

### 4.2 Pilot-Internal Types

Types not in the core catalog must use the prefix `pilot:` followed by domain and name:

- `pilot:delivery:check_in`
- `pilot:payment:note_added`
- `pilot:exception:manual_override`

Non-prefixed unknown types are rejected by the validator (schema_ok=0 if they somehow persist).

### 4.3 Unknown Type Policy

Events with types not found in the core catalog:

- May persist (pilot-internal events are legitimate)
- MUST have `schema_ok=0` (not core-recognized)
- MUST use the `pilot:` prefix

---

## 5. Evidence Schema (Binding for Day 2 API)

### 5.1 Upload Contract

Pilot API evidence upload accepts:

- `file` (required) — the evidence document
- `entity_type` (required) — what entity this evidence relates to
- `entity_id` (required) — which entity
- `evidence_kind` (required) — string referencing catalog or free text (not a frozen enum)
- `idempotency_key` (required) — per §3
- `note` (optional) — operator annotation

Response: the standard receipt (§2.2) with `event_type` being the appropriate PREP_EVIDENCE type, plus:

```json
{
  "evidence_ref": {
    "evidence_id": "string",
    "storage_ref": "string",
    "filename": "string",
    "content_hash": "string",
    "size_bytes": 0
  }
}
```

### 5.2 Retrieval (Pilot-Only, Minimal)

If Workbench needs to open evidence:

- `GET /api/v1/evidence/{evidence_id}/link` → returns a short-lived URL
- No listing, no search, no deletion endpoints in Day 2 scope

### 5.3 Storage Semantics

Evidence files are stored on the local filesystem (pilot is single-machine). The `storage_ref` is an opaque path or identifier. The file's `content_hash` is computed at upload time and recorded in the PREP_EVIDENCE event. State is never inferred from file existence.

---

## 6. API Surface (Binding for Day 2)

### 6.1 Endpoints

The Merchant Pilot API exposes exactly these endpoints:

```
GET  /api/v1/health              — 200 OK + version string (static, dumb)
GET  /api/v1/work-items          — list work items (projected from entities)
GET  /api/v1/work-items/{id}     — single work item detail
POST /api/v1/actions/apply       — apply a state transition (TRANSITION)
POST /api/v1/evidence            — upload evidence (PREP_EVIDENCE)
POST /api/v1/notes               — add a note (PREP_NOTE)
GET  /api/v1/events/{entity_id}  — event trace for an entity
GET  /api/v1/evidence/{id}/link  — short-lived evidence URL (pilot-only)
```

No export, proof-lite, or reconciliation endpoints. Those are operator CLI tools, not API surfaces.

### 6.2 Auth

Single API key via `X-API-Key` header, stored in `.env`. No OAuth/JWT for pilot.

### 6.3 Governance Fields

Every API response MUST include:

- `schema_version`
- `core_requirements_ref`
- `core_event_requirements_hash`

`pilot_contract_ref` is recommended in `/health` response and OpenAPI metadata. Adding it to every response and event_log is a planned extension, not a Day 2 requirement.

### 6.4 WorkItem Projection

WorkItem shape is defined by the API implementation, not this contract. The API projects trades, deliveries, exceptions, and evidence into WorkItem objects for the workbench.

Two governance constraints on all WorkItem responses:

- `data_source` field MUST be present (`"MOCK"` | `"PILOT"` | `"CORE"`)
- The canonical trio (`schema_version`, `core_requirements_ref`, `core_event_requirements_hash`) MUST be present

Full WorkItem field specification is deferred to the API design document.

---

## 7. Ledger Epoch Boundary

Phase 1C merge (SHA 8090cf95, 2026-03-04) is the ledger epoch boundary. Events created before this point are pre-ledger artifacts:

- They have no content hashes, no schema validation, no idempotency protection
- Export correctly reports them as non-replayable
- Do not attempt to retrofit them

Replay readiness is measured from the epoch boundary forward. The export command SHOULD report `replay_readiness_since_epoch` in addition to global readiness.

---

## 8. Conformance Tests (Must Exist Before Merge)

### 8.1 API Conformance

For each mutating endpoint:

- Response includes `schema_version`, `core_requirements_ref`, `core_event_requirements_hash`
- Receipt matches §2.2 shape
- Idempotency behavior matches §3.2

### 8.2 No Settlement Fields

Automated check asserting none of the §1.2 forbidden fields appear in:

- WorkItem responses
- Receipts
- Any API response schema

### 8.3 Evidence Behavior

- Uploading same file + same idempotency_key → dedup receipt
- Changing payload with same idempotency_key → conflict
- Upload emits a PREP_EVIDENCE event

### 8.4 Dedup Zero-Write

- Same idempotency_key + same content_hash → no truth table writes, no timestamp changes
- Tested via the existing 5 dedup timestamp stability tests (PR #11, SHA f8e94e98)

---

## Not In This Contract

- WorkItem full field specification (deferred to API design)
- Settlement trigger engine design (deferred to dedicated Phase)
- Batch event ingestion (not until performance requires it)
- Core replay protocol (Phase 3)
- `pilot_contract_ref` as a required field on every event (planned extension)
