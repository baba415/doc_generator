# SPEC_PHASE2_PR14_DRIFT_OPERATIONS

Status: Step 1 specification lock only (no implementation in this step).

## Objective

Implement PR14 as the **operational closure loop** for PR13 drift monitoring:
- convert drift report outcomes into deterministic, auditable operator work items,
- keep manual intervention inside Exceptions Inbox,
- keep drift behavior governance-only (no autonomous remediation).

PR14 must not alter PR8/PR9/PR10 formulas, gate thresholds, recommendation logic, or waiver behavior.

---

## Hard Constraints (non-negotiable)

1. Spec-only in this step. No runtime code, test, config, or script modifications.
2. No PR8/PR9/PR10 gate formula changes.
3. No threshold auto-tuning.
4. No automatic waiver creation/activation/deactivation.
5. No policy mutation from drift outcomes.
6. Preserve invariants:
   - legacy commands unchanged (`run.py generate|validate|serve`),
   - lane/pack/receipt invariants unchanged,
   - KG canonical storage, MT display,
   - deterministic UTC/as-of behavior,
   - evidence immutability and audit trail.
7. Schema discipline:
   - reuse existing `exception_cases`, `human_decisions`, `event_log`,
   - no new base table unless implementation is blocked.

---

## Scope Boundaries

### In scope
- Drift triage service that maps PR13 drift states to exception cases.
- Deterministic case idempotency and severity mapping.
- Read-only portfolio drift operations summary + link into exceptions queue.
- CLI contracts for triage/status.

### Out of scope
- Any correction of underlying gate performance (PR15+).
- Any auto-approval, auto-remediation, or auto-waiver.
- Any policy/registry edits triggered by drift.
- Any change to PR13 reason-code precedence.

---

## Drift-to-Case Mapping Contract

### Source
- PR13 drift report payload generated via `phase2_drift_report(...)`.

### Case projection rule
- For each gate block (`pr8`, `pr9`, `pr10`) create/maintain one deterministic case keyed by:
  - `as_of_date`
  - `lookback_window_days`
  - `benchmark_version`
  - `gate_name`
  - `reason_code`

### Case type
- `case_type = "DRIFT_MONITORING"`

### Severity mapping
- `MISMATCH` -> `BLOCKER`
- `ALERT` -> `BLOCKER`
- `INSUFFICIENT_DATA` -> `REVIEW`
- `WATCH` -> `INFO`
- `PASS` -> no new case

### Idempotency key
- `drift_case::<as_of_date>::<lookback_window_days>::<benchmark_version>::<gate_name>::<reason_code>`

### Lifecycle behavior
1. If gate state in `{MISMATCH, ALERT, INSUFFICIENT_DATA, WATCH}`:
   - upsert `OPEN` case by idempotency key (no duplicates on replay).
2. If gate state is `PASS`:
   - auto-resolve previously open `DRIFT_MONITORING` cases for same `gate_name` + `benchmark_version`,
   - set resolution reason code `drift_cleared` in `details_json`.

### Case details_json contract
Must include:
- `as_of_date`
- `lookback_window_days`
- `benchmark_version`
- `generated_at_utc`
- `gate_name`
- `drift_state`
- `reason_code`
- `comparisons` (full array from drift report)
- `report_json_path`
- `report_md_path`
- `blocking_reasons` (aggregate array)

---

## Service Contracts

### `phase2_drift_triage(...)`
Signature:
- `phase2_drift_triage(as_of_date, lookback_window_days, benchmark_version, out_dir, benchmark_metrics_ref=None, live_metrics_ref=None) -> dict`

Behavior:
1. Build PR13 drift report (reuse existing pipeline).
2. Project drift gates to cases (mapping above).
3. Persist audit events:
   - `PHASE2_DRIFT_TRIAGE_COMPLETED`
   - `PHASE2_DRIFT_CASE_OPENED`
   - `PHASE2_DRIFT_CASE_UPDATED`
   - `PHASE2_DRIFT_CASE_RESOLVED`
4. Return summary:
   - `cases_opened`
   - `cases_updated`
   - `cases_resolved`
   - `open_cases_by_severity`
   - `drift_state`
   - `recommendation`

### `phase2_drift_operations_snapshot(...)`
Read-only summary used by UI:
- open drift cases by gate/severity/reason
- most recent triage timestamp
- links to latest drift report

---

## CLI Contracts

### `phase2-drift-triage`
Example:
```bash
python3 run.py phase2-drift-triage \
  --as-of 2026-02-28 \
  --lookback-window-days 30 \
  --benchmark-version phase2.pr12.v1 \
  --out-dir .state/phase2-proof/pr14/manual
```

Optional refs:
- `--benchmark-metrics <path>`
- `--live-metrics <path>`

Output:
- triage summary + drift report refs + case counts.

### `phase2-drift-status`
Example:
```bash
python3 run.py phase2-drift-status \
  --as-of 2026-02-28 \
  --lookback-window-days 30 \
  --benchmark-version phase2.pr12.v1
```

Output:
- current drift health summary + open drift case counts (read-only).

---

## UI Contract (read-only + exception-first)

Portfolio (`/v2/portfolio`) enhancements:
1. Add "Drift Ops Summary" mini-strip:
   - open drift cases by severity,
   - last triage timestamp,
   - aggregate drift state/recommendation.
2. Add explicit link:
   - `/v2/exceptions?case_type=DRIFT_MONITORING&status=OPEN`
3. No direct resolve/edit controls in portfolio strip.
4. Resolution remains in exceptions workflow only.

Exceptions (`/v2/exceptions`) behavior:
- support filtering by `case_type=DRIFT_MONITORING`.

---

## Determinism & UTC Canon

1. All timestamps (`created_at`, `updated_at`, `resolved_at`, triage/report times) are UTC.
2. Case projection for identical inputs must be deterministic.
3. Re-running triage with same inputs:
   - no duplicate open cases,
   - idempotent counts and stable refs.

---

## Acceptance Tests (implementation step)

### Service/CLI
1. Drift triage opens expected case(s) for `ALERT`.
2. Drift triage opens expected case(s) for `INSUFFICIENT_DATA`.
3. Triage replay is idempotent (no duplicate cases).
4. Gate `PASS` resolves prior open drift case(s) with `drift_cleared`.
5. CLI `phase2-drift-triage` writes report + returns case summary.
6. CLI `phase2-drift-status` returns read-only deterministic snapshot.

### UI
7. Portfolio renders Drift Ops Summary + Open Drift Cases link.
8. Portfolio has no raw edit controls for drift operations.
9. Exceptions filter returns only drift monitoring cases when `case_type=DRIFT_MONITORING`.

### Regression
10. `./scripts/test_default.sh` passes.
11. `python3 -m unittest -v tests.test_phase2_ui` passes in host-capable env.
12. Legacy `run.py validate` and `run.py generate` pass.
13. `./scripts/host_ui_smoke.sh 8865` passes in host-capable env.

---

## Proof Bundle Contract (implementation step)

Write under:
- `.state/phase2-proof/pr14/<timestamp>/`

Required artifacts:
- `phase2_drift_report_<as_of>.json/.md`
- `phase2_drift_triage_<as_of>.json`
- `phase2_drift_status_<as_of>.json`
- drift-case snapshot export (`open_drift_cases.json`)
- test logs (`test_default.log`, `test_phase2_ui.log`, targeted PR14 tests)
- legacy logs (`legacy_validate.log`, `legacy_generate.log`, host smoke log)
- provenance (`tested_sha.txt`, `git_status_short.txt`)

---

## Explicit Non-Goals

- No PR8/PR9/PR10 formula rewrites.
- No threshold changes or self-tuning.
- No automatic waiver changes.
- No policy mutation.
- No PR15+ implementation scope.
