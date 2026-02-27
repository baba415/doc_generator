from __future__ import annotations

import difflib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from adapters.sqlite_repo import SQLiteRepo
from core.config import RuntimeConfig
from core.hashing import canonical_json_sha256, sha256_file
from core.ids import new_ulid
from core.time import utc_now_iso_z
from core.units import mt_to_kg_int
from domain.services import Phase1Service


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

    def auto_run(self, payload: dict[str, Any], *, as_of_date: str, dry_run: bool = False, resume_from_run_id: str | None = None) -> dict[str, Any]:
        self.phase1.init_db()
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
