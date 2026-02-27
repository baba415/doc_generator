# SPEC_PHASE1_6

## Scope
Phase 1.6 extends Phase 1/1.5 with LPO validity lifecycle, lot-based delivery planning, planned-delivery materialization, and simplified LPO-first UI orchestration.

This phase is a delta on existing foundations. Legacy commands and outputs remain unchanged.

## Invariants
- Legacy commands remain unchanged: `generate`, `validate`, `serve`.
- New Phase flow stays in isolated paths: `.state/` and `output_v2/`.
- Pack semantics unchanged: exactly 4 pack docs (`WAYBILL`, `WEIGHING_TICKET`, `COA`, `INVOICE`).
- Receipt is generated only via `mark-paid`.
- Lane symmetry remains:
  - Lane A -> `vendor_of_record_id=guildgate`
  - Lane B -> `vendor_of_record_id=ananta_flows`
  - only vendor-of-record identity/numbering changes between A/B.
- Operator defaults to `guildgate` in Phase 1.6 flows.
- Evidence originals are immutable and never overwritten.

## Quantity Canon (locked)
- Canonical storage/calculation unit: **KG** (`INTEGER`) for all operational quantities.
- MT is display/business unit in UI and PDFs.
- Conversion rule: `1 MT = 1000 KG`.
- Quantization: `0.001 MT` precision, deterministic conversion.
- Lot split remainder is assigned to the **last lot**.

## LPO Lifecycle
Contracts include:
- `lpo_valid_from`, `lpo_valid_to`
- `lpo_state`: `ACTIVE|CANCELLED|EXPIRED|CLOSED`
- `cancelled_at`, `expired_at`, `closed_at`, `close_reason`
- `master_contract_id` (nullable recurrence baseline)

State precedence:
`CANCELLED > CLOSED > EXPIRED > ACTIVE`

Backfill policy:
- `lpo_state='ACTIVE'`
- `lpo_valid_from=issue_date` if null
- `lpo_valid_to`:
  - keep explicit value if present
  - else null unless `validity_backfill_policy=fallback_due_date`.

## Planning Model
`planned_deliveries` stores lot plan rows with KG canonical quantity:
- `planned_qty_kg`, `lot_size_kg`
- `planned_date`
- `status`: `PLANNED|SCHEDULED|DISPATCHED|DELIVERED|INVOICED|PAID|SKIPPED|CANCELLED`
- optional `delivery_id`, `run_id`, `batch_id`.

Constraints:
- `UNIQUE(contract_line_id, sequence_no)`
- `UNIQUE(delivery_id)` when set
- `CHECK(planned_qty_kg > 0)`

Planning idempotency key:
`contract_id + start_date + cadence + max_lots_per_day + policy_version`

Materialization idempotency key:
`planned_delivery_id`

## Tolerance Policy Hierarchy
Over-delivery tolerance resolves as:
1. contract override
2. buyer override
3. global default (`5.0%`)

Applied consistently in:
- plan generation
- delivery materialization/addition
- contract status rollup.

## Auto Materialization Guard
Materialization requires:
- contract `lpo_state=ACTIVE`
- planned row in `PLANNED|SCHEDULED`
- required readiness evidence (or explicit override in automation stage).

## COA Keying
COA records are batch keyed by:
`(buyer_group, product_code, batch_id, run_id, profile_version)`

`record-coa` may accept `delivery_id`, but persists using the batch key and links deliveries via `delivery_coa_links`.

## Hash Canon
`content_sha256` uses canonical JSON:
- sorted keys
- compact separators
- normalized decimals
- UTC timestamps (`...Z`) normalization.

## New/Updated Commands
- `plan-deliveries`
- `materialize-delivery`
- `cancel-contract`
- `close-contract`
- `refresh-contract-state`

## DREP Extensions
- Existing `drep_*` views remain compatible.
- Added `drep_delivery_plan_status` with deterministic `as_of_date` from `report_context`.

