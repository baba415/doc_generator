from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


ALLOWED_MODES = {"normal_trade", "contract_processing_pilot"}
ALLOWED_FUNDING_MODES = {
    "none",
    "murabaha_supplier_direct",
    "direct_facility_to_merchant",
    "controlled_disbursement_on_behalf_of_merchant",
    "receivables_finance",
    "hybrid_topup",
}

ALLOWED_EVIDENCE_LEVELS = {"missing", "self_attested", "supplier_acknowledged"}


@dataclass
class BankAccount:
    bank_name: str
    account_name: str
    account_number: str
    currency: str = "NGN"
    sort_code: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BankAccount":
        return cls(
            bank_name=str(data.get("bank_name", "")).strip(),
            account_name=str(data.get("account_name", "")).strip(),
            account_number=str(data.get("account_number", "")).strip(),
            currency=str(data.get("currency", "NGN")).strip() or "NGN",
            sort_code=str(data.get("sort_code", "")).strip(),
        )


@dataclass
class Entity:
    name: str
    address: str = ""
    contact_person: str = ""
    city_state_country: str = ""
    rc_number: str = ""
    tin: str = ""
    website: str = ""
    phones: List[str] = field(default_factory=list)
    emails: List[str] = field(default_factory=list)
    bank: Optional[BankAccount] = None
    entity_id: str = ""
    code: str = ""
    aliases: List[str] = field(default_factory=list)

    @property
    def phone(self) -> str:
        return self.phones[0] if self.phones else ""

    @property
    def email(self) -> str:
        return self.emails[0] if self.emails else ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Entity":
        phones: List[str] = []
        raw_phones = data.get("phones")
        if isinstance(raw_phones, list):
            phones = [str(item).strip() for item in raw_phones if str(item).strip()]
        else:
            phone = str(data.get("phone", "")).strip()
            if phone:
                phones = [phone]

        emails: List[str] = []
        raw_emails = data.get("emails")
        if isinstance(raw_emails, list):
            emails = [str(item).strip() for item in raw_emails if str(item).strip()]
        else:
            email = str(data.get("email", "")).strip()
            if email:
                emails = [email]

        bank_payload = data.get("bank")
        bank = BankAccount.from_dict(bank_payload) if isinstance(bank_payload, dict) else None

        name = str(data.get("name") or data.get("legal_name") or "").strip()
        return cls(
            entity_id=str(data.get("entity_id", "")).strip(),
            code=str(data.get("code", "")).strip(),
            name=name,
            address=str(data.get("address", "")).strip(),
            contact_person=str(data.get("contact_person", "")).strip(),
            city_state_country=str(data.get("city_state_country", "")).strip(),
            rc_number=str(data.get("rc_number", "")).strip(),
            tin=str(data.get("tin", "")).strip(),
            website=str(data.get("website", "")).strip(),
            phones=phones,
            emails=emails,
            bank=bank,
            aliases=[str(item).strip() for item in data.get("aliases", []) if str(item).strip()] if isinstance(data.get("aliases"), list) else [],
        )


@dataclass
class SystemProfile:
    operator_entity_id: str
    default_use_collection_account: bool = True
    collection_account: Optional[BankAccount] = None
    collection_notice_template: str = ""
    logo_path: Optional[str] = None
    seal_path: Optional[str] = None
    signature_path: Optional[str] = None
    allow_vendor_on_behalf_generation: bool = False
    allow_mode_c_supplier_draft: bool = True

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SystemProfile":
        assets = data.get("assets", {}) if isinstance(data.get("assets"), dict) else {}
        features = data.get("features", {}) if isinstance(data.get("features"), dict) else {}
        return cls(
            operator_entity_id=str(data.get("operator_entity_id", "")).strip(),
            default_use_collection_account=bool(data.get("default_use_collection_account", True)),
            collection_account=BankAccount.from_dict(data.get("collection_account", {})) if isinstance(data.get("collection_account"), dict) else None,
            collection_notice_template=str(data.get("collection_notice_template", "")).strip(),
            logo_path=assets.get("logo_path"),
            seal_path=assets.get("seal_path"),
            signature_path=assets.get("signature_path"),
            allow_vendor_on_behalf_generation=bool(features.get("allow_vendor_on_behalf_generation", False)),
            allow_mode_c_supplier_draft=bool(features.get("allow_mode_c_supplier_draft", True)),
        )


@dataclass
class LineItem:
    description: str
    quantity: float
    unit: str
    unit_price: float
    amount: float
    tax_class: str = "agri_input"

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LineItem":
        payload = dict(data or {})
        quantity = float(payload.get("quantity", 0.0) or 0.0)
        unit_price = float(payload.get("unit_price", 0.0) or 0.0)
        if payload.get("amount", None) in (None, ""):
            payload["amount"] = round(quantity * unit_price, 2)
        amount = float(payload.get("amount", 0.0) or 0.0)
        payload["quantity"] = quantity
        payload["unit_price"] = unit_price
        payload["amount"] = amount
        if not payload.get("tax_class"):
            payload["tax_class"] = "agri_input"
        return cls(**payload)


@dataclass
class Transaction:
    invoice_date: str
    due_date: str
    currency: str
    payment_terms: str
    lpo_no: str
    lpo_date: str
    mode: str
    funding_mode: str
    agreement_ref: str
    order_type: str
    order_sub_type: str
    product_name: str
    product_code: str
    material_identification_code: str
    batch_id: str
    run_id: str
    manufacture_date: str
    expiry_date: str
    coa_document_no: str
    coa_version_no: str
    coa_template_name: str
    delivery_terms: str
    packaging: str
    truck_no: str
    driver_name: str
    driver_phone: str
    buyer: Entity
    ship_to: Entity
    processor: Entity
    vendor_of_record: Entity
    source: Optional[Entity]
    funder: Optional[Entity]
    line_items: List[LineItem]
    transport_charges: float
    installation_charges: float
    vat_rate: float
    discount: float
    wht_rate: float
    wht_applicable: bool
    vat_exemption_reason: str
    wht_exemption_reason: str
    notes: str
    quality_parameters: List[Dict[str, str]] = field(default_factory=list)
    allocation_reference: str = ""
    deal_id: str = ""
    buyer_id: str = ""
    processor_id: str = ""
    vendor_of_record_id: str = ""
    source_id: str = ""
    funder_id: str = ""
    producer_posture: bool = False
    use_collection_account: bool = True
    evidence_level: str = "self_attested"
    original_docs: List[str] = field(default_factory=list)
    include_originals_in_customer_pack: bool = False
    supplier_draft_only: bool = False
    invoice_no: str = ""
    waybill_no: str = ""
    weighing_no: str = ""
    receipt_no: str = ""
    coa_no: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Transaction":
        buyer_payload = data.get("buyer") or {}
        buyer = Entity.from_dict(buyer_payload) if isinstance(buyer_payload, dict) else Entity(name=str(buyer_payload))

        ship_to_payload = data.get("ship_to") or {}
        ship_to = Entity.from_dict(ship_to_payload) if isinstance(ship_to_payload, dict) else Entity(name=str(ship_to_payload))

        processor_payload = data.get("processor") or {}
        processor = Entity.from_dict(processor_payload) if isinstance(processor_payload, dict) else Entity(name=str(processor_payload))

        vendor_payload = data.get("vendor_of_record") or data.get("supplier") or {}
        vendor_of_record = Entity.from_dict(vendor_payload) if isinstance(vendor_payload, dict) else Entity(name=str(vendor_payload))

        source_payload = data.get("source")
        source = Entity.from_dict(source_payload) if isinstance(source_payload, dict) else (Entity(name=str(source_payload)) if source_payload else None)

        funder_data = data.get("funder")
        funder = Entity.from_dict(funder_data) if isinstance(funder_data, dict) else (Entity(name=str(funder_data)) if funder_data else None)
        line_items = [LineItem.from_dict(item) for item in data.get("line_items", [])]
        tx = cls(
            invoice_date=data["invoice_date"],
            due_date=data["due_date"],
            currency=data.get("currency", "NGN"),
            payment_terms=data.get("payment_terms", ""),
            lpo_no=data.get("lpo_no", ""),
            lpo_date=data.get("lpo_date", ""),
            mode=data.get("mode", "normal_trade"),
            funding_mode=data.get("funding_mode", "none"),
            agreement_ref=data.get("agreement_ref", ""),
            order_type=data.get("order_type", ""),
            order_sub_type=data.get("order_sub_type", ""),
            product_name=data.get("product_name", ""),
            product_code=data.get("product_code", ""),
            material_identification_code=data.get("material_identification_code", ""),
            batch_id=data.get("batch_id", ""),
            run_id=data.get("run_id", ""),
            manufacture_date=data.get("manufacture_date", ""),
            expiry_date=data.get("expiry_date", ""),
            coa_document_no=data.get("coa_document_no", ""),
            coa_version_no=data.get("coa_version_no", ""),
            coa_template_name=data.get("coa_template_name", ""),
            delivery_terms=data.get("delivery_terms", ""),
            packaging=data.get("packaging", ""),
            truck_no=data.get("truck_no", ""),
            driver_name=data.get("driver_name", ""),
            driver_phone=data.get("driver_phone", ""),
            buyer=buyer,
            ship_to=ship_to,
            processor=processor,
            vendor_of_record=vendor_of_record,
            source=source,
            funder=funder,
            line_items=line_items,
            transport_charges=float(data.get("transport_charges", 0.0)),
            installation_charges=float(data.get("installation_charges", 0.0)),
            vat_rate=float(data.get("vat_rate", 0.0)),
            discount=float(data.get("discount", 0.0)),
            wht_rate=float(data.get("wht_rate", 0.0)),
            wht_applicable=bool(data.get("wht_applicable", False)),
            vat_exemption_reason=data.get("vat_exemption_reason", ""),
            wht_exemption_reason=data.get("wht_exemption_reason", ""),
            notes=data.get("notes", ""),
            quality_parameters=data.get("quality_parameters", []),
            allocation_reference=data.get("allocation_reference", ""),
            deal_id=str(data.get("deal_id", "")).strip(),
            buyer_id=str(data.get("buyer_id", "")).strip(),
            processor_id=str(data.get("processor_id", "")).strip(),
            vendor_of_record_id=str(data.get("vendor_of_record_id", "")).strip(),
            source_id=str(data.get("source_id", "")).strip(),
            funder_id=str(data.get("funder_id", "")).strip(),
            producer_posture=bool(data.get("producer_posture", False)),
            use_collection_account=bool(data.get("use_collection_account", True)),
            evidence_level=str(data.get("evidence_level", "self_attested")).strip() or "self_attested",
            original_docs=[str(path).strip() for path in data.get("original_docs", []) if str(path).strip()] if isinstance(data.get("original_docs"), list) else [],
            include_originals_in_customer_pack=bool(data.get("include_originals_in_customer_pack", False)),
            supplier_draft_only=bool(data.get("supplier_draft_only", False)),
            invoice_no=data.get("invoice_no", ""),
            waybill_no=data.get("waybill_no", ""),
            weighing_no=data.get("weighing_no", ""),
            receipt_no=data.get("receipt_no", ""),
            coa_no=data.get("coa_no", ""),
        )
        tx.validate()
        return tx

    def validate(self) -> None:
        if self.mode not in ALLOWED_MODES:
            raise ValueError(f"Unsupported mode: {self.mode}")
        if self.funding_mode not in ALLOWED_FUNDING_MODES:
            raise ValueError(f"Unsupported funding mode: {self.funding_mode}")
        if not self.line_items:
            raise ValueError("At least one line item is required")
        if self.mode == "contract_processing_pilot" and not self.run_id:
            raise ValueError("run_id is required in contract_processing_pilot mode")
        if self.funding_mode == "murabaha_supplier_direct" and not (self.funder_id or (self.funder and self.funder.name)):
            raise ValueError("funder_id (or funder) is required for murabaha_supplier_direct")
        if not (self.vendor_of_record_id or self.vendor_of_record.name):
            raise ValueError("vendor_of_record_id or vendor_of_record is required")
        if not (self.buyer_id or self.buyer.name):
            raise ValueError("buyer_id or buyer is required")
        if self.evidence_level not in ALLOWED_EVIDENCE_LEVELS:
            raise ValueError(f"Unsupported evidence_level: {self.evidence_level}")
        if self.producer_posture:
            if not self.run_id or not self.batch_id:
                raise ValueError("producer_posture requires run_id and batch_id")
            if self.mode != "contract_processing_pilot":
                raise ValueError("producer_posture requires contract_processing_pilot mode in v1")

    def subtotal(self) -> float:
        return round(sum(item.amount for item in self.line_items), 2)

    def vat_amount(self) -> float:
        return round(self.subtotal() * (self.vat_rate / 100), 2)

    def total_before_wht(self) -> float:
        return round(self.subtotal() + self.transport_charges + self.installation_charges + self.vat_amount() - self.discount, 2)

    def wht_amount(self) -> float:
        if not self.wht_applicable:
            return 0.0
        return round(self.total_before_wht() * (self.wht_rate / 100), 2)

    def grand_total(self) -> float:
        return round(self.total_before_wht() - self.wht_amount(), 2)

    def to_manifest_dict(self) -> Dict[str, Any]:
        return {
            "deal_id": self.deal_id,
            "invoice_no": self.invoice_no,
            "mode": self.mode,
            "funding_mode": self.funding_mode,
            "batch_id": self.batch_id,
            "run_id": self.run_id,
            "buyer_id": self.buyer_id,
            "vendor_of_record_id": self.vendor_of_record_id,
            "source_id": self.source_id,
            "processor_id": self.processor_id,
            "funder_id": self.funder_id,
            "producer_posture": self.producer_posture,
            "use_collection_account": self.use_collection_account,
            "evidence_level": self.evidence_level,
            "include_originals_in_customer_pack": self.include_originals_in_customer_pack,
            "original_docs_count": len(self.original_docs),
            "subtotal": self.subtotal(),
            "vat_amount": self.vat_amount(),
            "wht_amount": self.wht_amount(),
            "grand_total": self.grand_total(),
            "generated_at": datetime.utcnow().isoformat() + "Z",
        }


@dataclass
class GeneratedDocument:
    key: str
    txt_path: str
    pdf_path: str
    sha256: str


@dataclass
class GenerationResult:
    output_dir: str
    documents: List[GeneratedDocument]
    manifest_path: str
