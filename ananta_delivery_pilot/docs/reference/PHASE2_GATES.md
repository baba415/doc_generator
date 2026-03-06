# PHASE2_GATES

FINAL AMENDMENT BLOCK (MANDATORY BEFORE PR6)

1) KPI formula contract (SQL-level, no interpretation drift)
- touchless_rate = completed_deliveries_without_manual_decisions / completed_deliveries
- manual_inputs_per_delivery = (human_decisions + user_field_overrides) / completed_deliveries
- exception_resolution_time_hours = resolved_at_utc - created_at_utc (report p50/p95)
- first_time_lpo_to_pack_minutes = first_pack_generated_at_utc - intake_confirmed_at_utc
- auto_action_success_rate = successful_action_executions / attempted_action_executions
- All KPI queries require explicit as_of_date and fixed lookback window.

2) Timezone canon
- All SLA/KPI/event timestamps are canonicalized and computed in UTC only.
- UI may display local time, but persistence + metrics math remains UTC.

3) Versioned benchmark fixtures
- Introduce benchmark_version for fixture packs and KPI runs.
- KPI comparisons across PRs are valid only when benchmark_version matches.

4) Fixed benchmark dataset + seed scripts
- Add deterministic fixture seed command.
- Add deterministic KPI benchmark run command that outputs reproducible metrics.

5) Stop/Go authority + waiver policy
- Gate owners: Product + Ops + Engineering.
- Failed gate blocks promotion unless explicit waiver is logged.
- Waiver must include reason, expiry date, and fallback/feature-flag plan.

6) Run-All safety contract
- Mandatory dry-run preview before execute.
- Hard caps: max_contracts_per_run and max_actions_per_run.
- Skip blocked/ambiguous contracts only; continue safe contracts.
- Emit explicit per-contract skip reason codes.

7) RACI + SLA timers in SPEC_PHASE2_EXEC_CONTROL.md
- BLOCKER SLA: 4h
- REVIEW SLA: 24h
- INFO SLA: 72h
- Owner, approver, override authority, and financial/SLA accountability must be explicit per exception type.

8) Primary-path guardrail
- No raw edit forms on primary daily workspace.
- Raw/state-machine edits remain under Advanced only (audit/recovery path).

9) CI gate enforcement
- release-candidate-host-smoke must be required for RC branches.
- Release note must include CI run ID, URL, conclusion, and artifact name.

10) Performance budget starts in PR6
- Define p95 targets for page render and core query latency on benchmark fixtures.
- PR fails if performance regresses beyond agreed threshold.

## Concrete Defaults

- lookback_window_days = 30 (unless KPI report overrides it)
- max_contracts_per_run = 20
- max_actions_per_run = 200

## PR6–PR10 Gate Matrix

| PR | Owner | Metric Threshold | Pass/Fail Rule | Waiver Authority |
|---|---|---|---|---|
| PR6 (Canonical Daily Workspace) | Product + Ops + Engineering | `portfolio_action_start_rate >= 0.80` on benchmark_version-matched fixture run | Pass only if >=80% of daily actions are initiated from `/v2/portfolio` and no non-negotiable regression in legacy/default suites; otherwise fail | Product + Ops + Engineering (all three required), with logged reason + expiry + fallback plan |
| PR7 (Exception-First + Explainability) | Product + Ops + Engineering | `manual_interactions_in_exceptions_rate >= 0.80` | Pass only if >=80% of manual interactions occur in `/v2/exceptions` and operators can complete blocker resolution from exception cards + resume path; otherwise fail | Product + Ops + Engineering (all three required), with logged reason + expiry + fallback plan |
| PR8 (Intake + Planning Copilot) | Product + Ops + Engineering | `median_manual_fields_per_intake < 6` and `autoplan_zero_edit_common_case = true` | Pass only if median manual fields <6 and 150MT@30MT common case auto-plans with zero edits under fixed benchmark_version; otherwise fail | Product + Ops + Engineering (all three required), with logged reason + expiry + fallback plan |
| PR9 (Transport + Document Completion Copilot) | Product + Ops + Engineering | `manual_transport_fields_per_delivery < 3` and `doc_autolink_precision >= 0.90` | Pass only if both thresholds are met on benchmark fixtures and snapshot immutability tests pass; otherwise fail | Product + Ops + Engineering (all three required), with logged reason + expiry + fallback plan |
| PR10 (Settlement Copilot + KPI/SLA Strip) | Product + Ops + Engineering | `payment_suggestion_acceptance_rate >= 0.70` and `exception_aging_trend = stable_or_improving` over 2 weekly cycles | Pass only if acceptance >=70%, ambiguity routes to exceptions deterministically, and exception aging trend is stable/improving; otherwise fail | Product + Ops + Engineering (all three required), with logged reason + expiry + fallback plan |
