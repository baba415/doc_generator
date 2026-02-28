# SPEC_PHASE2_PR10_SETTLEMENT_KPI

Status: Step 1 specification lock only (no implementation in this step).

## Objective

Implement PR10 as a settlement copilot + portfolio KPI/SLA strip:
- assist payment allocation with deterministic suggestions,
- route ambiguity to exceptions first (no silent settlement overrides),
- expose deterministic KPI and aging trends for operations leadership.

## Hard Invariants (must remain unchanged)

- Legacy commands unchanged: `python3 run.py generate|validate|serve`.
- Lane invariants A/B unchanged.
- Pack semantics unchanged: exactly 4 docs in order (WB, WT, COA, Goods Invoice); receipt only on `mark-paid`.
- KG integer canonical storage; MT display only.
- Deterministic `--as-of` behavior.
- UTC canon for persisted timestamps and KPI/SLA calculations.
- Evidence immutability + hash/audit trail.
- No broad schema redesign; additive changes only if strictly blocked.
- Prefer existing PR1–PR9 tables/views; no base-table sprawl.

## PR10 Scope

### 1) Settlement Copilot (suggestions + ambiguity gating)

Primary behavior:
- Generate allocation suggestions for open invoices using deterministic heuristics:
  - exact invoice/reference match,
  - amount match/tolerance-safe remainder,
  - date proximity,
  - buyer/vendor context consistency.
- Auto-apply only when confidence and ambiguity gates pass.
- Route ambiguous/conflicting cases to `/v2/exceptions`; no direct raw edits on primary path.

Confidence contract (initial for PR10):
- `AUTO_APPLY`: `confidence >= 0.95` and single non-conflicting candidate.
- `REVIEW`: `0.75 <= confidence < 0.95` or minor non-critical mismatch.
- `BLOCKER`: `confidence < 0.75` or conflicting candidates/policy conflict.

Mandatory reason codes (machine-readable):
- `payment_reference_missing`
- `payment_reference_ambiguous`
- `amount_mismatch_outside_policy`
- `multiple_candidate_conflict`
- `withholding_evidence_missing`
- `invoice_not_settlement_eligible`
- `insufficient_payment_data`

WHT rule remains unchanged:
- `expected_wht` is informational only.
- Outstanding reduces only via cash allocations and certified withholding events.

### 2) KPI strip on `/v2/portfolio` (deterministic + UTC)

Required KPIs (display + export):
- `touchless_rate`
- `manual_inputs_per_delivery`
- `exception_resolution_time_hours_p50`
- `exception_resolution_time_hours_p95`
- `first_time_lpo_to_pack_minutes`
- `auto_action_success_rate`
- `payment_suggestion_acceptance_rate`

SQL-level formula contract (no interpretation drift):
- `touchless_rate = completed_deliveries_without_manual_decisions / completed_deliveries`
- `manual_inputs_per_delivery = (human_decisions + user_field_overrides) / completed_deliveries`
- `exception_resolution_time_hours = resolved_at_utc - created_at_utc` (report p50/p95)
- `first_time_lpo_to_pack_minutes = first_pack_generated_at_utc - intake_confirmed_at_utc`
- `auto_action_success_rate = successful_action_executions / attempted_action_executions`
- `payment_suggestion_acceptance_rate = accepted_payment_suggestions / total_payment_suggestions_reviewed`

Denominator-safe behavior:
- If denominator is `0`, metric value must be `null`.
- Gate evaluation for that metric must be:
  - `gate_pass=false`
  - `gate_reason_code="insufficient_<metric>_data"`
- Do not substitute synthetic success values (no implicit `1.0` for no-data conditions).

Benchmark/version contract:
- All KPI calculations require explicit:
  - `as_of_date` (UTC date),
  - `lookback_window_days` (default `30`),
  - `benchmark_version`.
- Gate comparison is valid only if benchmark versions match.
- On mismatch:
  - `<gate>_pass=false`
  - `<gate>_reason_code="benchmark_version_mismatch"`.

### 3) SLA/Aging trend surfaces (ops lead)

Required trend surfaces:
- exception aging trend (weekly p50/p95 hours),
- settlement aging trend (CURRENT, 1-30, 31-60, 61-90, 90+).

Trend window contract:
- Current week window: `[as_of_date-6, as_of_date]`.
- Previous week window: `[as_of_date-13, as_of_date-7]`.
- UTC date boundaries only.

Trend state:
- `stable_or_improving` if current p95 <= previous p95.
- If either window lacks minimum sample size, mark:
  - `trend_state="insufficient_data"`,
  - `trend_gate_pass=false`.

## Route / Service / API Contracts

### Routes (PR10)

- `GET /v2/portfolio?as_of=YYYY-MM-DD&lookback_window_days=30&benchmark_version=phase2.pr10.v1`
  - includes KPI strip + trend strip for ops lead.
- `GET /v2/contracts/{contract_id}/settle?as_of=YYYY-MM-DD`
  - shows open invoices, suggested allocations, and confidence/risk badges.
- `POST /v2/contracts/{contract_id}/settle/suggest`
  - computes deterministic suggestions (idempotent preview mode by default).
- `POST /v2/contracts/{contract_id}/settle/apply-suggestion`
  - applies approved suggestion or routes to exception if gates fail.
- Existing `mark-paid` path remains the only receipt trigger.

### Service contracts (PR10)

- `settlement_suggest_allocations(contract_id, as_of_date, dry_run=true, idempotency_key=...)`
- `settlement_apply_suggestion(contract_id, suggestion_id, decision, reason, as_of_date, idempotency_key=...)`
- `portfolio_kpi_strip(as_of_date, lookback_window_days, benchmark_version)`
- `portfolio_sla_trends(as_of_date, lookback_window_days, benchmark_version)`

Idempotency/replay contract:
- Suggestion preview and apply actions require deterministic idempotency keys.
- Replays must not duplicate payment allocations or receipts.
- Retry after partial failure must return deterministic outcome with prior decision context.

### CLI/API output contract

- Extend `autonomy-metrics` output with PR10 metrics and gate fields.
- Required metadata in output:
  - `as_of_date`
  - `lookback_window_days`
  - `benchmark_version`
  - `generated_at_utc`

## Data Contract (minimal additive only)

Use existing structures first:
- `payments`
- `payment_allocations`
- `tax_withholding_events`
- `sales_transactions`
- `exception_cases`
- `human_decisions`
- `action_intents` / `action_executions`
- `event_log`
- `drep_outstanding_payments`

Allowed additive changes only if strictly blocked:
- additive indexes for hot settlement/KPI queries,
- optional helper view for KPI strip/trend aggregation.

No destructive migration and no new broad base-table families in PR10.

## Stop/Go Gates (PR10)

Primary PR10 gates:
- `payment_suggestion_acceptance_rate >= 0.70` on benchmark fixtures.
- `exception_aging_trend_state = stable_or_improving` over two weekly windows.

Secondary guardrails:
- Portfolio route p95 render stays within prior budget (no regression).
- KPI strip query p95 stays within prior budget (no regression).

Promotion rule:
- Failed gate blocks promotion unless Product + Ops + Engineering waiver is logged with expiry and fallback/feature-flag plan.

## Tests (required for PR10)

### Service/domain
- Deterministic suggestion ranking and confidence output.
- Auto-apply only when unambiguous and threshold-safe.
- Ambiguous/conflicting suggestions create exception cases with reason codes.
- No duplicate side effects on idempotent replay.
- Denominator-safe KPI math including insufficient-data reason codes.
- Benchmark version mismatch handling is deterministic.
- UTC-only date boundary behavior for KPI and trend windows.

### UI/integration
- Settle page renders suggestion cards and confidence/risk badges.
- Ambiguous suggestions route to exceptions-first path.
- Portfolio shows KPI strip + trend strip with explicit `as_of`.
- Primary path remains decision-first; raw edits under `Advanced` only.

### Regression
- `./scripts/test_default.sh` passes.
- `python3 -m unittest -v tests.test_phase2_ui` passes in host-capable environment.
- host smoke remains green (`./scripts/host_ui_smoke.sh 8865`).
- legacy validate/generate remain green.

## Acceptance Criteria

1. Payment suggestions are deterministic, auditable, and idempotent.
2. Ambiguous/unsafe settlement actions are routed to exceptions, not auto-applied.
3. KPI strip metrics are computed with locked formulas, UTC canon, and denominator-safe behavior.
4. SLA/aging trends render with deterministic weekly windows and clear insufficiency signaling.
5. No regressions to legacy flows or Phase invariants.

## Proof Bundle (PR10)

Write under:
- `.state/phase2-proof/pr10/<timestamp>/`

Required artifacts:
- settlement suggestion benchmark report (`accepted/review/blocked`, confidence histogram),
- exception routing/resolution logs for ambiguous payments,
- KPI JSON with full PR10 fields + gate decisions + reason codes,
- trend output for current vs previous week windows,
- regression logs (`test_default`, `phase2_ui`, legacy validate/generate, host smoke),
- provenance (`tested_sha.txt`, `git_status_short.txt`).

## Out of Scope (PR10)

- PR11+ autonomy expansion beyond settlement/KPI strip.
- New legacy command behavior or legacy template changes.
- Broad parser redesign (already addressed in prior PRs).
- Transport/doc completion redesign beyond PR9 behavior.
