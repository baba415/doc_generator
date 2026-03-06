# SPEC_PHASE1

## Scope
- Implement Phase 1 only: persistent ledger, state machine, CLI workflow, DREP exports, and tests.
- Keep legacy commands unchanged: `generate`, `validate`, `serve`.
- Do not implement Phase 2/3 UI or OCR in this phase.

## Non-Negotiable Invariants
1. Lane symmetry:
   - Lane A: `vendor_of_record_id = guildgate`
   - Lane B: `vendor_of_record_id = ananta_flows`
   - Between A/B only vendor-of-record identity and numbering change.
2. Operator invariant:
   - `operator_id` defaults to `guildgate` in all Phase 1 flows.
3. Pack semantics:
   - `generate-pack` produces exactly 4 PDFs in order:
     1. Waybill
     2. Weighing Ticket
     3. COA
     4. Goods Invoice
   - No receipt in pack generation.
   - No index/allocation/murabaha docs in pack generation.
4. Receipt semantics:
   - Receipt is generated only by `mark-paid`.
5. Evidence immutability:
   - Uploaded originals are immutable evidence artifacts and never overwritten.
6. Numbering:
   - Per vendor-of-record sequence isolation.
   - Internal IDs are immutable ULIDs stored separately from human numbers.
7. Legacy safety:
   - New flow writes state only to `.state/`.
   - New flow writes generated artifacts only to `output_v2/`.
   - Legacy templates and legacy numbering state are untouched.

## Additional Locked Clarifications
1. `record-coa` keying:
   - Command may accept `delivery_id`, but persisted COA identity is resolved by
     `(buyer_group, product_code, batch_id, run_id, profile_version)`.
   - Multiple deliveries may link to the same COA record.
2. `content_sha256` normalization:
   - Hash input uses canonical JSON with sorted keys, UTF-8, no extra whitespace.
   - Numerics are decimal-normalized to fixed scale (`0.00` for money/qty/rates).
   - Timestamps use UTC format: `YYYY-MM-DDTHH:MM:SSZ`.
3. Legacy `serve` smoke contract:
   - Non-interactive smoke: start server, assert `GET /` returns `200`, terminate cleanly.

## Data Model (SQLite)
- DB path: `.state/drep.sqlite`
- DB guardrails:
  - `PRAGMA foreign_keys=ON`
  - transactional writes per command
  - rollback on failure
- Schema versioning:
  - `schema_meta(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)`
  - idempotent init command

### Core tables
- `parties`
- `parties_snapshot`
- `contracts`
- `contract_line_items`
- `deliveries`
- `sales_transactions`
- `sales_lines`
- `documents`
- `document_sales_links`
- `payments`
- `payment_allocations`
- `tax_withholding_events`
- `evidence_originals`
- `coa_results`
- `numbering_sequences`
- `command_idempotency`

### Required uniqueness and idempotency
- `documents.doc_id` unique
- `documents.doc_number` unique per `(vendor_of_record_id, doc_type, doc_number)`
- `sales_transactions.invoice_no` unique per `vendor_of_record_id`
- `payments.receipt_no` unique
- `numbering_sequences` unique on `(vendor_of_record_id, doc_type, year)`
- `command_idempotency` unique on `(command_name, idempotency_key)`

### Delivery and sales linking
- Each delivery creates exactly one sales transaction in Phase 1.
- `document_sales_links` must map every generated doc to:
  - `sales_transaction_id`
  - `sales_line_id`
  - `delivery_id`

## State Machines

### Delivery state transitions
- `PLANNED -> DISPATCHED -> DELIVERED -> INVOICED -> PAID`
- Invalid transitions are blocked.

### Contract rollup status
- `OPEN | PARTIAL | COMPLETE | CANCELLED`
- Derived from line fulfillment and payment progress.

## Validation Gates (blocking)
- Missing vendor-of-record TIN
- Missing `run_id`
- Missing `batch_id`
- Missing required COA result rows for active profile
- Pack generation attempted before delivery is `DELIVERED`
- Over-delivery above tolerance (default `1.0%`) unless force override with reason
- Placeholder TIN blocked unless `--allow-placeholder-tin` is explicitly set

## COA Profiles
- Source: `config/coa_profiles.json`
- Key: `(buyer_group, product_code)` with profile version.
- `record-coa` stores:
  - `profile_key`
  - `profile_version`
  - full required parameter set
- Required rows must be complete before `generate-pack`.

## Tax and Outstanding Semantics
- `gross_amount` is contractual invoice gross before deductions.
- `expected_wht` is informational only.
- `outstanding_balance` is reduced only by:
  - cash allocations
  - certified withholding events
- Certified withholding requires evidence fields:
  - `withholder_party_id`
  - `withholding_type`
  - `certificate_ref` or `evidence_path` + `evidence_hash`
  - `certified_at`

## DREP Export Views and CLI
- Views:
  - `drep_contracts`
  - `drep_procurement`
  - `drep_sales` (invoice grain)
  - `drep_sales_lines` (line grain)
  - `drep_outstanding_payments`
- `export-drep --as-of YYYY-MM-DD` is deterministic:
  - no `TODAY()`/volatile date functions
  - UTC date boundary
  - null due date treated as `CURRENT`

### Required `drep_sales_lines` doc linkage columns
- waybill: `waybill_doc_id`, `waybill_no`, `waybill_pdf_sha256`
- weighing ticket: `weighing_doc_id`, `weighing_no`, `weighing_pdf_sha256`
- COA: `coa_doc_id`, `coa_no`, `coa_pdf_sha256`
- invoice: `invoice_doc_id`, `invoice_no`, `invoice_pdf_sha256`

## CLI Commands (Phase 1)
- `init-db`
- `create-contract --input <json>`
- `add-delivery --input <json>`
- `mark-dispatched --delivery-id <id>`
- `mark-delivered --delivery-id <id>`
- `record-coa --input <json>`
- `generate-pack --delivery-id <id> [--skip-pdf]`
- `mark-paid --input <json>`
- `export-drep --as-of YYYY-MM-DD --out-dir <path>`

### `--skip-pdf` semantics
- Do not render PDFs.
- Persist document rows and manifest entries.
- Set `pdf_sha256 = NULL`.
- Set deterministic `content_sha256` using canonical payload normalization.

### `mark-paid` idempotency
- Idempotency key source:
  - explicit `idempotency_key` if provided
  - otherwise `external_reference`
- Replay returns previously created payment/receipt metadata without duplicate inserts.

### `generate-pack` idempotency
- Idempotency key: `delivery_id` for immutable first-generation in Phase 1.
- Replay returns existing generated pack metadata.

## Party Snapshot Contract
- `generate-pack` creates one `parties_snapshot`.
- Snapshot is linked to `sales_transactions` and all generated docs.
- Snapshot preserves buyer/vendor/operator identity at issuance time.

## Legacy Compatibility Contract
1. Before/after smoke runs must pass:
   - `python3 run.py generate --transaction data/sample_transaction_contract_processing.json`
   - `python3 run.py validate --transaction data/sample_transaction_contract_processing.json`
2. Legacy `serve` smoke must pass:
   - Start server process
   - HTTP `GET /` returns `200`
   - Process terminates cleanly
3. Legacy output path remains usable (`output/` untouched by new flow).

## Deliverables
1. Phase 1 implementation with modular structure:
   - `core/`
   - `domain/`
   - `adapters/`
   - `apps/`
2. Updated `README.md`
3. End-to-end sample scenario files
4. `ANANTA_MERGE_NOTES.md`
5. `PHASE2_PHASE3_PLAN.md`
