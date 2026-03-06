# SPEC_PHASE2_AUTONOMY

## Mission
Implement Phase 2 as an autonomy-first operating system on top of existing Phase 1.6 foundations:
- maximize touchless execution,
- keep humans only on critical exceptions,
- preserve all legacy and Phase invariants.

This spec defines **schema/contracts/tests/perf budgets** and the required **PR1..PR5 order**.

---

## Scope Lock

### Unchanged behavior (hard constraints)
1. Legacy commands remain unchanged:
   - `python3 run.py generate`
   - `python3 run.py validate`
   - `python3 run.py serve`
2. Existing Phase invariants remain unchanged:
   - Lane A/B only in new flow.
   - Pack is exactly 4 docs in order: `WB, WT, COA, GOODS invoice`.
   - Receipt generated only on `mark-paid`.
   - Evidence originals immutable (`sha256`, `captured_at`, `stored_path`).
   - Deterministic reporting via `--as-of`.
   - Canonical quantity storage in KG integer fields; MT for display/docs only.
3. Additive evolution only:
   - no breaking of existing tables/views/CLI behavior,
   - schema extension by migrations only,
   - compatibility shims for old routes/commands.

### Out of scope
- No redesign of legacy renderer contracts.
- No replacement of existing Phase 1.6 data model.
- No OCR “magic” claims; parser-assisted only.

---

## Canonical Runtime Semantics

1. **Policy resolution precedence** (persisted on every intent execution):
   1) contract override  
   2) master contract override  
   3) buyer override  
   4) global default

Phase boundary note:
- PR2 persists `policy_version` from runtime config for deterministic idempotency.
- PR3 introduces active read-path resolution from `policy_sets` / `policy_overrides` with fallback to config defaults.

2. **Gate -> Intent -> Execution chain**:
   - Gates evaluate deterministically with `as_of_date`.
   - Intents are created only when gate bundle passes.
   - Failed gates create exception cases (never silent override).

3. **Idempotency requirements**:
   - every autonomy run, intent execution, and case decision has deterministic idempotency key.
   - replays return prior response (no duplicate side effects).

4. **Event logging**:
   - all gate decisions, intent outcomes, and case actions append to immutable `event_log`.

---

## PR Plan (Sequential)

Each PR must:
- include migrations/tests/docs for its scope only,
- produce proof bundle under `.state/phase2-proof/<pr>/<timestamp>/`,
- pass targeted tests + non-regression tests before proceeding.

### PR1 — Autonomy Data Foundation

#### 1) New tables
- `master_contracts`
- `contract_cycles`
- `policy_sets`
- `policy_overrides`
- `capacity_calendar`
- `gate_evaluations`
- `action_intents`
- `action_executions`
- `exception_cases`
- `human_decisions`
- `decision_features`
- `decision_outcomes`
- `event_log` (append-only)

#### 2) Transport / master-data tables
- `transport_partners`
- `transport_trucks`
- `transport_drivers`
- `truck_driver_assignments` (effective-dated)
- `transport_compliance_docs`
- `transport_aliases`
- `delivery_transport_snapshot`
- `delivery_transport_suggestions`

#### 3) User helper tables
- `users`
- `user_roles`
- `user_preferences`

#### 4) Migration + constraints
- idempotent migration scripts (`CREATE TABLE IF NOT EXISTS` + safe backfills),
- FK constraints enabled (`PRAGMA foreign_keys=ON`),
- uniqueness/consistency checks:
  - natural key uniqueness where appropriate,
  - append-only enforcement for `event_log`,
  - validity window checks for effective-dated records,
  - non-negative numeric checks.

#### 5) Required indexes (minimum)
- by `as_of_date`, `status`, `contract_id`, `delivery_id`, `case_status`, `intent_status`, `created_at`.
- all gateway hot paths indexed:
  - `action_intents(contract_id, status, scheduled_at)`
  - `exception_cases(status, severity, created_at)`
  - `gate_evaluations(contract_id, gate_name, as_of_date)`
  - transport lookups by `(truck_id, effective_from/effective_to)` and `(driver_id, effective_from/effective_to)`.

#### PR1 acceptance
- migrations run repeatedly without drift,
- existing Phase tests pass unchanged,
- baseline reads/writes unaffected.

---

### PR2 — Gate + Intent Engine

#### 1) Deterministic gate engine
Implement gates:
- `contract_active_valid`
- `quantity_tolerance`
- `evidence_ready`
- `coa_complete`
- `payment_match_confidence`
- `capacity_available`
- `transport_assignment_valid`

Persist each gate run in `gate_evaluations`:
- `evaluation_id`, `gate_name`, `subject_type`, `subject_id`, `as_of_date`,
- `status` (`PASS|FAIL|WARN`), `score`, `reason_code`, `details_json`, `evaluated_at`.

#### 2) Intent engine
Intents:
- `plan_deliveries`
- `materialize_due`
- `auto_progress`
- `generate_pack`
- `apply_payment`

Contract:
- `action_intents`: planned action, prerequisites, policy version snapshot.
- `action_executions`: actual attempt(s), idempotency key, success/failure details.

#### 3) Exception creation
If gate fails or confidence below threshold:
- create `exception_cases`,
- attach blocked intent reference,
- no side effects executed for blocked intent.

#### 4) CLI contracts
Add commands:
- `run-autonomy --as-of YYYY-MM-DD [--contract-id <id>] [--dry-run]`
- `list-cases [--status OPEN|RESOLVED]`
- `decide-case --case-id <id> --decision APPROVE|REJECT|OVERRIDE --reason <text>`
- `autonomy-metrics --as-of YYYY-MM-DD --out-dir <dir>`

#### PR2 acceptance
- intent idempotency proven with replay tests,
- exceptions-only flow proven,
- decision + resume path proven.

---

### PR3 — Entity Intelligence (Transport + MDM)

#### 1) Suggestion service
Auto-suggest from aliases/history:
- driver from truck,
- truck from driver,
- partner from corridor history.

Persist:
- candidates + confidence + explanation in `delivery_transport_suggestions`.

#### 2) Auto-apply policy
- auto-apply when confidence >= configured threshold and compliance valid,
- otherwise create `exception_cases`.

#### 3) Immutable execution snapshot
At delivery materialization:
- write `delivery_transport_snapshot` with selected values and source/reason,
- generated docs read from snapshot, not mutable master rows.

#### 4) Learning loop
Store accepted/rejected outcomes:
- `decision_features` + `decision_outcomes`,
- no online model training requirement in this phase; deterministic scoring first.

#### PR3 acceptance
- deterministic suggestion ranking,
- conflict/compliance-expired cases block with explicit reason,
- snapshot immutability validated.

---

### PR4 — UI Command Center (Autonomy-first UX)

#### 1) New default route
- `/v2/command-center` becomes primary entry.

#### 2) Primary panels
- `Needs Decision`
- `Auto Running`
- `Completed Today`

#### 3) Case workflow
- `/v2/cases/{case_id}` with approve/reject/override actions.

#### 4) Existing pages retained
- keep `intake/plan/execute/settle` compatibility,
- move advanced/manual controls behind explicit “Advanced”.

#### 5) Primary actions
- `Process Intake`
- `Run Due Actions`
- `Resolve Case`

#### PR4 acceptance
- happy-path can run with minimal clicks,
- exceptions-only interaction model holds,
- existing route compatibility and legacy behavior preserved.

---

### PR5 — Performance + Reliability Hardening

#### 1) Performance budgets
- command-center query p95 < 300ms on seeded large dataset.
- autonomy run cycle completes within configured budget for target N contracts (define N in benchmark fixture).

#### 2) Benchmark/test harness
- fixture generator for high-volume contracts/deliveries/cases.
- performance tests in default test pipeline (can be tagged and run in CI profile).

#### 3) Query and reliability guards
- EXPLAIN plan checks for hot queries,
- query-count guardrails to catch obvious N+1 patterns,
- structured telemetry counters:
  - `touchless_rate`
  - `manual_intervention_count`
  - `exception_resolution_time`
  - `auto_action_success_rate`

#### 4) Reporting
- add `drep_autonomy_metrics` view with deterministic `as_of_date` semantics.

#### PR5 acceptance
- perf + functional tests pass,
- no regressions in legacy or Phase 1/1.5/1.6 behavior.

---

## Data Contracts (Required)

### `exception_cases`
Minimum fields:
- `case_id` (ULID PK)
- `case_type`
- `severity` (`BLOCKER|REVIEW`)
- `status` (`OPEN|RESOLVED|REJECTED`)
- `subject_type`, `subject_id`
- `trigger_gate`, `trigger_intent_id`
- `reason_code`, `reason_text`
- `proposed_resolution_json`
- `created_at`, `resolved_at`

### `action_intents`
Minimum fields:
- `intent_id` (ULID PK)
- `intent_type`
- `contract_id`, optional `delivery_id`
- `as_of_date`
- `status` (`PENDING|BLOCKED|EXECUTED|FAILED`)
- `policy_version_resolved`
- `idempotency_key` (unique)
- `created_at`, `updated_at`

### `action_executions`
Minimum fields:
- `execution_id` (ULID PK)
- `intent_id` (FK)
- `attempt_no`
- `status`
- `error_code`, `error_detail`
- `result_json`
- `executed_at`
- unique (`intent_id`, `attempt_no`)

### `event_log` (append-only)
Minimum fields:
- `event_id` (ULID PK)
- `event_type`
- `subject_type`, `subject_id`
- `payload_json`
- `created_at`
- `source` (`ui|cli|automation`)

No update/delete in service layer; insert only.

---

## Test Strategy

### Non-regression baseline (every PR)
- `./scripts/test_default.sh`
- targeted legacy smoke:
  - `python3 run.py validate --transaction data/sample_transaction_contract_processing.json`
  - `python3 run.py generate --transaction data/sample_transaction_contract_processing.json`

### PR-specific tests
- PR1: migration idempotency + FK/constraint tests.
- PR2: gate pass/fail matrix + intent idempotency + case lifecycle tests.
- PR3: suggestion confidence determinism + snapshot immutability tests.
- PR4: command-center route/handler tests + case decision UI flow tests.
- PR5: benchmark/perf assertions + metrics view determinism tests.

### Host smoke (outside sandbox)
- `python3 run.py serve` GET `/` = 200
- `python3 run.py serve-v2` GET `/v2/command-center` = 200
- non-skip pack generation path.

---

## Proof Bundle Contract

Per PR write:
- `.state/phase2-proof/prX/<timestamp>/SUMMARY.md`
- command logs (`tests.log`, `legacy_validate.log`, `legacy_generate.log`)
- targeted scenario logs
- DB snapshots (where applicable)
- perf outputs for PR5 (`benchmark.json`, explain summaries)

---

## Rollout Artifacts

Final phase deliverables (after PR5):
1. `SPEC_PHASE2_AUTONOMY.md` (this file)
2. PR1..PR5 implementation + proof bundles
3. Updated `README.md` + operational runbook
4. `PHASE2_ROLLOUT_PLAN.md` with staged enablement and rollback steps

---

## Stop Condition

After PR5 proof:
- return only P0/P1 issues,
- provide release recommendation (`GO` / `NO-GO`) with reasons and rollback note.
