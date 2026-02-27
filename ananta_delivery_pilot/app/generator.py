from __future__ import annotations

import shutil
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Dict, List, Tuple

from .entity_registry import EntityRegistry
from .numbering import NumberStore
from .pdf_layout import (
    render_allocation_note_pdf,
    render_coa_pdf,
    render_invoice_pdf,
    render_murabaha_schedule_pdf,
    render_pack_pdf,
    render_receipt_pdf,
    render_waybill_pdf,
    render_weighing_ticket_pdf,
)
from .pdf_merge import PdfMergeError, merge_pdfs
from .policies import FunderPolicyEngine, TaxDecisionEngine
from .schemas import Entity, GeneratedDocument, GenerationResult, SystemProfile, Transaction
from .templates_engine import (
    build_allocation_note_text,
    build_coa_text,
    build_invoice_text,
    build_murabaha_schedule_text,
    build_pack_text,
    build_receipt_text,
    build_waybill_text,
    build_weighing_ticket_text,
)
from .utils import ensure_dir, read_json, safe_write_text, sha256_file, utc_timestamp, write_json


class DeliveryPackGenerator:
    def __init__(self, root_dir: Path) -> None:
        self.root_dir = root_dir
        self.config_dir = root_dir / "config"
        self.output_root = root_dir / "output"
        self.state_dir = root_dir / ".state"
        ensure_dir(self.output_root)
        ensure_dir(self.state_dir)

        self.number_store = NumberStore(self.state_dir / "numbering.json")
        self.tax_engine = TaxDecisionEngine(self.config_dir / "tax_rules.json")
        self.policy_engine = FunderPolicyEngine(self.config_dir / "funders")
        self.coa_profiles = read_json(self.config_dir / "coa_profiles.json")
        self.entity_registry = EntityRegistry.load_many(
            [
                self.config_dir / "entities.json",
                self.config_dir / "entities.local.json",
            ]
        )

    def load_system_profile(self) -> SystemProfile:
        profile_data = read_json(self.config_dir / "system_profile.json")
        return SystemProfile.from_dict(profile_data)

    def generate_from_file(self, transaction_json_path: Path, funder_policy_file: str | None = None) -> GenerationResult:
        payload = read_json(transaction_json_path)
        return self.generate(payload, funder_policy_file=funder_policy_file)

    def validate_from_file(self, transaction_json_path: Path, funder_policy_file: str | None = None) -> Dict[str, object]:
        payload = read_json(transaction_json_path)
        return self.validate(payload, funder_policy_file=funder_policy_file)

    def validate(self, payload: Dict, funder_policy_file: str | None = None) -> Dict[str, object]:
        system = self.load_system_profile()
        tx = Transaction.from_dict(payload)
        operator = self.entity_registry.get(system.operator_entity_id)

        errors: List[str] = []
        warnings: List[str] = []

        try:
            self._resolve_entities(tx)
        except KeyError as error:
            errors.append(str(error))

        product_key = (tx.product_code or "").upper().strip()
        if product_key and product_key not in self.coa_profiles:
            warnings.append(f"No COA profile found for product_code '{product_key}'. COA rows may be blank.")

        lane = self._lane_for_vendor(tx.vendor_of_record_id)
        if lane == "C" and not system.allow_vendor_on_behalf_generation:
            if tx.supplier_draft_only:
                if not system.allow_mode_c_supplier_draft:
                    errors.append("Mode C draft invoice requested but allow_mode_c_supplier_draft is disabled.")
            else:
                warnings.append("Mode C: invoice will not be generated (external on-behalf is disabled). Attach supplier originals; set supplier_draft_only=true for internal draft if needed.")

        missing_originals = [path for path in tx.original_docs if not Path(path).expanduser().exists()]
        if missing_originals:
            errors.append(f"Original docs not found: {', '.join(missing_originals)}")

        policy_violations: List[str] = []
        if funder_policy_file and tx.funder:
            policy = self.policy_engine.load_policy(funder_policy_file)
            policy_result = self.policy_engine.check_transaction(tx, policy)
            if not policy_result.ok:
                policy_violations.extend(policy_result.violations)

        return {
            "ok": len(errors) == 0,
            "lane": lane,
            "vendor_of_record_entity": tx.vendor_of_record.name,
            "operator_entity": operator.name,
            "errors": errors,
            "warnings": warnings,
            "policy_violations": policy_violations,
            "transaction": tx.to_manifest_dict(),
        }

    def generate(self, payload: Dict, funder_policy_file: str | None = None) -> GenerationResult:
        system = self.load_system_profile()
        tx = Transaction.from_dict(payload)
        operator = self.entity_registry.get(system.operator_entity_id)
        self._resolve_entities(tx)
        if not tx.deal_id:
            tx.deal_id = uuid.uuid4().hex
        self._apply_coa_profile(tx)

        self._attach_numbers(tx)
        tax_outcome = self.tax_engine.apply(tx)

        policy_violations: List[str] = []
        if funder_policy_file and tx.funder:
            policy = self.policy_engine.load_policy(funder_policy_file)
            policy_result = self.policy_engine.check_transaction(tx, policy)
            if not policy_result.ok:
                policy_violations.extend(policy_result.violations)

        vendor_code = tx.vendor_of_record.code or "VENDOR"
        output_dir = ensure_dir(self.output_root / vendor_code / tx.invoice_no)
        evidence_originals: List[Dict[str, str]] = []
        if tx.original_docs:
            originals_dir = ensure_dir(output_dir / "evidence" / "originals")
            for idx, raw_path in enumerate(tx.original_docs, start=1):
                src = Path(raw_path)
                if not src.is_absolute():
                    src = (self.root_dir / raw_path).resolve()
                if not src.exists():
                    raise FileNotFoundError(f"Original doc not found: {raw_path}")
                dest = originals_dir / f"{idx:02d}-{src.name}"
                shutil.copy2(src, dest)
                evidence_originals.append(
                    {
                        "source_path": raw_path,
                        "stored_path": str(dest),
                        "sha256": sha256_file(dest),
                    }
                )
            if tx.evidence_level == "self_attested":
                tx.evidence_level = "supplier_acknowledged"

        docs: List[Tuple[str, str, str, bytes]] = []

        lane = self._lane_for_vendor(tx.vendor_of_record_id)
        is_mode_c = lane == "C"

        include_invoice = True
        invoice_draft = False
        invoice_doc_key = "invoice"
        invoice_base_name = f"PI-{tx.invoice_no}"
        invoice_title = "Commercial Invoice"
        invoice_watermark = ""

        if is_mode_c and not system.allow_vendor_on_behalf_generation:
            include_invoice = False
            if tx.supplier_draft_only:
                if not system.allow_mode_c_supplier_draft:
                    raise ValueError("Mode C draft invoice requested but allow_mode_c_supplier_draft is disabled.")
                invoice_draft = True
                invoice_doc_key = "invoice_draft"
                invoice_base_name = f"DRAFT-PI-{tx.invoice_no}"
                invoice_title = "Commercial Invoice (Draft)"
                invoice_watermark = "DRAFT"
                include_invoice = True

        if include_invoice:
            invoice_text = build_invoice_text(tx, system, operator, tax_outcome, draft=invoice_draft)
            invoice_pdf = render_invoice_pdf(
                self.root_dir,
                tx,
                system,
                operator,
                tax_outcome,
                title=invoice_title,
                internal_watermark=invoice_watermark,
            )
            docs.append((invoice_doc_key, invoice_base_name, invoice_text, invoice_pdf))

        waybill_text = build_waybill_text(tx, system, operator)
        waybill_pdf = render_waybill_pdf(self.root_dir, tx, system, operator)
        docs.append(("waybill", f"{tx.waybill_no}", waybill_text, waybill_pdf))

        weighing_text = build_weighing_ticket_text(tx, system, operator)
        weighing_pdf = render_weighing_ticket_pdf(self.root_dir, tx, system, operator)
        docs.append(("weighing_ticket", f"{tx.weighing_no}", weighing_text, weighing_pdf))

        coa_text = build_coa_text(tx, system, operator)
        coa_pdf = render_coa_pdf(self.root_dir, tx, system, operator)
        docs.append(("coa", f"{tx.coa_no}", coa_text, coa_pdf))

        receipt_text = build_receipt_text(tx, system, operator)
        receipt_pdf = render_receipt_pdf(self.root_dir, tx, system, operator)
        docs.append(("receipt", f"{tx.receipt_no}", receipt_text, receipt_pdf))

        if tx.mode == "contract_processing_pilot":
            allocation_text = build_allocation_note_text(tx, system, operator)
            allocation_pdf = render_allocation_note_pdf(self.root_dir, tx, system, operator)
            docs.append(("allocation_note", f"ALLOC-{tx.run_id or tx.invoice_no}", allocation_text, allocation_pdf))

        if tx.funding_mode == "murabaha_supplier_direct":
            murabaha_text = build_murabaha_schedule_text(tx, system, operator)
            murabaha_pdf = render_murabaha_schedule_pdf(self.root_dir, tx, system, operator)
            docs.append(("murabaha_schedule", f"MURABAHA-{tx.invoice_no}", murabaha_text, murabaha_pdf))

        generated_docs: List[GeneratedDocument] = []
        pack_text_sections: List[str] = []
        pack_entries = [f"{base_name}.pdf" for _, base_name, _, _ in docs]

        for key, base_name, text, pdf_data in docs:
            txt_path = output_dir / f"{base_name}.txt"
            pdf_path = output_dir / f"{base_name}.pdf"
            safe_write_text(txt_path, text)
            pdf_path.write_bytes(pdf_data)
            digest = sha256_file(pdf_path)
            generated_docs.append(
                GeneratedDocument(
                    key=key,
                    txt_path=str(txt_path),
                    pdf_path=str(pdf_path),
                    sha256=digest,
                )
            )
            pack_text_sections.append(text)

        pack_text = build_pack_text(pack_text_sections, tx, system, operator)
        pack_txt_path = output_dir / f"PACK-{tx.invoice_no}.txt"
        pack_pdf_path = output_dir / f"PACK-{tx.invoice_no}.pdf"
        safe_write_text(pack_txt_path, pack_text)
        pack_index_pdf = render_pack_pdf(self.root_dir, tx, system, operator, tax_outcome, entries=pack_entries)
        pack_pdf_bytes = pack_index_pdf
        pack_combined = False
        try:
            pack_pdf_bytes = merge_pdfs([pack_index_pdf, *[pdf for _, _, _, pdf in docs]])
            pack_combined = True
        except PdfMergeError:
            # Keep a usable index PDF even if PDF merge deps are missing.
            pack_pdf_bytes = pack_index_pdf

        pack_pdf_path.write_bytes(pack_pdf_bytes)
        pack_hash = sha256_file(pack_pdf_path)
        generated_docs.append(
            GeneratedDocument(
                key="pack",
                txt_path=str(pack_txt_path),
                pdf_path=str(pack_pdf_path),
                sha256=pack_hash,
            )
        )

        manifest = {
            "schema_version": "1.2",
            "generated_at": utc_timestamp(),
            "lane": self._lane_for_vendor(tx.vendor_of_record_id),
            "vendor_of_record_entity": tx.vendor_of_record.name,
            "operator_entity": operator.name,
            "transaction": tx.to_manifest_dict(),
            "tax_outcome": tax_outcome,
            "policy_violations": policy_violations,
            "documents": [asdict(doc) for doc in generated_docs],
            "pack_combined": pack_combined,
            "collection_account": asdict(system.collection_account) if system.collection_account else {},
            "collection_notice": system.collection_notice_template.format(
                operator_name=operator.name,
                vendor_of_record_name=tx.vendor_of_record.name,
            )
            if tx.use_collection_account and system.collection_notice_template and tx.vendor_of_record_id != system.operator_entity_id
            else "",
            "evidence": {
                "evidence_level": tx.evidence_level,
                "include_originals_in_customer_pack": tx.include_originals_in_customer_pack,
                "originals": evidence_originals,
            },
        }

        manifest_path = output_dir / "manifest.json"
        write_json(manifest_path, manifest)

        return GenerationResult(
            output_dir=str(output_dir),
            documents=generated_docs,
            manifest_path=str(manifest_path),
        )

    def _attach_numbers(self, tx: Transaction) -> None:
        if not tx.invoice_no:
            vendor_code = tx.vendor_of_record.code or "VENDOR"
            tx.invoice_no = self.number_store.next_invoice_no(vendor_code=vendor_code, year=tx.invoice_date[:4])
        if not tx.waybill_no:
            tx.waybill_no = self.number_store.waybill_no(tx.invoice_no, seq=1)
        if not tx.weighing_no:
            tx.weighing_no = self.number_store.weighing_no(tx.waybill_no, seq=1)
        if not tx.receipt_no:
            vendor_code = tx.vendor_of_record.code or "VENDOR"
            tx.receipt_no = self.number_store.next_receipt_no(vendor_code=vendor_code, invoice_no=tx.invoice_no, year=tx.invoice_date[:4])
        if not tx.coa_no:
            tx.coa_no = self.number_store.next_coa_no(tx.batch_id)

    def _resolve_entities(self, tx: Transaction) -> None:
        tx.buyer_id, tx.buyer = self._resolve_entity_pair(tx.buyer_id, tx.buyer)
        tx.vendor_of_record_id, tx.vendor_of_record = self._resolve_entity_pair(tx.vendor_of_record_id, tx.vendor_of_record)
        if not tx.vendor_of_record.code:
            tx.vendor_of_record.code = self._derive_code(tx.vendor_of_record.name)

        if tx.source_id or tx.source:
            tx.source_id, resolved = self._resolve_entity_pair(tx.source_id, tx.source or Entity(name=""))
            tx.source = resolved
        else:
            tx.source = tx.vendor_of_record

        tx.processor_id, tx.processor = self._resolve_entity_pair(tx.processor_id, tx.processor)

        if tx.funder_id or tx.funder:
            tx.funder_id, tx.funder = self._resolve_entity_pair(tx.funder_id, tx.funder or Entity(name=""))

    def _resolve_entity_pair(self, entity_id: str, fallback: Entity) -> tuple[str, Entity]:
        if entity_id:
            return entity_id, self.entity_registry.get(entity_id)
        resolved_id = self.entity_registry.resolve_id(fallback.name)
        if resolved_id:
            return resolved_id, self.entity_registry.get(resolved_id)
        return entity_id, fallback

    @staticmethod
    def _derive_code(name: str) -> str:
        raw = "".join(ch for ch in (name or "").upper() if ch.isalnum())
        return (raw[:10] or "VENDOR").upper()

    @staticmethod
    def _lane_for_vendor(vendor_of_record_id: str) -> str:
        if vendor_of_record_id == "guildgate":
            return "A"
        if vendor_of_record_id == "ananta_flows":
            return "B"
        return "C"

    def _apply_coa_profile(self, tx: Transaction) -> None:
        product_key = (tx.product_code or "").upper().strip()
        profile = self.coa_profiles.get(product_key)
        if not profile:
            return

        def normalized(value: str) -> str:
            return "".join(ch.lower() for ch in value if ch.isalnum())

        selected_profile = dict(profile)
        buyer_key = normalized(tx.buyer_id or tx.buyer.name)
        overrides = profile.get("buyer_overrides", {})
        for override_key, override_payload in overrides.items():
            key = normalized(str(override_key))
            if key and (buyer_key == key or key in buyer_key):
                selected_profile = {**profile, **override_payload}
                break

        if not tx.material_identification_code:
            tx.material_identification_code = selected_profile.get("material_identification_code", "")
        if not tx.coa_document_no:
            tx.coa_document_no = selected_profile.get("coa_document_no", "")
        if not tx.coa_version_no:
            tx.coa_version_no = selected_profile.get("coa_version_no", "")
        if not tx.coa_template_name:
            tx.coa_template_name = selected_profile.get("coa_template_name", "")

        profile_rows = selected_profile.get("quality_parameters", [])
        if not profile_rows:
            return

        incoming_by_key: Dict[str, Dict] = {}
        for row in tx.quality_parameters:
            key = normalized(str(row.get("parameter", "")))
            if key:
                incoming_by_key[key] = row

        merged: List[Dict] = []
        used_keys: set[str] = set()
        for row in profile_rows:
            parameter = str(row.get("parameter", "")).strip()
            if not parameter:
                continue
            key = normalized(parameter)
            existing = incoming_by_key.get(key, {})
            used_keys.add(key)
            merged.append(
                {
                    "parameter": parameter,
                    "standard": str(existing.get("standard") or row.get("standard") or "").strip(),
                    "result": str(existing.get("result") or row.get("result") or "").strip(),
                }
            )

        for row in tx.quality_parameters:
            parameter = str(row.get("parameter", "")).strip()
            key = normalized(parameter)
            if not key or key in used_keys:
                continue
            merged.append(
                {
                    "parameter": parameter,
                    "standard": str(row.get("standard", "")).strip(),
                    "result": str(row.get("result", "")).strip(),
                }
            )

        tx.quality_parameters = merged
