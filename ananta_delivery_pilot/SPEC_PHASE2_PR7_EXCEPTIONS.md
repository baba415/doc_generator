# SPEC_PHASE2_PR7_EXCEPTIONS

Status: Step 1 specification lock only (no PR7 implementation in this step).

## Objective

Implement PR7 as an exception-first operational layer:
- manual work happens primarily in `/v2/exceptions`,
- operators make decisions from compact decision cards,
- resume paths are explicit and auditable,
- command center explains what ran, what blocked, and next action.

## Hard Invariants (must remain unchanged)

- Legacy commands unchanged: `python3 run.py generate|validate|serve`.
- Lane invariants A/B unchanged.
- Pack semantics unchanged: exactly 4 docs in order (WB, WT, COA, Goods Invoice); receipt only on `mark-paid`.
- KG integer canonical storage; MT display only.
- Deterministic `--as-of` behavior.
- UTC canon for persisted timestamps and KPI/SLA calculations.
- Evidence immutability + hash/audit trail.
- No broad schema redesign; additive changes only if blocked.

## PR7 Scope

### 1) Exceptions inbox as primary manual surface

Route:
- `/v2/exceptions` is the canonical manual workspace.

Behavior:
- group by `severity` then `reason_code`,
- show decision card per case with:
  - case id, contract id, case type, created_at_utc,
  - blocker/review severity badge,
  - reason code and concise details payload,
  - recommended next action text.

Decision actions (required):
- `APPROVE`
- `REJECT`
- `OVERRIDE`

Decision input contract:
- reason is mandatory for all decisions,
- decision payload must be persisted in `human_decisions`,
- idempotent decision submission per case/action/reason hash.

### 2) Consequence preview + resume

Each open case card must support:
- consequence preview (what downstream actions may run / remain blocked),
- `Approve + Resume` with optional dry-run toggle,
- visible result summary after resume:
  - autonomy_run_id,
  - actions attempted/succeeded/skipped/blocked,
  - resulting case status.

No silent resume:
- failed resume must keep case trace and expose error reason.

### 3) Autopilot explainability panel

Add/extend explainability panel from portfolio and contract execute views:
- what ran,
- what skipped,
- what blocked,
- why (reason_code + gate),
- next recommended action.

Data source:
- `action_intents` + latest `action_executions`,
- linked `exception_cases`,
- deterministic ordering by created_at_utc then id.

### 4) Primary-path guardrail

- Portfolio and default command-center path must not expose raw state-machine edit forms.
- Raw/manual edit controls remain under explicit `Advanced` sections only (audit/recovery path).
- Exceptions page remains the first-class surface for manual intervention.

### 5) SLA framing on exceptions

Use locked SLA tiers from control spec:
- `BLOCKER` target: 4h
- `REVIEW` target: 24h
- `INFO` target: 72h

UI requirements:
- show age and SLA state (within/at-risk/breached),
- all SLA calculations based on UTC timestamps.

## Data and API Contracts (PR7)

No destructive schema changes.

Allowed additive changes (only if needed):
- optional view for exception queue rollups (preferred over base-table sprawl),
- optional helper fields in response payloads for consequence preview.

Must use existing truth tables:
- `exception_cases`,
- `human_decisions`,
- `action_intents`,
- `action_executions`,
- `event_log`.

Audit events required:
- decision submitted,
- case resolved/reopened (if applicable),
- resume requested,
- resume completed/failed.

## KPI and Stop/Go Gate (PR7)

Gate metric:
- `manual_interactions_in_exceptions_rate >= 0.80`

Definition:
- numerator: manual interactions performed via `/v2/exceptions` decision actions
- denominator: total manual interactions across primary workflow pages
- evaluated with explicit `as_of_date` and fixed lookback window.

Defaults:
- `lookback_window_days = 30` unless report override,
- `benchmark_version` must match for cross-PR comparison.

Stop/Go:
- fail gate if metric < 0.80,
- failed gate blocks promotion unless waiver logged by Product + Ops + Engineering (with expiry and fallback plan).

## Tests (required for PR7)

### Service / domain tests
- `decide_exception_case` enforces required reason.
- decision idempotency (replay yields stable outcome, no duplicate side effects).
- resume flow persists deterministic audit trail.
- consequence preview payload is deterministic for fixed as-of.

### UI tests
- `/v2/exceptions` groups cards by severity/reason_code.
- decision action with missing reason fails.
- approve/reject/override paths persist decisions.
- approve+resume returns run summary and refreshed case state.
- explainability panel shows run timeline with blocked/skip reasons.
- no raw edit forms on primary path pages.

### Regression
- `./scripts/test_default.sh` passes.
- `python3 -m unittest -v tests.test_phase2_ui` passes in host-capable environment.
- host smoke remains green:
  - `./scripts/host_ui_smoke.sh 8865`
- legacy validate/generate still pass.

## Acceptance Criteria

1. Operators can clear blockers without opening Plan/Execute raw forms.
2. At least 80% manual interactions are performed in `/v2/exceptions` (benchmark version matched).
3. Every decision has reason + audit trace + deterministic resume behavior.
4. Explainability panel can answer what ran/blocked/next without table inspection.
5. Legacy and Phase invariants remain unchanged.

## Proof Bundle (PR7)

Write under:
- `.state/phase2-proof/pr7/<timestamp>/`

Required artifacts:
- exceptions route snapshots (grouped cards),
- decision/reason enforcement logs,
- resume run summaries,
- explainability timeline snapshots,
- KPI output for manual_interactions_in_exceptions_rate,
- regression logs (`test_default`, `phase2_ui`, legacy validate/generate, host smoke).

## Out of Scope (PR7)

- PR8 intake/planning copilot deepening.
- PR9 transport/doc completion copilot.
- PR10 settlement copilot + KPI strip.
