from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from pypdf import PdfMerger

from adapters.csv_export import export_drep_views
from adapters.pdf_bridge import PdfBridge
from adapters.sqlite_repo import SQLiteRepo
from adapters.storage import output_delivery_dir, persist_evidence_original
from core.config import RuntimeConfig, infer_buyer_group
from core.enums import DeliveryStatus, DocumentType, PaymentStatus
from core.hashing import canonical_json_sha256, sha256_file
from core.ids import new_ulid
from core.manifest import build_manifest, write_manifest
from core.time import utc_now_iso_z, utc_today_iso
from core.units import kg_to_mt_decimal, mt_to_kg_int
from domain.validators import ensure_vendor_tin

_DOC_TYPE_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("WAYBILL", ("waybill", "wb-", "wb_", " wb ")),
    ("WEIGHING_TICKET", ("weigh", "wt-", "wt_", "ticket")),
    ("COA", ("coa", "certificate", "analysis")),
    ("SUPPLIER_INVOICE", ("supplier invoice", "supplier-invoice", "supplier_invoice", "invoice", "inv-")),
    ("RECEIPT", ("receipt", "rcpt-")),
    ("LPO", ("lpo", "purchase order", "po-")),
)


def _doc_type_from_filename(filename: str) -> str:
    lowered = str(filename or "").strip().lower()
    for doc_type, hints in _DOC_TYPE_HINTS:
        if any(hint in lowered for hint in hints):
            return doc_type
    return "OTHER"


def _norm_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


class Phase1Service:
    def __init__(self, config: RuntimeConfig, repo: SQLiteRepo) -> None:
        self.config = config
        self.repo = repo
        self.pdf_bridge = PdfBridge(config)

    def init_db(self) -> dict[str, Any]:
        return self.repo.init_db(self.config)

    def create_contract(self, payload: dict[str, Any], *, allow_placeholder_tin: bool) -> dict[str, Any]:
        return self.repo.create_contract(self.config, payload, allow_placeholder_tin=allow_placeholder_tin)

    def add_delivery(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.repo.add_delivery(payload)

    def plan_deliveries(
        self,
        *,
        contract_id: str,
        start_date: str | None = None,
        cadence: str = "daily",
        max_lots_per_day: int | None = None,
    ) -> dict[str, Any]:
        cadence_norm = (cadence or "daily").strip().lower()
        if cadence_norm not in {"daily", "manual"}:
            raise ValueError("cadence must be daily or manual")
        fallback_policy = self.config.delivery_policies or {}
        fallback_version = str(
            fallback_policy.get("policy_version")
            or fallback_policy.get("schema_version")
            or "phase1_6.v1"
        )
        start_value = (start_date or "").strip()
        with self.repo.transaction() as conn:
            contract = conn.execute("SELECT * FROM contracts WHERE contract_id = ?", (contract_id,)).fetchone()
            if not contract:
                raise ValueError(f"Unknown contract_id: {contract_id}")
            contract = dict(contract)
            if str(contract.get("lpo_state") or "ACTIVE").upper() in {"CANCELLED", "EXPIRED"}:
                raise ValueError("Cannot plan deliveries for CANCELLED/EXPIRED contract")
            start_iso = start_value or str(contract.get("lpo_valid_from") or contract.get("issue_date") or utc_today_iso())
            runtime_policy = self.repo.resolve_policy_runtime(
                as_of_date=start_iso,
                contract_id=contract_id,
                master_contract_id=str(contract.get("master_contract_id") or "").strip() or None,
                buyer_id=str(contract.get("buyer_id") or "").strip() or None,
                fallback_policy=fallback_policy,
                fallback_version=fallback_version,
            )
            policy = runtime_policy.get("policy", {})
            policy = policy if isinstance(policy, dict) else {}
            planning_cfg = policy.get("planning", {}) if isinstance(policy.get("planning"), dict) else {}
            include_weekends = bool(planning_cfg.get("include_weekends", True))
            if max_lots_per_day is None:
                max_lots_per_day = int(planning_cfg.get("default_max_lots_per_day", 1))
            if max_lots_per_day <= 0:
                raise ValueError("max_lots_per_day must be >= 1")
            policy_version = str(runtime_policy.get("policy_version") or fallback_version)
            policy_source_key = str(runtime_policy.get("policy_source_key") or f"config:{fallback_version}")
            idempotency_key = f"{contract_id}|{start_iso}|{cadence_norm}|{max_lots_per_day}|{policy_version}|{policy_source_key}"
            existing = self.repo.find_idempotent_response(conn, command_name="plan-deliveries", idempotency_key=idempotency_key)
            if existing:
                return existing
            lines = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM contract_line_items WHERE contract_id = ? ORDER BY line_no",
                    (contract_id,),
                ).fetchall()
            ]
            if not lines:
                raise ValueError("No contract lines to plan")
            start_dt = date.fromisoformat(start_iso)
            valid_to_iso = str(contract.get("lpo_valid_to") or "").strip() or None
            valid_to_dt = date.fromisoformat(valid_to_iso) if valid_to_iso else None
            products_cfg = policy.get("products", {}) if isinstance(policy.get("products"), dict) else {}
            now = utc_now_iso_z()
            rows_out: list[dict[str, Any]] = []
            total_planned_kg = 0
            common_case_line_count = 0
            for line in lines:
                product_code = str(line.get("product_code") or "").upper()
                product_policy = products_cfg.get(product_code) or {}
                lot_mt = product_policy.get("default_lot_mt")
                if lot_mt in (None, ""):
                    raise ValueError(f"No delivery lot policy configured for product_code={product_code}")
                lot_size_kg = mt_to_kg_int(lot_mt)
                expected_qty_kg = int(line.get("expected_qty_kg") or 0)
                if expected_qty_kg <= 0:
                    expected_qty_kg = mt_to_kg_int(line.get("expected_qty") or 0)
                if expected_qty_kg <= 0:
                    raise ValueError(f"expected_qty must be > 0 for contract_line_id={line['contract_line_id']}")
                full_lots = expected_qty_kg // lot_size_kg
                remainder_kg = expected_qty_kg % lot_size_kg
                lot_list = [lot_size_kg] * int(full_lots)
                if remainder_kg > 0:
                    lot_list.append(int(remainder_kg))
                if not lot_list:
                    raise ValueError(f"No lots produced for contract_line_id={line['contract_line_id']}")
                if remainder_kg == 0:
                    common_case_line_count += 1
                sequence_start = conn.execute(
                    "SELECT COALESCE(MAX(sequence_no), 0) AS max_seq FROM planned_deliveries WHERE contract_line_id = ?",
                    (line["contract_line_id"],),
                ).fetchone()
                seq = int(sequence_start["max_seq"] or 0)
                cursor_date = start_dt
                lots_used_today = 0
                for idx, qty_kg in enumerate(lot_list, start=1):
                    if cadence_norm == "daily":
                        while not include_weekends and cursor_date.weekday() >= 5:
                            cursor_date = cursor_date + timedelta(days=1)
                        if lots_used_today >= max_lots_per_day:
                            cursor_date = cursor_date + timedelta(days=1)
                            lots_used_today = 0
                            while not include_weekends and cursor_date.weekday() >= 5:
                                cursor_date = cursor_date + timedelta(days=1)
                    else:
                        cursor_date = start_dt
                    if valid_to_dt and cursor_date > valid_to_dt:
                        raise ValueError(
                            f"Planning window too short: cannot fit all lots by lpo_valid_to={valid_to_dt.isoformat()} "
                            f"with max_lots_per_day={max_lots_per_day}"
                        )
                    seq += 1
                    planned_delivery_id = new_ulid()
                    conn.execute(
                        """
                        INSERT INTO planned_deliveries(
                            planned_delivery_id, contract_id, contract_line_id, sequence_no, planned_qty_kg,
                            lot_size_kg, planned_date, status, notes, created_at, updated_at
                        )
                        VALUES(?, ?, ?, ?, ?, ?, ?, 'PLANNED', ?, ?, ?)
                        """,
                        (
                            planned_delivery_id,
                            contract_id,
                            line["contract_line_id"],
                            seq,
                            int(qty_kg),
                            int(lot_size_kg),
                            cursor_date.isoformat(),
                            f"auto_plan policy={policy_version} lot_index={idx}",
                            now,
                            now,
                        ),
                    )
                    rows_out.append(
                        {
                            "planned_delivery_id": planned_delivery_id,
                            "contract_line_id": line["contract_line_id"],
                            "sequence_no": seq,
                            "planned_qty_kg": int(qty_kg),
                            "planned_qty_mt": format(kg_to_mt_decimal(int(qty_kg)), "f"),
                            "lot_size_kg": int(lot_size_kg),
                            "lot_size_mt": format(kg_to_mt_decimal(int(lot_size_kg)), "f"),
                            "planned_date": cursor_date.isoformat(),
                        }
                    )
                    total_planned_kg += int(qty_kg)
                    lots_used_today += 1
            tolerance_pct = self._resolve_planning_tolerance_pct(contract=contract, runtime_policy=policy)
            expected_contract_kg = 0
            for line in lines:
                expected_contract_kg += int(line.get("expected_qty_kg") or 0)
            allowed_kg = int(expected_contract_kg * (1.0 + tolerance_pct / 100.0))
            if expected_contract_kg > 0 and total_planned_kg > allowed_kg:
                raise ValueError(
                    f"Planned quantity {total_planned_kg}kg exceeds expected {expected_contract_kg}kg + tolerance {tolerance_pct:.2f}%"
                )
            response = {
                "ok": True,
                "contract_id": contract_id,
                "policy_version": policy_version,
                "policy_source": str(runtime_policy.get("source") or "config"),
                "policy_source_key": policy_source_key,
                "policy_set_ids": [row.get("policy_set_id") for row in runtime_policy.get("selected_sets", []) if row.get("policy_set_id")],
                "cadence": cadence_norm,
                "start_date": start_iso,
                "max_lots_per_day": max_lots_per_day,
                "planned_count": len(rows_out),
                "planned_total_kg": total_planned_kg,
                "planned_total_mt": format(kg_to_mt_decimal(total_planned_kg), "f"),
                "common_case_eligible": bool(len(lines) > 0 and common_case_line_count == len(lines)),
                "zero_edit_common_case": bool(len(lines) > 0 and common_case_line_count == len(lines)),
                "planned_deliveries": rows_out,
            }
            self.repo.save_idempotent_response(
                conn,
                command_name="plan-deliveries",
                idempotency_key=idempotency_key,
                response=response,
            )
            return response

    def _resolve_planning_tolerance_pct(
        self,
        *,
        contract: dict[str, Any],
        runtime_policy: dict[str, Any],
    ) -> float:
        planning_cfg = runtime_policy.get("planning", {}) if isinstance(runtime_policy.get("planning"), dict) else {}
        if planning_cfg.get("tolerance_pct") not in (None, ""):
            return float(planning_cfg["tolerance_pct"])
        over_cfg = runtime_policy.get("over_delivery", {}) if isinstance(runtime_policy.get("over_delivery"), dict) else {}
        buyer_overrides = over_cfg.get("buyer_overrides", {}) if isinstance(over_cfg.get("buyer_overrides"), dict) else {}
        buyer_id = str(contract.get("buyer_id") or "").strip()
        if buyer_id and buyer_id in buyer_overrides:
            return float(buyer_overrides[buyer_id])
        if over_cfg.get("global_default_tolerance_pct") not in (None, ""):
            return float(over_cfg["global_default_tolerance_pct"])
        return float(contract.get("over_delivery_tolerance_pct") or 5.0)

    def materialize_delivery(
        self,
        *,
        planned_delivery_id: str,
        run_id: str | None = None,
        batch_id: str | None = None,
        qty_mt: float | None = None,
        as_of_date: str | None = None,
    ) -> dict[str, Any]:
        refresh_date = str(as_of_date or utc_today_iso()).strip()
        self.refresh_contract_state(as_of_date=refresh_date)
        idempotency_key = str(planned_delivery_id)
        with self.repo.transaction() as conn:
            existing = self.repo.find_idempotent_response(
                conn,
                command_name="materialize-delivery",
                idempotency_key=idempotency_key,
            )
            if existing:
                return existing
            planned = conn.execute(
                """
                SELECT pd.*, c.vendor_of_record_id, c.lpo_state, cli.line_no, cli.product_code, cli.unit_price, cli.unit, cli.unit_price_basis
                FROM planned_deliveries pd
                JOIN contracts c ON c.contract_id = pd.contract_id
                JOIN contract_line_items cli ON cli.contract_line_id = pd.contract_line_id
                WHERE pd.planned_delivery_id = ?
                """,
                (planned_delivery_id,),
            ).fetchone()
            if not planned:
                raise ValueError(f"Unknown planned_delivery_id: {planned_delivery_id}")
            planned = dict(planned)
            if str(planned.get("lpo_state") or "").upper() != "ACTIVE":
                raise ValueError("Materialization blocked: contract LPO state must be ACTIVE")
            if str(planned.get("status") or "").upper() not in {"PLANNED", "SCHEDULED"}:
                raise ValueError("Materialization blocked: planned delivery status must be PLANNED or SCHEDULED")

            quantity_kg = int(planned["planned_qty_kg"])
            if qty_mt is not None:
                quantity_kg = mt_to_kg_int(qty_mt)
            delivery_date = str(planned["planned_date"])
            vendor = self.config.registry.get(str(planned["vendor_of_record_id"]))
            vendor_code = (vendor.code or "VENDOR").upper()
            contract_token = str(planned["contract_id"]).replace("-", "")[:8].upper()
            run_id_value = (run_id or str(planned.get("run_id") or "")).strip() or (
                f"RUN-{contract_token}-{delivery_date.replace('-', '')}-{planned['sequence_no']:02d}"
            )
            batch_id_value = (batch_id or str(planned.get("batch_id") or "")).strip() or (
                f"{vendor_code}-{str(planned['product_code']).upper()}-{contract_token}-{delivery_date.replace('-', '')}-{planned['sequence_no']:02d}"
            )
            delivery_payload = {
                "contract_id": planned["contract_id"],
                "line_no": int(planned["line_no"]),
                "planned_delivery_id": planned_delivery_id,
                "delivery_ref": f"PLN-{planned_delivery_id[:10]}",
                "run_id": run_id_value,
                "batch_id": batch_id_value,
                "delivery_date": delivery_date,
                "delivered_qty": float(quantity_kg),
                "unit": "kgs",
                "unit_price": float(planned["unit_price"]),
                "unit_price_basis": str(planned.get("unit_price_basis") or "KG"),
                "notes": f"materialized_from_planned:{planned_delivery_id}",
            }
        result = self.add_delivery(delivery_payload)
        with self.repo.transaction() as conn:
            now = utc_now_iso_z()
            conn.execute(
                """
                UPDATE planned_deliveries
                SET delivery_id = ?, run_id = ?, batch_id = ?, materialized_qty_kg = ?, delivered_qty_kg = ?,
                    status = 'SCHEDULED', updated_at = ?
                WHERE planned_delivery_id = ?
                """,
                (result["delivery_id"], run_id_value, batch_id_value, quantity_kg, quantity_kg, now, planned_delivery_id),
            )
            response = {
                "ok": True,
                "planned_delivery_id": planned_delivery_id,
                "delivery_id": result["delivery_id"],
                "contract_id": result["contract_id"],
                "status": result["status"],
                "materialized_qty_kg": quantity_kg,
                "materialized_qty_mt": format(kg_to_mt_decimal(quantity_kg), "f"),
            }
            self.repo.save_idempotent_response(
                conn,
                command_name="materialize-delivery",
                idempotency_key=idempotency_key,
                response=response,
            )
            return response

    def cancel_contract(self, *, contract_id: str, reason: str) -> dict[str, Any]:
        return self.repo.cancel_contract(contract_id=contract_id, reason=reason)

    def close_contract(self, *, contract_id: str, reason: str) -> dict[str, Any]:
        return self.repo.close_contract(contract_id=contract_id, reason=reason)

    def refresh_contract_state(self, *, as_of_date: str) -> dict[str, Any]:
        return self.repo.refresh_lpo_states(as_of_date=as_of_date)

    def mark_dispatched(self, delivery_id: str) -> dict[str, Any]:
        return self.repo.mark_delivery_status(delivery_id, DeliveryStatus.DISPATCHED)

    def mark_delivered(self, delivery_id: str) -> dict[str, Any]:
        return self.repo.mark_delivery_status(delivery_id, DeliveryStatus.DELIVERED)

    def record_coa(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.repo.upsert_coa_result(self.config, payload)

    def generate_pack(
        self,
        *,
        delivery_id: str,
        allow_placeholder_tin: bool,
        skip_pdf: bool,
        original_docs: list[str] | None = None,
    ) -> dict[str, Any]:
        original_docs = original_docs or []
        idempotency_key = delivery_id
        now = utc_now_iso_z()
        with self.repo.transaction() as conn:
            existing = self.repo.find_idempotent_response(
                conn, command_name="generate-pack", idempotency_key=idempotency_key
            )
            if existing:
                return existing

            bundle = conn.execute(
                """
                SELECT
                  d.*,
                  c.contract_ref, c.lpo_no, c.lpo_date, c.issue_date, c.due_date, c.due_terms, c.currency, c.lane,
                  c.buyer_id, c.vendor_of_record_id, c.operator_id, c.source_id, c.processor_id, c.contract_id,
                  c.expected_total_qty, c.expected_total_value
                FROM deliveries d
                JOIN contracts c ON c.contract_id = d.contract_id
                WHERE d.delivery_id = ?
                """,
                (delivery_id,),
            ).fetchone()
            if not bundle:
                raise ValueError(f"Unknown delivery_id: {delivery_id}")
            bundle = dict(bundle)

            if bundle["status"] != DeliveryStatus.DELIVERED.value:
                raise ValueError("Invoice attempt before delivery status is DELIVERED")

            vendor = self.config.registry.get(str(bundle["vendor_of_record_id"]))
            ensure_vendor_tin(vendor.tin, allow_placeholder_tin=allow_placeholder_tin)
            if not str(bundle.get("run_id") or "").strip():
                raise ValueError("Missing run_id")
            if not str(bundle.get("batch_id") or "").strip():
                raise ValueError("Missing batch_id")

            coa_record = conn.execute(
                """
                SELECT cr.*
                FROM delivery_coa_links dcl
                JOIN coa_results cr ON cr.coa_record_id = dcl.coa_record_id
                WHERE dcl.delivery_id = ?
                ORDER BY cr.updated_at DESC
                LIMIT 1
                """,
                (delivery_id,),
            ).fetchone()
            if not coa_record:
                raise ValueError("Missing COA record for delivery")
            coa_record = dict(coa_record)

            contract_line = conn.execute(
                "SELECT * FROM contract_line_items WHERE contract_line_id = ?",
                (bundle["contract_line_id"],),
            ).fetchone()
            if not contract_line:
                raise ValueError("Missing contract line item for delivery")
            contract_line = dict(contract_line)

            invoice_year = int(str(bundle["delivery_date"]).split("-")[0])
            invoice_seq = self.repo.next_sequence(
                conn,
                vendor_of_record_id=str(bundle["vendor_of_record_id"]),
                doc_type=DocumentType.INVOICE.value,
                year=invoice_year,
            )
            invoice_no = self.repo.format_doc_number(
                DocumentType.INVOICE,
                invoice_no="",
                year=invoice_year,
                sequence=invoice_seq,
            )
            waybill_no = self.repo.format_doc_number(
                DocumentType.WAYBILL,
                invoice_no=invoice_no,
                year=invoice_year,
                sequence=1,
            )
            weighing_no = self.repo.format_doc_number(
                DocumentType.WEIGHING_TICKET,
                invoice_no=invoice_no,
                year=invoice_year,
                sequence=1,
            )
            coa_no = str(coa_record.get("coa_no") or "")
            if not coa_no:
                coa_seq = self.repo.next_sequence(
                    conn,
                    vendor_of_record_id=str(bundle["vendor_of_record_id"]),
                    doc_type=DocumentType.COA.value,
                    year=invoice_year,
                )
                coa_no = self.repo.format_doc_number(
                    DocumentType.COA,
                    invoice_no=invoice_no,
                    year=invoice_year,
                    sequence=coa_seq,
                )
                conn.execute("UPDATE coa_results SET coa_no = ?, updated_at = ? WHERE coa_record_id = ?", (coa_no, now, coa_record["coa_record_id"]))

            snapshot_id = self.repo.create_parties_snapshot(
                conn,
                buyer_id=str(bundle["buyer_id"]),
                vendor_id=str(bundle["vendor_of_record_id"]),
                operator_id=str(bundle["operator_id"]),
            )

            sales_transaction_id = new_ulid()
            gross_amount = float(bundle["gross_amount"])
            expected_wht_amount = 0.0
            conn.execute(
                """
                INSERT INTO sales_transactions(
                    sales_transaction_id, contract_id, delivery_id, snapshot_id, vendor_of_record_id, buyer_id,
                    operator_id, lane, invoice_no, invoice_date, due_date, currency, gross_amount, amount_due,
                    expected_wht_amount, created_at, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sales_transaction_id,
                    bundle["contract_id"],
                    delivery_id,
                    snapshot_id,
                    bundle["vendor_of_record_id"],
                    bundle["buyer_id"],
                    bundle["operator_id"],
                    bundle["lane"],
                    invoice_no,
                    bundle["delivery_date"],
                    bundle.get("due_date") or bundle["delivery_date"],
                    bundle["currency"],
                    gross_amount,
                    gross_amount,
                    expected_wht_amount,
                    now,
                    now,
                ),
            )

            sales_line_id = new_ulid()
            conn.execute(
                """
                INSERT INTO sales_lines(
                    sales_line_id, sales_transaction_id, delivery_id, contract_line_id, line_no, product_code,
                    description, quantity, quantity_kg, unit, unit_price, unit_price_basis, gross_amount, created_at, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sales_line_id,
                    sales_transaction_id,
                    delivery_id,
                    bundle["contract_line_id"],
                    1,
                    contract_line["product_code"],
                    contract_line["description"],
                    bundle["delivered_qty"],
                    int(bundle.get("delivered_qty_kg") or 0),
                    bundle["unit"],
                    bundle["unit_price"],
                    str(bundle.get("unit_price_basis") or "KG"),
                    gross_amount,
                    now,
                    now,
                ),
            )

            self.repo.ensure_delivery_marked_invoiced(conn, delivery_id)

            sales_transaction = conn.execute(
                "SELECT * FROM sales_transactions WHERE sales_transaction_id = ?",
                (sales_transaction_id,),
            ).fetchone()
            sales_transaction = dict(sales_transaction) if sales_transaction else {}
            sales_lines = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM sales_lines WHERE sales_transaction_id = ? ORDER BY line_no",
                    (sales_transaction_id,),
                ).fetchall()
            ]

            doc_numbers = {
                DocumentType.WAYBILL: waybill_no,
                DocumentType.WEIGHING_TICKET: weighing_no,
                DocumentType.COA: coa_no,
                DocumentType.INVOICE: invoice_no,
            }

            rendered_docs = self.pdf_bridge.render_pack_documents(
                bundle=bundle,
                sales_transaction=sales_transaction,
                sales_lines=sales_lines,
                coa_record=coa_record,
                doc_numbers=doc_numbers,
                skip_pdf=skip_pdf,
            )

            vendor_code = (vendor.code or "VENDOR").upper()
            output_dir = output_delivery_dir(
                self.config.output_v2_dir,
                vendor_code=vendor_code,
                invoice_no=invoice_no,
            )

            evidence_records: list[dict[str, Any]] = []
            for index, raw_path in enumerate(original_docs, start=1):
                source = Path(raw_path).expanduser()
                if not source.is_absolute():
                    source = (self.config.root_dir / source).resolve()
                if not source.exists():
                    raise FileNotFoundError(f"Original evidence file not found: {raw_path}")
                stored = persist_evidence_original(
                    source_path=source,
                    dest_dir=output_dir / "evidence" / "originals",
                    order_index=index,
                )
                evidence_records.append(stored)
                file_name = Path(stored["source_path"]).name
                doc_type = _doc_type_from_filename(file_name).lower()
                conn.execute(
                    """
                    INSERT INTO evidence_originals(
                        evidence_id, contract_id, delivery_id, sales_transaction_id, sales_line_id,
                        file_name, doc_type, link_status, link_confidence, link_reason_code, link_source, linked_at,
                        source_path, stored_path, sha256, captured_at, created_at, updated_at
                    )
                    VALUES(?, ?, ?, ?, NULL, ?, ?, 'UNLINKED', NULL, NULL, NULL, NULL, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        stored["evidence_id"],
                        bundle["contract_id"],
                        delivery_id,
                        sales_transaction_id,
                        file_name,
                        doc_type,
                        stored["source_path"],
                        stored["stored_path"],
                        stored["sha256"],
                        stored["captured_at"],
                        now,
                        now,
                    ),
                )

            persisted_docs: list[dict[str, Any]] = []
            for rendered in rendered_docs:
                doc_id = new_ulid()
                filename = rendered["filename"]
                pdf_path = output_dir / filename
                pdf_sha256 = rendered["pdf_sha256"]
                if rendered["pdf_bytes"] is not None:
                    pdf_path.write_bytes(rendered["pdf_bytes"])
                    pdf_sha256 = sha256_file(pdf_path)
                else:
                    pdf_path = Path("")

                conn.execute(
                    """
                    INSERT INTO documents(
                        doc_id, delivery_id, sales_transaction_id, snapshot_id, vendor_of_record_id, doc_type, doc_number,
                        revision_no, status, pdf_path, pdf_sha256, content_sha256, generated_at, created_at, updated_at
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, 1, 'ACTIVE', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        doc_id,
                        delivery_id,
                        sales_transaction_id,
                        snapshot_id,
                        bundle["vendor_of_record_id"],
                        rendered["doc_type"],
                        rendered["doc_number"],
                        str(pdf_path) if str(pdf_path) else None,
                        pdf_sha256,
                        rendered["content_sha256"],
                        now,
                        now,
                        now,
                    ),
                )
                self.repo.link_document_to_sales_lines(
                    conn,
                    doc_id=doc_id,
                    sales_transaction_id=sales_transaction_id,
                    delivery_id=delivery_id,
                )
                persisted_docs.append(
                    {
                        "doc_id": doc_id,
                        "doc_type": rendered["doc_type"],
                        "doc_number": rendered["doc_number"],
                        "filename": filename,
                        "pdf_path": str(pdf_path) if str(pdf_path) else None,
                        "pdf_sha256": pdf_sha256,
                        "content_sha256": rendered["content_sha256"],
                    }
                )

            combined_pack_path: str | None = None
            combined_pack_sha256: str | None = None
            if not skip_pdf:
                ordered_doc_paths: list[Path] = []
                for doc in persisted_docs:
                    path_str = str(doc.get("pdf_path") or "").strip()
                    if not path_str:
                        continue
                    path = Path(path_str)
                    if path.exists():
                        ordered_doc_paths.append(path)
                if ordered_doc_paths:
                    combined = output_dir / f"PACK-{invoice_no}.pdf"
                    merger = PdfMerger()
                    try:
                        for path in ordered_doc_paths:
                            merger.append(str(path))
                        merger.write(str(combined))
                    finally:
                        merger.close()
                    combined_pack_path = str(combined)
                    combined_pack_sha256 = sha256_file(combined)

            manifest_payload = build_manifest(
                delivery_id=delivery_id,
                sales_transaction_id=sales_transaction_id,
                lpo_no=str(bundle["lpo_no"]),
                invoice_no=invoice_no,
                run_id=str(bundle["run_id"]),
                batch_id=str(bundle["batch_id"]),
                buyer_id=str(bundle["buyer_id"]),
                vendor_of_record_id=str(bundle["vendor_of_record_id"]),
                docs=persisted_docs,
            )
            manifest_path = output_dir / "manifest.json"
            write_manifest(manifest_path, manifest_payload)
            manifest_hash = sha256_file(manifest_path)

            self.repo.refresh_contract_status(conn, str(bundle["contract_id"]))
            response = {
                "ok": True,
                "delivery_id": delivery_id,
                "sales_transaction_id": sales_transaction_id,
                "invoice_no": invoice_no,
                "output_dir": str(output_dir),
                "manifest_path": str(manifest_path),
                "manifest_sha256": manifest_hash,
                "combined_pack_pdf_path": combined_pack_path,
                "combined_pack_pdf_sha256": combined_pack_sha256,
                "documents": persisted_docs,
                "evidence_originals": evidence_records,
            }
            self.repo.save_idempotent_response(
                conn,
                command_name="generate-pack",
                idempotency_key=idempotency_key,
                response=response,
            )
            return response

    def mark_paid(
        self,
        payload: dict[str, Any],
        *,
        allow_placeholder_tin: bool = False,
        skip_pdf: bool = False,
    ) -> dict[str, Any]:
        idempotency_key = str(payload.get("idempotency_key") or payload.get("external_reference") or "").strip()
        if not idempotency_key:
            raise ValueError("mark-paid requires idempotency_key or external_reference")

        payment_date = str(payload.get("payment_date") or utc_today_iso()).strip()
        payment_method = str(payload.get("payment_method") or "Bank Transfer").strip()
        amount_received = float(payload.get("amount_received") or 0.0)
        allocations_payload = payload.get("allocations") or []
        if not allocations_payload:
            raise ValueError("allocations[] is required")

        withholding_events_payload = payload.get("certified_withholding_events") or []
        now = utc_now_iso_z()

        with self.repo.transaction() as conn:
            existing = self.repo.find_idempotent_response(
                conn, command_name="mark-paid", idempotency_key=idempotency_key
            )
            if existing:
                return existing

            resolved_allocations: list[dict[str, Any]] = []
            for allocation in allocations_payload:
                sales_transaction_id = str(allocation.get("sales_transaction_id") or "").strip()
                invoice_no = str(allocation.get("invoice_no") or "").strip()
                if not sales_transaction_id:
                    if not invoice_no:
                        raise ValueError("Each allocation requires sales_transaction_id or invoice_no")
                    sales = conn.execute(
                        "SELECT * FROM sales_transactions WHERE invoice_no = ?",
                        (invoice_no,),
                    ).fetchone()
                else:
                    sales = conn.execute(
                        "SELECT * FROM sales_transactions WHERE sales_transaction_id = ?",
                        (sales_transaction_id,),
                    ).fetchone()
                if not sales:
                    raise ValueError(f"Sales transaction not found for allocation: {allocation}")
                sales = dict(sales)
                resolved_allocations.append(
                    {
                        "sales_transaction": sales,
                        "allocated_amount": float(allocation.get("allocated_amount") or 0.0),
                        "notes": str(allocation.get("notes") or ""),
                    }
                )

            first_sales = resolved_allocations[0]["sales_transaction"]
            vendor = self.config.registry.get(str(first_sales["vendor_of_record_id"]))
            ensure_vendor_tin(vendor.tin, allow_placeholder_tin=allow_placeholder_tin)

            receipt_year = int(payment_date.split("-")[0])
            receipt_seq = self.repo.next_sequence(
                conn,
                vendor_of_record_id=str(first_sales["vendor_of_record_id"]),
                doc_type=DocumentType.RECEIPT.value,
                year=receipt_year,
            )
            receipt_no = self.repo.format_doc_number(
                DocumentType.RECEIPT,
                invoice_no=str(first_sales["invoice_no"]),
                year=receipt_year,
                sequence=receipt_seq,
            )
            while conn.execute(
                "SELECT 1 FROM payments WHERE receipt_no = ?",
                (receipt_no,),
            ).fetchone():
                receipt_seq = self.repo.next_sequence(
                    conn,
                    vendor_of_record_id=str(first_sales["vendor_of_record_id"]),
                    doc_type=DocumentType.RECEIPT.value,
                    year=receipt_year,
                )
                receipt_no = self.repo.format_doc_number(
                    DocumentType.RECEIPT,
                    invoice_no=str(first_sales["invoice_no"]),
                    year=receipt_year,
                    sequence=receipt_seq,
                )

            payment_id = new_ulid()
            conn.execute(
                """
                INSERT INTO payments(
                    payment_id, vendor_of_record_id, buyer_id, payment_date, amount_received, currency,
                    payment_method, external_reference, idempotency_key, receipt_no, created_at, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payment_id,
                    first_sales["vendor_of_record_id"],
                    first_sales["buyer_id"],
                    payment_date,
                    amount_received,
                    first_sales["currency"],
                    payment_method,
                    payload.get("external_reference"),
                    idempotency_key,
                    receipt_no,
                    now,
                    now,
                ),
            )

            allocated_sum = 0.0
            touched_sales_ids: set[str] = set()
            for allocation in resolved_allocations:
                sales = allocation["sales_transaction"]
                allocated_amount = float(allocation["allocated_amount"])
                allocated_sum += allocated_amount
                touched_sales_ids.add(str(sales["sales_transaction_id"]))
                conn.execute(
                    """
                    INSERT INTO payment_allocations(
                        allocation_id, payment_id, sales_transaction_id, allocated_amount, allocation_date, notes, created_at
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_ulid(),
                        payment_id,
                        sales["sales_transaction_id"],
                        allocated_amount,
                        payment_date,
                        allocation["notes"],
                        now,
                    ),
                )

            if allocated_sum - amount_received > 0.01:
                raise ValueError("Allocated sum cannot exceed amount_received")

            for event in withholding_events_payload:
                sales_transaction_id = str(event.get("sales_transaction_id") or "").strip()
                if not sales_transaction_id:
                    raise ValueError("Withholding event requires sales_transaction_id")
                amount = float(event.get("amount") or 0.0)
                withholder_party_id = str(event.get("withholder_party_id") or first_sales["buyer_id"]).strip()
                withholding_type = str(event.get("withholding_type") or "WHT").strip().upper()
                certificate_ref = str(event.get("certificate_ref") or "").strip() or None
                evidence_path = str(event.get("evidence_path") or "").strip() or None
                evidence_hash = str(event.get("evidence_hash") or "").strip() or None
                certified_at = str(event.get("certified_at") or "").strip()
                if not certified_at:
                    raise ValueError("Withholding event requires certified_at")
                if not certificate_ref and not (evidence_path and evidence_hash):
                    raise ValueError("Certified withholding requires certificate_ref or evidence_path+evidence_hash")
                conn.execute(
                    """
                    INSERT INTO tax_withholding_events(
                        withholding_event_id, sales_transaction_id, sales_line_id, withholder_party_id, withholding_type,
                        amount, certificate_ref, evidence_path, evidence_hash, certified_at, created_at
                    )
                    VALUES(?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_ulid(),
                        sales_transaction_id,
                        withholder_party_id,
                        withholding_type,
                        amount,
                        certificate_ref,
                        evidence_path,
                        evidence_hash,
                        certified_at,
                        now,
                    ),
                )
                touched_sales_ids.add(sales_transaction_id)

            for sales_id in touched_sales_ids:
                outstanding = conn.execute(
                    """
                    SELECT outstanding_balance
                    FROM drep_sales
                    WHERE sales_transaction_id = ?
                    """,
                    (sales_id,),
                ).fetchone()
                if not outstanding:
                    continue
                balance = float(outstanding["outstanding_balance"])
                delivery = conn.execute(
                    "SELECT delivery_id, contract_id FROM sales_transactions WHERE sales_transaction_id = ?",
                    (sales_id,),
                ).fetchone()
                if not delivery:
                    continue
                if balance <= 0:
                    conn.execute(
                        "UPDATE deliveries SET status = ?, paid_at = ?, updated_at = ? WHERE delivery_id = ?",
                        (DeliveryStatus.PAID.value, now, now, delivery["delivery_id"]),
                    )
                    conn.execute(
                        """
                        UPDATE planned_deliveries
                        SET status = 'PAID', updated_at = ?
                        WHERE delivery_id = ?
                        """,
                        (now, delivery["delivery_id"]),
                    )
                self.repo.refresh_contract_status(conn, delivery["contract_id"])

            # Receipt generation against first sales transaction.
            first_bundle = conn.execute(
                """
                SELECT
                  d.*,
                  c.contract_ref, c.lpo_no, c.lpo_date, c.issue_date, c.due_date, c.due_terms, c.currency,
                  c.buyer_id, c.vendor_of_record_id, c.operator_id, c.source_id, c.processor_id, c.contract_id
                FROM deliveries d
                JOIN sales_transactions st ON st.delivery_id = d.delivery_id
                JOIN contracts c ON c.contract_id = d.contract_id
                WHERE st.sales_transaction_id = ?
                """,
                (first_sales["sales_transaction_id"],),
            ).fetchone()
            first_bundle = dict(first_bundle) if first_bundle else {}
            first_sales_lines = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM sales_lines WHERE sales_transaction_id = ? ORDER BY line_no",
                    (first_sales["sales_transaction_id"],),
                ).fetchall()
            ]
            first_coa = conn.execute(
                """
                SELECT cr.* FROM delivery_coa_links dcl
                JOIN coa_results cr ON cr.coa_record_id = dcl.coa_record_id
                WHERE dcl.delivery_id = ?
                ORDER BY cr.updated_at DESC
                LIMIT 1
                """,
                (first_bundle.get("delivery_id"),),
            ).fetchone()
            if not first_coa:
                # fallback for legacy rows with no explicit link
                first_coa = {
                    "results_json": "[]",
                    "coa_no": "",
                    "profile_key": f"{infer_buyer_group(first_bundle.get('buyer_id', ''))}:{first_sales_lines[0]['product_code'] if first_sales_lines else ''}",
                    "profile_version": "01",
                }
            else:
                first_coa = dict(first_coa)

            receipt_rendered = self.pdf_bridge.render_receipt_document(
                bundle=first_bundle,
                sales_transaction=first_sales,
                sales_lines=first_sales_lines,
                coa_record=first_coa,
                receipt_no=receipt_no,
                invoice_no=str(first_sales["invoice_no"]),
                skip_pdf=skip_pdf,
            )
            vendor_code = (vendor.code or "VENDOR").upper()
            output_dir = output_delivery_dir(self.config.output_v2_dir, vendor_code=vendor_code, invoice_no=str(first_sales["invoice_no"]))
            receipt_path = output_dir / receipt_rendered["filename"]
            if receipt_rendered["pdf_bytes"] is not None:
                receipt_path.write_bytes(receipt_rendered["pdf_bytes"])
            receipt_doc_id = new_ulid()
            snapshot_id = self.repo.create_parties_snapshot(
                conn,
                buyer_id=str(first_sales["buyer_id"]),
                vendor_id=str(first_sales["vendor_of_record_id"]),
                operator_id=str(first_sales["operator_id"]),
            )
            conn.execute(
                """
                INSERT INTO documents(
                    doc_id, delivery_id, sales_transaction_id, snapshot_id, vendor_of_record_id, doc_type, doc_number,
                    revision_no, status, pdf_path, pdf_sha256, content_sha256, generated_at, created_at, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, 1, 'ACTIVE', ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt_doc_id,
                    first_sales["delivery_id"],
                    first_sales["sales_transaction_id"],
                    snapshot_id,
                    first_sales["vendor_of_record_id"],
                    DocumentType.RECEIPT.value,
                    receipt_no,
                    str(receipt_path) if receipt_rendered["pdf_bytes"] is not None else None,
                    receipt_rendered["pdf_sha256"],
                    receipt_rendered["content_sha256"],
                    now,
                    now,
                    now,
                ),
            )
            self.repo.link_document_to_sales_lines(
                conn,
                doc_id=receipt_doc_id,
                sales_transaction_id=str(first_sales["sales_transaction_id"]),
                delivery_id=str(first_sales["delivery_id"]),
            )
            conn.execute(
                "UPDATE payments SET receipt_doc_id = ?, updated_at = ? WHERE payment_id = ?",
                (receipt_doc_id, now, payment_id),
            )

            response = {
                "ok": True,
                "payment_id": payment_id,
                "receipt_no": receipt_no,
                "receipt_doc_id": receipt_doc_id,
                "receipt_path": str(receipt_path),
                "allocated_amount_total": allocated_sum,
                "amount_received": amount_received,
                "idempotency_key": idempotency_key,
            }
            self.repo.save_idempotent_response(
                conn,
                command_name="mark-paid",
                idempotency_key=idempotency_key,
                response=response,
            )
            return response

    def _load_idempotent_response(
        self,
        *,
        command_name: str,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        key = str(idempotency_key or "").strip()
        if not key:
            return None
        with self.repo.transaction() as conn:
            return self.repo.find_idempotent_response(
                conn,
                command_name=command_name,
                idempotency_key=key,
            )

    def settlement_suggest_allocations(
        self,
        *,
        contract_id: str,
        as_of_date: str,
        payment_reference: str = "",
        amount_received: float | None = None,
        payment_date: str | None = None,
        payment_method: str = "Bank Transfer",
        dry_run: bool = True,
        idempotency_key: str | None = None,
        persist: bool = True,
    ) -> dict[str, Any]:
        amount_value = _safe_float(amount_received, 0.0)
        payment_ref = str(payment_reference or "").strip()
        payment_date_value = str(payment_date or as_of_date or utc_today_iso()).strip()
        benchmark_payload = {
            "contract_id": contract_id,
            "as_of_date": as_of_date,
            "payment_reference": payment_ref,
            "amount_received": round(amount_value, 2),
            "payment_date": payment_date_value,
            "payment_method": str(payment_method or "Bank Transfer").strip(),
            "dry_run": bool(dry_run),
        }
        suggest_key = str(idempotency_key or "").strip() or f"settlement-suggest::{canonical_json_sha256(benchmark_payload)}"
        if persist:
            existing = self._load_idempotent_response(command_name="settlement-suggest", idempotency_key=suggest_key)
            if existing:
                return existing

        sales_rows = self.repo.fetch_all(
            """
            SELECT
              ds.sales_transaction_id,
              ds.invoice_no,
              ds.invoice_date,
              ds.due_date,
              ds.amount_due,
              ds.outstanding_balance,
              ds.expected_wht_amount,
              ds.certified_withheld_amount,
              ds.buyer_id,
              ds.vendor_of_record_id
            FROM drep_sales ds
            WHERE ds.contract_id = ?
              AND ds.outstanding_balance > 0
            ORDER BY ds.due_date ASC, ds.invoice_no ASC
            """,
            (contract_id,),
        )
        ref_norm = _norm_token(payment_ref)
        has_single_candidate = len(sales_rows) == 1
        suggestions: list[dict[str, Any]] = []
        for row in sales_rows:
            invoice_no = str(row.get("invoice_no") or "")
            invoice_norm = _norm_token(invoice_no)
            outstanding = float(row.get("outstanding_balance") or 0.0)
            expected_wht_amount = float(row.get("expected_wht_amount") or 0.0)
            certified_withheld_amount = float(row.get("certified_withheld_amount") or 0.0)
            score = 0.0
            reason_bits: list[str] = []

            if ref_norm and invoice_norm and (invoice_norm in ref_norm or ref_norm in invoice_norm):
                score += 0.70
                reason_bits.append("invoice_reference_match")
            elif ref_norm and invoice_norm:
                invoice_suffix = invoice_norm[-6:] if len(invoice_norm) >= 6 else invoice_norm
                if invoice_suffix and invoice_suffix in ref_norm:
                    score += 0.30
                    reason_bits.append("invoice_reference_partial")
            else:
                reason_bits.append("invoice_reference_missing")

            if amount_value > 0 and outstanding > 0:
                delta = abs(amount_value - outstanding)
                near_threshold = max(1.0, outstanding * 0.01)
                mismatch_threshold = max(10.0, outstanding * 0.05)
                if delta <= 0.01:
                    score += 0.20
                    reason_bits.append("amount_exact")
                elif delta <= near_threshold:
                    score += 0.10
                    reason_bits.append("amount_near")
                elif delta > mismatch_threshold:
                    reason_bits.append("amount_mismatch_outside_policy")
            withholding_gap = max(expected_wht_amount - certified_withheld_amount, 0.0)
            if amount_value > 0 and outstanding > 0 and withholding_gap > 0:
                shortfall = max(outstanding - amount_value, 0.0)
                if shortfall > 0 and shortfall <= (withholding_gap + 1.0):
                    reason_bits.append("withholding_evidence_missing")
            if has_single_candidate:
                score += 0.10
                reason_bits.append("single_candidate")

            confidence = round(min(0.99, score), 4)
            suggested_amount = round(outstanding if amount_value <= 0 else min(outstanding, amount_value), 2)
            suggestion_id = f"SUG-{canonical_json_sha256({'contract_id': contract_id, 'sales_transaction_id': row['sales_transaction_id'], 'as_of_date': as_of_date, 'payment_reference': payment_ref, 'amount_received': round(amount_value, 2)})[:20]}"
            suggestions.append(
                {
                    "suggestion_id": suggestion_id,
                    "sales_transaction_id": str(row.get("sales_transaction_id") or ""),
                    "invoice_no": invoice_no,
                    "invoice_date": str(row.get("invoice_date") or ""),
                    "due_date": str(row.get("due_date") or ""),
                    "outstanding_balance": outstanding,
                    "suggested_amount": suggested_amount,
                    "expected_wht_amount": expected_wht_amount,
                    "certified_withheld_amount": certified_withheld_amount,
                    "confidence": confidence,
                    "reason_bits": reason_bits,
                }
            )

        suggestions.sort(
            key=lambda item: (
                -float(item.get("confidence") or 0.0),
                str(item.get("due_date") or ""),
                str(item.get("invoice_no") or ""),
            )
        )
        top = suggestions[0] if suggestions else None
        second = suggestions[1] if len(suggestions) > 1 else None
        top_confidence = float(top.get("confidence") or 0.0) if isinstance(top, dict) else 0.0
        ambiguous = bool(
            top
            and second
            and top_confidence >= 0.75
            and abs(top_confidence - float(second.get("confidence") or 0.0)) < 0.05
        )
        reason_code = "pass"
        decision_class = "BLOCKER"
        if not top:
            reason_code = "invoice_not_settlement_eligible"
            decision_class = "BLOCKER"
        elif amount_value <= 0 and not payment_ref:
            reason_code = "insufficient_payment_data"
            decision_class = "BLOCKER"
        elif ambiguous:
            reason_code = "multiple_candidate_conflict"
            decision_class = "BLOCKER"
        elif "withholding_evidence_missing" in list(top.get("reason_bits") or []):
            reason_code = "withholding_evidence_missing"
            decision_class = "BLOCKER"
        elif "amount_mismatch_outside_policy" in list(top.get("reason_bits") or []):
            reason_code = "amount_mismatch_outside_policy"
            decision_class = "BLOCKER"
        elif top_confidence >= 0.95 and "invoice_reference_match" in list(top.get("reason_bits") or []):
            reason_code = "auto_threshold_met"
            decision_class = "AUTO_APPLY"
        elif top_confidence >= 0.75:
            reason_code = "review_threshold"
            decision_class = "REVIEW"
        elif not payment_ref:
            reason_code = "payment_reference_missing"
            decision_class = "BLOCKER"
        else:
            reason_code = "payment_reference_ambiguous"
            decision_class = "BLOCKER"

        response = {
            "ok": True,
            "contract_id": contract_id,
            "as_of_date": as_of_date,
            "payment_date": payment_date_value,
            "payment_method": str(payment_method or "Bank Transfer").strip(),
            "payment_reference": payment_ref,
            "amount_received": round(amount_value, 2),
            "dry_run": bool(dry_run),
            "suggestion_set_id": suggest_key,
            "suggestions": suggestions,
            "top_suggestion": top,
            "decision_class": decision_class,
            "reason_code": reason_code,
            "ambiguous": ambiguous,
        }
        if persist:
            with self.repo.transaction() as conn:
                self.repo.save_idempotent_response(
                    conn,
                    command_name="settlement-suggest",
                    idempotency_key=suggest_key,
                    response=response,
                )
                self.repo.append_event(
                    conn,
                    entity_type="CONTRACT",
                    entity_id=contract_id,
                    event_type="SETTLEMENT_SUGGESTED",
                    as_of_date=as_of_date,
                    payload={
                        "suggestion_set_id": suggest_key,
                        "decision_class": decision_class,
                        "reason_code": reason_code,
                        "suggestion_count": len(suggestions),
                    },
                    source="settlement-copilot",
                )
        return response

    def settlement_apply_suggestion(
        self,
        *,
        contract_id: str,
        suggestion_set_id: str,
        suggestion_id: str,
        as_of_date: str,
        decision: str = "APPLY",
        reason: str = "",
        allow_placeholder_tin: bool = True,
        skip_pdf: bool = False,
        payment_reference: str = "",
        payment_date: str | None = None,
        payment_method: str = "Bank Transfer",
        amount_received: float | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        decision_norm = str(decision or "APPLY").strip().upper()
        if decision_norm not in {"APPLY", "APPROVE", "AUTO_APPLY", "REJECT"}:
            raise ValueError("decision must be APPLY, APPROVE, AUTO_APPLY or REJECT")
        apply_key = str(idempotency_key or "").strip() or (
            f"settlement-apply::{canonical_json_sha256({'contract_id': contract_id, 'suggestion_set_id': suggestion_set_id, 'suggestion_id': suggestion_id, 'as_of_date': as_of_date, 'decision': decision_norm})}"
        )
        existing = self._load_idempotent_response(command_name="settlement-apply", idempotency_key=apply_key)
        if existing:
            return existing

        suggestion_set = self._load_idempotent_response(command_name="settlement-suggest", idempotency_key=suggestion_set_id)
        if not suggestion_set:
            raise ValueError("Unknown suggestion_set_id")
        if str(suggestion_set.get("contract_id") or "") != contract_id:
            raise ValueError("suggestion_set_id does not match contract")
        suggestions = suggestion_set.get("suggestions")
        if not isinstance(suggestions, list):
            suggestions = []
        selected = None
        for row in suggestions:
            if str(row.get("suggestion_id") or "") == suggestion_id:
                selected = row
                break
        if not isinstance(selected, dict):
            raise ValueError("Unknown suggestion_id")

        confidence = float(selected.get("confidence") or 0.0)
        decision_class = str(suggestion_set.get("decision_class") or "BLOCKER").upper()
        ambiguous = bool(suggestion_set.get("ambiguous"))
        should_apply = decision_norm in {"APPLY", "APPROVE", "AUTO_APPLY"} and decision_class == "AUTO_APPLY" and not ambiguous

        if should_apply:
            external_reference = str(payment_reference or suggestion_set.get("payment_reference") or "").strip() or f"SETTLE-{new_ulid()}"
            payment_payload = {
                "payment_date": str(payment_date or suggestion_set.get("payment_date") or as_of_date).strip(),
                "payment_method": str(payment_method or suggestion_set.get("payment_method") or "Bank Transfer").strip(),
                "external_reference": external_reference,
                "idempotency_key": f"{apply_key}::mark-paid",
                "amount_received": _safe_float(
                    amount_received,
                    _safe_float(suggestion_set.get("amount_received"), _safe_float(selected.get("suggested_amount"), 0.0)),
                ),
                "allocations": [
                    {
                        "sales_transaction_id": str(selected.get("sales_transaction_id") or ""),
                        "allocated_amount": _safe_float(selected.get("suggested_amount"), 0.0),
                        "notes": "settlement_copilot_auto_apply",
                    }
                ],
            }
            paid = self.mark_paid(
                payment_payload,
                allow_placeholder_tin=allow_placeholder_tin,
                skip_pdf=skip_pdf,
            )
            response = {
                "ok": True,
                "status": "APPLIED",
                "contract_id": contract_id,
                "suggestion_set_id": suggestion_set_id,
                "suggestion_id": suggestion_id,
                "decision_class": decision_class,
                "confidence": confidence,
                "reason_code": "applied",
                "mark_paid_result": paid,
            }
            with self.repo.transaction() as conn:
                self.repo.append_event(
                    conn,
                    entity_type="CONTRACT",
                    entity_id=contract_id,
                    event_type="SETTLEMENT_SUGGESTION_ACCEPTED",
                    as_of_date=as_of_date,
                    payload={
                        "suggestion_set_id": suggestion_set_id,
                        "suggestion_id": suggestion_id,
                        "sales_transaction_id": str(selected.get("sales_transaction_id") or ""),
                        "confidence": confidence,
                        "decision": decision_norm,
                    },
                    source="settlement-copilot",
                )
                self.repo.save_idempotent_response(
                    conn,
                    command_name="settlement-apply",
                    idempotency_key=apply_key,
                    response=response,
                )
            return response

        reason_code = (
            "multiple_candidate_conflict"
            if ambiguous
            else str(suggestion_set.get("reason_code") or "payment_reference_ambiguous")
        )
        severity = "BLOCKER" if decision_class == "BLOCKER" or decision_norm == "REJECT" else "REVIEW"
        details = {
            "as_of_date": as_of_date,
            "suggestion_set_id": suggestion_set_id,
            "suggestion_id": suggestion_id,
            "decision": decision_norm,
            "decision_class": decision_class,
            "confidence": confidence,
            "reason": str(reason or "").strip(),
            "selected_candidate": selected,
            "all_candidates": suggestions[:3],
        }
        case_key = f"settlement|{contract_id}|{suggestion_set_id}|{suggestion_id}|{reason_code}"
        with self.repo.transaction() as conn:
            case_row = self.repo.create_or_get_exception_case(
                conn,
                autonomy_run_id=new_ulid(),
                action_intent_id=None,
                contract_id=contract_id,
                delivery_id=None,
                planned_delivery_id=None,
                case_type="settlement_allocation",
                severity=severity,
                reason_code=reason_code,
                details=details,
                idempotency_key=case_key,
            )
            self.repo.add_decision_feature(
                conn,
                exception_case_id=str(case_row["exception_case_id"]),
                feature_key="settlement_suggestion_candidates",
                feature_payload=details,
            )
            self.repo.append_event(
                conn,
                entity_type="EXCEPTION_CASE",
                entity_id=str(case_row["exception_case_id"]),
                event_type="CASE_OPENED",
                as_of_date=as_of_date,
                payload={"reason_code": reason_code, "suggestion_set_id": suggestion_set_id},
                source="settlement-copilot",
            )
            self.repo.append_event(
                conn,
                entity_type="CONTRACT",
                entity_id=contract_id,
                event_type="SETTLEMENT_SUGGESTION_ROUTED_EXCEPTION",
                as_of_date=as_of_date,
                payload={
                    "reason_code": reason_code,
                    "suggestion_set_id": suggestion_set_id,
                    "suggestion_id": suggestion_id,
                    "decision": decision_norm,
                    "confidence": confidence,
                },
                source="settlement-copilot",
            )
            response = {
                "ok": False,
                "status": "EXCEPTION_ROUTED",
                "contract_id": contract_id,
                "suggestion_set_id": suggestion_set_id,
                "suggestion_id": suggestion_id,
                "decision_class": decision_class,
                "confidence": confidence,
                "reason_code": reason_code,
                "exception_case_id": str(case_row["exception_case_id"]),
            }
            self.repo.save_idempotent_response(
                conn,
                command_name="settlement-apply",
                idempotency_key=apply_key,
                response=response,
            )
            return response

    def portfolio_kpi_strip(
        self,
        *,
        as_of_date: str,
        lookback_window_days: int = 30,
        benchmark_version: str = "phase2.pr10.v1",
    ) -> dict[str, Any]:
        from domain.automation import AutomationOrchestrator

        orchestrator = AutomationOrchestrator(self.config, self.repo, self)
        metrics = orchestrator.compute_metrics_snapshot(
            as_of_date=as_of_date,
            lookback_window_days=lookback_window_days,
            benchmark_version=benchmark_version,
        )
        return {
            "as_of_date": as_of_date,
            "lookback_window_days": int(lookback_window_days),
            "benchmark_version": benchmark_version,
            "touchless_rate": metrics.get("touchless_rate"),
            "touchless_rate_reason_code": metrics.get("touchless_rate_reason_code"),
            "manual_inputs_per_delivery": metrics.get("manual_inputs_per_delivery"),
            "manual_inputs_per_delivery_reason_code": metrics.get("manual_inputs_per_delivery_reason_code"),
            "exception_resolution_time_hours_p50": metrics.get("exception_resolution_time_hours_p50"),
            "exception_resolution_time_hours_p95": metrics.get("exception_resolution_time_hours_p95"),
            "exception_resolution_time_reason_code": metrics.get("exception_resolution_time_reason_code"),
            "first_time_lpo_to_pack_minutes": metrics.get("first_time_lpo_to_pack_minutes"),
            "first_time_lpo_to_pack_reason_code": metrics.get("first_time_lpo_to_pack_reason_code"),
            "auto_action_success_rate": metrics.get("auto_action_success_rate"),
            "auto_action_success_rate_reason_code": metrics.get("auto_action_success_rate_reason_code"),
            "payment_suggestion_acceptance_rate": metrics.get("payment_suggestion_acceptance_rate"),
            "payment_suggestion_acceptance_rate_reason_code": metrics.get("payment_suggestion_acceptance_rate_reason_code"),
            "pr10_gate_pass": metrics.get("pr10_gate_pass"),
            "pr10_gate_reason_code": metrics.get("pr10_gate_reason_code"),
        }

    def portfolio_gate_health_strip(
        self,
        *,
        as_of_date: str,
        lookback_window_days: int = 30,
        benchmark_version: str = "phase2.pr11.v1",
    ) -> dict[str, Any]:
        from domain.automation import AutomationOrchestrator

        orchestrator = AutomationOrchestrator(self.config, self.repo, self)
        return orchestrator.phase2_gate_health_snapshot(
            as_of_date=as_of_date,
            lookback_window_days=lookback_window_days,
            benchmark_version=benchmark_version,
            waivers_path=self.config.state_dir / "release-readiness" / "phase2_gate_waivers.json",
        )

    def portfolio_drift_strip(
        self,
        *,
        as_of_date: str,
        lookback_window_days: int = 30,
        benchmark_version: str = "phase2.pr12.v1",
    ) -> dict[str, Any]:
        from domain.automation import AutomationOrchestrator

        orchestrator = AutomationOrchestrator(self.config, self.repo, self)
        return orchestrator.phase2_drift_health_snapshot(
            as_of_date=as_of_date,
            lookback_window_days=lookback_window_days,
            benchmark_version=benchmark_version,
        )

    def portfolio_drift_operations_strip(
        self,
        *,
        as_of_date: str,
        lookback_window_days: int = 30,
        benchmark_version: str = "phase2.pr12.v1",
    ) -> dict[str, Any]:
        from domain.automation import AutomationOrchestrator

        orchestrator = AutomationOrchestrator(self.config, self.repo, self)
        return orchestrator.phase2_drift_operations_snapshot(
            as_of_date=as_of_date,
            lookback_window_days=lookback_window_days,
            benchmark_version=benchmark_version,
        )

    def portfolio_drift_root_cause_strip(
        self,
        *,
        as_of_date: str,
        lookback_window_days: int = 30,
        benchmark_version: str = "phase2.pr12.v1",
    ) -> dict[str, Any]:
        from domain.automation import AutomationOrchestrator

        orchestrator = AutomationOrchestrator(self.config, self.repo, self)
        return orchestrator.phase2_drift_root_cause_snapshot(
            as_of_date=as_of_date,
            lookback_window_days=lookback_window_days,
            benchmark_version=benchmark_version,
        )

    def portfolio_operator_playbooks_strip(
        self,
        *,
        as_of_date: str,
        lookback_window_days: int = 30,
        benchmark_version: str = "phase2.pr12.v1",
    ) -> dict[str, Any]:
        from domain.automation import AutomationOrchestrator

        orchestrator = AutomationOrchestrator(self.config, self.repo, self)
        return orchestrator.phase2_operator_playbooks_snapshot(
            as_of_date=as_of_date,
            lookback_window_days=lookback_window_days,
            benchmark_version=benchmark_version,
        )

    def portfolio_sla_trends(
        self,
        *,
        as_of_date: str,
        lookback_window_days: int = 30,
        benchmark_version: str = "phase2.pr10.v1",
    ) -> dict[str, Any]:
        from domain.automation import AutomationOrchestrator

        orchestrator = AutomationOrchestrator(self.config, self.repo, self)
        metrics = orchestrator.compute_metrics_snapshot(
            as_of_date=as_of_date,
            lookback_window_days=lookback_window_days,
            benchmark_version=benchmark_version,
        )
        return {
            "as_of_date": as_of_date,
            "lookback_window_days": int(lookback_window_days),
            "benchmark_version": benchmark_version,
            "exception_resolution_trend_state": metrics.get("exception_resolution_trend_state"),
            "exception_resolution_trend_reason_code": metrics.get("exception_resolution_trend_reason_code"),
            "exception_resolution_current_p95_hours": metrics.get("exception_resolution_current_p95_hours"),
            "exception_resolution_previous_p95_hours": metrics.get("exception_resolution_previous_p95_hours"),
            "settlement_aging_trend_state": metrics.get("settlement_aging_trend_state"),
            "settlement_aging_trend_reason_code": metrics.get("settlement_aging_trend_reason_code"),
            "settlement_aging_current": metrics.get("settlement_aging_current"),
            "settlement_aging_previous": metrics.get("settlement_aging_previous"),
        }

    def export_drep(self, *, as_of_date: str, out_dir: Path) -> dict[str, Any]:
        exports = export_drep_views(self.repo, as_of_date=as_of_date, out_dir=out_dir)
        return {
            "ok": True,
            "as_of_date": as_of_date,
            "exports": exports,
        }

    def _command_center_section(self, row: dict[str, Any]) -> str:
        if int(row.get("needs_decision_count") or 0) > 0:
            return "NEEDS_DECISION"
        if str(row.get("lpo_state") or "").upper() != "ACTIVE":
            return "AT_RISK"
        if int(row.get("due_planned_lots") or 0) > 0 or int(row.get("delivered_not_invoiced") or 0) > 0:
            return "DUE_ACTION"
        if float(row.get("outstanding_total") or 0.0) > 0:
            return "AT_RISK"
        if int(row.get("open_planned_lots") or 0) > 0:
            return "AT_RISK"
        return "AT_RISK"

    def _run_all_skip_reason(self, row: dict[str, Any]) -> str | None:
        if int(row.get("needs_decision_count") or 0) > 0:
            return "OPEN_EXCEPTION_CASES"
        if str(row.get("lpo_state") or "").upper() != "ACTIVE":
            return "LPO_NOT_ACTIVE"
        if int(row.get("due_planned_lots") or 0) <= 0 and int(row.get("delivered_not_invoiced") or 0) <= 0:
            return "NOT_DUE"
        return None

    def command_center_rows(self, *, as_of_date: str, limit: int = 200) -> list[dict[str, Any]]:
        self.repo.set_as_of_date(as_of_date)
        rows = self.repo.fetch_all(
            """
            WITH delivery_agg AS (
              SELECT
                contract_id,
                COALESCE(SUM(delivered_qty_kg), 0) AS delivered_qty_kg,
                SUM(CASE WHEN status = 'DELIVERED' THEN 1 ELSE 0 END) AS delivered_not_invoiced
              FROM deliveries
              GROUP BY contract_id
            ),
            plan_agg AS (
              SELECT
                contract_id,
                SUM(CASE WHEN status IN ('PLANNED', 'SCHEDULED') THEN 1 ELSE 0 END) AS open_planned_lots,
                SUM(
                  CASE
                    WHEN status IN ('PLANNED', 'SCHEDULED') AND planned_date <= ?
                      THEN 1
                    ELSE 0
                  END
                ) AS due_planned_lots
              FROM planned_deliveries
              GROUP BY contract_id
            ),
            outstanding_agg AS (
              SELECT
                contract_id,
                COALESCE(SUM(outstanding_balance), 0) AS outstanding_total
              FROM drep_outstanding_payments
              GROUP BY contract_id
            ),
            exception_agg AS (
              SELECT
                contract_id,
                COUNT(*) AS needs_decision_count
              FROM exception_cases
              WHERE status = 'OPEN'
              GROUP BY contract_id
            )
            SELECT
              c.contract_id,
              c.contract_ref,
              c.lpo_no,
              c.lpo_state,
              c.status,
              c.issue_date,
              c.buyer_id,
              c.vendor_of_record_id,
              c.expected_total_qty_kg,
              COALESCE(d.delivered_qty_kg, 0) AS delivered_qty_kg,
              COALESCE(p.open_planned_lots, 0) AS open_planned_lots,
              COALESCE(p.due_planned_lots, 0) AS due_planned_lots,
              COALESCE(d.delivered_not_invoiced, 0) AS delivered_not_invoiced,
              COALESCE(o.outstanding_total, 0) AS outstanding_total,
              COALESCE(e.needs_decision_count, 0) AS needs_decision_count
            FROM contracts c
            LEFT JOIN delivery_agg d ON d.contract_id = c.contract_id
            LEFT JOIN plan_agg p ON p.contract_id = c.contract_id
            LEFT JOIN outstanding_agg o ON o.contract_id = c.contract_id
            LEFT JOIN exception_agg e ON e.contract_id = c.contract_id
            WHERE c.status IN ('OPEN', 'PARTIAL')
            ORDER BY c.issue_date DESC, c.created_at DESC
            LIMIT ?
            """,
            (as_of_date, limit),
        )
        for row in rows:
            next_action = "Run Recommended"
            if int(row.get("needs_decision_count") or 0) > 0:
                next_action = "Needs Decision"
            elif str(row.get("lpo_state") or "").upper() != "ACTIVE":
                next_action = "Review LPO State"
            elif int(row.get("delivered_not_invoiced") or 0) > 0 or int(row.get("due_planned_lots") or 0) > 0:
                next_action = "Run Recommended"
            elif float(row.get("outstanding_total") or 0.0) > 0:
                next_action = "Settle"
            row["next_action"] = next_action
            row["section"] = self._command_center_section(row)
        return rows

    def command_center_sections(self, *, as_of_date: str, limit: int = 200) -> dict[str, Any]:
        rows = self.command_center_rows(as_of_date=as_of_date, limit=limit)
        sections: dict[str, list[dict[str, Any]]] = {
            "NEEDS_DECISION": [],
            "DUE_ACTION": [],
            "AT_RISK": [],
        }
        for row in rows:
            section = str(row.get("section") or "DUE_ACTION").upper()
            sections.setdefault(section, []).append(row)
        return {
            "as_of_date_utc": as_of_date,
            "rows": rows,
            "sections": sections,
        }

    def run_all_eligible(
        self,
        *,
        as_of_date_utc: str | None = None,
        mode: str,
        benchmark_version: str = "phase2.pr6.v1",
        max_contracts_per_run: int = 20,
        max_actions_per_run: int = 200,
        preview_token: str | None = None,
    ) -> dict[str, Any]:
        if mode not in {"preview", "execute"}:
            raise ValueError("mode must be preview or execute")
        if max_contracts_per_run <= 0:
            raise ValueError("max_contracts_per_run must be > 0")
        if max_actions_per_run <= 0:
            raise ValueError("max_actions_per_run must be > 0")

        as_of_date = str(as_of_date_utc or utc_today_iso()).strip()
        rows = self.command_center_rows(as_of_date=as_of_date, limit=1000)
        eligible_all: list[dict[str, Any]] = []
        skipped_contracts: list[dict[str, Any]] = []
        for row in rows:
            contract_id = str(row.get("contract_id") or "")
            reason = self._run_all_skip_reason(row)
            if reason:
                skipped_contracts.append(
                    {
                        "contract_id": contract_id,
                        "reason_code": reason,
                    }
                )
                continue
            eligible_all.append(row)

        capped_eligible = eligible_all[:max_contracts_per_run]
        for row in eligible_all[max_contracts_per_run:]:
            skipped_contracts.append(
                {
                    "contract_id": str(row.get("contract_id") or ""),
                    "reason_code": "CONTRACT_CAP_REACHED",
                }
            )

        contract_scope_hash = canonical_json_sha256(
            {
                "as_of_date_utc": as_of_date,
                "benchmark_version": benchmark_version,
                "eligible_contracts": [str(row.get("contract_id") or "") for row in eligible_all],
                "max_contracts_per_run": max_contracts_per_run,
                "max_actions_per_run": max_actions_per_run,
            }
        )
        preview_seed = {
            "as_of_date_utc": as_of_date,
            "benchmark_version": benchmark_version,
            "max_contracts_per_run": max_contracts_per_run,
            "max_actions_per_run": max_actions_per_run,
            "contract_scope_hash": contract_scope_hash,
            "eligible_contract_ids": [str(row.get("contract_id") or "") for row in capped_eligible],
            "skipped_contracts": sorted(
                skipped_contracts,
                key=lambda item: (str(item.get("contract_id") or ""), str(item.get("reason_code") or "")),
            ),
        }
        preview_token_value = canonical_json_sha256(preview_seed)
        preview_idempotency_key = (
            "run_all_preview::"
            f"{as_of_date}::{benchmark_version}::{max_contracts_per_run}::{max_actions_per_run}::{contract_scope_hash}"
        )

        if mode == "preview":
            with self.repo.transaction() as conn:
                existing = self.repo.find_idempotent_response(
                    conn,
                    command_name="run-all-eligible-preview",
                    idempotency_key=preview_idempotency_key,
                )
                if existing:
                    return existing
                response = {
                    "ok": True,
                    "mode": "preview",
                    "as_of_date_utc": as_of_date,
                    "benchmark_version": benchmark_version,
                    "max_contracts_per_run": max_contracts_per_run,
                    "max_actions_per_run": max_actions_per_run,
                    "contract_scope_hash": contract_scope_hash,
                    "eligible_contract_ids": [str(row.get("contract_id") or "") for row in capped_eligible],
                    "eligible_count": len(capped_eligible),
                    "skipped_contracts": sorted(
                        skipped_contracts,
                        key=lambda item: (str(item.get("contract_id") or ""), str(item.get("reason_code") or "")),
                    ),
                    "preview_token": preview_token_value,
                    "dry_run_required": True,
                    "execute_idempotency_key": (
                        "run_all_execute::"
                        f"{as_of_date}::{benchmark_version}::{preview_token_value}::"
                        f"{canonical_json_sha256({'eligible_contract_ids': [str(row.get('contract_id') or '') for row in capped_eligible]})}"
                    ),
                }
                self.repo.save_idempotent_response(
                    conn,
                    command_name="run-all-eligible-preview",
                    idempotency_key=preview_idempotency_key,
                    response=response,
                )
                return response

        expected_preview_token = preview_token_value
        provided_preview_token = str(preview_token or "").strip()
        if not provided_preview_token:
            raise ValueError("preview_token is required for run-all execute")
        if provided_preview_token != expected_preview_token:
            raise ValueError("preview_token mismatch for current run-all scope")

        eligible_contract_ids = [str(row.get("contract_id") or "") for row in capped_eligible]
        eligible_contracts_hash = canonical_json_sha256({"eligible_contract_ids": eligible_contract_ids})
        execute_idempotency_key = (
            "run_all_execute::"
            f"{as_of_date}::{benchmark_version}::{provided_preview_token}::{eligible_contracts_hash}"
        )
        expected_actions_per_contract = 5
        executed_contracts: list[dict[str, Any]] = []
        capped_skips: list[dict[str, Any]] = []
        attempted_actions = 0

        with self.repo.transaction() as conn:
            existing = self.repo.find_idempotent_response(
                conn,
                command_name="run-all-eligible-execute",
                idempotency_key=execute_idempotency_key,
            )
            if existing:
                existing["idempotent_replay"] = True
                return existing

        for contract_id in eligible_contract_ids:
            if attempted_actions + expected_actions_per_contract > max_actions_per_run:
                capped_skips.append(
                    {
                        "contract_id": contract_id,
                        "reason_code": "ACTION_CAP_REACHED",
                    }
                )
                continue
            existing_actions = self.repo.fetch_one(
                """
                WITH latest_executions AS (
                  SELECT
                    ae.action_intent_id,
                    ae.status,
                    ROW_NUMBER() OVER (PARTITION BY ae.action_intent_id ORDER BY ae.execution_no DESC) AS rn
                  FROM action_executions ae
                )
                SELECT
                  COUNT(DISTINCT CASE
                    WHEN ai.status = 'EXECUTED' AND le.status = 'SUCCESS' THEN ai.intent_type
                    ELSE NULL
                  END) AS successful_intents,
                  COUNT(DISTINCT CASE
                    WHEN ai.status IN ('FAILED', 'BLOCKED') THEN ai.intent_type
                    ELSE NULL
                  END) AS failed_or_blocked_intents
                FROM action_intents ai
                LEFT JOIN latest_executions le
                  ON le.action_intent_id = ai.action_intent_id
                 AND le.rn = 1
                WHERE ai.contract_id = ? AND ai.as_of_date = ?
                """,
                (contract_id, as_of_date),
            )
            successful_intents = int((existing_actions or {"successful_intents": 0})["successful_intents"] or 0)
            failed_or_blocked = int((existing_actions or {"failed_or_blocked_intents": 0})["failed_or_blocked_intents"] or 0)
            already_applied_actions = successful_intents
            if successful_intents >= expected_actions_per_contract and failed_or_blocked == 0:
                executed_contracts.append(
                    {
                        "contract_id": contract_id,
                        "status": "ALREADY_APPLIED",
                        "already_applied_actions": already_applied_actions,
                        "autonomy_run_id": None,
                        "case_ids": [],
                    }
                )
                continue
            cycle = self.run_recommended_cycle(contract_id=contract_id, as_of_date=as_of_date, dry_run=False)
            status = "SUCCESS" if cycle.get("ok") else "BLOCKED"
            executed_contracts.append(
                {
                    "contract_id": contract_id,
                    "status": status,
                    "already_applied_actions": already_applied_actions,
                    "autonomy_run_id": cycle.get("autonomy_run_id"),
                    "case_ids": cycle.get("case_ids", []),
                }
            )
            attempted_actions += expected_actions_per_contract

        response = {
            "ok": True,
            "mode": "execute",
            "as_of_date_utc": as_of_date,
            "benchmark_version": benchmark_version,
            "max_contracts_per_run": max_contracts_per_run,
            "max_actions_per_run": max_actions_per_run,
            "contract_scope_hash": contract_scope_hash,
            "preview_token": provided_preview_token,
            "execute_idempotency_key": execute_idempotency_key,
            "executed_contracts": executed_contracts,
            "skipped_contracts": sorted(
                [*skipped_contracts, *capped_skips],
                key=lambda item: (str(item.get("contract_id") or ""), str(item.get("reason_code") or "")),
            ),
            "summary": {
                "eligible_count": len(eligible_contract_ids),
                "executed_count": len(executed_contracts),
                "already_applied_count": sum(
                    1 for item in executed_contracts if str(item.get("status") or "").upper() == "ALREADY_APPLIED"
                ),
                "blocked_count": sum(
                    1 for item in executed_contracts if str(item.get("status") or "").upper() == "BLOCKED"
                ),
                "skipped_count": len(skipped_contracts) + len(capped_skips),
                "attempted_actions": attempted_actions,
            },
            "idempotent_replay": False,
        }
        with self.repo.transaction() as conn:
            self.repo.save_idempotent_response(
                conn,
                command_name="run-all-eligible-execute",
                idempotency_key=execute_idempotency_key,
                response=response,
            )
        return response

    def run_recommended_cycle(
        self,
        *,
        contract_id: str,
        as_of_date: str,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        from domain.automation import AutomationOrchestrator

        steps: list[dict[str, Any]] = []
        refresh = self.refresh_contract_state(as_of_date=as_of_date)
        steps.append(
            {
                "step": "refresh_contract_state",
                "status": "SUCCESS",
                "details": {"as_of_date": as_of_date, "updated": refresh.get("updated", 0)},
            }
        )
        contract = self.repo.fetch_one("SELECT * FROM contracts WHERE contract_id = ?", (contract_id,))
        if not contract:
            raise ValueError(f"Unknown contract_id: {contract_id}")
        lpo_state = str(contract.get("lpo_state") or "ACTIVE").upper()
        if lpo_state != "ACTIVE":
            steps.append(
                {
                    "step": "contract_state_gate",
                    "status": "BLOCKED",
                    "details": {"lpo_state": lpo_state},
                }
            )
            return {
                "ok": False,
                "contract_id": contract_id,
                "as_of_date": as_of_date,
                "autonomy_run_id": None,
                "steps": steps,
                "case_ids": [
                    str(row["exception_case_id"])
                    for row in self.repo.list_exception_cases(status="OPEN")
                    if str(row.get("contract_id") or "") == contract_id
                ],
            }

        orchestrator = AutomationOrchestrator(self.config, self.repo, self)
        autonomy = orchestrator.run_autonomy(
            as_of_date=as_of_date,
            contract_id=contract_id,
            dry_run=dry_run,
        )
        steps.append(
            {
                "step": "run_autonomy",
                "status": "SUCCESS" if autonomy.get("ok") else "FAILED",
                "details": autonomy.get("summary", {}),
            }
        )

        export_dir = (
            self.config.state_dir
            / "exports"
            / "command_center"
            / as_of_date
            / contract_id
        )
        export = self.export_drep(as_of_date=as_of_date, out_dir=export_dir)
        steps.append(
            {
                "step": "export_drep",
                "status": "SUCCESS" if export.get("ok") else "FAILED",
                "details": {"export_count": len(export.get("exports", [])), "out_dir": str(export_dir)},
            }
        )

        open_cases = [
            row
            for row in self.repo.list_exception_cases(status="OPEN")
            if str(row.get("contract_id") or "") == contract_id
        ]
        if open_cases:
            steps.append(
                {
                    "step": "exceptions",
                    "status": "BLOCKED",
                    "details": {"open_cases": len(open_cases)},
                }
            )
        return {
            "ok": len(open_cases) == 0,
            "contract_id": contract_id,
            "as_of_date": as_of_date,
            "autonomy_run_id": autonomy.get("autonomy_run_id"),
            "steps": steps,
            "case_ids": [str(row["exception_case_id"]) for row in open_cases],
            "export_dir": str(export_dir),
        }

    def command_center_timeline(
        self,
        *,
        autonomy_run_id: str,
        contract_id: str,
    ) -> list[dict[str, Any]]:
        rows = self.repo.fetch_all(
            """
            SELECT
              ai.action_intent_id,
              ai.intent_type,
              ai.status AS intent_status,
              ai.created_at,
              ai.policy_version,
              ae.status AS execution_status,
              ae.response_json,
              ae.error_json
            FROM action_intents ai
            LEFT JOIN action_executions ae
              ON ae.action_intent_id = ai.action_intent_id
             AND ae.execution_no = (
               SELECT MAX(ex2.execution_no)
               FROM action_executions ex2
               WHERE ex2.action_intent_id = ai.action_intent_id
             )
            WHERE ai.autonomy_run_id = ?
              AND ai.contract_id = ?
            ORDER BY ai.created_at ASC, ai.action_intent_id ASC
            """,
            (autonomy_run_id, contract_id),
        )
        timeline: list[dict[str, Any]] = []
        for row in rows:
            response_json = row.get("response_json")
            error_json = row.get("error_json")
            try:
                response = json.loads(response_json or "{}")
            except Exception:
                response = {}
            try:
                error = json.loads(error_json or "{}") if error_json else {}
            except Exception:
                error = {"raw": str(error_json)}
            timeline.append(
                {
                    "intent_type": str(row.get("intent_type") or ""),
                    "intent_status": str(row.get("intent_status") or ""),
                    "execution_status": str(row.get("execution_status") or ""),
                    "policy_version": str(row.get("policy_version") or ""),
                    "response": response,
                    "error": error,
                    "created_at": str(row.get("created_at") or ""),
                }
            )
        return timeline

    def execute_transport_cards(self, *, contract_id: str, as_of_date: str) -> list[dict[str, Any]]:
        rows = self.repo.fetch_all(
            """
            WITH open_transport_cases AS (
              SELECT delivery_id, COUNT(*) AS open_count
              FROM exception_cases
              WHERE status = 'OPEN'
                AND case_type = 'transport_assignment'
                AND contract_id = ?
              GROUP BY delivery_id
            ),
            top_truck AS (
              SELECT delivery_id, candidate_label, confidence
              FROM (
                SELECT
                  delivery_id,
                  candidate_label,
                  confidence,
                  ROW_NUMBER() OVER (PARTITION BY delivery_id ORDER BY confidence DESC, created_at ASC) AS rn
                FROM delivery_transport_suggestions
                WHERE entity_type = 'TRUCK'
              )
              WHERE rn = 1
            ),
            top_driver AS (
              SELECT delivery_id, candidate_label, confidence
              FROM (
                SELECT
                  delivery_id,
                  candidate_label,
                  confidence,
                  ROW_NUMBER() OVER (PARTITION BY delivery_id ORDER BY confidence DESC, created_at ASC) AS rn
                FROM delivery_transport_suggestions
                WHERE entity_type = 'DRIVER'
              )
              WHERE rn = 1
            )
            SELECT
              d.delivery_id,
              d.delivery_date,
              d.status AS delivery_status,
              d.run_id,
              d.batch_id,
              d.truck_no,
              d.driver_name,
              COALESCE(s.snapshot_id, '') AS snapshot_id,
              COALESCE(s.reason_code, '') AS snapshot_reason_code,
              COALESCE(s.confidence, 0) AS snapshot_confidence,
              COALESCE(tc.open_count, 0) AS open_transport_cases,
              COALESCE(tt.candidate_label, '') AS suggested_truck_no,
              COALESCE(td.candidate_label, '') AS suggested_driver_name,
              COALESCE(tt.confidence, 0) AS suggested_truck_confidence,
              COALESCE(td.confidence, 0) AS suggested_driver_confidence
            FROM deliveries d
            LEFT JOIN delivery_transport_snapshot s ON s.delivery_id = d.delivery_id
            LEFT JOIN open_transport_cases tc ON tc.delivery_id = d.delivery_id
            LEFT JOIN top_truck tt ON tt.delivery_id = d.delivery_id
            LEFT JOIN top_driver td ON td.delivery_id = d.delivery_id
            WHERE d.contract_id = ?
            ORDER BY d.delivery_date DESC, d.created_at DESC
            """,
            (contract_id, contract_id),
        )
        cards: list[dict[str, Any]] = []
        for row in rows:
            row_copy = dict(row)
            row_copy["as_of_date"] = as_of_date
            if str(row_copy.get("snapshot_id") or "").strip():
                row_copy["copilot_status"] = "SNAPSHOT_APPLIED"
            elif int(row_copy.get("open_transport_cases") or 0) > 0:
                row_copy["copilot_status"] = "EXCEPTION_OPEN"
            elif str(row_copy.get("suggested_truck_no") or "").strip() or str(row_copy.get("suggested_driver_name") or "").strip():
                row_copy["copilot_status"] = "SUGGESTED"
            else:
                row_copy["copilot_status"] = "NO_SUGGESTION"
            cards.append(row_copy)
        return cards

    def execute_document_completion_status(self, *, contract_id: str, as_of_date: str) -> dict[str, Any]:
        summary = self.repo.fetch_one(
            """
            SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN link_status = 'AUTO_LINKED' THEN 1 ELSE 0 END) AS auto_linked,
              SUM(CASE WHEN link_status = 'MANUAL_LINKED' THEN 1 ELSE 0 END) AS manual_linked,
              SUM(CASE WHEN link_status = 'REVIEW' THEN 1 ELSE 0 END) AS review,
              SUM(CASE WHEN link_status = 'BLOCKED' THEN 1 ELSE 0 END) AS blocked,
              SUM(CASE WHEN link_status = 'UNLINKED' THEN 1 ELSE 0 END) AS unlinked
            FROM evidence_originals
            WHERE contract_id = ?
            """,
            (contract_id,),
        ) or {}
        delivery_rows = self.repo.fetch_all(
            """
            SELECT delivery_id, delivery_date, status
            FROM deliveries
            WHERE contract_id = ?
            ORDER BY delivery_date ASC, created_at ASC
            """,
            (contract_id,),
        )
        evidence_rows = self.repo.fetch_all(
            """
            SELECT delivery_id, doc_type, link_status
            FROM evidence_originals
            WHERE contract_id = ?
            """,
            (contract_id,),
        )
        required_doc_types = {"waybill", "weighing_ticket", "coa", "supplier_invoice"}
        docs_by_delivery: dict[str, set[str]] = {}
        for row in evidence_rows:
            delivery_id = str(row.get("delivery_id") or "").strip()
            if not delivery_id:
                continue
            doc_type = str(row.get("doc_type") or "").strip().lower()
            if not doc_type:
                continue
            docs_by_delivery.setdefault(delivery_id, set()).add(doc_type)
        missing_prompts: list[dict[str, Any]] = []
        for row in delivery_rows:
            delivery_id = str(row.get("delivery_id") or "")
            present = docs_by_delivery.get(delivery_id, set())
            missing = sorted(required_doc_types - present)
            if missing:
                missing_prompts.append(
                    {
                        "delivery_id": delivery_id,
                        "delivery_date": str(row.get("delivery_date") or ""),
                        "delivery_status": str(row.get("status") or ""),
                        "missing_doc_types": missing,
                    }
                )
        return {
            "as_of_date": as_of_date,
            "summary": {
                "total": int(summary.get("total") or 0),
                "auto_linked": int(summary.get("auto_linked") or 0),
                "manual_linked": int(summary.get("manual_linked") or 0),
                "review": int(summary.get("review") or 0),
                "blocked": int(summary.get("blocked") or 0),
                "unlinked": int(summary.get("unlinked") or 0),
            },
            "missing_prompts": missing_prompts,
        }

    def document_completion_copilot(
        self,
        *,
        contract_id: str,
        as_of_date: str,
        autonomy_run_id: str | None = None,
        action_intent_id: str | None = None,
        source: str = "document-completion-copilot",
    ) -> dict[str, Any]:
        as_of = str(as_of_date or utc_today_iso()).strip()
        deliveries = self.repo.fetch_all(
            """
            SELECT delivery_id, delivery_ref, run_id, batch_id, delivery_date
            FROM deliveries
            WHERE contract_id = ?
            ORDER BY delivery_date ASC, created_at ASC, delivery_id ASC
            """,
            (contract_id,),
        )
        sales_rows = self.repo.fetch_all(
            """
            SELECT st.sales_transaction_id, st.delivery_id, sl.sales_line_id
            FROM sales_transactions st
            JOIN sales_lines sl ON sl.sales_transaction_id = st.sales_transaction_id
            WHERE st.contract_id = ?
            ORDER BY sl.line_no ASC, sl.sales_line_id ASC
            """,
            (contract_id,),
        )
        docs_rows = self.repo.fetch_all(
            """
            SELECT doc_id, delivery_id, sales_transaction_id, doc_type, doc_number
            FROM documents
            WHERE delivery_id IN (
              SELECT delivery_id FROM deliveries WHERE contract_id = ?
            )
            ORDER BY generated_at DESC
            """,
            (contract_id,),
        )
        evidence_rows = self.repo.fetch_all(
            """
            SELECT *
            FROM evidence_originals
            WHERE contract_id = ?
            ORDER BY created_at ASC, evidence_id ASC
            """,
            (contract_id,),
        )
        sales_by_delivery: dict[str, dict[str, str]] = {}
        for row in sales_rows:
            delivery_id = str(row.get("delivery_id") or "").strip()
            if not delivery_id or delivery_id in sales_by_delivery:
                continue
            sales_by_delivery[delivery_id] = {
                "sales_transaction_id": str(row.get("sales_transaction_id") or ""),
                "sales_line_id": str(row.get("sales_line_id") or ""),
            }
        doc_number_map: dict[str, dict[str, str]] = {}
        for row in docs_rows:
            number_norm = _norm_token(str(row.get("doc_number") or ""))
            if not number_norm:
                continue
            doc_number_map[number_norm] = {
                "delivery_id": str(row.get("delivery_id") or ""),
                "sales_transaction_id": str(row.get("sales_transaction_id") or ""),
                "doc_type": str(row.get("doc_type") or "").upper(),
            }
        delivery_index = {
            str(row.get("delivery_id") or ""): dict(row)
            for row in deliveries
            if str(row.get("delivery_id") or "").strip()
        }
        now = utc_now_iso_z()
        auto_linked = 0
        review_cases = 0
        blocker_cases = 0
        processed = 0
        for row in evidence_rows:
            evidence_id = str(row.get("evidence_id") or "").strip()
            if not evidence_id:
                continue
            filename = str(row.get("file_name") or Path(str(row.get("source_path") or "")).name).strip()
            if not filename:
                filename = Path(str(row.get("stored_path") or "")).name
            filename_norm = _norm_token(filename)
            doc_type = str(row.get("doc_type") or "").strip().lower() or _doc_type_from_filename(filename).lower()
            existing_status = str(row.get("link_status") or "UNLINKED").strip().upper()
            if existing_status in {"AUTO_LINKED", "MANUAL_LINKED"} and str(row.get("delivery_id") or "").strip():
                continue
            processed += 1

            candidates: list[dict[str, Any]] = []
            explicit_delivery_id = str(row.get("delivery_id") or "").strip()
            if explicit_delivery_id and explicit_delivery_id in delivery_index:
                delivery_info = delivery_index[explicit_delivery_id]
                sales_ctx = sales_by_delivery.get(explicit_delivery_id, {})
                candidates.append(
                    {
                        "delivery_id": explicit_delivery_id,
                        "sales_transaction_id": str(row.get("sales_transaction_id") or sales_ctx.get("sales_transaction_id") or ""),
                        "sales_line_id": str(row.get("sales_line_id") or sales_ctx.get("sales_line_id") or ""),
                        "confidence": 1.0,
                        "reason_bits": ["existing_delivery"],
                    }
                )
            else:
                for delivery in deliveries:
                    delivery_id = str(delivery.get("delivery_id") or "")
                    score = 0.0
                    reason_bits: list[str] = []
                    run_norm = _norm_token(str(delivery.get("run_id") or ""))
                    batch_norm = _norm_token(str(delivery.get("batch_id") or ""))
                    ref_norm = _norm_token(str(delivery.get("delivery_ref") or ""))
                    if run_norm and run_norm in filename_norm:
                        score += 0.55
                        reason_bits.append("run_id_match")
                    if batch_norm and batch_norm in filename_norm:
                        score += 0.55
                        reason_bits.append("batch_id_match")
                    if ref_norm and ref_norm in filename_norm:
                        score += 0.40
                        reason_bits.append("delivery_ref_match")
                    if _norm_token(delivery_id) and _norm_token(delivery_id) in filename_norm:
                        score += 0.45
                        reason_bits.append("delivery_id_match")
                    if len(deliveries) == 1:
                        score += 0.20
                        reason_bits.append("single_delivery_context")
                    sales_ctx = sales_by_delivery.get(delivery_id, {})
                    candidates.append(
                        {
                            "delivery_id": delivery_id,
                            "sales_transaction_id": str(sales_ctx.get("sales_transaction_id") or ""),
                            "sales_line_id": str(sales_ctx.get("sales_line_id") or ""),
                            "confidence": min(1.0, round(score, 4)),
                            "reason_bits": reason_bits,
                        }
                    )

            expected_doc_type = {
                "waybill": "WAYBILL",
                "weighing_ticket": "WEIGHING_TICKET",
                "weighing": "WEIGHING_TICKET",
                "coa": "COA",
                "supplier_invoice": "INVOICE",
                "invoice": "INVOICE",
            }.get(doc_type)
            for doc_no_norm, doc_info in doc_number_map.items():
                if not doc_no_norm or doc_no_norm not in filename_norm:
                    continue
                if expected_doc_type and str(doc_info.get("doc_type") or "").upper() != expected_doc_type:
                    continue
                delivery_id = str(doc_info.get("delivery_id") or "")
                if not delivery_id:
                    continue
                matched = None
                for candidate in candidates:
                    if str(candidate.get("delivery_id") or "") == delivery_id:
                        matched = candidate
                        break
                if matched is None:
                    sales_ctx = sales_by_delivery.get(delivery_id, {})
                    matched = {
                        "delivery_id": delivery_id,
                        "sales_transaction_id": str(doc_info.get("sales_transaction_id") or sales_ctx.get("sales_transaction_id") or ""),
                        "sales_line_id": str(sales_ctx.get("sales_line_id") or ""),
                        "confidence": 0.0,
                        "reason_bits": [],
                    }
                    candidates.append(matched)
                matched["confidence"] = min(1.0, round(float(matched.get("confidence") or 0.0) + 0.80, 4))
                reason_bits = matched.get("reason_bits") if isinstance(matched.get("reason_bits"), list) else []
                if "doc_number_match" not in reason_bits:
                    reason_bits.append("doc_number_match")
                matched["reason_bits"] = reason_bits
                if not str(matched.get("sales_transaction_id") or "").strip():
                    matched["sales_transaction_id"] = str(doc_info.get("sales_transaction_id") or "")
                if not str(matched.get("sales_line_id") or "").strip():
                    sales_ctx = sales_by_delivery.get(delivery_id, {})
                    matched["sales_line_id"] = str(sales_ctx.get("sales_line_id") or "")

            candidates = [candidate for candidate in candidates if str(candidate.get("delivery_id") or "").strip()]
            candidates.sort(
                key=lambda item: (
                    -float(item.get("confidence") or 0.0),
                    str(item.get("delivery_id") or ""),
                )
            )
            top = candidates[0] if candidates else None
            second = candidates[1] if len(candidates) > 1 else None
            top_confidence = float(top.get("confidence") or 0.0) if isinstance(top, dict) else 0.0
            ambiguous = bool(
                top
                and second
                and abs(top_confidence - float(second.get("confidence") or 0.0)) < 0.05
                and top_confidence >= 0.70
            )
            top_reasons = top.get("reason_bits") if isinstance(top, dict) and isinstance(top.get("reason_bits"), list) else []
            strong_signal = any(reason in {"existing_delivery", "run_id_match", "batch_id_match", "doc_number_match"} for reason in top_reasons)

            if top and top_confidence >= 0.93 and strong_signal and not ambiguous:
                with self.repo.transaction() as conn:
                    conn.execute(
                        """
                        UPDATE evidence_originals
                        SET delivery_id = ?,
                            sales_transaction_id = ?,
                            sales_line_id = ?,
                            file_name = COALESCE(NULLIF(file_name, ''), ?),
                            doc_type = ?,
                            link_status = 'AUTO_LINKED',
                            link_confidence = ?,
                            link_reason_code = ?,
                            link_source = ?,
                            linked_at = ?,
                            updated_at = ?
                        WHERE evidence_id = ?
                        """,
                        (
                            str(top.get("delivery_id") or ""),
                            str(top.get("sales_transaction_id") or "") or None,
                            str(top.get("sales_line_id") or "") or None,
                            filename,
                            doc_type,
                            top_confidence,
                            "strong_match",
                            source,
                            now,
                            now,
                            evidence_id,
                        ),
                    )
                    self.repo.append_event(
                        conn,
                        entity_type="EVIDENCE",
                        entity_id=evidence_id,
                        event_type="EVIDENCE_AUTO_LINKED",
                        as_of_date=as_of,
                        payload={
                            "delivery_id": str(top.get("delivery_id") or ""),
                            "sales_transaction_id": str(top.get("sales_transaction_id") or ""),
                            "sales_line_id": str(top.get("sales_line_id") or ""),
                            "doc_type": doc_type,
                            "confidence": top_confidence,
                            "reason_bits": top_reasons,
                        },
                        source=source,
                    )
                auto_linked += 1
                continue

            if not top:
                reason_code = "doc_link_no_match"
                severity = "BLOCKER"
                target_status = "BLOCKED"
            elif len(candidates) > 1 and top_confidence <= 0.0:
                reason_code = "doc_link_ambiguous"
                severity = "BLOCKER"
                target_status = "BLOCKED"
            elif ambiguous:
                reason_code = "doc_link_ambiguous"
                severity = "BLOCKER"
                target_status = "BLOCKED"
            elif top_confidence >= 0.75:
                reason_code = "doc_link_partial"
                severity = "REVIEW"
                target_status = "REVIEW"
            else:
                reason_code = "doc_link_low_confidence"
                severity = "REVIEW"
                target_status = "REVIEW"
            if severity == "BLOCKER":
                blocker_cases += 1
            else:
                review_cases += 1

            case_payload = {
                "evidence_id": evidence_id,
                "contract_id": contract_id,
                "as_of_date": as_of,
                "doc_type": doc_type,
                "file_name": filename,
                "top_confidence": top_confidence,
                "reason_code": reason_code,
                "candidates": candidates[:3],
                "selected_candidate": top or {},
            }
            case_key = f"doc-link|{evidence_id}|{reason_code}|{as_of}"
            target_delivery_id = str(top.get("delivery_id") or "").strip() if isinstance(top, dict) else ""
            with self.repo.transaction() as conn:
                conn.execute(
                    """
                    UPDATE evidence_originals
                    SET file_name = COALESCE(NULLIF(file_name, ''), ?),
                        doc_type = ?,
                        link_status = ?,
                        link_confidence = ?,
                        link_reason_code = ?,
                        link_source = ?,
                        updated_at = ?
                    WHERE evidence_id = ?
                    """,
                    (
                        filename,
                        doc_type,
                        target_status,
                        top_confidence if top else 0.0,
                        reason_code,
                        source,
                        now,
                        evidence_id,
                    ),
                )
                case_row = self.repo.create_or_get_exception_case(
                    conn,
                    autonomy_run_id=autonomy_run_id,
                    action_intent_id=action_intent_id,
                    contract_id=contract_id,
                    delivery_id=target_delivery_id or None,
                    planned_delivery_id=None,
                    case_type="document_linkage",
                    severity=severity,
                    reason_code=reason_code,
                    details=case_payload,
                    idempotency_key=case_key,
                )
                self.repo.add_decision_feature(
                    conn,
                    exception_case_id=str(case_row["exception_case_id"]),
                    feature_key="document_link_candidates",
                    feature_payload=case_payload,
                )
                self.repo.append_event(
                    conn,
                    entity_type="EXCEPTION_CASE",
                    entity_id=str(case_row["exception_case_id"]),
                    event_type="CASE_OPENED",
                    as_of_date=as_of,
                    payload={
                        "reason_code": reason_code,
                        "evidence_id": evidence_id,
                    },
                    source=source,
                )

        status = self.execute_document_completion_status(contract_id=contract_id, as_of_date=as_of)
        return {
            "ok": True,
            "contract_id": contract_id,
            "as_of_date": as_of,
            "processed": processed,
            "auto_linked": auto_linked,
            "review_cases": review_cases,
            "blocker_cases": blocker_cases,
            "status": status,
        }

    def exception_case_cards(
        self,
        *,
        status: str = "OPEN",
        as_of_date_utc: str | None = None,
        contract_id: str | None = None,
        case_type: str | None = None,
    ) -> list[dict[str, Any]]:
        as_of_date = str(as_of_date_utc or utc_today_iso()).strip()
        as_of_dt = datetime.fromisoformat(f"{as_of_date}T23:59:59+00:00")
        rows = self.repo.list_exception_cases(
            status=status,
            case_type=case_type,
            contract_id=contract_id,
        )
        contract_ids = sorted({str(row.get("contract_id") or "") for row in rows if str(row.get("contract_id") or "")})
        open_counts: dict[str, int] = {}
        contract_states: dict[str, str] = {}
        if contract_ids:
            placeholders = ",".join(["?"] * len(contract_ids))
            for count_row in self.repo.fetch_all(
                f"""
                SELECT contract_id, COUNT(*) AS open_count
                FROM exception_cases
                WHERE status = 'OPEN' AND contract_id IN ({placeholders})
                GROUP BY contract_id
                """,
                tuple(contract_ids),
            ):
                open_counts[str(count_row.get("contract_id") or "")] = int(count_row.get("open_count") or 0)
            for state_row in self.repo.fetch_all(
                f"""
                SELECT contract_id, lpo_state
                FROM contracts
                WHERE contract_id IN ({placeholders})
                """,
                tuple(contract_ids),
            ):
                contract_states[str(state_row.get("contract_id") or "")] = str(state_row.get("lpo_state") or "")

        cards: list[dict[str, Any]] = []
        for row in rows:
            details_json = row.get("details_json")
            try:
                details = json.loads(details_json or "{}")
            except Exception:
                details = {"raw": str(details_json)}
            created_at = str(row.get("created_at") or "")
            created_dt = self._parse_utc_z(created_at)
            age_hours = (
                round(max((as_of_dt - created_dt).total_seconds(), 0.0) / 3600.0, 2)
                if created_dt is not None
                else 0.0
            )
            severity = str(row.get("severity") or "REVIEW").upper()
            sla_target_hours = {"BLOCKER": 4, "REVIEW": 24, "INFO": 72}.get(severity, 24)
            if age_hours >= float(sla_target_hours):
                sla_state = "BREACHED"
            elif age_hours >= float(sla_target_hours) * 0.75:
                sla_state = "AT_RISK"
            else:
                sla_state = "WITHIN"
            case_contract_id = str(row.get("contract_id") or "")
            resume_as_of = str(details.get("as_of_date") or as_of_date)
            consequence_preview = {
                "resume_supported": bool(case_contract_id),
                "resume_as_of_date": resume_as_of,
                "recommended_decision": "APPROVE" if severity in {"REVIEW", "INFO"} else "OVERRIDE",
                "expected_next_action": (
                    f"run-autonomy --as-of {resume_as_of} --contract-id {case_contract_id}"
                    if case_contract_id
                    else "No contract-linked resume path"
                ),
                "open_cases_for_contract": int(open_counts.get(case_contract_id, 0)),
                "contract_lpo_state": str(contract_states.get(case_contract_id, "")),
            }
            cards.append(
                {
                    "exception_case_id": str(row.get("exception_case_id") or ""),
                    "contract_id": case_contract_id,
                    "case_type": str(row.get("case_type") or ""),
                    "severity": severity,
                    "status": str(row.get("status") or ""),
                    "reason_code": str(row.get("reason_code") or ""),
                    "created_at": created_at,
                    "updated_at": str(row.get("updated_at") or ""),
                    "age_hours": age_hours,
                    "sla_target_hours": sla_target_hours,
                    "sla_state": sla_state,
                    "details": details,
                    "consequence_preview": consequence_preview,
                }
            )
        cards.sort(key=lambda item: (item["severity"], item["reason_code"], item["created_at"]))
        return cards

    def exception_case_activity(self, *, case_id: str) -> list[dict[str, Any]]:
        rows = self.repo.fetch_all(
            """
            SELECT event_id, event_type, payload_json, source, created_at
            FROM event_log
            WHERE entity_type = 'EXCEPTION_CASE'
              AND entity_id = ?
            ORDER BY created_at ASC, event_id ASC
            """,
            (case_id,),
        )
        activity: list[dict[str, Any]] = []
        for row in rows:
            payload_json = row.get("payload_json")
            try:
                payload = json.loads(payload_json or "{}")
            except Exception:
                payload = {"raw": str(payload_json)}
            activity.append(
                {
                    "event_id": str(row.get("event_id") or ""),
                    "event_type": str(row.get("event_type") or ""),
                    "source": str(row.get("source") or ""),
                    "created_at": str(row.get("created_at") or ""),
                    "payload": payload,
                }
            )
        return activity

    def _parse_utc_z(self, value: str) -> datetime | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            if text.endswith("Z"):
                return datetime.fromisoformat(text.replace("Z", "+00:00"))
            parsed = datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except Exception:
            return None

    def decide_exception_case(
        self,
        *,
        case_id: str,
        decision: str,
        reason: str,
        resume: bool,
        dry_run_resume: bool,
    ) -> dict[str, Any]:
        from domain.automation import AutomationOrchestrator

        if not str(reason or "").strip():
            raise ValueError("reason is required")
        orchestrator = AutomationOrchestrator(self.config, self.repo, self)
        return orchestrator.decide_case(
            case_id=case_id,
            decision=decision,
            reason=reason,
            resume=resume,
            dry_run_resume=dry_run_resume,
        )

    def dashboard_rows(self, *, limit: int = 100) -> dict[str, list[dict[str, Any]]]:
        contracts = self.repo.fetch_all(
            """
            SELECT
              dc.contract_id,
              dc.contract_ref,
              dc.lpo_no,
              dc.buyer_id,
              dc.vendor_of_record_id,
              dc.lpo_state,
              dc.lpo_valid_from,
              dc.lpo_valid_to,
              c.lane,
              dc.status,
              dc.issue_date,
              dc.expected_total_qty_kg,
              dc.expected_total_qty_mt,
              dc.expected_total_qty,
              dc.expected_total_value,
              dc.delivered_qty_total_kg,
              dc.delivered_qty_total_mt,
              c.over_delivery_tolerance_pct
            FROM drep_contracts dc
            JOIN contracts c ON c.contract_id = dc.contract_id
            ORDER BY dc.created_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        deliveries = self.repo.fetch_all(
            """
            SELECT
              d.delivery_id,
              d.contract_id,
              d.delivery_ref,
              d.run_id,
              d.batch_id,
              d.delivery_date,
              d.delivered_qty,
              d.delivered_qty_kg,
              d.status,
              c.lpo_no,
              c.buyer_id,
              c.vendor_of_record_id
            FROM deliveries d
            JOIN contracts c ON c.contract_id = d.contract_id
            ORDER BY d.created_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        sales = self.repo.fetch_all(
            """
            SELECT
              sales_transaction_id,
              delivery_id,
              contract_id,
              invoice_no,
              invoice_date,
              due_date,
              buyer_id,
              vendor_of_record_id,
              amount_due,
              amount_paid_to_date,
              certified_withheld_amount,
              outstanding_balance
            FROM drep_sales
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        return {
            "contracts": contracts,
            "deliveries": deliveries,
            "sales": sales,
        }

    def planned_rows(self, *, limit: int = 250, contract_id: str | None = None) -> list[dict[str, Any]]:
        if contract_id:
            return self.repo.fetch_all(
                """
                SELECT
                  pd.planned_delivery_id,
                  pd.contract_id,
                  pd.contract_line_id,
                  pd.sequence_no,
                  pd.planned_qty_kg,
                  pd.lot_size_kg,
                  pd.planned_date,
                  pd.status,
                  pd.delivery_id,
                  pd.run_id,
                  pd.batch_id,
                  pd.updated_at,
                  c.contract_ref,
                  c.lpo_no,
                  c.lpo_state,
                  cli.product_code
                FROM planned_deliveries pd
                JOIN contracts c ON c.contract_id = pd.contract_id
                JOIN contract_line_items cli ON cli.contract_line_id = pd.contract_line_id
                WHERE pd.contract_id = ?
                ORDER BY pd.planned_date ASC, pd.sequence_no ASC
                LIMIT ?
                """,
                (contract_id, limit),
            )
        return self.repo.fetch_all(
            """
            SELECT
              pd.planned_delivery_id,
              pd.contract_id,
              pd.contract_line_id,
              pd.sequence_no,
              pd.planned_qty_kg,
              pd.lot_size_kg,
              pd.planned_date,
              pd.status,
              pd.delivery_id,
              pd.run_id,
              pd.batch_id,
              pd.updated_at,
              c.contract_ref,
              c.lpo_no,
              c.lpo_state,
              cli.product_code
            FROM planned_deliveries pd
            JOIN contracts c ON c.contract_id = pd.contract_id
            JOIN contract_line_items cli ON cli.contract_line_id = pd.contract_line_id
            ORDER BY pd.updated_at DESC
            LIMIT ?
            """,
            (limit,),
        )

    def update_planned_delivery(
        self,
        *,
        planned_delivery_id: str,
        planned_date: str | None = None,
        planned_qty_mt: float | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        with self.repo.transaction() as conn:
            row = conn.execute(
                """
                SELECT
                  pd.*,
                  c.lpo_state,
                  c.lpo_valid_from,
                  c.lpo_valid_to,
                  c.over_delivery_tolerance_pct,
                  cli.expected_qty_kg
                FROM planned_deliveries pd
                JOIN contracts c ON c.contract_id = pd.contract_id
                JOIN contract_line_items cli ON cli.contract_line_id = pd.contract_line_id
                WHERE pd.planned_delivery_id = ?
                """,
                (planned_delivery_id,),
            ).fetchone()
            if not row:
                raise ValueError(f"Unknown planned_delivery_id: {planned_delivery_id}")
            row = dict(row)
            if str(row.get("status") or "").upper() not in {"PLANNED", "SCHEDULED"}:
                raise ValueError("Only PLANNED/SCHEDULED rows can be edited")
            if str(row.get("lpo_state") or "").upper() != "ACTIVE":
                raise ValueError("Cannot edit plan for non-ACTIVE contract")

            resolved_date = (planned_date or str(row["planned_date"])).strip()
            if not resolved_date:
                raise ValueError("planned_date is required")
            valid_from = str(row.get("lpo_valid_from") or "").strip()
            valid_to = str(row.get("lpo_valid_to") or "").strip()
            if valid_from and resolved_date < valid_from:
                raise ValueError("planned_date is before lpo_valid_from")
            if valid_to and resolved_date > valid_to:
                raise ValueError("planned_date is after lpo_valid_to")

            resolved_qty_kg = int(row["planned_qty_kg"])
            if planned_qty_mt is not None:
                resolved_qty_kg = mt_to_kg_int(planned_qty_mt)
            if resolved_qty_kg <= 0:
                raise ValueError("planned quantity must be > 0")

            other_sum = conn.execute(
                """
                SELECT COALESCE(SUM(planned_qty_kg), 0) AS qty
                FROM planned_deliveries
                WHERE contract_line_id = ?
                  AND planned_delivery_id <> ?
                  AND status <> 'CANCELLED'
                """,
                (row["contract_line_id"], planned_delivery_id),
            ).fetchone()
            expected_qty_kg = int(row.get("expected_qty_kg") or 0)
            tolerance_pct = float(row.get("over_delivery_tolerance_pct") or 5.0)
            allowed_kg = int(expected_qty_kg * (1.0 + (tolerance_pct / 100.0)))
            new_total = int(other_sum["qty"] or 0) + resolved_qty_kg
            if expected_qty_kg > 0 and new_total > allowed_kg:
                raise ValueError(
                    f"planned quantity exceeds allowed tolerance: {new_total}kg > {allowed_kg}kg"
                )

            now = utc_now_iso_z()
            conn.execute(
                """
                UPDATE planned_deliveries
                SET planned_date = ?,
                    planned_qty_kg = ?,
                    notes = COALESCE(?, notes),
                    updated_at = ?
                WHERE planned_delivery_id = ?
                """,
                (resolved_date, resolved_qty_kg, notes, now, planned_delivery_id),
            )

            updated = conn.execute(
                "SELECT * FROM planned_deliveries WHERE planned_delivery_id = ?",
                (planned_delivery_id,),
            ).fetchone()
            return {
                "ok": True,
                "planned_delivery_id": planned_delivery_id,
                "planned_date": resolved_date,
                "planned_qty_kg": resolved_qty_kg,
                "planned_qty_mt": format(kg_to_mt_decimal(resolved_qty_kg), "f"),
                "status": updated["status"] if updated else row["status"],
            }

    def approve_planned_deliveries(self, *, contract_id: str) -> dict[str, Any]:
        with self.repo.transaction() as conn:
            now = utc_now_iso_z()
            contract_row = conn.execute(
                "SELECT lpo_state FROM contracts WHERE contract_id = ?",
                (contract_id,),
            ).fetchone()
            if not contract_row:
                raise ValueError(f"Unknown contract_id: {contract_id}")
            contract_row = dict(contract_row)
            if str(contract_row.get("lpo_state") or "").upper() != "ACTIVE":
                raise ValueError("Cannot approve plan for non-ACTIVE contract")
            pending_rows = conn.execute(
                """
                SELECT COUNT(*) AS total
                FROM planned_deliveries
                WHERE contract_id = ?
                  AND status = 'PLANNED'
                """,
                (contract_id,),
            ).fetchone()
            scheduled_count = int((pending_rows or {"total": 0})["total"] or 0)
            conn.execute(
                """
                UPDATE planned_deliveries
                SET status = 'SCHEDULED',
                    updated_at = ?
                WHERE contract_id = ?
                  AND status = 'PLANNED'
                """,
                (now, contract_id),
            )
            return {
                "ok": True,
                "contract_id": contract_id,
                "scheduled_count": scheduled_count,
            }

    def materialize_due_deliveries(
        self,
        *,
        contract_id: str,
        as_of_date: str,
        run_id: str | None = None,
        batch_id: str | None = None,
        auto_progress: bool = True,
        auto_record_coa: bool = True,
        auto_generate_pack: bool = True,
        allow_placeholder_tin: bool = True,
        original_docs: list[str] | None = None,
    ) -> dict[str, Any]:
        original_docs = original_docs or []
        refresh_date = str(as_of_date or utc_today_iso()).strip()
        self.refresh_contract_state(as_of_date=refresh_date)
        contract = self.repo.fetch_one("SELECT * FROM contracts WHERE contract_id = ?", (contract_id,))
        if not contract:
            raise ValueError(f"Unknown contract_id: {contract_id}")
        if str(contract.get("lpo_state") or "").upper() != "ACTIVE":
            raise ValueError("Materialization blocked: contract lpo_state must be ACTIVE")

        due_rows = self.repo.fetch_all(
            """
            SELECT * FROM planned_deliveries
            WHERE contract_id = ?
              AND status IN ('PLANNED', 'SCHEDULED')
              AND planned_date <= ?
            ORDER BY planned_date ASC, sequence_no ASC
            """,
            (contract_id, refresh_date),
        )
        if not due_rows:
            doc_completion = self.document_completion_copilot(
                contract_id=contract_id,
                as_of_date=refresh_date,
            )
            return {
                "ok": True,
                "contract_id": contract_id,
                "processed": [],
                "message": "No due planned deliveries",
                "document_completion": doc_completion,
            }

        processed: list[dict[str, Any]] = []
        for row in due_rows:
            planned_delivery_id = str(row["planned_delivery_id"])
            try:
                materialized = self.materialize_delivery(
                    planned_delivery_id=planned_delivery_id,
                    run_id=run_id,
                    batch_id=batch_id,
                    qty_mt=None,
                    as_of_date=refresh_date,
                )
                entry: dict[str, Any] = {"planned_delivery_id": planned_delivery_id, "materialize": materialized}
                delivery_id = str(materialized["delivery_id"])
                if auto_progress:
                    entry["mark_dispatched"] = self.mark_dispatched(delivery_id)
                    entry["mark_delivered"] = self.mark_delivered(delivery_id)
                if auto_record_coa:
                    coa_payload = self.coa_template_for_delivery(delivery_id, default_result="PASS")
                    entry["record_coa"] = self.record_coa(coa_payload)
                if auto_generate_pack:
                    entry["generate_pack"] = self.generate_pack(
                        delivery_id=delivery_id,
                        allow_placeholder_tin=allow_placeholder_tin,
                        skip_pdf=False,
                        original_docs=original_docs,
                    )
                processed.append(entry)
            except Exception as error:
                return {
                    "ok": False,
                    "contract_id": contract_id,
                    "processed": processed,
                    "blocked_planned_delivery_id": planned_delivery_id,
                    "error": str(error),
                }
        doc_completion = self.document_completion_copilot(
            contract_id=contract_id,
            as_of_date=refresh_date,
        )
        return {
            "ok": True,
            "contract_id": contract_id,
            "processed": processed,
            "document_completion": doc_completion,
        }

    def coa_template_for_delivery(self, delivery_id: str, *, default_result: str = "PASS") -> dict[str, Any]:
        bundle = self.repo.get_delivery_bundle(delivery_id)
        line = self.repo.fetch_one(
            "SELECT product_code FROM contract_line_items WHERE contract_line_id = ?",
            (bundle["contract_line_id"],),
        )
        if not line:
            raise ValueError("Unable to resolve product_code from contract line")
        product_code = str(line["product_code"]).upper()
        buyer_party = self.config.registry.get(str(bundle["buyer_id"]))
        buyer_group = infer_buyer_group(str(bundle["buyer_id"]), buyer_party.name)
        profile = _resolve_coa_profile_for_ui(
            self.config.coa_profiles,
            buyer_group=buyer_group,
            product_code=product_code,
        )
        rows = []
        for row in profile.get("quality_parameters", []):
            rows.append(
                {
                    "parameter": str(row.get("parameter", "")),
                    "standard": str(row.get("standard", "")),
                    "result": default_result,
                }
            )
        return {
            "delivery_id": delivery_id,
            "buyer_group": buyer_group,
            "product_code": product_code,
            "run_id": str(bundle["run_id"]),
            "batch_id": str(bundle["batch_id"]),
            "profile_key": f"{buyer_group}:{product_code}",
            "profile_version": str(profile.get("coa_version_no") or profile.get("version") or "01"),
            "results": rows,
        }

    def capture_evidence_original(
        self,
        *,
        contract_id: str,
        source_path: Path,
        delivery_id: str | None = None,
        sales_transaction_id: str | None = None,
    ) -> dict[str, Any]:
        source_path = source_path.expanduser().resolve()
        if not source_path.exists():
            raise FileNotFoundError(f"Evidence file not found: {source_path}")
        file_hash = sha256_file(source_path)
        file_name = source_path.name
        doc_type = _doc_type_from_filename(file_name)
        now = utc_now_iso_z()
        existing = self.repo.fetch_one(
            """
            SELECT * FROM evidence_originals
            WHERE contract_id = ? AND sha256 = ?
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (contract_id, file_hash),
        )
        if existing:
            with self.repo.transaction() as conn:
                conn.execute(
                    """
                    UPDATE evidence_originals
                    SET delivery_id = COALESCE(delivery_id, ?),
                        sales_transaction_id = COALESCE(sales_transaction_id, ?),
                        doc_type = COALESCE(NULLIF(TRIM(doc_type), ''), ?),
                        file_name = COALESCE(file_name, ?),
                        updated_at = ?
                    WHERE evidence_id = ?
                    """,
                    (
                        delivery_id,
                        sales_transaction_id,
                        doc_type.lower(),
                        file_name,
                        now,
                        str(existing["evidence_id"]),
                    ),
                )
            row = self.repo.fetch_one("SELECT * FROM evidence_originals WHERE evidence_id = ?", (str(existing["evidence_id"]),))
            return {
                "evidence_id": str(row["evidence_id"]),
                "source_path": str(row["source_path"]),
                "stored_path": str(row["stored_path"]),
                "sha256": str(row["sha256"]),
                "captured_at": str(row["captured_at"]),
                "deduped": True,
                "doc_type": str(row.get("doc_type") or "").upper(),
            }
        dest_dir = self.config.state_dir / "evidence" / "originals" / contract_id
        next_index = len(list(dest_dir.glob("*"))) + 1 if dest_dir.exists() else 1
        stored = persist_evidence_original(
            source_path=source_path,
            dest_dir=dest_dir,
            order_index=next_index,
        )
        with self.repo.transaction() as conn:
            conn.execute(
                """
                INSERT INTO evidence_originals(
                    evidence_id, contract_id, delivery_id, sales_transaction_id, sales_line_id,
                    file_name, doc_type, link_status, link_confidence, link_reason_code, link_source, linked_at,
                    source_path, stored_path, sha256, captured_at, created_at, updated_at
                )
                VALUES(?, ?, ?, ?, NULL, ?, ?, 'UNLINKED', NULL, NULL, NULL, NULL, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stored["evidence_id"],
                    contract_id,
                    delivery_id,
                    sales_transaction_id,
                    file_name,
                    doc_type.lower(),
                    stored["source_path"],
                    stored["stored_path"],
                    stored["sha256"],
                    stored["captured_at"],
                    now,
                    now,
                ),
            )
        return {
            **stored,
            "deduped": False,
            "doc_type": doc_type,
        }


def _resolve_coa_profile_for_ui(coa_profiles: dict[str, Any], *, buyer_group: str, product_code: str) -> dict[str, Any]:
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
            if isinstance(override, dict):
                selected.update(override)
            break
    return selected
