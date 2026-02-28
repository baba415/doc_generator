# SPEC_PHASE2_PR6_UI

Status: Step 1 specification only (no implementation in this step).

## Objective

Implement PR6 as the canonical daily workspace so operators can run the day from `/v2/portfolio` with minimal page switching:

1. See what needs action now.
2. Run safe autonomous actions quickly.
3. Resolve blockers only through exception workflows.

## Hard Invariants (must remain unchanged)

- Legacy commands unchanged: `python3 run.py generate|validate|serve`.
- Lane invariants A/B unchanged.
- Pack semantics unchanged: exactly 4 docs in order (WB, WT, COA, Goods Invoice); receipt only on `mark-paid`.
- KG integer canonical storage; MT display only.
- Deterministic `--as-of` behavior.
- All default `today` resolution uses UTC date (`today_utc`), never local server date.
- Evidence immutability and audit hashing.
- No schema sprawl unless blocked; prefer views over new base tables.

## PR6 Scope

### 1) Canonical daily workspace

Route:
- `/v2/portfolio` remains canonical landing page.
- Add alias route `/v2/workbench` -> same view as `/v2/portfolio`.

UI sections required:
- `Needs Decision`
- `Due Actions`
- `At Risk`

Contract card required fields:
- contract/lpo identifiers
- buyer/vendor labels
- lpo_state
- due/open lot counts
- delivered-not-invoiced count
- outstanding total
- next action badge

Primary actions only:
- `Run Recommended` (single contract)
- `Resolve Exceptions` (single contract)

Advanced actions:
- Existing Plan/Execute/Settle links remain available only under Advanced presentation.

### 2) Run Recommended (single contract)

Backend orchestration entrypoint (service-level, not web-only logic):
- Refresh contract state (`as_of_date` from UI input or `today_utc` date).
- Run recommended cycle with existing gate contracts.
- Return structured step timeline:
  - step
  - status (`SUCCESS|SKIPPED|BLOCKED|ERROR`)
  - reason_code/details
  - emitted case IDs (if any)

UI behavior:
- success -> execute timeline view with run context
- blocked -> redirect to `/v2/exceptions?contract_id=<id>` with blocker context

### 3) Run All Eligible (bulk, guarded)

Add portfolio action: `Run All Eligible`.

Mandatory safety contract:
- dry-run preview required before execute.
- hard caps:
  - `max_contracts_per_run = 20`
  - `max_actions_per_run = 200`
- skip blocked/ambiguous contracts only; continue safe contracts.
- emit per-contract skip reason codes.
- no silent override.

Idempotency and replay contract:
- Preview idempotency key (deterministic):
  - `run_all_preview::{as_of_date_utc}::{benchmark_version}::{max_contracts_per_run}::{max_actions_per_run}::{contract_scope_hash}`
- Execute idempotency key (deterministic):
  - `run_all_execute::{as_of_date_utc}::{benchmark_version}::{preview_token}::{eligible_contracts_hash}`
- `preview_token` is derived from normalized preview payload (sorted keys, UTC-normalized dates).
- Retry behavior after partial completion:
  - already-executed contract actions must be recognized via idempotency records and skipped as `ALREADY_APPLIED`;
  - remaining eligible contracts continue;
  - no duplicate side effects (no duplicate documents, payments, or action executions).

Result payload contract:
- run summary totals
- executed contracts list
- skipped contracts list with reason_code
- blocked contracts with case_ids

### 4) Today Queue sourcing

Prefer a single aggregated query path (or view-backed query) for portfolio sections.
Do not introduce N+1 query patterns.

Allow additive view if needed:
- `ui_task_queue` (preferred as SQL view first).

Minimum queue typing:
- `NEEDS_DECISION`
- `DUE_ACTION`
- `AT_RISK`

UTC canon for queueing:
- Queue windows and “due today” calculations use `as_of_date_utc`.
- If user does not provide `as_of_date`, server resolves `as_of_date_utc = datetime.now(timezone.utc).date()`.

## Data / Telemetry Requirements (PR6 minimum)

No destructive schema changes.

Permitted additive items only if needed:
- new view(s) for queue composition and KPI measurement.
- telemetry event rows for:
  - action initiated from portfolio
  - run recommended result
  - bulk run preview/execute/skip outcomes

All timestamps persisted in UTC.

## KPI Contracts (PR6-relevant)

Use formulas from `SPEC_PHASE2_EXEC_CONTROL.md` and `PHASE2_GATES.md`.

PR6 gate metric:
- `portfolio_action_start_rate >= 0.80` on benchmark_version-matched fixtures.

Defaults:
- `lookback_window_days = 30` unless explicit override in KPI report.
- `max_contracts_per_run = 20`
- `max_actions_per_run = 200`

## Performance Budgets (PR6 mandatory)

Benchmark fixture:
- `benchmark_version` must match for all perf comparisons.
- Fixture size for PR6 perf gate: `500 active contracts` in portfolio workload set.

Targets:
- `/v2/portfolio` server render p95 `< 1.5s` on benchmark fixture.
- portfolio core aggregated query p95 `< 300ms` on benchmark fixture.

Fail rule:
- PR6 fails if either p95 target regresses beyond threshold on the same benchmark_version and fixture size.

## Tests (required for PR6)

### Unit/Service
- portfolio queue classification logic.
- run-all cap enforcement.
- run-all skip reason emission.
- idempotent behavior for repeated run requests.
- run-all preview/execute idempotency key determinism.
- retry after partial completion does not duplicate side effects and emits `ALREADY_APPLIED`.

### UI route/integration
- `/v2/portfolio` renders all three sections.
- `/v2/workbench` maps to portfolio.
- single run success renders timeline.
- single run blocked redirects to filtered exceptions.
- run-all preview required before execute.
- run-all execute obeys caps and skip semantics.
- default “today” path resolves using UTC date for queue and action windows.

### Regression
- `./scripts/test_default.sh` passes.
- `python3 -m unittest -v tests.test_phase2_ui` passes in host-capable environment.
- legacy `validate` and `generate` still pass.
- host smoke script passes:
  - `./scripts/host_ui_smoke.sh 8865`

## Acceptance Criteria

1. Operators can initiate day-to-day actions from `/v2/portfolio` without visiting Plan/Execute/Settle for common cases.
2. Blocked flows redirect directly to exceptions with scoped contract context.
3. Bulk run cannot execute without preview and respects hard caps.
4. Every skipped contract in bulk run has a machine-readable reason code.
5. Run-all retries after partial completion are idempotent and non-duplicative.
6. No legacy behavior regressions.

## Proof Bundle (PR6)

Write under:
- `.state/phase2-proof/pr6/<timestamp>/`

Required artifacts:
- portfolio route output snapshots
- run recommended success/blocked evidence
- run-all preview/execute logs with skip reasons
- KPI measurement output for portfolio_action_start_rate
- regression logs (`test_default`, `phase2_ui`, legacy validate/generate, host smoke)

## Out of Scope (PR6)

- PR7 exception UX redesign and consequence cards.
- PR8 intake/planning copilot deepening.
- PR9 transport/doc completion copilot.
- PR10 settlement copilot and KPI strip UI.
