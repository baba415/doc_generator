# SPEC_PHASE2_PR16_OPERATOR_PLAYBOOKS

Status: Step 1 specification lock only (no implementation in this step).

## Objective

Implement PR16 as **operator playbook orchestration** on top of PR13/PR14/PR15:
- convert recurring drift/root-cause signals into deterministic, auditable guidance,
- surface only read-only playbook recommendations in UI/CLI,
- keep all corrective actions human-driven through existing exception workflows.

PR16 is guidance-only. It must not mutate policy, thresholds, waivers, or operational state.

---

## Hard Constraints (non-negotiable)

1. Spec-only in this step. No runtime code/tests/config/scripts modifications.
2. No changes to:
   - PR8/PR9/PR10 formulas,
   - PR13 drift precedence/state logic,
   - PR14 triage mapping/lifecycle,
   - PR15 root-cause diagnosis/ranking logic.
3. No policy mutation, threshold mutation, auto-waiver, or auto-remediation.
4. Preserve invariants:
   - legacy commands unchanged (`run.py generate|validate|serve`),
   - lane/pack/receipt invariants unchanged,
   - KG canonical storage, MT display,
   - UTC/as-of determinism,
   - evidence immutability + audit trail.
5. Schema discipline:
   - prefer read-only artifacts/event payloads/views,
   - no new base table unless implementation is blocked and justified.

---

## Scope Boundaries

### In scope
- Deterministic mapping of `root_cause_code` to playbook recommendations.
- Read-only operator playbook report artifact generation.
- Portfolio read-only playbook panel + drift exceptions deep-link.
- CLI export command for playbook report generation.

### Out of scope
- Executing any remediation step from playbooks.
- Editing policy sets, thresholds, contracts, deliveries, or decisions.
- Auto-resolving/opening exception cases from playbook output.
- PR17+ execution/autonomy features.

---

## Playbook Taxonomy (allowed codes only)

Allowed `playbook_code` values:
- `PB_OBSERVABILITY_RECOVERY`
- `PB_BENCHMARK_ALIGNMENT`
- `PB_INTAKE_QUALITY_STABILIZATION`
- `PB_PLANNING_POLICY_REVIEW`
- `PB_TRANSPORT_ASSIGNMENT_RETRAIN`
- `PB_DOCUMENT_LINKAGE_TUNING`
- `PB_SETTLEMENT_MATCHING_REVIEW`
- `PB_MANUAL_OVERRIDE_REDUCTION`
- `PB_MONITOR_ONLY`

Any playbook output using a non-listed code is invalid.

---

## Root-Cause → Playbook Mapping Contract

Deterministic mapping:

| root_cause_code | primary_playbook | secondary_playbook |
|---|---|---|
| `insufficient_observability_data` | `PB_OBSERVABILITY_RECOVERY` | `PB_MONITOR_ONLY` |
| `benchmark_dataset_misalignment` | `PB_BENCHMARK_ALIGNMENT` | `PB_MONITOR_ONLY` |
| `input_quality_regression` | `PB_INTAKE_QUALITY_STABILIZATION` | `PB_MANUAL_OVERRIDE_REDUCTION` |
| `planning_policy_mismatch` | `PB_PLANNING_POLICY_REVIEW` | `PB_MANUAL_OVERRIDE_REDUCTION` |
| `transport_assignment_instability` | `PB_TRANSPORT_ASSIGNMENT_RETRAIN` | `PB_MANUAL_OVERRIDE_REDUCTION` |
| `document_linkage_instability` | `PB_DOCUMENT_LINKAGE_TUNING` | `PB_MANUAL_OVERRIDE_REDUCTION` |
| `settlement_matching_instability` | `PB_SETTLEMENT_MATCHING_REVIEW` | `PB_MANUAL_OVERRIDE_REDUCTION` |
| `manual_override_concentration` | `PB_MANUAL_OVERRIDE_REDUCTION` | `PB_MONITOR_ONLY` |
| `no_recurring_root_cause` | `PB_MONITOR_ONLY` | _none_ |

---

## Severity / Urgency Classification Contract

Playbook urgency derives from PR15 aggregate state + root-cause code:

1. `URGENT`:
   - aggregate state `ALERT`, or
   - root cause in `{insufficient_observability_data, benchmark_dataset_misalignment}`.
2. `HIGH`:
   - aggregate state `WATCH`,
   - recurring root cause is `true`,
   - and affected contracts >= 2.
3. `MEDIUM`:
   - aggregate state `WATCH` and recurring is `false`.
4. `LOW`:
   - aggregate state `PASS` and root cause `no_recurring_root_cause`.

Deterministic precedence:
`URGENT` > `HIGH` > `MEDIUM` > `LOW`.

---

## Expected Evidence Checklist by Playbook

Required `evidence_checklist` per playbook:

- `PB_OBSERVABILITY_RECOVERY`
  - latest drift report JSON path
  - latest drift triage status snapshot
  - latest autonomy metrics snapshot
  - missing telemetry fields list

- `PB_BENCHMARK_ALIGNMENT`
  - benchmark run id/version
  - drift report benchmark_version
  - live metrics benchmark_version
  - mismatch proof refs

- `PB_INTAKE_QUALITY_STABILIZATION`
  - intake decision distribution snapshot
  - correction-memory hit/miss sample refs
  - affected buyer/product tuples

- `PB_PLANNING_POLICY_REVIEW`
  - lot split outcomes (common-case vs edited)
  - planning-related exception references
  - policy version/source refs

- `PB_TRANSPORT_ASSIGNMENT_RETRAIN`
  - transport suggestion confidence distribution
  - accepted/rejected assignment feedback refs
  - compliance-expiry conflict refs

- `PB_DOCUMENT_LINKAGE_TUNING`
  - auto-link precision snapshot
  - ambiguous/blocked document linkage case refs
  - fingerprint/source filename samples

- `PB_SETTLEMENT_MATCHING_REVIEW`
  - suggestion acceptance snapshot
  - ambiguous allocation case refs
  - payment reference pattern misses

- `PB_MANUAL_OVERRIDE_REDUCTION`
  - override concentration ratios
  - top repeated override reasons
  - corresponding exception case refs

- `PB_MONITOR_ONLY`
  - latest drift + root-cause report refs

If a checklist cannot be completed, playbook `evidence_completeness` must be `PARTIAL` with missing items explicitly listed.

---

## Deterministic Ranking / Selection Rules

### Candidate generation
- Build candidates from PR15 `top_recurring_causes` (max 3), then map to playbooks.
- Always include `PB_MONITOR_ONLY` fallback when no recurring causes exist.

### Ranking score (deterministic)
For each candidate playbook:
- `urgency_weight`: `URGENT=4`, `HIGH=3`, `MEDIUM=2`, `LOW=1`
- `recurrence_weight`: `recurring=true => +2`, else `+0`
- `impact_weight`: `min(affected_contracts, 3)`
- `evidence_penalty`: `-1` if checklist completeness is `PARTIAL`

`score = urgency_weight + recurrence_weight + impact_weight + evidence_penalty`

### Tie-break
If scores equal:
1. higher urgency,
2. higher affected contracts,
3. lexical `playbook_code` ascending.

### Output cap
- Return top 3 playbooks only.

---

## Service Contracts

### `phase2_operator_playbooks(...)`
Signature:
- `phase2_operator_playbooks(as_of_date, lookback_window_days, benchmark_version, out_dir, drift_report_ref=None, triage_status_ref=None, root_cause_report_ref=None) -> dict`

Behavior:
1. Load source reports:
   - PR13 drift report
   - PR14 drift status/summary
   - PR15 root-cause report (generate if not provided)
2. Build deterministic candidate playbooks from mapping contract.
3. Rank + select top playbooks using deterministic score rules.
4. Emit JSON + Markdown artifacts.
5. Append audit event:
   - `PHASE2_OPERATOR_PLAYBOOKS_EXPORTED`
6. Return summary:
   - `aggregate_state`
   - `aggregate_reason_code`
   - `selected_playbooks`
   - `report_json_path`
   - `report_md_path`

No mutation to policies/thresholds/waivers/cases.

### `phase2_operator_playbooks_snapshot(...)`
Read-only snapshot for UI context:
- latest aggregate state/reason,
- top playbooks (max 3),
- latest report refs.

---

## CLI Contract

Additive command:

```bash
python3 run.py phase2-operator-playbooks \
  --as-of 2026-02-28 \
  --lookback-window-days 30 \
  --benchmark-version phase2.pr12.v1 \
  --out-dir .state/phase2-proof/pr16/manual
```

Optional refs:
- `--drift-report <path>`
- `--triage-status <path>`
- `--root-cause-report <path>`

Output:
- summary + artifact refs only (no apply/execute behavior).

---

## UI Contract (read-only)

Portfolio (`/v2/portfolio`) add **Operator Playbooks** panel:
- aggregate state + reason,
- top 3 playbooks (code, urgency, score, evidence completeness),
- report link.

Required deep-link:
- `/v2/exceptions?case_type=DRIFT_MONITORING&status=OPEN`

Guardrail:
- no execute/apply/remediate controls in this panel.
- no button that mutates state.

---

## Output Artifact Contract

### Artifact names
- `phase2_operator_playbooks_<as_of_date>.json`
- `phase2_operator_playbooks_<as_of_date>.md`

### JSON contract (top-level)
- `inputs`
  - `as_of_date`
  - `lookback_window_days`
  - `benchmark_version`
  - `generated_at_utc`
  - `drift_report_ref`
  - `triage_status_ref`
  - `root_cause_report_ref`
- `aggregate`
  - `state`
  - `reason_code`
  - `selection_count`
- `playbooks[]`
  - `playbook_code`
  - `urgency`
  - `score`
  - `root_cause_code`
  - `affected_contracts`
  - `recurring`
  - `evidence_completeness` (`COMPLETE|PARTIAL`)
  - `evidence_checklist[]`
  - `missing_evidence[]`
  - `evidence_refs[]`
- `source_refs`
  - `drift_report`
  - `triage_status`
  - `root_cause_report`

---

## Determinism + UTC Canon

1. All timestamps in artifacts/events are UTC.
2. Same inputs and source refs must produce identical ranking + selection.
3. Missing source refs produce deterministic fallback:
   - aggregate reason `insufficient_observability_data`,
   - primary playbook `PB_OBSERVABILITY_RECOVERY`.

---

## Acceptance Tests (implementation step)

### Service/CLI
1. Mapping contract returns deterministic playbook codes for each root-cause code.
2. Ranking and tie-break are deterministic for identical inputs.
3. Missing root-cause report/ref yields `PB_OBSERVABILITY_RECOVERY` guidance.
4. CLI output matches artifact contract schema.
5. `PHASE2_OPERATOR_PLAYBOOKS_EXPORTED` event appended.

### UI
6. Portfolio shows read-only Operator Playbooks panel.
7. Panel includes drift exceptions deep-link.
8. No execute/apply/edit controls present in panel.

### Regression
9. `./scripts/test_default.sh` passes.
10. `python3 -m unittest -v tests.test_phase2_ui` passes in host-capable env.
11. Legacy `run.py validate` and `run.py generate` pass.
12. `./scripts/host_ui_smoke.sh 8865` passes in host-capable env.

---

## Proof Bundle Contract (implementation step)

Write under:
- `.state/phase2-proof/pr16/<timestamp>/`

Required artifacts:
- `phase2_operator_playbooks_<as_of>.json/.md`
- source refs used (`phase2_drift_report_<as_of>.json`, drift status snapshot, root-cause report)
- regression logs (`test_default.log`, `test_phase2_ui.log`, legacy validate/generate, host smoke)
- provenance (`tested_sha.txt`, `git_status_short.txt`)

---

## Stop/Go Criteria (PR16 completion)

`GO` only if all conditions pass:
1. Playbook selection deterministic across repeated runs with same source refs.
2. All selected playbooks use allowed taxonomy codes only.
3. UI panel remains read-only with no mutation controls.
4. Required regression and legacy checks pass.

`STOP` if any condition fails:
- block promotion,
- capture failure reason in proof bundle,
- no hidden waivers or formula/policy mutations permitted.

---

## Explicit Non-Goals

- No execution of playbooks.
- No change to gate/drift/root-cause formulas or precedence.
- No changes to waiver flow or exception lifecycle.
- No PR17+ scope.
