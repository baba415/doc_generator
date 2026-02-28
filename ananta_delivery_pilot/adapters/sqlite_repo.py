from __future__ import annotations

import csv
import json
import sqlite3
from datetime import date, timedelta
from contextlib import contextmanager
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterator

from core.config import RuntimeConfig, infer_buyer_group, infer_lane
from core.enums import ContractStatus, DeliveryStatus, DocumentType, PaymentStatus
from core.hashing import canonical_json_sha256
from core.ids import new_ulid
from core.time import utc_now_iso_z, utc_today_iso
from core.units import kg_to_mt_decimal, mt_to_kg_int
from domain.transitions import derive_contract_status, validate_delivery_transition
from domain.validators import (
    ensure_required_coa_rows,
    ensure_run_and_batch,
    ensure_vendor_tin,
    ensure_within_overdelivery_tolerance,
)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS report_context (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  as_of_date TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS parties (
  party_id TEXT PRIMARY KEY,
  legal_name TEXT NOT NULL,
  code TEXT,
  tin TEXT,
  rc_number TEXT,
  address TEXT,
  city_state_country TEXT,
  website TEXT,
  phone TEXT,
  email TEXT,
  bank_name TEXT,
  bank_account_name TEXT,
  bank_account_number TEXT,
  bank_currency TEXT,
  aliases_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS parties_snapshot (
  snapshot_id TEXT PRIMARY KEY,
  buyer_id TEXT NOT NULL,
  buyer_name TEXT NOT NULL,
  buyer_tin TEXT,
  buyer_rc_number TEXT,
  vendor_of_record_id TEXT NOT NULL,
  vendor_of_record_name TEXT NOT NULL,
  vendor_of_record_tin TEXT,
  vendor_of_record_rc_number TEXT,
  operator_id TEXT NOT NULL,
  operator_name TEXT NOT NULL,
  operator_tin TEXT,
  operator_rc_number TEXT,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contracts (
  contract_id TEXT PRIMARY KEY,
  master_contract_id TEXT REFERENCES contracts(contract_id),
  contract_ref TEXT NOT NULL,
  lpo_no TEXT NOT NULL,
  lpo_date TEXT,
  buyer_id TEXT NOT NULL REFERENCES parties(party_id),
  vendor_of_record_id TEXT NOT NULL REFERENCES parties(party_id),
  operator_id TEXT NOT NULL REFERENCES parties(party_id),
  source_id TEXT REFERENCES parties(party_id),
  processor_id TEXT REFERENCES parties(party_id),
  lane TEXT NOT NULL,
  currency TEXT NOT NULL,
  issue_date TEXT NOT NULL,
  lpo_valid_from TEXT,
  lpo_valid_to TEXT,
  lpo_state TEXT NOT NULL DEFAULT 'ACTIVE',
  cancelled_at TEXT,
  expired_at TEXT,
  closed_at TEXT,
  close_reason TEXT,
  due_date TEXT,
  due_terms TEXT,
  expected_total_qty REAL NOT NULL,
  expected_total_qty_kg INTEGER NOT NULL DEFAULT 0,
  expected_total_value REAL NOT NULL,
  over_delivery_tolerance_pct REAL NOT NULL DEFAULT 5.0,
  status TEXT NOT NULL DEFAULT 'OPEN',
  notes TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contract_line_items (
  contract_line_id TEXT PRIMARY KEY,
  contract_id TEXT NOT NULL REFERENCES contracts(contract_id) ON DELETE CASCADE,
  line_no INTEGER NOT NULL,
  product_code TEXT NOT NULL,
  description TEXT NOT NULL,
  expected_qty REAL NOT NULL,
  delivered_qty REAL NOT NULL DEFAULT 0,
  expected_qty_kg INTEGER NOT NULL DEFAULT 0,
  delivered_qty_kg INTEGER NOT NULL DEFAULT 0,
  unit TEXT NOT NULL,
  unit_price REAL NOT NULL,
  unit_price_basis TEXT NOT NULL DEFAULT 'KG',
  expected_value REAL NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(contract_id, line_no)
);

CREATE TABLE IF NOT EXISTS deliveries (
  delivery_id TEXT PRIMARY KEY,
  contract_id TEXT NOT NULL REFERENCES contracts(contract_id) ON DELETE CASCADE,
  contract_line_id TEXT REFERENCES contract_line_items(contract_line_id),
  delivery_ref TEXT,
  run_id TEXT NOT NULL,
  batch_id TEXT NOT NULL,
  delivery_date TEXT NOT NULL,
  delivered_qty REAL NOT NULL,
  delivered_qty_kg INTEGER NOT NULL DEFAULT 0,
  unit TEXT NOT NULL,
  unit_price REAL NOT NULL,
  unit_price_basis TEXT NOT NULL DEFAULT 'KG',
  gross_amount REAL NOT NULL,
  truck_no TEXT,
  driver_name TEXT,
  driver_phone TEXT,
  notes TEXT,
  status TEXT NOT NULL DEFAULT 'PLANNED',
  dispatched_at TEXT,
  delivered_at TEXT,
  invoiced_at TEXT,
  paid_at TEXT,
  over_delivery_override_reason TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS procurements (
  procurement_id TEXT PRIMARY KEY,
  contract_id TEXT NOT NULL REFERENCES contracts(contract_id) ON DELETE CASCADE,
  delivery_id TEXT REFERENCES deliveries(delivery_id) ON DELETE SET NULL,
  source_id TEXT REFERENCES parties(party_id),
  processor_id TEXT REFERENCES parties(party_id),
  product_code TEXT NOT NULL,
  quantity REAL NOT NULL,
  quantity_kg INTEGER NOT NULL DEFAULT 0,
  unit TEXT NOT NULL,
  unit_cost REAL NOT NULL,
  gross_amount REAL NOT NULL,
  run_id TEXT,
  batch_id TEXT,
  document_ref TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sales_transactions (
  sales_transaction_id TEXT PRIMARY KEY,
  contract_id TEXT NOT NULL REFERENCES contracts(contract_id) ON DELETE CASCADE,
  delivery_id TEXT NOT NULL UNIQUE REFERENCES deliveries(delivery_id) ON DELETE CASCADE,
  snapshot_id TEXT NOT NULL REFERENCES parties_snapshot(snapshot_id),
  vendor_of_record_id TEXT NOT NULL REFERENCES parties(party_id),
  buyer_id TEXT NOT NULL REFERENCES parties(party_id),
  operator_id TEXT NOT NULL REFERENCES parties(party_id),
  lane TEXT NOT NULL,
  invoice_no TEXT NOT NULL,
  invoice_date TEXT NOT NULL,
  due_date TEXT,
  currency TEXT NOT NULL,
  gross_amount REAL NOT NULL,
  amount_due REAL NOT NULL,
  expected_wht_amount REAL NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(vendor_of_record_id, invoice_no)
);

CREATE TABLE IF NOT EXISTS sales_lines (
  sales_line_id TEXT PRIMARY KEY,
  sales_transaction_id TEXT NOT NULL REFERENCES sales_transactions(sales_transaction_id) ON DELETE CASCADE,
  delivery_id TEXT NOT NULL REFERENCES deliveries(delivery_id) ON DELETE CASCADE,
  contract_line_id TEXT REFERENCES contract_line_items(contract_line_id),
  line_no INTEGER NOT NULL,
  product_code TEXT NOT NULL,
  description TEXT NOT NULL,
  quantity REAL NOT NULL,
  quantity_kg INTEGER NOT NULL DEFAULT 0,
  unit TEXT NOT NULL,
  unit_price REAL NOT NULL,
  unit_price_basis TEXT NOT NULL DEFAULT 'KG',
  gross_amount REAL NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(sales_transaction_id, line_no)
);

CREATE TABLE IF NOT EXISTS documents (
  doc_id TEXT PRIMARY KEY,
  delivery_id TEXT NOT NULL REFERENCES deliveries(delivery_id) ON DELETE CASCADE,
  sales_transaction_id TEXT NOT NULL REFERENCES sales_transactions(sales_transaction_id) ON DELETE CASCADE,
  snapshot_id TEXT NOT NULL REFERENCES parties_snapshot(snapshot_id),
  vendor_of_record_id TEXT NOT NULL REFERENCES parties(party_id),
  doc_type TEXT NOT NULL,
  doc_number TEXT NOT NULL,
  revision_no INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'ACTIVE',
  pdf_path TEXT,
  pdf_sha256 TEXT,
  content_sha256 TEXT NOT NULL,
  generated_at TEXT NOT NULL,
  superseded_by_doc_id TEXT REFERENCES documents(doc_id),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(vendor_of_record_id, doc_type, doc_number)
);

CREATE TABLE IF NOT EXISTS document_sales_links (
  link_id TEXT PRIMARY KEY,
  doc_id TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
  sales_transaction_id TEXT NOT NULL REFERENCES sales_transactions(sales_transaction_id) ON DELETE CASCADE,
  sales_line_id TEXT NOT NULL REFERENCES sales_lines(sales_line_id) ON DELETE CASCADE,
  delivery_id TEXT NOT NULL REFERENCES deliveries(delivery_id) ON DELETE CASCADE,
  created_at TEXT NOT NULL,
  UNIQUE(doc_id, sales_line_id)
);

CREATE TABLE IF NOT EXISTS payments (
  payment_id TEXT PRIMARY KEY,
  vendor_of_record_id TEXT NOT NULL REFERENCES parties(party_id),
  buyer_id TEXT NOT NULL REFERENCES parties(party_id),
  payment_date TEXT NOT NULL,
  amount_received REAL NOT NULL,
  currency TEXT NOT NULL,
  payment_method TEXT NOT NULL,
  external_reference TEXT,
  idempotency_key TEXT NOT NULL,
  receipt_no TEXT NOT NULL,
  receipt_doc_id TEXT REFERENCES documents(doc_id),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(idempotency_key),
  UNIQUE(receipt_no)
);

CREATE TABLE IF NOT EXISTS payment_allocations (
  allocation_id TEXT PRIMARY KEY,
  payment_id TEXT NOT NULL REFERENCES payments(payment_id) ON DELETE CASCADE,
  sales_transaction_id TEXT NOT NULL REFERENCES sales_transactions(sales_transaction_id) ON DELETE CASCADE,
  allocated_amount REAL NOT NULL,
  allocation_date TEXT NOT NULL,
  notes TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(payment_id, sales_transaction_id)
);

CREATE TABLE IF NOT EXISTS tax_withholding_events (
  withholding_event_id TEXT PRIMARY KEY,
  sales_transaction_id TEXT NOT NULL REFERENCES sales_transactions(sales_transaction_id) ON DELETE CASCADE,
  sales_line_id TEXT REFERENCES sales_lines(sales_line_id) ON DELETE SET NULL,
  withholder_party_id TEXT NOT NULL REFERENCES parties(party_id),
  withholding_type TEXT NOT NULL,
  amount REAL NOT NULL,
  certificate_ref TEXT,
  evidence_path TEXT,
  evidence_hash TEXT,
  certified_at TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence_originals (
  evidence_id TEXT PRIMARY KEY,
  contract_id TEXT REFERENCES contracts(contract_id) ON DELETE CASCADE,
  delivery_id TEXT REFERENCES deliveries(delivery_id) ON DELETE CASCADE,
  sales_transaction_id TEXT REFERENCES sales_transactions(sales_transaction_id) ON DELETE SET NULL,
  source_path TEXT NOT NULL,
  stored_path TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  captured_at TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS coa_results (
  coa_record_id TEXT PRIMARY KEY,
  buyer_group TEXT NOT NULL,
  product_code TEXT NOT NULL,
  batch_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  profile_key TEXT NOT NULL,
  profile_version TEXT NOT NULL,
  coa_no TEXT NOT NULL,
  results_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(buyer_group, product_code, batch_id, run_id, profile_version)
);

CREATE TABLE IF NOT EXISTS delivery_coa_links (
  link_id TEXT PRIMARY KEY,
  delivery_id TEXT NOT NULL REFERENCES deliveries(delivery_id) ON DELETE CASCADE,
  coa_record_id TEXT NOT NULL REFERENCES coa_results(coa_record_id) ON DELETE CASCADE,
  created_at TEXT NOT NULL,
  UNIQUE(delivery_id, coa_record_id)
);

CREATE TABLE IF NOT EXISTS planned_deliveries (
  planned_delivery_id TEXT PRIMARY KEY,
  contract_id TEXT NOT NULL REFERENCES contracts(contract_id) ON DELETE CASCADE,
  contract_line_id TEXT NOT NULL REFERENCES contract_line_items(contract_line_id) ON DELETE CASCADE,
  sequence_no INTEGER NOT NULL,
  planned_qty_kg INTEGER NOT NULL CHECK(planned_qty_kg > 0),
  lot_size_kg INTEGER NOT NULL CHECK(lot_size_kg > 0),
  planned_date TEXT NOT NULL,
  run_id TEXT,
  batch_id TEXT,
  status TEXT NOT NULL DEFAULT 'PLANNED',
  delivery_id TEXT UNIQUE REFERENCES deliveries(delivery_id) ON DELETE SET NULL,
  notes TEXT,
  materialized_qty_kg INTEGER NOT NULL DEFAULT 0,
  delivered_qty_kg INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(contract_line_id, sequence_no)
);

CREATE TABLE IF NOT EXISTS numbering_sequences (
  vendor_of_record_id TEXT NOT NULL REFERENCES parties(party_id),
  doc_type TEXT NOT NULL,
  year INTEGER NOT NULL,
  current_value INTEGER NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(vendor_of_record_id, doc_type, year)
);

CREATE TABLE IF NOT EXISTS command_idempotency (
  command_name TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  response_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(command_name, idempotency_key)
);

CREATE TABLE IF NOT EXISTS automation_runs (
  run_id TEXT PRIMARY KEY,
  idempotency_key TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL,
  dry_run INTEGER NOT NULL DEFAULT 0,
  as_of_date TEXT NOT NULL,
  input_json TEXT NOT NULL,
  normalized_input_json TEXT,
  metrics_json TEXT,
  failure_reason TEXT,
  started_at TEXT NOT NULL,
  completed_at TEXT,
  duration_seconds REAL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS automation_decisions (
  decision_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES automation_runs(run_id) ON DELETE CASCADE,
  stage TEXT NOT NULL,
  field_name TEXT NOT NULL,
  required_flag INTEGER NOT NULL DEFAULT 0,
  proposed_value TEXT,
  source_type TEXT,
  source_ref TEXT,
  confidence REAL,
  decision TEXT NOT NULL,
  reason_code TEXT,
  rule_path TEXT,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_automation_decisions_run ON automation_decisions(run_id);

CREATE TABLE IF NOT EXISTS exception_queue (
  exception_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES automation_runs(run_id) ON DELETE CASCADE,
  stage TEXT NOT NULL,
  exception_type TEXT NOT NULL,
  severity TEXT NOT NULL,
  field_name TEXT,
  proposed_value TEXT,
  reason TEXT NOT NULL,
  suggestions_json TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'OPEN',
  resolved_value TEXT,
  resolution_note TEXT,
  created_at TEXT NOT NULL,
  resolved_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_exception_queue_run_status ON exception_queue(run_id, status);

CREATE TABLE IF NOT EXISTS master_contracts (
  master_contract_id TEXT PRIMARY KEY,
  master_contract_ref TEXT NOT NULL,
  buyer_id TEXT REFERENCES parties(party_id),
  vendor_of_record_id TEXT REFERENCES parties(party_id),
  operator_id TEXT REFERENCES parties(party_id),
  source_id TEXT REFERENCES parties(party_id),
  processor_id TEXT REFERENCES parties(party_id),
  status TEXT NOT NULL DEFAULT 'ACTIVE',
  effective_from TEXT,
  effective_to TEXT,
  notes TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(master_contract_ref)
);

CREATE TABLE IF NOT EXISTS contract_cycles (
  contract_cycle_id TEXT PRIMARY KEY,
  master_contract_id TEXT NOT NULL REFERENCES master_contracts(master_contract_id) ON DELETE CASCADE,
  contract_id TEXT REFERENCES contracts(contract_id) ON DELETE SET NULL,
  cycle_no INTEGER NOT NULL CHECK(cycle_no > 0),
  cycle_start TEXT,
  cycle_end TEXT,
  status TEXT NOT NULL DEFAULT 'OPEN',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  CHECK(cycle_end IS NULL OR cycle_start IS NULL OR cycle_end >= cycle_start),
  UNIQUE(master_contract_id, cycle_no),
  UNIQUE(contract_id)
);

CREATE TABLE IF NOT EXISTS policy_sets (
  policy_set_id TEXT PRIMARY KEY,
  policy_scope TEXT NOT NULL,
  scope_ref_id TEXT,
  version TEXT NOT NULL,
  policy_json TEXT NOT NULL,
  is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0, 1)),
  effective_from TEXT,
  effective_to TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  CHECK(effective_to IS NULL OR effective_from IS NULL OR effective_to >= effective_from),
  UNIQUE(policy_scope, scope_ref_id, version)
);

CREATE TABLE IF NOT EXISTS policy_overrides (
  policy_override_id TEXT PRIMARY KEY,
  policy_set_id TEXT NOT NULL REFERENCES policy_sets(policy_set_id) ON DELETE CASCADE,
  override_scope TEXT NOT NULL,
  scope_ref_id TEXT,
  key_path TEXT NOT NULL,
  override_value_json TEXT NOT NULL,
  reason TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(policy_set_id, override_scope, scope_ref_id, key_path)
);

CREATE TABLE IF NOT EXISTS capacity_calendar (
  capacity_entry_id TEXT PRIMARY KEY,
  as_of_date TEXT NOT NULL,
  corridor_key TEXT,
  buyer_id TEXT REFERENCES parties(party_id),
  processor_id TEXT REFERENCES parties(party_id),
  max_lots INTEGER NOT NULL DEFAULT 0 CHECK(max_lots >= 0),
  max_qty_kg INTEGER NOT NULL DEFAULT 0 CHECK(max_qty_kg >= 0),
  reserved_lots INTEGER NOT NULL DEFAULT 0 CHECK(reserved_lots >= 0),
  reserved_qty_kg INTEGER NOT NULL DEFAULT 0 CHECK(reserved_qty_kg >= 0),
  notes TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(as_of_date, corridor_key, buyer_id, processor_id)
);

CREATE TABLE IF NOT EXISTS gate_evaluations (
  gate_evaluation_id TEXT PRIMARY KEY,
  autonomy_run_id TEXT,
  contract_id TEXT REFERENCES contracts(contract_id) ON DELETE CASCADE,
  delivery_id TEXT REFERENCES deliveries(delivery_id) ON DELETE CASCADE,
  planned_delivery_id TEXT REFERENCES planned_deliveries(planned_delivery_id) ON DELETE CASCADE,
  gate_name TEXT NOT NULL,
  subject_type TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  as_of_date TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('PASS', 'FAIL', 'WARN')),
  score REAL,
  reason_code TEXT,
  details_json TEXT NOT NULL DEFAULT '{}',
  evaluated_at TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS action_intents (
  action_intent_id TEXT PRIMARY KEY,
  autonomy_run_id TEXT,
  intent_type TEXT NOT NULL,
  contract_id TEXT REFERENCES contracts(contract_id) ON DELETE CASCADE,
  delivery_id TEXT REFERENCES deliveries(delivery_id) ON DELETE CASCADE,
  planned_delivery_id TEXT REFERENCES planned_deliveries(planned_delivery_id) ON DELETE CASCADE,
  as_of_date TEXT NOT NULL,
  scheduled_at TEXT,
  status TEXT NOT NULL DEFAULT 'PENDING',
  policy_version TEXT,
  payload_json TEXT NOT NULL DEFAULT '{}',
  idempotency_key TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(idempotency_key)
);

CREATE TABLE IF NOT EXISTS action_executions (
  action_execution_id TEXT PRIMARY KEY,
  action_intent_id TEXT NOT NULL REFERENCES action_intents(action_intent_id) ON DELETE CASCADE,
  execution_no INTEGER NOT NULL CHECK(execution_no > 0),
  status TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  request_json TEXT NOT NULL DEFAULT '{}',
  response_json TEXT NOT NULL DEFAULT '{}',
  error_json TEXT,
  executed_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(action_intent_id, execution_no),
  UNIQUE(idempotency_key)
);

CREATE TABLE IF NOT EXISTS exception_cases (
  exception_case_id TEXT PRIMARY KEY,
  autonomy_run_id TEXT,
  action_intent_id TEXT REFERENCES action_intents(action_intent_id) ON DELETE SET NULL,
  contract_id TEXT REFERENCES contracts(contract_id) ON DELETE CASCADE,
  delivery_id TEXT REFERENCES deliveries(delivery_id) ON DELETE CASCADE,
  planned_delivery_id TEXT REFERENCES planned_deliveries(planned_delivery_id) ON DELETE CASCADE,
  case_type TEXT NOT NULL,
  severity TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'OPEN',
  reason_code TEXT,
  details_json TEXT NOT NULL DEFAULT '{}',
  idempotency_key TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  resolved_at TEXT,
  UNIQUE(idempotency_key)
);

CREATE TABLE IF NOT EXISTS users (
  user_id TEXT PRIMARY KEY,
  email TEXT NOT NULL,
  display_name TEXT,
  status TEXT NOT NULL DEFAULT 'ACTIVE',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(email)
);

CREATE TABLE IF NOT EXISTS human_decisions (
  human_decision_id TEXT PRIMARY KEY,
  exception_case_id TEXT NOT NULL REFERENCES exception_cases(exception_case_id) ON DELETE CASCADE,
  decided_by_user_id TEXT REFERENCES users(user_id) ON DELETE SET NULL,
  decision TEXT NOT NULL,
  reason TEXT NOT NULL,
  decision_payload_json TEXT NOT NULL DEFAULT '{}',
  idempotency_key TEXT NOT NULL,
  decided_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(idempotency_key)
);

CREATE TABLE IF NOT EXISTS decision_features (
  decision_feature_id TEXT PRIMARY KEY,
  exception_case_id TEXT NOT NULL REFERENCES exception_cases(exception_case_id) ON DELETE CASCADE,
  feature_key TEXT NOT NULL,
  feature_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(exception_case_id, feature_key)
);

CREATE TABLE IF NOT EXISTS decision_outcomes (
  decision_outcome_id TEXT PRIMARY KEY,
  exception_case_id TEXT NOT NULL REFERENCES exception_cases(exception_case_id) ON DELETE CASCADE,
  human_decision_id TEXT REFERENCES human_decisions(human_decision_id) ON DELETE SET NULL,
  outcome_label TEXT NOT NULL,
  outcome_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_log (
  event_id TEXT PRIMARY KEY,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  as_of_date TEXT,
  payload_json TEXT NOT NULL DEFAULT '{}',
  source TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS trg_event_log_no_update
BEFORE UPDATE ON event_log
BEGIN
  SELECT RAISE(ABORT, 'event_log is append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_event_log_no_delete
BEFORE DELETE ON event_log
BEGIN
  SELECT RAISE(ABORT, 'event_log is append-only');
END;

CREATE TABLE IF NOT EXISTS transport_partners (
  transport_partner_id TEXT PRIMARY KEY,
  legal_name TEXT NOT NULL,
  code TEXT,
  status TEXT NOT NULL DEFAULT 'ACTIVE',
  phone TEXT,
  email TEXT,
  notes TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(code)
);

CREATE TABLE IF NOT EXISTS transport_trucks (
  transport_truck_id TEXT PRIMARY KEY,
  transport_partner_id TEXT REFERENCES transport_partners(transport_partner_id) ON DELETE SET NULL,
  truck_no TEXT NOT NULL,
  capacity_kg INTEGER CHECK(capacity_kg >= 0),
  status TEXT NOT NULL DEFAULT 'ACTIVE',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(truck_no)
);

CREATE TABLE IF NOT EXISTS transport_drivers (
  transport_driver_id TEXT PRIMARY KEY,
  transport_partner_id TEXT REFERENCES transport_partners(transport_partner_id) ON DELETE SET NULL,
  full_name TEXT NOT NULL,
  phone TEXT,
  license_no TEXT,
  status TEXT NOT NULL DEFAULT 'ACTIVE',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(license_no)
);

CREATE TABLE IF NOT EXISTS truck_driver_assignments (
  assignment_id TEXT PRIMARY KEY,
  transport_truck_id TEXT NOT NULL REFERENCES transport_trucks(transport_truck_id) ON DELETE CASCADE,
  transport_driver_id TEXT NOT NULL REFERENCES transport_drivers(transport_driver_id) ON DELETE CASCADE,
  effective_from TEXT NOT NULL,
  effective_to TEXT,
  is_primary INTEGER NOT NULL DEFAULT 1 CHECK(is_primary IN (0, 1)),
  notes TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  CHECK(effective_to IS NULL OR effective_to >= effective_from),
  UNIQUE(transport_truck_id, transport_driver_id, effective_from)
);

CREATE TABLE IF NOT EXISTS transport_compliance_docs (
  compliance_doc_id TEXT PRIMARY KEY,
  entity_type TEXT NOT NULL CHECK(entity_type IN ('PARTNER', 'TRUCK', 'DRIVER')),
  entity_id TEXT NOT NULL,
  doc_type TEXT NOT NULL,
  doc_ref TEXT,
  valid_from TEXT,
  valid_to TEXT,
  evidence_path TEXT,
  evidence_hash TEXT,
  status TEXT NOT NULL DEFAULT 'ACTIVE',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  CHECK(valid_to IS NULL OR valid_from IS NULL OR valid_to >= valid_from)
);

CREATE TABLE IF NOT EXISTS transport_aliases (
  transport_alias_id TEXT PRIMARY KEY,
  entity_type TEXT NOT NULL CHECK(entity_type IN ('PARTNER', 'TRUCK', 'DRIVER')),
  entity_id TEXT NOT NULL,
  alias_text TEXT NOT NULL,
  normalized_alias TEXT NOT NULL,
  source TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(entity_type, normalized_alias)
);

CREATE TABLE IF NOT EXISTS delivery_transport_snapshot (
  snapshot_id TEXT PRIMARY KEY,
  delivery_id TEXT UNIQUE REFERENCES deliveries(delivery_id) ON DELETE CASCADE,
  planned_delivery_id TEXT REFERENCES planned_deliveries(planned_delivery_id) ON DELETE SET NULL,
  transport_partner_id TEXT REFERENCES transport_partners(transport_partner_id) ON DELETE SET NULL,
  transport_truck_id TEXT REFERENCES transport_trucks(transport_truck_id) ON DELETE SET NULL,
  transport_driver_id TEXT REFERENCES transport_drivers(transport_driver_id) ON DELETE SET NULL,
  partner_name TEXT,
  truck_no TEXT,
  driver_name TEXT,
  driver_phone TEXT,
  source_type TEXT,
  source_ref TEXT,
  confidence REAL,
  reason_code TEXT,
  payload_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS delivery_transport_suggestions (
  suggestion_id TEXT PRIMARY KEY,
  delivery_id TEXT REFERENCES deliveries(delivery_id) ON DELETE CASCADE,
  planned_delivery_id TEXT REFERENCES planned_deliveries(planned_delivery_id) ON DELETE CASCADE,
  entity_type TEXT NOT NULL CHECK(entity_type IN ('PARTNER', 'TRUCK', 'DRIVER')),
  candidate_entity_id TEXT NOT NULL,
  candidate_label TEXT NOT NULL,
  confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
  explanation_json TEXT NOT NULL DEFAULT '{}',
  accepted INTEGER CHECK(accepted IN (0, 1)),
  created_at TEXT NOT NULL,
  UNIQUE(planned_delivery_id, entity_type, candidate_entity_id)
);

CREATE TABLE IF NOT EXISTS user_roles (
  user_role_id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  role_code TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(user_id, role_code)
);

CREATE TABLE IF NOT EXISTS user_preferences (
  user_id TEXT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
  preferences_json TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_master_contracts_status ON master_contracts(status);
CREATE INDEX IF NOT EXISTS idx_contract_cycles_master ON contract_cycles(master_contract_id, cycle_no);
CREATE INDEX IF NOT EXISTS idx_policy_sets_scope ON policy_sets(policy_scope, scope_ref_id, is_active);
CREATE INDEX IF NOT EXISTS idx_capacity_calendar_lookup ON capacity_calendar(as_of_date, corridor_key, buyer_id, processor_id);
CREATE INDEX IF NOT EXISTS idx_gate_eval_lookup ON gate_evaluations(contract_id, gate_name, as_of_date);
CREATE INDEX IF NOT EXISTS idx_gate_eval_delivery ON gate_evaluations(delivery_id, gate_name, as_of_date);
CREATE INDEX IF NOT EXISTS idx_action_intent_lookup ON action_intents(contract_id, status, scheduled_at);
CREATE INDEX IF NOT EXISTS idx_action_intent_delivery ON action_intents(delivery_id, status);
CREATE INDEX IF NOT EXISTS idx_action_exec_intent ON action_executions(action_intent_id, created_at);
CREATE INDEX IF NOT EXISTS idx_exception_cases_status ON exception_cases(status, severity, created_at);
CREATE INDEX IF NOT EXISTS idx_exception_cases_contract ON exception_cases(contract_id, status, created_at);
CREATE INDEX IF NOT EXISTS idx_human_decisions_case ON human_decisions(exception_case_id, decided_at);
CREATE INDEX IF NOT EXISTS idx_decision_outcomes_case ON decision_outcomes(exception_case_id, created_at);
CREATE INDEX IF NOT EXISTS idx_event_log_entity ON event_log(entity_type, entity_id, created_at);
CREATE INDEX IF NOT EXISTS idx_event_log_type_date ON event_log(event_type, as_of_date, created_at);
CREATE INDEX IF NOT EXISTS idx_transport_trucks_partner ON transport_trucks(transport_partner_id, truck_no);
CREATE INDEX IF NOT EXISTS idx_transport_drivers_partner ON transport_drivers(transport_partner_id, full_name);
CREATE INDEX IF NOT EXISTS idx_truck_driver_effective ON truck_driver_assignments(transport_truck_id, effective_from, effective_to);
CREATE INDEX IF NOT EXISTS idx_driver_truck_effective ON truck_driver_assignments(transport_driver_id, effective_from, effective_to);
CREATE INDEX IF NOT EXISTS idx_transport_compliance_entity ON transport_compliance_docs(entity_type, entity_id, valid_to);
CREATE INDEX IF NOT EXISTS idx_transport_alias_lookup ON transport_aliases(entity_type, normalized_alias);
CREATE INDEX IF NOT EXISTS idx_delivery_transport_snapshot_plan ON delivery_transport_snapshot(planned_delivery_id, created_at);
CREATE INDEX IF NOT EXISTS idx_delivery_transport_suggestions_plan ON delivery_transport_suggestions(planned_delivery_id, entity_type, confidence);
CREATE INDEX IF NOT EXISTS idx_user_roles_user ON user_roles(user_id, role_code);
"""


VIEWS_SQL = """
DROP VIEW IF EXISTS drep_contracts;
DROP VIEW IF EXISTS drep_procurement;
DROP VIEW IF EXISTS drep_sales;
DROP VIEW IF EXISTS drep_sales_lines;
DROP VIEW IF EXISTS drep_outstanding_payments;
DROP VIEW IF EXISTS drep_delivery_plan_status;

CREATE VIEW IF NOT EXISTS drep_contracts AS
SELECT
  c.contract_id,
  c.contract_ref,
  c.lpo_no,
  c.issue_date,
  c.lpo_valid_from,
  c.lpo_valid_to,
  c.lpo_state,
  c.due_date,
  c.buyer_id,
  c.vendor_of_record_id,
  c.operator_id,
  c.source_id,
  c.processor_id,
  c.currency,
  c.expected_total_qty_kg,
  ROUND(c.expected_total_qty_kg / 1000.0, 3) AS expected_total_qty_mt,
  ROUND(c.expected_total_qty_kg / 1000.0, 3) AS expected_total_qty,
  c.expected_total_value,
  COALESCE(SUM(d.delivered_qty_kg), 0) AS delivered_qty_total_kg,
  ROUND(COALESCE(SUM(d.delivered_qty_kg), 0) / 1000.0, 3) AS delivered_qty_total_mt,
  ROUND(COALESCE(SUM(d.delivered_qty_kg), 0) / 1000.0, 3) AS delivered_qty_total,
  c.status,
  c.created_at,
  c.updated_at
FROM contracts c
LEFT JOIN deliveries d ON d.contract_id = c.contract_id
GROUP BY
  c.contract_id, c.contract_ref, c.lpo_no, c.issue_date, c.lpo_valid_from, c.lpo_valid_to, c.lpo_state, c.due_date, c.buyer_id, c.vendor_of_record_id,
  c.operator_id, c.source_id, c.processor_id, c.currency, c.expected_total_qty_kg, c.expected_total_value,
  c.status, c.created_at, c.updated_at;

CREATE VIEW IF NOT EXISTS drep_procurement AS
SELECT
  p.procurement_id,
  p.contract_id,
  p.delivery_id,
  p.source_id,
  p.processor_id,
  p.product_code,
  p.quantity,
  p.quantity_kg,
  ROUND(p.quantity_kg / 1000.0, 3) AS quantity_mt,
  p.unit,
  p.unit_cost,
  p.gross_amount,
  p.run_id,
  p.batch_id,
  p.document_ref,
  p.created_at
FROM procurements p;

CREATE VIEW IF NOT EXISTS drep_sales AS
SELECT
  st.sales_transaction_id,
  st.contract_id,
  st.delivery_id,
  st.snapshot_id,
  st.vendor_of_record_id,
  st.buyer_id,
  st.operator_id,
  st.lane,
  st.invoice_no,
  st.invoice_date,
  st.due_date,
  st.currency,
  st.gross_amount,
  st.amount_due,
  st.expected_wht_amount,
  COALESCE(pa.amount_paid_to_date, 0) AS amount_paid_to_date,
  COALESCE(wh.certified_withheld_amount, 0) AS certified_withheld_amount,
  st.amount_due - COALESCE(pa.amount_paid_to_date, 0) - COALESCE(wh.certified_withheld_amount, 0) AS outstanding_balance,
  st.created_at,
  st.updated_at
FROM sales_transactions st
LEFT JOIN (
  SELECT sales_transaction_id, SUM(allocated_amount) AS amount_paid_to_date
  FROM payment_allocations
  GROUP BY sales_transaction_id
) pa ON pa.sales_transaction_id = st.sales_transaction_id
LEFT JOIN (
  SELECT sales_transaction_id, SUM(amount) AS certified_withheld_amount
  FROM tax_withholding_events
  GROUP BY sales_transaction_id
) wh ON wh.sales_transaction_id = st.sales_transaction_id;

CREATE VIEW IF NOT EXISTS drep_sales_lines AS
SELECT
  sl.sales_line_id,
  sl.sales_transaction_id,
  sl.delivery_id,
  sl.contract_line_id,
  sl.line_no,
  sl.product_code,
  sl.description,
  sl.quantity,
  sl.quantity_kg,
  ROUND(sl.quantity_kg / 1000.0, 3) AS quantity_mt,
  sl.unit,
  sl.unit_price,
  sl.unit_price_basis,
  sl.gross_amount,
  MAX(CASE WHEN d.doc_type = 'WAYBILL' THEN d.doc_id END) AS waybill_doc_id,
  MAX(CASE WHEN d.doc_type = 'WAYBILL' THEN d.doc_number END) AS waybill_no,
  MAX(CASE WHEN d.doc_type = 'WAYBILL' THEN d.pdf_sha256 END) AS waybill_pdf_sha256,
  MAX(CASE WHEN d.doc_type = 'WEIGHING_TICKET' THEN d.doc_id END) AS weighing_doc_id,
  MAX(CASE WHEN d.doc_type = 'WEIGHING_TICKET' THEN d.doc_number END) AS weighing_no,
  MAX(CASE WHEN d.doc_type = 'WEIGHING_TICKET' THEN d.pdf_sha256 END) AS weighing_pdf_sha256,
  MAX(CASE WHEN d.doc_type = 'COA' THEN d.doc_id END) AS coa_doc_id,
  MAX(CASE WHEN d.doc_type = 'COA' THEN d.doc_number END) AS coa_no,
  MAX(CASE WHEN d.doc_type = 'COA' THEN d.pdf_sha256 END) AS coa_pdf_sha256,
  MAX(CASE WHEN d.doc_type = 'INVOICE' THEN d.doc_id END) AS invoice_doc_id,
  MAX(CASE WHEN d.doc_type = 'INVOICE' THEN d.doc_number END) AS invoice_no,
  MAX(CASE WHEN d.doc_type = 'INVOICE' THEN d.pdf_sha256 END) AS invoice_pdf_sha256
FROM sales_lines sl
LEFT JOIN document_sales_links dsl
  ON dsl.sales_line_id = sl.sales_line_id
LEFT JOIN documents d
  ON d.doc_id = dsl.doc_id
GROUP BY
  sl.sales_line_id, sl.sales_transaction_id, sl.delivery_id, sl.contract_line_id, sl.line_no,
  sl.product_code, sl.description, sl.quantity, sl.quantity_kg, sl.unit, sl.unit_price, sl.gross_amount;

CREATE VIEW IF NOT EXISTS drep_outstanding_payments AS
SELECT
  ds.sales_transaction_id,
  ds.contract_id,
  ds.delivery_id,
  ds.invoice_no,
  ds.invoice_date,
  ds.due_date,
  ds.vendor_of_record_id,
  ds.buyer_id,
  ds.currency,
  ds.gross_amount,
  ds.amount_due,
  ds.expected_wht_amount,
  ds.amount_paid_to_date,
  ds.certified_withheld_amount,
  ds.outstanding_balance,
  CASE
    WHEN ds.outstanding_balance <= 0 THEN 'CURRENT'
    WHEN ds.due_date IS NULL THEN 'CURRENT'
    WHEN julianday(rc.as_of_date) - julianday(ds.due_date) <= 0 THEN 'CURRENT'
    WHEN julianday(rc.as_of_date) - julianday(ds.due_date) <= 30 THEN '1-30'
    WHEN julianday(rc.as_of_date) - julianday(ds.due_date) <= 60 THEN '31-60'
    WHEN julianday(rc.as_of_date) - julianday(ds.due_date) <= 90 THEN '61-90'
    ELSE '90+'
  END AS aging_bucket,
  CASE
    WHEN ds.due_date IS NULL THEN 0
    ELSE CAST(julianday(rc.as_of_date) - julianday(ds.due_date) AS INTEGER)
  END AS days_past_due,
  rc.as_of_date
FROM drep_sales ds
CROSS JOIN report_context rc
WHERE rc.id = 1;

CREATE VIEW IF NOT EXISTS drep_delivery_plan_status AS
SELECT
  pd.contract_id,
  pd.contract_line_id,
  pd.planned_delivery_id,
  pd.planned_date,
  ROUND(pd.lot_size_kg / 1000.0, 3) AS lot_size_mt,
  ROUND(pd.planned_qty_kg / 1000.0, 3) AS planned_qty_mt,
  ROUND(pd.materialized_qty_kg / 1000.0, 3) AS materialized_qty_mt,
  ROUND(
    COALESCE(
      CASE
        WHEN d.delivery_id IS NOT NULL THEN d.delivered_qty_kg
        ELSE pd.delivered_qty_kg
      END,
      0
    ) / 1000.0,
    3
  ) AS delivered_qty_mt,
  ROUND(
    (
      COALESCE(
        CASE
          WHEN d.delivery_id IS NOT NULL THEN d.delivered_qty_kg
          ELSE pd.delivered_qty_kg
        END,
        0
      ) - pd.planned_qty_kg
    ) / 1000.0,
    3
  ) AS variance_qty_mt,
  ROUND(
    (
      pd.planned_qty_kg - COALESCE(
        CASE
          WHEN d.delivery_id IS NOT NULL THEN d.delivered_qty_kg
          ELSE pd.delivered_qty_kg
        END,
        0
      )
    ) / 1000.0,
    3
  ) AS open_planned_qty_mt,
  c.lpo_state,
  rc.as_of_date,
  pd.updated_at
FROM planned_deliveries pd
JOIN contracts c ON c.contract_id = pd.contract_id
LEFT JOIN deliveries d ON d.delivery_id = pd.delivery_id
CROSS JOIN report_context rc
WHERE rc.id = 1;
"""


def _safe_json_value(raw: Any) -> Any:
    if raw is None:
        return None
    if isinstance(raw, (dict, list, int, float, bool)):
        return raw
    text = str(raw).strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return text


def _safe_json_dict(raw: Any) -> dict[str, Any]:
    value = _safe_json_value(raw)
    return value if isinstance(value, dict) else {}


def _deep_merge_dicts(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _deep_merge_dicts(current, value)
        else:
            merged[key] = value
    return merged


def _set_path(target: dict[str, Any], path: str, value: Any) -> None:
    normalized = str(path or "").strip()
    if not normalized:
        return
    parts = [part for part in normalized.split(".") if part]
    if not parts:
        return
    cursor = target
    for index, part in enumerate(parts):
        final = index == len(parts) - 1
        if final:
            cursor[part] = value
            return
        node = cursor.get(part)
        if not isinstance(node, dict):
            node = {}
            cursor[part] = node
        cursor = node


class SQLiteRepo:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def init_db(self, config: RuntimeConfig) -> dict[str, Any]:
        now = utc_now_iso_z()
        with self.transaction() as conn:
            conn.executescript(SCHEMA_SQL)
            self._apply_schema_migrations(conn, config)
            conn.executescript(VIEWS_SQL)
            conn.execute(
                """
                INSERT INTO schema_meta(version, applied_at)
                VALUES(1, ?)
                ON CONFLICT(version) DO NOTHING
                """,
                (now,),
            )
            conn.execute(
                """
                INSERT INTO report_context(id, as_of_date)
                VALUES(1, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (utc_today_iso(),),
            )
            self._seed_parties(conn, config)
        return {"ok": True, "db_path": str(self.db_path)}

    def _table_columns(self, conn: sqlite3.Connection, table_name: str) -> set[str]:
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        return {str(row["name"]) for row in rows}

    def _add_column_if_missing(self, conn: sqlite3.Connection, table_name: str, column_def: str) -> None:
        column_name = column_def.split()[0]
        if column_name in self._table_columns(conn, table_name):
            return
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_def}")

    def _apply_schema_migrations(self, conn: sqlite3.Connection, config: RuntimeConfig) -> None:
        self._add_column_if_missing(conn, "contracts", "master_contract_id TEXT REFERENCES contracts(contract_id)")
        self._add_column_if_missing(conn, "contracts", "lpo_valid_from TEXT")
        self._add_column_if_missing(conn, "contracts", "lpo_valid_to TEXT")
        self._add_column_if_missing(conn, "contracts", "lpo_state TEXT NOT NULL DEFAULT 'ACTIVE'")
        self._add_column_if_missing(conn, "contracts", "cancelled_at TEXT")
        self._add_column_if_missing(conn, "contracts", "expired_at TEXT")
        self._add_column_if_missing(conn, "contracts", "closed_at TEXT")
        self._add_column_if_missing(conn, "contracts", "close_reason TEXT")
        self._add_column_if_missing(conn, "contracts", "expected_total_qty_kg INTEGER NOT NULL DEFAULT 0")
        self._add_column_if_missing(conn, "contract_line_items", "expected_qty_kg INTEGER NOT NULL DEFAULT 0")
        self._add_column_if_missing(conn, "contract_line_items", "delivered_qty_kg INTEGER NOT NULL DEFAULT 0")
        self._add_column_if_missing(conn, "contract_line_items", "unit_price_basis TEXT NOT NULL DEFAULT 'KG'")
        self._add_column_if_missing(conn, "deliveries", "delivered_qty_kg INTEGER NOT NULL DEFAULT 0")
        self._add_column_if_missing(conn, "deliveries", "unit_price_basis TEXT NOT NULL DEFAULT 'KG'")
        self._add_column_if_missing(conn, "procurements", "quantity_kg INTEGER NOT NULL DEFAULT 0")
        self._add_column_if_missing(conn, "sales_lines", "quantity_kg INTEGER NOT NULL DEFAULT 0")
        self._add_column_if_missing(conn, "sales_lines", "unit_price_basis TEXT NOT NULL DEFAULT 'KG'")

        backfill_policy = str(config.delivery_policies.get("validity_backfill_policy") or "null_if_missing")
        now = utc_now_iso_z()
        conn.execute(
            """
            UPDATE contracts
            SET lpo_state = 'ACTIVE',
                lpo_valid_from = COALESCE(lpo_valid_from, issue_date),
                updated_at = ?
            WHERE lpo_state IS NULL OR TRIM(lpo_state) = ''
            """,
            (now,),
        )
        conn.execute(
            """
            UPDATE contracts
            SET lpo_valid_from = COALESCE(lpo_valid_from, issue_date),
                updated_at = ?
            WHERE lpo_valid_from IS NULL
            """,
            (now,),
        )
        if backfill_policy == "fallback_due_date":
            conn.execute(
                """
                UPDATE contracts
                SET lpo_valid_to = due_date,
                    updated_at = ?
                WHERE lpo_valid_to IS NULL AND due_date IS NOT NULL
                """,
                (now,),
            )

        # Keep qty_kg columns coherent for migrated rows.
        conn.execute(
            """
            UPDATE contract_line_items
            SET expected_qty_kg = CASE
                WHEN LOWER(unit) IN ('kg', 'kgs', 'kilogram', 'kilograms') THEN CAST(ROUND(expected_qty, 0) AS INTEGER)
                WHEN LOWER(unit) IN ('mt', 'ton', 'tons', 'tonne', 'tonnes') THEN CAST(ROUND(expected_qty * 1000.0, 0) AS INTEGER)
                ELSE CAST(ROUND(expected_qty, 0) AS INTEGER)
            END
            WHERE expected_qty_kg = 0
            """
        )
        conn.execute(
            """
            UPDATE contracts
            SET expected_total_qty_kg = COALESCE((
                SELECT SUM(cli.expected_qty_kg)
                FROM contract_line_items cli
                WHERE cli.contract_id = contracts.contract_id
            ), 0)
            WHERE expected_total_qty_kg = 0
            """
        )
        conn.execute(
            """
            UPDATE contracts
            SET expected_total_qty = ROUND(expected_total_qty_kg / 1000.0, 3)
            WHERE expected_total_qty_kg > 0
            """
        )
        conn.execute(
            """
            UPDATE contract_line_items
            SET delivered_qty_kg = CASE
                WHEN LOWER(unit) IN ('kg', 'kgs', 'kilogram', 'kilograms') THEN CAST(ROUND(delivered_qty, 0) AS INTEGER)
                WHEN LOWER(unit) IN ('mt', 'ton', 'tons', 'tonne', 'tonnes') THEN CAST(ROUND(delivered_qty * 1000.0, 0) AS INTEGER)
                ELSE CAST(ROUND(delivered_qty, 0) AS INTEGER)
            END
            WHERE delivered_qty_kg = 0
            """
        )
        conn.execute(
            """
            UPDATE deliveries
            SET delivered_qty_kg = CASE
                WHEN LOWER(unit) IN ('kg', 'kgs', 'kilogram', 'kilograms') THEN CAST(ROUND(delivered_qty, 0) AS INTEGER)
                WHEN LOWER(unit) IN ('mt', 'ton', 'tons', 'tonne', 'tonnes') THEN CAST(ROUND(delivered_qty * 1000.0, 0) AS INTEGER)
                ELSE CAST(ROUND(delivered_qty, 0) AS INTEGER)
            END
            WHERE delivered_qty_kg = 0
            """
        )
        conn.execute(
            """
            UPDATE procurements
            SET quantity_kg = CASE
                WHEN LOWER(unit) IN ('kg', 'kgs', 'kilogram', 'kilograms') THEN CAST(ROUND(quantity, 0) AS INTEGER)
                WHEN LOWER(unit) IN ('mt', 'ton', 'tons', 'tonne', 'tonnes') THEN CAST(ROUND(quantity * 1000.0, 0) AS INTEGER)
                ELSE CAST(ROUND(quantity, 0) AS INTEGER)
            END
            WHERE quantity_kg = 0
            """
        )
        conn.execute(
            """
            UPDATE sales_lines
            SET quantity_kg = CASE
                WHEN LOWER(unit) IN ('kg', 'kgs', 'kilogram', 'kilograms') THEN CAST(ROUND(quantity, 0) AS INTEGER)
                WHEN LOWER(unit) IN ('mt', 'ton', 'tons', 'tonne', 'tonnes') THEN CAST(ROUND(quantity * 1000.0, 0) AS INTEGER)
                ELSE CAST(ROUND(quantity, 0) AS INTEGER)
            END
            WHERE quantity_kg = 0
            """
        )
        conn.execute(
            """
            UPDATE contract_line_items
            SET unit_price_basis = 'KG'
            WHERE unit_price_basis IS NULL OR TRIM(unit_price_basis) = ''
            """
        )
        conn.execute(
            """
            UPDATE deliveries
            SET unit_price_basis = 'KG'
            WHERE unit_price_basis IS NULL OR TRIM(unit_price_basis) = ''
            """
        )
        conn.execute(
            """
            UPDATE sales_lines
            SET unit_price_basis = 'KG'
            WHERE unit_price_basis IS NULL OR TRIM(unit_price_basis) = ''
            """
        )

    def _seed_parties(self, conn: sqlite3.Connection, config: RuntimeConfig) -> None:
        now = utc_now_iso_z()
        for party_id, entity in config.registry.entities.items():
            conn.execute(
                """
                INSERT INTO parties(
                    party_id, legal_name, code, tin, rc_number, address, city_state_country, website,
                    phone, email, bank_name, bank_account_name, bank_account_number, bank_currency, aliases_json,
                    created_at, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(party_id) DO UPDATE SET
                    legal_name=excluded.legal_name,
                    code=excluded.code,
                    tin=excluded.tin,
                    rc_number=excluded.rc_number,
                    address=excluded.address,
                    city_state_country=excluded.city_state_country,
                    website=excluded.website,
                    phone=excluded.phone,
                    email=excluded.email,
                    bank_name=excluded.bank_name,
                    bank_account_name=excluded.bank_account_name,
                    bank_account_number=excluded.bank_account_number,
                    bank_currency=excluded.bank_currency,
                    aliases_json=excluded.aliases_json,
                    updated_at=excluded.updated_at
                """,
                (
                    party_id,
                    entity.name,
                    entity.code,
                    entity.tin,
                    entity.rc_number,
                    entity.address,
                    entity.city_state_country,
                    entity.website,
                    entity.phone,
                    entity.email,
                    entity.bank.bank_name if entity.bank else "",
                    entity.bank.account_name if entity.bank else "",
                    entity.bank.account_number if entity.bank else "",
                    entity.bank.currency if entity.bank else "NGN",
                    json.dumps(entity.aliases or []),
                    now,
                    now,
                ),
            )

    def fetch_one(self, query: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute(query, params).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def fetch_all(self, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(query, params).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def resolve_policy_runtime(
        self,
        *,
        as_of_date: str,
        contract_id: str | None,
        master_contract_id: str | None,
        buyer_id: str | None,
        fallback_policy: dict[str, Any],
        fallback_version: str,
    ) -> dict[str, Any]:
        context = {
            "contract": contract_id or None,
            "master_contract": master_contract_id or None,
            "buyer": buyer_id or None,
            "global": None,
        }
        precedence = ["global", "buyer", "master_contract", "contract"]
        selected_sets: list[dict[str, Any]] = []
        fallback = fallback_policy if isinstance(fallback_policy, dict) else {}
        merged_policy: dict[str, Any] = _deep_merge_dicts({}, fallback)

        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            for scope in precedence:
                scope_ref = context[scope]
                row = conn.execute(
                    """
                    SELECT *
                    FROM policy_sets
                    WHERE is_active = 1
                      AND LOWER(policy_scope) = LOWER(?)
                      AND (
                        (? IS NULL AND (scope_ref_id IS NULL OR TRIM(scope_ref_id) = ''))
                        OR scope_ref_id = ?
                      )
                      AND (effective_from IS NULL OR effective_from <= ?)
                      AND (effective_to IS NULL OR effective_to >= ?)
                    ORDER BY
                      CASE WHEN effective_from IS NULL THEN 0 ELSE 1 END DESC,
                      effective_from DESC,
                      updated_at DESC,
                      created_at DESC,
                      version DESC,
                      policy_set_id ASC
                    LIMIT 1
                    """,
                    (scope, scope_ref, scope_ref, as_of_date, as_of_date),
                ).fetchone()
                if not row:
                    continue
                selected = dict(row)
                base_policy = _safe_json_dict(selected.get("policy_json"))
                scope_policy = _deep_merge_dicts({}, base_policy)
                override_rows = conn.execute(
                    """
                    SELECT *
                    FROM policy_overrides
                    WHERE policy_set_id = ?
                    ORDER BY
                      CASE LOWER(override_scope)
                        WHEN 'global' THEN 1
                        WHEN 'buyer' THEN 2
                        WHEN 'master_contract' THEN 3
                        WHEN 'contract' THEN 4
                        ELSE 5
                      END ASC,
                      key_path ASC,
                      policy_override_id ASC
                    """,
                    (selected["policy_set_id"],),
                ).fetchall()
                for raw_override in override_rows:
                    override = dict(raw_override)
                    override_scope = str(override.get("override_scope") or "global").strip().lower()
                    override_ref = str(override.get("scope_ref_id") or "").strip() or None
                    expected_ref = context.get(override_scope)
                    if override_scope == "global":
                        if override_ref not in {None, ""}:
                            continue
                    else:
                        if not expected_ref or override_ref != expected_ref:
                            continue
                    _set_path(scope_policy, str(override.get("key_path") or ""), _safe_json_value(override.get("override_value_json")))
                merged_policy = _deep_merge_dicts(merged_policy, scope_policy)
                selected_sets.append(
                    {
                        "scope": scope,
                        "scope_ref_id": scope_ref,
                        "policy_set_id": selected["policy_set_id"],
                        "version": str(selected.get("version") or ""),
                    }
                )

        if selected_sets:
            source_key = "|".join(
                f"{item['scope']}:{item['policy_set_id']}:{item['version']}"
                for item in selected_sets
            )
            version_key = "|".join(
                f"{item['scope']}={item['version']}"
                for item in selected_sets
            )
            return {
                "source": "db",
                "policy": merged_policy,
                "selected_sets": selected_sets,
                "policy_source_key": source_key,
                "policy_version": version_key,
            }

        return {
            "source": "config",
            "policy": fallback,
            "selected_sets": [],
            "policy_source_key": f"config:{fallback_version}",
            "policy_version": str(fallback_version or "config"),
        }

    def _resolve_tolerance_pct(self, config: RuntimeConfig, *, buyer_id: str, payload_override: Any = None) -> float:
        if payload_override not in (None, ""):
            return float(payload_override)
        over_cfg = (
            config.automation_thresholds.get("over_delivery", {})
            if isinstance(config.automation_thresholds, dict)
            else {}
        )
        buyer_overrides = over_cfg.get("buyer_overrides", {}) if isinstance(over_cfg.get("buyer_overrides"), dict) else {}
        if buyer_id in buyer_overrides:
            return float(buyer_overrides[buyer_id])
        return float(over_cfg.get("global_default_tolerance_pct", 5.0))

    def _qty_to_kg(self, qty: float, unit: str) -> int:
        unit_norm = (unit or "").strip().lower()
        if unit_norm in {"kg", "kgs", "kilogram", "kilograms"}:
            return int(round(qty))
        if unit_norm in {"mt", "ton", "tons", "tonne", "tonnes"}:
            return mt_to_kg_int(qty)
        return int(round(qty))

    def _normalize_unit_price_basis(self, basis: str | None, *, unit_hint: str = "") -> str:
        basis_norm = (basis or "").strip().upper()
        if not basis_norm:
            unit_norm = (unit_hint or "").strip().lower()
            basis_norm = "MT" if unit_norm in {"mt", "ton", "tons", "tonne", "tonnes"} else "KG"
        if basis_norm not in {"KG", "MT"}:
            raise ValueError("unit_price_basis must be KG or MT")
        return basis_norm

    def _gross_amount_from_kg(self, *, quantity_kg: int, unit_price: float, unit_price_basis: str) -> float:
        qty = Decimal(str(quantity_kg))
        price = Decimal(str(unit_price))
        basis_norm = self._normalize_unit_price_basis(unit_price_basis)
        unit_price_kg = price if basis_norm == "KG" else (price / Decimal("1000"))
        return float((qty * unit_price_kg).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    def create_contract(self, config: RuntimeConfig, payload: dict[str, Any], *, allow_placeholder_tin: bool) -> dict[str, Any]:
        contract_ref = str(payload.get("contract_ref") or payload.get("lpo_no") or "").strip()
        if not contract_ref:
            raise ValueError("contract_ref or lpo_no is required")

        lines = payload.get("lines") or payload.get("product_lines") or []
        if not isinstance(lines, list) or not lines:
            raise ValueError("lines[] is required")

        buyer_id = str(payload.get("buyer_id") or "").strip()
        if not buyer_id:
            raise ValueError("buyer_id is required")

        vendor_id = str(payload.get("vendor_of_record_id") or "").strip()
        if not vendor_id:
            raise ValueError("vendor_of_record_id is required")

        operator_id = str(payload.get("operator_id") or config.system_profile.operator_entity_id or "guildgate").strip()
        source_id = str(payload.get("source_id") or "").strip() or None
        processor_id = str(payload.get("processor_id") or "").strip() or None
        currency = str(payload.get("currency") or "NGN").strip().upper()
        issue_date = str(payload.get("issue_date") or utc_today_iso()).strip()
        due_date = str(payload.get("due_date") or "").strip() or None
        due_terms = str(payload.get("due_terms") or payload.get("terms") or "").strip() or None
        lpo_no = str(payload.get("lpo_no") or contract_ref).strip()
        lpo_date = str(payload.get("lpo_date") or "").strip() or None
        notes = str(payload.get("notes") or "").strip() or None
        tolerance_pct = self._resolve_tolerance_pct(
            config,
            buyer_id=buyer_id,
            payload_override=payload.get("over_delivery_tolerance_pct"),
        )
        lpo_valid_from = str(payload.get("lpo_valid_from") or issue_date).strip() or issue_date
        lpo_valid_to = str(payload.get("lpo_valid_to") or "").strip() or None
        master_contract_id = str(payload.get("master_contract_id") or "").strip() or None

        vendor = config.registry.get(vendor_id)
        ensure_vendor_tin(vendor.tin, allow_placeholder_tin=allow_placeholder_tin)
        lane = infer_lane(vendor_id)
        if lane not in {"A", "B"}:
            raise ValueError("Phase 1 supports lanes A/B only (vendor_of_record must be guildgate or ananta_flows)")

        expected_total_qty_input = payload.get("expected_total_qty")
        expected_total_qty_kg_input = payload.get("expected_total_qty_kg")
        expected_total_qty_unit_raw = payload.get("expected_total_qty_unit")
        expected_total_qty_unit = (
            str(expected_total_qty_unit_raw).strip().lower()
            if expected_total_qty_unit_raw not in (None, "")
            else ""
        )
        computed_total_value = 0.0
        normalized_lines: list[dict[str, Any]] = []
        running_qty_kg = 0
        for index, line in enumerate(lines, start=1):
            product_code = str(line.get("product_code") or "").strip().upper()
            description = str(line.get("description") or "").strip()
            if not product_code or not description:
                raise ValueError(f"line {index}: product_code and description are required")
            qty = float(line.get("expected_qty") or line.get("quantity") or 0.0)
            unit = str(line.get("unit") or "kgs").strip()
            qty_kg = self._qty_to_kg(qty, unit)
            unit_price = float(line.get("unit_price") or 0.0)
            unit_price_basis = self._normalize_unit_price_basis(
                str(line.get("unit_price_basis") or payload.get("unit_price_basis") or ""),
                unit_hint=unit,
            )
            computed_expected_value = self._gross_amount_from_kg(
                quantity_kg=qty_kg,
                unit_price=unit_price,
                unit_price_basis=unit_price_basis,
            )
            provided_expected_value = line.get("expected_value")
            if provided_expected_value in (None, ""):
                expected_value = computed_expected_value
            else:
                expected_value = float(provided_expected_value)
                if abs(expected_value - computed_expected_value) > 0.01:
                    raise ValueError(
                        f"line {index}: expected_value ({expected_value:.2f}) does not match "
                        f"computed qty_kg*unit_price ({computed_expected_value:.2f}) using unit_price_basis={unit_price_basis}"
                    )
            running_qty_kg += qty_kg
            computed_total_value += expected_value
            normalized_lines.append(
                {
                    "line_no": index,
                    "product_code": product_code,
                    "description": description,
                    "expected_qty": qty,
                    "expected_qty_kg": qty_kg,
                    "unit": unit,
                    "unit_price": unit_price,
                    "unit_price_basis": unit_price_basis,
                    "expected_value": expected_value,
                }
            )

        expected_total_qty_kg = 0
        if expected_total_qty_kg_input not in (None, ""):
            expected_total_qty_kg = int(round(float(expected_total_qty_kg_input)))
        elif expected_total_qty_input not in (None, ""):
            provided_qty = float(expected_total_qty_input)
            unit_hint = expected_total_qty_unit or str(normalized_lines[0]["unit"]).strip().lower()
            if unit_hint in {"kg", "kgs", "kilogram", "kilograms"}:
                expected_total_qty_kg = int(round(provided_qty))
            else:
                expected_total_qty_kg = mt_to_kg_int(provided_qty)
        if expected_total_qty_kg <= 0:
            expected_total_qty_kg = running_qty_kg
        if expected_total_qty_kg <= 0:
            raise ValueError("expected_total_qty_kg must be > 0")
        expected_total_qty_mt = float(kg_to_mt_decimal(expected_total_qty_kg))

        provided_total_value = payload.get("expected_total_value")
        if provided_total_value in (None, ""):
            expected_total_value = computed_total_value
        else:
            expected_total_value = float(provided_total_value)
            if abs(expected_total_value - computed_total_value) > 0.01:
                raise ValueError(
                    f"expected_total_value ({expected_total_value:.2f}) does not match computed lines total ({computed_total_value:.2f})"
                )

        now = utc_now_iso_z()
        contract_id = new_ulid()

        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO contracts(
                    contract_id, master_contract_id, contract_ref, lpo_no, lpo_date, buyer_id, vendor_of_record_id,
                    operator_id, source_id, processor_id, lane, currency, issue_date, lpo_valid_from, lpo_valid_to, lpo_state,
                    due_date, due_terms,
                    expected_total_qty, expected_total_qty_kg, expected_total_value, over_delivery_tolerance_pct, status, notes,
                    created_at, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    contract_id,
                    master_contract_id,
                    contract_ref,
                    lpo_no,
                    lpo_date,
                    buyer_id,
                    vendor_id,
                    operator_id,
                    source_id,
                    processor_id,
                    lane,
                    currency,
                    issue_date,
                    lpo_valid_from,
                    lpo_valid_to,
                    due_date,
                    due_terms,
                    expected_total_qty_mt,
                    expected_total_qty_kg,
                    expected_total_value,
                    tolerance_pct,
                    ContractStatus.OPEN.value,
                    notes,
                    now,
                    now,
                ),
            )

            for line in normalized_lines:
                contract_line_id = new_ulid()
                conn.execute(
                    """
                    INSERT INTO contract_line_items(
                        contract_line_id, contract_id, line_no, product_code, description,
                        expected_qty, delivered_qty, expected_qty_kg, delivered_qty_kg, unit, unit_price, unit_price_basis, expected_value, created_at, updated_at
                    )
                    VALUES(?, ?, ?, ?, ?, ?, 0, ?, 0, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        contract_line_id,
                        contract_id,
                        line["line_no"],
                        line["product_code"],
                        line["description"],
                        line["expected_qty"],
                        line["expected_qty_kg"],
                        line["unit"],
                        line["unit_price"],
                        line["unit_price_basis"],
                        line["expected_value"],
                        now,
                        now,
                    ),
                )

        return {
            "ok": True,
            "contract_id": contract_id,
            "contract_ref": contract_ref,
            "lpo_no": lpo_no,
            "lane": lane,
            "expected_total_qty": expected_total_qty_mt,
            "expected_total_qty_kg": expected_total_qty_kg,
            "expected_total_value": expected_total_value,
        }

    def add_delivery(self, payload: dict[str, Any]) -> dict[str, Any]:
        contract_id = str(payload.get("contract_id") or "").strip()
        if not contract_id:
            raise ValueError("contract_id is required")

        contract = self.fetch_one("SELECT * FROM contracts WHERE contract_id = ?", (contract_id,))
        if not contract:
            raise ValueError(f"Unknown contract_id: {contract_id}")
        if str(contract.get("lpo_state") or "ACTIVE").upper() in {"CANCELLED", "EXPIRED"}:
            raise ValueError("Cannot add/materialize delivery when contract LPO state is CANCELLED or EXPIRED")

        line_no = int(payload.get("line_no") or 1)
        line = self.fetch_one(
            "SELECT * FROM contract_line_items WHERE contract_id = ? AND line_no = ?",
            (contract_id, line_no),
        )
        if not line:
            raise ValueError(f"No contract line found for line_no={line_no}")

        delivery_id = new_ulid()
        run_id = str(payload.get("run_id") or "").strip()
        batch_id = str(payload.get("batch_id") or "").strip()
        ensure_run_and_batch(run_id=run_id, batch_id=batch_id)

        delivery_date = str(payload.get("delivery_date") or utc_today_iso()).strip()
        delivered_qty = float(payload.get("delivered_qty") or payload.get("quantity") or line["expected_qty"])
        unit = str(payload.get("unit") or line["unit"]).strip()
        delivered_qty_kg = self._qty_to_kg(delivered_qty, unit)
        unit_price = float(payload.get("unit_price") or line["unit_price"])
        unit_price_basis = self._normalize_unit_price_basis(
            str(payload.get("unit_price_basis") or line.get("unit_price_basis") or ""),
            unit_hint=unit,
        )
        gross_amount = float(
            payload.get("gross_amount")
            or self._gross_amount_from_kg(
                quantity_kg=delivered_qty_kg,
                unit_price=unit_price,
                unit_price_basis=unit_price_basis,
            )
        )
        tolerance_pct = float(contract["over_delivery_tolerance_pct"] or 5.0)
        override_reason = str(payload.get("force_over_delivery_reason") or "").strip() or None
        expected_qty_kg_for_check = int(line.get("expected_qty_kg") or 0)
        if expected_qty_kg_for_check <= 0:
            expected_qty_kg_for_check = self._qty_to_kg(float(line["expected_qty"]), str(line["unit"]))
        ensure_within_overdelivery_tolerance(
            delivered_qty=float(delivered_qty_kg),
            expected_qty=float(expected_qty_kg_for_check),
            tolerance_pct=tolerance_pct,
            force_reason=override_reason,
        )

        now = utc_now_iso_z()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO deliveries(
                    delivery_id, contract_id, contract_line_id, delivery_ref, run_id, batch_id, delivery_date,
                    delivered_qty, delivered_qty_kg, unit, unit_price, unit_price_basis, gross_amount, truck_no, driver_name, driver_phone, notes,
                    status, over_delivery_override_reason, created_at, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    delivery_id,
                    contract_id,
                    line["contract_line_id"],
                    payload.get("delivery_ref"),
                    run_id,
                    batch_id,
                    delivery_date,
                    delivered_qty,
                    delivered_qty_kg,
                    unit,
                    unit_price,
                    unit_price_basis,
                    gross_amount,
                    payload.get("truck_no"),
                    payload.get("driver_name"),
                    payload.get("driver_phone"),
                    payload.get("notes"),
                    DeliveryStatus.PLANNED.value,
                    override_reason,
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO procurements(
                    procurement_id, contract_id, delivery_id, source_id, processor_id, product_code, quantity,
                    quantity_kg, unit, unit_cost, gross_amount, run_id, batch_id, document_ref, created_at, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_ulid(),
                    contract_id,
                    delivery_id,
                    contract.get("source_id"),
                    contract.get("processor_id"),
                    line["product_code"],
                    delivered_qty,
                    delivered_qty_kg,
                    unit,
                    unit_price,
                    gross_amount,
                    run_id,
                    batch_id,
                    payload.get("procurement_doc_ref"),
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                UPDATE contract_line_items
                SET delivered_qty = delivered_qty + ?,
                    delivered_qty_kg = delivered_qty_kg + ?,
                    updated_at = ?
                WHERE contract_line_id = ?
                """,
                (delivered_qty, delivered_qty_kg, now, line["contract_line_id"]),
            )

            planned_delivery_id = str(payload.get("planned_delivery_id") or "").strip()
            if planned_delivery_id:
                conn.execute(
                    """
                    UPDATE planned_deliveries
                    SET delivery_id = ?, materialized_qty_kg = ?, delivered_qty_kg = ?,
                        status = 'SCHEDULED', updated_at = ?
                    WHERE planned_delivery_id = ?
                    """,
                    (delivery_id, delivered_qty_kg, delivered_qty_kg, now, planned_delivery_id),
                )
            self.refresh_contract_status(conn, contract_id)
        return {
            "ok": True,
            "delivery_id": delivery_id,
            "contract_id": contract_id,
            "status": DeliveryStatus.PLANNED.value,
        }

    def mark_delivery_status(self, delivery_id: str, target: DeliveryStatus) -> dict[str, Any]:
        existing = self.fetch_one("SELECT * FROM deliveries WHERE delivery_id = ?", (delivery_id,))
        if not existing:
            raise ValueError(f"Unknown delivery_id: {delivery_id}")
        validate_delivery_transition(existing["status"], target.value)
        now = utc_now_iso_z()
        timestamp_column = {
            DeliveryStatus.DISPATCHED: "dispatched_at",
            DeliveryStatus.DELIVERED: "delivered_at",
            DeliveryStatus.INVOICED: "invoiced_at",
            DeliveryStatus.PAID: "paid_at",
        }[target]
        with self.transaction() as conn:
            conn.execute(
                f"""
                UPDATE deliveries
                SET status = ?, {timestamp_column} = ?, updated_at = ?
                WHERE delivery_id = ?
                """,
                (target.value, now, now, delivery_id),
            )
            conn.execute(
                """
                UPDATE planned_deliveries
                SET status = ?, updated_at = ?
                WHERE delivery_id = ?
                """,
                (target.value, now, delivery_id),
            )
        return {"ok": True, "delivery_id": delivery_id, "status": target.value}

    def create_parties_snapshot(
        self,
        conn: sqlite3.Connection,
        *,
        buyer_id: str,
        vendor_id: str,
        operator_id: str,
    ) -> str:
        buyer = conn.execute("SELECT * FROM parties WHERE party_id = ?", (buyer_id,)).fetchone()
        vendor = conn.execute("SELECT * FROM parties WHERE party_id = ?", (vendor_id,)).fetchone()
        operator = conn.execute("SELECT * FROM parties WHERE party_id = ?", (operator_id,)).fetchone()
        if not buyer or not vendor or not operator:
            raise ValueError("Snapshot requires buyer/vendor/operator parties")
        snapshot_id = new_ulid()
        payload = {
            "buyer": dict(buyer),
            "vendor_of_record": dict(vendor),
            "operator": dict(operator),
        }
        now = utc_now_iso_z()
        conn.execute(
            """
            INSERT INTO parties_snapshot(
                snapshot_id, buyer_id, buyer_name, buyer_tin, buyer_rc_number,
                vendor_of_record_id, vendor_of_record_name, vendor_of_record_tin, vendor_of_record_rc_number,
                operator_id, operator_name, operator_tin, operator_rc_number, payload_json, created_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                buyer["party_id"],
                buyer["legal_name"],
                buyer["tin"],
                buyer["rc_number"],
                vendor["party_id"],
                vendor["legal_name"],
                vendor["tin"],
                vendor["rc_number"],
                operator["party_id"],
                operator["legal_name"],
                operator["tin"],
                operator["rc_number"],
                json.dumps(payload, sort_keys=True),
                now,
            ),
        )
        return snapshot_id

    def next_sequence(self, conn: sqlite3.Connection, *, vendor_of_record_id: str, doc_type: str, year: int) -> int:
        row = conn.execute(
            """
            SELECT current_value FROM numbering_sequences
            WHERE vendor_of_record_id = ? AND doc_type = ? AND year = ?
            """,
            (vendor_of_record_id, doc_type, year),
        ).fetchone()
        now = utc_now_iso_z()
        if not row:
            next_value = 1
            conn.execute(
                """
                INSERT INTO numbering_sequences(vendor_of_record_id, doc_type, year, current_value, updated_at)
                VALUES(?, ?, ?, ?, ?)
                """,
                (vendor_of_record_id, doc_type, year, next_value, now),
            )
            return next_value
        next_value = int(row["current_value"]) + 1
        conn.execute(
            """
            UPDATE numbering_sequences
            SET current_value = ?, updated_at = ?
            WHERE vendor_of_record_id = ? AND doc_type = ? AND year = ?
            """,
            (next_value, now, vendor_of_record_id, doc_type, year),
        )
        return next_value

    def format_doc_number(self, doc_type: DocumentType, *, invoice_no: str, year: int, sequence: int, batch_id: str = "") -> str:
        if doc_type == DocumentType.INVOICE:
            return f"INV-{year}-{sequence:04d}"
        if doc_type == DocumentType.WAYBILL:
            return f"WB-{invoice_no}-01"
        if doc_type == DocumentType.WEIGHING_TICKET:
            return f"WT-WB-{invoice_no}-01-01"
        if doc_type == DocumentType.COA:
            return f"COA-{year}-{sequence:04d}"
        if doc_type == DocumentType.RECEIPT:
            return f"RCPT-{invoice_no}-{sequence:02d}"
        raise ValueError(f"Unsupported doc_type: {doc_type}")

    def save_idempotent_response(self, conn: sqlite3.Connection, *, command_name: str, idempotency_key: str, response: dict[str, Any]) -> None:
        conn.execute(
            """
            INSERT INTO command_idempotency(command_name, idempotency_key, response_json, created_at)
            VALUES(?, ?, ?, ?)
            """,
            (command_name, idempotency_key, json.dumps(response, sort_keys=True), utc_now_iso_z()),
        )

    def find_idempotent_response(self, conn: sqlite3.Connection, *, command_name: str, idempotency_key: str) -> dict[str, Any] | None:
        row = conn.execute(
            """
            SELECT response_json
            FROM command_idempotency
            WHERE command_name = ? AND idempotency_key = ?
            """,
            (command_name, idempotency_key),
        ).fetchone()
        if not row:
            return None
        return json.loads(row["response_json"])

    def set_as_of_date(self, as_of_date: str) -> None:
        with self.transaction() as conn:
            conn.execute("UPDATE report_context SET as_of_date = ? WHERE id = 1", (as_of_date,))

    def get_automation_run_by_idempotency(self, idempotency_key: str) -> dict[str, Any] | None:
        return self.fetch_one(
            "SELECT * FROM automation_runs WHERE idempotency_key = ?",
            (idempotency_key,),
        )

    def get_automation_run(self, run_id: str) -> dict[str, Any] | None:
        return self.fetch_one("SELECT * FROM automation_runs WHERE run_id = ?", (run_id,))

    def create_automation_run(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        idempotency_key: str,
        as_of_date: str,
        dry_run: bool,
        input_payload: dict[str, Any],
    ) -> None:
        now = utc_now_iso_z()
        conn.execute(
            """
            INSERT INTO automation_runs(
                run_id, idempotency_key, status, dry_run, as_of_date, input_json,
                started_at, created_at, updated_at
            )
            VALUES(?, ?, 'RUNNING', ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                idempotency_key,
                1 if dry_run else 0,
                as_of_date,
                json.dumps(input_payload, sort_keys=True),
                now,
                now,
                now,
            ),
        )

    def complete_automation_run(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        status: str,
        normalized_payload: dict[str, Any],
        metrics: dict[str, Any],
        started_at: str,
        failure_reason: str | None = None,
    ) -> None:
        completed_at = utc_now_iso_z()
        conn.execute(
            """
            UPDATE automation_runs
            SET status = ?,
                normalized_input_json = ?,
                metrics_json = ?,
                failure_reason = ?,
                completed_at = ?,
                duration_seconds = (julianday(?) - julianday(?)) * 86400.0,
                updated_at = ?
            WHERE run_id = ?
            """,
            (
                status,
                json.dumps(normalized_payload, sort_keys=True),
                json.dumps(metrics, sort_keys=True),
                failure_reason,
                completed_at,
                completed_at,
                started_at,
                completed_at,
                run_id,
            ),
        )

    def add_automation_decision(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        stage: str,
        field_name: str,
        required_flag: bool,
        proposed_value: Any,
        source_type: str,
        source_ref: str,
        confidence: float,
        decision: str,
        reason_code: str,
        rule_path: str,
    ) -> str:
        decision_id = new_ulid()
        conn.execute(
            """
            INSERT INTO automation_decisions(
                decision_id, run_id, stage, field_name, required_flag,
                proposed_value, source_type, source_ref, confidence, decision, reason_code, rule_path, created_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                decision_id,
                run_id,
                stage,
                field_name,
                1 if required_flag else 0,
                json.dumps(proposed_value, sort_keys=True) if isinstance(proposed_value, (dict, list)) else (None if proposed_value is None else str(proposed_value)),
                source_type,
                source_ref,
                float(confidence),
                decision,
                reason_code,
                rule_path,
                utc_now_iso_z(),
            ),
        )
        return decision_id

    def add_exception(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        stage: str,
        exception_type: str,
        severity: str,
        field_name: str,
        proposed_value: Any,
        reason: str,
        suggestions: list[dict[str, Any]] | None = None,
    ) -> str:
        exception_id = new_ulid()
        conn.execute(
            """
            INSERT INTO exception_queue(
                exception_id, run_id, stage, exception_type, severity, field_name,
                proposed_value, reason, suggestions_json, status, created_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?)
            """,
            (
                exception_id,
                run_id,
                stage,
                exception_type,
                severity,
                field_name,
                json.dumps(proposed_value, sort_keys=True) if isinstance(proposed_value, (dict, list)) else (None if proposed_value is None else str(proposed_value)),
                reason,
                json.dumps(suggestions or [], sort_keys=True),
                utc_now_iso_z(),
            ),
        )
        return exception_id

    def list_exceptions(self, *, run_id: str | None = None, status: str = "OPEN") -> list[dict[str, Any]]:
        if run_id:
            return self.fetch_all(
                "SELECT * FROM exception_queue WHERE run_id = ? AND status = ? ORDER BY created_at ASC",
                (run_id, status),
            )
        return self.fetch_all(
            "SELECT * FROM exception_queue WHERE status = ? ORDER BY created_at ASC",
            (status,),
        )

    def resolve_exception(self, *, exception_id: str, value: str, note: str) -> dict[str, Any]:
        now = utc_now_iso_z()
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM exception_queue WHERE exception_id = ?", (exception_id,)).fetchone()
            if not row:
                raise ValueError(f"Unknown exception_id: {exception_id}")
            if row["status"] == "RESOLVED":
                return dict(row)
            conn.execute(
                """
                UPDATE exception_queue
                SET status = 'RESOLVED',
                    resolved_value = ?,
                    resolution_note = ?,
                    resolved_at = ?
                WHERE exception_id = ?
                """,
                (value, note, now, exception_id),
            )
            return dict(conn.execute("SELECT * FROM exception_queue WHERE exception_id = ?", (exception_id,)).fetchone())

    def add_gate_evaluation(
        self,
        conn: sqlite3.Connection,
        *,
        autonomy_run_id: str,
        contract_id: str | None,
        delivery_id: str | None,
        planned_delivery_id: str | None,
        gate_name: str,
        subject_type: str,
        subject_id: str,
        as_of_date: str,
        status: str,
        score: float | None,
        reason_code: str,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        gate_evaluation_id = new_ulid()
        now = utc_now_iso_z()
        payload = json.dumps(details or {}, sort_keys=True)
        conn.execute(
            """
            INSERT INTO gate_evaluations(
                gate_evaluation_id, autonomy_run_id, contract_id, delivery_id, planned_delivery_id,
                gate_name, subject_type, subject_id, as_of_date, status, score, reason_code,
                details_json, evaluated_at, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                gate_evaluation_id,
                autonomy_run_id,
                contract_id,
                delivery_id,
                planned_delivery_id,
                gate_name,
                subject_type,
                subject_id,
                as_of_date,
                status,
                score,
                reason_code,
                payload,
                now,
                now,
            ),
        )
        return {
            "gate_evaluation_id": gate_evaluation_id,
            "gate_name": gate_name,
            "status": status,
            "score": score,
            "reason_code": reason_code,
            "details_json": payload,
        }

    def create_or_get_action_intent(
        self,
        conn: sqlite3.Connection,
        *,
        autonomy_run_id: str,
        intent_type: str,
        contract_id: str | None,
        delivery_id: str | None,
        planned_delivery_id: str | None,
        as_of_date: str,
        scheduled_at: str | None,
        policy_version: str | None,
        payload: dict[str, Any] | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        row = conn.execute(
            "SELECT * FROM action_intents WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if row:
            return dict(row)
        now = utc_now_iso_z()
        action_intent_id = new_ulid()
        conn.execute(
            """
            INSERT INTO action_intents(
                action_intent_id, autonomy_run_id, intent_type, contract_id, delivery_id, planned_delivery_id,
                as_of_date, scheduled_at, status, policy_version, payload_json, idempotency_key, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, ?, ?)
            """,
            (
                action_intent_id,
                autonomy_run_id,
                intent_type,
                contract_id,
                delivery_id,
                planned_delivery_id,
                as_of_date,
                scheduled_at,
                policy_version,
                json.dumps(payload or {}, sort_keys=True),
                idempotency_key,
                now,
                now,
            ),
        )
        row = conn.execute("SELECT * FROM action_intents WHERE action_intent_id = ?", (action_intent_id,)).fetchone()
        return dict(row)

    def get_action_intent(self, action_intent_id: str) -> dict[str, Any] | None:
        return self.fetch_one("SELECT * FROM action_intents WHERE action_intent_id = ?", (action_intent_id,))

    def list_action_intents(
        self,
        *,
        contract_id: str | None = None,
        as_of_date: str | None = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM action_intents WHERE 1=1"
        params: list[Any] = []
        if contract_id:
            sql += " AND contract_id = ?"
            params.append(contract_id)
        if as_of_date:
            sql += " AND as_of_date = ?"
            params.append(as_of_date)
        sql += " ORDER BY created_at ASC"
        return self.fetch_all(sql, tuple(params))

    def set_action_intent_status(
        self,
        conn: sqlite3.Connection,
        *,
        action_intent_id: str,
        status: str,
    ) -> None:
        conn.execute(
            "UPDATE action_intents SET status = ?, updated_at = ? WHERE action_intent_id = ?",
            (status, utc_now_iso_z(), action_intent_id),
        )

    def add_action_execution(
        self,
        conn: sqlite3.Connection,
        *,
        action_intent_id: str,
        status: str,
        idempotency_key: str,
        request_payload: dict[str, Any] | None,
        response_payload: dict[str, Any] | None,
        error_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        row = conn.execute(
            "SELECT * FROM action_executions WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if row:
            return dict(row)
        current = conn.execute(
            "SELECT COALESCE(MAX(execution_no), 0) AS max_no FROM action_executions WHERE action_intent_id = ?",
            (action_intent_id,),
        ).fetchone()
        execution_no = int(current["max_no"] or 0) + 1
        now = utc_now_iso_z()
        action_execution_id = new_ulid()
        conn.execute(
            """
            INSERT INTO action_executions(
                action_execution_id, action_intent_id, execution_no, status, idempotency_key,
                request_json, response_json, error_json, executed_at, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                action_execution_id,
                action_intent_id,
                execution_no,
                status,
                idempotency_key,
                json.dumps(request_payload or {}, sort_keys=True),
                json.dumps(response_payload or {}, sort_keys=True),
                json.dumps(error_payload, sort_keys=True) if error_payload else None,
                now,
                now,
            ),
        )
        row = conn.execute(
            "SELECT * FROM action_executions WHERE action_execution_id = ?",
            (action_execution_id,),
        ).fetchone()
        return dict(row)

    def create_or_get_exception_case(
        self,
        conn: sqlite3.Connection,
        *,
        autonomy_run_id: str,
        action_intent_id: str | None,
        contract_id: str | None,
        delivery_id: str | None,
        planned_delivery_id: str | None,
        case_type: str,
        severity: str,
        reason_code: str,
        details: dict[str, Any] | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        row = conn.execute(
            "SELECT * FROM exception_cases WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if row:
            return dict(row)
        now = utc_now_iso_z()
        exception_case_id = new_ulid()
        conn.execute(
            """
            INSERT INTO exception_cases(
                exception_case_id, autonomy_run_id, action_intent_id, contract_id, delivery_id, planned_delivery_id,
                case_type, severity, status, reason_code, details_json, idempotency_key, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?)
            """,
            (
                exception_case_id,
                autonomy_run_id,
                action_intent_id,
                contract_id,
                delivery_id,
                planned_delivery_id,
                case_type,
                severity,
                reason_code,
                json.dumps(details or {}, sort_keys=True),
                idempotency_key,
                now,
                now,
            ),
        )
        row = conn.execute("SELECT * FROM exception_cases WHERE exception_case_id = ?", (exception_case_id,)).fetchone()
        return dict(row)

    def list_exception_cases(self, *, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            return self.fetch_all(
                "SELECT * FROM exception_cases WHERE status = ? ORDER BY created_at ASC",
                (status,),
            )
        return self.fetch_all("SELECT * FROM exception_cases ORDER BY created_at ASC")

    def get_exception_case(self, exception_case_id: str) -> dict[str, Any] | None:
        return self.fetch_one("SELECT * FROM exception_cases WHERE exception_case_id = ?", (exception_case_id,))

    def add_human_decision(
        self,
        conn: sqlite3.Connection,
        *,
        exception_case_id: str,
        user_id: str | None,
        decision: str,
        reason: str,
        payload: dict[str, Any] | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        row = conn.execute(
            "SELECT * FROM human_decisions WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if row:
            return dict(row)
        now = utc_now_iso_z()
        human_decision_id = new_ulid()
        conn.execute(
            """
            INSERT INTO human_decisions(
                human_decision_id, exception_case_id, decided_by_user_id, decision, reason,
                decision_payload_json, idempotency_key, decided_at, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                human_decision_id,
                exception_case_id,
                user_id,
                decision,
                reason,
                json.dumps(payload or {}, sort_keys=True),
                idempotency_key,
                now,
                now,
            ),
        )
        return dict(
            conn.execute(
                "SELECT * FROM human_decisions WHERE human_decision_id = ?",
                (human_decision_id,),
            ).fetchone()
        )

    def resolve_exception_case(
        self,
        conn: sqlite3.Connection,
        *,
        exception_case_id: str,
        status: str,
    ) -> dict[str, Any]:
        now = utc_now_iso_z()
        conn.execute(
            """
            UPDATE exception_cases
            SET status = ?, resolved_at = CASE WHEN ? = 'RESOLVED' THEN ? ELSE resolved_at END, updated_at = ?
            WHERE exception_case_id = ?
            """,
            (status, status, now, now, exception_case_id),
        )
        row = conn.execute(
            "SELECT * FROM exception_cases WHERE exception_case_id = ?",
            (exception_case_id,),
        ).fetchone()
        return dict(row) if row else {}

    def append_event(
        self,
        conn: sqlite3.Connection,
        *,
        entity_type: str,
        entity_id: str,
        event_type: str,
        as_of_date: str | None,
        payload: dict[str, Any] | None,
        source: str,
    ) -> dict[str, Any]:
        event_id = new_ulid()
        now = utc_now_iso_z()
        conn.execute(
            """
            INSERT INTO event_log(
                event_id, entity_type, entity_id, event_type, as_of_date, payload_json, source, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                entity_type,
                entity_id,
                event_type,
                as_of_date,
                json.dumps(payload or {}, sort_keys=True),
                source,
                now,
            ),
        )
        return {
            "event_id": event_id,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "as_of_date": as_of_date,
        }

    def list_active_transport_assignments(self, *, as_of_date: str) -> list[dict[str, Any]]:
        return self.fetch_all(
            """
            SELECT
              a.assignment_id,
              a.transport_truck_id,
              a.transport_driver_id,
              a.is_primary,
              a.effective_from,
              a.effective_to,
              t.transport_partner_id,
              t.truck_no,
              t.status AS truck_status,
              d.full_name AS driver_name,
              d.phone AS driver_phone,
              d.status AS driver_status,
              p.legal_name AS partner_name
            FROM truck_driver_assignments a
            JOIN transport_trucks t ON t.transport_truck_id = a.transport_truck_id
            JOIN transport_drivers d ON d.transport_driver_id = a.transport_driver_id
            LEFT JOIN transport_partners p ON p.transport_partner_id = t.transport_partner_id
            WHERE a.effective_from <= ?
              AND (a.effective_to IS NULL OR a.effective_to >= ?)
              AND t.status = 'ACTIVE'
              AND d.status = 'ACTIVE'
            ORDER BY a.is_primary DESC, a.effective_from DESC, a.assignment_id ASC
            """,
            (as_of_date, as_of_date),
        )

    def list_transport_aliases(self, *, entity_type: str, entity_id: str) -> list[dict[str, Any]]:
        return self.fetch_all(
            """
            SELECT * FROM transport_aliases
            WHERE entity_type = ? AND entity_id = ?
            ORDER BY created_at ASC
            """,
            (entity_type, entity_id),
        )

    def transport_compliance_valid(self, *, entity_type: str, entity_id: str, as_of_date: str) -> tuple[bool, str]:
        rows = self.fetch_all(
            """
            SELECT * FROM transport_compliance_docs
            WHERE entity_type = ? AND entity_id = ? AND status = 'ACTIVE'
            ORDER BY created_at DESC
            """,
            (entity_type, entity_id),
        )
        if not rows:
            return True, "no_compliance_docs"
        for row in rows:
            valid_from = str(row.get("valid_from") or "").strip()
            valid_to = str(row.get("valid_to") or "").strip()
            if valid_from and as_of_date < valid_from:
                continue
            if valid_to and as_of_date > valid_to:
                continue
            return True, "compliance_valid"
        return False, "compliance_expired"

    def upsert_delivery_transport_suggestion(
        self,
        conn: sqlite3.Connection,
        *,
        delivery_id: str | None,
        planned_delivery_id: str | None,
        entity_type: str,
        candidate_entity_id: str,
        candidate_label: str,
        confidence: float,
        explanation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = utc_now_iso_z()
        row = conn.execute(
            """
            SELECT * FROM delivery_transport_suggestions
            WHERE ((planned_delivery_id = ?) OR (planned_delivery_id IS NULL AND ? IS NULL))
              AND entity_type = ?
              AND candidate_entity_id = ?
            """,
            (planned_delivery_id, planned_delivery_id, entity_type, candidate_entity_id),
        ).fetchone()
        if row:
            conn.execute(
                """
                UPDATE delivery_transport_suggestions
                SET delivery_id = COALESCE(?, delivery_id),
                    candidate_label = ?,
                    confidence = ?,
                    explanation_json = ?,
                    created_at = ?
                WHERE suggestion_id = ?
                """,
                (
                    delivery_id,
                    candidate_label,
                    float(confidence),
                    json.dumps(explanation or {}, sort_keys=True),
                    now,
                    row["suggestion_id"],
                ),
            )
            updated = conn.execute(
                "SELECT * FROM delivery_transport_suggestions WHERE suggestion_id = ?",
                (row["suggestion_id"],),
            ).fetchone()
            return dict(updated)
        suggestion_id = new_ulid()
        conn.execute(
            """
            INSERT INTO delivery_transport_suggestions(
                suggestion_id, delivery_id, planned_delivery_id, entity_type, candidate_entity_id, candidate_label,
                confidence, explanation_json, accepted, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
            """,
            (
                suggestion_id,
                delivery_id,
                planned_delivery_id,
                entity_type,
                candidate_entity_id,
                candidate_label,
                float(confidence),
                json.dumps(explanation or {}, sort_keys=True),
                now,
            ),
        )
        row = conn.execute(
            "SELECT * FROM delivery_transport_suggestions WHERE suggestion_id = ?",
            (suggestion_id,),
        ).fetchone()
        return dict(row)

    def mark_transport_suggestion_feedback(
        self,
        conn: sqlite3.Connection,
        *,
        suggestion_ids: list[str],
        accepted: bool,
    ) -> None:
        if not suggestion_ids:
            return
        placeholders = ",".join("?" for _ in suggestion_ids)
        conn.execute(
            f"UPDATE delivery_transport_suggestions SET accepted = ? WHERE suggestion_id IN ({placeholders})",
            [1 if accepted else 0, *suggestion_ids],
        )

    def upsert_delivery_transport_snapshot(
        self,
        conn: sqlite3.Connection,
        *,
        delivery_id: str,
        planned_delivery_id: str | None,
        transport_partner_id: str | None,
        transport_truck_id: str | None,
        transport_driver_id: str | None,
        partner_name: str | None,
        truck_no: str | None,
        driver_name: str | None,
        driver_phone: str | None,
        source_type: str,
        source_ref: str,
        confidence: float | None,
        reason_code: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = utc_now_iso_z()
        existing = conn.execute(
            "SELECT * FROM delivery_transport_snapshot WHERE delivery_id = ?",
            (delivery_id,),
        ).fetchone()
        if existing:
            return dict(existing)
        snapshot_id = new_ulid()
        conn.execute(
            """
            INSERT INTO delivery_transport_snapshot(
                snapshot_id, delivery_id, planned_delivery_id, transport_partner_id, transport_truck_id, transport_driver_id,
                partner_name, truck_no, driver_name, driver_phone, source_type, source_ref, confidence, reason_code, payload_json, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                delivery_id,
                planned_delivery_id,
                transport_partner_id,
                transport_truck_id,
                transport_driver_id,
                partner_name,
                truck_no,
                driver_name,
                driver_phone,
                source_type,
                source_ref,
                confidence,
                reason_code,
                json.dumps(payload or {}, sort_keys=True),
                now,
            ),
        )
        row = conn.execute(
            "SELECT * FROM delivery_transport_snapshot WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()
        return dict(row)

    def update_delivery_transport_fields(
        self,
        conn: sqlite3.Connection,
        *,
        delivery_id: str,
        truck_no: str | None,
        driver_name: str | None,
        driver_phone: str | None,
    ) -> None:
        conn.execute(
            """
            UPDATE deliveries
            SET truck_no = COALESCE(NULLIF(TRIM(truck_no), ''), ?),
                driver_name = COALESCE(NULLIF(TRIM(driver_name), ''), ?),
                driver_phone = COALESCE(NULLIF(TRIM(driver_phone), ''), ?),
                updated_at = ?
            WHERE delivery_id = ?
            """,
            (truck_no, driver_name, driver_phone, utc_now_iso_z(), delivery_id),
        )

    def add_decision_feature(
        self,
        conn: sqlite3.Connection,
        *,
        exception_case_id: str,
        feature_key: str,
        feature_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        existing = conn.execute(
            "SELECT * FROM decision_features WHERE exception_case_id = ? AND feature_key = ?",
            (exception_case_id, feature_key),
        ).fetchone()
        now = utc_now_iso_z()
        feature_json = json.dumps(feature_payload or {}, sort_keys=True)
        if existing:
            conn.execute(
                "UPDATE decision_features SET feature_json = ?, created_at = ? WHERE decision_feature_id = ?",
                (feature_json, now, existing["decision_feature_id"]),
            )
            updated = conn.execute(
                "SELECT * FROM decision_features WHERE decision_feature_id = ?",
                (existing["decision_feature_id"],),
            ).fetchone()
            return dict(updated)
        decision_feature_id = new_ulid()
        conn.execute(
            """
            INSERT INTO decision_features(decision_feature_id, exception_case_id, feature_key, feature_json, created_at)
            VALUES(?, ?, ?, ?, ?)
            """,
            (decision_feature_id, exception_case_id, feature_key, feature_json, now),
        )
        row = conn.execute(
            "SELECT * FROM decision_features WHERE decision_feature_id = ?",
            (decision_feature_id,),
        ).fetchone()
        return dict(row)

    def add_decision_outcome(
        self,
        conn: sqlite3.Connection,
        *,
        exception_case_id: str,
        human_decision_id: str | None,
        outcome_label: str,
        outcome_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        decision_outcome_id = new_ulid()
        now = utc_now_iso_z()
        conn.execute(
            """
            INSERT INTO decision_outcomes(
                decision_outcome_id, exception_case_id, human_decision_id, outcome_label, outcome_json, created_at
            ) VALUES(?, ?, ?, ?, ?, ?)
            """,
            (
                decision_outcome_id,
                exception_case_id,
                human_decision_id,
                outcome_label,
                json.dumps(outcome_payload or {}, sort_keys=True),
                now,
            ),
        )
        row = conn.execute(
            "SELECT * FROM decision_outcomes WHERE decision_outcome_id = ?",
            (decision_outcome_id,),
        ).fetchone()
        return dict(row)

    def export_view_csv(self, view_name: str, out_path: Path) -> dict[str, Any]:
        rows = self.fetch_all(f"SELECT * FROM {view_name}")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", newline="", encoding="utf-8") as handle:
            if rows:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
                writer.writeheader()
                writer.writerows(rows)
            else:
                handle.write("")
        return {"view": view_name, "rows": len(rows), "path": str(out_path)}

    def link_document_to_sales_lines(
        self,
        conn: sqlite3.Connection,
        *,
        doc_id: str,
        sales_transaction_id: str,
        delivery_id: str,
    ) -> None:
        rows = conn.execute(
            "SELECT sales_line_id FROM sales_lines WHERE sales_transaction_id = ?",
            (sales_transaction_id,),
        ).fetchall()
        now = utc_now_iso_z()
        for row in rows:
            conn.execute(
                """
                INSERT INTO document_sales_links(link_id, doc_id, sales_transaction_id, sales_line_id, delivery_id, created_at)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (new_ulid(), doc_id, sales_transaction_id, row["sales_line_id"], delivery_id, now),
            )

    def refresh_contract_status(self, conn: sqlite3.Connection, contract_id: str) -> None:
        contract = conn.execute(
            "SELECT lpo_state FROM contracts WHERE contract_id = ?",
            (contract_id,),
        ).fetchone()
        if not contract:
            return
        expected = conn.execute(
            """
            SELECT COALESCE(SUM(expected_qty_kg), 0) AS qty_kg
            FROM contract_line_items
            WHERE contract_id = ?
            """,
            (contract_id,),
        ).fetchone()
        delivered = conn.execute(
            """
            SELECT COALESCE(SUM(delivered_qty_kg), 0) AS qty_kg
            FROM deliveries
            WHERE contract_id = ? AND status IN ('DELIVERED', 'INVOICED', 'PAID')
            """,
            (contract_id,),
        ).fetchone()
        outstanding = conn.execute(
            """
            SELECT COALESCE(SUM(outstanding_balance), 0) AS outstanding
            FROM drep_sales
            WHERE contract_id = ?
            """,
            (contract_id,),
        ).fetchone()
        status = derive_contract_status(
            expected_qty=float((expected or {"qty_kg": 0})["qty_kg"] or 0.0),
            delivered_qty=float((delivered or {"qty_kg": 0})["qty_kg"] or 0.0),
            total_outstanding=float((outstanding or {"outstanding": 0})["outstanding"] or 0.0),
            cancelled=str(contract["lpo_state"] or "").upper() == "CANCELLED",
        )
        now = utc_now_iso_z()
        conn.execute(
            "UPDATE contracts SET status = ?, updated_at = ? WHERE contract_id = ?",
            (status.value, now, contract_id),
        )

    def refresh_lpo_states(self, *, as_of_date: str) -> dict[str, Any]:
        updated = 0
        with self.transaction() as conn:
            rows = conn.execute(
                """
                SELECT contract_id, lpo_state, lpo_valid_to, cancelled_at, closed_at, expired_at
                FROM contracts
                """
            ).fetchall()
            for row in rows:
                current_state = str(row["lpo_state"] or "ACTIVE").upper()
                target_state = current_state
                expired_at = row["expired_at"]
                if row["cancelled_at"] is not None or current_state == "CANCELLED":
                    target_state = "CANCELLED"
                elif row["closed_at"] is not None or current_state == "CLOSED":
                    target_state = "CLOSED"
                else:
                    valid_to = str(row["lpo_valid_to"] or "").strip()
                    if valid_to and as_of_date > valid_to:
                        target_state = "EXPIRED"
                        if expired_at is None:
                            expired_at = f"{as_of_date}T00:00:00Z"
                    else:
                        target_state = "ACTIVE"
                        expired_at = None
                if target_state != current_state or expired_at != row["expired_at"]:
                    conn.execute(
                        """
                        UPDATE contracts
                        SET lpo_state = ?, expired_at = ?, updated_at = ?
                        WHERE contract_id = ?
                        """,
                        (target_state, expired_at, utc_now_iso_z(), row["contract_id"]),
                    )
                    self.refresh_contract_status(conn, str(row["contract_id"]))
                    updated += 1
            conn.execute("UPDATE report_context SET as_of_date = ? WHERE id = 1", (as_of_date,))
        return {"ok": True, "as_of_date": as_of_date, "updated_contracts": updated}

    def cancel_contract(self, *, contract_id: str, reason: str) -> dict[str, Any]:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT contract_id, lpo_state, cancelled_at FROM contracts WHERE contract_id = ?",
                (contract_id,),
            ).fetchone()
            if not row:
                raise ValueError(f"Unknown contract_id: {contract_id}")
            if str(row["lpo_state"] or "").upper() == "CANCELLED":
                return {"ok": True, "contract_id": contract_id, "lpo_state": "CANCELLED", "idempotent_replay": True}
            now = utc_now_iso_z()
            conn.execute(
                """
                UPDATE contracts
                SET lpo_state = 'CANCELLED',
                    cancelled_at = COALESCE(cancelled_at, ?),
                    close_reason = COALESCE(?, close_reason),
                    updated_at = ?
                WHERE contract_id = ?
                """,
                (now, reason or None, now, contract_id),
            )
            self.refresh_contract_status(conn, contract_id)
        return {"ok": True, "contract_id": contract_id, "lpo_state": "CANCELLED"}

    def close_contract(self, *, contract_id: str, reason: str) -> dict[str, Any]:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT contract_id, lpo_state, closed_at FROM contracts WHERE contract_id = ?",
                (contract_id,),
            ).fetchone()
            if not row:
                raise ValueError(f"Unknown contract_id: {contract_id}")
            if str(row["lpo_state"] or "").upper() == "CLOSED":
                return {"ok": True, "contract_id": contract_id, "lpo_state": "CLOSED", "idempotent_replay": True}
            now = utc_now_iso_z()
            conn.execute(
                """
                UPDATE contracts
                SET lpo_state = 'CLOSED',
                    closed_at = COALESCE(closed_at, ?),
                    close_reason = COALESCE(?, close_reason),
                    updated_at = ?
                WHERE contract_id = ?
                """,
                (now, reason or None, now, contract_id),
            )
            self.refresh_contract_status(conn, contract_id)
        return {"ok": True, "contract_id": contract_id, "lpo_state": "CLOSED"}

    def get_delivery_bundle(self, delivery_id: str) -> dict[str, Any]:
        row = self.fetch_one(
            """
            SELECT
              d.delivery_id,
              d.contract_id AS delivery_contract_id,
              d.contract_line_id,
              d.delivery_ref,
              d.run_id,
              d.batch_id,
              d.delivery_date,
              d.delivered_qty,
              d.delivered_qty_kg,
              d.unit,
              d.unit_price,
              d.unit_price_basis,
              d.gross_amount,
              COALESCE(dts.truck_no, d.truck_no) AS truck_no,
              COALESCE(dts.driver_name, d.driver_name) AS driver_name,
              COALESCE(dts.driver_phone, d.driver_phone) AS driver_phone,
              d.notes,
              d.status,
              d.dispatched_at,
              d.delivered_at,
              d.invoiced_at,
              d.paid_at,
              d.over_delivery_override_reason,
              d.created_at AS delivery_created_at,
              d.updated_at AS delivery_updated_at,
              dts.snapshot_id AS delivery_transport_snapshot_id,
              c.contract_ref, c.lpo_no, c.lpo_date, c.issue_date, c.due_date, c.due_terms, c.currency,
              c.buyer_id, c.vendor_of_record_id, c.operator_id, c.source_id, c.processor_id, c.contract_id,
              c.expected_total_qty, c.expected_total_value
            FROM deliveries d
            JOIN contracts c ON c.contract_id = d.contract_id
            LEFT JOIN delivery_transport_snapshot dts ON dts.delivery_id = d.delivery_id
            WHERE d.delivery_id = ?
            """,
            (delivery_id,),
        )
        if not row:
            raise ValueError(f"Unknown delivery_id: {delivery_id}")
        return row

    def get_coa_record_for_delivery(self, delivery_id: str) -> dict[str, Any] | None:
        return self.fetch_one(
            """
            SELECT cr.*
            FROM delivery_coa_links dcl
            JOIN coa_results cr ON cr.coa_record_id = dcl.coa_record_id
            WHERE dcl.delivery_id = ?
            ORDER BY cr.updated_at DESC
            LIMIT 1
            """,
            (delivery_id,),
        )

    def upsert_coa_result(
        self,
        config: RuntimeConfig,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        delivery_id = str(payload.get("delivery_id") or "").strip() or None
        buyer_group = str(payload.get("buyer_group") or "").strip().upper()
        product_code = str(payload.get("product_code") or "").strip().upper()
        run_id = str(payload.get("run_id") or "").strip()
        batch_id = str(payload.get("batch_id") or "").strip()
        results = payload.get("results") or payload.get("quality_parameters") or []
        if not isinstance(results, list) or not results:
            raise ValueError("results[] is required")

        if delivery_id:
            bundle = self.get_delivery_bundle(delivery_id)
            run_id = run_id or str(bundle["run_id"])
            batch_id = batch_id or str(bundle["batch_id"])
            coa_vendor_id = str(bundle.get("vendor_of_record_id") or "")
            if not product_code:
                line = self.fetch_one("SELECT product_code FROM contract_line_items WHERE contract_line_id = ?", (bundle["contract_line_id"],))
                if line:
                    product_code = str(line["product_code"]).upper()
            if not buyer_group:
                buyer_party = config.registry.get(str(bundle["buyer_id"]))
                buyer_group = infer_buyer_group(str(bundle["buyer_id"]), buyer_party.name)
        else:
            coa_vendor_id = str(payload.get("vendor_of_record_id") or "").strip()

        ensure_run_and_batch(run_id=run_id, batch_id=batch_id)
        if not product_code:
            raise ValueError("product_code is required")
        if not buyer_group:
            raise ValueError("buyer_group is required")

        profile = _resolve_coa_profile(config.coa_profiles, buyer_group=buyer_group, product_code=product_code)
        profile_rows = profile["quality_parameters"]
        ensure_required_coa_rows(results, profile_rows)
        profile_version = str(profile.get("coa_version_no") or profile.get("version") or "01")
        profile_key = f"{buyer_group}:{product_code}"

        now = utc_now_iso_z()
        with self.transaction() as conn:
            existing = conn.execute(
                """
                SELECT * FROM coa_results
                WHERE buyer_group = ? AND product_code = ? AND batch_id = ? AND run_id = ? AND profile_version = ?
                """,
                (buyer_group, product_code, batch_id, run_id, profile_version),
            ).fetchone()

            if existing:
                coa_record_id = existing["coa_record_id"]
                coa_no = existing["coa_no"]
                conn.execute(
                    """
                    UPDATE coa_results
                    SET results_json = ?, updated_at = ?
                    WHERE coa_record_id = ?
                    """,
                    (json.dumps(results, sort_keys=True), now, coa_record_id),
                )
            else:
                coa_record_id = new_ulid()
                year = int(utc_today_iso().split("-")[0])
                sequence = self.next_sequence(
                    conn,
                    vendor_of_record_id=coa_vendor_id or "ananta_flows",
                    doc_type=DocumentType.COA.value,
                    year=year,
                )
                coa_no = self.format_doc_number(DocumentType.COA, invoice_no="", year=year, sequence=sequence)
                conn.execute(
                    """
                    INSERT INTO coa_results(
                        coa_record_id, buyer_group, product_code, batch_id, run_id, profile_key, profile_version,
                        coa_no, results_json, created_at, updated_at
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        coa_record_id,
                        buyer_group,
                        product_code,
                        batch_id,
                        run_id,
                        profile_key,
                        profile_version,
                        coa_no,
                        json.dumps(results, sort_keys=True),
                        now,
                        now,
                    ),
                )

            if delivery_id:
                conn.execute(
                    """
                    INSERT INTO delivery_coa_links(link_id, delivery_id, coa_record_id, created_at)
                    VALUES(?, ?, ?, ?)
                    ON CONFLICT(delivery_id, coa_record_id) DO NOTHING
                    """,
                    (new_ulid(), delivery_id, coa_record_id, now),
                )

        return {
            "ok": True,
            "coa_record_id": coa_record_id,
            "coa_no": coa_no,
            "profile_key": profile_key,
            "profile_version": profile_version,
            "linked_delivery_id": delivery_id,
        }

    def ensure_delivery_marked_invoiced(self, conn: sqlite3.Connection, delivery_id: str) -> None:
        now = utc_now_iso_z()
        current = conn.execute("SELECT status FROM deliveries WHERE delivery_id = ?", (delivery_id,)).fetchone()
        if not current:
            raise ValueError(f"Unknown delivery_id: {delivery_id}")
        if current["status"] == DeliveryStatus.INVOICED.value:
            return
        validate_delivery_transition(current["status"], DeliveryStatus.INVOICED.value)
        conn.execute(
            "UPDATE deliveries SET status = ?, invoiced_at = ?, updated_at = ? WHERE delivery_id = ?",
            (DeliveryStatus.INVOICED.value, now, now, delivery_id),
        )
        conn.execute(
            """
            UPDATE planned_deliveries
            SET status = 'INVOICED', updated_at = ?
            WHERE delivery_id = ?
            """,
            (now, delivery_id),
        )


def _resolve_coa_profile(coa_profiles: dict[str, Any], *, buyer_group: str, product_code: str) -> dict[str, Any]:
    product_key = product_code.upper()
    profile = dict(coa_profiles.get(product_key) or {})
    if not profile:
        raise ValueError(f"No COA profile configured for product_code={product_code}")

    overrides = profile.get("buyer_overrides", {}) if isinstance(profile.get("buyer_overrides"), dict) else {}
    buyer_key_norm = "".join(ch.lower() for ch in buyer_group if ch.isalnum())
    selected = dict(profile)
    for key, override in overrides.items():
        override_key_norm = "".join(ch.lower() for ch in str(key) if ch.isalnum())
        if override_key_norm and (override_key_norm == buyer_key_norm or override_key_norm in buyer_key_norm):
            selected = {**profile, **override}
            break

    quality_rows = selected.get("quality_parameters")
    if not isinstance(quality_rows, list) or not quality_rows:
        raise ValueError(f"COA profile has no quality parameters for product_code={product_code}")
    return selected
