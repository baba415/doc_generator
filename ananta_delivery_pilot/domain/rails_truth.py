from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol

from core.hashing import canonical_json_sha256, sha256_file, sha256_text
from core.time import utc_now_iso_z

EXECUTE_DREP_DAILY_VERSION = "execute_drep_daily_v1"
EXECUTE_PROOF_EXPORT_VERSION = "execute_proof_export_v1"
EXECUTE_PROOF_MANIFEST_VERSION = "execute_proof_manifest_v1"

DEFAULT_RAILS_TRUTH_FLAGS: dict[str, bool] = {
    "shadow_emit_only": True,
    "rails_write_enabled": False,
    "execute_contract_consume_enabled": False,
}

TRUST_ACTION_INVENTORY: tuple[dict[str, Any], ...] = (
    {"surface": "cli", "action": "create-contract", "trust_changing": True, "rails_evented_write_target": "YES"},
    {"surface": "cli", "action": "plan-deliveries", "trust_changing": True, "rails_evented_write_target": "YES"},
    {"surface": "cli", "action": "add-delivery", "trust_changing": True, "rails_evented_write_target": "YES"},
    {"surface": "cli", "action": "materialize-delivery", "trust_changing": True, "rails_evented_write_target": "YES"},
    {"surface": "cli", "action": "mark-dispatched", "trust_changing": True, "rails_evented_write_target": "YES"},
    {"surface": "cli", "action": "mark-delivered", "trust_changing": True, "rails_evented_write_target": "YES"},
    {"surface": "cli", "action": "record-coa", "trust_changing": True, "rails_evented_write_target": "YES"},
    {"surface": "cli", "action": "generate-pack", "trust_changing": True, "rails_evented_write_target": "YES"},
    {"surface": "cli", "action": "mark-paid", "trust_changing": True, "rails_evented_write_target": "YES"},
    {"surface": "cli", "action": "cancel-contract", "trust_changing": True, "rails_evented_write_target": "YES"},
    {"surface": "cli", "action": "close-contract", "trust_changing": True, "rails_evented_write_target": "YES"},
    {"surface": "cli", "action": "refresh-contract-state", "trust_changing": True, "rails_evented_write_target": "YES"},
    {"surface": "cli", "action": "exceptions resolve", "trust_changing": True, "rails_evented_write_target": "YES"},
    {"surface": "cli", "action": "decide-case", "trust_changing": True, "rails_evented_write_target": "YES"},
    {"surface": "cli", "action": "auto-run", "trust_changing": True, "rails_evented_write_target": "DERIVED"},
    {"surface": "cli", "action": "run-autonomy", "trust_changing": True, "rails_evented_write_target": "DERIVED"},
    {"surface": "cli", "action": "auto-resume", "trust_changing": True, "rails_evented_write_target": "DERIVED"},
    {"surface": "web", "action": "/v2/intake/confirm", "trust_changing": True, "rails_evented_write_target": "YES"},
    {"surface": "web", "action": "/v2/contracts/{id}/plan/rebuild|update|approve", "trust_changing": True, "rails_evented_write_target": "YES"},
    {"surface": "web", "action": "/v2/contracts/{id}/execute/materialize-due|materialize-one", "trust_changing": True, "rails_evented_write_target": "YES"},
    {"surface": "web", "action": "/v2/contracts/{id}/execute/generate-pack", "trust_changing": True, "rails_evented_write_target": "YES"},
    {"surface": "web", "action": "/v2/contracts/{id}/settle/suggest", "trust_changing": False, "rails_evented_write_target": "NO"},
    {"surface": "web", "action": "/v2/contracts/{id}/settle/apply-suggestion", "trust_changing": True, "rails_evented_write_target": "YES"},
    {"surface": "web", "action": "/v2/contracts/{id}/settle/mark-paid", "trust_changing": True, "rails_evented_write_target": "YES"},
    {"surface": "web", "action": "/v2/exceptions/decide", "trust_changing": True, "rails_evented_write_target": "YES"},
    {"surface": "web", "action": "/v2/exceptions/resolve", "trust_changing": True, "rails_evented_write_target": "YES"},
    {"surface": "web", "action": "/v2/contracts/{id}/run-recommended", "trust_changing": True, "rails_evented_write_target": "DERIVED"},
    {"surface": "web", "action": "/v2/run-all-eligible/execute", "trust_changing": True, "rails_evented_write_target": "DERIVED"},
)

TRUST_ACTION_EVENT_MAPPING: dict[str, dict[str, Any]] = {
    "create-contract": {
        "event_type": "DREP_CONTRACT_CREATED",
        "required_payload_fields": [
            "contract_id",
            "lpo_no",
            "buyer_id",
            "vendor_of_record_id",
            "issue_date",
            "lpo_valid_from",
            "lpo_valid_to",
            "expected_total_qty_kg",
            "currency",
            "unit_price_basis",
            "policy_pointers",
        ],
        "derived": False,
    },
    "plan-deliveries": {
        "event_type": "DREP_DELIVERY_PLAN_BUILT",
        "required_payload_fields": [
            "contract_id",
            "as_of_date",
            "start_date",
            "cadence",
            "max_lots_per_day",
            "policy_version",
            "planned_rows",
        ],
        "derived": False,
    },
    "plan-update": {
        "event_type": "DREP_DELIVERY_PLAN_UPDATED",
        "required_payload_fields": [
            "contract_id",
            "planned_delivery_id",
            "sequence_no",
            "planned_qty_kg",
            "planned_date",
            "reason",
        ],
        "derived": False,
    },
    "plan-approve": {
        "event_type": "DREP_DELIVERY_PLAN_APPROVED",
        "required_payload_fields": ["contract_id", "approved_at_utc", "approved_by", "plan_hash"],
        "derived": False,
    },
    "add-delivery": {
        "event_type": "DREP_DELIVERY_MATERIALIZED",
        "required_payload_fields": [
            "contract_id",
            "planned_delivery_id",
            "delivery_id",
            "delivery_ref",
            "run_id",
            "batch_id",
            "delivery_date",
            "delivered_qty_kg",
            "unit_price",
            "unit_price_basis",
        ],
        "derived": False,
    },
    "materialize-delivery": {
        "event_type": "DREP_DELIVERY_MATERIALIZED",
        "required_payload_fields": [
            "contract_id",
            "planned_delivery_id",
            "delivery_id",
            "delivery_ref",
            "run_id",
            "batch_id",
            "delivery_date",
            "delivered_qty_kg",
            "unit_price",
            "unit_price_basis",
        ],
        "derived": False,
    },
    "mark-dispatched": {
        "event_type": "DREP_DELIVERY_DISPATCHED",
        "required_payload_fields": ["contract_id", "delivery_id", "dispatched_at_utc"],
        "derived": False,
    },
    "mark-delivered": {
        "event_type": "DREP_DELIVERY_DELIVERED",
        "required_payload_fields": ["contract_id", "delivery_id", "delivered_at_utc", "delivered_qty_kg"],
        "derived": False,
    },
    "record-coa": {
        "event_type": "DREP_COA_RECORDED",
        "required_payload_fields": [
            "contract_id",
            "delivery_id",
            "coa_no",
            "product_code",
            "batch_id",
            "run_id",
            "profile_version",
            "buyer_group",
            "results",
        ],
        "derived": False,
    },
    "generate-pack": {
        "event_type": "DREP_PROOF_PACK_GENERATED",
        "required_payload_fields": [
            "contract_id",
            "delivery_id",
            "invoice_no",
            "pack_status",
            "manifest_contract_version",
            "manifest_path",
            "manifest_sha256",
            "pdf_path",
            "pdf_sha256",
            "doc_order",
        ],
        "derived": False,
    },
    "settle-apply-suggestion": {
        "event_type": "DREP_SETTLEMENT_SUGGESTION_APPLIED",
        "required_payload_fields": [
            "contract_id",
            "suggestion_set_id",
            "suggestion_id",
            "sales_transaction_id",
            "allocated_amount",
            "decision",
            "reason",
            "as_of_date",
        ],
        "derived": False,
    },
    "mark-paid": {
        "event_type": "DREP_PAYMENT_RECORDED",
        "required_payload_fields": [
            "contract_id",
            "receipt_no",
            "payment_date",
            "payment_method",
            "external_reference",
            "amount_received",
            "allocations",
        ],
        "derived": False,
    },
    "cancel-contract": {
        "event_type": "DREP_CONTRACT_CANCELLED",
        "required_payload_fields": ["contract_id", "cancelled_at_utc", "reason"],
        "derived": False,
    },
    "close-contract": {
        "event_type": "DREP_CONTRACT_CLOSED",
        "required_payload_fields": ["contract_id", "closed_at_utc", "reason"],
        "derived": False,
    },
    "refresh-contract-state": {
        "event_type": "DREP_CONTRACT_STATE_REFRESHED",
        "required_payload_fields": ["contract_id", "as_of_date", "previous_lpo_state", "next_lpo_state", "reason_code"],
        "derived": False,
    },
    "decide-case": {
        "event_type": "DREP_EXCEPTION_DECIDED",
        "required_payload_fields": [
            "exception_case_id",
            "case_type",
            "contract_id",
            "decision",
            "reason",
            "resume_requested",
            "dry_run_resume",
        ],
        "derived": False,
    },
    "exceptions-resolve": {
        "event_type": "DREP_EXCEPTION_RESOLVED",
        "required_payload_fields": ["exception_id", "run_id", "resolution_value", "note"],
        "derived": False,
    },
    "run-recommended": {"event_type": "DERIVED", "required_payload_fields": [], "derived": True},
    "run-all-eligible": {"event_type": "DERIVED", "required_payload_fields": [], "derived": True},
    "auto-run": {"event_type": "DERIVED", "required_payload_fields": [], "derived": True},
    "run-autonomy": {"event_type": "DERIVED", "required_payload_fields": [], "derived": True},
    "auto-resume": {"event_type": "DERIVED", "required_payload_fields": [], "derived": True},
}

_ACTION_ALIASES: dict[str, str] = {
    "intake-confirm": "create-contract",
    "plan-rebuild": "plan-deliveries",
    "plan-update": "plan-update",
    "plan-approve": "plan-approve",
    "materialize-one": "materialize-delivery",
    "materialize-due": "materialize-delivery",
    "settle-suggest": "settle-suggest",
    "settle-apply-suggestion": "settle-apply-suggestion",
    "exceptions resolve": "exceptions-resolve",
}


def resolve_rails_truth_flags(raw_flags: Mapping[str, Any] | None) -> dict[str, bool]:
    flags = dict(DEFAULT_RAILS_TRUTH_FLAGS)
    if isinstance(raw_flags, Mapping):
        for key in DEFAULT_RAILS_TRUTH_FLAGS:
            if key in raw_flags:
                flags[key] = bool(raw_flags[key])
    return flags


def canonical_trust_action(action: str) -> str:
    action_key = str(action or "").strip()
    if action_key in TRUST_ACTION_EVENT_MAPPING:
        return action_key
    return _ACTION_ALIASES.get(action_key, action_key)


def dg1a_action_inventory() -> list[dict[str, Any]]:
    return [dict(item) for item in TRUST_ACTION_INVENTORY]


def dg1a_action_event_mapping() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for action, spec in TRUST_ACTION_EVENT_MAPPING.items():
        rows.append(
            {
                "action": action,
                "event_type": spec["event_type"],
                "required_payload_fields": list(spec.get("required_payload_fields") or []),
                "derived": bool(spec.get("derived")),
            }
        )
    rows.sort(key=lambda item: str(item["action"]))
    return rows


def _missing_fields(payload: Mapping[str, Any], required_fields: list[str]) -> list[str]:
    missing: list[str] = []
    for field_name in required_fields:
        if field_name not in payload:
            missing.append(field_name)
            continue
        value = payload.get(field_name)
        if value is None:
            missing.append(field_name)
            continue
        if isinstance(value, str) and not value.strip():
            missing.append(field_name)
    return missing


def _require_non_empty(payload: Mapping[str, Any], field_name: str) -> str:
    value = str(payload.get(field_name) or "").strip()
    if not value:
        raise ValueError(f"Missing required field: {field_name}")
    return value


def build_trust_idempotency_key(action: str, payload: Mapping[str, Any]) -> str:
    action_key = canonical_trust_action(action)
    spec = TRUST_ACTION_EVENT_MAPPING.get(action_key)
    if not spec:
        raise ValueError(f"Unknown trust action: {action}")
    if bool(spec.get("derived")):
        raise ValueError(f"Derived action has no trust idempotency key: {action}")
    if not isinstance(payload, Mapping):
        raise ValueError("payload must be mapping")

    if action_key == "create-contract":
        return f"drep:contract:create:{_require_non_empty(payload, 'vendor_of_record_id')}:{_require_non_empty(payload, 'lpo_no')}"
    if action_key == "plan-deliveries":
        return (
            "drep:plan:build:"
            f"{_require_non_empty(payload, 'contract_id')}:"
            f"{_require_non_empty(payload, 'start_date')}:"
            f"{_require_non_empty(payload, 'cadence')}:"
            f"{_require_non_empty(payload, 'max_lots_per_day')}:"
            f"{_require_non_empty(payload, 'policy_version')}"
        )
    if action_key == "plan-update":
        update_payload = {
            "contract_id": payload.get("contract_id"),
            "planned_delivery_id": payload.get("planned_delivery_id"),
            "sequence_no": payload.get("sequence_no"),
            "planned_qty_kg": payload.get("planned_qty_kg"),
            "planned_date": payload.get("planned_date"),
            "reason": payload.get("reason"),
        }
        payload_hash = canonical_json_sha256(update_payload)
        return f"drep:plan:update:{_require_non_empty(payload, 'planned_delivery_id')}:{payload_hash}"
    if action_key == "plan-approve":
        return f"drep:plan:approve:{_require_non_empty(payload, 'contract_id')}:{_require_non_empty(payload, 'plan_hash')}"
    if action_key in {"add-delivery", "materialize-delivery"}:
        return f"drep:delivery:materialize:{_require_non_empty(payload, 'planned_delivery_id')}"
    if action_key == "mark-dispatched":
        return f"drep:delivery:dispatch:{_require_non_empty(payload, 'delivery_id')}"
    if action_key == "mark-delivered":
        return f"drep:delivery:deliver:{_require_non_empty(payload, 'delivery_id')}"
    if action_key == "record-coa":
        return (
            "drep:coa:record:"
            f"{_require_non_empty(payload, 'buyer_group')}:"
            f"{_require_non_empty(payload, 'product_code')}:"
            f"{_require_non_empty(payload, 'batch_id')}:"
            f"{_require_non_empty(payload, 'run_id')}:"
            f"{_require_non_empty(payload, 'profile_version')}"
        )
    if action_key == "generate-pack":
        return (
            "drep:pack:generate:"
            f"{_require_non_empty(payload, 'delivery_id')}:"
            f"{_require_non_empty(payload, 'manifest_sha256')}"
        )
    if action_key == "settle-apply-suggestion":
        return (
            "drep:settlement:suggestion:"
            f"{_require_non_empty(payload, 'suggestion_set_id')}:"
            f"{_require_non_empty(payload, 'suggestion_id')}:"
            f"{_require_non_empty(payload, 'decision')}"
        )
    if action_key == "mark-paid":
        return f"drep:payment:record:{_require_non_empty(payload, 'external_reference')}"
    if action_key == "cancel-contract":
        return f"drep:contract:cancel:{_require_non_empty(payload, 'contract_id')}"
    if action_key == "close-contract":
        return f"drep:contract:close:{_require_non_empty(payload, 'contract_id')}"
    if action_key == "refresh-contract-state":
        return f"drep:contract:refresh:{_require_non_empty(payload, 'contract_id')}:{_require_non_empty(payload, 'as_of_date')}"
    if action_key == "decide-case":
        reason_hash = sha256_text(_require_non_empty(payload, "reason"))
        return f"drep:exception:decide:{_require_non_empty(payload, 'exception_case_id')}:{_require_non_empty(payload, 'decision')}:{reason_hash}"
    if action_key == "exceptions-resolve":
        body_hash = sha256_text(f"{_require_non_empty(payload, 'resolution_value')}{_require_non_empty(payload, 'note')}")
        return f"drep:exception:resolve:{_require_non_empty(payload, 'exception_id')}:{body_hash}"
    raise ValueError(f"Unhandled trust action: {action_key}")


@dataclass(frozen=True)
class DG1BValidationResult:
    ok: bool
    contract: str
    reason_code: str
    details: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "contract": self.contract,
            "reason_code": self.reason_code,
            "details": self.details,
        }


def _is_non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_lower_hex_sha256(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    return all(ch in "0123456789abcdef" for ch in value)


def validate_execute_drep_daily_payload(payload: Any) -> DG1BValidationResult:
    contract_name = EXECUTE_DREP_DAILY_VERSION
    if not isinstance(payload, Mapping):
        return DG1BValidationResult(
            ok=False,
            contract=contract_name,
            reason_code="EXEC_DREP_DAILY_UNREADABLE_PAYLOAD",
            details={"error": "payload must be object map"},
        )

    required_keys = {
        "contract_version",
        "as_of_date",
        "deliveries_ingested",
        "trips_assigned",
        "trips_finalized",
        "board_close",
        "hold_full_by_reason",
        "hold_partial_by_reason",
        "handshake_valid_count",
        "handshake_total_finalized",
        "handshake_valid_rate",
        "open_incidents_count",
        "go_no_go",
        "go_no_go_reasons",
    }
    missing = sorted(required_keys - set(payload.keys()))
    unexpected = sorted(set(payload.keys()) - required_keys)
    if missing or unexpected:
        return DG1BValidationResult(
            ok=False,
            contract=contract_name,
            reason_code="EXEC_DREP_DAILY_SCHEMA_INVALID",
            details={"missing_keys": missing, "unexpected_keys": unexpected},
        )

    if payload.get("contract_version") != EXECUTE_DREP_DAILY_VERSION:
        return DG1BValidationResult(
            ok=False,
            contract=contract_name,
            reason_code="EXEC_DREP_DAILY_VERSION_MISMATCH",
            details={"expected": EXECUTE_DREP_DAILY_VERSION, "actual": payload.get("contract_version")},
        )

    for count_key in (
        "deliveries_ingested",
        "trips_assigned",
        "trips_finalized",
        "handshake_valid_count",
        "handshake_total_finalized",
        "open_incidents_count",
    ):
        if not _is_non_negative_int(payload.get(count_key)):
            return DG1BValidationResult(
                ok=False,
                contract=contract_name,
                reason_code="EXEC_DREP_DAILY_SCHEMA_INVALID",
                details={"invalid_field": count_key, "message": "must be non-negative integer"},
            )

    board_close = payload.get("board_close")
    board_keys = ("red", "yellow", "green", "total")
    if not isinstance(board_close, Mapping):
        return DG1BValidationResult(
            ok=False,
            contract=contract_name,
            reason_code="EXEC_DREP_DAILY_SCHEMA_INVALID",
            details={"invalid_field": "board_close", "message": "must be object"},
        )
    if sorted(set(board_close.keys())) != sorted(board_keys):
        return DG1BValidationResult(
            ok=False,
            contract=contract_name,
            reason_code="EXEC_DREP_DAILY_SCHEMA_INVALID",
            details={"invalid_field": "board_close", "message": "missing/extra board keys"},
        )
    for key in board_keys:
        if not _is_non_negative_int(board_close.get(key)):
            return DG1BValidationResult(
                ok=False,
                contract=contract_name,
                reason_code="EXEC_DREP_DAILY_SCHEMA_INVALID",
                details={"invalid_field": f"board_close.{key}", "message": "must be non-negative integer"},
            )
    if int(board_close["total"]) != int(board_close["red"]) + int(board_close["yellow"]) + int(board_close["green"]):
        return DG1BValidationResult(
            ok=False,
            contract=contract_name,
            reason_code="EXEC_DREP_DAILY_SCHEMA_INVALID",
            details={"invalid_field": "board_close.total", "message": "must equal red+yellow+green"},
        )

    for reason_map_key in ("hold_full_by_reason", "hold_partial_by_reason"):
        reason_map = payload.get(reason_map_key)
        if not isinstance(reason_map, Mapping):
            return DG1BValidationResult(
                ok=False,
                contract=contract_name,
                reason_code="EXEC_DREP_DAILY_SCHEMA_INVALID",
                details={"invalid_field": reason_map_key, "message": "must be object map"},
            )
        for key, value in reason_map.items():
            if not isinstance(key, str) or not key.strip() or not _is_non_negative_int(value):
                return DG1BValidationResult(
                    ok=False,
                    contract=contract_name,
                    reason_code="EXEC_DREP_DAILY_SCHEMA_INVALID",
                    details={"invalid_field": reason_map_key, "message": "reason map must use non-empty keys + non-negative int values"},
                )

    handshake_valid = int(payload.get("handshake_valid_count"))
    handshake_total = int(payload.get("handshake_total_finalized"))
    if handshake_valid > handshake_total:
        return DG1BValidationResult(
            ok=False,
            contract=contract_name,
            reason_code="EXEC_DREP_DAILY_SCHEMA_INVALID",
            details={"invalid_field": "handshake_valid_count", "message": "must be <= handshake_total_finalized"},
        )

    handshake_rate = payload.get("handshake_valid_rate")
    if isinstance(handshake_rate, bool) or not isinstance(handshake_rate, (int, float)):
        return DG1BValidationResult(
            ok=False,
            contract=contract_name,
            reason_code="EXEC_DREP_DAILY_SCHEMA_INVALID",
            details={"invalid_field": "handshake_valid_rate", "message": "must be numeric"},
        )
    expected_rate = (handshake_valid / handshake_total) if handshake_total else 0.0
    if abs(float(handshake_rate) - expected_rate) > 1e-12:
        return DG1BValidationResult(
            ok=False,
            contract=contract_name,
            reason_code="EXEC_DREP_DAILY_SCHEMA_INVALID",
            details={"invalid_field": "handshake_valid_rate", "message": "must equal valid/total ratio"},
        )

    reasons = payload.get("go_no_go_reasons")
    allowed_reasons = ("RED_QUEUE_OPEN", "LOW_HANDSHAKE_VALID_RATE", "NO_FINALIZED_TRIPS")
    reason_rank = {name: idx for idx, name in enumerate(allowed_reasons)}
    if not isinstance(reasons, list):
        return DG1BValidationResult(
            ok=False,
            contract=contract_name,
            reason_code="EXEC_DREP_DAILY_SCHEMA_INVALID",
            details={"invalid_field": "go_no_go_reasons", "message": "must be array"},
        )
    if len(set(reasons)) != len(reasons):
        return DG1BValidationResult(
            ok=False,
            contract=contract_name,
            reason_code="EXEC_DREP_DAILY_SCHEMA_INVALID",
            details={"invalid_field": "go_no_go_reasons", "message": "must be unique"},
        )
    for reason in reasons:
        if reason not in reason_rank:
            return DG1BValidationResult(
                ok=False,
                contract=contract_name,
                reason_code="EXEC_DREP_DAILY_SCHEMA_INVALID",
                details={"invalid_field": "go_no_go_reasons", "message": f"unsupported reason {reason!r}"},
            )
    ordered = sorted(reasons, key=lambda item: (reason_rank.get(item, 999), item))
    if list(reasons) != ordered:
        return DG1BValidationResult(
            ok=False,
            contract=contract_name,
            reason_code="EXEC_DREP_DAILY_REASON_ORDER_INVALID",
            details={"message": "go_no_go_reasons must be deterministic ordered", "expected_order": ordered},
        )

    go_no_go = payload.get("go_no_go")
    if go_no_go not in {"GO", "NO_GO"}:
        return DG1BValidationResult(
            ok=False,
            contract=contract_name,
            reason_code="EXEC_DREP_DAILY_SCHEMA_INVALID",
            details={"invalid_field": "go_no_go", "message": "must be GO or NO_GO"},
        )

    blocking_reasons = {"RED_QUEUE_OPEN", "LOW_HANDSHAKE_VALID_RATE"}
    has_blocking = any(reason in blocking_reasons for reason in reasons)
    if go_no_go == "NO_GO" and not has_blocking:
        return DG1BValidationResult(
            ok=False,
            contract=contract_name,
            reason_code="EXEC_DREP_DAILY_GO_NO_GO_BLOCKED",
            details={"message": "NO_GO requires blocking reason"},
        )
    if go_no_go == "GO" and has_blocking:
        return DG1BValidationResult(
            ok=False,
            contract=contract_name,
            reason_code="EXEC_DREP_DAILY_GO_NO_GO_BLOCKED",
            details={"message": "GO cannot include blocking reasons"},
        )
    has_no_finalized = "NO_FINALIZED_TRIPS" in reasons
    if handshake_total == 0 and not has_no_finalized:
        return DG1BValidationResult(
            ok=False,
            contract=contract_name,
            reason_code="EXEC_DREP_DAILY_GO_NO_GO_BLOCKED",
            details={"message": "NO_FINALIZED_TRIPS required when handshake_total_finalized == 0"},
        )
    if handshake_total > 0 and has_no_finalized:
        return DG1BValidationResult(
            ok=False,
            contract=contract_name,
            reason_code="EXEC_DREP_DAILY_GO_NO_GO_BLOCKED",
            details={"message": "NO_FINALIZED_TRIPS not allowed when handshake_total_finalized > 0"},
        )

    return DG1BValidationResult(
        ok=True,
        contract=contract_name,
        reason_code="PASS",
        details={"contract_version": EXECUTE_DREP_DAILY_VERSION},
    )


def validate_execute_proof_export_payload(payload: Any) -> DG1BValidationResult:
    contract_name = EXECUTE_PROOF_EXPORT_VERSION
    if not isinstance(payload, Mapping):
        return DG1BValidationResult(
            ok=False,
            contract=contract_name,
            reason_code="EXEC_PROOF_EXPORT_UNREADABLE_PAYLOAD",
            details={"error": "payload must be object map"},
        )

    required_keys = {
        "export_contract_version",
        "trip_id",
        "pack_status",
        "manifest_contract_version",
        "manifest_path",
        "manifest_sha256",
        "pdf_path",
        "pdf_sha256",
    }
    missing = sorted(required_keys - set(payload.keys()))
    unexpected = sorted(set(payload.keys()) - required_keys)
    if missing or unexpected:
        return DG1BValidationResult(
            ok=False,
            contract=contract_name,
            reason_code="EXEC_PROOF_EXPORT_SCHEMA_INVALID",
            details={"missing_keys": missing, "unexpected_keys": unexpected},
        )

    if payload.get("export_contract_version") != EXECUTE_PROOF_EXPORT_VERSION:
        return DG1BValidationResult(
            ok=False,
            contract=contract_name,
            reason_code="EXEC_PROOF_EXPORT_VERSION_MISMATCH",
            details={"expected": EXECUTE_PROOF_EXPORT_VERSION, "actual": payload.get("export_contract_version")},
        )

    trip_id = payload.get("trip_id")
    if not isinstance(trip_id, str) or not trip_id.strip():
        return DG1BValidationResult(
            ok=False,
            contract=contract_name,
            reason_code="EXEC_PROOF_EXPORT_SCHEMA_INVALID",
            details={"invalid_field": "trip_id", "message": "must be non-empty string"},
        )

    pack_status = payload.get("pack_status")
    if pack_status not in {"DRAFT", "FINAL"}:
        return DG1BValidationResult(
            ok=False,
            contract=contract_name,
            reason_code="EXEC_PROOF_EXPORT_SCHEMA_INVALID",
            details={"invalid_field": "pack_status", "message": "must be DRAFT or FINAL"},
        )

    manifest_contract_version = payload.get("manifest_contract_version")
    if manifest_contract_version != EXECUTE_PROOF_MANIFEST_VERSION:
        return DG1BValidationResult(
            ok=False,
            contract=contract_name,
            reason_code="EXEC_PROOF_EXPORT_MANIFEST_VERSION_MISMATCH",
            details={"expected": EXECUTE_PROOF_MANIFEST_VERSION, "actual": manifest_contract_version},
        )

    manifest_path_value = payload.get("manifest_path")
    manifest_sha256 = payload.get("manifest_sha256")
    if not isinstance(manifest_path_value, str) or not manifest_path_value.strip():
        return DG1BValidationResult(
            ok=False,
            contract=contract_name,
            reason_code="EXEC_PROOF_EXPORT_SCHEMA_INVALID",
            details={"invalid_field": "manifest_path", "message": "must be non-empty string"},
        )
    if not _is_lower_hex_sha256(manifest_sha256):
        return DG1BValidationResult(
            ok=False,
            contract=contract_name,
            reason_code="EXEC_PROOF_EXPORT_HASH_INVALID",
            details={"invalid_field": "manifest_sha256", "message": "must be lowercase 64-char hex"},
        )

    pdf_path = payload.get("pdf_path")
    pdf_sha256 = payload.get("pdf_sha256")
    if (pdf_path is None) != (pdf_sha256 is None):
        return DG1BValidationResult(
            ok=False,
            contract=contract_name,
            reason_code="EXEC_PROOF_EXPORT_SCHEMA_INVALID",
            details={"message": "pdf_path and pdf_sha256 must be both null or both non-null"},
        )
    if pdf_path is not None and (not isinstance(pdf_path, str) or not pdf_path.strip()):
        return DG1BValidationResult(
            ok=False,
            contract=contract_name,
            reason_code="EXEC_PROOF_EXPORT_SCHEMA_INVALID",
            details={"invalid_field": "pdf_path", "message": "must be null or non-empty string"},
        )
    if pdf_sha256 is not None and not _is_lower_hex_sha256(pdf_sha256):
        return DG1BValidationResult(
            ok=False,
            contract=contract_name,
            reason_code="EXEC_PROOF_EXPORT_HASH_INVALID",
            details={"invalid_field": "pdf_sha256", "message": "must be lowercase 64-char hex when present"},
        )
    if pack_status == "DRAFT" and (pdf_path is not None or pdf_sha256 is not None):
        return DG1BValidationResult(
            ok=False,
            contract=contract_name,
            reason_code="EXEC_PROOF_EXPORT_DRAFT_PDF_INVALID",
            details={"message": "DRAFT export must not include pdf_path/pdf_sha256"},
        )

    manifest_path = Path(manifest_path_value)
    if not manifest_path.exists():
        return DG1BValidationResult(
            ok=False,
            contract=contract_name,
            reason_code="EXEC_PROOF_EXPORT_UNREADABLE_PAYLOAD",
            details={"message": "manifest_path not found", "manifest_path": manifest_path_value},
        )

    try:
        manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return DG1BValidationResult(
            ok=False,
            contract=contract_name,
            reason_code="EXEC_PROOF_EXPORT_UNREADABLE_PAYLOAD",
            details={"message": "manifest_path unreadable", "error": str(exc), "manifest_path": manifest_path_value},
        )

    if not isinstance(manifest_payload, Mapping):
        return DG1BValidationResult(
            ok=False,
            contract=contract_name,
            reason_code="EXEC_PROOF_EXPORT_UNREADABLE_PAYLOAD",
            details={"message": "manifest payload must be object"},
        )

    if manifest_payload.get("contract_version") != EXECUTE_PROOF_MANIFEST_VERSION:
        return DG1BValidationResult(
            ok=False,
            contract=contract_name,
            reason_code="EXEC_PROOF_EXPORT_MANIFEST_VERSION_MISMATCH",
            details={
                "expected": EXECUTE_PROOF_MANIFEST_VERSION,
                "actual": manifest_payload.get("contract_version"),
                "manifest_path": manifest_path_value,
            },
        )

    if manifest_payload.get("trip_id") != trip_id:
        return DG1BValidationResult(
            ok=False,
            contract=contract_name,
            reason_code="EXEC_PROOF_EXPORT_SCHEMA_INVALID",
            details={"message": "manifest trip_id mismatch", "trip_id": trip_id, "manifest_trip_id": manifest_payload.get("trip_id")},
        )

    if manifest_payload.get("pack_status") != pack_status:
        return DG1BValidationResult(
            ok=False,
            contract=contract_name,
            reason_code="EXEC_PROOF_EXPORT_SCHEMA_INVALID",
            details={
                "message": "manifest pack_status mismatch",
                "pack_status": pack_status,
                "manifest_pack_status": manifest_payload.get("pack_status"),
            },
        )

    computed_manifest_hash = sha256_file(manifest_path)
    if computed_manifest_hash != manifest_sha256:
        return DG1BValidationResult(
            ok=False,
            contract=contract_name,
            reason_code="EXEC_PROOF_EXPORT_HASH_INVALID",
            details={
                "message": "manifest_sha256 mismatch",
                "expected": manifest_sha256,
                "computed": computed_manifest_hash,
                "manifest_path": manifest_path_value,
            },
        )

    return DG1BValidationResult(
        ok=True,
        contract=contract_name,
        reason_code="PASS",
        details={"manifest_path": manifest_path_value, "manifest_sha256": manifest_sha256},
    )


def build_shadow_event_envelope(
    *,
    action: str,
    payload: Mapping[str, Any],
    as_of_date: str,
    occurred_at_utc: str | None = None,
) -> dict[str, Any]:
    canonical_action = canonical_trust_action(action)
    mapping = TRUST_ACTION_EVENT_MAPPING.get(canonical_action)
    if not mapping:
        raise ValueError(f"Unknown trust action: {action}")
    if bool(mapping.get("derived")):
        raise ValueError(f"Derived action has no canonical trust event: {action}")
    missing = _missing_fields(payload, list(mapping.get("required_payload_fields") or []))
    if missing:
        raise ValueError(f"Missing required payload fields for {canonical_action}: {missing}")
    idempotency_key = build_trust_idempotency_key(canonical_action, payload)
    trade_id = str(payload.get("contract_id") or payload.get("delivery_id") or payload.get("exception_case_id") or "")
    if not trade_id:
        raise ValueError(f"Cannot derive trade_id for action: {canonical_action}")
    filtered_payload = {
        field_name: payload.get(field_name)
        for field_name in list(mapping.get("required_payload_fields") or [])
    }
    return {
        "event_type": str(mapping["event_type"]),
        "trade_id": trade_id,
        "idempotency_key": idempotency_key,
        "as_of_date": as_of_date,
        "occurred_at_utc": str(occurred_at_utc or utc_now_iso_z()),
        "payload": filtered_payload,
        "source": "ananta_delivery_pilot.dg1b.shadow",
        "mode": "shadow_emit_only",
    }


class RailsEventAdapter(Protocol):
    def emit(self, envelope: Mapping[str, Any]) -> dict[str, Any]:
        ...


class LocalNoopRailsAdapter:
    def __init__(self, *, rails_write_enabled: bool) -> None:
        self.rails_write_enabled = bool(rails_write_enabled)

    def emit(self, envelope: Mapping[str, Any]) -> dict[str, Any]:
        reason_code = "RAILS_NETWORK_WRITE_DISABLED" if self.rails_write_enabled else "RAILS_WRITE_DISABLED"
        return {
            "ok": True,
            "sent": False,
            "reason_code": reason_code,
            "event_type": str(envelope.get("event_type") or ""),
            "idempotency_key": str(envelope.get("idempotency_key") or ""),
        }


class RailsTruthShadowEmitter:
    def __init__(self, *, flags: Mapping[str, Any], adapter: RailsEventAdapter | None = None) -> None:
        resolved_flags = resolve_rails_truth_flags(flags)
        self.flags = resolved_flags
        self.adapter: RailsEventAdapter = adapter or LocalNoopRailsAdapter(
            rails_write_enabled=bool(resolved_flags.get("rails_write_enabled"))
        )

    def emit(
        self,
        *,
        action: str,
        payload: Mapping[str, Any],
        as_of_date: str,
        occurred_at_utc: str | None = None,
    ) -> dict[str, Any]:
        canonical_action = canonical_trust_action(action)
        mapping = TRUST_ACTION_EVENT_MAPPING.get(canonical_action)
        if not mapping:
            return {
                "ok": False,
                "action": action,
                "canonical_action": canonical_action,
                "reason_code": "DG1B_UNKNOWN_ACTION",
            }
        if bool(mapping.get("derived")):
            return {
                "ok": False,
                "action": action,
                "canonical_action": canonical_action,
                "reason_code": "DG1B_DERIVED_ACTION_NO_TRUST_EVENT",
            }
        try:
            envelope = build_shadow_event_envelope(
                action=canonical_action,
                payload=payload,
                as_of_date=as_of_date,
                occurred_at_utc=occurred_at_utc,
            )
        except Exception as exc:
            return {
                "ok": False,
                "action": action,
                "canonical_action": canonical_action,
                "reason_code": "DG1B_ENVELOPE_BUILD_FAILED",
                "error": str(exc),
            }

        adapter_result = self.adapter.emit(envelope)
        decision_log = {
            "action": action,
            "canonical_action": canonical_action,
            "event_type": envelope["event_type"],
            "idempotency_key": envelope["idempotency_key"],
            "mode": "shadow_emit_only",
            "rails_write_enabled": bool(self.flags.get("rails_write_enabled")),
            "adapter_result": adapter_result,
        }
        return {
            "ok": True,
            "envelope": envelope,
            "decision_log": decision_log,
        }

