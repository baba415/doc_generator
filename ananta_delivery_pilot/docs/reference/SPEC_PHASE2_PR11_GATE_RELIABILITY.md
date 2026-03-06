# SPEC_PHASE2_PR11_GATE_RELIABILITY

Status: Step 1 specification lock only (no implementation in this step).

## Objective

Implement PR11 as **gate reliability closure**:
- make PR8/PR9/PR10 stop/go metrics reproducible from deterministic fixtures,
- remove ambiguity between "insufficient data" and true threshold failure,
- produce auditable promotion artifacts for Product/Ops/Engineering gate decisions.

PR11 is reliability/operations hardening only. It does **not** redesign core Phase 1–2 workflow primitives.

## Hard Invariants (must remain unchanged)

- Legacy commands unchanged: `python3 run.py generate|validate|serve`.
- Lane invariants A/B unchanged.
- Pack semantics unchanged: exactly 4 docs in order (WB, WT, COA, Goods Invoice); receipt only on `mark-paid`.
- KG integer canonical storage; MT display only.
- Deterministic `--as-of` behavior.
- UTC canon for persisted timestamps and KPI/SLA math.
- Evidence originals immutable (`sha256 + captured_at + stored_path`).
- Additive-only evolution; no destructive migrations.

## PR11 Scope

### 1) Deterministic benchmark dataset runtime

Add deterministic benchmark generation and execution paths for gate metrics:

- Seed command (required):
  - `python3 run.py seed-phase2-benchmark --as-of YYYY-MM-DD --benchmark-version <version> [--reset]`
- Run command (required):
  - `python3 run.py run-phase2-benchmark --as-of YYYY-MM-DD --lookback-window-days N --benchmark-version <version> --out-dir <dir>`

Rules:
- Seeded fixtures must generate enough settlement suggestion decisions to evaluate `payment_suggestion_acceptance_rate` without denominator zero.
- Seeding must be idempotent per `(as_of_date, benchmark_version)`.
- If `--reset` is omitted, reruns reuse existing fixture set deterministically.
- Fixture metadata must persist:
  - `benchmark_version`
  - `seeded_at_utc`
  - `fixture_counts` (contracts, deliveries, suggestion_events, decisions).

### 2) Gate report contract (promotion-ready)

Add a single gate report output artifact:

- `phase2_gate_report_<as_of_date>.json`
- `phase2_gate_report_<as_of_date>.md`

Required sections:
- Inputs:
  - `as_of_date`
  - `lookback_window_days`
  - `benchmark_version`
  - `generated_at_utc`
- PR8 gate snapshot:
  - pass/fail
  - reason_code
  - metrics used
- PR9 gate snapshot:
  - pass/fail
  - reason_code
  - metrics used
- PR10 gate snapshot:
  - pass/fail
  - reason_code
  - metrics used
- Aggregate recommendation:
  - `promotion_recommendation` = `PASS | FAIL | WAIVED`
  - `blocking_reasons[]`
  - `waiver_refs[]` (if any).

### 3) Waiver policy storage and validation

Implement waiver registry and validation:

- Waiver file path:
  - `.state/release-readiness/phase2_gate_waivers.json`
- Required waiver fields:
  - `waiver_id`
  - `gate_name` (`pr8|pr9|pr10`)
  - `reason`
  - `owner_product`
  - `owner_ops`
  - `owner_engineering`
  - `created_at_utc`
  - `expires_at_utc`
  - `fallback_plan`
  - `active` (bool)

Waiver rules:
- Waiver is valid only if all owners are non-empty and `expires_at_utc` > `generated_at_utc`.
- Expired or malformed waivers must be ignored and surfaced as invalid waiver findings.
- `promotion_recommendation=WAIVED` only if all failed gates have valid active waivers.

### 4) Portfolio gate-health panel (read-only)

Extend `/v2/portfolio` with a compact gate-health strip (read-only):

- Inputs:
  - `as_of`
  - `lookback_window_days`
  - `benchmark_version`
- Display:
  - PR8/PR9/PR10 pass/fail badges,
  - reason codes,
  - waiver state (`none|active|expired|invalid`),
  - link to latest gate report artifact path.

Primary-path guardrail:
- No raw edit controls in the portfolio gate-health panel.
- Waiver edits remain out of primary path (ops/release tooling only).

## Data / Model Contract

Prefer existing tables and event streams.

Allowed additive data only if strictly required:
- optional small helper table for benchmark run metadata, e.g.:
  - `benchmark_runs(benchmark_run_id, benchmark_version, as_of_date, lookback_window_days, generated_at_utc, payload_json)`
- optional additive indexes for benchmark query hot paths.

No broad new base-table families.

## Route / Service / CLI Contracts

### Service contracts
- `seed_phase2_benchmark(as_of_date, benchmark_version, reset=False) -> {ok, benchmark_run_id, fixture_counts, seeded_at_utc}`
- `run_phase2_benchmark(as_of_date, lookback_window_days, benchmark_version, out_dir) -> {ok, report_json_path, report_md_path, promotion_recommendation}`
- `phase2_gate_report(as_of_date, lookback_window_days, benchmark_version, waivers_path=None) -> dict`

### CLI contracts
- `seed-phase2-benchmark --as-of YYYY-MM-DD --benchmark-version phase2.pr11.v1 [--reset]`
- `run-phase2-benchmark --as-of YYYY-MM-DD --lookback-window-days 30 --benchmark-version phase2.pr11.v1 --out-dir <dir>`
- `phase2-gate-report --as-of YYYY-MM-DD --lookback-window-days 30 --benchmark-version phase2.pr11.v1 [--waivers <path>] --out-dir <dir>`

### UI contract
- `/v2/portfolio?as_of=YYYY-MM-DD&lookback_window_days=30&benchmark_version=phase2.pr11.v1`
  - includes gate-health strip populated from gate report service output.

## Determinism and UTC Contract

- All benchmark/report timestamps are generated and compared in UTC.
- No `TODAY()` semantics anywhere in KPI/gate/report queries.
- Date windows must use explicit ISO UTC dates passed by caller.
- Re-running report with same `(as_of_date, lookback_window_days, benchmark_version)` and unchanged DB must produce identical JSON payload values except `generated_at_utc`.

## Acceptance Tests (required)

### Service/CLI
1. Seed benchmark is idempotent for same version/as_of without reset.
2. Benchmark run produces non-zero denominator for PR10 settlement acceptance metric.
3. Gate report marks insufficient-data reasons distinctly from threshold-failure reasons.
4. Waiver validation:
   - valid waiver converts failed gate to `WAIVED` recommendation.
   - expired waiver does not.
5. Gate report determinism:
   - repeated runs produce same gate outcomes for same inputs.

### UI
6. Portfolio gate-health strip renders PR8/PR9/PR10 state + reason + waiver state.
7. No raw edit controls appear in gate-health panel.

### Regression
8. `./scripts/test_default.sh` passes.
9. `python3 -m unittest -v tests.test_phase2_ui` passes in host-capable environment.
10. `python3 run.py validate --transaction data/sample_transaction_contract_processing.json` passes.
11. `python3 run.py generate --transaction data/sample_transaction_contract_processing.json` passes.
12. `./scripts/host_ui_smoke.sh 8865` passes in host-capable environment.

## Stop/Go Gates (PR11)

- PR11 gate passes only if:
  - deterministic benchmark/report commands succeed,
  - PR10 settlement acceptance gate can be evaluated from seeded benchmark data (non-null denominator),
  - waiver resolution logic is explicit/auditable.
- Promotion blocked on failed gate unless valid waiver exists per `PHASE2_GATES.md`.

## Proof Bundle Contract

Write under:
- `.state/phase2-proof/pr11/<timestamp>/`

Required artifacts:
- benchmark seed output JSON,
- benchmark run output JSON,
- gate report JSON + MD,
- waiver validation sample outputs,
- portfolio gate-health render capture,
- regression logs (`test_default`, `phase2_ui`, legacy validate/generate, host smoke),
- provenance (`tested_sha.txt`, `git_status_short.txt`).

## Out of Scope (PR11)

- PR12+ autonomy expansion, model-based policy tuning, or ML training loops.
- Settlement algorithm redesign beyond PR10 contracts.
- New field/transport/intake capability scope beyond reliability metrics and gate ops.
- Any legacy flow behavior changes.
