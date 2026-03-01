# SPEC_PHASE2_PR13_DRIFT_MONITORING

Status: Step 1 specification lock only (no implementation in this step).

## Objective

Implement PR13 as **benchmark-to-live drift monitoring** for PR8/PR9/PR10 gate reliability.

PR13 must detect and report when live operational metrics diverge from deterministic benchmark baselines, while preserving all existing gate formulas and operational controls.

## Hard Constraints (non-negotiable)

1. Spec-only in this step. No runtime code, test, config, or script modifications.
2. No PR8/PR9/PR10 formula changes.
3. No threshold auto-tuning.
4. No automatic waiver creation or activation.
5. No policy mutation from drift outputs.
6. Preserve all invariants:
   - legacy commands unchanged (`run.py generate|validate|serve`),
   - lane rules unchanged,
   - pack/receipt rules unchanged,
   - KG canonical storage, MT display,
   - UTC/as-of determinism,
   - evidence immutability.

## Scope Boundaries

### In scope
- Deterministic comparison of benchmark metrics vs live metrics for PR8/PR9/PR10.
- Drift report generation as JSON + Markdown artifacts.
- Read-only drift visibility in UI.

### Out of scope
- Any change to gate formulas, gate thresholds, or gate recommendation logic.
- Any autonomous action based on drift state.
- Any operational state transition or exception decision mutation.
- PR14+ autonomy behavior.

## Data Contract

All drift computations must be based on explicit caller inputs and UTC timestamps.

Required inputs:
- `as_of_date` (YYYY-MM-DD)
- `lookback_window_days` (int > 0)
- `benchmark_version` (string)
- `generated_at_utc` (derived at report build time)

Required source payloads:
1. Benchmark metrics payload (deterministic fixture path)
2. Live metrics payload (production/live data path)

Both payloads must include:
- `as_of_date`
- `lookback_window_days`
- `benchmark_version`
- `generated_at_utc`
- metric fields required by PR8/PR9/PR10 gate snapshots

## Drift Report Contract

### Artifact names
- `phase2_drift_report_<as_of_date>.json`
- `phase2_drift_report_<as_of_date>.md`

### JSON top-level schema
- `inputs`
  - `as_of_date`
  - `lookback_window_days`
  - `benchmark_version`
  - `generated_at_utc`
  - `benchmark_metrics_ref`
  - `live_metrics_ref`
- `gates`
  - list of `pr8`, `pr9`, `pr10` blocks
- `aggregate`
  - `drift_state`
  - `recommendation`
  - `blocking_reasons[]`

### Per-gate block schema
- `gate_name` (`pr8|pr9|pr10`)
- `drift_state` (`PASS|WATCH|ALERT|INSUFFICIENT_DATA|MISMATCH`)
- `reason_code`
- `comparisons[]` where each row contains:
  - `metric_name`
  - `benchmark_value`
  - `live_value`
  - `delta`
  - `delta_type` (`absolute|relative|categorical`)
  - `threshold`
  - `within_threshold` (bool|null)

### Aggregate contract
- `drift_state`:
  - `PASS` when all gates are pass/watch with valid data,
  - `WATCH` when one or more gates in watch, none in alert,
  - `ALERT` when any gate exceeds alert drift threshold,
  - `INSUFFICIENT_DATA` when required benchmark/live data missing,
  - `MISMATCH` when benchmark version mismatch invalidates comparison.
- `recommendation`:
  - `NO_ACTION`, `INVESTIGATE`, or `BLOCK_PROMOTION` (reporting only; no auto execution).

## Deterministic Drift Reason Codes

Allowed reason codes only:
- `pass`
- `insufficient_live_data`
- `insufficient_benchmark_data`
- `benchmark_version_mismatch`
- `drift_exceeds_threshold`
- `drift_within_watch_band`

Precedence (deterministic):
1. `benchmark_version_mismatch`
2. `insufficient_benchmark_data`
3. `insufficient_live_data`
4. `drift_exceeds_threshold`
5. `drift_within_watch_band`
6. `pass`

## Initial Default Drift Thresholds (auditable)

These thresholds are comparison thresholds only and **do not** alter gate formulas.

### PR8 drift thresholds
- `median_manual_fields_per_intake`:
  - alert: `abs(live - benchmark) > 1.0`
  - watch: `abs(live - benchmark) > 0.5`
- `autoplan_zero_edit_common_case_rate`:
  - alert: `live - benchmark < -0.10`
  - watch: `live - benchmark < -0.05`

### PR9 drift thresholds
- `manual_transport_fields_per_delivery`:
  - alert: `live - benchmark > 0.75`
  - watch: `live - benchmark > 0.30`
- `doc_autolink_precision`:
  - alert: `live - benchmark < -0.05`
  - watch: `live - benchmark < -0.03`

### PR10 drift thresholds
- `payment_suggestion_acceptance_rate`:
  - alert: `live - benchmark < -0.10`
  - watch: `live - benchmark < -0.05`
- `auto_action_success_rate`:
  - alert: `live - benchmark < -0.10`
  - watch: `live - benchmark < -0.05`

Threshold source:
- Additive config only (e.g., `config/drift_thresholds.json` in implementation step).
- If config missing, use above defaults.

## Service / CLI Contracts

### Service contract
- `phase2_drift_report(as_of_date, lookback_window_days, benchmark_version, out_dir, benchmark_metrics_ref=None, live_metrics_ref=None) -> dict`

Implementation expectation:
- Reuse existing PR11 benchmark/report pipeline as source of benchmark snapshots.
- Reuse existing live metrics export path (`autonomy-metrics`) for live snapshot.
- No duplicate gate stack.

### CLI contract
Additive command (if needed in implementation):
- `python3 run.py phase2-drift-report --as-of YYYY-MM-DD --lookback-window-days 30 --benchmark-version phase2.pr12.v1 --out-dir <dir>`

Optional explicit refs:
- `--benchmark-metrics <path>`
- `--live-metrics <path>`

## UI Contract (read-only)

Add a read-only drift strip/panel on `/v2/portfolio`:
- shows PR8/PR9/PR10 drift_state + reason_code + latest report link,
- no decision controls,
- no waiver controls,
- no policy edit controls.

Primary-path guardrail remains:
- operational actions remain in existing workflow,
- drift panel is observability-only.

## Acceptance Tests (for implementation step)

### Service/CLI
1. Drift report builds with deterministic output for same inputs.
2. Benchmark mismatch triggers `benchmark_version_mismatch`.
3. Missing benchmark metrics triggers `insufficient_benchmark_data`.
4. Missing live metrics triggers `insufficient_live_data`.
5. Metric deltas crossing thresholds trigger `drift_within_watch_band` or `drift_exceeds_threshold` deterministically.
6. Aggregate drift state follows precedence and deterministic recommendation mapping.

### UI
7. Portfolio drift strip renders read-only states and report links.
8. No raw edit controls appear in drift strip.

### Regression
9. `./scripts/test_default.sh` passes.
10. `python3 -m unittest -v tests.test_phase2_ui` passes in host-capable env.
11. `python3 run.py validate --transaction data/sample_transaction_contract_processing.json` passes.
12. `python3 run.py generate --transaction data/sample_transaction_contract_processing.json` passes.
13. `./scripts/host_ui_smoke.sh 8865` passes in host-capable env.

## Proof Bundle Contract (implementation step)

Write under:
- `.state/phase2-proof/pr13/<timestamp>/`

Required artifacts:
- drift report JSON + MD,
- benchmark metrics snapshot used,
- live metrics snapshot used,
- threshold config snapshot (or default-threshold declaration artifact),
- regression logs (`test_default`, `phase2_ui`, legacy validate/generate, host smoke),
- provenance (`tested_sha.txt`, `git_status_short.txt`).

## Explicit Non-Goals

- No gate redefinition.
- No threshold self-adjustment.
- No automatic remediation actions.
- No automatic waiver changes.
- No policy or registry mutation.
- No PR14+ scope.
