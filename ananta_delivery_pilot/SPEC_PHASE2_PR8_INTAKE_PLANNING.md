# SPEC_PHASE2_PR8_INTAKE_PLANNING

Status: Step 1 specification lock only (no implementation in this step).

## Objective

Implement PR8 as an intake + planning copilot:
- reduce manual form burden on `/v2/intake`,
- auto-generate delivery plans from policy defaults and LPO validity,
- keep humans focused on high-value decisions only.

## Hard Invariants (must remain unchanged)

- Legacy commands unchanged: `python3 run.py generate|validate|serve`.
- Lane invariants A/B unchanged.
- Pack semantics unchanged: exactly 4 docs in order (WB, WT, COA, Goods Invoice); receipt only on `mark-paid`.
- KG integer canonical storage; MT display only.
- Deterministic `--as-of` behavior.
- UTC canon for persisted timestamps and KPI/SLA calculations.
- Evidence immutability + hash/audit trail.
- No broad schema redesign; additive changes only if blocked.

## PR8 Scope

### 1) Intake Copilot (critical-first)

Primary route:
- `/v2/intake`

Primary-path behavior:
- show critical fields first; advanced fields collapsed by default,
- parser-assisted prefill when LPO file/content is provided,
- show extracted value vs registry value vs final value (diff-aware review),
- confidence badge per field and reason path.

Critical blocker field contract (must be explicit):
- `lpo_no`
- `buyer_id`
- `vendor_of_record_id`
- `product_code`
- `expected_qty_kg`
- `unit_price` (+ deterministic `unit_price_basis`)

Severity rule:
- missing/invalid critical field => `BLOCKER` exception,
- non-critical ambiguity => `REVIEW` exception,
- high-confidence values auto-apply.

Decision trace:
- persist auto-applied and user-corrected values with source + confidence + reason code,
- no silent mutation of registry records.

Parser idempotency:
- dedupe key: `file_sha256 + parser_version`,
- replaying same file/version must not create duplicate exception cases.

### 2) Planning Copilot (policy + validity aware)

Entry:
- triggered after intake confirm (or when opening contract plan page from intake path),
- common-case should not require manual lot-row creation.

Planning behavior:
- lot split from delivery policy (`config/delivery_policies.json`),
- canonical math in KG integers only,
- deterministic remainder assignment to last lot.

Common-case reference:
- `150000 kg` with `default_lot_mt=30` (`30000 kg`) -> exactly `5` planned lots.

Date allocation behavior:
- respects `cadence` (`daily|manual`),
- respects `max_lots_per_day`,
- respects validity window (`lpo_valid_from`/`lpo_valid_to`).

Deterministic blocker:
- if required lots cannot fit validity window under `max_lots_per_day`, block planning with explicit reason code; no silent truncation.

Per-delivery adjustments:
- allow quantity/date adjustments before final plan approval,
- enforce tolerance hierarchy consistently (contract > buyer > global default),
- hard block above allowed tolerance unless explicit override path is used.

### 3) Intake-to-Plan UX contract

Primary flow:
1. Upload/enter LPO.
2. Review critical-field diff cards with confidence.
3. Confirm intake.
4. Review auto-plan preview grid.
5. Approve plan.

Primary-path guardrail:
- no raw state-machine edit controls on intake primary path,
- raw/manual controls remain under `Advanced` only.

### 4) Memory loop and telemetry (PR8 minimum)

Required telemetry/events:
- intake parse started/completed,
- intake confirm,
- plan generated,
- plan blocked (with reason),
- plan approved,
- plan override applied.

Memory loop (additive, minimal):
- store correction outcomes sufficient to improve future prefill for same buyer/program pattern.
- prefer existing decision/event tables; add base tables only if strictly blocked.

## Data/API Contracts (PR8)

No destructive schema changes.

Allowed additive changes only if needed:
- helper view(s) for intake/planning KPI aggregation,
- optional lightweight correction-memory table if existing structures cannot support deterministic replay.

Must preserve:
- deterministic idempotency keys for intake parse and plan generation actions,
- machine-readable reason codes for all blocked planning outcomes.

## KPI and Stop/Go Gate (PR8)

Gate metrics from `PHASE2_GATES.md`:
- `median_manual_fields_per_intake < 6`
- `autoplan_zero_edit_common_case = true`

Evaluation constraints:
- explicit `as_of_date` required,
- `lookback_window_days = 30` default unless override,
- `benchmark_version` must match for cross-PR comparisons.

Stop/Go:
- fail gate if either metric fails,
- failed gate blocks promotion unless Product + Ops + Engineering waiver is logged with expiry and fallback plan.

## Tests (required for PR8)

### Service/domain
- parser idempotency (`file_sha256 + parser_version`) prevents duplicate exceptions,
- critical-field missing/low-confidence creates deterministic blocker,
- confidence auto-apply vs review routing is deterministic,
- common-case lot split yields `150000kg -> 5 x 30000kg`,
- validity window overflow creates blocker with reason code,
- per-lot edits enforce tolerance hierarchy and deterministic rejection above threshold.

### UI/integration
- `/v2/intake` primary path renders critical fields first with advanced collapsed,
- parse diff view shows extracted vs registry vs final values,
- confirm intake transitions directly to plan preview,
- plan preview supports editable lot date/qty and approve action,
- blocker states route to exceptions with scoped context.

### Regression
- `./scripts/test_default.sh` passes.
- `python3 -m unittest -v tests.test_phase2_ui` passes in host-capable environment.
- host smoke remains green:
  - `./scripts/host_ui_smoke.sh 8865`
- legacy validate/generate still pass.

## Acceptance Criteria

1. Standard intake can be completed with median manual fields `< 6` on benchmark fixtures.
2. Common-case `150000kg @ 30000kg lot` auto-plans with zero manual lot edits.
3. Planning fails deterministically (with reason code) when validity/capacity constraints make schedule impossible.
4. Intake and planning decisions are traceable with confidence + source + reason.
5. Legacy and Phase invariants remain unchanged.

## Proof Bundle (PR8)

Write under:
- `.state/phase2-proof/pr8/<timestamp>/`

Required artifacts:
- intake parser diff snapshots (critical fields),
- intake decision trace evidence,
- auto-plan output for common-case fixture,
- blocked validity-window scenario evidence,
- KPI output for `median_manual_fields_per_intake` and `autoplan_zero_edit_common_case`,
- regression logs (`test_default`, `phase2_ui`, legacy validate/generate, host smoke).

## Out of Scope (PR8)

- PR9 transport/doc completion copilot.
- PR10 settlement copilot + KPI strip.
