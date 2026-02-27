from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.pdf_layout import (
    render_coa_pdf,
    render_invoice_pdf,
    render_receipt_pdf,
    render_waybill_pdf,
    render_weighing_ticket_pdf,
)
from app.schemas import Entity, LineItem, Transaction
from core.config import RuntimeConfig
from core.enums import DocumentType
from core.hashing import canonical_json_sha256, sha256_bytes
from core.units import kg_to_mt_decimal, mt_to_kg_int


class PdfBridge:
    """Phase 1 bridge that reuses low-level PDF renderers only."""

    def __init__(self, config: RuntimeConfig) -> None:
        self.config = config

    def render_pack_documents(
        self,
        *,
        bundle: dict[str, Any],
        sales_transaction: dict[str, Any],
        sales_lines: list[dict[str, Any]],
        coa_record: dict[str, Any],
        doc_numbers: dict[DocumentType, str],
        skip_pdf: bool,
    ) -> list[dict[str, Any]]:
        tx = self._build_transaction(
            bundle=bundle,
            sales_transaction=sales_transaction,
            sales_lines=sales_lines,
            coa_record=coa_record,
            invoice_no=doc_numbers[DocumentType.INVOICE],
            waybill_no=doc_numbers[DocumentType.WAYBILL],
            weighing_no=doc_numbers[DocumentType.WEIGHING_TICKET],
            coa_no=doc_numbers[DocumentType.COA],
            receipt_no="",
        )
        tax = {
            "vat_rate": tx.vat_rate,
            "wht_rate": tx.wht_rate,
            "vat_basis": "configured transaction rule",
            "wht_basis": "configured transaction rule",
            "vat_exemption_reason": tx.vat_exemption_reason or "",
            "wht_exemption_reason": tx.wht_exemption_reason or "",
        }
        rendered: list[dict[str, Any]] = []
        operator = self._entity_from_party_id(self.config.system_profile.operator_entity_id)
        order: list[tuple[DocumentType, str, Any]] = [
            (DocumentType.WAYBILL, "waybill", lambda: render_waybill_pdf(self.config.root_dir, tx, self.config.system_profile, operator)),
            (
                DocumentType.WEIGHING_TICKET,
                "weighing_ticket",
                lambda: render_weighing_ticket_pdf(self.config.root_dir, tx, self.config.system_profile, operator),
            ),
            (DocumentType.COA, "coa", lambda: render_coa_pdf(self.config.root_dir, tx, self.config.system_profile, operator)),
            (
                DocumentType.INVOICE,
                "invoice",
                lambda: render_invoice_pdf(
                    self.config.root_dir,
                    tx,
                    self.config.system_profile,
                    operator,
                    tax,
                    title="Goods Invoice",
                ),
            ),
        ]
        for doc_type, key, renderer in order:
            payload = self._doc_content_payload(doc_type=doc_type, tx=tx, sales_transaction=sales_transaction, sales_lines=sales_lines)
            content_sha = canonical_json_sha256(payload)
            if skip_pdf:
                rendered.append(
                    {
                        "doc_type": doc_type.value,
                        "doc_number": doc_numbers[doc_type],
                        "filename": f"{doc_numbers[doc_type]}.pdf",
                        "pdf_bytes": None,
                        "pdf_sha256": None,
                        "content_sha256": content_sha,
                        "content_payload": payload,
                    }
                )
                continue
            pdf_bytes = renderer()
            rendered.append(
                {
                    "doc_type": doc_type.value,
                    "doc_number": doc_numbers[doc_type],
                    "filename": f"{doc_numbers[doc_type]}.pdf",
                    "pdf_bytes": pdf_bytes,
                    "pdf_sha256": sha256_bytes(pdf_bytes),
                    "content_sha256": content_sha,
                    "content_payload": payload,
                }
            )
        return rendered

    def render_receipt_document(
        self,
        *,
        bundle: dict[str, Any],
        sales_transaction: dict[str, Any],
        sales_lines: list[dict[str, Any]],
        coa_record: dict[str, Any],
        receipt_no: str,
        invoice_no: str,
        skip_pdf: bool = False,
    ) -> dict[str, Any]:
        tx = self._build_transaction(
            bundle=bundle,
            sales_transaction=sales_transaction,
            sales_lines=sales_lines,
            coa_record=coa_record,
            invoice_no=invoice_no,
            waybill_no="",
            weighing_no="",
            coa_no=coa_record.get("coa_no", ""),
            receipt_no=receipt_no,
        )
        payload = self._doc_content_payload(
            doc_type=DocumentType.RECEIPT,
            tx=tx,
            sales_transaction=sales_transaction,
            sales_lines=sales_lines,
        )
        content_sha = canonical_json_sha256(payload)
        operator = self._entity_from_party_id(self.config.system_profile.operator_entity_id)
        if skip_pdf:
            return {
                "doc_type": DocumentType.RECEIPT.value,
                "doc_number": receipt_no,
                "filename": f"{receipt_no}.pdf",
                "pdf_bytes": None,
                "pdf_sha256": None,
                "content_sha256": content_sha,
                "content_payload": payload,
            }
        pdf_bytes = render_receipt_pdf(self.config.root_dir, tx, self.config.system_profile, operator)
        return {
            "doc_type": DocumentType.RECEIPT.value,
            "doc_number": receipt_no,
            "filename": f"{receipt_no}.pdf",
            "pdf_bytes": pdf_bytes,
            "pdf_sha256": sha256_bytes(pdf_bytes),
            "content_sha256": content_sha,
            "content_payload": payload,
        }

    def _build_transaction(
        self,
        *,
        bundle: dict[str, Any],
        sales_transaction: dict[str, Any],
        sales_lines: list[dict[str, Any]],
        coa_record: dict[str, Any],
        invoice_no: str,
        waybill_no: str,
        weighing_no: str,
        coa_no: str,
        receipt_no: str,
    ) -> Transaction:
        buyer = self._entity_from_party_id(str(bundle["buyer_id"]))
        vendor = self._entity_from_party_id(str(bundle["vendor_of_record_id"]))
        source = self._entity_from_party_id(str(bundle.get("source_id") or bundle["vendor_of_record_id"]))
        processor = self._entity_from_party_id(str(bundle.get("processor_id") or bundle["vendor_of_record_id"]))
        line_items: list[LineItem] = []
        for line in sales_lines:
            quantity_kg = int(line.get("quantity_kg") or self._to_quantity_kg(line))
            quantity_mt = float(kg_to_mt_decimal(quantity_kg))
            unit_norm = str(line.get("unit") or "").strip().lower()
            unit_price = float(line["unit_price"])
            if unit_norm in {"kg", "kgs", "kilogram", "kilograms"}:
                unit_price = unit_price * 1000.0
            line_items.append(
                LineItem(
                    description=str(line["description"]),
                    quantity=quantity_mt,
                    unit="MT",
                    unit_price=unit_price,
                    amount=float(line["gross_amount"]),
                    tax_class="agri_input",
                )
            )
        quality_parameters = json.loads(coa_record.get("results_json") or "[]")
        due_date = str(sales_transaction.get("due_date") or bundle.get("due_date") or sales_transaction["invoice_date"])
        tx = Transaction(
            invoice_date=str(sales_transaction["invoice_date"]),
            due_date=due_date,
            currency=str(sales_transaction["currency"]),
            payment_terms=str(bundle.get("due_terms") or ""),
            lpo_no=str(bundle["lpo_no"]),
            lpo_date=str(bundle.get("lpo_date") or ""),
            mode="normal_trade",
            funding_mode="none",
            agreement_ref=str(bundle["contract_ref"]),
            order_type="Raw Materials",
            order_sub_type="Refined Oil",
            product_name=str(sales_lines[0]["description"]) if sales_lines else "Goods",
            product_code=str(sales_lines[0]["product_code"]) if sales_lines else "",
            material_identification_code=str(sales_lines[0]["product_code"]) if sales_lines else "",
            batch_id=str(bundle["batch_id"]),
            run_id=str(bundle["run_id"]),
            manufacture_date=str(bundle["delivery_date"]),
            expiry_date="",
            coa_document_no=str(coa_record.get("profile_key") or ""),
            coa_version_no=str(coa_record.get("profile_version") or "01"),
            coa_template_name="Certificate of Analysis",
            delivery_terms="DAP",
            packaging="Bulk",
            truck_no=str(bundle.get("truck_no") or ""),
            driver_name=str(bundle.get("driver_name") or ""),
            driver_phone=str(bundle.get("driver_phone") or ""),
            buyer=buyer,
            ship_to=buyer,
            processor=processor,
            vendor_of_record=vendor,
            source=source,
            funder=None,
            line_items=line_items,
            transport_charges=0.0,
            installation_charges=0.0,
            vat_rate=0.0,
            discount=0.0,
            wht_rate=0.0,
            wht_applicable=False,
            vat_exemption_reason="",
            wht_exemption_reason="",
            notes=str(bundle.get("notes") or ""),
            quality_parameters=quality_parameters,
            allocation_reference="",
            deal_id=str(sales_transaction["sales_transaction_id"]),
            buyer_id=str(bundle["buyer_id"]),
            processor_id=str(bundle.get("processor_id") or ""),
            vendor_of_record_id=str(bundle["vendor_of_record_id"]),
            source_id=str(bundle.get("source_id") or ""),
            funder_id="",
            producer_posture=False,
            use_collection_account=True,
            evidence_level="self_attested",
            original_docs=[],
            include_originals_in_customer_pack=False,
            supplier_draft_only=False,
            invoice_no=invoice_no,
            waybill_no=waybill_no,
            weighing_no=weighing_no,
            receipt_no=receipt_no,
            coa_no=coa_no,
        )
        return tx

    def _entity_from_party_id(self, party_id: str) -> Entity:
        return self.config.registry.get(party_id)

    def _to_quantity_kg(self, sales_line: dict[str, Any]) -> int:
        unit = str(sales_line.get("unit") or "").strip().lower()
        quantity = float(sales_line.get("quantity") or 0.0)
        if unit in {"kg", "kgs", "kilogram", "kilograms"}:
            return int(round(quantity))
        if unit in {"mt", "ton", "tons", "tonne", "tonnes"}:
            return mt_to_kg_int(quantity)
        return int(round(quantity))

    def _doc_content_payload(
        self,
        *,
        doc_type: DocumentType,
        tx: Transaction,
        sales_transaction: dict[str, Any],
        sales_lines: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "schema_version": "phase1.v1",
            "doc_type": doc_type.value,
            "doc_number": {
                DocumentType.WAYBILL: tx.waybill_no,
                DocumentType.WEIGHING_TICKET: tx.weighing_no,
                DocumentType.COA: tx.coa_no,
                DocumentType.INVOICE: tx.invoice_no,
                DocumentType.RECEIPT: tx.receipt_no,
            }[doc_type],
            "invoice_no": tx.invoice_no,
            "lpo_no": tx.lpo_no,
            "run_id": tx.run_id,
            "batch_id": tx.batch_id,
            "buyer_id": tx.buyer_id,
            "vendor_of_record_id": tx.vendor_of_record_id,
            "sales_transaction_id": sales_transaction["sales_transaction_id"],
            "currency": sales_transaction["currency"],
            "gross_amount": sales_transaction["gross_amount"],
            "lines": [
                {
                    "line_no": line["line_no"],
                    "product_code": line["product_code"],
                    "description": line["description"],
                    "quantity": line["quantity"],
                    "quantity_kg": int(line.get("quantity_kg") or self._to_quantity_kg(line)),
                    "quantity_mt": format(kg_to_mt_decimal(int(line.get("quantity_kg") or self._to_quantity_kg(line))), "f"),
                    "unit": line["unit"],
                    "unit_price": line["unit_price"],
                    "gross_amount": line["gross_amount"],
                }
                for line in sales_lines
            ],
        }
