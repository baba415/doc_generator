# SPEC_DG1A_RAILS_TRUTH_INTEGRATION

Status: Step 1 spec lock only (no runtime changes in this step)  
Owner: `ananta_delivery_pilot` (Merchant/Ops harness)  
Canonical dependencies:
- Rails/Core: `/Users/macbookairv2/Projects/ananta-mvp`
- Execute (canonical): `/Users/macbookairv2/Projects/ananta-execute`
- Execute contracts:
  - `/Users/macbookairv2/Projects/ananta-execute/execute_v11/contracts/drep_daily_contract.py`
  - `/Users/macbookairv2/Projects/ananta-execute/execute_v11/contracts/proof_pack_export_contract.py`

## 0) Scope lock and invariants

DG-1A introduces a **spec-only integration contract** to align this repo with Rails/Core protocol truth boundaries and Execute contract consumption.

Hard lock for DG-1A:
1. No runtime code changes.
2. No DB schema changes.
3. No CLI behavior changes.
4. Preserve current delivery flow and all existing invariants.
5. Treat `/Users/macbookairv2/Projects/ananta-execute` as canonical Execute source.
6. Do not use `/Users/macbookairv2/excute_card` as shipping source.

---

## 1) Trust-changing action inventory (current state -> DG-1A target)

Legend:
- **Rails evented write target**:
  - `YES` = action is trust-changing and must move to Rails `/api/events` when activated.
  - `NO` = read/report/admin action; remains non-evented in DG-1A.
  - `DERIVED` = orchestrator wrapper; emits/consumes underlying mapped events, no new canonical event.

| Surface | Action (current command/route) | Trust-changing | Rails evented write target | Notes |
|---|---|---:|---:|---|
| CLI | `create-contract` | Yes | YES | Contract/trade initialization intent. |
| CLI | `plan-deliveries` | Yes | YES | Planning state changes; policy pointers required. |
| CLI | `add-delivery` | Yes | YES | Legacy direct delivery create path. |
| CLI | `materialize-delivery` | Yes | YES | Planned -> realized delivery transition. |
| CLI | `mark-dispatched` | Yes | YES | Dispatch milestone transition. |
| CLI | `mark-delivered` | Yes | YES | Delivery milestone transition. |
| CLI | `record-coa` | Yes | YES | Quality adjudication evidence transition. |
| CLI | `generate-pack` | Yes | YES | Trust-critical proof-pack publication event. |
| CLI | `mark-paid` | Yes | YES | Settlement event (receipt emission path). |
| CLI | `cancel-contract` | Yes | YES | LPO state mutation (terminal). |
| CLI | `close-contract` | Yes | YES | LPO state mutation (terminal). |
| CLI | `refresh-contract-state` | Yes | YES | Deterministic state transition application. |
| CLI | `exceptions resolve` | Yes | YES | Legacy exception resolution action. |
| CLI | `decide-case` | Yes | YES | Manual adjudication decision on exception case. |
| CLI | `auto-run` / `run-autonomy` / `auto-resume` | Yes | DERIVED | Wrapper actions over mapped underlying events only. |
| Web | `/v2/intake/confirm` | Yes | YES | Contract creation + plan trigger path. |
| Web | `/v2/contracts/{id}/plan/rebuild|update|approve` | Yes | YES | Planning mutation paths. |
| Web | `/v2/contracts/{id}/execute/materialize-due|materialize-one` | Yes | YES | Delivery materialization transitions. |
| Web | `/v2/contracts/{id}/execute/generate-pack` | Yes | YES | Proof-pack generation transition. |
| Web | `/v2/contracts/{id}/settle/suggest` | No (proposal) | NO | Suggestion output only; no trust write until apply. |
| Web | `/v2/contracts/{id}/settle/apply-suggestion` | Yes | YES | Settlement apply or exception route transition. |
| Web | `/v2/contracts/{id}/settle/mark-paid` | Yes | YES | Direct settlement mutation. |
| Web | `/v2/exceptions/decide` and `/v2/exceptions/resolve` | Yes | YES | Human decision transitions. |
| Web | `/v2/contracts/{id}/run-recommended`, `/v2/run-all-eligible/execute` | Yes | DERIVED | Wrapper/orchestration actions only. |
| CLI/Web | `export-drep`, `phase2-*report/status/metrics`, `/v2/contracts/{id}/settle/export-drep` | No | NO | Read/export projections only. |
| CLI | `init-db`, `serve-v2` | No | NO | Local admin/runtime only, outside Rails truth path. |

---

## 2) Mapping table: action -> canonical event_type -> payload contract -> idempotency key

All event types below are **proposed DG-1A canonical names** to be allowlisted in Rails `eventCatalog.ts` during implementation (R-1 gate).

| Action | Canonical event_type | Required payload fields (minimum) | Idempotency key contract |
|---|---|---|---|
| create-contract / intake-confirm | `DREP_CONTRACT_CREATED` | `contract_id`, `lpo_no`, `buyer_id`, `vendor_of_record_id`, `issue_date`, `lpo_valid_from`, `lpo_valid_to`, `expected_total_qty_kg`, `currency`, `unit_price_basis`, `policy_pointers` | `drep:contract:create:{vendor_of_record_id}:{lpo_no}` |
| plan-deliveries / plan rebuild | `DREP_DELIVERY_PLAN_BUILT` | `contract_id`, `as_of_date`, `start_date`, `cadence`, `max_lots_per_day`, `policy_version`, `planned_rows[]` (`planned_delivery_id`,`sequence_no`,`planned_qty_kg`,`planned_date`) | `drep:plan:build:{contract_id}:{start_date}:{cadence}:{max_lots_per_day}:{policy_version}` |
| plan update | `DREP_DELIVERY_PLAN_UPDATED` | `contract_id`, `planned_delivery_id`, `sequence_no`, `planned_qty_kg`, `planned_date`, `reason` | `drep:plan:update:{planned_delivery_id}:{sha256(update_payload)}` |
| plan approve | `DREP_DELIVERY_PLAN_APPROVED` | `contract_id`, `approved_at_utc`, `approved_by`, `plan_hash` | `drep:plan:approve:{contract_id}:{plan_hash}` |
| add-delivery / materialize-delivery | `DREP_DELIVERY_MATERIALIZED` | `contract_id`, `planned_delivery_id`, `delivery_id`, `delivery_ref`, `run_id`, `batch_id`, `delivery_date`, `delivered_qty_kg`, `unit_price`, `unit_price_basis` | `drep:delivery:materialize:{planned_delivery_id}` |
| mark-dispatched | `DREP_DELIVERY_DISPATCHED` | `contract_id`, `delivery_id`, `dispatched_at_utc`, `transport_snapshot_ref?` | `drep:delivery:dispatch:{delivery_id}` |
| mark-delivered | `DREP_DELIVERY_DELIVERED` | `contract_id`, `delivery_id`, `delivered_at_utc`, `delivered_qty_kg` | `drep:delivery:deliver:{delivery_id}` |
| record-coa | `DREP_COA_RECORDED` | `contract_id`, `delivery_id`, `coa_no`, `product_code`, `batch_id`, `run_id`, `profile_version`, `results[]` | `drep:coa:record:{buyer_group}:{product_code}:{batch_id}:{run_id}:{profile_version}` |
| generate-pack | `DREP_PROOF_PACK_GENERATED` | `contract_id`, `delivery_id`, `invoice_no`, `pack_status`, `manifest_contract_version`, `manifest_path`, `manifest_sha256`, `pdf_path`, `pdf_sha256`, `doc_order` | `drep:pack:generate:{delivery_id}:{manifest_sha256}` |
| settle apply-suggestion | `DREP_SETTLEMENT_SUGGESTION_APPLIED` | `contract_id`, `suggestion_set_id`, `suggestion_id`, `sales_transaction_id`, `allocated_amount`, `decision`, `reason`, `as_of_date` | `drep:settlement:suggestion:{suggestion_set_id}:{suggestion_id}:{decision}` |
| mark-paid / settle mark-paid | `DREP_PAYMENT_RECORDED` | `contract_id`, `receipt_no`, `payment_date`, `payment_method`, `external_reference`, `amount_received`, `allocations[]` | `drep:payment:record:{external_reference}` |
| cancel-contract | `DREP_CONTRACT_CANCELLED` | `contract_id`, `cancelled_at_utc`, `reason` | `drep:contract:cancel:{contract_id}` |
| close-contract | `DREP_CONTRACT_CLOSED` | `contract_id`, `closed_at_utc`, `reason` | `drep:contract:close:{contract_id}` |
| refresh-contract-state | `DREP_CONTRACT_STATE_REFRESHED` | `contract_id`, `as_of_date`, `previous_lpo_state`, `next_lpo_state`, `reason_code` | `drep:contract:refresh:{contract_id}:{as_of_date}` |
| decide-case | `DREP_EXCEPTION_DECIDED` | `exception_case_id`, `case_type`, `contract_id`, `decision`, `reason`, `resume_requested`, `dry_run_resume` | `drep:exception:decide:{exception_case_id}:{decision}:{sha256(reason)}` |
| exceptions resolve (legacy) | `DREP_EXCEPTION_RESOLVED` | `exception_id`, `run_id`, `resolution_value`, `note` | `drep:exception:resolve:{exception_id}:{sha256(resolution_value+note)}` |
| run-recommended / run-all execute / auto-run / run-autonomy | `DERIVED` | Must emit only mapped underlying canonical events; no separate trust event | Wrapper idempotency key only for orchestration trace, not trust mutation |

Idempotency invariant for implementation:
- Rails trust-path dedupe remains `(trade_id, idempotency_key)` per `PROTOCOL.md`.
- DG-1A key formulas above are required deterministic producers.

---

## 3) Execute consumer contracts (strict version + fail-closed)

### 3.1 `execute_drep_daily_v1` (daily handoff)

Canonical contract:
- Version constant: `execute_drep_daily_v1`
- Validator: `/Users/macbookairv2/Projects/ananta-execute/execute_v11/contracts/drep_daily_contract.py`

DG-1A consumer rules:
1. Must validate payload with Execute validator semantics.
2. Must reject if `contract_version != execute_drep_daily_v1`.
3. Must reject unsupported/missing keys (no permissive parsing).
4. Must preserve deterministic reason ordering (`go_no_go_reasons` order).
5. Must fail closed when validator throws.

Fail-closed reason codes:
- `EXEC_DREP_DAILY_VERSION_MISMATCH`
- `EXEC_DREP_DAILY_SCHEMA_INVALID`
- `EXEC_DREP_DAILY_REASON_ORDER_INVALID`
- `EXEC_DREP_DAILY_GO_NO_GO_BLOCKED`
- `EXEC_DREP_DAILY_UNREADABLE_PAYLOAD`

### 3.2 `execute_proof_export_v1` (proof-pack export handshake)

Canonical contract:
- Version constant: `execute_proof_export_v1`
- Validator: `/Users/macbookairv2/Projects/ananta-execute/execute_v11/contracts/proof_pack_export_contract.py`

DG-1A consumer rules:
1. Must validate payload with Execute validator semantics.
2. Must reject if `export_contract_version != execute_proof_export_v1`.
3. Must reject if `manifest_contract_version` mismatches expected Execute manifest contract.
4. Must reject nullable PDF fields violations (`DRAFT` cannot include PDF pointers).
5. Must reject hash/path shape violations.

Fail-closed reason codes:
- `EXEC_PROOF_EXPORT_VERSION_MISMATCH`
- `EXEC_PROOF_EXPORT_SCHEMA_INVALID`
- `EXEC_PROOF_EXPORT_MANIFEST_VERSION_MISMATCH`
- `EXEC_PROOF_EXPORT_HASH_INVALID`
- `EXEC_PROOF_EXPORT_DRAFT_PDF_INVALID`
- `EXEC_PROOF_EXPORT_UNREADABLE_PAYLOAD`

No fallback parser is allowed for either contract in DG-1A.

---

## 4) Feature-flag rollout contract

DG-1A uses three explicit flags:

1. `shadow_emit_only`
   - Default: `true` in first rollout phase.
   - Behavior: emit planned Rails event envelopes and validation logs only; no trust-path source switch.
   - Local flow remains authoritative.

2. `rails_write_enabled`
   - Default: `false`.
   - Behavior when enabled: trust-changing actions write through Rails `/api/events` (single canonical write ingress).
   - Must remain `false` until all R-1 gates pass.

3. `execute_contract_consume_enabled`
   - Default: `false`.
   - Behavior when enabled: consume Execute producer payloads strictly via `execute_drep_daily_v1` and `execute_proof_export_v1` contract validation.
   - Any contract failure blocks consumption with fail-closed reason code.

Flag precedence:
- If `rails_write_enabled=false`, local behavior remains unchanged regardless of other flags.
- `shadow_emit_only` never mutates truth state.
- `execute_contract_consume_enabled` is independent of write path but still fail-closed.

---

## 5) Rails dependency gates (R-1 required), and R-2 notes

## 5.1 R-1 (required before activation of `rails_write_enabled`)

All must pass:
1. Rails `/api/events` allowlists all DG-1A canonical event types (or approved aliases) and enforces schema fail-closed.
2. Rails idempotency contract `(trade_id, idempotency_key)` is active with dedupe replay semantics.
3. Rails exposes stable `decision_meta` + reason codes (no ad-hoc strings).
4. Rails capabilities handshake includes `protocol_version`, `protocol_sha`, `event_registry_hash`.
5. Rails apply-status surface exists for replay/conflict resolution.
6. Protocol pin check policy is wired (strict in CI, fail-closed for write on mismatch).
7. `AFR1_EXECUTE_INTEGRATION_PRIMARY_PATH` roadmap dependency is marked shipped in Rails roadmap.

If any R-1 gate fails: `rails_write_enabled` remains `false` (NO-GO).

## 5.2 R-2 (post-activation hardening notes; not required to start)

R-2 targets:
1. Dual-write reconciliation dashboard (local vs Rails digest comparison).
2. Automated drift alarms on event outcome mismatch.
3. Formalized migration/deprecation plan for legacy local trust writes.
4. Proof bundle uplift to include Rails apply-status correlation receipts.

R-2 does not relax R-1 fail-closed requirements.

---

## 6) GO/NO-GO activation checklist

Activation target: enabling `rails_write_enabled=true` for controlled corridor.

GO only if all are true:
1. R-1 gates all pass.
2. `shadow_emit_only` dry-run emits deterministic event envelopes for all trust-changing actions in inventory.
3. Replay tests show zero duplicate side effects for repeated idempotency keys.
4. Execute contract consumers pass strict validation for both contract families.
5. Legacy behavior parity proof confirms no workflow disruption in approved corridor.
6. Rollback toggle path tested (`rails_write_enabled -> false`) without data corruption.

NO-GO if any are true:
1. Missing Rails event allowlist entries.
2. Protocol pin mismatch unresolved.
3. Any fail-open behavior observed for Execute contracts.
4. Non-deterministic idempotency key generation detected.

---

## 7) DG-1A proof bundle contract

Bundle root:
- `.state/phase2-proof/dg1a/<timestamp>/`

Required artifacts:
1. `dg1a_action_inventory.json` (frozen copy of Section 1 inventory rendered for run).
2. `dg1a_action_event_mapping.json` (Section 2 mapping materialized).
3. `dg1a_shadow_emit.log` (event envelope emission receipts, no writes).
4. `dg1a_execute_drep_daily_validation.json` (strict contract validation result + reason code on failure).
5. `dg1a_execute_proof_export_validation.json` (strict contract validation result + reason code on failure).
6. `dg1a_flag_snapshot.json` (`shadow_emit_only`, `rails_write_enabled`, `execute_contract_consume_enabled`).
7. `dg1a_go_no_go_checklist.json` (boolean per checklist item + final decision).
8. `legacy_validate.log` and `legacy_generate.log` (non-regression evidence).
9. `tested_sha.txt` and `git_status_short.txt`.

Proof validation rule:
- Missing required artifact => proof bundle invalid => NO-GO.

---

## 8) Non-goals and no-workflow-disruption clause

Non-goals for DG-1A:
1. No runtime migration of local DB truth to Rails in this step.
2. No redesign of planning/execute/settle workflow.
3. No UI flow changes or operator path changes.
4. No new policy logic, threshold tuning, or auto-remediation.
5. No changes to pack composition, receipt trigger, KG canon, or deterministic `--as-of` semantics.

Explicit no-workflow-disruption clause:
- Until `rails_write_enabled` is activated after R-1 GO, all existing CLI and web workflows continue unchanged, with local behavior preserved exactly.
- DG-1A spec lock must not alter current user-visible execution path.
