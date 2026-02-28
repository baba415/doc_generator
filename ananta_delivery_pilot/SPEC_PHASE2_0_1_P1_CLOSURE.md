# SPEC_PHASE2_0_1_P1_CLOSURE

## Objective
Close the current P1 backlog only (no broad Phase 2 redesign) by implementing:
1) parser-assisted intake with confidence UX,
2) automatic contract state refresh before execution/materialization,
3) deterministic fallback renderer coverage in the default CI test path.

## Scope Lock
- Additive changes only.
- Do **not** change legacy behavior:
  - `python3 run.py generate`
  - `python3 run.py validate`
  - `python3 run.py serve`
- Preserve existing invariants:
  - 4-doc pack order (WB, WT, COA, Invoice),
  - receipt only on `mark-paid`,
  - deterministic `--as-of` exports,
  - KG canonical storage/calculation.

## Out of Scope
- Full OCR pipeline.
- New autonomous “zero-touch” planner redesign.
- Schema rewrites unrelated to intake decisions and refresh safety.

---

## 1) Parser-Assisted Intake with Confidence UX

### Current gap
`/apps/web_v2.py` intake is manual-first; uploads are captured as evidence but not parsed into structured field proposals.

### Target behavior
When user uploads LPO on `/v2/intake`:
1. Parse supported known formats (parser-assisted, not OCR magic).
2. Build field proposals with confidence + source metadata.
3. Auto-apply high-confidence fields.
4. Route low-confidence fields to exception queue.
5. Show prefilled intake review screen with confidence badges.
6. Persist decision traces for:
   - auto-applied fields,
   - user-confirmed fields,
   - user-corrected fields.
7. On confirm, run existing `create-contract` + `plan-deliveries`.

### Parser adapter design
Add new module:
- `adapters/lpo_parser.py`

Data contract:
- `ParsedField`:
  - `field_name`
  - `proposed_value`
  - `confidence` (0..1)
  - `source_type` (`parser_json` | `parser_text` | `filename_hint` | `registry_match`)
  - `source_ref` (file path + parser rule id)
  - `reason_code`
- `ParsedLpoResult`:
  - `fields: list[ParsedField]`
  - `raw_extract_summary`
  - `parser_version`

Supported inputs (v1):
- `.json` (known key schema from prior examples),
- `.txt` (key-value/text rule extraction),
- `.pdf` with selectable text only (best-effort extraction via existing installed libs if available).
- If unsupported/unparsable: return low-confidence proposals and route to exception review.

### Field set for proposals
Minimum:
- `lpo_no`, `lpo_date`, `issue_date`, `lpo_valid_from`, `lpo_valid_to`,
- `buyer_id`, `vendor_of_record_id`, `source_id`, `processor_id`,
- `product_code`, `expected_qty_mt`, `unit_price`, `currency`.

### Confidence policy
Reuse thresholds from `config/automation_thresholds.json`:
- `identity_auto_apply_min`,
- `identity_review_min`.

Add intake-specific defaults (if absent) under a new optional block:
- `intake.field_auto_apply_min` (default `0.90`),
- `intake.field_review_min` (default `0.70`).

Decision outcomes:
- `auto_applied`
- `needs_review`
- `user_confirmed`
- `user_corrected`

### Critical-field severity lock
Exception severity for intake parser is fixed as:
- `BLOCKER` fields:
  - `lpo_no`
  - `buyer_id`
  - `vendor_of_record_id`
  - `product_code`
  - `expected_qty_mt`
  - `unit_price`
- All other intake fields use `REVIEW`.

### Decision trace + exception persistence
Do not introduce a separate decision system. Reuse existing automation audit tables:
- `automation_runs`
- `automation_decisions`
- `exception_queue`

Intake parser flow must create an intake run record (`run_id`) and persist all decisions.
Low-confidence fields must create `exception_queue` rows with:
- stage `intake_parser`,
- severity `REVIEW` or `BLOCKER` based on field criticality,
- reason + suggested options.

### Parse idempotency lock
- `intake/parse` idempotency key is:
  - `file_sha256 + parser_version`
- Re-running parse on identical file content and parser version:
  - must reuse existing decisions/exceptions for that parse context,
  - must not create duplicate `exception_queue` rows.
- Dedupe key for intake exceptions:
  - `(run_id, stage, field_name, exception_type, reason)`.

### Parse-stage normalization lock
- Parser proposals must normalize quantity/pricing into KG-canonical model before confirm:
  - persist canonical quantity as `expected_qty_kg` integer equivalent,
  - keep `expected_qty_mt` as display-only derived value for intake review.
- Price normalization must include `unit_price_basis` resolution (`KG` or `MT`) before contract creation.
- Confirm payload must use canonical normalized values to avoid post-confirm unit drift.

### UI changes
Extend `/apps/web_v2.py` only:
- Add POST parse endpoint:
  - `/v2/intake/parse`
- Add POST confirm endpoint:
  - `/v2/intake/confirm`
- Keep `/v2/intake` GET as entry point.

Flow:
1. Upload + Parse (`/v2/intake/parse`)
2. Review prefilled form with badges:
   - High confidence = prefilled + marked auto-applied
   - Low confidence = highlighted, requires confirm/correction
3. Confirm (`/v2/intake/confirm`) creates contract + plan and writes final decisions.

---

## 2) Automatic Expiry/State Refresh Before Execution

### Current gap
Execute/materialize paths rely on stored `lpo_state` and can miss stale state unless refresh was called separately.

### Target behavior
Always refresh contract state (`as_of=today`) before execute/materialize actions.
No stale-state materialization.

### Date boundary lock
- Use UTC date semantics consistently for refresh comparisons.
- `materialize_due_deliveries(as_of_date=X)` must refresh with exactly `X` before any selection/materialization.
- `materialize_delivery(...)` (single row) must refresh using UTC `today` unless explicit `as_of_date` is provided by caller.

### Service-path requirements
In `domain/services.py`:
- `materialize_delivery(...)`:
  - refresh state before checking `lpo_state`.
- `materialize_due_deliveries(...)`:
  - refresh state with the exact provided `as_of_date` before selecting due rows.
- Any execution helper called by UI execute actions must pass through refreshed state.

### UI-path requirements
In `apps/web_v2.py`, before:
- `_handle_execute_materialize_due`
- `_handle_execute_materialize_one`

call `refresh_contract_state(as_of_date=today)` (or form `as_of_date` when supplied), then continue.

If refreshed state is `EXPIRED` or `CANCELLED`, block with clear UI error + exception entry.

### Automation-path requirement
In `domain/automation.py`:
- At start of `auto_run(...)`, before stage execution:
  - call `phase1.refresh_contract_state(as_of_date=as_of_date)`.

If contract becomes non-active after refresh, execution/materialization stages must block deterministically.

---

## 3) Fallback Renderer Coverage in Default CI Path

### Current goal
Ensure fallback behavior is always exercised in default test path.

### Requirements
1. Keep deterministic tests in `tests/test_html_pdf_fallback.py`:
   - Playwright failure => fallback success (PDF bytes returned),
   - fallback failure => structured error containing both primary and fallback contexts.
2. Ensure default test command path includes this file:
   - `python3 -m unittest discover -s tests -p "test_*.py"`
3. Ensure acceptance runner path also includes fallback tests:
   - `scripts/phase1_acceptance.sh` must execute the default discover command (already true; keep it explicit in comments/log output).

### Canonical default test entrypoint lock
- Add `scripts/test_default.sh` as the single canonical default test command.
- `scripts/test_default.sh` must run:
  - `python3 -m unittest discover -s tests -p "test_*.py"`
- `README.md` and proof instructions must reference `scripts/test_default.sh` as the default CI/local gate.

### CI contract
If no dedicated CI workflow file is present in repo, “default CI path” is defined as:
- `scripts/test_default.sh`,
- acceptance script test command.

This spec requires both to continue including `tests/test_html_pdf_fallback.py`.

---

## Files Expected to Change (Scoped)
- `adapters/lpo_parser.py` (new)
- `domain/automation.py` (refresh-on-start + intake decision persistence support if required)
- `domain/services.py` (refresh guards in execution/materialization paths)
- `apps/web_v2.py` (intake parse/confirm UX + execute refresh hooks)
- `tests/test_phase2_ui.py` (new parser intake and stale-state blocking cases)
- `tests/test_phase15_automation.py` (automation refresh blocking case)
- `tests/test_html_pdf_fallback.py` (retain/adjust deterministic assertions)
- `README.md` (intake parser flow + test path note)
- optional tiny repo helper for parser fixtures:
  - `examples/intake/` test inputs

No legacy generator files should be modified.

---

## Test Plan

### A) Intake parser flow
1. Known-format LPO upload pre-populates:
   - buyer/vendor/product/qty/price/date fields.
2. At least one low-confidence case:
   - creates exception row with clear reason.
3. Decision trace:
   - one row per auto-applied or user-confirmed/corrected field.

Suggested tests:
- `tests.test_phase2_ui::test_intake_parse_prefill_known_format`
- `tests.test_phase2_ui::test_intake_low_confidence_routes_exception`
- `tests.test_phase2_ui::test_intake_confirm_persists_decision_trace`

### B) State refresh safety
1. Expired contract by date:
   - materialize/execute blocked after refresh.
2. Automation run:
   - refresh at run start causes deterministic block for expired contract.

Suggested tests:
- `tests.test_phase16_planning::test_materialization_blocked_after_auto_refresh_expired`
- `tests.test_phase15_automation::test_auto_run_blocks_expired_contract_after_refresh`

### C) Fallback CI coverage
Must pass:
- `tests.test_html_pdf_fallback::test_render_uses_fallback_when_playwright_fails`
- `tests.test_html_pdf_fallback::test_render_raises_structured_error_when_primary_and_fallback_fail`

### D) Non-regression
- Existing phase test suites pass.
- Legacy smoke commands still pass:
  - `python3 run.py validate --transaction data/sample_transaction_contract_processing.json`
  - `python3 run.py generate --transaction data/sample_transaction_contract_processing.json`

---

## Proof Bundle Contract
Write proof artifacts to:
- `.state/phase2_0_1-proof/<timestamp>/`

Required artifacts:
- `parser_intake_prefill.json` (or HTML snapshot + parsed field JSON),
- `parser_low_confidence_exception.json`,
- `decision_trace_rows.json`,
- `stale_state_block_ui.json`,
- `stale_state_block_automation.json`,
- `db_intake_decisions_rows.json`,
- `db_intake_exception_rows.json`,
- `db_refreshed_contract_state_before_block.json`,
- `fallback_tests.log`,
- `regression_tests.log`,
- `legacy_validate.json`,
- `legacy_generate.json`.

---

## Stop Condition
After implementation and test/proof run:
- Report only P0/P1 remaining issues,
- stop (no additional autonomy redesign in this phase).
