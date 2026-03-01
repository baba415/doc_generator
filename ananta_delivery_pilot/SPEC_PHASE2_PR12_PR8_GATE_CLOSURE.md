# SPEC_PHASE2_PR12_PR8_GATE_CLOSURE

Status: Step 1 specification lock only (no implementation in this step).

## Objective

Implement PR12 as a focused **PR8 gate closure** release:
- remove `autoplan_zero_edit_common_case_failed` for benchmarked common-case intake/planning,
- achieve PR8 gate PASS without waiver under deterministic benchmark execution,
- keep all existing phase invariants and legacy behavior unchanged.

PR12 is narrow hardening for intake+planning reliability only. It is not PR13+ autonomy expansion.

## Hard Invariants (must remain unchanged)

- Legacy commands unchanged: `python3 run.py generate|validate|serve`.
- Lane invariants A/B unchanged.
- Pack semantics unchanged: exactly 4 docs in order (WB, WT, COA, Goods Invoice); receipt only on `mark-paid`.
- KG integer canonical storage; MT display only.
- Deterministic `--as-of` behavior.
- UTC canon for persisted timestamps and KPI/SLA calculations.
- Evidence immutability + hash/audit trail.
- Additive-only evolution; no destructive migrations.

## PR12 Scope

### 1) Common-case autoplan fast-path (deterministic)

Add deterministic fast-path for common-case intake confirmations where all critical fields are satisfied and confidence is safe.

Common-case eligibility (all required):
- `buyer_id`, `vendor_of_record_id`, `product_code`, `expected_qty_kg`, `unit_price`, `lpo_no` present.
- No open BLOCKER exceptions for intake/plan stage.
- Delivery policy exists for product (`config/delivery_policies.json`).
- Quantity can be planned within validity and policy constraints.

Fast-path behavior:
- plan is generated and marked `zero_edit=true` when user does not change lot rows,
- lot split remains deterministic (KG integers, remainder to last lot),
- date allocation remains deterministic from existing policy + cadence inputs.

### 2) Confidence and correction-memory stabilization

Improve parser-assisted intake consistency for repeated buyer/product patterns:
- apply correction memory for previously approved fields before review rendering,
- keep decision trace for each auto-applied/reviewed field,
- do not auto-mutate registry records.

Critical-field severity remains locked:
- BLOCKER fields: `lpo_no`, `buyer_id`, `vendor_of_record_id`, `product_code`, `expected_qty_kg`, `unit_price`.

### 3) PR8 metric closure contract (no interpretation drift)

Keep existing KPI formulas and gates unchanged.

For PR8 gate evaluation:
- `median_manual_fields_per_intake < 6`
- `autoplan_zero_edit_common_case = true`
- explicit `as_of_date`, `lookback_window_days`, `benchmark_version` required.

Deterministic reason codes:
- `pass`
- `insufficient_intake_data`
- `benchmark_version_mismatch`
- `median_manual_fields_threshold_failed`
- `autoplan_zero_edit_common_case_failed`

### 4) Deterministic benchmark fixture update for PR8 closure

Add benchmark fixture version:
- `phase2.pr12.v1`

Requirements:
- fixture set includes common-case intake/planning rows sufficient to evaluate PR8 gate,
- seeded data must make `autoplan_zero_edit_common_case=true` achievable without waiver,
- seeding remains idempotent by `(as_of_date, benchmark_version)` with optional reset,
- fixture metadata persists benchmark version and counts.

### 5) Portfolio visibility (read-only)

No new operational controls.

Portfolio gate-health strip should continue showing:
- PR8 status + reason code,
- benchmark version used,
- waiver state and report link.

No raw edit controls in primary workspace.

## Route / Service / CLI Contracts

### Service
- Extend existing intake/plan services to emit deterministic `zero_edit_common_case` signals.
- Reuse existing PR11 gate report service; no duplicate gate-report pipeline.

### CLI
- Reuse existing benchmark and gate-report commands:
  - `seed-phase2-benchmark`
  - `run-phase2-benchmark`
  - `phase2-gate-report`
- Additive args only if strictly required for PR12 benchmark scenario generation.

## Data Contract

Prefer existing tables and event streams (`automation_runs`, `automation_decisions`, `event_log`, planned delivery tables).

Allowed additive changes only if blocked:
- one helper view/materialized query for deterministic PR8 common-case metric sourcing,
- additive indexes for hot metric queries.

No broad new base-table families.

## Acceptance Tests (required)

### Service/domain
1. Repeated known-format intake for same buyer/product applies correction memory deterministically.
2. Common-case intake confirmation with valid policy yields `zero_edit=true` plan outcome.
3. Common-case 150000kg @ 30000kg policy remains 5 lots with zero user lot edits.
4. Reason-code precedence is deterministic when multiple gate conditions fail.
5. PR8 gate for `phase2.pr12.v1` benchmark returns PASS without waiver.

### UI/integration
6. Intake critical-first flow still hides advanced controls by default.
7. Plan preview remains editable, but zero-edit common-case path requires no lot edits.
8. Portfolio gate-health shows PR8 PASS for `phase2.pr12.v1` benchmark report when criteria met.

### Regression
9. `./scripts/test_default.sh` passes.
10. `python3 -m unittest -v tests.test_phase2_ui` passes in host-capable environment.
11. `python3 run.py validate --transaction data/sample_transaction_contract_processing.json` passes.
12. `python3 run.py generate --transaction data/sample_transaction_contract_processing.json` passes.
13. `./scripts/host_ui_smoke.sh 8865` passes in host-capable environment.

## Stop/Go Gate (PR12)

PR12 passes only if all are true:
- PR8 gate is PASS without waiver for `benchmark_version=phase2.pr12.v1`.
- PR9 and PR10 gates do not regress versus PR11 baseline under same as_of/lookback.
- Determinism holds for repeated benchmark runs with same inputs.

If PR8 still fails, release remains waiver-governed only.

## Proof Bundle Contract

Write under:
- `.state/phase2-proof/pr12/<timestamp>/`

Required artifacts:
- PR12 benchmark seed output,
- PR12 benchmark run output,
- gate report JSON + MD with PR8 PASS without waiver,
- metric snapshot showing PR8 gate inputs and result,
- regression logs (`test_default`, `phase2_ui`, legacy validate/generate, host smoke),
- provenance (`tested_sha.txt`, `git_status_short.txt`).

## Out of Scope (PR12)

- PR13+ policy autonomy expansion, model training, or threshold auto-tuning.
- Settlement algorithm redesign.
- Transport/doc copilot scope beyond PR9 behavior.
- New legacy command semantics.
