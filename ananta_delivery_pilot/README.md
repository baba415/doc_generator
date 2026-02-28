# Ananta Delivery Pilot

This repository now has two flows:

1. **Legacy generator flow** (unchanged): `generate`, `validate`, `serve`, `generate-samples`, `entities`.
2. **Phase 1 DREP flow** (new): persistent SQLite ledger + state machine + exports + CLI.

## Legacy Commands (unchanged)

```bash
python3 run.py generate --transaction data/sample_transaction_contract_processing.json
python3 run.py validate --transaction data/sample_transaction_contract_processing.json
python3 run.py serve --host 127.0.0.1 --port 8765
```

Legacy outputs continue to write to `output/`.

## UI Testing (including Phase 1.5 STP)

Run:

```bash
cd /Users/macbookairv2/doc_generator/ananta_delivery_pilot
python3 run.py serve --host 127.0.0.1 --port 8765
```

Open: `http://127.0.0.1:8765`

Use these UI sections:
- `Form Mode`: legacy generation form.
- `JSON Mode`: legacy JSON generation.
- `Phase 1.5 STP (Automation Test)`: new automation panel.

Recommended STP UI test:
1. In `Phase 1.5 STP`, click `Quick Run (Latest + New IDs)`.
2. Set `as_of_date`.
3. Keep `Generate PDFs` checked to produce real documents.
4. Confirm `status=COMPLETED`, `run_id`, and `output_dir` in result.
5. If `status=NEEDS_REVIEW`, open `Advanced STP Controls` for exception list/resolve/resume.

Tip:
- Use `Use Latest + New IDs` to auto-suffix `lpo_no`, `run_id`, `batch_id`, `delivery_ref` (and payment `external_reference` if present) so repeated tests avoid duplicate collisions.

Workstream bundle (current files in one place):
- `current_workstream_2026-02-26/`

## Phase2 Web UI (workflow-first orchestration)

Run:

```bash
python3 run.py serve-v2 --host 127.0.0.1 --port 8865
```

Open:
- `http://127.0.0.1:8865/v2/portfolio` (default landing)
- `http://127.0.0.1:8865/v2/intake`
- `http://127.0.0.1:8865/v2/exceptions`

UI route smoke test:

```bash
PYTHONPYCACHEPREFIX=/tmp/pycache python3 -m unittest -v tests.test_phase2_ui
```

Host-level UI smoke (non-sandbox, non-skipped):

```bash
cd /Users/macbookairv2/doc_generator/ananta_delivery_pilot
./scripts/host_ui_smoke.sh 8865
```

This writes proof artifacts under `.state/phase2-proof/pr5/host-smoke-<timestamp>/`.
Release-candidate CI now runs the same script in `.github/workflows/release-candidate-host-smoke.yml`.

### Primary UI flow

1. **Intake** (`/v2/intake`)  
   Upload LPO for parser-assisted prefill (confidence badges + exception routing), then confirm to create contract and auto-plan deliveries. Manual create path remains available when no LPO is provided.
2. **Plan** (`/v2/contracts/<contract_id>/plan`)  
   Review lot split, edit planned date/qty for exceptions, rebuild schedule if needed.
3. **Execute** (`/v2/contracts/<contract_id>/execute`)  
   Materialize due deliveries (single or bulk), auto-progress to delivered, auto-record COA, auto-generate 4-pack.  
   PR9 adds transport suggestion/status cards and document-completion status with missing-original prompts.
4. **Settle** (`/v2/contracts/<contract_id>/settle`)  
   Mark payment, generate receipt, run DREP export with deterministic `as_of_date`.
5. **Exceptions** (`/v2/exceptions`)  
   Resolve blockers/review exceptions and continue.  
   Decision cards include SLA state, consequence preview, and `Approve + Resume` audit trail.

### Exception-first operations (PR7)

- `/v2/exceptions` is the primary manual workspace.
- Every decision (`APPROVE|REJECT|OVERRIDE`) requires a reason.
- Resume actions emit explicit case audit events:
  - `CASE_RESUME_REQUESTED`
  - `CASE_RESUME_COMPLETED` or `CASE_RESUME_FAILED`
- For blocked runs, use query-scoped navigation:
  - `/v2/exceptions?contract_id=<id>&run_id=<autonomy_run_id>`
  - The page renders an autopilot console timeline plus grouped decision cards.

Phase2 outputs persist to:
- `.state/drep.sqlite`
- `output_v2/<vendor_code>/<invoice_no>/`
- optional combined pack: `output_v2/<vendor_code>/<invoice_no>/PACK-<invoice_no>.pdf`

## Phase 1 Commands

Phase 1 commands are available directly via `run.py`:

```bash
python3 run.py init-db
python3 run.py create-contract --input data/phase1_contract.json --allow-placeholder-tin
python3 run.py add-delivery --input data/phase1_delivery.json
python3 run.py mark-dispatched --delivery-id <delivery_id>
python3 run.py mark-delivered --delivery-id <delivery_id>
python3 run.py record-coa --input data/phase1_coa.json
python3 run.py plan-deliveries --contract-id <contract_id> --start-date 2026-02-23 --cadence daily --max-lots-per-day 1
python3 run.py materialize-delivery --planned-delivery-id <planned_delivery_id>
python3 run.py generate-pack --delivery-id <delivery_id> --allow-placeholder-tin --skip-pdf
python3 run.py mark-paid --input data/phase1_payment.json --allow-placeholder-tin --skip-pdf
python3 run.py cancel-contract --contract-id <contract_id> --reason "cancelled by ops"
python3 run.py close-contract --contract-id <contract_id> --reason "fulfilled"
python3 run.py refresh-contract-state --as-of 2026-03-31
python3 run.py export-drep --as-of 2026-03-31 --out-dir .state/exports/2026-03-31
python3 run.py auto-run --input examples/stp/known_complete.json --as-of 2026-03-31
python3 run.py exceptions list
python3 run.py exceptions resolve --exception-id <id> --value <value> --note "resolution"
python3 run.py auto-resume --run-id <run_id>
python3 run.py run-autonomy --as-of 2026-03-31 --dry-run
python3 run.py list-cases --status OPEN
python3 run.py decide-case --case-id <case_id> --decision APPROVE --reason "override"
python3 run.py autonomy-metrics --as-of 2026-03-31 --lookback-window-days 30 --benchmark-version phase2.pr9.v1 --out-dir .state/automation_metrics
```

Phase 1 state writes to:
- `.state/drep.sqlite`

Phase 1 outputs write to:
- `output_v2/<vendor_code>/<invoice_no>/`

## Phase 1 Business Rules (implemented)

- Lane A: `vendor_of_record_id=guildgate`
- Lane B: `vendor_of_record_id=ananta_flows`
- Pack generation is exactly 4 docs and in order:
  1. Waybill
  2. Weighing Ticket
  3. COA
  4. Goods Invoice
- Receipt is generated only from `mark-paid`.
- Evidence originals are immutable.
- `generate-pack` enforces:
  - vendor TIN present (unless `--allow-placeholder-tin`)
  - run_id and batch_id present
  - delivery status is `DELIVERED`
  - COA required rows are complete
  - over-delivery tolerance checks
- Skip-PDF mode:
  - `--skip-pdf` persists metadata and manifest
  - `pdf_sha256=NULL`
  - deterministic `content_sha256` is still generated.

## Quantity Canon

- Storage and calculations use `*_qty_kg` integer fields.
- UI + documents display MT (`qty_mt = qty_kg / 1000`, 3 dp).
- Lot splitting is deterministic, with remainder assigned to the last lot.
- Price normalization supports `unit_price_basis` (`KG` default, `MT` supported) with deterministic gross computation from KG quantities.

## Reproducible End-to-End Scenario

1) Initialize DB:

```bash
python3 run.py init-db
```

2) Create contract:

```bash
python3 run.py create-contract --input data/phase1_contract.json --allow-placeholder-tin
```

Copy returned `contract_id` into `data/phase1_delivery.json` (`__SET_CONTRACT_ID__`).

3) Add delivery:

```bash
python3 run.py add-delivery --input data/phase1_delivery.json
```

Copy returned `delivery_id` into `data/phase1_coa.json` (`__SET_DELIVERY_ID__`).

4) Progress delivery:

```bash
python3 run.py mark-dispatched --delivery-id <delivery_id>
python3 run.py mark-delivered --delivery-id <delivery_id>
```

5) Record COA:

```bash
python3 run.py record-coa --input data/phase1_coa.json
```

6) Generate pack:

```bash
python3 run.py generate-pack --delivery-id <delivery_id> --allow-placeholder-tin
```

Copy returned `sales_transaction_id` into `data/phase1_payment.json` (`__SET_SALES_TRANSACTION_ID__`).

7) Record payment + receipt:

```bash
python3 run.py mark-paid --input data/phase1_payment.json --allow-placeholder-tin
```

8) Export DREP:

```bash
python3 run.py export-drep --as-of 2026-03-31 --out-dir .state/exports/2026-03-31
```

## DREP Exports

`export-drep` writes:

- `drep_contracts.csv`
- `drep_procurement.csv`
- `drep_sales.csv`
- `drep_sales_lines.csv`
- `drep_outstanding_payments.csv`
- `drep_delivery_plan_status.csv`

`drep_outstanding_payments` is deterministic based on `--as-of` (UTC date boundary), not `TODAY()`.

## Phase 1.5 STP Automation

- Benchmarks: `examples/stp/`
- Thresholds: `config/automation_thresholds.json`
- Spec: `SPEC_PHASE1_5.md`
- Runbook: `AUTOMATION_RUNBOOK.md`

## Phase 2 PR2 Gate + Intent Engine

- CLI autonomy runner: `run-autonomy`
- Case queue and decisions: `list-cases`, `decide-case`
- Deterministic metric export: `autonomy-metrics`
- Persistent tables used: `gate_evaluations`, `action_intents`, `action_executions`, `exception_cases`, `event_log`

## Phase 2 PR8.1 Intake/Planning KPI Closure

`autonomy-metrics` now exports runtime-computed PR8 gates from DB records (not proof-only files), including:
- `median_manual_fields_per_intake`
- `autoplan_zero_edit_common_case_rate`
- `intake_decision_distribution` (`auto_applied`, `needs_review`, `blocked`)
- `pr8_gate_pass` + `pr8_gate_reason_code`
- UTC metadata: `as_of_date`, `lookback_window_days`, `benchmark_version`, `generated_at_utc`

Benchmark-version handling is explicit:
- expected PR8 benchmark is `phase2.pr8.v1`
- mismatch sets `benchmark_version_match_pr8=false` and `pr8_gate_pass=false`

## Phase 2 PR3 Entity Intelligence (Transport + MDM)

- Materialization now applies transport suggestion logic from assignment history + aliases.
- Auto-apply when confidence meets threshold and compliance is valid.
- Creates immutable `delivery_transport_snapshot` records, and document rendering reads snapshot values first.
- Low-confidence/conflict/compliance-expired outcomes open transport exception cases and log decision features/outcomes.
- Key tests: `tests/test_phase2_pr3_transport.py`.

## Phase 2 PR3.1 Policy Runtime Closure

- Runtime policy now resolves from DB tables `policy_sets` + `policy_overrides` with precedence:
  `contract > master_contract > buyer > global` (active/effective-date bounded).
- `plan-deliveries` and autonomy gate evaluation use resolved policy values, with config fallback when no active DB policy exists.
- `action_intents` / `action_executions` payloads now persist policy metadata (`policy_source_key`, `policy_version`, selected policy set IDs) and idempotency keys include the resolved policy reference.
- Key tests: `tests/test_phase2_pr3_1_policy_runtime.py`.

## Recommended Daily Ops Flow (Phase 2 UI)

1. Open `/v2/portfolio` (Command Center).
2. Review `Needs Decision` badges; resolve blockers in `/v2/exceptions`.
3. Click `Run Recommended` per active contract.
4. Review timeline on `/v2/contracts/{id}/execute?run_id=...` for executed/skipped/blocked intents.
5. Confirm materialized deliveries and generate missing 4-doc packs when needed.
6. Go to `/v2/contracts/{id}/settle` to record payment and generate receipt.
7. Export DREP snapshot for a fixed as-of date from Settle (or command-center export step).

## Testing

Run tests:

```bash
./scripts/test_default.sh
```

Focused Phase 2 regression run:

```bash
PYTHONPYCACHEPREFIX=/tmp/pycache python3 -m unittest tests.test_phase16_planning tests.test_phase15_automation tests.test_phase2_ui tests.test_phase1
```

Backend fallback renderer proof:

```bash
PYTHONPYCACHEPREFIX=/tmp/pycache python3 -m unittest -v tests.test_html_pdf_fallback
```

## One-Command Acceptance Run

Run the full Phase 1 acceptance flow (tests, legacy validate smoke, contract → delivery → COA → pack → payment → DREP export):

```bash
./scripts/phase1_acceptance.sh
```

Optional real PDF rendering:

```bash
./scripts/phase1_acceptance.sh --with-pdf --as-of 2026-03-31
```

Artifacts are written to:

- `.state/acceptance-run/<utc_timestamp>/`

## Phase 1.5 Benchmark Run

Run KPI benchmark scenarios from `examples/stp/`:

```bash
./scripts/run_phase15_benchmarks.sh 2026-03-31
```

Outputs:
- `.state/automation/benchmarks/<utc_timestamp>/kpi_report.md`
- `.state/automation/benchmarks/<utc_timestamp>/kpi_report.json`

## Migration Notes

- Legacy commands and outputs are preserved.
- Phase 1 uses isolated storage (`.state/`, `output_v2/`) to avoid collisions with legacy runs.
- Legacy template files are untouched.

## Related Docs

- `SPEC_PHASE1.md`
- `SPEC_PHASE1_5.md`
- `SPEC_PHASE1_6.md`
- `ANANTA_MERGE_NOTES.md`
- `PHASE2_PHASE3_PLAN.md`
