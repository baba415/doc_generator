# SPEC_PHASE1_5

## Scope
Phase 1.5 adds automation-first STP on top of Phase 1 without rewriting core modules.

## Invariants
- Legacy commands unchanged: `generate`, `validate`, `serve`.
- Phase 1 commands and DB canon remain source of truth.
- Pack semantics unchanged: `WB`, `WT`, `COA`, `INVOICE`; receipt only via `mark-paid`.
- No silent override: all inferred values are written to decision trace.

## New Tables
- `automation_runs`
- `automation_decisions`
- `exception_queue`

## CLI
- `python3 run.py auto-run --input <json> --as-of YYYY-MM-DD [--dry-run]`
- `python3 run.py exceptions list [--run-id <id>]`
- `python3 run.py exceptions resolve --exception-id <id> --value <value> --note <text>`
- `python3 run.py auto-resume --run-id <id>`

## Orchestration Stages
1. `entity_resolution`
2. `contract_create`
3. `evidence_intake`
4. `delivery_create`
5. `coa`
6. `generate_pack`
7. `payment`
8. `export`

## Critical-Step Matrix
- Buyer identity unresolved: BLOCKER.
- Vendor-of-record unresolved or unsupported lane vendor: BLOCKER.
- Source/processor unresolved: REVIEW exception, continue.
- Missing COA required rows: BLOCKER for pack.
- Ambiguous payment allocation: REVIEW exception.

## Confidence Thresholds
Loaded from `config/automation_thresholds.json`:
- `identity_auto_apply_min`
- `identity_review_min`
- `payment_auto_apply_min`

## Over-Delivery Policy Hierarchy
1. Contract override (`over_delivery_tolerance_pct`)
2. Buyer override (`automation_thresholds.over_delivery.buyer_overrides`)
3. Global default (`5.0`)

## Dry-Run Contract
- Does not write business tables (`contracts`, `deliveries`, `sales_transactions`, `documents`, `payments`).
- Writes automation diagnostics (`automation_runs`, `automation_decisions`, `exception_queue`).
- Returns deterministic projected IDs and stage outputs.

## Idempotency
- `auto-run`: idempotency hash from normalized input + `as_of` + `dry_run` + resume source.
- `auto-resume`: replays with resolved exception values merged into original input.

## KPI Definitions
- `stp_rate = runs_completed_without_manual_intervention / total_runs`
- `auto_population_rate = auto_applied_required_fields / total_required_fields`
- `manual_interventions_count = open exception count for run`
- `exception_count_by_type = grouped open exceptions`
- `end_to_end_duration_seconds = run completion minus start`
