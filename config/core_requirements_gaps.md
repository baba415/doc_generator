# core_requirements_gaps

## 1) Hash Convention Used
- Convention: compute SHA-256 with `core_event_requirements_hash` set to `""`, then write that value back into the JSON.
- Command used: `sha256sum merchant/config/core_event_requirements.json`
- Pre-fill hash value: `55e485fddb9cd1d7ec6f435189b9450c38785b9e2b7782677d292bcbf6441a6c`

## 2) Pilot Fields Core Does Not Strictly Require (Potential UNKNOWN_FIELD Surface)
Sources: `core/src/app/api/events/route.ts`, `core/supabase/migrations/20260210120000_afr1_eventize_milestone_events.sql`, `merchant/ananta_delivery_pilot/domain/rails_truth.py`.

- `DREP_CONTRACT_CREATED -> TERMS_SUBMITTED`: pilot sends many business fields not validated by core (`contract_id`, `lpo_no`, `buyer_id`, `vendor_of_record_id`, `issue_date`, `lpo_valid_from`, `lpo_valid_to`, `expected_total_qty_kg`, `currency`, `unit_price_basis`, `policy_pointers`).
- `DREP_DELIVERY_MATERIALIZED -> SHIPMENT_CREATED`: pilot fields like `planned_delivery_id`, `delivery_id`, `delivery_ref`, `run_id`, `batch_id`, `delivery_date`, `delivered_qty_kg`, `unit_price`, `unit_price_basis` are not required by core shipment-create validation.
- `DREP_DELIVERY_DISPATCHED|DREP_DELIVERY_DELIVERED -> SHIPMENT_DISPATCHED|SHIPMENT_ARRIVED`: pilot lifecycle fields (`delivery_id`, `dispatched_at_utc`, `delivered_at_utc`, `delivered_qty_kg`, etc.) are not core-required.
- `DREP_* -> EVIDENCE_SUBMITTED` aliases: plan/settlement/exception payload objects are mostly opaque to core; core only enforces scope + evidence linkage fields.

## 3) Core-Required Fields Pilot Does Not Natively Provide Today
Sources: `core/src/app/api/events/route.ts`, `core/supabase/migrations/20260210120000_afr1_eventize_milestone_events.sql`, `core/supabase/migrations/20260209113000_afr1_eventize_shipment_create_link.sql`, `merchant/ananta_delivery_pilot/domain/rails_truth.py`.

- Global requirement for every write: top-level `trade_id` (UUID) is required in route handler; pilot mapping is contract-centric (`contract_id`) and does not define a canonical `trade_id` field.
- `TERMS_SUBMITTED` requires `payload.actor_org_id`, `payload.payment_terms`, `payload.delivery_term`, `payload.delivery_location` at DB layer.
- `SHIPMENT_CREATED`, `SHIPMENT_DISPATCHED`, `SHIPMENT_ARRIVED`, `SHIPMENT_ACCEPTED`, `SHIPMENT_REJECTED` require shipment linkage using `payload.shipment_id` (UUID) for milestone events.
- Shipment-scoped evidence aliases (`DREP_COA_RECORDED`, `DREP_PROOF_PACK_GENERATED`) require `payload.shipment_id` and shipment linkage to trade.
- Trade-scoped `EVIDENCE_SUBMITTED` aliases require `payload.url` or `payload.evidence_urls[0]`; most pilot mapped actions currently carry operational JSON, not evidence URL fields.
- `CONTRACT_CANCELLED` is creator-only (`trade.created_by`), while pilot cancel flow is role/process based and may not map to creator identity.

## 4) UUID Format Mismatch
Sources: `core/src/app/api/events/route.ts`, `core/supabase/migrations/20260210120000_afr1_eventize_milestone_events.sql`, `merchant/ananta_delivery_pilot/core/ids.py`.

- Core route + RPC enforce UUID format for `trade_id`, `shipment_id`, `shipment_milestone_id`, and selected actor/location IDs.
- Pilot ID generator emits ULID-style IDs (`new_ulid()`), not UUID v4.
- Directly forwarding pilot IDs into UUID-constrained core fields will fail validation unless translated.

## 5) State Prerequisites Pilot Cannot Satisfy Without Replay/Projection
Sources: `core/supabase/migrations/20260210120000_afr1_eventize_milestone_events.sql`, `core/supabase/migrations/20260209113000_afr1_eventize_shipment_create_link.sql`.

- Shipment creation and shipment milestone events require `trade.state = AUTHORIZED`.
- Reaching `AUTHORIZED` requires prior confirmation flow (`TERMS_SUBMITTED` -> buyer/seller confirmations -> gate-A authorization).
- `GATE_A_AUTHORIZED`, `HOLD_PLACED`, `HOLD_RELEASED`, `COVERAGE_INSTRUMENT_ATTACHED`, `EVIDENCE_ACCEPTED`, `EVIDENCE_REJECTED` require `HOUSE_FINANCE` role.
- If `trade.dispute_status = OPEN`, core blocks most events except `DISPUTE_RESOLVED` and `TRADE_CANCELLED`.
- `FINAL_TERMS_LOCKED` requires prior `TERMS_SUBMITTED` and (at round >= 3) prior `CONTACT_INITIATED`.

## 6) Pilot Event Types in TRUST_ACTION_EVENT_MAPPING With No Canonical Core Mapping
Sources: `merchant/ananta_delivery_pilot/domain/rails_truth.py`, `core/src/app/api/events/eventCatalog.ts`.

- Concrete mapped event types (`DREP_*`) all have alias entries in core event catalog.
- `DERIVED` is intentionally non-canonical and does not map to a core event type (by design).

## 7) Core Catalog vs Runtime State-Machine Divergence
Sources: `core/src/app/api/events/eventCatalog.ts`, `core/supabase/migrations/20260210120000_afr1_eventize_milestone_events.sql`.

- `CONTACT_INITIATED` and `NEGOTIATION_RETURNED_TO_DRAFT` exist in event catalog and route parsing.
- Current `apply_trade_event` allowed-type list (latest migration) does not include these two values.
- Net effect today: route accepts names but RPC returns invalid type (`bad_request`), so these are not currently executable in the DB state machine.
