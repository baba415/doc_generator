# AUTOMATION_RUNBOOK

## Input Contract
Use JSON input for `auto-run`.
Recommended fields:
- Identity: `buyer_id|buyer_name`, `vendor_of_record_id|vendor_of_record_name`, `source_id|source_name`, `processor_id|processor_name`
- Contract: `lpo_no`, `lpo_date`, `issue_date`, `due_date`, `product_code`, `expected_qty`, `unit_price`, `unit`
- Delivery: `delivery_date`, `run_id`, `batch_id`, `truck_no`, `driver_name`, `driver_phone`
- COA: `coa_results[]`
- Payment (optional): `payment{amount_received,payment_date,payment_method,external_reference,...}`
- Evidence: `evidence_files[]`

## Confidence Thresholds
Configured in `config/automation_thresholds.json`.
- High confidence => `auto_applied`
- Mid confidence => `needs_review`
- Low confidence => `blocked`

## Exception Taxonomy
- `identity_resolution`
- `unsupported_vendor_of_record`
- `missing_lpo`
- `missing_product_code`
- `missing_coa_results`
- `coa_validation_failed`
- `pack_generation_blocked`
- `ambiguous_payment_allocation`
- `runtime_error`

## Resume Flow
1. `python3 run.py exceptions list --run-id <run_id>`
2. Resolve each needed exception:
   - `python3 run.py exceptions resolve --exception-id <id> --value <value> --note "..."`
3. Resume:
   - `python3 run.py auto-resume --run-id <run_id>`

## KPI Review
Each `auto-run` returns:
- `stp_rate`
- `auto_population_rate`
- `manual_interventions_count`
- `exception_count_by_type`
- `end_to_end_duration_seconds`

## Benchmarks
Use files under `examples/stp/`:
- `known_complete.json`
- `known_partial.json`
- `unknown_entities.json`
