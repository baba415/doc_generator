from __future__ import annotations

import json
from datetime import date, timedelta
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
from core.hashing import sha256_file
from core.ids import new_ulid
from core.manifest import build_manifest, write_manifest
from core.time import utc_now_iso_z, utc_today_iso
from core.units import kg_to_mt_decimal, mt_to_kg_int
from domain.validators import ensure_vendor_tin


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
        policy = self.config.delivery_policies or {}
        planning_cfg = policy.get("planning", {}) if isinstance(policy.get("planning"), dict) else {}
        include_weekends = bool(planning_cfg.get("include_weekends", True))
        if max_lots_per_day is None:
            max_lots_per_day = int(planning_cfg.get("default_max_lots_per_day", 1))
        if max_lots_per_day <= 0:
            raise ValueError("max_lots_per_day must be >= 1")
        policy_version = str(policy.get("policy_version") or policy.get("schema_version") or "phase1_6.v1")
        start_value = (start_date or "").strip()
        with self.repo.transaction() as conn:
            contract = conn.execute("SELECT * FROM contracts WHERE contract_id = ?", (contract_id,)).fetchone()
            if not contract:
                raise ValueError(f"Unknown contract_id: {contract_id}")
            contract = dict(contract)
            if str(contract.get("lpo_state") or "ACTIVE").upper() in {"CANCELLED", "EXPIRED"}:
                raise ValueError("Cannot plan deliveries for CANCELLED/EXPIRED contract")
            start_iso = start_value or str(contract.get("lpo_valid_from") or contract.get("issue_date") or utc_today_iso())
            idempotency_key = f"{contract_id}|{start_iso}|{cadence_norm}|{max_lots_per_day}|{policy_version}"
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
            tolerance_pct = float(contract.get("over_delivery_tolerance_pct") or 5.0)
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
                "cadence": cadence_norm,
                "start_date": start_iso,
                "max_lots_per_day": max_lots_per_day,
                "planned_count": len(rows_out),
                "planned_total_kg": total_planned_kg,
                "planned_total_mt": format(kg_to_mt_decimal(total_planned_kg), "f"),
                "planned_deliveries": rows_out,
            }
            self.repo.save_idempotent_response(
                conn,
                command_name="plan-deliveries",
                idempotency_key=idempotency_key,
                response=response,
            )
            return response

    def materialize_delivery(
        self,
        *,
        planned_delivery_id: str,
        run_id: str | None = None,
        batch_id: str | None = None,
        qty_mt: float | None = None,
    ) -> dict[str, Any]:
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
                conn.execute(
                    """
                    INSERT INTO evidence_originals(
                        evidence_id, contract_id, delivery_id, sales_transaction_id, source_path, stored_path,
                        sha256, captured_at, created_at
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        stored["evidence_id"],
                        bundle["contract_id"],
                        delivery_id,
                        sales_transaction_id,
                        stored["source_path"],
                        stored["stored_path"],
                        stored["sha256"],
                        stored["captured_at"],
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

    def export_drep(self, *, as_of_date: str, out_dir: Path) -> dict[str, Any]:
        exports = export_drep_views(self.repo, as_of_date=as_of_date, out_dir=out_dir)
        return {
            "ok": True,
            "as_of_date": as_of_date,
            "exports": exports,
        }

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
            (contract_id, as_of_date),
        )
        if not due_rows:
            return {"ok": True, "contract_id": contract_id, "processed": [], "message": "No due planned deliveries"}

        processed: list[dict[str, Any]] = []
        for row in due_rows:
            planned_delivery_id = str(row["planned_delivery_id"])
            try:
                materialized = self.materialize_delivery(
                    planned_delivery_id=planned_delivery_id,
                    run_id=run_id,
                    batch_id=batch_id,
                    qty_mt=None,
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

        return {"ok": True, "contract_id": contract_id, "processed": processed}

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
        dest_dir = self.config.state_dir / "evidence" / "originals" / contract_id
        next_index = len(list(dest_dir.glob("*"))) + 1 if dest_dir.exists() else 1
        stored = persist_evidence_original(
            source_path=source_path,
            dest_dir=dest_dir,
            order_index=next_index,
        )
        now = utc_now_iso_z()
        with self.repo.transaction() as conn:
            conn.execute(
                """
                INSERT INTO evidence_originals(
                    evidence_id, contract_id, delivery_id, sales_transaction_id, source_path, stored_path,
                    sha256, captured_at, created_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stored["evidence_id"],
                    contract_id,
                    delivery_id,
                    sales_transaction_id,
                    stored["source_path"],
                    stored["stored_path"],
                    stored["sha256"],
                    stored["captured_at"],
                    now,
                ),
            )
        return stored


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
