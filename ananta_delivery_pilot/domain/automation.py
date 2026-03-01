from __future__ import annotations

import difflib
import json
import time
from datetime import date, datetime, timedelta, timezone
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any

from adapters.sqlite_repo import SQLiteRepo
from core.config import RuntimeConfig
from core.hashing import canonical_json_sha256, sha256_file
from core.ids import new_ulid
from core.time import utc_now_iso_z
from core.units import mt_to_kg_int
from domain.services import Phase1Service

PR8_BENCHMARK_VERSION = "phase2.pr12.v1"
PR9_BENCHMARK_VERSION = "phase2.pr9.v1"
PR10_BENCHMARK_VERSION = "phase2.pr10.v1"

DRIFT_ALLOWED_REASON_CODES = {
    "pass",
    "insufficient_live_data",
    "insufficient_benchmark_data",
    "benchmark_version_mismatch",
    "drift_exceeds_threshold",
    "drift_within_watch_band",
}

DRIFT_REASON_PRECEDENCE = [
    "benchmark_version_mismatch",
    "insufficient_benchmark_data",
    "insufficient_live_data",
    "drift_exceeds_threshold",
    "drift_within_watch_band",
    "pass",
]

ROOT_CAUSE_ALLOWED_CODES = {
    "insufficient_observability_data",
    "benchmark_dataset_misalignment",
    "input_quality_regression",
    "planning_policy_mismatch",
    "transport_assignment_instability",
    "document_linkage_instability",
    "settlement_matching_instability",
    "manual_override_concentration",
    "no_recurring_root_cause",
}

ROOT_CAUSE_REASON_PRECEDENCE = [
    "insufficient_observability_data",
    "benchmark_dataset_misalignment",
    "manual_override_concentration",
    "input_quality_regression",
    "planning_policy_mismatch",
    "transport_assignment_instability",
    "document_linkage_instability",
    "settlement_matching_instability",
    "no_recurring_root_cause",
]

PLAYBOOK_ALLOWED_CODES = {
    "PB_OBSERVABILITY_RECOVERY",
    "PB_BENCHMARK_ALIGNMENT",
    "PB_INTAKE_QUALITY_STABILIZATION",
    "PB_PLANNING_POLICY_REVIEW",
    "PB_TRANSPORT_ASSIGNMENT_RETRAIN",
    "PB_DOCUMENT_LINKAGE_TUNING",
    "PB_SETTLEMENT_MATCHING_REVIEW",
    "PB_MANUAL_OVERRIDE_REDUCTION",
    "PB_MONITOR_ONLY",
}

ROOT_CAUSE_PLAYBOOK_MAP: dict[str, tuple[str, str | None]] = {
    "insufficient_observability_data": ("PB_OBSERVABILITY_RECOVERY", "PB_MONITOR_ONLY"),
    "benchmark_dataset_misalignment": ("PB_BENCHMARK_ALIGNMENT", "PB_MONITOR_ONLY"),
    "input_quality_regression": ("PB_INTAKE_QUALITY_STABILIZATION", "PB_MANUAL_OVERRIDE_REDUCTION"),
    "planning_policy_mismatch": ("PB_PLANNING_POLICY_REVIEW", "PB_MANUAL_OVERRIDE_REDUCTION"),
    "transport_assignment_instability": ("PB_TRANSPORT_ASSIGNMENT_RETRAIN", "PB_MANUAL_OVERRIDE_REDUCTION"),
    "document_linkage_instability": ("PB_DOCUMENT_LINKAGE_TUNING", "PB_MANUAL_OVERRIDE_REDUCTION"),
    "settlement_matching_instability": ("PB_SETTLEMENT_MATCHING_REVIEW", "PB_MANUAL_OVERRIDE_REDUCTION"),
    "manual_override_concentration": ("PB_MANUAL_OVERRIDE_REDUCTION", "PB_MONITOR_ONLY"),
    "no_recurring_root_cause": ("PB_MONITOR_ONLY", None),
}

DEFAULT_DRIFT_THRESHOLDS: dict[str, Any] = {
    "version": "phase2.pr13.defaults.v1",
    "pr8": {
        "median_manual_fields_per_intake": {
            "delta_type": "absolute",
            "direction": "increase",
            "watch": 0.5,
            "alert": 1.0,
        },
        "autoplan_zero_edit_common_case_rate": {
            "delta_type": "absolute",
            "direction": "decrease",
            "watch": 0.05,
            "alert": 0.10,
        },
    },
    "pr9": {
        "manual_transport_fields_per_delivery": {
            "delta_type": "absolute",
            "direction": "increase",
            "watch": 0.30,
            "alert": 0.75,
        },
        "doc_autolink_precision": {
            "delta_type": "absolute",
            "direction": "decrease",
            "watch": 0.03,
            "alert": 0.05,
        },
    },
    "pr10": {
        "payment_suggestion_acceptance_rate": {
            "delta_type": "absolute",
            "direction": "decrease",
            "watch": 0.05,
            "alert": 0.10,
        },
        "auto_action_success_rate": {
            "delta_type": "absolute",
            "direction": "decrease",
            "watch": 0.05,
            "alert": 0.10,
        },
    },
}


@dataclass
class ResolutionResult:
    value: str | None
    confidence: float
    decision: str
    reason_code: str
    source_type: str
    source_ref: str
    suggestions: list[dict[str, Any]]


class AutomationOrchestrator:
    def __init__(self, config: RuntimeConfig, repo: SQLiteRepo, phase1: Phase1Service) -> None:
        self.config = config
        self.repo = repo
        self.phase1 = phase1
        threshold_cfg = config.automation_thresholds.get("confidence", {}) if isinstance(config.automation_thresholds, dict) else {}
        self.identity_auto_min = float(threshold_cfg.get("identity_auto_apply_min", 0.92))
        self.identity_review_min = float(threshold_cfg.get("identity_review_min", 0.70))
        self.payment_auto_min = float(threshold_cfg.get("payment_auto_apply_min", 0.90))
        self.transport_auto_min = float(threshold_cfg.get("transport_auto_apply_min", 0.90))
        self.transport_review_min = float(threshold_cfg.get("transport_review_min", 0.65))
        self.autonomy_intent_order = [
            "plan_deliveries",
            "materialize_due",
            "auto_progress",
            "generate_pack",
            "apply_payment",
        ]

    def auto_run(self, payload: dict[str, Any], *, as_of_date: str, dry_run: bool = False, resume_from_run_id: str | None = None) -> dict[str, Any]:
        self.phase1.init_db()
        self.phase1.refresh_contract_state(as_of_date=as_of_date)
        started = time.time()
        normalized = self._normalize_payload(payload)
        if resume_from_run_id:
            normalized = self._apply_resolutions(normalized, resume_from_run_id)

        idempotency_seed = {
            "normalized": normalized,
            "as_of_date": as_of_date,
            "dry_run": dry_run,
            "resume_from_run_id": resume_from_run_id or "",
        }
        idempotency_key = canonical_json_sha256(idempotency_seed)
        existing = self.repo.get_automation_run_by_idempotency(idempotency_key)
        if existing and existing.get("status") in {"COMPLETED", "FAILED", "NEEDS_REVIEW"}:
            response = {
                "ok": True,
                "replayed": True,
                "run_id": existing["run_id"],
                "status": existing["status"],
                "metrics": json.loads(existing["metrics_json"]) if existing.get("metrics_json") else {},
            }
            return response

        run_id = new_ulid()
        with self.repo.transaction() as conn:
            self.repo.create_automation_run(
                conn,
                run_id=run_id,
                idempotency_key=idempotency_key,
                as_of_date=as_of_date,
                dry_run=dry_run,
                input_payload=payload,
            )

        context: dict[str, Any] = {
            "run_id": run_id,
            "normalized": normalized,
            "decisions": [],
            "exceptions": [],
            "critical_blocked": False,
            "results": {},
            "dry_run": dry_run,
            "as_of_date": as_of_date,
            "started_at": utc_now_iso_z(),
        }

        try:
            self._stage_resolve_entities(context)
            self._stage_create_contract(context)
            self._stage_intake_evidence(context)
            self._stage_delivery_plan(context)
            self._stage_delivery_materialization(context)
            if "delivery" not in context["results"]:
                self._stage_create_delivery(context)
            self._stage_record_coa(context)
            self._stage_generate_pack(context)
            self._stage_payment(context)
            self._stage_export(context)
            status = "COMPLETED" if not context["critical_blocked"] and not self._has_open_blockers(run_id) else "NEEDS_REVIEW"
        except Exception as error:
            status = "FAILED"
            self._add_exception(
                run_id=run_id,
                stage="orchestrator",
                exception_type="runtime_error",
                severity="BLOCKER",
                field_name="pipeline",
                proposed_value=None,
                reason=str(error),
                suggestions=[],
            )
            context["critical_blocked"] = True

        metrics = self._build_metrics(run_id=run_id, started_epoch=started, status=status)
        with self.repo.transaction() as conn:
            self.repo.complete_automation_run(
                conn,
                run_id=run_id,
                status=status,
                normalized_payload=context["normalized"],
                metrics=metrics,
                started_at=context["started_at"],
                failure_reason=metrics.get("failure_reason"),
            )

        return {
            "ok": status in {"COMPLETED", "NEEDS_REVIEW"},
            "run_id": run_id,
            "status": status,
            "dry_run": dry_run,
            "results": context["results"],
            "metrics": metrics,
            "open_exceptions": self.repo.list_exceptions(run_id=run_id, status="OPEN"),
        }

    def auto_resume(self, run_id: str) -> dict[str, Any]:
        row = self.repo.get_automation_run(run_id)
        if not row:
            raise ValueError(f"Unknown run_id: {run_id}")
        input_payload = json.loads(row["input_json"])
        return self.auto_run(
            input_payload,
            as_of_date=str(row["as_of_date"]),
            dry_run=bool(int(row["dry_run"] or 0)),
            resume_from_run_id=run_id,
        )

    def list_exceptions(self, *, run_id: str | None = None) -> list[dict[str, Any]]:
        rows = self.repo.list_exceptions(run_id=run_id, status="OPEN")
        for row in rows:
            row["suggestions"] = json.loads(row.get("suggestions_json") or "[]")
        return rows

    def resolve_exception(self, *, exception_id: str, value: str, note: str) -> dict[str, Any]:
        return self.repo.resolve_exception(exception_id=exception_id, value=value, note=note)

    def run_autonomy(self, *, as_of_date: str, contract_id: str | None = None, dry_run: bool = False) -> dict[str, Any]:
        self.phase1.init_db()
        self.phase1.refresh_contract_state(as_of_date=as_of_date)
        autonomy_run_id = new_ulid()
        started = time.time()
        with self.repo.transaction() as conn:
            self.repo.append_event(
                conn,
                entity_type="AUTONOMY_RUN",
                entity_id=autonomy_run_id,
                event_type="RUN_STARTED",
                as_of_date=as_of_date,
                payload={"contract_id": contract_id, "dry_run": dry_run},
                source="run-autonomy",
            )

        contracts = self._contracts_for_autonomy(contract_id=contract_id)
        per_contract: list[dict[str, Any]] = []
        for contract in contracts:
            per_contract.append(
                self._run_contract_autonomy(
                    autonomy_run_id=autonomy_run_id,
                    contract=contract,
                    as_of_date=as_of_date,
                    dry_run=dry_run,
                )
            )

        summary = {
            "contracts_processed": len(per_contract),
            "intents_executed": sum(int(item.get("intents_executed", 0)) for item in per_contract),
            "intents_blocked": sum(int(item.get("intents_blocked", 0)) for item in per_contract),
            "exceptions_open": sum(int(item.get("exceptions_open", 0)) for item in per_contract),
            "duration_seconds": round(max(0.0, time.time() - started), 3),
        }
        with self.repo.transaction() as conn:
            self.repo.append_event(
                conn,
                entity_type="AUTONOMY_RUN",
                entity_id=autonomy_run_id,
                event_type="RUN_COMPLETED",
                as_of_date=as_of_date,
                payload=summary,
                source="run-autonomy",
            )
        return {
            "ok": True,
            "autonomy_run_id": autonomy_run_id,
            "as_of_date": as_of_date,
            "dry_run": dry_run,
            "contract_id": contract_id,
            "summary": summary,
            "contracts": per_contract,
        }

    def list_cases(self, *, status: str = "OPEN") -> dict[str, Any]:
        rows = self.repo.list_exception_cases(status=status)
        for row in rows:
            details_json = row.get("details_json")
            row["details"] = json.loads(details_json) if details_json else {}
        return {"cases": rows, "status": status, "count": len(rows)}

    def decide_case(
        self,
        *,
        case_id: str,
        decision: str,
        reason: str,
        user_id: str | None = None,
        resume: bool = True,
        dry_run_resume: bool = False,
    ) -> dict[str, Any]:
        normalized_decision = str(decision).strip().upper()
        if normalized_decision not in {"APPROVE", "REJECT", "OVERRIDE"}:
            raise ValueError("decision must be APPROVE, REJECT, or OVERRIDE")
        normalized_reason = str(reason).strip()
        if not normalized_reason:
            raise ValueError("reason is required")
        case = self.repo.get_exception_case(case_id)
        if not case:
            raise ValueError(f"Unknown case_id: {case_id}")
        idempotency_key = f"{case_id}|{normalized_decision}|{normalized_reason}"
        case_details = json.loads(case.get("details_json") or "{}")
        suggestion_ids = case_details.get("suggestion_ids") if isinstance(case_details.get("suggestion_ids"), list) else []
        case_type = str(case.get("case_type") or "")
        case_as_of = str(case_details.get("as_of_date") or utc_now_iso_z()[:10])
        with self.repo.transaction() as conn:
            decision_row = self.repo.add_human_decision(
                conn,
                exception_case_id=case_id,
                user_id=user_id,
                decision=normalized_decision,
                reason=normalized_reason,
                payload={"resume": resume, "dry_run_resume": dry_run_resume},
                idempotency_key=idempotency_key,
            )
            case_status = "RESOLVED" if normalized_decision in {"APPROVE", "OVERRIDE"} else "REJECTED"
            case_row = self.repo.resolve_exception_case(conn, exception_case_id=case_id, status=case_status)
            self.repo.append_event(
                conn,
                entity_type="EXCEPTION_CASE",
                entity_id=case_id,
                event_type="CASE_DECIDED",
                as_of_date=None,
                payload={
                    "decision": normalized_decision,
                    "reason": normalized_reason,
                    "status": case_status,
                    "decision_id": decision_row["human_decision_id"],
                },
                source="decide-case",
            )
            if suggestion_ids:
                self.repo.mark_transport_suggestion_feedback(
                    conn,
                    suggestion_ids=[str(item) for item in suggestion_ids if str(item).strip()],
                    accepted=normalized_decision in {"APPROVE", "OVERRIDE"},
                )
                self.repo.add_decision_feature(
                    conn,
                    exception_case_id=case_id,
                    feature_key="transport_suggestion_ids",
                    feature_payload={"suggestion_ids": suggestion_ids},
                )
                self.repo.add_decision_outcome(
                    conn,
                    exception_case_id=case_id,
                    human_decision_id=str(decision_row["human_decision_id"]),
                    outcome_label="transport_suggestion_feedback",
                    outcome_payload={
                        "accepted": bool(normalized_decision in {"APPROVE", "OVERRIDE"}),
                        "suggestion_ids": [str(item) for item in suggestion_ids if str(item).strip()],
                    },
                )
            if case_type == "document_linkage":
                evidence_id = str(case_details.get("evidence_id") or "").strip()
                selected_candidate = (
                    case_details.get("selected_candidate")
                    if isinstance(case_details.get("selected_candidate"), dict)
                    else {}
                )
                target_delivery_id = str(selected_candidate.get("delivery_id") or "").strip() or None
                target_sales_transaction_id = str(selected_candidate.get("sales_transaction_id") or "").strip() or None
                target_sales_line_id = str(selected_candidate.get("sales_line_id") or "").strip() or None
                if evidence_id:
                    if normalized_decision in {"APPROVE", "OVERRIDE"}:
                        conn.execute(
                            """
                            UPDATE evidence_originals
                            SET delivery_id = COALESCE(?, delivery_id),
                                sales_transaction_id = COALESCE(?, sales_transaction_id),
                                sales_line_id = COALESCE(?, sales_line_id),
                                link_status = 'MANUAL_LINKED',
                                link_reason_code = 'manual_link_approved',
                                link_source = 'exception_decision',
                                linked_at = ?,
                                updated_at = ?
                            WHERE evidence_id = ?
                            """,
                            (
                                target_delivery_id,
                                target_sales_transaction_id,
                                target_sales_line_id,
                                utc_now_iso_z(),
                                utc_now_iso_z(),
                                evidence_id,
                            ),
                        )
                        doc_outcome_label = "DOC_LINK_APPROVED"
                    else:
                        conn.execute(
                            """
                            UPDATE evidence_originals
                            SET link_status = 'AUTO_LINK_REJECTED',
                                link_reason_code = 'manual_link_rejected',
                                link_source = 'exception_decision',
                                updated_at = ?
                            WHERE evidence_id = ?
                            """,
                            (utc_now_iso_z(), evidence_id),
                        )
                        doc_outcome_label = "DOC_LINK_REJECTED"
                    self.repo.append_event(
                        conn,
                        entity_type="EVIDENCE",
                        entity_id=evidence_id,
                        event_type="EVIDENCE_LINK_DECIDED",
                        as_of_date=case_as_of,
                        payload={
                            "case_id": case_id,
                            "decision": normalized_decision,
                            "delivery_id": target_delivery_id,
                            "sales_transaction_id": target_sales_transaction_id,
                            "sales_line_id": target_sales_line_id,
                        },
                        source="decide-case",
                    )
                    self.repo.add_decision_outcome(
                        conn,
                        exception_case_id=case_id,
                        human_decision_id=str(decision_row["human_decision_id"]),
                        outcome_label=doc_outcome_label,
                        outcome_payload={
                            "evidence_id": evidence_id,
                            "decision": normalized_decision,
                            "delivery_id": target_delivery_id,
                            "sales_transaction_id": target_sales_transaction_id,
                            "sales_line_id": target_sales_line_id,
                        },
                    )
            self.repo.add_decision_outcome(
                conn,
                exception_case_id=case_id,
                human_decision_id=str(decision_row["human_decision_id"]),
                outcome_label="APPROVED" if normalized_decision in {"APPROVE", "OVERRIDE"} else "REJECTED",
                outcome_payload={
                    "decision": normalized_decision,
                    "reason": normalized_reason,
                    "case_type": case_type,
                    "reason_code": case.get("reason_code"),
                },
            )
            if resume and normalized_decision in {"APPROVE", "OVERRIDE"} and case.get("contract_id"):
                self.repo.append_event(
                    conn,
                    entity_type="EXCEPTION_CASE",
                    entity_id=case_id,
                    event_type="CASE_RESUME_REQUESTED",
                    as_of_date=case_as_of,
                    payload={
                        "decision": normalized_decision,
                        "dry_run_resume": bool(dry_run_resume),
                        "contract_id": str(case.get("contract_id") or ""),
                    },
                    source="decide-case",
                )
        resume_result: dict[str, Any] | None = None
        if resume and normalized_decision in {"APPROVE", "OVERRIDE"} and case.get("contract_id"):
            try:
                resume_result = self.run_autonomy(
                    as_of_date=case_as_of,
                    contract_id=str(case["contract_id"]),
                    dry_run=bool(dry_run_resume),
                )
                with self.repo.transaction() as conn:
                    self.repo.append_event(
                        conn,
                        entity_type="EXCEPTION_CASE",
                        entity_id=case_id,
                        event_type="CASE_RESUME_COMPLETED",
                        as_of_date=case_as_of,
                        payload={
                            "autonomy_run_id": str(resume_result.get("autonomy_run_id") or ""),
                            "ok": bool(resume_result.get("ok")),
                            "dry_run_resume": bool(dry_run_resume),
                        },
                        source="decide-case",
                    )
            except Exception as error:
                resume_result = {
                    "ok": False,
                    "error": str(error),
                    "autonomy_run_id": None,
                }
                with self.repo.transaction() as conn:
                    self.repo.append_event(
                        conn,
                        entity_type="EXCEPTION_CASE",
                        entity_id=case_id,
                        event_type="CASE_RESUME_FAILED",
                        as_of_date=case_as_of,
                        payload={
                            "error": str(error),
                            "dry_run_resume": bool(dry_run_resume),
                        },
                        source="decide-case",
                    )
        return {
            "ok": True,
            "case": case_row,
            "decision": decision_row,
            "resume_result": resume_result,
        }

    def autonomy_metrics(
        self,
        *,
        as_of_date: str,
        out_dir: Path,
        lookback_window_days: int = 30,
        benchmark_version: str = "phase2.pr10.v1",
    ) -> dict[str, Any]:
        metrics = self.compute_metrics_snapshot(
            as_of_date=as_of_date,
            lookback_window_days=lookback_window_days,
            benchmark_version=benchmark_version,
        )
        out_dir.mkdir(parents=True, exist_ok=True)
        metrics_path = out_dir / f"autonomy_metrics_{as_of_date}.json"
        metrics_path.write_text(json.dumps(metrics, indent=2, sort_keys=True), encoding="utf-8")
        with self.repo.transaction() as conn:
            self.repo.append_event(
                conn,
                entity_type="METRICS",
                entity_id=f"AUTONOMY::{as_of_date}::{benchmark_version}",
                event_type="AUTONOMY_METRICS_EXPORTED",
                as_of_date=as_of_date,
                payload={
                    "metrics_path": str(metrics_path),
                    "lookback_window_days": int(lookback_window_days),
                    "benchmark_version": benchmark_version,
                    "generated_at_utc": str(metrics.get("generated_at_utc") or ""),
                    "pr8_gate_pass": bool(metrics.get("pr8_gate_pass")),
                    "pr8_gate_reason_code": str(metrics.get("pr8_gate_reason_code") or ""),
                    "pr9_gate_pass": bool(metrics.get("pr9_gate_pass")),
                    "pr9_gate_reason_code": str(metrics.get("pr9_gate_reason_code") or ""),
                    "pr10_gate_pass": bool(metrics.get("pr10_gate_pass")),
                    "pr10_gate_reason_code": str(metrics.get("pr10_gate_reason_code") or ""),
                },
                source="autonomy-metrics",
            )
        return {"ok": True, "metrics": metrics, "metrics_path": str(metrics_path)}

    def seed_phase2_benchmark(
        self,
        *,
        as_of_date: str,
        benchmark_version: str,
        reset: bool = False,
        lookback_window_days: int = 30,
    ) -> dict[str, Any]:
        self.phase1.init_db()
        if lookback_window_days <= 0:
            raise ValueError("lookback_window_days must be > 0")
        try:
            as_of = date.fromisoformat(as_of_date)
        except ValueError as error:
            raise ValueError("as_of_date must be YYYY-MM-DD") from error

        existing = self.repo.get_benchmark_run(as_of_date=as_of_date, benchmark_version=benchmark_version)
        if existing and not reset:
            return {
                "ok": True,
                "reused": True,
                "as_of_date": as_of_date,
                "benchmark_version": benchmark_version,
                "benchmark_run_id": str(existing.get("benchmark_run_id") or ""),
                "lookback_window_days": int(existing.get("lookback_window_days") or lookback_window_days),
                "seeded_at_utc": str(existing.get("seeded_at_utc") or ""),
                "fixture_counts": json.loads(existing.get("fixture_counts_json") or "{}"),
                "fixture_metadata": json.loads(existing.get("fixture_metadata_json") or "{}"),
            }

        fixture_key = f"{as_of_date}|{benchmark_version}"
        benchmark_run_id = f"BRUN-{canonical_json_sha256({'fixture_key': fixture_key})[:20]}"
        seeded_at_utc = utc_now_iso_z()
        issue_date = (as_of - timedelta(days=14)).isoformat()
        due_date_open = (as_of - timedelta(days=20)).isoformat()
        due_date_paid = (as_of - timedelta(days=10)).isoformat()
        invoice_date = (as_of - timedelta(days=7)).isoformat()
        current_case_created = (as_of - timedelta(days=3)).isoformat()
        previous_case_created = (as_of - timedelta(days=11)).isoformat()

        contract_id = f"CTR-{canonical_json_sha256({'fixture_key': fixture_key, 'kind': 'contract'})[:20]}"
        contract_line_id = f"CLN-{canonical_json_sha256({'fixture_key': fixture_key, 'kind': 'line'})[:20]}"
        planned_ids = [
            f"PLN-{canonical_json_sha256({'fixture_key': fixture_key, 'kind': 'plan', 'idx': idx})[:20]}"
            for idx in range(1, 6)
        ]
        delivery_paid_id = f"DLV-{canonical_json_sha256({'fixture_key': fixture_key, 'kind': 'delivery', 'idx': 1})[:20]}"
        delivery_open_id = f"DLV-{canonical_json_sha256({'fixture_key': fixture_key, 'kind': 'delivery', 'idx': 2})[:20]}"
        snapshot_id = f"SNAP-{canonical_json_sha256({'fixture_key': fixture_key, 'kind': 'snapshot'})[:20]}"
        sales_paid_id = f"SAL-{canonical_json_sha256({'fixture_key': fixture_key, 'kind': 'sales', 'idx': 1})[:20]}"
        sales_open_id = f"SAL-{canonical_json_sha256({'fixture_key': fixture_key, 'kind': 'sales', 'idx': 2})[:20]}"
        sales_line_paid_id = f"SLL-{canonical_json_sha256({'fixture_key': fixture_key, 'kind': 'sales-line', 'idx': 1})[:20]}"
        sales_line_open_id = f"SLL-{canonical_json_sha256({'fixture_key': fixture_key, 'kind': 'sales-line', 'idx': 2})[:20]}"
        payment_id = f"PAY-{canonical_json_sha256({'fixture_key': fixture_key, 'kind': 'payment'})[:20]}"
        allocation_id = f"PAL-{canonical_json_sha256({'fixture_key': fixture_key, 'kind': 'allocation'})[:20]}"
        intake_run_id = f"ARUN-{canonical_json_sha256({'fixture_key': fixture_key, 'kind': 'intake-run'})[:20]}"
        case_current_id = f"CASE-{canonical_json_sha256({'fixture_key': fixture_key, 'kind': 'case', 'idx': 1})[:20]}"
        case_previous_id = f"CASE-{canonical_json_sha256({'fixture_key': fixture_key, 'kind': 'case', 'idx': 2})[:20]}"
        decision_current_id = f"HDEC-{canonical_json_sha256({'fixture_key': fixture_key, 'kind': 'decision', 'idx': 1})[:20]}"
        decision_previous_id = f"HDEC-{canonical_json_sha256({'fixture_key': fixture_key, 'kind': 'decision', 'idx': 2})[:20]}"
        invoice_token = canonical_json_sha256({"fixture_key": fixture_key, "kind": "invoice-token"})[:6].upper()

        now = utc_now_iso_z()
        with self.repo.transaction() as conn:
            buyer = conn.execute("SELECT * FROM parties WHERE party_id = 'buyer_nycil'").fetchone()
            vendor = conn.execute("SELECT * FROM parties WHERE party_id = 'ananta_flows'").fetchone()
            operator = conn.execute("SELECT * FROM parties WHERE party_id = 'guildgate'").fetchone()
            if not buyer or not vendor or not operator:
                raise ValueError("Required seeded parties are missing; run init-db first")
            snapshot_payload = {
                "buyer": {"party_id": "buyer_nycil", "name": str(buyer["legal_name"])},
                "vendor_of_record": {"party_id": "ananta_flows", "name": str(vendor["legal_name"])},
                "operator": {"party_id": "guildgate", "name": str(operator["legal_name"])},
                "fixture_key": fixture_key,
            }
            conn.execute(
                """
                INSERT INTO parties_snapshot(
                  snapshot_id, buyer_id, buyer_name, buyer_tin, buyer_rc_number,
                  vendor_of_record_id, vendor_of_record_name, vendor_of_record_tin, vendor_of_record_rc_number,
                  operator_id, operator_name, operator_tin, operator_rc_number, payload_json, created_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(snapshot_id) DO UPDATE SET
                  payload_json = excluded.payload_json,
                  created_at = excluded.created_at
                """,
                (
                    snapshot_id,
                    "buyer_nycil",
                    str(buyer["legal_name"]),
                    buyer["tin"],
                    buyer["rc_number"],
                    "ananta_flows",
                    str(vendor["legal_name"]),
                    vendor["tin"],
                    vendor["rc_number"],
                    "guildgate",
                    str(operator["legal_name"]),
                    operator["tin"],
                    operator["rc_number"],
                    json.dumps(snapshot_payload, sort_keys=True),
                    f"{issue_date}T08:00:00Z",
                ),
            )
            conn.execute(
                """
                INSERT INTO contracts(
                  contract_id, contract_ref, lpo_no, lpo_date, buyer_id, vendor_of_record_id, operator_id,
                  source_id, processor_id, lane, currency, issue_date, lpo_valid_from, lpo_valid_to, lpo_state,
                  due_date, due_terms, expected_total_qty, expected_total_qty_kg, expected_total_value,
                  over_delivery_tolerance_pct, status, notes, created_at, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?, ?, ?, 5.0, 'OPEN', ?, ?, ?)
                ON CONFLICT(contract_id) DO UPDATE SET
                  issue_date = excluded.issue_date,
                  lpo_valid_from = excluded.lpo_valid_from,
                  lpo_valid_to = excluded.lpo_valid_to,
                  expected_total_qty = excluded.expected_total_qty,
                  expected_total_qty_kg = excluded.expected_total_qty_kg,
                  expected_total_value = excluded.expected_total_value,
                  over_delivery_tolerance_pct = excluded.over_delivery_tolerance_pct,
                  status = excluded.status,
                  notes = excluded.notes,
                  updated_at = excluded.updated_at
                """,
                (
                    contract_id,
                    f"BENCH-{benchmark_version}-{as_of_date}",
                    f"LPO-BENCH-{benchmark_version}-{as_of_date}",
                    issue_date,
                    "buyer_nycil",
                    "ananta_flows",
                    "guildgate",
                    "ananta_flows",
                    "processor_partner_refinery",
                    "B",
                    "NGN",
                    issue_date,
                    issue_date,
                    as_of_date,
                    due_date_open,
                    "14 days",
                    150.0,
                    150000,
                    340500000.0,
                    f"phase2_benchmark fixture={fixture_key}",
                    f"{issue_date}T07:30:00Z",
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO contract_line_items(
                  contract_line_id, contract_id, line_no, product_code, description, expected_qty, delivered_qty,
                  expected_qty_kg, delivered_qty_kg, unit, unit_price, unit_price_basis, expected_value, created_at, updated_at
                )
                VALUES(?, ?, 1, 'RBDPO', 'Phase2 benchmark lot policy line', 150.0, 60.0, 150000, 60000, 'mt', 2270.0, 'KG', 340500000.0, ?, ?)
                ON CONFLICT(contract_line_id) DO UPDATE SET
                  expected_qty = excluded.expected_qty,
                  expected_qty_kg = excluded.expected_qty_kg,
                  delivered_qty = excluded.delivered_qty,
                  delivered_qty_kg = excluded.delivered_qty_kg,
                  updated_at = excluded.updated_at
                """,
                (contract_line_id, contract_id, f"{issue_date}T07:31:00Z", now),
            )

            for idx, planned_id in enumerate(planned_ids, start=1):
                planned_date = (as_of - timedelta(days=6 - idx)).isoformat()
                conn.execute(
                    """
                    INSERT INTO planned_deliveries(
                      planned_delivery_id, contract_id, contract_line_id, sequence_no, planned_qty_kg, lot_size_kg,
                      planned_date, run_id, batch_id, status, delivery_id, notes, materialized_qty_kg, delivered_qty_kg, created_at, updated_at
                    )
                    VALUES(?, ?, ?, ?, 30000, 30000, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(planned_delivery_id) DO UPDATE SET
                      planned_date = excluded.planned_date,
                      run_id = excluded.run_id,
                      batch_id = excluded.batch_id,
                      status = excluded.status,
                      delivery_id = excluded.delivery_id,
                      notes = excluded.notes,
                      materialized_qty_kg = excluded.materialized_qty_kg,
                      delivered_qty_kg = excluded.delivered_qty_kg,
                      updated_at = excluded.updated_at
                    """,
                    (
                        planned_id,
                        contract_id,
                        contract_line_id,
                        idx,
                        planned_date,
                        f"RUN-BENCH-{idx:02d}",
                        f"AFL-RBDPO-BENCH-{idx:02d}",
                        "PAID" if idx == 1 else ("INVOICED" if idx == 2 else "PLANNED"),
                        None,
                        f"auto_plan fixture={fixture_key}",
                        30000 if idx in {1, 2} else 0,
                        30000 if idx in {1, 2} else 0,
                        f"{planned_date}T07:00:00Z",
                        now,
                    ),
                )

            deliveries = [
                (
                    delivery_paid_id,
                    "RUN-BENCH-01",
                    "AFL-RBDPO-BENCH-01",
                    "PAID",
                    (as_of - timedelta(days=6)).isoformat(),
                    f"{(as_of - timedelta(days=6)).isoformat()}T09:00:00Z",
                    f"{(as_of - timedelta(days=5)).isoformat()}T12:00:00Z",
                    f"{(as_of - timedelta(days=4)).isoformat()}T14:00:00Z",
                    f"{(as_of - timedelta(days=3)).isoformat()}T11:00:00Z",
                ),
                (
                    delivery_open_id,
                    "RUN-BENCH-02",
                    "AFL-RBDPO-BENCH-02",
                    "INVOICED",
                    (as_of - timedelta(days=5)).isoformat(),
                    f"{(as_of - timedelta(days=5)).isoformat()}T09:15:00Z",
                    f"{(as_of - timedelta(days=4)).isoformat()}T13:00:00Z",
                    f"{(as_of - timedelta(days=3)).isoformat()}T15:00:00Z",
                    None,
                ),
            ]
            for delivery_id, run_id, batch_id, status, delivery_date, dispatched_at, delivered_at, invoiced_at, paid_at in deliveries:
                conn.execute(
                    """
                    INSERT INTO deliveries(
                      delivery_id, contract_id, contract_line_id, delivery_ref, run_id, batch_id, delivery_date,
                      delivered_qty, delivered_qty_kg, unit, unit_price, unit_price_basis, gross_amount,
                      truck_no, driver_name, driver_phone, notes, status, dispatched_at, delivered_at, invoiced_at, paid_at, created_at, updated_at
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, 30000, 30000, 'kgs', 2270.0, 'KG', 68100000.0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(delivery_id) DO UPDATE SET
                      status = excluded.status,
                      dispatched_at = excluded.dispatched_at,
                      delivered_at = excluded.delivered_at,
                      invoiced_at = excluded.invoiced_at,
                      paid_at = excluded.paid_at,
                      notes = excluded.notes,
                      updated_at = excluded.updated_at
                    """,
                    (
                        delivery_id,
                        contract_id,
                        contract_line_id,
                        f"DLV-BENCH-{delivery_id[-4:]}",
                        run_id,
                        batch_id,
                        delivery_date,
                        f"T{delivery_id[-4:]}",
                        "Idowu Atanda",
                        "08052803019",
                        f"benchmark fixture={fixture_key}",
                        status,
                        dispatched_at,
                        delivered_at,
                        invoiced_at,
                        paid_at,
                        f"{delivery_date}T08:00:00Z",
                        now,
                    ),
                )
            conn.execute(
                "UPDATE planned_deliveries SET delivery_id = ? WHERE planned_delivery_id = ?",
                (delivery_paid_id, planned_ids[0]),
            )
            conn.execute(
                "UPDATE planned_deliveries SET delivery_id = ? WHERE planned_delivery_id = ?",
                (delivery_open_id, planned_ids[1]),
            )

            sales_rows = [
                (
                    sales_paid_id,
                    delivery_paid_id,
                    sales_line_paid_id,
                    f"INV-BENCH-{invoice_token}-PAID-001",
                    due_date_paid,
                    68100000.0,
                    68100000.0,
                    0.0,
                ),
                (
                    sales_open_id,
                    delivery_open_id,
                    sales_line_open_id,
                    f"INV-BENCH-{invoice_token}-OPEN-001",
                    due_date_open,
                    68100000.0,
                    68100000.0,
                    0.0,
                ),
            ]
            for sales_id, delivery_id, sales_line_id, invoice_no, due_date, gross_amount, amount_due, expected_wht in sales_rows:
                conn.execute(
                    """
                    INSERT INTO sales_transactions(
                      sales_transaction_id, contract_id, delivery_id, snapshot_id, vendor_of_record_id, buyer_id, operator_id,
                      lane, invoice_no, invoice_date, due_date, currency, gross_amount, amount_due, expected_wht_amount, created_at, updated_at
                    )
                    VALUES(?, ?, ?, ?, 'ananta_flows', 'buyer_nycil', 'guildgate', 'B', ?, ?, ?, 'NGN', ?, ?, ?, ?, ?)
                    ON CONFLICT(sales_transaction_id) DO UPDATE SET
                      due_date = excluded.due_date,
                      gross_amount = excluded.gross_amount,
                      amount_due = excluded.amount_due,
                      expected_wht_amount = excluded.expected_wht_amount,
                      updated_at = excluded.updated_at
                    """,
                    (
                        sales_id,
                        contract_id,
                        delivery_id,
                        snapshot_id,
                        invoice_no,
                        invoice_date,
                        due_date,
                        gross_amount,
                        amount_due,
                        expected_wht,
                        f"{invoice_date}T16:00:00Z",
                        now,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO sales_lines(
                      sales_line_id, sales_transaction_id, delivery_id, contract_line_id, line_no, product_code, description,
                      quantity, quantity_kg, unit, unit_price, unit_price_basis, gross_amount, created_at, updated_at
                    )
                    VALUES(?, ?, ?, ?, 1, 'RBDPO', 'Benchmark sales line', 30000.0, 30000, 'kgs', 2270.0, 'KG', 68100000.0, ?, ?)
                    ON CONFLICT(sales_line_id) DO UPDATE SET
                      quantity = excluded.quantity,
                      quantity_kg = excluded.quantity_kg,
                      gross_amount = excluded.gross_amount,
                      updated_at = excluded.updated_at
                    """,
                    (
                        sales_line_id,
                        sales_id,
                        delivery_id,
                        contract_line_id,
                        f"{invoice_date}T16:05:00Z",
                        now,
                    ),
                )

            conn.execute(
                """
                INSERT INTO payments(
                  payment_id, vendor_of_record_id, buyer_id, payment_date, amount_received, currency, payment_method,
                  external_reference, idempotency_key, receipt_no, created_at, updated_at
                )
                VALUES(?, 'ananta_flows', 'buyer_nycil', ?, 68100000.0, 'NGN', 'Bank Transfer', ?, ?, ?, ?, ?)
                ON CONFLICT(payment_id) DO UPDATE SET
                  amount_received = excluded.amount_received,
                  payment_date = excluded.payment_date,
                  updated_at = excluded.updated_at
                """,
                (
                    payment_id,
                    (as_of - timedelta(days=3)).isoformat(),
                    f"BANK-BENCH-{benchmark_version}-{as_of_date}",
                    f"bench-payment::{fixture_key}",
                    f"RCPT-BENCH-{as_of_date}-{invoice_token}",
                    f"{(as_of - timedelta(days=3)).isoformat()}T12:30:00Z",
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO payment_allocations(
                  allocation_id, payment_id, sales_transaction_id, allocated_amount, allocation_date, notes, created_at
                )
                VALUES(?, ?, ?, 68100000.0, ?, ?, ?)
                ON CONFLICT(allocation_id) DO UPDATE SET
                  allocated_amount = excluded.allocated_amount,
                  allocation_date = excluded.allocation_date,
                  notes = excluded.notes
                """,
                (
                    allocation_id,
                    payment_id,
                    sales_paid_id,
                    (as_of - timedelta(days=3)).isoformat(),
                    f"benchmark fixture={fixture_key}",
                    f"{(as_of - timedelta(days=3)).isoformat()}T12:31:00Z",
                ),
            )

            conn.execute(
                """
                INSERT INTO automation_runs(
                  run_id, idempotency_key, status, dry_run, as_of_date, input_json, normalized_input_json,
                  metrics_json, started_at, completed_at, duration_seconds, created_at, updated_at
                )
                VALUES(?, ?, 'COMPLETED', 1, ?, '{}', '{}', '{}', ?, ?, 1.0, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                  status = excluded.status,
                  as_of_date = excluded.as_of_date,
                  completed_at = excluded.completed_at,
                  updated_at = excluded.updated_at
                """,
                (
                    intake_run_id,
                    f"benchmark-intake::{fixture_key}",
                    (as_of - timedelta(days=2)).isoformat(),
                    f"{(as_of - timedelta(days=2)).isoformat()}T08:00:00Z",
                    f"{(as_of - timedelta(days=2)).isoformat()}T08:01:00Z",
                    f"{(as_of - timedelta(days=2)).isoformat()}T08:00:00Z",
                    now,
                ),
            )
            if benchmark_version == PR8_BENCHMARK_VERSION:
                parser_decisions = [
                    ("buyer_id", "auto_applied", 0.98),
                    ("vendor_of_record_id", "auto_applied", 0.98),
                    ("product_code", "auto_applied", 0.97),
                    ("expected_qty_kg", "auto_applied", 0.97),
                    ("unit_price", "auto_applied", 0.96),
                ]
                confirm_decisions = [
                    ("issue_date", "user_confirmed", 1.0),
                    ("lpo_valid_to", "user_confirmed", 1.0),
                ]
            else:
                parser_decisions = [
                    ("buyer_id", "auto_applied", 0.98),
                    ("vendor_of_record_id", "auto_applied", 0.97),
                    ("product_code", "auto_applied", 0.96),
                    ("expected_qty_kg", "auto_applied", 0.96),
                    ("unit_price", "needs_review", 0.82),
                ]
                confirm_decisions = [
                    ("unit_price", "user_corrected", 1.0),
                    ("description", "user_corrected", 1.0),
                    ("issue_date", "user_confirmed", 1.0),
                    ("lpo_valid_to", "user_confirmed", 1.0),
                ]
            for idx, (field_name, decision, confidence) in enumerate(parser_decisions, start=1):
                decision_id = f"ADEC-{canonical_json_sha256({'fixture_key': fixture_key, 'kind': 'parser-decision', 'idx': idx})[:20]}"
                conn.execute(
                    """
                    INSERT INTO automation_decisions(
                      decision_id, run_id, stage, field_name, required_flag, proposed_value,
                      source_type, source_ref, confidence, decision, reason_code, rule_path, created_at
                    )
                    VALUES(?, ?, 'intake_parser', ?, 1, 'fixture', 'parser', 'benchmark', ?, ?, ?, 'benchmark.intake_parser', ?)
                    ON CONFLICT(decision_id) DO UPDATE SET
                      confidence = excluded.confidence,
                      decision = excluded.decision,
                      reason_code = excluded.reason_code,
                      created_at = excluded.created_at
                    """,
                    (
                        decision_id,
                        intake_run_id,
                        field_name,
                        confidence,
                        decision,
                        "auto_threshold_met" if decision == "auto_applied" else "review_threshold",
                        f"{(as_of - timedelta(days=2)).isoformat()}T08:00:{idx:02d}Z",
                    ),
                )
            for idx, (field_name, decision, confidence) in enumerate(confirm_decisions, start=1):
                decision_id = f"ADEC-{canonical_json_sha256({'fixture_key': fixture_key, 'kind': 'confirm-decision', 'idx': idx})[:20]}"
                conn.execute(
                    """
                    INSERT INTO automation_decisions(
                      decision_id, run_id, stage, field_name, required_flag, proposed_value,
                      source_type, source_ref, confidence, decision, reason_code, rule_path, created_at
                    )
                    VALUES(?, ?, 'intake_confirm', ?, 1, 'fixture', 'user_input', '/v2/intake/confirm', ?, ?, ?, 'benchmark.intake_confirm', ?)
                    ON CONFLICT(decision_id) DO UPDATE SET
                      confidence = excluded.confidence,
                      decision = excluded.decision,
                      reason_code = excluded.reason_code,
                      created_at = excluded.created_at
                    """,
                    (
                        decision_id,
                        intake_run_id,
                        field_name,
                        confidence,
                        decision,
                        decision,
                        f"{(as_of - timedelta(days=2)).isoformat()}T08:01:{idx:02d}Z",
                    ),
                )

            for idx in range(1, 3):
                intent_id = f"INT-{canonical_json_sha256({'fixture_key': fixture_key, 'kind': 'intent', 'idx': idx})[:20]}"
                exec_id = f"EXE-{canonical_json_sha256({'fixture_key': fixture_key, 'kind': 'execution', 'idx': idx})[:20]}"
                created_at = f"{(as_of - timedelta(days=2)).isoformat()}T10:{idx:02d}:00Z"
                conn.execute(
                    """
                    INSERT INTO action_intents(
                      action_intent_id, autonomy_run_id, intent_type, contract_id, delivery_id, planned_delivery_id,
                      as_of_date, scheduled_at, status, policy_version, payload_json, idempotency_key, created_at, updated_at
                    )
                    VALUES(?, ?, 'materialize_due', ?, ?, ?, ?, ?, 'SUCCESS', ?, '{}', ?, ?, ?)
                    ON CONFLICT(action_intent_id) DO UPDATE SET
                      status = excluded.status,
                      as_of_date = excluded.as_of_date,
                      updated_at = excluded.updated_at
                    """,
                    (
                        intent_id,
                        f"AUTO-{fixture_key}",
                        contract_id,
                        delivery_paid_id if idx == 1 else delivery_open_id,
                        planned_ids[idx - 1],
                        as_of_date,
                        created_at,
                        benchmark_version,
                        f"bench-intent::{fixture_key}::{idx}",
                        created_at,
                        now,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO action_executions(
                      action_execution_id, action_intent_id, execution_no, status, idempotency_key,
                      request_json, response_json, error_json, executed_at, created_at
                    )
                    VALUES(?, ?, 1, ?, ?, '{}', '{}', NULL, ?, ?)
                    ON CONFLICT(action_execution_id) DO UPDATE SET
                      status = excluded.status,
                      executed_at = excluded.executed_at
                    """,
                    (
                        exec_id,
                        intent_id,
                        "SUCCESS",
                        f"bench-execution::{fixture_key}::{idx}",
                        created_at,
                        created_at,
                    ),
                )

            exception_cases = [
                (
                    case_previous_id,
                    previous_case_created,
                    f"{previous_case_created}T09:00:00Z",
                    f"{(as_of - timedelta(days=9)).isoformat()}T09:00:00Z",
                    decision_previous_id,
                    "previous_window",
                ),
                (
                    case_current_id,
                    current_case_created,
                    f"{current_case_created}T09:00:00Z",
                    f"{current_case_created}T12:00:00Z",
                    decision_current_id,
                    "current_window",
                ),
            ]
            for case_id, case_date, created_at, resolved_at, decision_id, label in exception_cases:
                conn.execute(
                    """
                    INSERT INTO exception_cases(
                      exception_case_id, autonomy_run_id, action_intent_id, contract_id, delivery_id, planned_delivery_id,
                      case_type, severity, status, reason_code, details_json, idempotency_key, created_at, updated_at, resolved_at
                    )
                    VALUES(?, ?, NULL, ?, NULL, NULL, 'benchmark_case', 'REVIEW', 'RESOLVED', ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(exception_case_id) DO UPDATE SET
                      status = excluded.status,
                      reason_code = excluded.reason_code,
                      details_json = excluded.details_json,
                      resolved_at = excluded.resolved_at,
                      updated_at = excluded.updated_at
                    """,
                    (
                        case_id,
                        f"AUTO-{fixture_key}",
                        contract_id,
                        f"benchmark_{label}",
                        json.dumps({"as_of_date": case_date, "fixture_key": fixture_key}, sort_keys=True),
                        f"bench-case::{fixture_key}::{label}",
                        created_at,
                        now,
                        resolved_at,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO human_decisions(
                      human_decision_id, exception_case_id, decided_by_user_id, decision, reason,
                      decision_payload_json, idempotency_key, decided_at, created_at
                    )
                    VALUES(?, ?, NULL, 'APPROVE', 'benchmark decision', '{}', ?, ?, ?)
                    ON CONFLICT(human_decision_id) DO UPDATE SET
                      decision = excluded.decision,
                      reason = excluded.reason,
                      decided_at = excluded.decided_at
                    """,
                    (
                        decision_id,
                        case_id,
                        f"bench-decision::{fixture_key}::{label}",
                        resolved_at,
                        resolved_at,
                    ),
                )

            for idx in range(1, 3):
                outcome_id = f"DOUT-{canonical_json_sha256({'fixture_key': fixture_key, 'kind': 'transport-feedback', 'idx': idx})[:20]}"
                created_at = f"{(as_of - timedelta(days=2)).isoformat()}T11:{idx:02d}:00Z"
                conn.execute(
                    """
                    INSERT INTO decision_outcomes(
                      decision_outcome_id, exception_case_id, human_decision_id, outcome_label, outcome_json, created_at
                    )
                    VALUES(?, ?, ?, 'transport_suggestion_feedback', ?, ?)
                    ON CONFLICT(decision_outcome_id) DO UPDATE SET
                      created_at = excluded.created_at
                    """,
                    (
                        outcome_id,
                        case_current_id,
                        decision_current_id,
                        json.dumps({"accepted": True, "fixture_key": fixture_key}, sort_keys=True),
                        created_at,
                    ),
                )

            for idx, delivery_id in enumerate((delivery_paid_id, delivery_open_id), start=1):
                transport_snapshot_id = f"TSNAP-{canonical_json_sha256({'fixture_key': fixture_key, 'kind': 'transport-snapshot', 'idx': idx})[:20]}"
                created_at = f"{(as_of - timedelta(days=2)).isoformat()}T10:{20 + idx:02d}:00Z"
                conn.execute(
                    """
                    INSERT INTO delivery_transport_snapshot(
                      snapshot_id, delivery_id, planned_delivery_id, transport_partner_id, transport_truck_id, transport_driver_id,
                      partner_name, truck_no, driver_name, driver_phone, source_type, source_ref, confidence, reason_code, payload_json, created_at
                    )
                    VALUES(?, ?, ?, NULL, NULL, NULL, 'Benchmark Transport', ?, 'Idowu Atanda', '08052803019', 'history',
                           'benchmark', 0.96, 'auto_threshold_met', ?, ?)
                    ON CONFLICT(snapshot_id) DO UPDATE SET
                      confidence = excluded.confidence,
                      reason_code = excluded.reason_code,
                      created_at = excluded.created_at
                    """,
                    (
                        transport_snapshot_id,
                        delivery_id,
                        planned_ids[idx - 1],
                        f"T{delivery_id[-4:]}",
                        json.dumps({"fixture_key": fixture_key, "delivery_id": delivery_id}, sort_keys=True),
                        created_at,
                    ),
                )

            for idx in range(1, 11):
                evidence_id = f"EVD-{canonical_json_sha256({'fixture_key': fixture_key, 'kind': 'evidence', 'idx': idx})[:20]}"
                link_status = "AUTO_LINKED" if idx <= 9 else "AUTO_LINK_REJECTED"
                linked_at = f"{(as_of - timedelta(days=2)).isoformat()}T12:{idx:02d}:00Z"
                conn.execute(
                    """
                    INSERT INTO evidence_originals(
                      evidence_id, contract_id, delivery_id, sales_transaction_id, sales_line_id, file_name, doc_type,
                      link_status, link_confidence, link_reason_code, link_source, linked_at, source_path, stored_path,
                      sha256, captured_at, created_at, updated_at
                    )
                    VALUES(?, ?, ?, ?, ?, ?, 'WAYBILL', ?, 0.95, ?, 'benchmark', ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(evidence_id) DO UPDATE SET
                      link_status = excluded.link_status,
                      link_reason_code = excluded.link_reason_code,
                      linked_at = excluded.linked_at,
                      updated_at = excluded.updated_at
                    """,
                    (
                        evidence_id,
                        contract_id,
                        delivery_paid_id if idx % 2 else delivery_open_id,
                        sales_paid_id if idx % 2 else sales_open_id,
                        sales_line_paid_id if idx % 2 else sales_line_open_id,
                        f"bench-evidence-{idx}.pdf",
                        link_status,
                        "auto_linked" if link_status == "AUTO_LINKED" else "manual_link_rejected",
                        linked_at,
                        f"/tmp/bench/{fixture_key}/{idx}.pdf",
                        f"/tmp/bench-store/{fixture_key}/{idx}.pdf",
                        canonical_json_sha256({"fixture_key": fixture_key, "evidence": idx}),
                        linked_at,
                        linked_at,
                        now,
                    ),
                )

            for idx in range(1, 11):
                event_id = f"EVT-{canonical_json_sha256({'fixture_key': fixture_key, 'kind': 'settlement-event', 'idx': idx})[:20]}"
                event_type = "SETTLEMENT_SUGGESTION_ACCEPTED" if idx <= 8 else "SETTLEMENT_SUGGESTION_ROUTED_EXCEPTION"
                created_at = f"{(as_of - timedelta(days=1)).isoformat()}T14:{idx:02d}:00Z"
                conn.execute(
                    """
                    INSERT OR IGNORE INTO event_log(
                      event_id, entity_type, entity_id, event_type, as_of_date, payload_json, source, created_at
                    )
                    VALUES(?, 'CONTRACT', ?, ?, ?, ?, 'phase2-benchmark', ?)
                    """,
                    (
                        event_id,
                        contract_id,
                        event_type,
                        as_of_date,
                        json.dumps({"fixture_key": fixture_key, "idx": idx}, sort_keys=True),
                        created_at,
                    ),
                )

            fixture_counts = {
                "contracts": 1,
                "deliveries": 2,
                "planned_deliveries": 5,
                "sales_transactions": 2,
                "suggestion_events": 10,
                "decisions": len(parser_decisions) + len(confirm_decisions) + len(exception_cases),
            }
            fixture_metadata = {
                "fixture_key": fixture_key,
                "as_of_date": as_of_date,
                "benchmark_version": benchmark_version,
                "contract_ids": [contract_id],
                "delivery_ids": [delivery_paid_id, delivery_open_id],
                "sales_transaction_ids": [sales_paid_id, sales_open_id],
                "intake_run_id": intake_run_id,
                "case_ids": [case_previous_id, case_current_id],
                "pr8_target_common_case_pass": bool(benchmark_version == PR8_BENCHMARK_VERSION),
                "seed_reset_requested": bool(reset),
            }
            self.repo.upsert_benchmark_run(
                conn,
                benchmark_run_id=benchmark_run_id,
                as_of_date=as_of_date,
                benchmark_version=benchmark_version,
                lookback_window_days=int(lookback_window_days),
                fixture_counts=fixture_counts,
                fixture_metadata=fixture_metadata,
                seeded_at_utc=seeded_at_utc,
            )

        return {
            "ok": True,
            "reused": False,
            "as_of_date": as_of_date,
            "benchmark_version": benchmark_version,
            "benchmark_run_id": benchmark_run_id,
            "lookback_window_days": int(lookback_window_days),
            "seeded_at_utc": seeded_at_utc,
            "fixture_counts": fixture_counts,
            "fixture_metadata": fixture_metadata,
        }

    def run_phase2_benchmark(
        self,
        *,
        as_of_date: str,
        lookback_window_days: int,
        benchmark_version: str,
        out_dir: Path,
        waivers_path: Path | None = None,
    ) -> dict[str, Any]:
        seed_result = self.seed_phase2_benchmark(
            as_of_date=as_of_date,
            benchmark_version=benchmark_version,
            reset=False,
            lookback_window_days=lookback_window_days,
        )
        out_dir.mkdir(parents=True, exist_ok=True)
        seed_path = out_dir / f"phase2_benchmark_seed_{as_of_date}.json"
        seed_path.write_text(json.dumps(seed_result, indent=2, sort_keys=True), encoding="utf-8")
        report = self.phase2_gate_report(
            as_of_date=as_of_date,
            lookback_window_days=lookback_window_days,
            benchmark_version=benchmark_version,
            waivers_path=waivers_path,
            out_dir=out_dir,
        )
        return {
            "ok": True,
            "seed": seed_result,
            "seed_path": str(seed_path),
            "report_json_path": report["report_json_path"],
            "report_md_path": report["report_md_path"],
            "promotion_recommendation": report["promotion_recommendation"],
            "gate_report": report["gate_report"],
        }

    def phase2_gate_health_snapshot(
        self,
        *,
        as_of_date: str,
        lookback_window_days: int,
        benchmark_version: str,
        waivers_path: Path | None = None,
    ) -> dict[str, Any]:
        report = self.phase2_gate_report(
            as_of_date=as_of_date,
            lookback_window_days=lookback_window_days,
            benchmark_version=benchmark_version,
            waivers_path=waivers_path,
            out_dir=None,
            persist=False,
        )
        latest_row = self.repo.fetch_one(
            """
            SELECT report_json_path, report_md_path
            FROM benchmark_runs
            WHERE report_md_path IS NOT NULL AND report_md_path <> ''
            ORDER BY generated_at_utc DESC, updated_at DESC
            LIMIT 1
            """
        )
        latest_report_path = ""
        if latest_row:
            latest_report_path = str(latest_row.get("report_md_path") or latest_row.get("report_json_path") or "")
        return {
            "as_of_date": report["gate_report"]["inputs"]["as_of_date"],
            "lookback_window_days": report["gate_report"]["inputs"]["lookback_window_days"],
            "benchmark_version": report["gate_report"]["inputs"]["benchmark_version"],
            "generated_at_utc": report["gate_report"]["inputs"]["generated_at_utc"],
            "promotion_recommendation": report["promotion_recommendation"],
            "gates": report["gate_report"]["gates"],
            "waiver_validation": report["gate_report"]["waiver_validation"],
            "latest_report_path": latest_report_path,
        }

    def phase2_drift_health_snapshot(
        self,
        *,
        as_of_date: str,
        lookback_window_days: int,
        benchmark_version: str,
    ) -> dict[str, Any]:
        report = self.phase2_drift_report(
            as_of_date=as_of_date,
            lookback_window_days=lookback_window_days,
            benchmark_version=benchmark_version,
            out_dir=None,
            persist=False,
        )
        latest_row = self.repo.fetch_one(
            """
            SELECT payload_json
            FROM event_log
            WHERE event_type = 'PHASE2_DRIFT_REPORT_EXPORTED'
            ORDER BY created_at DESC
            LIMIT 1
            """
        )
        latest_report_path = ""
        if latest_row and latest_row.get("payload_json"):
            payload = json.loads(str(latest_row.get("payload_json") or "{}"))
            latest_report_path = str(payload.get("report_md_path") or payload.get("report_json_path") or "")
        return {
            "as_of_date": report["drift_report"]["inputs"]["as_of_date"],
            "lookback_window_days": report["drift_report"]["inputs"]["lookback_window_days"],
            "benchmark_version": report["drift_report"]["inputs"]["benchmark_version"],
            "generated_at_utc": report["drift_report"]["inputs"]["generated_at_utc"],
            "drift_state": report["drift_report"]["aggregate"]["drift_state"],
            "recommendation": report["drift_report"]["aggregate"]["recommendation"],
            "gates": report["drift_report"]["gates"],
            "latest_report_path": latest_report_path,
        }

    def phase2_drift_report(
        self,
        *,
        as_of_date: str,
        lookback_window_days: int,
        benchmark_version: str,
        out_dir: Path | None = None,
        benchmark_metrics_ref: Path | None = None,
        live_metrics_ref: Path | None = None,
        persist: bool = True,
    ) -> dict[str, Any]:
        if lookback_window_days <= 0:
            raise ValueError("lookback_window_days must be > 0")
        try:
            date.fromisoformat(as_of_date)
        except ValueError as error:
            raise ValueError("as_of_date must be YYYY-MM-DD") from error

        generated_at_utc = utc_now_iso_z()
        snapshot_out_dir = out_dir
        if snapshot_out_dir is None:
            snapshot_out_dir = self.config.state_dir / "tmp" / "phase2-drift" / f"{as_of_date}-{benchmark_version}"
        snapshot_out_dir.mkdir(parents=True, exist_ok=True)

        benchmark_gate_report_ref = ""
        if benchmark_metrics_ref is not None:
            benchmark_metrics = self._load_metrics_snapshot_from_ref(benchmark_metrics_ref)
            benchmark_metrics_source = str(benchmark_metrics_ref)
            benchmark_snapshot_path = str(benchmark_metrics_ref)
        else:
            self.seed_phase2_benchmark(
                as_of_date=as_of_date,
                benchmark_version=benchmark_version,
                reset=False,
                lookback_window_days=int(lookback_window_days),
            )
            benchmark_run = self.phase2_gate_report(
                as_of_date=as_of_date,
                lookback_window_days=int(lookback_window_days),
                benchmark_version=benchmark_version,
                out_dir=snapshot_out_dir,
                persist=False,
            )
            benchmark_gate_report = benchmark_run.get("gate_report") if isinstance(benchmark_run, dict) else {}
            if not isinstance(benchmark_gate_report, dict):
                benchmark_gate_report = {}
            benchmark_metrics = self._benchmark_metrics_from_gate_report(
                gate_report=benchmark_gate_report,
                as_of_date=as_of_date,
                lookback_window_days=int(lookback_window_days),
                benchmark_version=benchmark_version,
                generated_at_utc=generated_at_utc,
            )
            benchmark_metrics_source = "phase2-benchmark-pipeline"
            benchmark_gate_report_ref = str(benchmark_run.get("report_json_path") or "")
            benchmark_snapshot_file = snapshot_out_dir / f"phase2_benchmark_metrics_{as_of_date}.json"
            benchmark_snapshot_file.write_text(
                json.dumps(benchmark_metrics, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            benchmark_snapshot_path = str(benchmark_snapshot_file)

        if live_metrics_ref is not None:
            live_metrics = self._load_metrics_snapshot_from_ref(live_metrics_ref)
            live_metrics_source = str(live_metrics_ref)
            live_snapshot_path = str(live_metrics_ref)
        else:
            if persist:
                live_export = self.autonomy_metrics(
                    as_of_date=as_of_date,
                    out_dir=snapshot_out_dir,
                    lookback_window_days=int(lookback_window_days),
                    benchmark_version=benchmark_version,
                )
                live_metrics = live_export.get("metrics") if isinstance(live_export, dict) else {}
                if not isinstance(live_metrics, dict):
                    live_metrics = {}
                live_metrics_source = "autonomy-metrics"
                live_snapshot_path = str(live_export.get("metrics_path") or "")
            else:
                live_metrics = self.compute_metrics_snapshot(
                    as_of_date=as_of_date,
                    lookback_window_days=int(lookback_window_days),
                    benchmark_version=benchmark_version,
                )
                live_snapshot = snapshot_out_dir / f"autonomy_metrics_{as_of_date}.json"
                live_snapshot.write_text(json.dumps(live_metrics, indent=2, sort_keys=True), encoding="utf-8")
                live_metrics_source = "compute_metrics_snapshot"
                live_snapshot_path = str(live_snapshot)

        thresholds, threshold_source, threshold_config_version = self._resolve_drift_thresholds()
        thresholds_path = ""
        if out_dir is not None:
            threshold_file = snapshot_out_dir / f"phase2_drift_thresholds_{as_of_date}.json"
            threshold_payload = {
                "source": threshold_source,
                "config_version": threshold_config_version,
                "thresholds": thresholds,
                "generated_at_utc": generated_at_utc,
            }
            threshold_file.write_text(json.dumps(threshold_payload, indent=2, sort_keys=True), encoding="utf-8")
            thresholds_path = str(threshold_file)

        metric_specs = self._drift_metric_specs()
        gates: list[dict[str, Any]] = []
        for gate_name, metric_names in metric_specs.items():
            comparisons: list[dict[str, Any]] = []
            has_alert = False
            has_watch = False
            missing_benchmark = False
            missing_live = False

            for metric_name in metric_names:
                threshold = thresholds.get(gate_name, {}).get(metric_name, {})
                benchmark_value = benchmark_metrics.get(metric_name)
                live_value = live_metrics.get(metric_name)
                comparison = self._evaluate_drift_metric(
                    metric_name=metric_name,
                    benchmark_value=benchmark_value,
                    live_value=live_value,
                    threshold=threshold if isinstance(threshold, dict) else {},
                )
                comparisons.append(comparison)
                if comparison["benchmark_value"] is None:
                    missing_benchmark = True
                if comparison["live_value"] is None:
                    missing_live = True
                state = str(comparison.get("comparison_state") or "")
                if state == "alert":
                    has_alert = True
                elif state == "watch":
                    has_watch = True

            version_mismatch = (
                str(benchmark_metrics.get("benchmark_version") or "").strip() != benchmark_version
                or str(live_metrics.get("benchmark_version") or "").strip() != benchmark_version
            )
            candidates: list[str] = []
            if version_mismatch:
                candidates.append("benchmark_version_mismatch")
            if missing_benchmark:
                candidates.append("insufficient_benchmark_data")
            if missing_live:
                candidates.append("insufficient_live_data")
            if has_alert:
                candidates.append("drift_exceeds_threshold")
            elif has_watch:
                candidates.append("drift_within_watch_band")
            else:
                candidates.append("pass")
            reason_code = self._resolve_drift_reason_code(candidates)
            drift_state = self._reason_to_drift_state(reason_code)
            gates.append(
                {
                    "gate_name": gate_name,
                    "drift_state": drift_state,
                    "reason_code": reason_code,
                    "comparisons": comparisons,
                }
            )

        aggregate_state = self._aggregate_drift_state(gates)
        recommendation = self._aggregate_drift_recommendation(aggregate_state)
        blocking_reasons = [
            f"{gate.get('gate_name')}:{gate.get('reason_code')}"
            for gate in gates
            if str(gate.get("drift_state") or "").upper() in {"MISMATCH", "INSUFFICIENT_DATA", "ALERT"}
        ]
        drift_report = {
            "inputs": {
                "as_of_date": as_of_date,
                "lookback_window_days": int(lookback_window_days),
                "benchmark_version": benchmark_version,
                "generated_at_utc": generated_at_utc,
                "benchmark_metrics_ref": benchmark_metrics_source,
                "live_metrics_ref": live_metrics_source,
            },
            "gates": gates,
            "aggregate": {
                "drift_state": aggregate_state,
                "recommendation": recommendation,
                "blocking_reasons": blocking_reasons,
            },
        }

        report_json_path = ""
        report_md_path = ""
        if out_dir is not None:
            report_json = snapshot_out_dir / f"phase2_drift_report_{as_of_date}.json"
            report_md = snapshot_out_dir / f"phase2_drift_report_{as_of_date}.md"
            report_json.write_text(json.dumps(drift_report, indent=2, sort_keys=True), encoding="utf-8")
            report_md.write_text(self._phase2_drift_markdown(drift_report), encoding="utf-8")
            report_json_path = str(report_json)
            report_md_path = str(report_md)

        if persist:
            with self.repo.transaction() as conn:
                self.repo.append_event(
                    conn,
                    entity_type="METRICS",
                    entity_id=f"PHASE2_DRIFT_REPORT::{as_of_date}::{benchmark_version}",
                    event_type="PHASE2_DRIFT_REPORT_EXPORTED",
                    as_of_date=as_of_date,
                    payload={
                        "as_of_date": as_of_date,
                        "benchmark_version": benchmark_version,
                        "report_json_path": report_json_path,
                        "report_md_path": report_md_path,
                        "benchmark_metrics_path": benchmark_snapshot_path,
                        "benchmark_gate_report_ref": benchmark_gate_report_ref,
                        "live_metrics_path": live_snapshot_path,
                        "thresholds_path": thresholds_path,
                        "threshold_source": threshold_source,
                        "threshold_config_version": threshold_config_version,
                        "drift_state": aggregate_state,
                        "recommendation": recommendation,
                        "lookback_window_days": int(lookback_window_days),
                    },
                    source="phase2-drift-report",
                )
                report_idempotency_key = canonical_json_sha256(
                    {
                        "as_of_date": as_of_date,
                        "lookback_window_days": int(lookback_window_days),
                        "benchmark_version": benchmark_version,
                        "benchmark_metrics_ref": str(benchmark_metrics_ref) if benchmark_metrics_ref else "",
                        "live_metrics_ref": str(live_metrics_ref) if live_metrics_ref else "",
                    }
                )
                existing_report = self.repo.find_idempotent_response(
                    conn,
                    command_name="phase2-drift-report",
                    idempotency_key=report_idempotency_key,
                )
                if not existing_report:
                    self.repo.save_idempotent_response(
                        conn,
                        command_name="phase2-drift-report",
                        idempotency_key=report_idempotency_key,
                        response={
                            "ok": True,
                            "drift_state": aggregate_state,
                            "recommendation": recommendation,
                            "report_json_path": report_json_path,
                            "report_md_path": report_md_path,
                            "drift_report": drift_report,
                        },
                    )

        return {
            "ok": True,
            "drift_state": aggregate_state,
            "recommendation": recommendation,
            "report_json_path": report_json_path,
            "report_md_path": report_md_path,
            "benchmark_metrics_path": benchmark_snapshot_path,
            "benchmark_gate_report_ref": benchmark_gate_report_ref,
            "live_metrics_path": live_snapshot_path,
            "thresholds_path": thresholds_path,
            "threshold_source": threshold_source,
            "threshold_config_version": threshold_config_version,
            "drift_report": drift_report,
        }

    def phase2_drift_triage(
        self,
        *,
        as_of_date: str,
        lookback_window_days: int,
        benchmark_version: str,
        out_dir: Path,
        benchmark_metrics_ref: Path | None = None,
        live_metrics_ref: Path | None = None,
    ) -> dict[str, Any]:
        report_result = self.phase2_drift_report(
            as_of_date=as_of_date,
            lookback_window_days=lookback_window_days,
            benchmark_version=benchmark_version,
            out_dir=out_dir,
            benchmark_metrics_ref=benchmark_metrics_ref,
            live_metrics_ref=live_metrics_ref,
            persist=True,
        )
        drift_report = report_result["drift_report"]
        gates = drift_report.get("gates") if isinstance(drift_report.get("gates"), list) else []
        aggregate = drift_report.get("aggregate") if isinstance(drift_report.get("aggregate"), dict) else {}
        generated_at_utc = str(drift_report.get("inputs", {}).get("generated_at_utc") or utc_now_iso_z())

        opened_count = 0
        updated_count = 0
        resolved_count = 0

        with self.repo.transaction() as conn:
            for gate in gates:
                if not isinstance(gate, dict):
                    continue
                gate_name = str(gate.get("gate_name") or "").strip().lower()
                if gate_name not in {"pr8", "pr9", "pr10"}:
                    continue
                drift_state = str(gate.get("drift_state") or "").upper()
                reason_code = str(gate.get("reason_code") or "pass").strip()
                comparisons = gate.get("comparisons") if isinstance(gate.get("comparisons"), list) else []
                open_for_gate = conn.execute(
                    """
                    SELECT *
                    FROM exception_cases
                    WHERE case_type = 'DRIFT_MONITORING'
                      AND status = 'OPEN'
                      AND json_extract(details_json, '$.benchmark_version') = ?
                      AND lower(json_extract(details_json, '$.gate_name')) = ?
                    ORDER BY created_at ASC
                    """,
                    (benchmark_version, gate_name),
                ).fetchall()
                open_rows = [dict(row) for row in open_for_gate]

                if drift_state == "PASS":
                    for row in open_rows:
                        details = self._safe_json_object(row.get("details_json"))
                        details["resolution_reason_code"] = "drift_cleared"
                        details["resolved_by_as_of_date"] = as_of_date
                        details["resolved_at_utc"] = generated_at_utc
                        conn.execute(
                            """
                            UPDATE exception_cases
                            SET status = 'RESOLVED',
                                details_json = ?,
                                updated_at = ?,
                                resolved_at = ?
                            WHERE exception_case_id = ?
                            """,
                            (
                                json.dumps(details, sort_keys=True),
                                generated_at_utc,
                                generated_at_utc,
                                str(row["exception_case_id"]),
                            ),
                        )
                        self.repo.append_event(
                            conn,
                            entity_type="EXCEPTION_CASE",
                            entity_id=str(row["exception_case_id"]),
                            event_type="PHASE2_DRIFT_CASE_RESOLVED",
                            as_of_date=as_of_date,
                            payload={
                                "gate_name": gate_name,
                                "benchmark_version": benchmark_version,
                                "reason_code": "drift_cleared",
                                "drift_state": "PASS",
                            },
                            source="phase2-drift-triage",
                        )
                        resolved_count += 1
                    continue

                severity = self._drift_case_severity(drift_state)
                drift_case_key = (
                    f"drift_case::{as_of_date}::{int(lookback_window_days)}::"
                    f"{benchmark_version}::{gate_name}::{reason_code}"
                )
                case_details = {
                    "as_of_date": as_of_date,
                    "lookback_window_days": int(lookback_window_days),
                    "benchmark_version": benchmark_version,
                    "generated_at_utc": generated_at_utc,
                    "gate_name": gate_name,
                    "drift_state": drift_state,
                    "reason_code": reason_code,
                    "comparisons": comparisons,
                    "report_json_path": str(report_result.get("report_json_path") or ""),
                    "report_md_path": str(report_result.get("report_md_path") or ""),
                    "blocking_reasons": aggregate.get("blocking_reasons", []),
                }

                existing = self.repo.get_exception_case_by_idempotency(idempotency_key=drift_case_key)
                target_case_id = ""
                if existing:
                    target_case_id = str(existing["exception_case_id"])
                    previous_details = self._safe_json_object(existing.get("details_json"))
                    if str(previous_details.get("generated_at_utc") or "").strip():
                        case_details["generated_at_utc"] = str(previous_details.get("generated_at_utc") or "")
                    previous_details_canonical = json.dumps(previous_details, sort_keys=True)
                    next_details_canonical = json.dumps(case_details, sort_keys=True)
                    current_status = str(existing.get("status") or "").upper()
                    current_severity = str(existing.get("severity") or "").upper()
                    current_reason = str(existing.get("reason_code") or "")
                    needs_update = (
                        current_status != "OPEN"
                        or current_severity != severity
                        or current_reason != reason_code
                        or previous_details_canonical != next_details_canonical
                    )
                    if needs_update:
                        conn.execute(
                            """
                            UPDATE exception_cases
                            SET status = 'OPEN',
                                severity = ?,
                                reason_code = ?,
                                details_json = ?,
                                updated_at = ?,
                                resolved_at = NULL
                            WHERE exception_case_id = ?
                            """,
                            (
                                severity,
                                reason_code,
                                next_details_canonical,
                                generated_at_utc,
                                target_case_id,
                            ),
                        )
                        self.repo.append_event(
                            conn,
                            entity_type="EXCEPTION_CASE",
                            entity_id=target_case_id,
                            event_type="PHASE2_DRIFT_CASE_UPDATED",
                            as_of_date=as_of_date,
                            payload={
                                "gate_name": gate_name,
                                "benchmark_version": benchmark_version,
                                "reason_code": reason_code,
                                "drift_state": drift_state,
                            },
                            source="phase2-drift-triage",
                        )
                        updated_count += 1
                else:
                    case_row = self.repo.create_or_get_exception_case(
                        conn,
                        autonomy_run_id="",
                        action_intent_id=None,
                        contract_id=None,
                        delivery_id=None,
                        planned_delivery_id=None,
                        case_type="DRIFT_MONITORING",
                        severity=severity,
                        reason_code=reason_code,
                        details=case_details,
                        idempotency_key=drift_case_key,
                    )
                    target_case_id = str(case_row["exception_case_id"])
                    self.repo.append_event(
                        conn,
                        entity_type="EXCEPTION_CASE",
                        entity_id=target_case_id,
                        event_type="PHASE2_DRIFT_CASE_OPENED",
                        as_of_date=as_of_date,
                        payload={
                            "gate_name": gate_name,
                            "benchmark_version": benchmark_version,
                            "reason_code": reason_code,
                            "drift_state": drift_state,
                        },
                        source="phase2-drift-triage",
                    )
                    opened_count += 1

                for row in open_rows:
                    case_id = str(row.get("exception_case_id") or "")
                    if not case_id or case_id == target_case_id:
                        continue
                    details = self._safe_json_object(row.get("details_json"))
                    details["resolution_reason_code"] = "drift_reason_replaced"
                    details["resolved_by_as_of_date"] = as_of_date
                    details["resolved_at_utc"] = generated_at_utc
                    conn.execute(
                        """
                        UPDATE exception_cases
                        SET status = 'RESOLVED',
                            details_json = ?,
                            updated_at = ?,
                            resolved_at = ?
                        WHERE exception_case_id = ?
                        """,
                        (
                            json.dumps(details, sort_keys=True),
                            generated_at_utc,
                            generated_at_utc,
                            case_id,
                        ),
                    )
                    self.repo.append_event(
                        conn,
                        entity_type="EXCEPTION_CASE",
                        entity_id=case_id,
                        event_type="PHASE2_DRIFT_CASE_RESOLVED",
                        as_of_date=as_of_date,
                        payload={
                            "gate_name": gate_name,
                            "benchmark_version": benchmark_version,
                            "reason_code": "drift_reason_replaced",
                            "drift_state": drift_state,
                        },
                        source="phase2-drift-triage",
                    )
                    resolved_count += 1

            severity_rows = conn.execute(
                """
                SELECT severity, COUNT(*) AS cnt
                FROM exception_cases
                WHERE case_type = 'DRIFT_MONITORING'
                  AND status = 'OPEN'
                  AND json_extract(details_json, '$.as_of_date') = ?
                  AND json_extract(details_json, '$.lookback_window_days') = ?
                  AND json_extract(details_json, '$.benchmark_version') = ?
                GROUP BY severity
                """,
                (as_of_date, int(lookback_window_days), benchmark_version),
            ).fetchall()
            open_cases_by_severity = {
                str(row["severity"] or "").upper(): int(row["cnt"] or 0)
                for row in severity_rows
            }
            self.repo.append_event(
                conn,
                entity_type="METRICS",
                entity_id=f"PHASE2_DRIFT_TRIAGE::{as_of_date}::{benchmark_version}",
                event_type="PHASE2_DRIFT_TRIAGE_COMPLETED",
                as_of_date=as_of_date,
                payload={
                    "as_of_date": as_of_date,
                    "lookback_window_days": int(lookback_window_days),
                    "benchmark_version": benchmark_version,
                    "generated_at_utc": generated_at_utc,
                    "cases_opened": int(opened_count),
                    "cases_updated": int(updated_count),
                    "cases_resolved": int(resolved_count),
                    "drift_state": str(report_result.get("drift_state") or ""),
                    "recommendation": str(report_result.get("recommendation") or ""),
                    "report_json_path": str(report_result.get("report_json_path") or ""),
                    "report_md_path": str(report_result.get("report_md_path") or ""),
                    "open_cases_by_severity": open_cases_by_severity,
                },
                source="phase2-drift-triage",
            )

        snapshot = self.phase2_drift_operations_snapshot(
            as_of_date=as_of_date,
            lookback_window_days=lookback_window_days,
            benchmark_version=benchmark_version,
        )
        return {
            "ok": True,
            "as_of_date": as_of_date,
            "lookback_window_days": int(lookback_window_days),
            "benchmark_version": benchmark_version,
            "drift_state": str(report_result.get("drift_state") or ""),
            "recommendation": str(report_result.get("recommendation") or ""),
            "report_json_path": str(report_result.get("report_json_path") or ""),
            "report_md_path": str(report_result.get("report_md_path") or ""),
            "cases_opened": int(opened_count),
            "cases_updated": int(updated_count),
            "cases_resolved": int(resolved_count),
            "open_cases_by_severity": snapshot.get("open_cases_by_severity", {}),
        }

    def phase2_drift_operations_snapshot(
        self,
        *,
        as_of_date: str,
        lookback_window_days: int,
        benchmark_version: str,
    ) -> dict[str, Any]:
        if lookback_window_days <= 0:
            raise ValueError("lookback_window_days must be > 0")
        try:
            date.fromisoformat(as_of_date)
        except ValueError as error:
            raise ValueError("as_of_date must be YYYY-MM-DD") from error
        open_rows = self.repo.fetch_all(
            """
            SELECT *
            FROM exception_cases
            WHERE case_type = 'DRIFT_MONITORING'
              AND status = 'OPEN'
              AND json_extract(details_json, '$.as_of_date') = ?
              AND json_extract(details_json, '$.lookback_window_days') = ?
              AND json_extract(details_json, '$.benchmark_version') = ?
            ORDER BY created_at ASC
            """,
            (as_of_date, int(lookback_window_days), benchmark_version),
        )
        open_cases_by_severity: dict[str, int] = {}
        open_cases_by_gate: dict[str, int] = {}
        open_cases_by_reason: dict[str, int] = {}
        open_cases: list[dict[str, Any]] = []
        for row in open_rows:
            details = self._safe_json_object(row.get("details_json"))
            severity = str(row.get("severity") or "").upper()
            gate_name = str(details.get("gate_name") or "")
            reason_code = str(row.get("reason_code") or "")
            open_cases_by_severity[severity] = int(open_cases_by_severity.get(severity, 0)) + 1
            if gate_name:
                open_cases_by_gate[gate_name] = int(open_cases_by_gate.get(gate_name, 0)) + 1
            if reason_code:
                open_cases_by_reason[reason_code] = int(open_cases_by_reason.get(reason_code, 0)) + 1
            open_cases.append(
                {
                    "exception_case_id": str(row.get("exception_case_id") or ""),
                    "severity": severity,
                    "reason_code": reason_code,
                    "case_type": str(row.get("case_type") or ""),
                    "created_at": str(row.get("created_at") or ""),
                    "gate_name": gate_name,
                    "drift_state": str(details.get("drift_state") or ""),
                }
            )

        latest_triage_row = self.repo.fetch_one(
            """
            SELECT created_at, payload_json
            FROM event_log
            WHERE event_type = 'PHASE2_DRIFT_TRIAGE_COMPLETED'
              AND as_of_date = ?
              AND json_extract(payload_json, '$.benchmark_version') = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (as_of_date, benchmark_version),
        )
        latest_report_row = self.repo.fetch_one(
            """
            SELECT payload_json
            FROM event_log
            WHERE event_type = 'PHASE2_DRIFT_REPORT_EXPORTED'
              AND as_of_date = ?
              AND json_extract(payload_json, '$.benchmark_version') = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (as_of_date, benchmark_version),
        )
        latest_triage_payload = self._safe_json_object((latest_triage_row or {}).get("payload_json"))
        latest_report_payload = self._safe_json_object((latest_report_row or {}).get("payload_json"))
        latest_report_json_path = str(
            latest_triage_payload.get("report_json_path")
            or latest_report_payload.get("report_json_path")
            or ""
        )
        latest_report_md_path = str(
            latest_triage_payload.get("report_md_path")
            or latest_report_payload.get("report_md_path")
            or ""
        )
        drift_state = str(latest_triage_payload.get("drift_state") or "")
        recommendation = str(latest_triage_payload.get("recommendation") or "")
        return {
            "as_of_date": as_of_date,
            "lookback_window_days": int(lookback_window_days),
            "benchmark_version": benchmark_version,
            "open_cases_total": int(len(open_cases)),
            "open_cases_by_severity": open_cases_by_severity,
            "open_cases_by_gate": open_cases_by_gate,
            "open_cases_by_reason": open_cases_by_reason,
            "open_cases": open_cases,
            "latest_triage_at_utc": str((latest_triage_row or {}).get("created_at") or ""),
            "drift_state": drift_state,
            "recommendation": recommendation,
            "latest_report_json_path": latest_report_json_path,
            "latest_report_md_path": latest_report_md_path,
        }

    def phase2_drift_root_cause(
        self,
        *,
        as_of_date: str,
        lookback_window_days: int,
        benchmark_version: str,
        out_dir: Path,
        drift_report_ref: Path | None = None,
        triage_status_ref: Path | None = None,
        persist: bool = True,
    ) -> dict[str, Any]:
        if lookback_window_days <= 0:
            raise ValueError("lookback_window_days must be > 0")
        try:
            as_of = date.fromisoformat(as_of_date)
        except ValueError as error:
            raise ValueError("as_of_date must be YYYY-MM-DD") from error
        generated_at_utc = utc_now_iso_z()
        out_dir.mkdir(parents=True, exist_ok=True)
        lookback_start = as_of - timedelta(days=int(lookback_window_days) - 1)
        lookback_start_iso = lookback_start.isoformat()

        drift_report_source = ""
        if drift_report_ref is not None:
            drift_payload = self._safe_json_object(json.loads(drift_report_ref.read_text(encoding="utf-8")))
            drift_report_source = str(drift_report_ref)
        else:
            drift_result = self.phase2_drift_report(
                as_of_date=as_of_date,
                lookback_window_days=int(lookback_window_days),
                benchmark_version=benchmark_version,
                out_dir=out_dir,
                persist=False,
            )
            drift_payload = self._safe_json_object(drift_result.get("drift_report"))
            drift_report_source = str(drift_result.get("report_json_path") or "")

        triage_status_source = ""
        if triage_status_ref is not None:
            triage_snapshot = self._safe_json_object(json.loads(triage_status_ref.read_text(encoding="utf-8")))
            triage_status_source = str(triage_status_ref)
        else:
            triage_snapshot = self.phase2_drift_operations_snapshot(
                as_of_date=as_of_date,
                lookback_window_days=int(lookback_window_days),
                benchmark_version=benchmark_version,
            )
            triage_status_path = out_dir / f"phase2_drift_status_{as_of_date}.json"
            triage_status_path.write_text(json.dumps(triage_snapshot, indent=2, sort_keys=True), encoding="utf-8")
            triage_status_source = str(triage_status_path)

        metrics = self.compute_metrics_snapshot(
            as_of_date=as_of_date,
            lookback_window_days=int(lookback_window_days),
            benchmark_version=benchmark_version,
        )
        gates = drift_payload.get("gates") if isinstance(drift_payload.get("gates"), list) else []
        drift_inputs = drift_payload.get("inputs") if isinstance(drift_payload.get("inputs"), dict) else {}
        aggregate_drift = drift_payload.get("aggregate") if isinstance(drift_payload.get("aggregate"), dict) else {}
        required_inputs = {"as_of_date", "lookback_window_days", "benchmark_version", "generated_at_utc"}
        missing_required_inputs = any(
            str(drift_inputs.get(field) or "").strip() == ""
            for field in required_inputs
        )
        has_gate_rows = any(isinstance(item, dict) for item in gates)
        insufficient_observability = bool(missing_required_inputs or not has_gate_rows)
        benchmark_misalignment = bool(
            str(drift_inputs.get("benchmark_version") or "").strip() != benchmark_version
            or str(metrics.get("benchmark_version") or "").strip() != benchmark_version
            or str(aggregate_drift.get("drift_state") or "").upper() == "MISMATCH"
        )

        drift_cases = self.repo.fetch_all(
            """
            SELECT exception_case_id, contract_id, reason_code, details_json, created_at, resolved_at
            FROM exception_cases
            WHERE case_type = 'DRIFT_MONITORING'
              AND COALESCE(NULLIF(json_extract(details_json, '$.as_of_date'), ''), substr(created_at, 1, 10)) BETWEEN ? AND ?
            ORDER BY created_at ASC, exception_case_id ASC
            """,
            (lookback_start_iso, as_of_date),
        )

        group_stats: dict[tuple[str, str], dict[str, Any]] = {}
        for row in drift_cases:
            details = self._safe_json_object(row.get("details_json"))
            if str(details.get("benchmark_version") or "").strip() != benchmark_version:
                continue
            gate_name = str(details.get("gate_name") or "").strip().lower()
            if gate_name not in {"pr8", "pr9", "pr10"}:
                continue
            derived_code = self._derive_root_cause_from_case_details(gate_name=gate_name, details=details)
            if derived_code not in ROOT_CAUSE_ALLOWED_CODES:
                continue
            key = (gate_name, derived_code)
            stats = group_stats.setdefault(
                key,
                {
                    "occurrence_count": 0,
                    "dates": set(),
                    "contract_ids": set(),
                    "case_ids": [],
                    "resolution_durations": [],
                },
            )
            stats["occurrence_count"] = int(stats["occurrence_count"]) + 1
            as_of_marker = str(details.get("as_of_date") or "")[:10] or str(row.get("created_at") or "")[:10]
            if as_of_marker:
                stats["dates"].add(as_of_marker)
            contract_id = str(row.get("contract_id") or "").strip()
            if contract_id:
                stats["contract_ids"].add(contract_id)
            case_id = str(row.get("exception_case_id") or "").strip()
            if case_id:
                stats["case_ids"].append(case_id)
            created_dt = self._parse_iso_dt(str(row.get("created_at") or ""))
            resolved_dt = self._parse_iso_dt(str(row.get("resolved_at") or ""))
            if created_dt and resolved_dt:
                duration_hours = max((resolved_dt - created_dt).total_seconds(), 0.0) / 3600.0
                stats["resolution_durations"].append(duration_hours)

        diagnosed_gate_rows: list[dict[str, Any]] = []
        causes_for_aggregate: list[str] = []
        for gate in gates:
            if not isinstance(gate, dict):
                continue
            gate_name = str(gate.get("gate_name") or "").strip().lower()
            if gate_name not in {"pr8", "pr9", "pr10"}:
                continue
            drift_state = str(gate.get("drift_state") or "").upper()
            drift_reason_code = str(gate.get("reason_code") or "pass")
            comparisons = gate.get("comparisons") if isinstance(gate.get("comparisons"), list) else []
            cause_code = self._diagnose_root_cause_for_gate(
                gate_name=gate_name,
                drift_state=drift_state,
                drift_reason_code=drift_reason_code,
                comparisons=comparisons,
                metrics=metrics,
                benchmark_misalignment=benchmark_misalignment,
                insufficient_observability=insufficient_observability,
            )
            causes_for_aggregate.append(cause_code)
            occurrence_stats = group_stats.get((gate_name, cause_code), {})
            occurrence_count = int(occurrence_stats.get("occurrence_count") or 0)
            date_count = len(occurrence_stats.get("dates", set()))
            recurring = bool(occurrence_count >= 3 and date_count >= 2)
            contract_ids = sorted(str(item) for item in occurrence_stats.get("contract_ids", set()) if str(item))
            case_ids = [str(item) for item in occurrence_stats.get("case_ids", []) if str(item)]
            manual_decision_count = 0
            if case_ids:
                placeholders = ",".join("?" for _ in case_ids)
                manual_row = self.repo.fetch_one(
                    f"""
                    SELECT COUNT(*) AS cnt
                    FROM human_decisions
                    WHERE exception_case_id IN ({placeholders})
                    """,
                    tuple(case_ids),
                ) or {"cnt": 0}
                manual_decision_count = int(manual_row.get("cnt") or 0)
            failed_or_blocked_actions = 0
            if contract_ids:
                placeholders = ",".join("?" for _ in contract_ids)
                failed_row = self.repo.fetch_one(
                    f"""
                    SELECT COUNT(*) AS cnt
                    FROM action_intents
                    WHERE contract_id IN ({placeholders})
                      AND as_of_date BETWEEN ? AND ?
                      AND status IN ('FAILED', 'BLOCKED')
                    """,
                    tuple(contract_ids) + (lookback_start_iso, as_of_date),
                ) or {"cnt": 0}
                failed_or_blocked_actions = int(failed_row.get("cnt") or 0)
            duration_values = [float(value) for value in occurrence_stats.get("resolution_durations", []) if value is not None]
            avg_resolution_time_hours = round(sum(duration_values) / len(duration_values), 4) if duration_values else 0.0
            diagnosed_gate_rows.append(
                {
                    "gate_name": gate_name,
                    "drift_state": drift_state,
                    "drift_reason_code": drift_reason_code,
                    "diagnosed_causes": [
                        {
                            "root_cause_code": cause_code,
                            "recurring": recurring,
                            "occurrence_count": occurrence_count,
                            "affected_contracts": len(contract_ids),
                            "root_cause_confidence": self._root_cause_confidence(
                                drift_state=drift_state,
                                drift_reason_code=drift_reason_code,
                                recurring=recurring,
                            ),
                            "impact_metrics": {
                                "exception_count": occurrence_count,
                                "manual_decision_count": manual_decision_count,
                                "failed_or_blocked_actions": failed_or_blocked_actions,
                                "avg_resolution_time_hours": avg_resolution_time_hours,
                            },
                            "evidence_refs": [drift_report_source, triage_status_source] + case_ids[:5],
                        }
                    ],
                }
            )

        top_recurring_causes = self._rank_root_cause_summary(diagnosed_gate_rows)
        aggregate_reason_code = self._resolve_root_cause_aggregate_reason(
            diagnosed_codes=causes_for_aggregate,
            insufficient_observability=insufficient_observability,
            benchmark_misalignment=benchmark_misalignment,
        )
        aggregate_state = self._root_cause_aggregate_state(
            aggregate_reason_code=aggregate_reason_code,
            diagnosed_gate_rows=diagnosed_gate_rows,
        )
        recommended_manual_actions = self._root_cause_actions_for_reasons(
            aggregate_reason_code=aggregate_reason_code,
            top_recurring_causes=top_recurring_causes,
        )

        report_payload = {
            "inputs": {
                "as_of_date": as_of_date,
                "lookback_window_days": int(lookback_window_days),
                "benchmark_version": benchmark_version,
                "generated_at_utc": generated_at_utc,
                "drift_report_ref": drift_report_source,
                "triage_status_ref": triage_status_source,
            },
            "gates": diagnosed_gate_rows,
            "aggregate": {
                "state": aggregate_state,
                "reason_code": aggregate_reason_code,
                "top_recurring_causes": top_recurring_causes,
                "recommended_manual_actions": recommended_manual_actions,
            },
        }

        report_json_path = out_dir / f"phase2_drift_root_cause_{as_of_date}.json"
        report_md_path = out_dir / f"phase2_drift_root_cause_{as_of_date}.md"
        report_json_path.write_text(json.dumps(report_payload, indent=2, sort_keys=True), encoding="utf-8")
        report_md_path.write_text(self._phase2_drift_root_cause_markdown(report_payload), encoding="utf-8")

        if persist:
            with self.repo.transaction() as conn:
                self.repo.append_event(
                    conn,
                    entity_type="METRICS",
                    entity_id=f"PHASE2_DRIFT_ROOT_CAUSE::{as_of_date}::{benchmark_version}",
                    event_type="PHASE2_DRIFT_ROOT_CAUSE_EXPORTED",
                    as_of_date=as_of_date,
                    payload={
                        "as_of_date": as_of_date,
                        "lookback_window_days": int(lookback_window_days),
                        "benchmark_version": benchmark_version,
                        "generated_at_utc": generated_at_utc,
                        "aggregate_state": aggregate_state,
                        "aggregate_reason_code": aggregate_reason_code,
                        "top_recurring_causes": top_recurring_causes,
                        "report_json_path": str(report_json_path),
                        "report_md_path": str(report_md_path),
                    },
                    source="phase2-drift-root-cause",
                )

        return {
            "ok": True,
            "aggregate_state": aggregate_state,
            "aggregate_reason_code": aggregate_reason_code,
            "top_recurring_causes": top_recurring_causes,
            "report_json_path": str(report_json_path),
            "report_md_path": str(report_md_path),
            "root_cause_report": report_payload,
        }

    def phase2_drift_root_cause_snapshot(
        self,
        *,
        as_of_date: str,
        lookback_window_days: int,
        benchmark_version: str,
    ) -> dict[str, Any]:
        latest_row = self.repo.fetch_one(
            """
            SELECT payload_json, created_at
            FROM event_log
            WHERE event_type = 'PHASE2_DRIFT_ROOT_CAUSE_EXPORTED'
              AND as_of_date = ?
              AND json_extract(payload_json, '$.benchmark_version') = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (as_of_date, benchmark_version),
        )
        payload = self._safe_json_object((latest_row or {}).get("payload_json"))
        top_causes = payload.get("top_recurring_causes") if isinstance(payload.get("top_recurring_causes"), list) else []
        return {
            "as_of_date": as_of_date,
            "lookback_window_days": int(lookback_window_days),
            "benchmark_version": benchmark_version,
            "latest_generated_at_utc": str(payload.get("generated_at_utc") or ""),
            "aggregate_state": str(payload.get("aggregate_state") or "PASS"),
            "aggregate_reason_code": str(payload.get("aggregate_reason_code") or "no_recurring_root_cause"),
            "top_recurring_causes": top_causes[:3],
            "latest_report_json_path": str(payload.get("report_json_path") or ""),
            "latest_report_md_path": str(payload.get("report_md_path") or ""),
        }

    def phase2_operator_playbooks(
        self,
        *,
        as_of_date: str,
        lookback_window_days: int,
        benchmark_version: str,
        out_dir: Path,
        drift_report_ref: Path | None = None,
        triage_status_ref: Path | None = None,
        root_cause_report_ref: Path | None = None,
        persist: bool = True,
    ) -> dict[str, Any]:
        if lookback_window_days <= 0:
            raise ValueError("lookback_window_days must be > 0")
        try:
            date.fromisoformat(as_of_date)
        except ValueError as error:
            raise ValueError("as_of_date must be YYYY-MM-DD") from error
        out_dir.mkdir(parents=True, exist_ok=True)
        generated_at_utc = utc_now_iso_z()

        if drift_report_ref is not None:
            drift_payload_raw = json.loads(drift_report_ref.read_text(encoding="utf-8"))
            drift_payload = self._safe_json_object(drift_payload_raw)
            drift_report_source = str(drift_report_ref)
        else:
            drift_result = self.phase2_drift_report(
                as_of_date=as_of_date,
                lookback_window_days=int(lookback_window_days),
                benchmark_version=benchmark_version,
                out_dir=out_dir,
                persist=False,
            )
            drift_payload = self._safe_json_object(drift_result.get("drift_report"))
            drift_report_source = str(drift_result.get("report_json_path") or "")

        if triage_status_ref is not None:
            triage_snapshot_raw = json.loads(triage_status_ref.read_text(encoding="utf-8"))
            triage_snapshot = self._safe_json_object(triage_snapshot_raw)
            triage_status_source = str(triage_status_ref)
        else:
            triage_snapshot = self.phase2_drift_operations_snapshot(
                as_of_date=as_of_date,
                lookback_window_days=int(lookback_window_days),
                benchmark_version=benchmark_version,
            )
            triage_status_path = out_dir / f"phase2_drift_status_{as_of_date}.json"
            triage_status_path.write_text(json.dumps(triage_snapshot, indent=2, sort_keys=True), encoding="utf-8")
            triage_status_source = str(triage_status_path)

        if root_cause_report_ref is not None:
            root_cause_payload_raw = json.loads(root_cause_report_ref.read_text(encoding="utf-8"))
            root_cause_payload = self._safe_json_object(root_cause_payload_raw)
            root_cause_source = str(root_cause_report_ref)
        else:
            drift_ref_for_rc = Path(drift_report_source) if drift_report_source else None
            triage_ref_for_rc = Path(triage_status_source) if triage_status_source else None
            root_cause_result = self.phase2_drift_root_cause(
                as_of_date=as_of_date,
                lookback_window_days=int(lookback_window_days),
                benchmark_version=benchmark_version,
                out_dir=out_dir,
                drift_report_ref=drift_ref_for_rc,
                triage_status_ref=triage_ref_for_rc,
                persist=False,
            )
            root_cause_payload = self._safe_json_object(root_cause_result.get("root_cause_report"))
            root_cause_source = str(root_cause_result.get("report_json_path") or "")

        root_aggregate = root_cause_payload.get("aggregate") if isinstance(root_cause_payload.get("aggregate"), dict) else {}
        aggregate_state = str(root_aggregate.get("state") or "INSUFFICIENT_DATA")
        aggregate_reason_code = str(root_aggregate.get("reason_code") or "insufficient_observability_data")
        top_root_causes = root_aggregate.get("top_recurring_causes") if isinstance(root_aggregate.get("top_recurring_causes"), list) else []
        if not top_root_causes:
            top_root_causes = [
                {
                    "root_cause_code": "insufficient_observability_data",
                    "occurrence_count": 0,
                    "affected_contracts": 0,
                    "recurring": False,
                    "gates": [],
                }
            ]

        drift_inputs = drift_payload.get("inputs") if isinstance(drift_payload.get("inputs"), dict) else {}
        source_refs = {
            "drift_report": drift_report_source,
            "triage_status": triage_status_source,
            "root_cause_report": root_cause_source,
        }
        triage_latest_report_ref = str(triage_snapshot.get("latest_report_json_path") or "").strip()
        missing_source_refs = (
            not all(str(source_refs.get(key) or "").strip() for key in ("drift_report", "triage_status", "root_cause_report"))
            or not triage_latest_report_ref
        )
        if missing_source_refs:
            aggregate_state = "INSUFFICIENT_DATA"
            aggregate_reason_code = "insufficient_observability_data"
            top_root_causes = [
                {
                    "root_cause_code": "insufficient_observability_data",
                    "occurrence_count": 0,
                    "affected_contracts": 0,
                    "recurring": False,
                    "gates": [],
                }
            ]
        candidates_by_playbook: dict[str, dict[str, Any]] = {}
        for item in top_root_causes:
            if not isinstance(item, dict):
                continue
            root_cause_code = str(item.get("root_cause_code") or "").strip()
            if root_cause_code not in ROOT_CAUSE_PLAYBOOK_MAP:
                root_cause_code = "insufficient_observability_data"
            mapped = ROOT_CAUSE_PLAYBOOK_MAP[root_cause_code]
            playbook_codes = [mapped[0]]
            if mapped[1]:
                playbook_codes.append(str(mapped[1]))
            for playbook_code in playbook_codes:
                if playbook_code not in PLAYBOOK_ALLOWED_CODES:
                    continue
                affected_contracts = int(item.get("affected_contracts") or 0)
                recurring = bool(item.get("recurring"))
                urgency = self._playbook_urgency(
                    aggregate_state=aggregate_state,
                    root_cause_code=root_cause_code,
                    recurring=recurring,
                    affected_contracts=affected_contracts,
                )
                evidence_checklist, missing_evidence, evidence_refs = self._playbook_evidence_bundle(
                    playbook_code=playbook_code,
                    root_cause_code=root_cause_code,
                    source_refs=source_refs,
                    drift_inputs=drift_inputs,
                    root_cause_entry=item,
                )
                evidence_completeness = "COMPLETE" if not missing_evidence else "PARTIAL"
                score = (
                    self._playbook_urgency_weight(urgency)
                    + (2 if recurring else 0)
                    + min(affected_contracts, 3)
                    + (-1 if evidence_completeness == "PARTIAL" else 0)
                )
                candidate = {
                    "playbook_code": playbook_code,
                    "urgency": urgency,
                    "score": int(score),
                    "root_cause_code": root_cause_code,
                    "affected_contracts": affected_contracts,
                    "recurring": recurring,
                    "occurrence_count": int(item.get("occurrence_count") or 0),
                    "evidence_completeness": evidence_completeness,
                    "evidence_checklist": evidence_checklist,
                    "missing_evidence": missing_evidence,
                    "evidence_refs": evidence_refs,
                }
                existing = candidates_by_playbook.get(playbook_code)
                if existing is None:
                    candidates_by_playbook[playbook_code] = candidate
                else:
                    existing_key = (
                        int(existing.get("score") or 0),
                        self._playbook_urgency_weight(str(existing.get("urgency") or "")),
                        int(existing.get("affected_contracts") or 0),
                        str(existing.get("playbook_code") or ""),
                    )
                    candidate_key = (
                        int(candidate.get("score") or 0),
                        self._playbook_urgency_weight(str(candidate.get("urgency") or "")),
                        int(candidate.get("affected_contracts") or 0),
                        str(candidate.get("playbook_code") or ""),
                    )
                    if candidate_key > existing_key:
                        candidates_by_playbook[playbook_code] = candidate

        ranked_candidates = sorted(
            candidates_by_playbook.values(),
            key=lambda row: (
                -int(row.get("score") or 0),
                -self._playbook_urgency_weight(str(row.get("urgency") or "")),
                -int(row.get("affected_contracts") or 0),
                str(row.get("playbook_code") or ""),
            ),
        )
        if missing_source_refs:
            observability_index = next(
                (
                    index
                    for index, row in enumerate(ranked_candidates)
                    if str(row.get("playbook_code") or "") == "PB_OBSERVABILITY_RECOVERY"
                ),
                -1,
            )
            if observability_index > 0:
                ranked_candidates.insert(0, ranked_candidates.pop(observability_index))
        selected_playbooks = ranked_candidates[:3]
        aggregate = {
            "state": aggregate_state,
            "reason_code": aggregate_reason_code,
            "selection_count": len(selected_playbooks),
        }
        report_payload = {
            "inputs": {
                "as_of_date": as_of_date,
                "lookback_window_days": int(lookback_window_days),
                "benchmark_version": benchmark_version,
                "generated_at_utc": generated_at_utc,
                "drift_report_ref": drift_report_source,
                "triage_status_ref": triage_status_source,
                "root_cause_report_ref": root_cause_source,
            },
            "aggregate": aggregate,
            "playbooks": selected_playbooks,
            "source_refs": source_refs,
        }

        report_json_path = out_dir / f"phase2_operator_playbooks_{as_of_date}.json"
        report_md_path = out_dir / f"phase2_operator_playbooks_{as_of_date}.md"
        report_json_path.write_text(json.dumps(report_payload, indent=2, sort_keys=True), encoding="utf-8")
        report_md_path.write_text(self._phase2_operator_playbooks_markdown(report_payload), encoding="utf-8")

        if persist:
            with self.repo.transaction() as conn:
                self.repo.append_event(
                    conn,
                    entity_type="METRICS",
                    entity_id=f"PHASE2_OPERATOR_PLAYBOOKS::{as_of_date}::{benchmark_version}",
                    event_type="PHASE2_OPERATOR_PLAYBOOKS_EXPORTED",
                    as_of_date=as_of_date,
                    payload={
                        "as_of_date": as_of_date,
                        "lookback_window_days": int(lookback_window_days),
                        "benchmark_version": benchmark_version,
                        "generated_at_utc": generated_at_utc,
                        "aggregate_state": aggregate_state,
                        "aggregate_reason_code": aggregate_reason_code,
                        "selection_count": len(selected_playbooks),
                        "selected_playbooks": selected_playbooks,
                        "report_json_path": str(report_json_path),
                        "report_md_path": str(report_md_path),
                    },
                    source="phase2-operator-playbooks",
                )

        return {
            "ok": True,
            "aggregate_state": aggregate_state,
            "aggregate_reason_code": aggregate_reason_code,
            "selected_playbooks": selected_playbooks,
            "report_json_path": str(report_json_path),
            "report_md_path": str(report_md_path),
            "operator_playbooks_report": report_payload,
        }

    def phase2_operator_playbooks_snapshot(
        self,
        *,
        as_of_date: str,
        lookback_window_days: int,
        benchmark_version: str,
    ) -> dict[str, Any]:
        latest_row = self.repo.fetch_one(
            """
            SELECT payload_json
            FROM event_log
            WHERE event_type = 'PHASE2_OPERATOR_PLAYBOOKS_EXPORTED'
              AND as_of_date = ?
              AND json_extract(payload_json, '$.benchmark_version') = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (as_of_date, benchmark_version),
        )
        payload = self._safe_json_object((latest_row or {}).get("payload_json"))
        selected = payload.get("selected_playbooks") if isinstance(payload.get("selected_playbooks"), list) else []
        return {
            "as_of_date": as_of_date,
            "lookback_window_days": int(lookback_window_days),
            "benchmark_version": benchmark_version,
            "aggregate_state": str(payload.get("aggregate_state") or "PASS"),
            "aggregate_reason_code": str(payload.get("aggregate_reason_code") or "no_recurring_root_cause"),
            "selection_count": int(payload.get("selection_count") or len(selected)),
            "selected_playbooks": selected[:3],
            "latest_generated_at_utc": str(payload.get("generated_at_utc") or ""),
            "latest_report_json_path": str(payload.get("report_json_path") or ""),
            "latest_report_md_path": str(payload.get("report_md_path") or ""),
        }

    def phase2_gate_report(
        self,
        *,
        as_of_date: str,
        lookback_window_days: int,
        benchmark_version: str,
        waivers_path: Path | None = None,
        out_dir: Path | None = None,
        persist: bool = True,
    ) -> dict[str, Any]:
        if lookback_window_days <= 0:
            raise ValueError("lookback_window_days must be > 0")
        try:
            date.fromisoformat(as_of_date)
        except ValueError as error:
            raise ValueError("as_of_date must be YYYY-MM-DD") from error
        generated_at_utc = utc_now_iso_z()
        run_row = self.repo.get_benchmark_run(as_of_date=as_of_date, benchmark_version=benchmark_version)
        available_versions = self.repo.fetch_all(
            "SELECT benchmark_version FROM benchmark_runs WHERE as_of_date = ? ORDER BY benchmark_version ASC",
            (as_of_date,),
        )
        available_version_values = [str(row.get("benchmark_version") or "") for row in available_versions]
        benchmark_match = run_row is not None
        if run_row is None and available_version_values:
            benchmark_guard_reason = "benchmark_version_mismatch"
        elif run_row is None:
            benchmark_guard_reason = "benchmark_not_seeded"
        else:
            benchmark_guard_reason = "pass"

        pr8_metrics = self.compute_metrics_snapshot(
            as_of_date=as_of_date,
            lookback_window_days=lookback_window_days,
            benchmark_version=PR8_BENCHMARK_VERSION,
        )
        pr9_metrics = self.compute_metrics_snapshot(
            as_of_date=as_of_date,
            lookback_window_days=lookback_window_days,
            benchmark_version=PR9_BENCHMARK_VERSION,
        )
        pr10_metrics = self.compute_metrics_snapshot(
            as_of_date=as_of_date,
            lookback_window_days=lookback_window_days,
            benchmark_version=PR10_BENCHMARK_VERSION,
        )

        gates: list[dict[str, Any]] = [
            {
                "gate_name": "pr8",
                "pass": bool(pr8_metrics.get("pr8_gate_pass")),
                "reason_code": str(pr8_metrics.get("pr8_gate_reason_code") or ""),
                "metrics": {
                    "median_manual_fields_per_intake": pr8_metrics.get("median_manual_fields_per_intake"),
                    "autoplan_zero_edit_common_case_rate": pr8_metrics.get("autoplan_zero_edit_common_case_rate"),
                    "intake_decision_distribution": pr8_metrics.get("intake_decision_distribution"),
                },
            },
            {
                "gate_name": "pr9",
                "pass": bool(pr9_metrics.get("pr9_gate_pass")),
                "reason_code": str(pr9_metrics.get("pr9_gate_reason_code") or ""),
                "metrics": {
                    "manual_transport_fields_per_delivery": pr9_metrics.get("manual_transport_fields_per_delivery"),
                    "doc_autolink_precision": pr9_metrics.get("doc_autolink_precision"),
                    "deliveries_with_transport_assignment": pr9_metrics.get("deliveries_with_transport_assignment"),
                },
            },
            {
                "gate_name": "pr10",
                "pass": bool(pr10_metrics.get("pr10_gate_pass")),
                "reason_code": str(pr10_metrics.get("pr10_gate_reason_code") or ""),
                "metrics": {
                    "payment_suggestion_acceptance_rate": pr10_metrics.get("payment_suggestion_acceptance_rate"),
                    "exception_resolution_trend_state": pr10_metrics.get("exception_resolution_trend_state"),
                    "touchless_rate": pr10_metrics.get("touchless_rate"),
                    "auto_action_success_rate": pr10_metrics.get("auto_action_success_rate"),
                },
            },
        ]
        if not benchmark_match:
            for gate in gates:
                gate["pass"] = False
                gate["reason_code"] = benchmark_guard_reason

        waiver_file = waivers_path or (self.config.state_dir / "release-readiness" / "phase2_gate_waivers.json")
        waiver_validation = self._validate_phase2_gate_waivers(
            waivers_path=waiver_file,
            generated_at_utc=generated_at_utc,
        )
        valid_waivers_by_gate: dict[str, list[dict[str, Any]]] = waiver_validation["valid_waivers_by_gate"]
        invalid_waivers_by_gate: dict[str, list[dict[str, Any]]] = waiver_validation["invalid_waivers_by_gate"]

        failed_gates: list[dict[str, Any]] = [gate for gate in gates if not bool(gate.get("pass"))]
        blocking_reasons: list[str] = []
        waiver_refs: list[str] = []
        all_failed_waived = True
        for gate in failed_gates:
            gate_name = str(gate.get("gate_name") or "").lower()
            valid_for_gate = valid_waivers_by_gate.get(gate_name, [])
            invalid_for_gate = invalid_waivers_by_gate.get(gate_name, [])
            if valid_for_gate:
                gate["waiver_state"] = "active"
                gate["waiver_refs"] = [str(item.get("waiver_id") or "") for item in valid_for_gate if item.get("waiver_id")]
                waiver_refs.extend(gate["waiver_refs"])
            elif invalid_for_gate:
                gate["waiver_state"] = "invalid"
                gate["waiver_refs"] = []
                all_failed_waived = False
                blocking_reasons.append(f"{gate_name}:{gate.get('reason_code')}:invalid_waiver")
            else:
                gate["waiver_state"] = "none"
                gate["waiver_refs"] = []
                all_failed_waived = False
                blocking_reasons.append(f"{gate_name}:{gate.get('reason_code')}")
        for gate in gates:
            gate.setdefault("waiver_state", "none")
            gate.setdefault("waiver_refs", [])

        if not failed_gates:
            promotion_recommendation = "PASS"
        elif all_failed_waived:
            promotion_recommendation = "WAIVED"
        else:
            promotion_recommendation = "FAIL"

        gate_report = {
            "inputs": {
                "as_of_date": as_of_date,
                "lookback_window_days": int(lookback_window_days),
                "benchmark_version": benchmark_version,
                "benchmark_version_match": bool(benchmark_match),
                "benchmark_guard_reason_code": benchmark_guard_reason,
                "available_benchmark_versions": available_version_values,
                "generated_at_utc": generated_at_utc,
            },
            "gates": gates,
            "aggregate": {
                "promotion_recommendation": promotion_recommendation,
                "blocking_reasons": blocking_reasons,
                "waiver_refs": sorted({item for item in waiver_refs if item}),
            },
            "waiver_validation": waiver_validation["summary"],
        }
        report_json_path = ""
        report_md_path = ""
        if out_dir is not None:
            out_dir.mkdir(parents=True, exist_ok=True)
            report_json = out_dir / f"phase2_gate_report_{as_of_date}.json"
            report_md = out_dir / f"phase2_gate_report_{as_of_date}.md"
            report_json.write_text(json.dumps(gate_report, indent=2, sort_keys=True), encoding="utf-8")
            report_md.write_text(self._phase2_gate_markdown(gate_report), encoding="utf-8")
            report_json_path = str(report_json)
            report_md_path = str(report_md)

        fixture_counts = json.loads(run_row.get("fixture_counts_json") or "{}") if run_row else {}
        fixture_metadata = json.loads(run_row.get("fixture_metadata_json") or "{}") if run_row else {}
        if persist:
            with self.repo.transaction() as conn:
                benchmark_run_id = str(run_row.get("benchmark_run_id") or new_ulid()) if run_row else new_ulid()
                self.repo.upsert_benchmark_run(
                    conn,
                    benchmark_run_id=benchmark_run_id,
                    as_of_date=as_of_date,
                    benchmark_version=benchmark_version,
                    lookback_window_days=int(lookback_window_days if lookback_window_days > 0 else 30),
                    fixture_counts=fixture_counts,
                    fixture_metadata=fixture_metadata,
                    seeded_at_utc=str(run_row.get("seeded_at_utc") or generated_at_utc) if run_row else generated_at_utc,
                    generated_at_utc=generated_at_utc,
                    report_json_path=report_json_path or None,
                    report_md_path=report_md_path or None,
                )
                self.repo.append_event(
                    conn,
                    entity_type="METRICS",
                    entity_id=f"PHASE2_GATE_REPORT::{as_of_date}::{benchmark_version}",
                    event_type="PHASE2_GATE_REPORT_EXPORTED",
                    as_of_date=as_of_date,
                    payload={
                        "promotion_recommendation": promotion_recommendation,
                        "report_json_path": report_json_path,
                        "report_md_path": report_md_path,
                        "lookback_window_days": int(lookback_window_days),
                    },
                    source="phase2-gate-report",
                )
                report_idempotency_key = canonical_json_sha256(
                    {
                        "as_of_date": as_of_date,
                        "lookback_window_days": int(lookback_window_days),
                        "benchmark_version": benchmark_version,
                    }
                )
                existing_report = self.repo.find_idempotent_response(
                    conn,
                    command_name="phase2-gate-report",
                    idempotency_key=report_idempotency_key,
                )
                if not existing_report:
                    self.repo.save_idempotent_response(
                        conn,
                        command_name="phase2-gate-report",
                        idempotency_key=report_idempotency_key,
                        response={
                            "ok": True,
                            "promotion_recommendation": promotion_recommendation,
                            "report_json_path": report_json_path,
                            "report_md_path": report_md_path,
                            "gate_report": gate_report,
                        },
                    )

        return {
            "ok": True,
            "promotion_recommendation": promotion_recommendation,
            "report_json_path": report_json_path,
            "report_md_path": report_md_path,
            "gate_report": gate_report,
        }

    def _phase2_gate_markdown(self, report: dict[str, Any]) -> str:
        inputs = report.get("inputs") if isinstance(report.get("inputs"), dict) else {}
        gates = report.get("gates") if isinstance(report.get("gates"), list) else []
        aggregate = report.get("aggregate") if isinstance(report.get("aggregate"), dict) else {}
        lines = [
            "# Phase 2 Gate Report",
            "",
            "## Inputs",
            f"- as_of_date: {inputs.get('as_of_date')}",
            f"- lookback_window_days: {inputs.get('lookback_window_days')}",
            f"- benchmark_version: {inputs.get('benchmark_version')}",
            f"- benchmark_version_match: {inputs.get('benchmark_version_match')}",
            f"- benchmark_guard_reason_code: {inputs.get('benchmark_guard_reason_code')}",
            f"- generated_at_utc: {inputs.get('generated_at_utc')}",
            "",
            "## Gate Snapshots",
            "| Gate | Pass | Reason | Waiver State |",
            "|---|---:|---|---|",
        ]
        for gate in gates:
            lines.append(
                f"| {gate.get('gate_name')} | {gate.get('pass')} | {gate.get('reason_code')} | {gate.get('waiver_state')} |"
            )
        lines.extend(
            [
                "",
                "## Aggregate Recommendation",
                f"- promotion_recommendation: {aggregate.get('promotion_recommendation')}",
                f"- blocking_reasons: {json.dumps(aggregate.get('blocking_reasons', []), sort_keys=True)}",
                f"- waiver_refs: {json.dumps(aggregate.get('waiver_refs', []), sort_keys=True)}",
                "",
                "## Raw JSON",
                "```json",
                json.dumps(report, indent=2, sort_keys=True),
                "```",
                "",
            ]
        )
        return "\n".join(lines)

    def _phase2_drift_markdown(self, report: dict[str, Any]) -> str:
        inputs = report.get("inputs") if isinstance(report.get("inputs"), dict) else {}
        gates = report.get("gates") if isinstance(report.get("gates"), list) else []
        aggregate = report.get("aggregate") if isinstance(report.get("aggregate"), dict) else {}
        lines = [
            "# Phase 2 Drift Report",
            "",
            "## Inputs",
            f"- as_of_date: {inputs.get('as_of_date')}",
            f"- lookback_window_days: {inputs.get('lookback_window_days')}",
            f"- benchmark_version: {inputs.get('benchmark_version')}",
            f"- generated_at_utc: {inputs.get('generated_at_utc')}",
            f"- benchmark_metrics_ref: {inputs.get('benchmark_metrics_ref')}",
            f"- live_metrics_ref: {inputs.get('live_metrics_ref')}",
            "",
            "## Per-Gate Drift",
        ]
        for gate in gates:
            lines.extend(
                [
                    f"### {gate.get('gate_name')}",
                    f"- drift_state: {gate.get('drift_state')}",
                    f"- reason_code: {gate.get('reason_code')}",
                    "",
                    "| Metric | Benchmark | Live | Delta | Threshold | Within Threshold |",
                    "|---|---:|---:|---:|---|---:|",
                ]
            )
            comparisons = gate.get("comparisons") if isinstance(gate.get("comparisons"), list) else []
            for row in comparisons:
                threshold = row.get("threshold") if isinstance(row.get("threshold"), dict) else {}
                lines.append(
                    "| {metric} | {benchmark} | {live} | {delta} | {threshold} | {within} |".format(
                        metric=row.get("metric_name"),
                        benchmark=row.get("benchmark_value"),
                        live=row.get("live_value"),
                        delta=row.get("delta"),
                        threshold=json.dumps(threshold, sort_keys=True),
                        within=row.get("within_threshold"),
                    )
                )
            if not comparisons:
                lines.append("| _none_ | | | | | |")
            lines.append("")
        lines.extend(
            [
                "## Aggregate",
                f"- drift_state: {aggregate.get('drift_state')}",
                f"- recommendation: {aggregate.get('recommendation')}",
                f"- blocking_reasons: {json.dumps(aggregate.get('blocking_reasons', []), sort_keys=True)}",
                "",
                "## Raw JSON",
                "```json",
                json.dumps(report, indent=2, sort_keys=True),
                "```",
                "",
            ]
        )
        return "\n".join(lines)

    def _phase2_drift_root_cause_markdown(self, report: dict[str, Any]) -> str:
        inputs = report.get("inputs") if isinstance(report.get("inputs"), dict) else {}
        gates = report.get("gates") if isinstance(report.get("gates"), list) else []
        aggregate = report.get("aggregate") if isinstance(report.get("aggregate"), dict) else {}
        lines = [
            "# Phase 2 Drift Root Cause Report",
            "",
            "## Inputs",
            f"- as_of_date: {inputs.get('as_of_date')}",
            f"- lookback_window_days: {inputs.get('lookback_window_days')}",
            f"- benchmark_version: {inputs.get('benchmark_version')}",
            f"- generated_at_utc: {inputs.get('generated_at_utc')}",
            f"- drift_report_ref: {inputs.get('drift_report_ref')}",
            f"- triage_status_ref: {inputs.get('triage_status_ref')}",
            "",
            "## Aggregate",
            f"- state: {aggregate.get('state')}",
            f"- reason_code: {aggregate.get('reason_code')}",
            f"- top_recurring_causes: {json.dumps(aggregate.get('top_recurring_causes', []), sort_keys=True)}",
            "",
            "## Per-Gate Diagnosis",
        ]
        for gate in gates:
            lines.extend(
                [
                    f"### {gate.get('gate_name')}",
                    f"- drift_state: {gate.get('drift_state')}",
                    f"- drift_reason_code: {gate.get('drift_reason_code')}",
                    "| Root Cause | Recurring | Occurrence Count | Affected Contracts | Confidence | Impact Metrics |",
                    "|---|---:|---:|---:|---:|---|",
                ]
            )
            diagnosed = gate.get("diagnosed_causes") if isinstance(gate.get("diagnosed_causes"), list) else []
            for item in diagnosed:
                if not isinstance(item, dict):
                    continue
                lines.append(
                    "| {code} | {recurring} | {count} | {contracts} | {confidence} | {impact} |".format(
                        code=item.get("root_cause_code"),
                        recurring=item.get("recurring"),
                        count=item.get("occurrence_count"),
                        contracts=item.get("affected_contracts"),
                        confidence=item.get("root_cause_confidence"),
                        impact=json.dumps(item.get("impact_metrics") or {}, sort_keys=True),
                    )
                )
            if not diagnosed:
                lines.append("| _none_ | | | | | |")
            lines.append("")
        lines.extend(
            [
                "## Recommended Manual Actions",
                *[f"- {line}" for line in aggregate.get("recommended_manual_actions", []) if isinstance(line, str)],
                "",
                "## Raw JSON",
                "```json",
                json.dumps(report, indent=2, sort_keys=True),
                "```",
                "",
            ]
        )
        return "\n".join(lines)

    def _phase2_operator_playbooks_markdown(self, report: dict[str, Any]) -> str:
        inputs = report.get("inputs") if isinstance(report.get("inputs"), dict) else {}
        aggregate = report.get("aggregate") if isinstance(report.get("aggregate"), dict) else {}
        playbooks = report.get("playbooks") if isinstance(report.get("playbooks"), list) else []
        lines = [
            "# Phase 2 Operator Playbooks",
            "",
            "## Inputs",
            f"- as_of_date: {inputs.get('as_of_date')}",
            f"- lookback_window_days: {inputs.get('lookback_window_days')}",
            f"- benchmark_version: {inputs.get('benchmark_version')}",
            f"- generated_at_utc: {inputs.get('generated_at_utc')}",
            f"- drift_report_ref: {inputs.get('drift_report_ref')}",
            f"- triage_status_ref: {inputs.get('triage_status_ref')}",
            f"- root_cause_report_ref: {inputs.get('root_cause_report_ref')}",
            "",
            "## Aggregate",
            f"- state: {aggregate.get('state')}",
            f"- reason_code: {aggregate.get('reason_code')}",
            f"- selection_count: {aggregate.get('selection_count')}",
            "",
            "## Selected Playbooks",
            "| Playbook | Urgency | Score | Root Cause | Recurring | Affected Contracts | Evidence Completeness |",
            "|---|---|---:|---|---:|---:|---|",
        ]
        for row in playbooks:
            if not isinstance(row, dict):
                continue
            lines.append(
                "| {playbook} | {urgency} | {score} | {cause} | {recurring} | {contracts} | {evidence} |".format(
                    playbook=row.get("playbook_code"),
                    urgency=row.get("urgency"),
                    score=row.get("score"),
                    cause=row.get("root_cause_code"),
                    recurring=row.get("recurring"),
                    contracts=row.get("affected_contracts"),
                    evidence=row.get("evidence_completeness"),
                )
            )
        if not playbooks:
            lines.append("| _none_ | | | | | | |")
        lines.extend(
            [
                "",
                "## Raw JSON",
                "```json",
                json.dumps(report, indent=2, sort_keys=True),
                "```",
                "",
            ]
        )
        return "\n".join(lines)

    def _playbook_urgency(
        self,
        *,
        aggregate_state: str,
        root_cause_code: str,
        recurring: bool,
        affected_contracts: int,
    ) -> str:
        state = str(aggregate_state or "").upper()
        code = str(root_cause_code or "")
        if state == "ALERT" or code in {"insufficient_observability_data", "benchmark_dataset_misalignment"}:
            return "URGENT"
        if state == "WATCH":
            if recurring and int(affected_contracts) >= 2:
                return "HIGH"
            return "MEDIUM"
        if state == "PASS" and code == "no_recurring_root_cause":
            return "LOW"
        if recurring:
            return "HIGH"
        return "MEDIUM"

    def _playbook_urgency_weight(self, urgency: str) -> int:
        value = str(urgency or "").upper()
        if value == "URGENT":
            return 4
        if value == "HIGH":
            return 3
        if value == "MEDIUM":
            return 2
        return 1

    def _playbook_evidence_bundle(
        self,
        *,
        playbook_code: str,
        root_cause_code: str,
        source_refs: dict[str, str],
        drift_inputs: dict[str, Any],
        root_cause_entry: dict[str, Any],
    ) -> tuple[list[str], list[str], list[str]]:
        source_drift = str(source_refs.get("drift_report") or "").strip()
        source_triage = str(source_refs.get("triage_status") or "").strip()
        source_root = str(source_refs.get("root_cause_report") or "").strip()
        benchmark_ref = str(drift_inputs.get("benchmark_metrics_ref") or "").strip()
        live_ref = str(drift_inputs.get("live_metrics_ref") or "").strip()
        evidence_refs = [ref for ref in [source_drift, source_triage, source_root] if ref]
        entry_refs = root_cause_entry.get("evidence_refs") if isinstance(root_cause_entry.get("evidence_refs"), list) else []
        for ref in entry_refs:
            value = str(ref or "").strip()
            if value and value not in evidence_refs:
                evidence_refs.append(value)

        checklist_rules: dict[str, list[tuple[str, bool]]] = {
            "PB_OBSERVABILITY_RECOVERY": [
                ("latest drift report JSON path", bool(source_drift)),
                ("latest drift triage status snapshot", bool(source_triage)),
                ("latest autonomy metrics snapshot", bool(live_ref)),
                ("missing telemetry fields list", root_cause_code == "insufficient_observability_data"),
            ],
            "PB_BENCHMARK_ALIGNMENT": [
                ("benchmark run/version reference", bool(benchmark_ref)),
                ("drift report benchmark_version reference", bool(source_drift)),
                ("live metrics benchmark_version reference", bool(live_ref)),
                ("version mismatch evidence", root_cause_code == "benchmark_dataset_misalignment"),
            ],
            "PB_INTAKE_QUALITY_STABILIZATION": [
                ("intake decision distribution snapshot", bool(source_root)),
                ("correction memory evidence refs", bool(entry_refs)),
                ("affected buyer/product tuples", int(root_cause_entry.get("affected_contracts") or 0) > 0),
            ],
            "PB_PLANNING_POLICY_REVIEW": [
                ("lot split common-case outcomes", bool(source_root)),
                ("planning exception references", bool(entry_refs)),
                ("policy version/source refs", bool(source_drift)),
            ],
            "PB_TRANSPORT_ASSIGNMENT_RETRAIN": [
                ("transport confidence distribution refs", bool(source_root)),
                ("assignment feedback refs", bool(entry_refs)),
                ("compliance conflict refs", bool(source_triage)),
            ],
            "PB_DOCUMENT_LINKAGE_TUNING": [
                ("auto-link precision snapshot", bool(source_root)),
                ("ambiguous/blocked linkage case refs", bool(entry_refs)),
                ("fingerprint/source filename samples", bool(source_drift)),
            ],
            "PB_SETTLEMENT_MATCHING_REVIEW": [
                ("payment suggestion acceptance snapshot", bool(source_root)),
                ("ambiguous allocation case refs", bool(entry_refs)),
                ("payment reference mismatch refs", bool(source_drift)),
            ],
            "PB_MANUAL_OVERRIDE_REDUCTION": [
                ("override concentration ratios", bool(source_root)),
                ("top repeated override reasons", bool(entry_refs)),
                ("exception case references", bool(source_triage)),
            ],
            "PB_MONITOR_ONLY": [
                ("latest drift + root-cause report refs", bool(source_drift and source_root)),
            ],
        }
        rules = checklist_rules.get(playbook_code, [])
        evidence_checklist = [item for item, _ in rules]
        missing_evidence = [item for item, ok in rules if not ok]
        return evidence_checklist, missing_evidence, evidence_refs

    def _diagnose_root_cause_for_gate(
        self,
        *,
        gate_name: str,
        drift_state: str,
        drift_reason_code: str,
        comparisons: list[dict[str, Any]],
        metrics: dict[str, Any],
        benchmark_misalignment: bool,
        insufficient_observability: bool,
    ) -> str:
        reason = str(drift_reason_code or "pass")
        if insufficient_observability or reason in {"insufficient_benchmark_data", "insufficient_live_data"}:
            return "insufficient_observability_data"
        if benchmark_misalignment or reason == "benchmark_version_mismatch":
            return "benchmark_dataset_misalignment"
        if str(drift_state or "").upper() == "PASS" and reason == "pass":
            return "no_recurring_root_cause"

        manual_total = int(metrics.get("manual_interactions_total") or 0)
        manual_overrides = int(metrics.get("manual_interactions_user_overrides") or 0)
        override_ratio = (manual_overrides / manual_total) if manual_total > 0 else 0.0
        if gate_name == "pr8" and str(drift_state or "").upper() != "PASS" and manual_total >= 3 and override_ratio >= 0.70:
            return "manual_override_concentration"

        dominant_metric = ""
        dominant_score = -1.0
        for item in comparisons:
            if not isinstance(item, dict):
                continue
            metric_name = str(item.get("metric_name") or "")
            comparison_state = str(item.get("comparison_state") or "")
            if comparison_state == "alert":
                score = 2.0
            elif comparison_state == "watch":
                score = 1.0
            else:
                score = 0.0
            delta_value = item.get("delta")
            try:
                score += abs(float(delta_value or 0.0))
            except Exception:
                score += 0.0
            if score > dominant_score:
                dominant_score = score
                dominant_metric = metric_name

        if gate_name == "pr8":
            if dominant_metric == "autoplan_zero_edit_common_case_rate":
                return "planning_policy_mismatch"
            if dominant_metric == "median_manual_fields_per_intake":
                return "input_quality_regression"
            return "no_recurring_root_cause"
        if gate_name == "pr9":
            if dominant_metric == "manual_transport_fields_per_delivery":
                return "transport_assignment_instability"
            if dominant_metric == "doc_autolink_precision":
                return "document_linkage_instability"
            return "no_recurring_root_cause"
        if gate_name == "pr10":
            if dominant_metric in {"payment_suggestion_acceptance_rate", "auto_action_success_rate"}:
                return "settlement_matching_instability"
            return "no_recurring_root_cause"
        return "no_recurring_root_cause"

    def _derive_root_cause_from_case_details(self, *, gate_name: str, details: dict[str, Any]) -> str:
        reason_code = str(details.get("reason_code") or "")
        comparisons = details.get("comparisons") if isinstance(details.get("comparisons"), list) else []
        return self._diagnose_root_cause_for_gate(
            gate_name=gate_name,
            drift_state=str(details.get("drift_state") or ""),
            drift_reason_code=reason_code,
            comparisons=comparisons,
            metrics={},
            benchmark_misalignment=(reason_code == "benchmark_version_mismatch"),
            insufficient_observability=(reason_code in {"insufficient_benchmark_data", "insufficient_live_data"}),
        )

    def _root_cause_confidence(
        self,
        *,
        drift_state: str,
        drift_reason_code: str,
        recurring: bool,
    ) -> float:
        reason = str(drift_reason_code or "")
        state = str(drift_state or "").upper()
        if reason in {"benchmark_version_mismatch", "insufficient_benchmark_data", "insufficient_live_data"}:
            base = 0.99
        elif state == "ALERT":
            base = 0.90
        elif state == "WATCH":
            base = 0.82
        else:
            base = 0.70
        if recurring:
            base += 0.05
        return round(min(base, 1.0), 4)

    def _rank_root_cause_summary(self, diagnosed_gate_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        aggregated: dict[str, dict[str, Any]] = {}
        for gate_row in diagnosed_gate_rows:
            if not isinstance(gate_row, dict):
                continue
            gate_name = str(gate_row.get("gate_name") or "")
            state = str(gate_row.get("drift_state") or "").upper()
            diagnosed = gate_row.get("diagnosed_causes") if isinstance(gate_row.get("diagnosed_causes"), list) else []
            for item in diagnosed:
                if not isinstance(item, dict):
                    continue
                code = str(item.get("root_cause_code") or "")
                if code not in ROOT_CAUSE_ALLOWED_CODES:
                    continue
                bucket = aggregated.setdefault(
                    code,
                    {
                        "root_cause_code": code,
                        "occurrence_count": 0,
                        "affected_contracts": 0,
                        "recurring": False,
                        "gates": set(),
                        "max_severity_weight": 0,
                    },
                )
                bucket["occurrence_count"] = int(bucket["occurrence_count"]) + int(item.get("occurrence_count") or 0)
                bucket["affected_contracts"] = int(bucket["affected_contracts"]) + int(item.get("affected_contracts") or 0)
                bucket["recurring"] = bool(bucket["recurring"] or bool(item.get("recurring")))
                if gate_name:
                    bucket["gates"].add(gate_name)
                bucket["max_severity_weight"] = max(
                    int(bucket["max_severity_weight"]),
                    self._drift_state_weight(state),
                )
        ranked = sorted(
            aggregated.values(),
            key=lambda item: (
                -int(item.get("occurrence_count") or 0),
                -int(item.get("affected_contracts") or 0),
                -int(item.get("max_severity_weight") or 0),
                str(item.get("root_cause_code") or ""),
            ),
        )
        output: list[dict[str, Any]] = []
        for row in ranked:
            output.append(
                {
                    "root_cause_code": str(row.get("root_cause_code") or ""),
                    "occurrence_count": int(row.get("occurrence_count") or 0),
                    "affected_contracts": int(row.get("affected_contracts") or 0),
                    "recurring": bool(row.get("recurring")),
                    "gates": sorted(str(item) for item in row.get("gates", set()) if str(item)),
                }
            )
        return output[:3]

    def _resolve_root_cause_aggregate_reason(
        self,
        *,
        diagnosed_codes: list[str],
        insufficient_observability: bool,
        benchmark_misalignment: bool,
    ) -> str:
        candidates: list[str] = []
        if insufficient_observability:
            candidates.append("insufficient_observability_data")
        if benchmark_misalignment:
            candidates.append("benchmark_dataset_misalignment")
        for code in diagnosed_codes:
            if code in ROOT_CAUSE_ALLOWED_CODES:
                candidates.append(code)
        if not candidates:
            candidates.append("no_recurring_root_cause")
        for code in ROOT_CAUSE_REASON_PRECEDENCE:
            if code in candidates:
                return code
        return "no_recurring_root_cause"

    def _root_cause_aggregate_state(
        self,
        *,
        aggregate_reason_code: str,
        diagnosed_gate_rows: list[dict[str, Any]],
    ) -> str:
        if aggregate_reason_code == "no_recurring_root_cause":
            return "PASS"
        if aggregate_reason_code == "insufficient_observability_data":
            return "INSUFFICIENT_DATA"
        state_weights = [self._drift_state_weight(str(row.get("drift_state") or "")) for row in diagnosed_gate_rows]
        max_weight = max(state_weights) if state_weights else 0
        if max_weight >= self._drift_state_weight("ALERT"):
            return "ALERT"
        if max_weight >= self._drift_state_weight("WATCH"):
            return "WATCH"
        return "WATCH"

    def _root_cause_actions_for_reasons(
        self,
        *,
        aggregate_reason_code: str,
        top_recurring_causes: list[dict[str, Any]],
    ) -> list[str]:
        actions: list[str] = []
        if aggregate_reason_code == "insufficient_observability_data":
            actions.append("Capture missing telemetry snapshots for the lookback window before triage review.")
        if aggregate_reason_code == "benchmark_dataset_misalignment":
            actions.append("Align benchmark_version across benchmark run, drift report, and live metrics references.")
        if aggregate_reason_code == "manual_override_concentration":
            actions.append("Review repeated override reasons and tighten decision-card prompts for the affected gate.")
        cause_to_action = {
            "input_quality_regression": "Audit intake parsing quality and correction memory hit rate for affected buyers/products.",
            "planning_policy_mismatch": "Review lot policy/cadence assumptions against recent common-case delivery planning outcomes.",
            "transport_assignment_instability": "Review transport suggestion confidence and assignment history freshness for affected routes.",
            "document_linkage_instability": "Review document fingerprint hints and linkage confidence thresholds for recurring document types.",
            "settlement_matching_instability": "Review payment reference quality and ambiguous allocation patterns for affected counterparties.",
        }
        for row in top_recurring_causes:
            if not isinstance(row, dict):
                continue
            code = str(row.get("root_cause_code") or "")
            action = cause_to_action.get(code)
            if action and action not in actions:
                actions.append(action)
        if not actions:
            actions.append("No recurring root cause detected; continue monitoring drift trend windows.")
        return actions

    def _drift_metric_specs(self) -> dict[str, list[str]]:
        return {
            "pr8": [
                "median_manual_fields_per_intake",
                "autoplan_zero_edit_common_case_rate",
            ],
            "pr9": [
                "manual_transport_fields_per_delivery",
                "doc_autolink_precision",
            ],
            "pr10": [
                "payment_suggestion_acceptance_rate",
                "auto_action_success_rate",
            ],
        }

    def _resolve_drift_thresholds(self) -> tuple[dict[str, Any], str, str]:
        if isinstance(self.config.drift_thresholds, dict) and self.config.drift_thresholds:
            merged = json.loads(json.dumps(DEFAULT_DRIFT_THRESHOLDS))
            for gate_name in ("pr8", "pr9", "pr10"):
                gate_cfg = self.config.drift_thresholds.get(gate_name)
                if not isinstance(gate_cfg, dict):
                    continue
                for metric_name, metric_default in merged.get(gate_name, {}).items():
                    metric_cfg = gate_cfg.get(metric_name)
                    if not isinstance(metric_cfg, dict):
                        continue
                    merged_metric = dict(metric_default)
                    merged_metric.update(metric_cfg)
                    merged[gate_name][metric_name] = merged_metric
            version = str(self.config.drift_thresholds.get("version") or "configured")
            return merged, "config", version
        return json.loads(json.dumps(DEFAULT_DRIFT_THRESHOLDS)), "default", str(
            DEFAULT_DRIFT_THRESHOLDS.get("version") or "phase2.pr13.defaults.v1"
        )

    def _load_metrics_snapshot_from_ref(self, path: Path) -> dict[str, Any]:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"Metrics snapshot at {path} must be a JSON object")
        return payload

    def _benchmark_metrics_from_gate_report(
        self,
        *,
        gate_report: dict[str, Any],
        as_of_date: str,
        lookback_window_days: int,
        benchmark_version: str,
        generated_at_utc: str,
    ) -> dict[str, Any]:
        gates = gate_report.get("gates") if isinstance(gate_report.get("gates"), list) else []
        by_gate = {
            str(row.get("gate_name") or ""): row
            for row in gates
            if isinstance(row, dict) and str(row.get("gate_name") or "").strip()
        }
        pr8 = by_gate.get("pr8") if isinstance(by_gate.get("pr8"), dict) else {}
        pr9 = by_gate.get("pr9") if isinstance(by_gate.get("pr9"), dict) else {}
        pr10 = by_gate.get("pr10") if isinstance(by_gate.get("pr10"), dict) else {}
        pr8_metrics = pr8.get("metrics") if isinstance(pr8.get("metrics"), dict) else {}
        pr9_metrics = pr9.get("metrics") if isinstance(pr9.get("metrics"), dict) else {}
        pr10_metrics = pr10.get("metrics") if isinstance(pr10.get("metrics"), dict) else {}
        return {
            "as_of_date": as_of_date,
            "lookback_window_days": int(lookback_window_days),
            "benchmark_version": benchmark_version,
            "generated_at_utc": generated_at_utc,
            "median_manual_fields_per_intake": pr8_metrics.get("median_manual_fields_per_intake"),
            "autoplan_zero_edit_common_case_rate": pr8_metrics.get("autoplan_zero_edit_common_case_rate"),
            "manual_transport_fields_per_delivery": pr9_metrics.get("manual_transport_fields_per_delivery"),
            "doc_autolink_precision": pr9_metrics.get("doc_autolink_precision"),
            "payment_suggestion_acceptance_rate": pr10_metrics.get("payment_suggestion_acceptance_rate"),
            "auto_action_success_rate": pr10_metrics.get("auto_action_success_rate"),
        }

    def _evaluate_drift_metric(
        self,
        *,
        metric_name: str,
        benchmark_value: Any,
        live_value: Any,
        threshold: dict[str, Any],
    ) -> dict[str, Any]:
        delta_type = str(threshold.get("delta_type") or "absolute")
        direction = str(threshold.get("direction") or "increase").lower()
        watch = threshold.get("watch")
        alert = threshold.get("alert")
        benchmark_numeric: float | None = None
        live_numeric: float | None = None
        try:
            benchmark_numeric = float(benchmark_value) if benchmark_value is not None else None
        except (TypeError, ValueError):
            benchmark_numeric = None
        try:
            live_numeric = float(live_value) if live_value is not None else None
        except (TypeError, ValueError):
            live_numeric = None
        delta: float | None = None
        comparison_state = "insufficient"
        within_threshold: bool | None = None
        if benchmark_numeric is not None and live_numeric is not None:
            delta = round(live_numeric - benchmark_numeric, 6)
            watch_v = float(watch) if watch is not None else None
            alert_v = float(alert) if alert is not None else None
            alert_hit = False
            watch_hit = False
            if direction == "decrease":
                if alert_v is not None and delta < -alert_v:
                    alert_hit = True
                elif watch_v is not None and delta < -watch_v:
                    watch_hit = True
            else:
                if alert_v is not None and delta > alert_v:
                    alert_hit = True
                elif watch_v is not None and delta > watch_v:
                    watch_hit = True
            if alert_hit:
                comparison_state = "alert"
                within_threshold = False
            elif watch_hit:
                comparison_state = "watch"
                within_threshold = False
            else:
                comparison_state = "pass"
                within_threshold = True
        return {
            "metric_name": metric_name,
            "benchmark_value": benchmark_numeric,
            "live_value": live_numeric,
            "delta": delta,
            "delta_type": delta_type,
            "threshold": {
                "watch": watch,
                "alert": alert,
                "direction": direction,
            },
            "within_threshold": within_threshold,
            "comparison_state": comparison_state,
        }

    def _resolve_drift_reason_code(self, candidates: list[str]) -> str:
        normalized = [code for code in candidates if code in DRIFT_ALLOWED_REASON_CODES]
        for code in DRIFT_REASON_PRECEDENCE:
            if code in normalized:
                return code
        return "pass"

    def _reason_to_drift_state(self, reason_code: str) -> str:
        if reason_code == "benchmark_version_mismatch":
            return "MISMATCH"
        if reason_code in {"insufficient_benchmark_data", "insufficient_live_data"}:
            return "INSUFFICIENT_DATA"
        if reason_code == "drift_exceeds_threshold":
            return "ALERT"
        if reason_code == "drift_within_watch_band":
            return "WATCH"
        return "PASS"

    def _aggregate_drift_state(self, gates: list[dict[str, Any]]) -> str:
        states = [str(item.get("drift_state") or "") for item in gates]
        if any(state == "MISMATCH" for state in states):
            return "MISMATCH"
        if any(state == "INSUFFICIENT_DATA" for state in states):
            return "INSUFFICIENT_DATA"
        if any(state == "ALERT" for state in states):
            return "ALERT"
        if any(state == "WATCH" for state in states):
            return "WATCH"
        return "PASS"

    def _aggregate_drift_recommendation(self, drift_state: str) -> str:
        if drift_state == "PASS":
            return "NO_ACTION"
        if drift_state == "WATCH":
            return "INVESTIGATE"
        return "BLOCK_PROMOTION"

    def _validate_phase2_gate_waivers(
        self,
        *,
        waivers_path: Path,
        generated_at_utc: str,
    ) -> dict[str, Any]:
        valid_by_gate: dict[str, list[dict[str, Any]]] = {"pr8": [], "pr9": [], "pr10": []}
        invalid_by_gate: dict[str, list[dict[str, Any]]] = {"pr8": [], "pr9": [], "pr10": []}
        if not waivers_path.exists():
            return {
                "valid_waivers_by_gate": valid_by_gate,
                "invalid_waivers_by_gate": invalid_by_gate,
                "summary": {
                    "waivers_path": str(waivers_path),
                    "exists": False,
                    "valid_waiver_count": 0,
                    "invalid_waiver_count": 0,
                    "invalid_waiver_findings": [],
                },
            }
        raw = json.loads(waivers_path.read_text(encoding="utf-8") or "[]")
        waivers = raw.get("waivers") if isinstance(raw, dict) else raw
        waivers = waivers if isinstance(waivers, list) else []
        generated_dt = self._parse_iso_dt(generated_at_utc)
        invalid_findings: list[dict[str, Any]] = []
        for item in waivers:
            if not isinstance(item, dict):
                invalid_findings.append({"waiver_id": "", "gate_name": "", "reason_code": "waiver_not_object"})
                continue
            gate_name = str(item.get("gate_name") or "").strip().lower()
            waiver_id = str(item.get("waiver_id") or "").strip()
            record = {
                "waiver_id": waiver_id,
                "gate_name": gate_name,
                "reason": str(item.get("reason") or "").strip(),
                "owner_product": str(item.get("owner_product") or "").strip(),
                "owner_ops": str(item.get("owner_ops") or "").strip(),
                "owner_engineering": str(item.get("owner_engineering") or "").strip(),
                "created_at_utc": str(item.get("created_at_utc") or "").strip(),
                "expires_at_utc": str(item.get("expires_at_utc") or "").strip(),
                "fallback_plan": str(item.get("fallback_plan") or "").strip(),
                "active": bool(item.get("active")),
            }
            if gate_name not in {"pr8", "pr9", "pr10"}:
                invalid_findings.append({"waiver_id": waiver_id, "gate_name": gate_name, "reason_code": "invalid_gate_name"})
                continue
            missing_fields = [
                field
                for field in ("waiver_id", "reason", "owner_product", "owner_ops", "owner_engineering", "created_at_utc", "expires_at_utc", "fallback_plan")
                if not str(record.get(field) or "").strip()
            ]
            expires_dt = self._parse_iso_dt(record["expires_at_utc"])
            created_dt = self._parse_iso_dt(record["created_at_utc"])
            if missing_fields:
                invalid_by_gate[gate_name].append(record)
                invalid_findings.append(
                    {
                        "waiver_id": waiver_id,
                        "gate_name": gate_name,
                        "reason_code": f"missing_fields:{','.join(missing_fields)}",
                    }
                )
                continue
            if not record["active"]:
                invalid_by_gate[gate_name].append(record)
                invalid_findings.append({"waiver_id": waiver_id, "gate_name": gate_name, "reason_code": "inactive_waiver"})
                continue
            if created_dt is None or expires_dt is None or generated_dt is None:
                invalid_by_gate[gate_name].append(record)
                invalid_findings.append({"waiver_id": waiver_id, "gate_name": gate_name, "reason_code": "invalid_timestamp"})
                continue
            if expires_dt <= generated_dt:
                invalid_by_gate[gate_name].append(record)
                invalid_findings.append({"waiver_id": waiver_id, "gate_name": gate_name, "reason_code": "waiver_expired"})
                continue
            valid_by_gate[gate_name].append(record)
        return {
            "valid_waivers_by_gate": valid_by_gate,
            "invalid_waivers_by_gate": invalid_by_gate,
            "summary": {
                "waivers_path": str(waivers_path),
                "exists": True,
                "valid_waiver_count": sum(len(items) for items in valid_by_gate.values()),
                "invalid_waiver_count": sum(len(items) for items in invalid_by_gate.values()),
                "invalid_waiver_findings": invalid_findings,
            },
        }

    def compute_metrics_snapshot(
        self,
        *,
        as_of_date: str,
        lookback_window_days: int = 30,
        benchmark_version: str = "phase2.pr10.v1",
    ) -> dict[str, Any]:
        self.phase1.init_db()
        if lookback_window_days <= 0:
            raise ValueError("lookback_window_days must be > 0")
        try:
            as_of = date.fromisoformat(as_of_date)
        except ValueError as error:
            raise ValueError("as_of_date must be YYYY-MM-DD") from error
        lookback_start = as_of - timedelta(days=lookback_window_days - 1)
        lookback_start_iso = lookback_start.isoformat()

        intent_rows = self.repo.fetch_all(
            "SELECT action_intent_id, status FROM action_intents WHERE as_of_date = ?",
            (as_of_date,),
        )
        exec_rows = self.repo.fetch_all(
            """
            SELECT ae.status
            FROM action_executions ae
            JOIN action_intents ai ON ai.action_intent_id = ae.action_intent_id
            WHERE ai.as_of_date = ?
            """,
            (as_of_date,),
        )
        cases_open = self.repo.fetch_all(
            """
            SELECT exception_case_id FROM exception_cases
            WHERE status = 'OPEN' AND json_extract(details_json, '$.as_of_date') = ?
            """,
            (as_of_date,),
        )
        decisions = self.repo.fetch_all(
            "SELECT human_decision_id FROM human_decisions WHERE substr(decided_at, 1, 10) = ?",
            (as_of_date,),
        )
        decisions_in_exceptions_row = self.repo.fetch_one(
            """
            SELECT COUNT(*) AS cnt
            FROM human_decisions
            WHERE substr(decided_at, 1, 10) BETWEEN ? AND ?
            """,
            (lookback_start_iso, as_of_date),
        )
        user_override_row = self.repo.fetch_one(
            """
            SELECT COUNT(*) AS cnt
            FROM automation_decisions
            WHERE decision IN ('user_corrected', 'user_confirmed')
              AND substr(created_at, 1, 10) BETWEEN ? AND ?
            """,
            (lookback_start_iso, as_of_date),
        )
        manual_via_exceptions = int((decisions_in_exceptions_row or {"cnt": 0})["cnt"] or 0)
        manual_user_overrides = int((user_override_row or {"cnt": 0})["cnt"] or 0)
        manual_total = manual_via_exceptions + manual_user_overrides
        if manual_total == 0:
            manual_interactions_in_exceptions_rate = 1.0
            manual_interactions_gate_pass = True
        else:
            manual_interactions_in_exceptions_rate = manual_via_exceptions / manual_total
            manual_interactions_gate_pass = manual_interactions_in_exceptions_rate >= 0.80

        total_intents = len(intent_rows)
        success_exec = sum(1 for row in exec_rows if str(row.get("status") or "").upper() in {"SUCCESS", "SKIPPED"})
        open_count = len(cases_open)
        touchless_rate_legacy = round((total_intents - open_count) / total_intents, 4) if total_intents else 0.0
        auto_action_success_rate_legacy = round(success_exec / total_intents, 4) if total_intents else 0.0
        intake_metrics = self._pr8_intake_metrics(
            lookback_start_iso=lookback_start_iso,
            as_of_date=as_of_date,
            benchmark_version=benchmark_version,
        )
        pr9_metrics = self._pr9_transport_doc_metrics(
            lookback_start_iso=lookback_start_iso,
            as_of_date=as_of_date,
            benchmark_version=benchmark_version,
        )
        pr10_metrics = self._pr10_settlement_metrics(
            lookback_start_iso=lookback_start_iso,
            as_of_date=as_of_date,
            benchmark_version=benchmark_version,
            manual_human_decisions=manual_via_exceptions,
            manual_user_overrides=manual_user_overrides,
        )
        generated_at_utc = utc_now_iso_z()
        metrics: dict[str, Any] = {
            "as_of_date": as_of_date,
            "lookback_window_days": int(lookback_window_days),
            "lookback_window_start_date": lookback_start_iso,
            "benchmark_version": benchmark_version,
            "generated_at_utc": generated_at_utc,
            "intents_total": total_intents,
            "executions_success_or_skipped": success_exec,
            "exceptions_open": open_count,
            "manual_intervention_count": len(decisions),
            "touchless_rate": pr10_metrics["touchless_rate"],
            "touchless_rate_reason_code": pr10_metrics["touchless_rate_reason_code"],
            "touchless_rate_legacy": touchless_rate_legacy,
            "auto_action_success_rate": pr10_metrics["auto_action_success_rate"],
            "auto_action_success_rate_reason_code": pr10_metrics["auto_action_success_rate_reason_code"],
            "auto_action_success_rate_legacy": auto_action_success_rate_legacy,
            "manual_interactions_via_exceptions": manual_via_exceptions,
            "manual_interactions_user_overrides": manual_user_overrides,
            "manual_interactions_total": manual_total,
            "manual_interactions_in_exceptions_rate": round(manual_interactions_in_exceptions_rate, 4),
            "manual_interactions_in_exceptions_gate_threshold": 0.80,
            "manual_interactions_in_exceptions_gate_pass": bool(manual_interactions_gate_pass),
            "median_manual_fields_per_intake": intake_metrics["median_manual_fields_per_intake"],
            "median_manual_fields_per_intake_gate_threshold": 6,
            "median_manual_fields_per_intake_gate_pass": intake_metrics["median_manual_fields_per_intake_gate_pass"],
            "autoplan_common_case_total": intake_metrics["autoplan_common_case_total"],
            "autoplan_zero_edit_common_case_count": intake_metrics["autoplan_zero_edit_common_case_count"],
            "autoplan_zero_edit_common_case_rate": intake_metrics["autoplan_zero_edit_common_case_rate"],
            "autoplan_zero_edit_common_case_gate_target": 1.0,
            "autoplan_zero_edit_common_case_gate_pass": intake_metrics["autoplan_zero_edit_common_case_gate_pass"],
            "intake_runs_with_confirm_total": intake_metrics["intake_runs_with_confirm_total"],
            "intake_decision_distribution": intake_metrics["intake_decision_distribution"],
            "benchmark_version_expected_pr8": intake_metrics["benchmark_version_expected_pr8"],
            "benchmark_version_match_pr8": intake_metrics["benchmark_version_match_pr8"],
            "pr8_gate_pass": intake_metrics["pr8_gate_pass"],
            "pr8_gate_reason_code": intake_metrics["pr8_gate_reason_code"],
            "manual_transport_field_updates": pr9_metrics["manual_transport_field_updates"],
            "deliveries_with_transport_assignment": pr9_metrics["deliveries_with_transport_assignment"],
            "manual_transport_fields_per_delivery": pr9_metrics["manual_transport_fields_per_delivery"],
            "manual_transport_fields_per_delivery_gate_threshold": 3.0,
            "manual_transport_fields_per_delivery_gate_pass": pr9_metrics["manual_transport_fields_per_delivery_gate_pass"],
            "true_positive_autolinks": pr9_metrics["true_positive_autolinks"],
            "false_positive_autolinks": pr9_metrics["false_positive_autolinks"],
            "doc_autolink_precision": pr9_metrics["doc_autolink_precision"],
            "doc_autolink_precision_gate_threshold": 0.90,
            "doc_autolink_precision_gate_pass": pr9_metrics["doc_autolink_precision_gate_pass"],
            "benchmark_version_expected_pr9": pr9_metrics["benchmark_version_expected_pr9"],
            "benchmark_version_match_pr9": pr9_metrics["benchmark_version_match_pr9"],
            "pr9_gate_pass": pr9_metrics["pr9_gate_pass"],
            "pr9_gate_reason_code": pr9_metrics["pr9_gate_reason_code"],
        }
        metrics.update(pr10_metrics)
        return metrics

    def _pr8_intake_metrics(
        self,
        *,
        lookback_start_iso: str,
        as_of_date: str,
        benchmark_version: str,
    ) -> dict[str, Any]:
        expected_benchmark_version = PR8_BENCHMARK_VERSION
        benchmark_match = benchmark_version == expected_benchmark_version
        scoped_contract_ids: list[str] = []
        scoped_run_ids: list[str] = []
        benchmark_run = self.repo.get_benchmark_run(
            as_of_date=as_of_date,
            benchmark_version=benchmark_version,
        )
        if benchmark_run:
            fixture_metadata = json.loads(benchmark_run.get("fixture_metadata_json") or "{}")
            if isinstance(fixture_metadata, dict):
                contract_ids = fixture_metadata.get("contract_ids")
                if isinstance(contract_ids, list):
                    scoped_contract_ids = [str(item).strip() for item in contract_ids if str(item).strip()]
                intake_run_id = str(fixture_metadata.get("intake_run_id") or "").strip()
                if intake_run_id:
                    scoped_run_ids = [intake_run_id]

        if scoped_run_ids:
            run_placeholders = ",".join("?" for _ in scoped_run_ids)
            distribution_rows = self.repo.fetch_all(
                f"""
                SELECT ad.decision, COUNT(*) AS cnt
                FROM automation_decisions ad
                WHERE ad.stage = 'intake_parser'
                  AND ad.run_id IN ({run_placeholders})
                GROUP BY ad.decision
                """,
                tuple(scoped_run_ids),
            )
        else:
            distribution_rows = self.repo.fetch_all(
                """
                SELECT ad.decision, COUNT(*) AS cnt
                FROM automation_decisions ad
                JOIN automation_runs ar ON ar.run_id = ad.run_id
                WHERE ad.stage = 'intake_parser'
                  AND ar.as_of_date BETWEEN ? AND ?
                GROUP BY ad.decision
                """,
                (lookback_start_iso, as_of_date),
            )

        decision_distribution = {"auto_applied": 0, "needs_review": 0, "blocked": 0}
        for row in distribution_rows:
            key = str(row.get("decision") or "").strip()
            if key in decision_distribution:
                decision_distribution[key] = int(row.get("cnt") or 0)

        if scoped_run_ids:
            confirm_rows = self.repo.fetch_all(
                f"""
                SELECT DISTINCT ad.run_id
                FROM automation_decisions ad
                WHERE ad.stage = 'intake_confirm'
                  AND ad.run_id IN ({run_placeholders})
                ORDER BY ad.run_id
                """,
                tuple(scoped_run_ids),
            )
        else:
            confirm_rows = self.repo.fetch_all(
                """
                SELECT DISTINCT ad.run_id
                FROM automation_decisions ad
                JOIN automation_runs ar ON ar.run_id = ad.run_id
                WHERE ad.stage = 'intake_confirm'
                  AND ar.as_of_date BETWEEN ? AND ?
                ORDER BY ad.run_id
                """,
                (lookback_start_iso, as_of_date),
            )
        confirm_run_ids = [str(row.get("run_id") or "") for row in confirm_rows if str(row.get("run_id") or "").strip()]

        if scoped_run_ids:
            corrected_rows = self.repo.fetch_all(
                f"""
                SELECT ad.run_id, COUNT(*) AS cnt
                FROM automation_decisions ad
                WHERE ad.stage = 'intake_confirm'
                  AND ad.decision = 'user_corrected'
                  AND ad.run_id IN ({run_placeholders})
                GROUP BY ad.run_id
                """,
                tuple(scoped_run_ids),
            )
        else:
            corrected_rows = self.repo.fetch_all(
                """
                SELECT ad.run_id, COUNT(*) AS cnt
                FROM automation_decisions ad
                JOIN automation_runs ar ON ar.run_id = ad.run_id
                WHERE ad.stage = 'intake_confirm'
                  AND ad.decision = 'user_corrected'
                  AND ar.as_of_date BETWEEN ? AND ?
                GROUP BY ad.run_id
                """,
                (lookback_start_iso, as_of_date),
            )
        corrected_by_run = {
            str(row.get("run_id") or ""): int(row.get("cnt") or 0)
            for row in corrected_rows
            if str(row.get("run_id") or "").strip()
        }
        corrected_counts = [int(corrected_by_run.get(run_id, 0)) for run_id in confirm_run_ids]
        median_manual_fields = float(median(corrected_counts)) if corrected_counts else None
        median_manual_gate_pass = bool(
            median_manual_fields is not None and median_manual_fields < 6.0
        )

        if scoped_contract_ids:
            placeholders = ",".join("?" for _ in scoped_contract_ids)
            lines = self.repo.fetch_all(
                f"""
                SELECT cli.contract_line_id, cli.product_code, cli.expected_qty_kg
                FROM contract_line_items cli
                WHERE cli.contract_id IN ({placeholders})
                """,
                tuple(scoped_contract_ids),
            )
        else:
            lines = self.repo.fetch_all(
                """
                SELECT cli.contract_line_id, cli.product_code, cli.expected_qty_kg
                FROM contracts c
                JOIN contract_line_items cli ON cli.contract_id = c.contract_id
                WHERE c.issue_date BETWEEN ? AND ?
                """,
                (lookback_start_iso, as_of_date),
            )
        products_policy = self.config.delivery_policies.get("products", {}) if isinstance(self.config.delivery_policies, dict) else {}
        common_case_total = 0
        zero_edit_count = 0
        for line in lines:
            product_code = str(line.get("product_code") or "").upper()
            policy = products_policy.get(product_code) if isinstance(products_policy, dict) else None
            if not isinstance(policy, dict):
                continue
            lot_mt = policy.get("default_lot_mt")
            if lot_mt in (None, ""):
                continue
            lot_size_kg = mt_to_kg_int(lot_mt)
            expected_qty_kg = int(line.get("expected_qty_kg") or 0)
            if expected_qty_kg <= 0 or lot_size_kg <= 0 or expected_qty_kg % lot_size_kg != 0:
                continue
            common_case_total += 1
            expected_lot_count = expected_qty_kg // lot_size_kg
            planned_rows = self.repo.fetch_all(
                """
                SELECT planned_qty_kg, notes
                FROM planned_deliveries
                WHERE contract_line_id = ?
                  AND status <> 'CANCELLED'
                ORDER BY sequence_no ASC
                """,
                (line["contract_line_id"],),
            )
            if len(planned_rows) != expected_lot_count:
                continue
            all_default_qty = all(int(row.get("planned_qty_kg") or 0) == lot_size_kg for row in planned_rows)
            no_manual_override = all("web_v2_edit" not in str(row.get("notes") or "").lower() for row in planned_rows)
            if all_default_qty and no_manual_override:
                zero_edit_count += 1

        if common_case_total > 0:
            zero_edit_rate_value = zero_edit_count / common_case_total
            zero_edit_rate = round(zero_edit_rate_value, 4)
            zero_edit_gate_pass = zero_edit_count == common_case_total
        else:
            zero_edit_rate = None
            zero_edit_gate_pass = False

        has_intake_confirm_data = len(confirm_run_ids) > 0
        if benchmark_match and has_intake_confirm_data and median_manual_gate_pass and zero_edit_gate_pass:
            pr8_gate_pass = True
            pr8_reason = "pass"
        elif not benchmark_match:
            pr8_gate_pass = False
            pr8_reason = "benchmark_version_mismatch"
        elif not has_intake_confirm_data:
            pr8_gate_pass = False
            pr8_reason = "insufficient_intake_data"
        elif not median_manual_gate_pass:
            pr8_gate_pass = False
            pr8_reason = "median_manual_fields_threshold_failed"
        else:
            pr8_gate_pass = False
            pr8_reason = "autoplan_zero_edit_common_case_failed"

        return {
            "median_manual_fields_per_intake": median_manual_fields,
            "median_manual_fields_per_intake_gate_pass": median_manual_gate_pass,
            "autoplan_common_case_total": int(common_case_total),
            "autoplan_zero_edit_common_case_count": int(zero_edit_count),
            "autoplan_zero_edit_common_case_rate": zero_edit_rate,
            "autoplan_zero_edit_common_case_gate_pass": bool(zero_edit_gate_pass),
            "intake_runs_with_confirm_total": int(len(confirm_run_ids)),
            "intake_decision_distribution": decision_distribution,
            "benchmark_version_expected_pr8": expected_benchmark_version,
            "benchmark_version_match_pr8": bool(benchmark_match),
            "pr8_gate_pass": bool(pr8_gate_pass),
            "pr8_gate_reason_code": pr8_reason,
        }

    def _pr9_transport_doc_metrics(
        self,
        *,
        lookback_start_iso: str,
        as_of_date: str,
        benchmark_version: str,
    ) -> dict[str, Any]:
        expected_benchmark_version = PR9_BENCHMARK_VERSION
        benchmark_match = benchmark_version == expected_benchmark_version

        transport_rows = self.repo.fetch_one(
            """
            SELECT
              COUNT(DISTINCT d.delivery_id) AS deliveries_with_transport_assignment
            FROM deliveries d
            LEFT JOIN delivery_transport_snapshot s ON s.delivery_id = d.delivery_id
            WHERE substr(COALESCE(s.created_at, d.updated_at, d.created_at), 1, 10) BETWEEN ? AND ?
              AND (
                s.snapshot_id IS NOT NULL
                OR TRIM(COALESCE(d.truck_no, '')) <> ''
                OR TRIM(COALESCE(d.driver_name, '')) <> ''
              )
            """,
            (lookback_start_iso, as_of_date),
        ) or {"deliveries_with_transport_assignment": 0}
        deliveries_with_transport_assignment = int(transport_rows.get("deliveries_with_transport_assignment") or 0)

        manual_transport_row = self.repo.fetch_one(
            """
            SELECT COUNT(*) AS cnt
            FROM decision_outcomes
            WHERE outcome_label = 'transport_suggestion_feedback'
              AND substr(created_at, 1, 10) BETWEEN ? AND ?
            """,
            (lookback_start_iso, as_of_date),
        ) or {"cnt": 0}
        manual_transport_field_updates = int(manual_transport_row.get("cnt") or 0)
        if deliveries_with_transport_assignment > 0:
            manual_transport_fields_per_delivery = round(
                manual_transport_field_updates / deliveries_with_transport_assignment,
                4,
            )
            manual_transport_gate_pass = manual_transport_fields_per_delivery < 3.0
        else:
            manual_transport_fields_per_delivery = None
            manual_transport_gate_pass = False

        doc_rows = self.repo.fetch_one(
            """
            SELECT
              SUM(CASE WHEN link_status = 'AUTO_LINKED' THEN 1 ELSE 0 END) AS true_positive_autolinks,
              SUM(CASE WHEN link_status = 'AUTO_LINK_REJECTED' THEN 1 ELSE 0 END) AS false_positive_autolinks
            FROM evidence_originals
            WHERE substr(COALESCE(linked_at, updated_at, created_at), 1, 10) BETWEEN ? AND ?
            """,
            (lookback_start_iso, as_of_date),
        ) or {"true_positive_autolinks": 0, "false_positive_autolinks": 0}
        true_positive_autolinks = int(doc_rows.get("true_positive_autolinks") or 0)
        false_positive_autolinks = int(doc_rows.get("false_positive_autolinks") or 0)
        doc_denom = true_positive_autolinks + false_positive_autolinks
        if doc_denom > 0:
            doc_autolink_precision = round(true_positive_autolinks / doc_denom, 4)
            doc_autolink_precision_gate_pass = doc_autolink_precision >= 0.90
        else:
            doc_autolink_precision = None
            doc_autolink_precision_gate_pass = False

        if not benchmark_match:
            pr9_gate_pass = False
            pr9_reason = "benchmark_version_mismatch"
        elif deliveries_with_transport_assignment == 0:
            pr9_gate_pass = False
            pr9_reason = "insufficient_transport_data"
        elif not manual_transport_gate_pass:
            pr9_gate_pass = False
            pr9_reason = "manual_transport_fields_threshold_failed"
        elif doc_denom == 0:
            pr9_gate_pass = False
            pr9_reason = "insufficient_doc_autolink_data"
        elif not doc_autolink_precision_gate_pass:
            pr9_gate_pass = False
            pr9_reason = "doc_autolink_precision_failed"
        else:
            pr9_gate_pass = True
            pr9_reason = "pass"

        return {
            "manual_transport_field_updates": manual_transport_field_updates,
            "deliveries_with_transport_assignment": deliveries_with_transport_assignment,
            "manual_transport_fields_per_delivery": manual_transport_fields_per_delivery,
            "manual_transport_fields_per_delivery_gate_pass": bool(manual_transport_gate_pass),
            "true_positive_autolinks": true_positive_autolinks,
            "false_positive_autolinks": false_positive_autolinks,
            "doc_autolink_precision": doc_autolink_precision,
            "doc_autolink_precision_gate_pass": bool(doc_autolink_precision_gate_pass),
            "benchmark_version_expected_pr9": expected_benchmark_version,
            "benchmark_version_match_pr9": bool(benchmark_match),
            "pr9_gate_pass": bool(pr9_gate_pass),
            "pr9_gate_reason_code": pr9_reason,
        }

    def _parse_iso_dt(self, value: str) -> datetime | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            if text.endswith("Z"):
                return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
            parsed = datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except Exception:
            return None

    def _safe_json_object(self, value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        text = str(value or "").strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _drift_case_severity(self, drift_state: str) -> str:
        state = str(drift_state or "").upper()
        if state in {"MISMATCH", "ALERT"}:
            return "BLOCKER"
        if state == "INSUFFICIENT_DATA":
            return "REVIEW"
        if state == "WATCH":
            return "INFO"
        return "INFO"

    def _drift_state_weight(self, drift_state: str) -> int:
        state = str(drift_state or "").upper()
        if state in {"MISMATCH", "ALERT"}:
            return 4
        if state == "WATCH":
            return 3
        if state == "INSUFFICIENT_DATA":
            return 2
        return 1

    def _percentile(self, values: list[float], pct: float) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        if len(ordered) == 1:
            return round(float(ordered[0]), 4)
        rank = (len(ordered) - 1) * (pct / 100.0)
        lower = int(rank)
        upper = min(lower + 1, len(ordered) - 1)
        fraction = rank - lower
        value = ordered[lower] + (ordered[upper] - ordered[lower]) * fraction
        return round(float(value), 4)

    def _settlement_aging_counts(self, *, as_of_date: str) -> dict[str, int]:
        row = self.repo.fetch_one(
            """
            SELECT
              SUM(
                CASE
                  WHEN outstanding_balance <= 0 THEN 0
                  WHEN due_date IS NULL THEN 1
                  WHEN julianday(?) - julianday(due_date) <= 0 THEN 1
                  ELSE 0
                END
              ) AS bucket_current,
              SUM(
                CASE
                  WHEN outstanding_balance <= 0 THEN 0
                  WHEN due_date IS NOT NULL AND julianday(?) - julianday(due_date) > 0 AND julianday(?) - julianday(due_date) <= 30 THEN 1
                  ELSE 0
                END
              ) AS bucket_1_30,
              SUM(
                CASE
                  WHEN outstanding_balance <= 0 THEN 0
                  WHEN due_date IS NOT NULL AND julianday(?) - julianday(due_date) > 30 AND julianday(?) - julianday(due_date) <= 60 THEN 1
                  ELSE 0
                END
              ) AS bucket_31_60,
              SUM(
                CASE
                  WHEN outstanding_balance <= 0 THEN 0
                  WHEN due_date IS NOT NULL AND julianday(?) - julianday(due_date) > 60 AND julianday(?) - julianday(due_date) <= 90 THEN 1
                  ELSE 0
                END
              ) AS bucket_61_90,
              SUM(
                CASE
                  WHEN outstanding_balance <= 0 THEN 0
                  WHEN due_date IS NOT NULL AND julianday(?) - julianday(due_date) > 90 THEN 1
                  ELSE 0
                END
              ) AS bucket_90_plus
            FROM drep_sales
            WHERE outstanding_balance > 0
              AND (invoice_date IS NULL OR invoice_date <= ?)
            """,
            (
                as_of_date,
                as_of_date,
                as_of_date,
                as_of_date,
                as_of_date,
                as_of_date,
                as_of_date,
                as_of_date,
                as_of_date,
            ),
        ) or {}
        return {
            "CURRENT": int(row.get("bucket_current") or 0),
            "1-30": int(row.get("bucket_1_30") or 0),
            "31-60": int(row.get("bucket_31_60") or 0),
            "61-90": int(row.get("bucket_61_90") or 0),
            "90+": int(row.get("bucket_90_plus") or 0),
        }

    def _pr10_settlement_metrics(
        self,
        *,
        lookback_start_iso: str,
        as_of_date: str,
        benchmark_version: str,
        manual_human_decisions: int,
        manual_user_overrides: int,
    ) -> dict[str, Any]:
        expected_benchmark_version = PR10_BENCHMARK_VERSION
        benchmark_match = benchmark_version == expected_benchmark_version
        completed_row = self.repo.fetch_one(
            """
            SELECT COUNT(*) AS cnt
            FROM deliveries
            WHERE status IN ('INVOICED', 'PAID')
              AND substr(COALESCE(invoiced_at, paid_at, updated_at, created_at), 1, 10) BETWEEN ? AND ?
            """,
            (lookback_start_iso, as_of_date),
        ) or {"cnt": 0}
        completed_deliveries = int(completed_row.get("cnt") or 0)
        manual_delivery_row = self.repo.fetch_one(
            """
            SELECT COUNT(DISTINCT ec.delivery_id) AS cnt
            FROM exception_cases ec
            JOIN human_decisions hd ON hd.exception_case_id = ec.exception_case_id
            JOIN deliveries d ON d.delivery_id = ec.delivery_id
            WHERE ec.delivery_id IS NOT NULL
              AND d.status IN ('INVOICED', 'PAID')
              AND substr(hd.decided_at, 1, 10) BETWEEN ? AND ?
            """,
            (lookback_start_iso, as_of_date),
        ) or {"cnt": 0}
        deliveries_with_manual_decisions = int(manual_delivery_row.get("cnt") or 0)
        completed_without_manual = max(completed_deliveries - deliveries_with_manual_decisions, 0)
        if completed_deliveries > 0:
            touchless_rate = round(completed_without_manual / completed_deliveries, 4)
            manual_inputs_per_delivery = round((manual_human_decisions + manual_user_overrides) / completed_deliveries, 4)
            touchless_reason = "pass"
            manual_inputs_reason = "pass"
        else:
            touchless_rate = None
            manual_inputs_per_delivery = None
            touchless_reason = "insufficient_touchless_data"
            manual_inputs_reason = "insufficient_manual_input_data"

        action_row = self.repo.fetch_one(
            """
            SELECT
              COUNT(*) AS attempted_actions,
              SUM(CASE WHEN status = 'SUCCESS' THEN 1 ELSE 0 END) AS successful_actions
            FROM action_executions
            WHERE substr(created_at, 1, 10) BETWEEN ? AND ?
            """,
            (lookback_start_iso, as_of_date),
        ) or {"attempted_actions": 0, "successful_actions": 0}
        attempted_actions = int(action_row.get("attempted_actions") or 0)
        successful_actions = int(action_row.get("successful_actions") or 0)
        if attempted_actions > 0:
            auto_action_success_rate = round(successful_actions / attempted_actions, 4)
            auto_action_reason = "pass"
        else:
            auto_action_success_rate = None
            auto_action_reason = "insufficient_action_execution_data"

        suggestion_row = self.repo.fetch_one(
            """
            SELECT
              SUM(CASE WHEN event_type = 'SETTLEMENT_SUGGESTION_ACCEPTED' THEN 1 ELSE 0 END) AS accepted_count,
              SUM(CASE WHEN event_type IN ('SETTLEMENT_SUGGESTION_ACCEPTED', 'SETTLEMENT_SUGGESTION_ROUTED_EXCEPTION') THEN 1 ELSE 0 END) AS reviewed_count
            FROM event_log
            WHERE event_type IN ('SETTLEMENT_SUGGESTION_ACCEPTED', 'SETTLEMENT_SUGGESTION_ROUTED_EXCEPTION')
              AND COALESCE(as_of_date, substr(created_at, 1, 10)) BETWEEN ? AND ?
            """,
            (lookback_start_iso, as_of_date),
        ) or {"accepted_count": 0, "reviewed_count": 0}
        accepted_suggestions = int(suggestion_row.get("accepted_count") or 0)
        reviewed_suggestions = int(suggestion_row.get("reviewed_count") or 0)
        if reviewed_suggestions > 0:
            payment_suggestion_acceptance_rate = round(accepted_suggestions / reviewed_suggestions, 4)
            payment_suggestion_reason = "pass"
        else:
            payment_suggestion_acceptance_rate = None
            payment_suggestion_reason = "insufficient_payment_suggestion_data"

        resolution_rows = self.repo.fetch_all(
            """
            SELECT created_at, resolved_at
            FROM exception_cases
            WHERE resolved_at IS NOT NULL
              AND status = 'RESOLVED'
              AND substr(resolved_at, 1, 10) BETWEEN ? AND ?
            """,
            (lookback_start_iso, as_of_date),
        )
        resolution_hours: list[float] = []
        for row in resolution_rows:
            created_dt = self._parse_iso_dt(str(row.get("created_at") or ""))
            resolved_dt = self._parse_iso_dt(str(row.get("resolved_at") or ""))
            if created_dt is None or resolved_dt is None:
                continue
            resolution_hours.append(max((resolved_dt - created_dt).total_seconds(), 0.0) / 3600.0)
        if resolution_hours:
            exception_p50 = self._percentile(resolution_hours, 50.0)
            exception_p95 = self._percentile(resolution_hours, 95.0)
            exception_reason = "pass"
        else:
            exception_p50 = None
            exception_p95 = None
            exception_reason = "insufficient_exception_resolution_data"

        lpo_pack_rows = self.repo.fetch_all(
            """
            SELECT
              c.contract_id,
              COALESCE(
                (
                  SELECT MIN(el.created_at)
                  FROM event_log el
                  WHERE el.entity_type = 'CONTRACT'
                    AND el.entity_id = c.contract_id
                    AND el.event_type = 'INTAKE_CONFIRMED'
                ),
                c.created_at
              ) AS intake_confirmed_at,
              (
                SELECT MIN(d.generated_at)
                FROM documents d
                JOIN sales_transactions st ON st.sales_transaction_id = d.sales_transaction_id
                WHERE d.doc_type = 'INVOICE'
                  AND st.contract_id = c.contract_id
              ) AS first_pack_generated_at
            FROM contracts c
            WHERE substr(
              COALESCE(
                (
                  SELECT MIN(el.created_at)
                  FROM event_log el
                  WHERE el.entity_type = 'CONTRACT'
                    AND el.entity_id = c.contract_id
                    AND el.event_type = 'INTAKE_CONFIRMED'
                ),
                c.created_at
              ),
              1,
              10
            ) BETWEEN ? AND ?
            """,
            (lookback_start_iso, as_of_date),
        )
        lpo_to_pack_minutes_values: list[float] = []
        for row in lpo_pack_rows:
            intake_dt = self._parse_iso_dt(str(row.get("intake_confirmed_at") or ""))
            pack_dt = self._parse_iso_dt(str(row.get("first_pack_generated_at") or ""))
            if intake_dt is None or pack_dt is None:
                continue
            lpo_to_pack_minutes_values.append(max((pack_dt - intake_dt).total_seconds(), 0.0) / 60.0)
        if lpo_to_pack_minutes_values:
            first_time_lpo_to_pack_minutes = self._percentile(lpo_to_pack_minutes_values, 50.0)
            first_time_lpo_reason = "pass"
        else:
            first_time_lpo_to_pack_minutes = None
            first_time_lpo_reason = "insufficient_lpo_pack_data"

        as_of_dt = date.fromisoformat(as_of_date)
        current_start = (as_of_dt - timedelta(days=6)).isoformat()
        prev_start = (as_of_dt - timedelta(days=13)).isoformat()
        prev_end = (as_of_dt - timedelta(days=7)).isoformat()
        current_rows = self.repo.fetch_all(
            """
            SELECT created_at, resolved_at
            FROM exception_cases
            WHERE resolved_at IS NOT NULL
              AND status = 'RESOLVED'
              AND substr(resolved_at, 1, 10) BETWEEN ? AND ?
            """,
            (current_start, as_of_date),
        )
        previous_rows = self.repo.fetch_all(
            """
            SELECT created_at, resolved_at
            FROM exception_cases
            WHERE resolved_at IS NOT NULL
              AND status = 'RESOLVED'
              AND substr(resolved_at, 1, 10) BETWEEN ? AND ?
            """,
            (prev_start, prev_end),
        )
        current_hours: list[float] = []
        previous_hours: list[float] = []
        for bucket, rows in ((current_hours, current_rows), (previous_hours, previous_rows)):
            for row in rows:
                created_dt = self._parse_iso_dt(str(row.get("created_at") or ""))
                resolved_dt = self._parse_iso_dt(str(row.get("resolved_at") or ""))
                if created_dt is None or resolved_dt is None:
                    continue
                bucket.append(max((resolved_dt - created_dt).total_seconds(), 0.0) / 3600.0)
        current_p95 = self._percentile(current_hours, 95.0)
        previous_p95 = self._percentile(previous_hours, 95.0)
        if current_p95 is None or previous_p95 is None:
            exception_trend_state = "insufficient_data"
            exception_trend_reason = "insufficient_exception_trend_data"
        elif float(current_p95) <= float(previous_p95):
            exception_trend_state = "stable_or_improving"
            exception_trend_reason = "pass"
        else:
            exception_trend_state = "worsening"
            exception_trend_reason = "exception_p95_worsening"

        settlement_aging_current = self._settlement_aging_counts(as_of_date=as_of_date)
        settlement_aging_previous = self._settlement_aging_counts(as_of_date=prev_end)
        current_total_aging = int(sum(settlement_aging_current.values()))
        previous_total_aging = int(sum(settlement_aging_previous.values()))
        if current_total_aging <= 0 or previous_total_aging <= 0:
            settlement_aging_trend_state = "insufficient_data"
            settlement_aging_trend_reason = "insufficient_settlement_aging_data"
        elif int(settlement_aging_current.get("90+", 0)) <= int(settlement_aging_previous.get("90+", 0)):
            settlement_aging_trend_state = "stable_or_improving"
            settlement_aging_trend_reason = "pass"
        else:
            settlement_aging_trend_state = "worsening"
            settlement_aging_trend_reason = "settlement_aging_90_plus_worsening"

        payment_acceptance_gate_pass = (
            payment_suggestion_acceptance_rate is not None and payment_suggestion_acceptance_rate >= 0.70
        )
        exception_trend_gate_pass = exception_trend_state == "stable_or_improving"
        if not benchmark_match:
            pr10_gate_pass = False
            pr10_gate_reason = "benchmark_version_mismatch"
        elif payment_suggestion_acceptance_rate is None:
            pr10_gate_pass = False
            pr10_gate_reason = "insufficient_payment_suggestion_data"
        elif not payment_acceptance_gate_pass:
            pr10_gate_pass = False
            pr10_gate_reason = "payment_suggestion_acceptance_rate_failed"
        elif not exception_trend_gate_pass:
            pr10_gate_pass = False
            pr10_gate_reason = exception_trend_reason
        else:
            pr10_gate_pass = True
            pr10_gate_reason = "pass"

        return {
            "completed_deliveries": completed_deliveries,
            "completed_deliveries_without_manual_decisions": completed_without_manual,
            "touchless_rate": touchless_rate,
            "touchless_rate_reason_code": touchless_reason,
            "manual_inputs_per_delivery": manual_inputs_per_delivery,
            "manual_inputs_per_delivery_reason_code": manual_inputs_reason,
            "attempted_action_executions": attempted_actions,
            "successful_action_executions": successful_actions,
            "auto_action_success_rate": auto_action_success_rate,
            "auto_action_success_rate_reason_code": auto_action_reason,
            "accepted_payment_suggestions": accepted_suggestions,
            "total_payment_suggestions_reviewed": reviewed_suggestions,
            "payment_suggestion_acceptance_rate": payment_suggestion_acceptance_rate,
            "payment_suggestion_acceptance_rate_reason_code": payment_suggestion_reason,
            "exception_resolution_time_hours_p50": exception_p50,
            "exception_resolution_time_hours_p95": exception_p95,
            "exception_resolution_time_reason_code": exception_reason,
            "first_time_lpo_to_pack_minutes": first_time_lpo_to_pack_minutes,
            "first_time_lpo_to_pack_reason_code": first_time_lpo_reason,
            "exception_resolution_current_p95_hours": current_p95,
            "exception_resolution_previous_p95_hours": previous_p95,
            "exception_resolution_trend_state": exception_trend_state,
            "exception_resolution_trend_reason_code": exception_trend_reason,
            "settlement_aging_current": settlement_aging_current,
            "settlement_aging_previous": settlement_aging_previous,
            "settlement_aging_trend_state": settlement_aging_trend_state,
            "settlement_aging_trend_reason_code": settlement_aging_trend_reason,
            "benchmark_version_expected_pr10": expected_benchmark_version,
            "benchmark_version_match_pr10": bool(benchmark_match),
            "pr10_gate_pass": bool(pr10_gate_pass),
            "pr10_gate_reason_code": pr10_gate_reason,
        }

    def _contracts_for_autonomy(self, *, contract_id: str | None) -> list[dict[str, Any]]:
        if contract_id:
            row = self.repo.fetch_one("SELECT * FROM contracts WHERE contract_id = ?", (contract_id,))
            return [row] if row else []
        return self.repo.fetch_all(
            """
            SELECT * FROM contracts
            WHERE status IN ('OPEN', 'PARTIAL')
            ORDER BY issue_date ASC, contract_id ASC
            """
        )

    def _run_contract_autonomy(
        self,
        *,
        autonomy_run_id: str,
        contract: dict[str, Any],
        as_of_date: str,
        dry_run: bool,
    ) -> dict[str, Any]:
        contract_id = str(contract["contract_id"])
        runtime_policy = self._resolve_runtime_policy(contract=contract, as_of_date=as_of_date)
        gates = self._evaluate_contract_gates(
            autonomy_run_id=autonomy_run_id,
            contract=contract,
            as_of_date=as_of_date,
            runtime_policy=runtime_policy,
        )
        intents: list[dict[str, Any]] = []
        blocked = 0
        executed = 0
        for intent_type in self.autonomy_intent_order:
            intents.append(
                self._execute_intent_with_gates(
                    autonomy_run_id=autonomy_run_id,
                    contract=contract,
                    intent_type=intent_type,
                    gates=gates,
                    as_of_date=as_of_date,
                    dry_run=dry_run,
                    runtime_policy=runtime_policy,
                )
            )
        for item in intents:
            status = str(item.get("status") or "").upper()
            if status in {"BLOCKED", "FAILED"}:
                blocked += 1
            elif status in {"SUCCESS", "SKIPPED"}:
                executed += 1
        open_cases = self.repo.fetch_one(
            "SELECT COUNT(*) AS cnt FROM exception_cases WHERE contract_id = ? AND status = 'OPEN'",
            (contract_id,),
        )
        return {
            "contract_id": contract_id,
            "lpo_no": contract.get("lpo_no"),
            "gates": gates,
            "intents": intents,
            "intents_executed": executed,
            "intents_blocked": blocked,
            "exceptions_open": int((open_cases or {"cnt": 0})["cnt"] or 0),
            "runtime_policy": {
                "source": runtime_policy["source"],
                "policy_source_key": runtime_policy["policy_source_key"],
                "policy_version": runtime_policy["policy_version"],
                "selected_policy_set_ids": runtime_policy["selected_policy_set_ids"],
            },
        }

    def _resolve_runtime_policy(
        self,
        *,
        contract: dict[str, Any],
        as_of_date: str,
    ) -> dict[str, Any]:
        fallback_policy = self.config.automation_thresholds if isinstance(self.config.automation_thresholds, dict) else {}
        fallback_version = str(fallback_policy.get("schema_version") or "phase2.v1")
        resolved = self.repo.resolve_policy_runtime(
            as_of_date=as_of_date,
            contract_id=str(contract.get("contract_id") or "").strip() or None,
            master_contract_id=str(contract.get("master_contract_id") or "").strip() or None,
            buyer_id=str(contract.get("buyer_id") or "").strip() or None,
            fallback_policy=fallback_policy,
            fallback_version=fallback_version,
        )
        selected_sets = resolved.get("selected_sets", [])
        selected_sets = selected_sets if isinstance(selected_sets, list) else []
        return {
            "source": str(resolved.get("source") or "config"),
            "policy": resolved.get("policy", {}) if isinstance(resolved.get("policy"), dict) else {},
            "selected_sets": selected_sets,
            "selected_policy_set_ids": [str(row.get("policy_set_id")) for row in selected_sets if row.get("policy_set_id")],
            "policy_source_key": str(resolved.get("policy_source_key") or f"config:{fallback_version}"),
            "policy_version": str(resolved.get("policy_version") or fallback_version),
        }

    def _resolved_gate_overrides(self, *, contract_id: str) -> set[str]:
        rows = self.repo.fetch_all(
            """
            SELECT details_json
            FROM exception_cases
            WHERE contract_id = ? AND status = 'RESOLVED' AND reason_code = 'gate_failed'
            """,
            (contract_id,),
        )
        overrides: set[str] = set()
        for row in rows:
            try:
                details = json.loads(row.get("details_json") or "{}")
            except Exception:
                details = {}
            gate_name = str(details.get("gate_name") or "").strip()
            if gate_name:
                overrides.add(gate_name)
        return overrides

    def _evaluate_contract_gates(
        self,
        *,
        autonomy_run_id: str,
        contract: dict[str, Any],
        as_of_date: str,
        runtime_policy: dict[str, Any],
    ) -> dict[str, dict[str, Any]]:
        contract_id = str(contract["contract_id"])
        overrides = self._resolved_gate_overrides(contract_id=contract_id)
        gate_names = [
            "contract_active_valid",
            "quantity_tolerance",
            "evidence_ready",
            "coa_complete",
            "payment_match_confidence",
            "capacity_available",
            "transport_assignment_valid",
        ]
        results: dict[str, dict[str, Any]] = {}
        for gate_name in gate_names:
            outcome = self._evaluate_gate(
                gate_name=gate_name,
                contract=contract,
                as_of_date=as_of_date,
                runtime_policy=runtime_policy,
            )
            if outcome["status"] == "FAIL" and gate_name in overrides:
                outcome = {
                    **outcome,
                    "status": "PASS",
                    "score": 1.0,
                    "reason_code": "override_resolved_case",
                    "details": {**outcome.get("details", {}), "override": True},
                }
            with self.repo.transaction() as conn:
                self.repo.add_gate_evaluation(
                    conn,
                    autonomy_run_id=autonomy_run_id,
                    contract_id=contract_id,
                    delivery_id=None,
                    planned_delivery_id=None,
                    gate_name=gate_name,
                    subject_type="CONTRACT",
                    subject_id=contract_id,
                    as_of_date=as_of_date,
                    status=str(outcome["status"]),
                    score=float(outcome["score"]) if outcome.get("score") is not None else None,
                    reason_code=str(outcome["reason_code"]),
                    details=dict(outcome.get("details", {})),
                )
                self.repo.append_event(
                    conn,
                    entity_type="CONTRACT",
                    entity_id=contract_id,
                    event_type="GATE_EVALUATED",
                    as_of_date=as_of_date,
                    payload={
                        "gate_name": gate_name,
                        "status": outcome["status"],
                        "reason_code": outcome["reason_code"],
                    },
                    source="run-autonomy",
                )
            results[gate_name] = {
                "status": str(outcome["status"]),
                "score": float(outcome["score"]) if outcome.get("score") is not None else None,
                "reason_code": str(outcome["reason_code"]),
                "details": dict(outcome.get("details", {})),
            }
        return results

    def _evaluate_gate(
        self,
        *,
        gate_name: str,
        contract: dict[str, Any],
        as_of_date: str,
        runtime_policy: dict[str, Any],
    ) -> dict[str, Any]:
        contract_id = str(contract["contract_id"])
        if gate_name == "contract_active_valid":
            lpo_state = str(contract.get("lpo_state") or "ACTIVE").upper()
            passed = lpo_state == "ACTIVE"
            return {
                "status": "PASS" if passed else "FAIL",
                "score": 1.0 if passed else 0.0,
                "reason_code": "active" if passed else f"lpo_state_{lpo_state.lower()}",
                "details": {"lpo_state": lpo_state},
            }
        if gate_name == "quantity_tolerance":
            expected_kg = int(contract.get("expected_total_qty_kg") or 0)
            tolerance_pct = self._resolve_overdelivery_tolerance_policy(
                contract=contract,
                runtime_policy=runtime_policy,
            )
            delivered = self.repo.fetch_one(
                """
                SELECT COALESCE(SUM(delivered_qty_kg), 0) AS delivered_qty_kg
                FROM deliveries
                WHERE contract_id = ? AND status IN ('DELIVERED', 'INVOICED', 'PAID')
                """,
                (contract_id,),
            )
            delivered_kg = int((delivered or {"delivered_qty_kg": 0})["delivered_qty_kg"] or 0)
            allowed_kg = int(expected_kg * (1.0 + tolerance_pct / 100.0))
            passed = expected_kg <= 0 or delivered_kg <= allowed_kg
            return {
                "status": "PASS" if passed else "FAIL",
                "score": 1.0 if passed else 0.0,
                "reason_code": "within_tolerance" if passed else "over_tolerance",
                "details": {
                    "expected_qty_kg": expected_kg,
                    "delivered_qty_kg": delivered_kg,
                    "allowed_qty_kg": allowed_kg,
                    "tolerance_pct": tolerance_pct,
                },
            }
        if gate_name == "evidence_ready":
            row = self.repo.fetch_one(
                "SELECT COUNT(*) AS cnt FROM evidence_originals WHERE contract_id = ?",
                (contract_id,),
            )
            count = int((row or {"cnt": 0})["cnt"] or 0)
            passed = count > 0
            return {
                "status": "PASS" if passed else "FAIL",
                "score": 1.0 if passed else 0.0,
                "reason_code": "evidence_present" if passed else "missing_evidence",
                "details": {"evidence_count": count},
            }
        if gate_name == "coa_complete":
            delivered = self.repo.fetch_one(
                """
                SELECT COUNT(*) AS cnt
                FROM deliveries
                WHERE contract_id = ? AND status IN ('DELIVERED', 'INVOICED', 'PAID')
                """,
                (contract_id,),
            )
            linked = self.repo.fetch_one(
                """
                SELECT COUNT(*) AS cnt
                FROM deliveries d
                WHERE d.contract_id = ? AND d.status IN ('DELIVERED', 'INVOICED', 'PAID')
                  AND EXISTS (
                    SELECT 1 FROM delivery_coa_links l WHERE l.delivery_id = d.delivery_id
                  )
                """,
                (contract_id,),
            )
            delivered_count = int((delivered or {"cnt": 0})["cnt"] or 0)
            linked_count = int((linked or {"cnt": 0})["cnt"] or 0)
            passed = linked_count >= delivered_count
            return {
                "status": "PASS" if passed else "FAIL",
                "score": 1.0 if passed else 0.0,
                "reason_code": "coa_complete" if passed else "coa_missing_rows",
                "details": {"delivered_count": delivered_count, "coa_linked_count": linked_count},
            }
        if gate_name == "payment_match_confidence":
            row = self.repo.fetch_one(
                """
                SELECT COUNT(*) AS cnt
                FROM drep_outstanding_payments
                WHERE contract_id = ? AND outstanding_balance > 0
                """,
                (contract_id,),
            )
            outstanding_count = int((row or {"cnt": 0})["cnt"] or 0)
            thresholds = self._resolve_payment_thresholds(runtime_policy)
            review_min = thresholds["review_min"]
            auto_min = thresholds["auto_min"]
            score = 1.0 if outstanding_count == 0 else 0.8
            if outstanding_count == 0:
                return {
                    "status": "PASS",
                    "score": score,
                    "reason_code": "no_outstanding",
                    "details": {"outstanding_invoice_count": 0, "auto_min": auto_min, "review_min": review_min},
                }
            if score >= auto_min:
                return {
                    "status": "PASS",
                    "score": score,
                    "reason_code": "payment_confident",
                    "details": {"outstanding_invoice_count": outstanding_count, "auto_min": auto_min, "review_min": review_min},
                }
            if score < review_min:
                return {
                    "status": "FAIL",
                    "score": score,
                    "reason_code": "payment_low_confidence",
                    "details": {"outstanding_invoice_count": outstanding_count, "auto_min": auto_min, "review_min": review_min},
                }
            return {
                "status": "WARN",
                "score": score,
                "reason_code": "awaiting_payment_feed",
                "details": {"outstanding_invoice_count": outstanding_count, "auto_min": auto_min, "review_min": review_min},
            }
        if gate_name == "capacity_available":
            rows = self.repo.fetch_all(
                """
                SELECT max_lots, reserved_lots, max_qty_kg, reserved_qty_kg
                FROM capacity_calendar
                WHERE as_of_date = ?
                  AND (buyer_id IS NULL OR buyer_id = ?)
                  AND (processor_id IS NULL OR processor_id = ?)
                """,
                (as_of_date, contract.get("buyer_id"), contract.get("processor_id")),
            )
            if not rows:
                return {
                    "status": "PASS",
                    "score": 1.0,
                    "reason_code": "no_capacity_limit",
                    "details": {"rows": 0},
                }
            blocked = False
            for row in rows:
                if int(row.get("max_lots") or 0) and int(row.get("reserved_lots") or 0) > int(row.get("max_lots") or 0):
                    blocked = True
                if int(row.get("max_qty_kg") or 0) and int(row.get("reserved_qty_kg") or 0) > int(row.get("max_qty_kg") or 0):
                    blocked = True
            return {
                "status": "FAIL" if blocked else "PASS",
                "score": 0.0 if blocked else 1.0,
                "reason_code": "capacity_exceeded" if blocked else "capacity_available",
                "details": {"rows": len(rows)},
            }
        if gate_name == "transport_assignment_valid":
            row = self.repo.fetch_one(
                """
                SELECT COUNT(*) AS cnt
                FROM deliveries
                WHERE contract_id = ?
                  AND status IN ('DISPATCHED', 'DELIVERED', 'INVOICED', 'PAID')
                  AND (COALESCE(TRIM(truck_no), '') = '' OR COALESCE(TRIM(driver_name), '') = '')
                """,
                (contract_id,),
            )
            missing = int((row or {"cnt": 0})["cnt"] or 0)
            status = "PASS" if missing == 0 else "WARN"
            return {
                "status": status,
                "score": 1.0 if status == "PASS" else 0.75,
                "reason_code": "transport_complete" if status == "PASS" else "transport_missing_details",
                "details": {"missing_rows": missing},
            }
        return {
            "status": "WARN",
            "score": 0.5,
            "reason_code": "unknown_gate",
            "details": {"gate_name": gate_name},
        }

    def _resolve_overdelivery_tolerance_policy(
        self,
        *,
        contract: dict[str, Any],
        runtime_policy: dict[str, Any],
    ) -> float:
        policy = runtime_policy.get("policy", {}) if isinstance(runtime_policy.get("policy"), dict) else {}
        over_cfg = policy.get("over_delivery", {}) if isinstance(policy.get("over_delivery"), dict) else {}
        buyer_overrides = over_cfg.get("buyer_overrides", {}) if isinstance(over_cfg.get("buyer_overrides"), dict) else {}
        buyer_id = str(contract.get("buyer_id") or "").strip()
        if buyer_id and buyer_id in buyer_overrides:
            return float(buyer_overrides[buyer_id])
        if over_cfg.get("global_default_tolerance_pct") not in (None, ""):
            return float(over_cfg["global_default_tolerance_pct"])
        return float(contract.get("over_delivery_tolerance_pct") or 5.0)

    def _resolve_payment_thresholds(self, runtime_policy: dict[str, Any]) -> dict[str, float]:
        policy = runtime_policy.get("policy", {}) if isinstance(runtime_policy.get("policy"), dict) else {}
        confidence_cfg = policy.get("confidence", {}) if isinstance(policy.get("confidence"), dict) else {}
        auto_min = float(confidence_cfg.get("payment_auto_apply_min", self.payment_auto_min))
        review_default = self.identity_review_min
        review_min = float(confidence_cfg.get("payment_review_min", confidence_cfg.get("identity_review_min", review_default)))
        return {"auto_min": auto_min, "review_min": review_min}

    def _apply_transport_intelligence(
        self,
        *,
        autonomy_run_id: str,
        action_intent_id: str,
        contract_id: str,
        as_of_date: str,
        processed_rows: list[dict[str, Any]],
    ) -> dict[str, Any]:
        applied = 0
        review_cases = 0
        blocker_cases = 0
        details: list[dict[str, Any]] = []
        for row in processed_rows:
            materialize = row.get("materialize") if isinstance(row, dict) else None
            delivery_id = str((materialize or {}).get("delivery_id") or "").strip()
            planned_delivery_id = str(row.get("planned_delivery_id") or "").strip() or None
            if not delivery_id:
                continue
            outcome = self._resolve_transport_for_delivery(
                autonomy_run_id=autonomy_run_id,
                action_intent_id=action_intent_id,
                contract_id=contract_id,
                delivery_id=delivery_id,
                planned_delivery_id=planned_delivery_id,
                as_of_date=as_of_date,
            )
            details.append(outcome)
            if outcome.get("applied"):
                applied += 1
            if outcome.get("case_severity") == "REVIEW":
                review_cases += 1
            if outcome.get("case_severity") == "BLOCKER":
                blocker_cases += 1
        if blocker_cases > 0:
            return {
                "blocked": True,
                "error": "Transport assignment blocked by conflict/compliance",
                "applied": applied,
                "review_cases": review_cases,
                "blocker_cases": blocker_cases,
                "details": details,
            }
        return {
            "blocked": False,
            "applied": applied,
            "review_cases": review_cases,
            "blocker_cases": blocker_cases,
            "details": details,
        }

    def _resolve_transport_for_delivery(
        self,
        *,
        autonomy_run_id: str,
        action_intent_id: str | None,
        contract_id: str,
        delivery_id: str,
        planned_delivery_id: str | None,
        as_of_date: str,
    ) -> dict[str, Any]:
        delivery = self.repo.fetch_one("SELECT * FROM deliveries WHERE delivery_id = ?", (delivery_id,))
        if not delivery:
            return {"delivery_id": delivery_id, "applied": False, "reason": "delivery_not_found"}
        truck_hint = str(delivery.get("truck_no") or "").strip()
        driver_hint = str(delivery.get("driver_name") or "").strip()
        candidates = self.repo.list_active_transport_assignments(as_of_date=as_of_date)
        if not candidates:
            return {"delivery_id": delivery_id, "applied": False, "reason": "no_active_transport_assignments"}

        scored: list[dict[str, Any]] = []
        for candidate in candidates:
            score, reason_bits = self._score_transport_candidate(
                candidate=candidate,
                truck_hint=truck_hint,
                driver_hint=driver_hint,
            )
            scored.append(
                {
                    "candidate": candidate,
                    "score": score,
                    "reason_bits": reason_bits,
                }
            )
        scored.sort(key=lambda item: item["score"], reverse=True)
        top = scored[0]
        top_score = float(top["score"])
        ambiguous_top = len(scored) > 1 and abs(float(scored[1]["score"]) - top_score) < 0.01

        suggestion_ids: list[str] = []
        with self.repo.transaction() as conn:
            for item in scored[:3]:
                candidate = item["candidate"]
                explanation = {
                    "score": item["score"],
                    "reason_bits": item["reason_bits"],
                    "as_of_date": as_of_date,
                }
                partner_suggestion = self.repo.upsert_delivery_transport_suggestion(
                    conn,
                    delivery_id=delivery_id,
                    planned_delivery_id=planned_delivery_id,
                    entity_type="PARTNER",
                    candidate_entity_id=str(candidate.get("transport_partner_id") or ""),
                    candidate_label=str(candidate.get("partner_name") or ""),
                    confidence=float(item["score"]),
                    explanation=explanation,
                )
                truck_suggestion = self.repo.upsert_delivery_transport_suggestion(
                    conn,
                    delivery_id=delivery_id,
                    planned_delivery_id=planned_delivery_id,
                    entity_type="TRUCK",
                    candidate_entity_id=str(candidate["transport_truck_id"]),
                    candidate_label=str(candidate.get("truck_no") or ""),
                    confidence=float(item["score"]),
                    explanation=explanation,
                )
                driver_suggestion = self.repo.upsert_delivery_transport_suggestion(
                    conn,
                    delivery_id=delivery_id,
                    planned_delivery_id=planned_delivery_id,
                    entity_type="DRIVER",
                    candidate_entity_id=str(candidate["transport_driver_id"]),
                    candidate_label=str(candidate.get("driver_name") or ""),
                    confidence=float(item["score"]),
                    explanation=explanation,
                )
                for sug in (partner_suggestion, truck_suggestion, driver_suggestion):
                    suggestion_id = str(sug.get("suggestion_id") or "")
                    if suggestion_id:
                        suggestion_ids.append(suggestion_id)

        selected = top["candidate"]
        truck_ok, truck_reason = self.repo.transport_compliance_valid(
            entity_type="TRUCK",
            entity_id=str(selected["transport_truck_id"]),
            as_of_date=as_of_date,
        )
        driver_ok, driver_reason = self.repo.transport_compliance_valid(
            entity_type="DRIVER",
            entity_id=str(selected["transport_driver_id"]),
            as_of_date=as_of_date,
        )
        compliance_ok = truck_ok and driver_ok

        if ambiguous_top:
            return self._create_transport_case(
                autonomy_run_id=autonomy_run_id,
                action_intent_id=action_intent_id,
                contract_id=contract_id,
                delivery_id=delivery_id,
                planned_delivery_id=planned_delivery_id,
                as_of_date=as_of_date,
                reason_code="transport_conflict",
                severity="BLOCKER",
                top_score=top_score,
                suggestion_ids=suggestion_ids,
                details={"reason": "multiple_top_candidates"},
            )
        if not compliance_ok:
            return self._create_transport_case(
                autonomy_run_id=autonomy_run_id,
                action_intent_id=action_intent_id,
                contract_id=contract_id,
                delivery_id=delivery_id,
                planned_delivery_id=planned_delivery_id,
                as_of_date=as_of_date,
                reason_code="transport_compliance_expired",
                severity="BLOCKER",
                top_score=top_score,
                suggestion_ids=suggestion_ids,
                details={"truck_reason": truck_reason, "driver_reason": driver_reason},
            )

        if top_score < self.transport_auto_min:
            return self._create_transport_case(
                autonomy_run_id=autonomy_run_id,
                action_intent_id=action_intent_id,
                contract_id=contract_id,
                delivery_id=delivery_id,
                planned_delivery_id=planned_delivery_id,
                as_of_date=as_of_date,
                reason_code="transport_low_confidence",
                severity="REVIEW",
                top_score=top_score,
                suggestion_ids=suggestion_ids,
                details={"threshold": self.transport_auto_min},
            )

        with self.repo.transaction() as conn:
            self.repo.update_delivery_transport_fields(
                conn,
                delivery_id=delivery_id,
                truck_no=str(selected.get("truck_no") or ""),
                driver_name=str(selected.get("driver_name") or ""),
                driver_phone=str(selected.get("driver_phone") or ""),
            )
            snapshot = self.repo.upsert_delivery_transport_snapshot(
                conn,
                delivery_id=delivery_id,
                planned_delivery_id=planned_delivery_id,
                transport_partner_id=str(selected.get("transport_partner_id") or "") or None,
                transport_truck_id=str(selected.get("transport_truck_id") or "") or None,
                transport_driver_id=str(selected.get("transport_driver_id") or "") or None,
                partner_name=str(selected.get("partner_name") or ""),
                truck_no=str(selected.get("truck_no") or ""),
                driver_name=str(selected.get("driver_name") or ""),
                driver_phone=str(selected.get("driver_phone") or ""),
                source_type="autonomy_suggestion",
                source_ref="assignment_history",
                confidence=top_score,
                reason_code="transport_auto_applied",
                payload={
                    "as_of_date": as_of_date,
                    "reason_bits": top.get("reason_bits", []),
                    "suggestion_ids": suggestion_ids,
                },
            )
            self.repo.mark_transport_suggestion_feedback(conn, suggestion_ids=suggestion_ids, accepted=True)
            self.repo.append_event(
                conn,
                entity_type="DELIVERY",
                entity_id=delivery_id,
                event_type="TRANSPORT_SNAPSHOT_APPLIED",
                as_of_date=as_of_date,
                payload={
                    "snapshot_id": snapshot.get("snapshot_id"),
                    "confidence": top_score,
                    "suggestion_ids": suggestion_ids,
                },
                source="run-autonomy",
            )
        return {
            "delivery_id": delivery_id,
            "applied": True,
            "case_severity": None,
            "confidence": top_score,
            "snapshot_reason": "transport_auto_applied",
        }

    def _create_transport_case(
        self,
        *,
        autonomy_run_id: str,
        action_intent_id: str | None,
        contract_id: str,
        delivery_id: str,
        planned_delivery_id: str | None,
        as_of_date: str,
        reason_code: str,
        severity: str,
        top_score: float,
        suggestion_ids: list[str],
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        case_key = f"{delivery_id}|{reason_code}|{as_of_date}"
        payload = {
            "delivery_id": delivery_id,
            "planned_delivery_id": planned_delivery_id,
            "as_of_date": as_of_date,
            "top_score": top_score,
            "suggestion_ids": suggestion_ids,
            **(details or {}),
        }
        with self.repo.transaction() as conn:
            case = self.repo.create_or_get_exception_case(
                conn,
                autonomy_run_id=autonomy_run_id,
                action_intent_id=action_intent_id,
                contract_id=contract_id,
                delivery_id=delivery_id,
                planned_delivery_id=planned_delivery_id,
                case_type="transport_assignment",
                severity=severity,
                reason_code=reason_code,
                details=payload,
                idempotency_key=case_key,
            )
            self.repo.add_decision_feature(
                conn,
                exception_case_id=str(case["exception_case_id"]),
                feature_key="transport_candidates",
                feature_payload=payload,
            )
            self.repo.append_event(
                conn,
                entity_type="EXCEPTION_CASE",
                entity_id=str(case["exception_case_id"]),
                event_type="CASE_OPENED",
                as_of_date=as_of_date,
                payload={"reason_code": reason_code, "delivery_id": delivery_id},
                source="run-autonomy",
            )
        return {
            "delivery_id": delivery_id,
            "applied": False,
            "case_id": case["exception_case_id"],
            "case_severity": severity,
            "reason_code": reason_code,
            "confidence": top_score,
        }

    def _score_transport_candidate(
        self,
        *,
        candidate: dict[str, Any],
        truck_hint: str,
        driver_hint: str,
    ) -> tuple[float, list[str]]:
        reason_bits: list[str] = []
        score = 0.0
        normalized_truck_hint = _norm(truck_hint)
        normalized_driver_hint = _norm(driver_hint)
        truck_no = str(candidate.get("truck_no") or "")
        driver_name = str(candidate.get("driver_name") or "")
        if normalized_truck_hint:
            if normalized_truck_hint == _norm(truck_no):
                score += 0.95
                reason_bits.append("truck_exact_match")
            elif normalized_truck_hint in _norm(truck_no) or _norm(truck_no) in normalized_truck_hint:
                score += 0.7
                reason_bits.append("truck_partial_match")
            aliases = self.repo.list_transport_aliases(entity_type="TRUCK", entity_id=str(candidate["transport_truck_id"]))
            if any(_norm(str(alias.get("normalized_alias") or alias.get("alias_text") or "")) == normalized_truck_hint for alias in aliases):
                score += 0.2
                reason_bits.append("truck_alias_match")
        if normalized_driver_hint:
            if normalized_driver_hint == _norm(driver_name):
                score += 0.95
                reason_bits.append("driver_exact_match")
            elif normalized_driver_hint in _norm(driver_name) or _norm(driver_name) in normalized_driver_hint:
                score += 0.7
                reason_bits.append("driver_partial_match")
            aliases = self.repo.list_transport_aliases(entity_type="DRIVER", entity_id=str(candidate["transport_driver_id"]))
            if any(_norm(str(alias.get("normalized_alias") or alias.get("alias_text") or "")) == normalized_driver_hint for alias in aliases):
                score += 0.2
                reason_bits.append("driver_alias_match")
        if not normalized_truck_hint and not normalized_driver_hint:
            if int(candidate.get("is_primary") or 0) == 1:
                score += 0.95
                reason_bits.append("single_or_primary_assignment")
            else:
                score += 0.75
                reason_bits.append("historical_assignment")
        if not reason_bits:
            reason_bits.append("weak_match")
            score = max(score, 0.4)
        return min(1.0, round(score, 4)), reason_bits

    def _intent_gate_requirements(self, intent_type: str) -> list[str]:
        return {
            "plan_deliveries": ["contract_active_valid", "quantity_tolerance", "capacity_available"],
            "materialize_due": [
                "contract_active_valid",
                "quantity_tolerance",
                "evidence_ready",
                "capacity_available",
                "transport_assignment_valid",
            ],
            "auto_progress": ["contract_active_valid"],
            "generate_pack": ["contract_active_valid", "coa_complete"],
            "apply_payment": ["payment_match_confidence"],
        }.get(intent_type, [])

    def _execute_intent_with_gates(
        self,
        *,
        autonomy_run_id: str,
        contract: dict[str, Any],
        intent_type: str,
        gates: dict[str, dict[str, Any]],
        as_of_date: str,
        dry_run: bool,
        runtime_policy: dict[str, Any],
    ) -> dict[str, Any]:
        contract_id = str(contract["contract_id"])
        policy_version = str(runtime_policy.get("policy_version") or "phase2.v1")
        policy_source_key = str(runtime_policy.get("policy_source_key") or "config")
        selected_policy_set_ids = runtime_policy.get("selected_policy_set_ids", [])
        selected_policy_set_ids = selected_policy_set_ids if isinstance(selected_policy_set_ids, list) else []
        policy_ref = "|".join(selected_policy_set_ids) if selected_policy_set_ids else "-"
        intent_key = f"{contract_id}|{intent_type}|{as_of_date}|{policy_version}|{policy_source_key}|{policy_ref}|dry={1 if dry_run else 0}"
        policy_metadata = {
            "policy_version": policy_version,
            "policy_source_key": policy_source_key,
            "policy_source": str(runtime_policy.get("source") or "config"),
            "selected_policy_set_ids": selected_policy_set_ids,
        }
        with self.repo.transaction() as conn:
            intent_row = self.repo.create_or_get_action_intent(
                conn,
                autonomy_run_id=autonomy_run_id,
                intent_type=intent_type,
                contract_id=contract_id,
                delivery_id=None,
                planned_delivery_id=None,
                as_of_date=as_of_date,
                scheduled_at=None,
                policy_version=policy_version,
                payload={"intent_type": intent_type, "policy": policy_metadata},
                idempotency_key=intent_key,
            )
            self.repo.append_event(
                conn,
                entity_type="ACTION_INTENT",
                entity_id=str(intent_row["action_intent_id"]),
                event_type="INTENT_CREATED",
                as_of_date=as_of_date,
                payload={"intent_type": intent_type, "contract_id": contract_id, "policy": policy_metadata},
                source="run-autonomy",
            )

        required_gates = self._intent_gate_requirements(intent_type)
        blocking_gate = next((gate for gate in required_gates if gates.get(gate, {}).get("status") == "FAIL"), None)
        if blocking_gate:
            case_key = f"{contract_id}|{intent_type}|{blocking_gate}|{as_of_date}"
            with self.repo.transaction() as conn:
                self.repo.set_action_intent_status(
                    conn,
                    action_intent_id=str(intent_row["action_intent_id"]),
                    status="BLOCKED",
                )
                case_row = self.repo.create_or_get_exception_case(
                    conn,
                    autonomy_run_id=autonomy_run_id,
                    action_intent_id=str(intent_row["action_intent_id"]),
                    contract_id=contract_id,
                    delivery_id=None,
                    planned_delivery_id=None,
                    case_type="gate_block",
                    severity="BLOCKER",
                    reason_code="gate_failed",
                    details={
                        "gate_name": blocking_gate,
                        "intent_type": intent_type,
                        "as_of_date": as_of_date,
                    },
                    idempotency_key=case_key,
                )
                self.repo.append_event(
                    conn,
                    entity_type="EXCEPTION_CASE",
                    entity_id=str(case_row["exception_case_id"]),
                    event_type="CASE_OPENED",
                    as_of_date=as_of_date,
                    payload={"gate_name": blocking_gate, "intent_type": intent_type},
                    source="run-autonomy",
                )
                self.repo.add_action_execution(
                    conn,
                    action_intent_id=str(intent_row["action_intent_id"]),
                    status="BLOCKED",
                    idempotency_key=f"{intent_row['action_intent_id']}|blocked|{as_of_date}",
                    request_payload={"intent_type": intent_type, "contract_id": contract_id, "policy": policy_metadata},
                    response_payload={"blocked_by_gate": blocking_gate, "policy": policy_metadata},
                    error_payload={"reason": "Gate failed"},
                )
            return {
                "intent_type": intent_type,
                "status": "BLOCKED",
                "blocked_by_gate": blocking_gate,
                "action_intent_id": intent_row["action_intent_id"],
            }

        request_payload = {
            "contract_id": contract_id,
            "as_of_date": as_of_date,
            "dry_run": dry_run,
            "policy": policy_metadata,
        }
        status = "SUCCESS"
        response_payload: dict[str, Any] = {}
        error_payload: dict[str, Any] | None = None
        try:
            response_payload = self._execute_intent_logic(
                intent_type=intent_type,
                contract_id=contract_id,
                as_of_date=as_of_date,
                dry_run=dry_run,
                autonomy_run_id=autonomy_run_id,
                action_intent_id=str(intent_row["action_intent_id"]),
                runtime_policy=runtime_policy,
            )
            if bool(response_payload.get("blocked")):
                status = "BLOCKED"
            elif str(response_payload.get("status") or "").upper() == "SKIPPED":
                status = "SKIPPED"
            elif response_payload.get("ok") is False:
                status = "FAILED"
        except Exception as error:
            status = "FAILED"
            error_payload = {"message": str(error)}
            response_payload = {"ok": False, "error": str(error)}

        with self.repo.transaction() as conn:
            intent_status = {
                "SUCCESS": "EXECUTED",
                "SKIPPED": "SKIPPED",
                "FAILED": "FAILED",
                "BLOCKED": "BLOCKED",
            }.get(status, "FAILED")
            self.repo.set_action_intent_status(
                conn,
                action_intent_id=str(intent_row["action_intent_id"]),
                status=intent_status,
            )
            self.repo.add_action_execution(
                conn,
                action_intent_id=str(intent_row["action_intent_id"]),
                status=status,
                idempotency_key=f"{intent_row['action_intent_id']}|{status.lower()}|{as_of_date}",
                request_payload=request_payload,
                response_payload=response_payload,
                error_payload=error_payload,
            )
            event_type = "INTENT_EXECUTED" if status in {"SUCCESS", "SKIPPED"} else "INTENT_FAILED"
            self.repo.append_event(
                conn,
                entity_type="ACTION_INTENT",
                entity_id=str(intent_row["action_intent_id"]),
                event_type=event_type,
                as_of_date=as_of_date,
                payload={"intent_type": intent_type, "status": status, "policy": policy_metadata},
                source="run-autonomy",
            )
            if status in {"FAILED", "BLOCKED"}:
                case_key = f"{contract_id}|{intent_type}|{status}|{as_of_date}"
                case_row = self.repo.create_or_get_exception_case(
                    conn,
                    autonomy_run_id=autonomy_run_id,
                    action_intent_id=str(intent_row["action_intent_id"]),
                    contract_id=contract_id,
                    delivery_id=None,
                    planned_delivery_id=None,
                    case_type="intent_failure",
                    severity="BLOCKER" if status == "BLOCKED" else "REVIEW",
                    reason_code="intent_failed",
                    details={
                        "intent_type": intent_type,
                        "status": status,
                        "response": response_payload,
                        "as_of_date": as_of_date,
                    },
                    idempotency_key=case_key,
                )
                self.repo.append_event(
                    conn,
                    entity_type="EXCEPTION_CASE",
                    entity_id=str(case_row["exception_case_id"]),
                    event_type="CASE_OPENED",
                    as_of_date=as_of_date,
                    payload={"intent_type": intent_type, "status": status},
                    source="run-autonomy",
                )

        return {
            "intent_type": intent_type,
            "status": status,
            "response": response_payload,
            "action_intent_id": intent_row["action_intent_id"],
        }

    def _execute_intent_logic(
        self,
        *,
        intent_type: str,
        contract_id: str,
        as_of_date: str,
        dry_run: bool,
        autonomy_run_id: str,
        action_intent_id: str,
        runtime_policy: dict[str, Any],
    ) -> dict[str, Any]:
        if intent_type == "plan_deliveries":
            existing = self.repo.fetch_one(
                "SELECT COUNT(*) AS cnt FROM planned_deliveries WHERE contract_id = ?",
                (contract_id,),
            )
            if int((existing or {"cnt": 0})["cnt"] or 0) > 0:
                return {"ok": True, "status": "SKIPPED", "reason": "already_planned"}
            if dry_run:
                return {"ok": True, "planned_count": 0, "status": "SUCCESS", "dry_run": True}
            policy = runtime_policy.get("policy", {}) if isinstance(runtime_policy.get("policy"), dict) else {}
            planning_cfg = policy.get("planning", {}) if isinstance(policy.get("planning"), dict) else {}
            cadence = str(planning_cfg.get("default_cadence") or "daily").strip().lower()
            if cadence not in {"daily", "manual"}:
                cadence = "daily"
            max_lots_per_day = int(planning_cfg.get("default_max_lots_per_day", 1))
            if max_lots_per_day <= 0:
                max_lots_per_day = 1
            planned = self.phase1.plan_deliveries(
                contract_id=contract_id,
                start_date=as_of_date,
                cadence=cadence,
                max_lots_per_day=max_lots_per_day,
            )
            return {
                "ok": True,
                "planned_count": planned.get("planned_count", 0),
                "status": "SUCCESS",
                "policy": {
                    "policy_version": runtime_policy.get("policy_version"),
                    "policy_source_key": runtime_policy.get("policy_source_key"),
                },
            }
        if intent_type == "materialize_due":
            if dry_run:
                return {"ok": True, "status": "SUCCESS", "processed_count": 0, "dry_run": True}
            result = self.phase1.materialize_due_deliveries(
                contract_id=contract_id,
                as_of_date=as_of_date,
                auto_progress=True,
                auto_record_coa=True,
                auto_generate_pack=False,
                allow_placeholder_tin=True,
            )
            if not result.get("ok", False):
                return {"ok": False, "blocked": True, "status": "FAILED", "error": result.get("error")}
            processed_rows = result.get("processed", [])
            transport = self._apply_transport_intelligence(
                autonomy_run_id=autonomy_run_id,
                action_intent_id=action_intent_id,
                contract_id=contract_id,
                as_of_date=as_of_date,
                processed_rows=processed_rows if isinstance(processed_rows, list) else [],
            )
            if transport.get("blocked"):
                return {
                    "ok": False,
                    "blocked": True,
                    "status": "FAILED",
                    "error": transport.get("error"),
                    "transport": transport,
                }
            doc_completion = self.phase1.document_completion_copilot(
                contract_id=contract_id,
                as_of_date=as_of_date,
                autonomy_run_id=autonomy_run_id,
                action_intent_id=action_intent_id,
                source="run-autonomy",
            )
            if int(doc_completion.get("blocker_cases") or 0) > 0:
                return {
                    "ok": False,
                    "blocked": True,
                    "status": "FAILED",
                    "error": "Document completion blocked by ambiguous/conflicting links",
                    "transport": transport,
                    "document_completion": doc_completion,
                }
            return {
                "ok": True,
                "status": "SUCCESS",
                "processed_count": len(processed_rows) if isinstance(processed_rows, list) else 0,
                "transport": transport,
                "document_completion": doc_completion,
                "policy": {
                    "policy_version": runtime_policy.get("policy_version"),
                    "policy_source_key": runtime_policy.get("policy_source_key"),
                },
            }
        if intent_type == "auto_progress":
            row = self.repo.fetch_one(
                """
                SELECT COUNT(*) AS cnt
                FROM deliveries
                WHERE contract_id = ? AND status IN ('DISPATCHED', 'DELIVERED', 'INVOICED', 'PAID')
                """,
                (contract_id,),
            )
            return {"ok": True, "status": "SUCCESS", "deliveries_progressed": int((row or {"cnt": 0})["cnt"] or 0)}
        if intent_type == "generate_pack":
            delivery_rows = self.repo.fetch_all(
                """
                SELECT d.delivery_id
                FROM deliveries d
                LEFT JOIN documents inv
                  ON inv.delivery_id = d.delivery_id AND inv.doc_type = 'INVOICE' AND inv.status = 'ACTIVE'
                WHERE d.contract_id = ? AND d.status = 'DELIVERED' AND inv.doc_id IS NULL
                ORDER BY d.delivery_date ASC, d.delivery_id ASC
                """,
                (contract_id,),
            )
            if not delivery_rows:
                return {"ok": True, "status": "SKIPPED", "reason": "no_pending_delivered_items"}
            if dry_run:
                return {"ok": True, "status": "SUCCESS", "generated_count": len(delivery_rows), "dry_run": True}
            generated = 0
            for row in delivery_rows:
                self.phase1.generate_pack(
                    delivery_id=str(row["delivery_id"]),
                    allow_placeholder_tin=True,
                    skip_pdf=False,
                    original_docs=[],
                )
                generated += 1
            return {"ok": True, "status": "SUCCESS", "generated_count": generated}
        if intent_type == "apply_payment":
            return {"ok": True, "status": "SKIPPED", "reason": "no_payment_feed"}
        return {"ok": True, "status": "SKIPPED", "reason": "unknown_intent"}

    def _stage_resolve_entities(self, context: dict[str, Any]) -> None:
        payload = context["normalized"]
        run_id = context["run_id"]
        stage = "entity_resolution"

        buyer_input = payload.get("buyer_id") or payload.get("buyer_name")
        vendor_input = payload.get("vendor_of_record_id") or payload.get("vendor_of_record_name")
        source_input = payload.get("source_id") or payload.get("source_name")
        processor_input = payload.get("processor_id") or payload.get("processor_name")

        buyer = self._resolve_entity(buyer_input, role="buyer", required=True)
        vendor = self._resolve_entity(vendor_input, role="vendor_of_record", required=True)
        source = self._resolve_entity(source_input, role="source", required=False)
        processor = self._resolve_entity(processor_input, role="processor", required=False)

        for field_name, result in (
            ("buyer_id", buyer),
            ("vendor_of_record_id", vendor),
            ("source_id", source),
            ("processor_id", processor),
        ):
            self._add_decision(
                run_id=run_id,
                stage=stage,
                field_name=field_name,
                required_flag=(field_name in {"buyer_id", "vendor_of_record_id"}),
                proposed_value=result.value,
                source_type=result.source_type,
                source_ref=result.source_ref,
                confidence=result.confidence,
                decision=result.decision,
                reason_code=result.reason_code,
                rule_path=f"{stage}.{field_name}",
            )
            if result.decision in {"needs_review", "blocked"}:
                severity = "BLOCKER" if field_name in {"buyer_id", "vendor_of_record_id"} else "REVIEW"
                self._add_exception(
                    run_id=run_id,
                    stage=stage,
                    exception_type="identity_resolution",
                    severity=severity,
                    field_name=field_name,
                    proposed_value=result.value or "",
                    reason=f"{field_name} unresolved or low confidence ({result.reason_code})",
                    suggestions=result.suggestions,
                )
                if severity == "BLOCKER":
                    context["critical_blocked"] = True
            payload[field_name] = result.value

        if payload.get("vendor_of_record_id") not in {"guildgate", "ananta_flows"}:
            self._add_exception(
                run_id=run_id,
                stage=stage,
                exception_type="unsupported_vendor_of_record",
                severity="BLOCKER",
                field_name="vendor_of_record_id",
                proposed_value=payload.get("vendor_of_record_id"),
                reason="Phase 1.5 supports lanes A/B only for vendor_of_record",
                suggestions=[],
            )
            context["critical_blocked"] = True

    def _stage_create_contract(self, context: dict[str, Any]) -> None:
        if context["critical_blocked"]:
            return
        payload = context["normalized"]
        run_id = context["run_id"]
        stage = "contract_create"

        lpo_no = str(payload.get("lpo_no") or payload.get("contract_ref") or "").strip()
        allow_no_lpo = bool(payload.get("allow_no_lpo"))
        if not lpo_no and not allow_no_lpo:
            self._add_exception(
                run_id=run_id,
                stage=stage,
                exception_type="missing_lpo",
                severity="BLOCKER",
                field_name="lpo_no",
                proposed_value="",
                reason="Missing LPO/contract reference and allow_no_lpo is false",
                suggestions=[],
            )
            context["critical_blocked"] = True
            return
        if not lpo_no and allow_no_lpo:
            lpo_no = f"AUTO-NOLPO-{new_ulid()[:8]}"

        issue_date = str(payload.get("issue_date") or payload.get("invoice_date") or "2026-02-25").strip()
        due_date = str(payload.get("due_date") or "").strip() or None
        unit = str(payload.get("unit") or "kgs").strip().lower()
        unit_price_basis = str(payload.get("unit_price_basis") or ("MT" if unit in {"mt", "ton", "tons", "tonne", "tonnes"} else "KG")).strip().upper()
        expected_qty_raw = float(payload.get("expected_qty") or payload.get("quantity") or 0.0)
        if expected_qty_raw <= 0:
            expected_qty_raw = 30000.0 if unit in {"kg", "kgs", "kilogram", "kilograms"} else 30.0
        if unit in {"mt", "ton", "tons", "tonne", "tonnes"}:
            expected_qty_kg = mt_to_kg_int(expected_qty_raw)
        else:
            expected_qty_kg = int(round(expected_qty_raw))
        unit_price = float(payload.get("unit_price") or 0.0)
        if unit_price <= 0:
            unit_price = 2270.0
        product_code = str(payload.get("product_code") or "").strip().upper()
        if not product_code:
            self._add_exception(
                run_id=run_id,
                stage=stage,
                exception_type="missing_product_code",
                severity="BLOCKER",
                field_name="product_code",
                proposed_value="",
                reason="Product code required for contract lines and COA profile",
                suggestions=[],
            )
            context["critical_blocked"] = True
            return
        description = str(payload.get("description") or f"Supply of {product_code} linked to {lpo_no}").strip()
        tolerance_pct = self._resolve_tolerance(payload)

        contract_payload = {
            "contract_ref": lpo_no,
            "lpo_no": lpo_no,
            "lpo_date": payload.get("lpo_date"),
            "buyer_id": payload.get("buyer_id"),
            "vendor_of_record_id": payload.get("vendor_of_record_id"),
            "operator_id": self.config.system_profile.operator_entity_id or "guildgate",
            "source_id": payload.get("source_id"),
            "processor_id": payload.get("processor_id"),
            "currency": payload.get("currency") or "NGN",
            "issue_date": issue_date,
            "due_date": due_date,
            "due_terms": payload.get("due_terms") or "14 days",
            "expected_total_qty": round(expected_qty_kg / 1000.0, 3),
            "expected_total_qty_kg": expected_qty_kg,
            "expected_total_value": round(expected_qty_kg * unit_price, 2),
            "over_delivery_tolerance_pct": tolerance_pct,
            "unit_price_basis": unit_price_basis,
            "notes": payload.get("notes"),
            "lines": [
                {
                    "product_code": product_code,
                    "description": description,
                    "expected_qty": expected_qty_kg,
                    "unit": "kgs",
                    "unit_price": unit_price,
                    "unit_price_basis": unit_price_basis,
                }
            ],
        }
        context["results"]["contract_payload"] = contract_payload
        if context["dry_run"]:
            context["results"]["contract"] = {"contract_id": f"DRY-CONTRACT-{new_ulid()[:8]}", "lpo_no": lpo_no}
            self._add_decision(
                run_id=run_id,
                stage=stage,
                field_name="contract_id",
                required_flag=True,
                proposed_value=context["results"]["contract"]["contract_id"],
                source_type="rule",
                source_ref="dry_run",
                confidence=1.0,
                decision="auto_applied",
                reason_code="dry_run_simulated",
                rule_path=f"{stage}.create_contract",
            )
            return

        result = self.phase1.create_contract(contract_payload, allow_placeholder_tin=bool(payload.get("allow_placeholder_tin", True)))
        context["results"]["contract"] = result
        self._add_decision(
            run_id=run_id,
            stage=stage,
            field_name="contract_id",
            required_flag=True,
            proposed_value=result["contract_id"],
            source_type="phase1_service",
            source_ref="create_contract",
            confidence=1.0,
            decision="auto_applied",
            reason_code="created",
            rule_path=f"{stage}.create_contract",
        )

    def _stage_intake_evidence(self, context: dict[str, Any]) -> None:
        payload = context["normalized"]
        run_id = context["run_id"]
        stage = "evidence_intake"
        files = payload.get("evidence_files") or []
        if not isinstance(files, list):
            files = []
        evidence_rows: list[dict[str, Any]] = []
        seen_hashes: set[str] = set()
        for raw in files:
            path = Path(str(raw)).expanduser()
            if not path.is_absolute():
                path = (self.config.root_dir / path).resolve()
            if not path.exists():
                self._add_exception(
                    run_id=run_id,
                    stage=stage,
                    exception_type="missing_evidence_file",
                    severity="REVIEW",
                    field_name="evidence_files",
                    proposed_value=str(raw),
                    reason="Evidence file path not found",
                    suggestions=[],
                )
                continue
            file_hash = sha256_file(path)
            duplicate = file_hash in seen_hashes or bool(
                self.repo.fetch_one("SELECT evidence_id FROM evidence_originals WHERE sha256 = ? LIMIT 1", (file_hash,))
            )
            seen_hashes.add(file_hash)
            doc_type = self._classify_evidence(path.name)
            decision = "needs_review" if duplicate else "auto_applied"
            reason_code = "duplicate_hash" if duplicate else "new_evidence"
            self._add_decision(
                run_id=run_id,
                stage=stage,
                field_name=f"evidence:{path.name}",
                required_flag=False,
                proposed_value={"path": str(path), "hash": file_hash, "doc_type": doc_type},
                source_type="file_upload",
                source_ref=str(path),
                confidence=1.0,
                decision=decision,
                reason_code=reason_code,
                rule_path=f"{stage}.classify_hash_dedupe",
            )
            if duplicate:
                evidence_rows.append(
                    {
                        "path": str(path),
                        "hash": file_hash,
                        "doc_type": doc_type,
                        "duplicate": True,
                    }
                )
                continue
            saved = {"path": str(path), "hash": file_hash, "doc_type": doc_type, "duplicate": False}
            if not context["dry_run"] and context["results"].get("contract"):
                saved_row = self.phase1.capture_evidence_original(
                    contract_id=context["results"]["contract"]["contract_id"],
                    source_path=path,
                )
                saved["stored_path"] = saved_row["stored_path"]
            evidence_rows.append(saved)
        context["results"]["evidence"] = evidence_rows

    def _stage_delivery_plan(self, context: dict[str, Any]) -> None:
        if context["critical_blocked"]:
            return
        run_id = context["run_id"]
        stage = "delivery_plan"
        contract = context["results"].get("contract")
        if not contract:
            return
        payload = context["normalized"]
        try:
            if context["dry_run"]:
                preview_lot_kg = 30000
                planned = {
                    "ok": True,
                    "contract_id": contract["contract_id"],
                    "planned_count": 1,
                    "planned_total_kg": preview_lot_kg,
                    "planned_total_mt": "30.000",
                    "planned_deliveries": [
                        {
                            "planned_delivery_id": f"DRY-PLAN-{new_ulid()[:8]}",
                            "contract_line_id": "DRY-LINE-1",
                            "sequence_no": 1,
                            "planned_qty_kg": preview_lot_kg,
                            "planned_qty_mt": "30.000",
                            "lot_size_kg": preview_lot_kg,
                            "lot_size_mt": "30.000",
                            "planned_date": str(payload.get("delivery_date") or payload.get("issue_date") or "2026-02-23"),
                        }
                    ],
                }
            else:
                planned = self.phase1.plan_deliveries(
                    contract_id=str(contract["contract_id"]),
                    start_date=str(payload.get("delivery_start_date") or payload.get("delivery_date") or payload.get("issue_date") or ""),
                    cadence=str(payload.get("cadence") or "daily"),
                    max_lots_per_day=int(payload.get("max_lots_per_day") or 1),
                )
            context["results"]["delivery_plan"] = planned
            self._add_decision(
                run_id=run_id,
                stage=stage,
                field_name="planned_count",
                required_flag=True,
                proposed_value=planned.get("planned_count"),
                source_type="phase1_service" if not context["dry_run"] else "rule",
                source_ref="plan_deliveries",
                confidence=1.0,
                decision="auto_applied",
                reason_code="planned",
                rule_path=f"{stage}.plan_deliveries",
            )
        except Exception as error:
            self._add_exception(
                run_id=run_id,
                stage=stage,
                exception_type="delivery_plan_blocked",
                severity="BLOCKER",
                field_name="contract_id",
                proposed_value=str(contract["contract_id"]),
                reason=str(error),
                suggestions=[],
            )
            context["critical_blocked"] = True

    def _stage_delivery_materialization(self, context: dict[str, Any]) -> None:
        if context["critical_blocked"]:
            return
        run_id = context["run_id"]
        stage = "delivery_materialization"
        plan = context["results"].get("delivery_plan") or {}
        planned_rows = plan.get("planned_deliveries") if isinstance(plan, dict) else None
        if not isinstance(planned_rows, list) or not planned_rows:
            return
        payload = context["normalized"]
        evidence = context["results"].get("evidence") or []
        evidence_types = {str(row.get("doc_type") or "") for row in evidence if isinstance(row, dict)}
        manual_override = bool(payload.get("materialize_override"))
        has_required_evidence = ("lpo" in evidence_types) and bool({"waybill", "supplier_invoice"} & evidence_types)
        if not (manual_override or has_required_evidence):
            self._add_exception(
                run_id=run_id,
                stage=stage,
                exception_type="materialization_readiness_missing_evidence",
                severity="BLOCKER",
                field_name="evidence_files",
                proposed_value=list(evidence_types),
                reason="Materialize requires LPO evidence plus waybill or supplier invoice evidence, unless materialize_override=true",
                suggestions=[],
            )
            context["critical_blocked"] = True
            return

        as_of_date = str(context.get("as_of_date") or "").strip()
        due_rows = [
            row
            for row in planned_rows
            if str(row.get("planned_date") or "").strip() and str(row.get("planned_date")) <= as_of_date
        ]
        if not due_rows:
            self._add_exception(
                run_id=run_id,
                stage=stage,
                exception_type="no_due_planned_deliveries",
                severity="REVIEW",
                field_name="planned_date",
                proposed_value=as_of_date,
                reason="No planned deliveries are due for as_of_date",
                suggestions=[],
            )
            return

        materialized: list[dict[str, Any]] = []
        run_override_base = str(payload.get("run_id") or "").strip()
        batch_override_base = str(payload.get("batch_id") or "").strip()
        multi_row = len(due_rows) > 1
        for index, row in enumerate(due_rows, start=1):
            planned_delivery_id = str(row.get("planned_delivery_id") or "").strip()
            if not planned_delivery_id:
                self._add_exception(
                    run_id=run_id,
                    stage=stage,
                    exception_type="materialization_missing_plan_id",
                    severity="BLOCKER",
                    field_name="planned_delivery_id",
                    proposed_value="",
                    reason="Planned row missing planned_delivery_id",
                    suggestions=[],
                )
                context["critical_blocked"] = True
                break
            try:
                run_override = run_override_base or None
                batch_override = batch_override_base or None
                if multi_row and run_override:
                    run_override = f"{run_override}-{index:02d}"
                if multi_row and batch_override:
                    batch_override = f"{batch_override}-{index:02d}"
                if context["dry_run"]:
                    delivery = {
                        "ok": True,
                        "delivery_id": f"DRY-DELIVERY-{new_ulid()[:8]}",
                        "status": "PLANNED",
                        "planned_delivery_id": planned_delivery_id,
                    }
                else:
                    delivery = self.phase1.materialize_delivery(
                        planned_delivery_id=planned_delivery_id,
                        run_id=run_override,
                        batch_id=batch_override,
                        as_of_date=as_of_date,
                    )
                    self.phase1.mark_dispatched(str(delivery["delivery_id"]))
                    self.phase1.mark_delivered(str(delivery["delivery_id"]))
                materialized.append(delivery)
            except Exception as error:
                self._add_exception(
                    run_id=run_id,
                    stage=stage,
                    exception_type="materialization_blocked",
                    severity="BLOCKER",
                    field_name="planned_delivery_id",
                    proposed_value=planned_delivery_id,
                    reason=str(error),
                    suggestions=[],
                )
                context["critical_blocked"] = True
                break

        if not materialized:
            return
        context["results"]["deliveries_materialized"] = materialized
        context["results"]["delivery"] = materialized[0]
        self._add_decision(
            run_id=run_id,
            stage=stage,
            field_name="deliveries_materialized_count",
            required_flag=True,
            proposed_value=len(materialized),
            source_type="phase1_service" if not context["dry_run"] else "rule",
            source_ref="materialize_delivery",
            confidence=1.0,
            decision="auto_applied",
            reason_code="materialized_due_deliveries",
            rule_path=f"{stage}.materialize_all_due",
        )

    def _stage_create_delivery(self, context: dict[str, Any]) -> None:
        if context["critical_blocked"]:
            return
        run_id = context["run_id"]
        stage = "delivery_create"
        payload = context["normalized"]
        contract = context["results"].get("contract")
        if not contract:
            return

        delivery_date = str(payload.get("delivery_date") or payload.get("issue_date") or "2026-02-25").strip()
        unit = str(payload.get("unit") or "kgs").strip().lower()
        delivered_qty_raw = float(payload.get("delivered_qty") or payload.get("quantity") or payload.get("expected_qty") or 0.0)
        if delivered_qty_raw <= 0:
            delivered_qty_raw = 30000.0 if unit in {"kg", "kgs", "kilogram", "kilograms"} else 30.0
        if unit in {"mt", "ton", "tons", "tonne", "tonnes"}:
            delivered_qty_kg = mt_to_kg_int(delivered_qty_raw)
        else:
            delivered_qty_kg = int(round(delivered_qty_raw))

        run_id_value = str(payload.get("run_id") or "").strip() or f"RUN-{delivery_date.replace('-', '')}-01"
        batch_id_value = str(payload.get("batch_id") or "").strip()
        if not batch_id_value:
            vendor_id = payload.get("vendor_of_record_id") or "vendor"
            vendor_code = (self.config.registry.get(vendor_id).code or "VENDOR").upper()
            product_code = str(payload.get("product_code") or "PRODUCT").upper()
            batch_id_value = f"{vendor_code}-{product_code}-{delivery_date.replace('-', '')}-01"

        delivery_payload = {
            "contract_id": contract["contract_id"],
            "line_no": 1,
            "delivery_ref": str(payload.get("delivery_ref") or f"AUTO-DLV-{new_ulid()[:8]}"),
            "run_id": run_id_value,
            "batch_id": batch_id_value,
            "delivery_date": delivery_date,
            "delivered_qty": delivered_qty_kg,
            "unit": "kgs",
            "unit_price": float(payload.get("unit_price") or 2270.0),
            "unit_price_basis": str(payload.get("unit_price_basis") or "KG").strip().upper(),
            "truck_no": str(payload.get("truck_no") or ""),
            "driver_name": str(payload.get("driver_name") or ""),
            "driver_phone": str(payload.get("driver_phone") or ""),
            "notes": str(payload.get("notes") or ""),
        }
        context["results"]["delivery_payload"] = delivery_payload
        if context["dry_run"]:
            delivery = {"delivery_id": f"DRY-DELIVERY-{new_ulid()[:8]}", "status": "PLANNED"}
            context["results"]["delivery"] = delivery
            self._add_decision(
                run_id=run_id,
                stage=stage,
                field_name="delivery_id",
                required_flag=True,
                proposed_value=delivery["delivery_id"],
                source_type="rule",
                source_ref="dry_run",
                confidence=1.0,
                decision="auto_applied",
                reason_code="dry_run_simulated",
                rule_path=f"{stage}.add_delivery",
            )
            return
        delivery = self.phase1.add_delivery(delivery_payload)
        self.phase1.mark_dispatched(str(delivery["delivery_id"]))
        self.phase1.mark_delivered(str(delivery["delivery_id"]))
        context["results"]["delivery"] = delivery
        self._add_decision(
            run_id=run_id,
            stage=stage,
            field_name="delivery_id",
            required_flag=True,
            proposed_value=delivery["delivery_id"],
            source_type="phase1_service",
            source_ref="add_delivery",
            confidence=1.0,
            decision="auto_applied",
            reason_code="created_and_advanced",
            rule_path=f"{stage}.add_delivery",
        )

    def _stage_record_coa(self, context: dict[str, Any]) -> None:
        if context["critical_blocked"]:
            return
        run_id = context["run_id"]
        stage = "coa"
        payload = context["normalized"]
        deliveries = context["results"].get("deliveries_materialized")
        if not isinstance(deliveries, list) or not deliveries:
            delivery = context["results"].get("delivery")
            deliveries = [delivery] if delivery else []
        if not deliveries:
            return
        provided = payload.get("coa_results")
        if isinstance(provided, list) and provided:
            source_type = "provided"
            reason_code = "provided_results"
        else:
            self._add_exception(
                run_id=run_id,
                stage=stage,
                exception_type="missing_coa_results",
                severity="BLOCKER",
                field_name="coa_results",
                proposed_value="",
                reason="Missing required COA rows for active profile",
                suggestions=[],
            )
            context["critical_blocked"] = True
            return

        coa_records: list[dict[str, Any]] = []
        for delivery in deliveries:
            if not delivery:
                continue
            delivery_id = str(delivery["delivery_id"])
            if context["dry_run"]:
                coa_records.append(
                    {
                        "coa_record_id": f"DRY-COA-{new_ulid()[:8]}",
                        "linked_delivery_id": delivery_id,
                    }
                )
                continue
            try:
                result = self.phase1.record_coa({"delivery_id": delivery_id, "results": provided})
            except Exception as error:
                self._add_exception(
                    run_id=run_id,
                    stage=stage,
                    exception_type="coa_validation_failed",
                    severity="BLOCKER",
                    field_name="coa_results",
                    proposed_value=provided,
                    reason=str(error),
                    suggestions=[],
                )
                context["critical_blocked"] = True
                return
            coa_records.append(result)
        if not coa_records:
            return
        context["results"]["coa_records"] = coa_records
        context["results"]["coa"] = coa_records[0]
        self._add_decision(
            run_id=run_id,
            stage=stage,
            field_name="coa_records_count",
            required_flag=True,
            proposed_value=len(coa_records),
            source_type=source_type if not context["dry_run"] else "rule",
            source_ref="record_coa",
            confidence=1.0,
            decision="auto_applied",
            reason_code=reason_code,
            rule_path=f"{stage}.record_coa_multi",
        )

    def _stage_generate_pack(self, context: dict[str, Any]) -> None:
        if context["critical_blocked"]:
            return
        run_id = context["run_id"]
        stage = "generate_pack"
        deliveries = context["results"].get("deliveries_materialized")
        if not isinstance(deliveries, list) or not deliveries:
            delivery = context["results"].get("delivery")
            deliveries = [delivery] if delivery else []
        if not deliveries:
            return
        original_docs = [row["path"] for row in context["results"].get("evidence", []) if row.get("doc_type") in {"waybill", "weighing", "coa", "supplier_invoice", "lpo"}]
        packs: list[dict[str, Any]] = []
        for delivery in deliveries:
            if not delivery:
                continue
            delivery_id = str(delivery["delivery_id"])
            if context["dry_run"]:
                packs.append(
                    {
                        "delivery_id": delivery_id,
                        "sales_transaction_id": f"DRY-SALES-{new_ulid()[:8]}",
                        "invoice_no": f"INV-DRY-{new_ulid()[:4]}",
                        "output_dir": str(self.config.output_v2_dir / "DRY"),
                    }
                )
                continue
            try:
                result = self.phase1.generate_pack(
                    delivery_id=delivery_id,
                    allow_placeholder_tin=bool(context["normalized"].get("allow_placeholder_tin", True)),
                    skip_pdf=bool(context["normalized"].get("skip_pdf", False)),
                    original_docs=original_docs,
                )
            except Exception as error:
                self._add_exception(
                    run_id=run_id,
                    stage=stage,
                    exception_type="pack_generation_blocked",
                    severity="BLOCKER",
                    field_name="delivery_id",
                    proposed_value=delivery_id,
                    reason=str(error),
                    suggestions=[],
                )
                context["critical_blocked"] = True
                break
            packs.append(result)

        if not packs:
            return
        context["results"]["packs"] = packs
        context["results"]["pack"] = packs[0]
        self._add_decision(
            run_id=run_id,
            stage=stage,
            field_name="packs_generated_count",
            required_flag=True,
            proposed_value=len(packs),
            source_type="phase1_service" if not context["dry_run"] else "rule",
            source_ref="generate_pack",
            confidence=1.0,
            decision="auto_applied",
            reason_code="generated_multi",
            rule_path=f"{stage}.generate_pack_multi",
        )

    def _stage_payment(self, context: dict[str, Any]) -> None:
        run_id = context["run_id"]
        stage = "payment"
        payload = context["normalized"]
        payment_payload = payload.get("payment") or {}
        if not isinstance(payment_payload, dict) or not payment_payload:
            return
        pack = context["results"].get("pack")
        if not pack:
            self._add_exception(
                run_id=run_id,
                stage=stage,
                exception_type="missing_invoice_for_payment",
                severity="REVIEW",
                field_name="payment",
                proposed_value=payment_payload,
                reason="Payment provided but no generated invoice/sales transaction",
                suggestions=[],
            )
            return

        match = self._resolve_payment_match(payment_payload, fallback_sales_id=str(pack.get("sales_transaction_id") or ""))
        self._add_decision(
            run_id=run_id,
            stage=stage,
            field_name="payment_allocation.sales_transaction_id",
            required_flag=False,
            proposed_value=match.get("sales_transaction_id"),
            source_type=match.get("source_type", "rule"),
            source_ref=match.get("source_ref", ""),
            confidence=float(match.get("confidence", 0.0)),
            decision=str(match.get("decision", "needs_review")),
            reason_code=str(match.get("reason_code", "")),
            rule_path=f"{stage}.match_payment",
        )

        if match["decision"] != "auto_applied":
            self._add_exception(
                run_id=run_id,
                stage=stage,
                exception_type="ambiguous_payment_allocation",
                severity="REVIEW",
                field_name="payment_allocation.sales_transaction_id",
                proposed_value=payment_payload,
                reason="Ambiguous payment allocation; manual resolution required",
                suggestions=match.get("suggestions", []),
            )
            return

        if context["dry_run"]:
            context["results"]["payment"] = {"status": "DRY_RUN", "sales_transaction_id": match["sales_transaction_id"]}
            return

        amount_received = float(payment_payload.get("amount_received") or match.get("amount_due") or 0.0)
        external_reference = str(payment_payload.get("external_reference") or f"AUTO-{new_ulid()[:8]}")
        mark_payload: dict[str, Any] = {
            "payment_date": str(payment_payload.get("payment_date") or context["normalized"].get("issue_date") or "2026-02-25"),
            "payment_method": str(payment_payload.get("payment_method") or "Bank Transfer"),
            "external_reference": external_reference,
            "idempotency_key": external_reference,
            "amount_received": amount_received,
            "allocations": [
                {
                    "sales_transaction_id": match["sales_transaction_id"],
                    "allocated_amount": amount_received,
                    "notes": "Automation allocation",
                }
            ],
        }
        withheld = payment_payload.get("certified_withholding")
        if isinstance(withheld, dict) and float(withheld.get("amount") or 0.0) > 0:
            mark_payload["certified_withholding_events"] = [
                {
                    "sales_transaction_id": match["sales_transaction_id"],
                    "withholder_party_id": str(withheld.get("withholder_party_id") or ""),
                    "withholding_type": str(withheld.get("withholding_type") or "WHT"),
                    "amount": float(withheld.get("amount") or 0.0),
                    "certificate_ref": str(withheld.get("certificate_ref") or ""),
                    "certified_at": str(withheld.get("certified_at") or ""),
                    "evidence_path": withheld.get("evidence_path"),
                    "evidence_hash": withheld.get("evidence_hash"),
                }
            ]
        result = self.phase1.mark_paid(
            mark_payload,
            allow_placeholder_tin=bool(context["normalized"].get("allow_placeholder_tin", True)),
            skip_pdf=bool(context["normalized"].get("skip_pdf", False)),
        )
        context["results"]["payment"] = result

    def _stage_export(self, context: dict[str, Any]) -> None:
        as_of = str(context["as_of_date"])
        if context["dry_run"]:
            context["results"]["export"] = {"status": "DRY_RUN", "as_of_date": as_of}
            return
        out_dir = self.config.state_dir / "automation" / "runs" / context["run_id"] / "exports"
        result = self.phase1.export_drep(as_of_date=as_of, out_dir=out_dir)
        context["results"]["export"] = result

    def _resolve_tolerance(self, payload: dict[str, Any]) -> float:
        if payload.get("over_delivery_tolerance_pct") not in (None, ""):
            return float(payload.get("over_delivery_tolerance_pct"))
        over_cfg = self.config.automation_thresholds.get("over_delivery", {}) if isinstance(self.config.automation_thresholds, dict) else {}
        buyer_overrides = over_cfg.get("buyer_overrides", {}) if isinstance(over_cfg.get("buyer_overrides"), dict) else {}
        buyer_id = str(payload.get("buyer_id") or "")
        if buyer_id in buyer_overrides:
            return float(buyer_overrides[buyer_id])
        return float(over_cfg.get("global_default_tolerance_pct", 5.0))

    def _resolve_payment_match(self, payment: dict[str, Any], *, fallback_sales_id: str) -> dict[str, Any]:
        by_sales_id = str(payment.get("sales_transaction_id") or "").strip()
        by_invoice = str(payment.get("invoice_no") or "").strip()
        if by_sales_id:
            sale = self.repo.fetch_one("SELECT * FROM drep_sales WHERE sales_transaction_id = ?", (by_sales_id,))
            if sale:
                return {
                    "sales_transaction_id": sale["sales_transaction_id"],
                    "amount_due": sale["outstanding_balance"],
                    "confidence": 1.0,
                    "decision": "auto_applied",
                    "reason_code": "explicit_sales_transaction_id",
                    "source_type": "input",
                    "source_ref": "payment.sales_transaction_id",
                    "suggestions": [],
                }
        if by_invoice:
            sale = self.repo.fetch_one("SELECT * FROM drep_sales WHERE invoice_no = ?", (by_invoice,))
            if sale:
                return {
                    "sales_transaction_id": sale["sales_transaction_id"],
                    "amount_due": sale["outstanding_balance"],
                    "confidence": 0.98,
                    "decision": "auto_applied",
                    "reason_code": "explicit_invoice_no",
                    "source_type": "input",
                    "source_ref": "payment.invoice_no",
                    "suggestions": [],
                }
        amount = float(payment.get("amount_received") or 0.0)
        if amount > 0:
            matches = self.repo.fetch_all(
                "SELECT * FROM drep_sales WHERE ABS(outstanding_balance - ?) <= 0.5 AND outstanding_balance > 0",
                (amount,),
            )
            if len(matches) == 1:
                return {
                    "sales_transaction_id": matches[0]["sales_transaction_id"],
                    "amount_due": matches[0]["outstanding_balance"],
                    "confidence": 0.95,
                    "decision": "auto_applied",
                    "reason_code": "single_amount_match",
                    "source_type": "heuristic",
                    "source_ref": "amount_exact_match",
                    "suggestions": [],
                }
            if len(matches) > 1:
                return {
                    "sales_transaction_id": None,
                    "amount_due": amount,
                    "confidence": 0.6,
                    "decision": "needs_review",
                    "reason_code": "ambiguous_amount_match",
                    "source_type": "heuristic",
                    "source_ref": "amount_exact_match",
                    "suggestions": [
                        {
                            "sales_transaction_id": row["sales_transaction_id"],
                            "invoice_no": row["invoice_no"],
                            "outstanding": row["outstanding_balance"],
                        }
                        for row in matches[:5]
                    ],
                }
        if fallback_sales_id:
            sale = self.repo.fetch_one("SELECT * FROM drep_sales WHERE sales_transaction_id = ?", (fallback_sales_id,))
            if sale:
                return {
                    "sales_transaction_id": fallback_sales_id,
                    "amount_due": sale["outstanding_balance"],
                    "confidence": 0.9,
                    "decision": "auto_applied",
                    "reason_code": "fallback_latest_pack",
                    "source_type": "pipeline",
                    "source_ref": "generate_pack.sales_transaction_id",
                    "suggestions": [],
                }
        return {
            "sales_transaction_id": None,
            "amount_due": amount,
            "confidence": 0.0,
            "decision": "blocked",
            "reason_code": "no_payment_match",
            "source_type": "heuristic",
            "source_ref": "",
            "suggestions": [],
        }

    def _resolve_entity(self, raw_value: Any, *, role: str, required: bool) -> ResolutionResult:
        if raw_value is None or str(raw_value).strip() == "":
            return ResolutionResult(
                value=None,
                confidence=0.0,
                decision="blocked" if required else "needs_review",
                reason_code="missing_input",
                source_type="input",
                source_ref="",
                suggestions=[],
            )

        value = str(raw_value).strip()
        if value in self.config.registry.entities:
            return ResolutionResult(
                value=value,
                confidence=1.0,
                decision="auto_applied",
                reason_code="explicit_id",
                source_type="input",
                source_ref=value,
                suggestions=[],
            )

        resolved = self.config.registry.resolve_id(value)
        if resolved:
            confidence = 0.98
            return ResolutionResult(
                value=resolved,
                confidence=confidence,
                decision="auto_applied" if confidence >= self.identity_auto_min else "needs_review",
                reason_code="alias_match",
                source_type="registry_alias",
                source_ref=value,
                suggestions=[],
            )

        target_prefix = {
            "buyer": "buyer_",
            "vendor_of_record": "",
            "source": "",
            "processor": "processor_",
        }.get(role, "")

        suggestions = self._fuzzy_entity_suggestions(value=value, target_prefix=target_prefix, role=role)
        if suggestions and suggestions[0]["confidence"] >= self.identity_auto_min:
            top = suggestions[0]
            return ResolutionResult(
                value=str(top["entity_id"]),
                confidence=float(top["confidence"]),
                decision="auto_applied",
                reason_code="fuzzy_high_confidence",
                source_type="fuzzy",
                source_ref=value,
                suggestions=suggestions[:3],
            )
        if suggestions and suggestions[0]["confidence"] >= self.identity_review_min:
            top = suggestions[0]
            return ResolutionResult(
                value=str(top["entity_id"]),
                confidence=float(top["confidence"]),
                decision="needs_review",
                reason_code="fuzzy_needs_review",
                source_type="fuzzy",
                source_ref=value,
                suggestions=suggestions[:3],
            )
        return ResolutionResult(
            value=None,
            confidence=suggestions[0]["confidence"] if suggestions else 0.0,
            decision="blocked" if required else "needs_review",
            reason_code="unresolved",
            source_type="fuzzy",
            source_ref=value,
            suggestions=suggestions[:3],
        )

    def _fuzzy_entity_suggestions(self, *, value: str, target_prefix: str, role: str) -> list[dict[str, Any]]:
        norm_value = _norm(value)
        suggestions: list[dict[str, Any]] = []
        for entity_id, entity in self.config.registry.entities.items():
            if role == "vendor_of_record" and entity_id not in {"guildgate", "ananta_flows"}:
                continue
            if target_prefix and not entity_id.startswith(target_prefix):
                continue
            names = [entity.name, *list(entity.aliases or []), entity_id]
            confidence = max((_sim(norm_value, _norm(candidate)) for candidate in names if candidate), default=0.0)
            suggestions.append(
                {
                    "entity_id": entity_id,
                    "name": entity.name,
                    "confidence": round(confidence, 4),
                }
            )
        suggestions.sort(key=lambda item: item["confidence"], reverse=True)
        return suggestions

    def _classify_evidence(self, filename: str) -> str:
        name = filename.lower()
        if "waybill" in name or name.startswith("wb"):
            return "waybill"
        if "weigh" in name or name.startswith("wt"):
            return "weighing"
        if "coa" in name or "certificate" in name:
            return "coa"
        if "invoice" in name or name.startswith("inv"):
            return "supplier_invoice"
        if "lpo" in name or "po" in name:
            return "lpo"
        return "other"

    def _normalize_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        normalized = json.loads(json.dumps(payload))
        if "buyer" in normalized and isinstance(normalized["buyer"], dict):
            normalized.setdefault("buyer_id", normalized["buyer"].get("entity_id"))
            normalized.setdefault("buyer_name", normalized["buyer"].get("name"))
        if "vendor_of_record" in normalized and isinstance(normalized["vendor_of_record"], dict):
            normalized.setdefault("vendor_of_record_id", normalized["vendor_of_record"].get("entity_id"))
            normalized.setdefault("vendor_of_record_name", normalized["vendor_of_record"].get("name"))
        if "source" in normalized and isinstance(normalized["source"], dict):
            normalized.setdefault("source_id", normalized["source"].get("entity_id"))
            normalized.setdefault("source_name", normalized["source"].get("name"))
        if "processor" in normalized and isinstance(normalized["processor"], dict):
            normalized.setdefault("processor_id", normalized["processor"].get("entity_id"))
            normalized.setdefault("processor_name", normalized["processor"].get("name"))
        return normalized

    def _apply_resolutions(self, payload: dict[str, Any], run_id: str) -> dict[str, Any]:
        resolved = self.repo.fetch_all(
            "SELECT field_name, resolved_value FROM exception_queue WHERE run_id = ? AND status = 'RESOLVED'",
            (run_id,),
        )
        out = dict(payload)
        for row in resolved:
            field = str(row.get("field_name") or "").strip()
            if not field:
                continue
            value = row.get("resolved_value")
            if isinstance(value, str):
                stripped = value.strip()
                if stripped and stripped[0] in "{[\"-0123456789tfn":
                    try:
                        value = json.loads(stripped)
                    except Exception:
                        value = stripped
            out[field] = value
        return out

    def _add_decision(
        self,
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
    ) -> None:
        with self.repo.transaction() as conn:
            self.repo.add_automation_decision(
                conn,
                run_id=run_id,
                stage=stage,
                field_name=field_name,
                required_flag=required_flag,
                proposed_value=proposed_value,
                source_type=source_type,
                source_ref=source_ref,
                confidence=confidence,
                decision=decision,
                reason_code=reason_code,
                rule_path=rule_path,
            )

    def _add_exception(
        self,
        *,
        run_id: str,
        stage: str,
        exception_type: str,
        severity: str,
        field_name: str,
        proposed_value: Any,
        reason: str,
        suggestions: list[dict[str, Any]],
    ) -> None:
        with self.repo.transaction() as conn:
            self.repo.add_exception(
                conn,
                run_id=run_id,
                stage=stage,
                exception_type=exception_type,
                severity=severity,
                field_name=field_name,
                proposed_value=proposed_value,
                reason=reason,
                suggestions=suggestions,
            )

    def _has_open_blockers(self, run_id: str) -> bool:
        row = self.repo.fetch_one(
            "SELECT COUNT(*) AS cnt FROM exception_queue WHERE run_id = ? AND status = 'OPEN' AND severity = 'BLOCKER'",
            (run_id,),
        )
        return bool(row and int(row["cnt"]) > 0)

    def _build_metrics(self, *, run_id: str, started_epoch: float, status: str) -> dict[str, Any]:
        decisions = self.repo.fetch_all(
            "SELECT required_flag, decision FROM automation_decisions WHERE run_id = ?",
            (run_id,),
        )
        total_required = sum(1 for row in decisions if int(row["required_flag"]) == 1)
        auto_required = sum(1 for row in decisions if int(row["required_flag"]) == 1 and row["decision"] == "auto_applied")
        open_exceptions = self.repo.fetch_all(
            "SELECT exception_type, severity FROM exception_queue WHERE run_id = ? AND status = 'OPEN'",
            (run_id,),
        )
        blocker_rows = self.repo.fetch_all(
            "SELECT exception_type, reason FROM exception_queue WHERE run_id = ? AND status = 'OPEN' AND severity = 'BLOCKER' ORDER BY created_at ASC LIMIT 5",
            (run_id,),
        )
        exception_count_by_type: dict[str, int] = {}
        for row in open_exceptions:
            exception_count_by_type[row["exception_type"]] = exception_count_by_type.get(row["exception_type"], 0) + 1

        manual_interventions_count = len(open_exceptions)
        duration = max(0.0, time.time() - started_epoch)
        runs = self.repo.fetch_all("SELECT status, metrics_json FROM automation_runs")
        completed_without_manual = 0
        total_runs = len(runs)
        for run in runs:
            if run.get("status") != "COMPLETED":
                continue
            metrics_json = json.loads(run.get("metrics_json") or "{}")
            if int(metrics_json.get("manual_interventions_count", 0)) == 0:
                completed_without_manual += 1
        if status == "COMPLETED" and manual_interventions_count == 0:
            completed_without_manual += 1
        stp_rate = (completed_without_manual / total_runs) if total_runs else 0.0

        auto_population_rate = (auto_required / total_required) if total_required else 0.0
        return {
            "stp_rate": round(stp_rate, 4),
            "auto_population_rate": round(auto_population_rate, 4),
            "manual_interventions_count": manual_interventions_count,
            "exception_count_by_type": exception_count_by_type,
            "end_to_end_duration_seconds": round(duration, 3),
            "kpi_gate_complete_evidence_pass": manual_interventions_count <= 1 and auto_population_rate >= 0.9 and status == "COMPLETED",
            "kpi_gate_partial_evidence_pass": manual_interventions_count <= 3,
            "status": status,
            "failure_reason": "Open blocker exceptions" if status != "COMPLETED" and self._has_open_blockers(run_id) else "",
            "top_blockers": [f"{row['exception_type']}: {row['reason']}" for row in blocker_rows],
        }


def _norm(value: str) -> str:
    return "".join(ch.lower() for ch in str(value) if ch.isalnum())


def _sim(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()
