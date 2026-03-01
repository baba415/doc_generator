# SPEC_PHASE2_PR15_DRIFT_ROOT_CAUSE_REDUCTION

Status: Step 1 specification lock only (no implementation in this step).

## Objective

Implement PR15 as **deterministic drift root-cause diagnostics** on top of PR13/PR14:
- identify recurring root causes behind PR8/PR9/PR10 drift outcomes,
- provide auditable, read-only operator guidance for reduction actions,
- preserve existing gate formulas and governance controls.

PR15 must not change PR8/PR9/PR10 formulas, thresholds, recommendation logic, drift-state logic, or waiver behavior.

---

## Hard Constraints (non-negotiable)

1. Spec-only in this step. No runtime code, tests, config, or script modifications.
2. No PR8/PR9/PR10 gate formula changes.
3. No PR13 drift-state formula/precedence changes.
4. No PR14 triage mapping/waiver behavior changes.
5. No auto-remediation, no auto-waiver, no policy mutation.
6. Preserve invariants:
   - legacy commands unchanged (`run.py generate|validate|serve`),
   - lane/pack/receipt invariants unchanged,
   - KG canonical storage, MT display,
   - UTC/as-of determinism,
   - evidence immutability and audit trail.
7. Schema discipline:
   - prefer views/event payloads over new base tables,
   - add base table only if implementation is blocked and justified in PR notes.

---

## Scope Boundaries

### In scope
- Drift root-cause service based on existing reports and operational telemetry.
- Deterministic recurring-cause ranking by gate (`pr8`, `pr9`, `pr10`).
- Read-only root-cause summary in portfolio and deep-link into exceptions.
- CLI report command for operational review and release governance.

### Out of scope
- Any change to gate formulas/thresholds/recommendation logic.
- Any change to drift-state reason-code precedence.
- Automated correction actions (policy edits, threshold edits, registry edits, auto-case resolution changes).
- PR16+ capability work.

---

## Root-Cause Taxonomy Contract

Allowed root-cause codes only:
- `insufficient_observability_data`
- `benchmark_dataset_misalignment`
- `input_quality_regression`
- `planning_policy_mismatch`
- `transport_assignment_instability`
- `document_linkage_instability`
- `settlement_matching_instability`
- `manual_override_concentration`
- `no_recurring_root_cause`

Each diagnosed cause must include:
- `root_cause_code`
- `root_cause_confidence` (0.0–1.0)
- `evidence_refs[]` (event/report paths or ids)
- `gate_name`
- `impact_metrics` (see contract below)

No free-text cause codes are allowed in persisted output.

---

## Deterministic Diagnosis Rules

### Inputs
- `as_of_date`
- `lookback_window_days`
- `benchmark_version`
- `generated_at_utc`

### Source data (reuse existing)
1. PR13 drift report (`phase2_drift_report_<as_of>.json`)
2. PR14 drift triage status (open/resolved `DRIFT_MONITORING` cases)
3. Existing telemetry:
   - `exception_cases` / `human_decisions`
   - `action_intents` / `action_executions`
   - `event_log`
   - `autonomy-metrics` and benchmark metrics snapshots

### Recurrence definition
A root cause is `recurring=true` only if:
- it appears at least **3 times** in the lookback window, and
- spans at least **2 distinct UTC dates**.

### Deterministic tie-break order
If two causes have equal recurrence counts:
1. higher affected-contract count,
2. higher affected-gate severity impact (`ALERT` > `WATCH` > `INSUFFICIENT_DATA` > `PASS`),
3. lexical `root_cause_code` ascending.

### Reason-code precedence for aggregate state
1. `insufficient_observability_data`
2. `benchmark_dataset_misalignment`
3. `manual_override_concentration`
4. any gate-specific instability/mismatch cause
5. `no_recurring_root_cause`

---

## Service Contract

### `phase2_drift_root_cause(...)`
Signature:
- `phase2_drift_root_cause(as_of_date, lookback_window_days, benchmark_version, out_dir, drift_report_ref=None, triage_status_ref=None) -> dict`

Behavior:
1. Load drift report + triage summary + operational telemetry.
2. Diagnose per-gate causes deterministically.
3. Compute recurrence, impact, and ranked top causes.
4. Emit JSON + Markdown artifacts.
5. Append audit event:
   - `PHASE2_DRIFT_ROOT_CAUSE_EXPORTED`
6. Return summary payload with:
   - `aggregate_state`
   - `aggregate_reason_code`
   - `top_recurring_causes`
   - `report_json_path`
   - `report_md_path`

No writes to policy tables, gate outputs, or waiver files.

---

## Drift Root-Cause Report Contract

### Artifact names
- `phase2_drift_root_cause_<as_of_date>.json`
- `phase2_drift_root_cause_<as_of_date>.md`

### JSON top-level
- `inputs`
  - `as_of_date`
  - `lookback_window_days`
  - `benchmark_version`
  - `generated_at_utc`
  - `drift_report_ref`
  - `triage_status_ref`
- `gates[]`
  - one block each for `pr8`, `pr9`, `pr10`
- `aggregate`
  - `state`: `PASS|WATCH|ALERT|INSUFFICIENT_DATA`
  - `reason_code`
  - `top_recurring_causes[]`
  - `recommended_manual_actions[]` (read-only guidance text)

### Per-gate block
- `gate_name`
- `drift_state`
- `drift_reason_code`
- `diagnosed_causes[]`
  - `root_cause_code`
  - `recurring` (bool)
  - `occurrence_count`
  - `affected_contracts`
  - `root_cause_confidence`
  - `impact_metrics`
  - `evidence_refs[]`

### `impact_metrics` minimum fields
- `exception_count`
- `manual_decision_count`
- `failed_or_blocked_actions`
- `avg_resolution_time_hours`

---

## CLI Contract

Additive command in implementation step:

```bash
python3 run.py phase2-drift-root-cause \
  --as-of 2026-02-28 \
  --lookback-window-days 30 \
  --benchmark-version phase2.pr12.v1 \
  --out-dir .state/phase2-proof/pr15/manual
```

Optional refs:
- `--drift-report <path>`
- `--triage-status <path>`

Output:
- summary + report paths; no state mutation.

---

## UI Contract (read-only)

Portfolio (`/v2/portfolio`) add read-only **Drift Root Cause Summary**:
- top 3 recurring causes with gate tags and occurrence counts,
- aggregate state/reason,
- report link.

Required deep-link:
- `/v2/exceptions?case_type=DRIFT_MONITORING&status=OPEN`

Guardrail:
- no resolve/edit/remediation controls in this panel.
- all manual actions stay in exceptions workflow.

---

## Determinism + UTC Canon

1. All timestamps in artifacts and events are UTC.
2. Same inputs + same source snapshots must produce identical root-cause ranking.
3. Missing required source data yields deterministic `insufficient_observability_data`.
4. `benchmark_version` mismatch with drift inputs yields `benchmark_dataset_misalignment`.

---

## Acceptance Tests (implementation step)

### Service/CLI
1. Root-cause report is deterministic for identical inputs.
2. Missing telemetry yields `insufficient_observability_data`.
3. Benchmark mismatch yields `benchmark_dataset_misalignment`.
4. Recurrence threshold logic (>=3 occurrences, >=2 UTC dates) is enforced.
5. Tie-break order is deterministic and tested.
6. CLI output matches report contract schema.
7. `PHASE2_DRIFT_ROOT_CAUSE_EXPORTED` event is appended.

### UI
8. Portfolio renders read-only root-cause summary + exceptions link.
9. No edit/remediation controls in root-cause panel.

### Regression
10. `./scripts/test_default.sh` passes.
11. `python3 -m unittest -v tests.test_phase2_ui` passes in host-capable env.
12. Legacy `run.py validate` and `run.py generate` pass.
13. `./scripts/host_ui_smoke.sh 8865` passes in host-capable env.

---

## Proof Bundle Contract (implementation step)

Write under:
- `.state/phase2-proof/pr15/<timestamp>/`

Required artifacts:
- `phase2_drift_root_cause_<as_of>.json/.md`
- source refs used (`phase2_drift_report_<as_of>.json`, drift status snapshot)
- regression logs (`test_default.log`, `test_phase2_ui.log`, legacy validate/generate, host smoke)
- provenance (`tested_sha.txt`, `git_status_short.txt`)

---

## Explicit Non-Goals

- No gate formula updates (PR8/PR9/PR10 unchanged).
- No drift threshold/state logic updates (PR13 unchanged).
- No triage lifecycle behavior changes (PR14 unchanged).
- No automatic corrective actions.
- No policy/registry/waiver mutation.
- No PR16+ scope.
