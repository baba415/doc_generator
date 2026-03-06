# SPEC_PHASE2_PR9_TRANSPORT_DOCS

Status: Step 1 specification lock only (no implementation in this step).

## Objective

Implement PR9 as transport + document completion copilot:
- reduce repetitive transport data entry during execution,
- auto-link uploaded originals to delivery/sales context with confidence gates,
- keep manual work concentrated in exception decisions.

## Hard Invariants (must remain unchanged)

- Legacy commands unchanged: `python3 run.py generate|validate|serve`.
- Lane invariants A/B unchanged.
- Pack semantics unchanged: exactly 4 docs in order (WB, WT, COA, Goods Invoice); receipt only on `mark-paid`.
- KG integer canonical storage; MT display only.
- Deterministic `--as-of` behavior.
- UTC canon for persisted timestamps and KPI/SLA calculations.
- Evidence immutability + hash/audit trail.
- No broad schema redesign; additive changes only if strictly blocked.
- Prefer existing PR1–PR8 tables/views; no base-table sprawl.

## Carry-Forward Requirement (from PR8.1)

Before PR9 is accepted, the PR8 gate reason behavior must be explicit:
- If PR8 intake-confirm data is absent in the lookback window, set:
  - `pr8_gate_pass=false`
  - `pr8_gate_reason_code="insufficient_intake_data"`
- Do not report threshold-failure reason codes for true no-data conditions.

## PR9 Scope

### 1) Transport Copilot (execution-time assist)

Primary behavior:
- Use assignment history, aliases, and compliance state to suggest transport partner/truck/driver.
- Auto-apply suggestion only when confidence `>= transport_auto_min` and compliance valid.
- Low-confidence/conflict/compliance-expired must create exception cases (no silent apply).

Execution contract:
- Selection of truck or driver should suggest the counterpart fields (driver/truck/phone/partner).
- Suggestions must include explanation payload (score + reason bits + source signal references).
- Applied values must be snapshotted via `delivery_transport_snapshot`; documents read snapshot first.

Safety:
- Snapshot is immutable operational evidence.
- Do not rewrite historical snapshot rows when master transport records change.

### 2) Document Completion Copilot (evidence linking)

Primary behavior:
- Ingest uploaded originals, classify by doc type, hash + dedupe, and propose link targets.
- Link evidence to `delivery_id`, `sales_transaction_id`, `sales_line_id`, and contract where determinable.
- If confidence is ambiguous/conflicting, route to exception queue with proposed options.

Doc-link confidence classes (PR9):
- `strong`: fingerprint + entity/run/batch alignment => auto-link
- `partial`: missing one critical discriminator => `REVIEW` exception
- `ambiguous/conflict`: multiple plausible targets => `BLOCKER` exception

Deterministic requirements:
- Same evidence hash and same context replay should yield same link proposal and decision.
- Re-run must be idempotent (no duplicate link rows/events).

### 3) Missing-doc prompting

At execution/settlement surfaces:
- Show unresolved required docs per delivery/sales line.
- Prompt only unresolved/missing items.
- Avoid forcing operators through raw forms when high-confidence auto-link is available.

### 4) UI contract for PR9

Primary surfaces:
- `/v2/contracts/{id}/execute`: transport suggestion panel + evidence auto-link status.
- `/v2/exceptions`: transport/doc-link decision cards with approve/reject/override + reason.

Guardrail:
- Primary path remains action/decision oriented.
- Raw edit controls remain under `Advanced` only.

## Data/API Contracts (PR9)

Use existing structures first:
- `delivery_transport_suggestions`
- `delivery_transport_snapshot`
- `transport_*` master/alias/compliance tables
- `evidence_originals`
- `document_sales_links`
- `exception_cases`, `human_decisions`, `event_log`

Allowed additive changes only if blocked:
- helper view(s) for PR9 KPI aggregation,
- additive indexes for hot linking/suggestion queries.

No destructive migrations.

## KPI and Stop/Go Gate (PR9)

From `PHASE2_GATES.md`:
- `manual_transport_fields_per_delivery < 3`
- `doc_autolink_precision >= 0.90` (benchmark fixtures)

Metric definitions (deterministic):
- `manual_transport_fields_per_delivery = manual_transport_field_updates / deliveries_with_transport_assignment`
- `doc_autolink_precision = true_positive_autolinks / (true_positive_autolinks + false_positive_autolinks)`

Evaluation constraints:
- explicit `as_of_date` required,
- `lookback_window_days = 30` default unless override,
- `benchmark_version` must match for cross-PR comparisons,
- UTC-only timestamp math.

Stop/Go:
- fail if either threshold fails,
- failed gate blocks promotion unless Product + Ops + Engineering waiver with expiry + fallback plan.

## Tests (required for PR9)

### Service/domain
- transport suggestion auto-apply when confidence/compliance pass,
- conflict and compliance-expired produce deterministic exception cases,
- snapshot immutability preserved across re-runs and master-data edits,
- evidence hash dedupe idempotency for repeated upload/link attempts,
- deterministic auto-link routing: strong/partial/ambiguous cases.

### Integration/UI
- execute page shows transport suggestion + link status without raw-edit-first UX,
- exception decision flow resolves and resumes transport/doc blockers,
- missing-doc prompts surface only unresolved required docs.

### Regression
- `./scripts/test_default.sh` passes,
- `python3 -m unittest -v tests.test_phase2_ui` passes in host-capable environment,
- host smoke remains green (`./scripts/host_ui_smoke.sh 8865`),
- legacy validate/generate remain green.

## Acceptance Criteria

1. Manual transport entry average is below 3 fields per delivery on benchmark fixtures.
2. Auto-link precision is at least 0.90 on fixed benchmark fixtures.
3. Transport snapshot evidence is immutable and used in document generation.
4. Ambiguous/conflicting transport/doc-link scenarios always route to exceptions with reason codes.
5. Carry-forward no-data reason-code fix (`insufficient_intake_data`) is implemented.
6. Legacy and phase invariants remain unchanged.

## Proof Bundle (PR9)

Write under:
- `.state/phase2-proof/pr9/<timestamp>/`

Required artifacts:
- transport suggestion outcomes + confidence distributions,
- transport exception/resolution logs,
- evidence auto-link benchmark report (TP/FP counts + precision),
- snapshot immutability proof,
- KPI output + gate pass/fail status,
- regression logs (`test_default`, `phase2_ui`, legacy validate/generate, host smoke).

## Out of Scope (PR9)

- PR10 settlement copilot and KPI strip expansion.
- Broader autonomy redesign outside transport/doc completion.
