from __future__ import annotations

from typing import List

from .schemas import Entity, LineItem, SystemProfile, Transaction
from .utils import amount_to_words_naira, nfmt


def _line(char: str = "-", length: int = 96) -> str:
    return char * length


def _collection_notice(system: SystemProfile, operator: Entity, vendor: Entity) -> str:
    template = system.collection_notice_template or ""
    if not template:
        return ""
    if operator.entity_id and vendor.entity_id and operator.entity_id == vendor.entity_id:
        return ""
    normalized_operator = "".join(ch.lower() for ch in operator.name if ch.isalnum())
    normalized_vendor = "".join(ch.lower() for ch in vendor.name if ch.isalnum())
    if normalized_operator and normalized_operator == normalized_vendor:
        return ""
    return template.format(operator_name=operator.name, vendor_of_record_name=vendor.name)


def _header(title: str, operator: Entity, vendor: Entity, source: Entity | None) -> str:
    operator_phones = " / ".join(operator.phones)
    operator_emails = " / ".join(operator.emails)
    vendor_meta = f"TIN: {vendor.tin or '-'} | RC: {vendor.rc_number or '-'}"
    operator_meta = f"TIN: {operator.tin or '-'} | RC: {operator.rc_number or '-'}"
    source_line = f"Source/Producer (Traceability): {source.name}" if source and source.name else "Source/Producer (Traceability): -"
    return (
        f"Operated by: {operator.name}\n"
        f"{operator_meta}\n"
        f"Contacts: {operator_phones or '-'} | {operator_emails or '-'}\n"
        f"Vendor-of-record (Tax Supplier): {vendor.name}\n"
        f"{vendor_meta}\n"
        f"{source_line}\n"
        f"{_line()}\n"
        f"{title}\n"
        f"{_line()}\n"
    )


def _party_block(tx: Transaction) -> str:
    return (
        f"Buyer: {tx.buyer.name}\n"
        f"Buyer Address: {tx.buyer.address}\n"
        f"Ship To: {tx.ship_to.name} | {tx.ship_to.address}\n"
        f"Processor: {tx.processor.name}\n"
        f"Delivery Terms: {tx.delivery_terms} | Packaging: {tx.packaging}\n"
        f"Truck No: {tx.truck_no} | Driver: {tx.driver_name} ({tx.driver_phone})\n"
    )


def _item_table(items: List[LineItem]) -> str:
    rows = [
        f"{'#':<3}{'Description':<56}{'Qty':>10}{'Unit':>8}{'Rate':>10}{'Amount':>12}",
        _line(),
    ]
    for index, item in enumerate(items, start=1):
        rows.append(
            f"{index:<3}{item.description[:56]:<56}{item.quantity:>10,.2f}{item.unit:>8}{item.unit_price:>10,.2f}{item.amount:>12,.2f}"
        )
    rows.append(_line())
    return "\n".join(rows)


def build_invoice_text(tx: Transaction, system: SystemProfile, operator: Entity, tax: dict, *, draft: bool = False) -> str:
    title = "COMMERCIAL INVOICE (DRAFT)" if draft else "COMMERCIAL INVOICE"
    vendor = tx.vendor_of_record
    source = tx.source
    basis_line = "Invoice Basis: Processing/Allocation Pilot" if tx.mode == "contract_processing_pilot" else "Invoice Basis: Standard Trade"

    content = [
        _header(title, operator, vendor, source),
        f"Invoice No: {tx.invoice_no} | Date: {tx.invoice_date} | Due: {tx.due_date}",
        f"LPO No: {tx.lpo_no or 'N/A'} | LPO Date: {tx.lpo_date or 'N/A'}",
        f"Agreement Ref: {tx.agreement_ref or 'N/A'} | {basis_line}",
        f"Order Type: {tx.order_type} | Sub Type: {tx.order_sub_type}",
        f"Funding Mode: {tx.funding_mode}",
        f"Run ID: {tx.run_id or 'N/A'} | Batch ID: {tx.batch_id or 'N/A'}",
        _line(),
        _party_block(tx),
        _line(),
        _item_table(tx.line_items),
        f"Subtotal: {nfmt(tx.subtotal())} {tx.currency}",
        f"Transport Charges: {nfmt(tx.transport_charges)} {tx.currency}",
        f"Installation Charges: {nfmt(tx.installation_charges)} {tx.currency}",
        f"VAT ({tax['vat_rate']}%): {nfmt(tx.vat_amount())} {tx.currency}",
        f"Discount: {nfmt(tx.discount)} {tx.currency}",
        f"WHT ({tax['wht_rate']}% if applicable): {nfmt(tx.wht_amount())} {tx.currency}",
        f"TOTAL PAYABLE: {nfmt(tx.grand_total())} {tx.currency}",
        f"Amount in Words: {amount_to_words_naira(tx.grand_total())}",
        _line(),
        f"VAT Exemption Reason: {tax.get('vat_exemption_reason') or 'N/A'}",
        f"WHT Exemption Reason: {tax.get('wht_exemption_reason') or 'N/A'}",
    ]

    if tx.use_collection_account and system.collection_account:
        notice = _collection_notice(system, operator, vendor)
        content.extend(
            [
                _line(),
                f"Payment Instructions (Collection Account): {system.collection_account.account_name} / {system.collection_account.account_number} ({system.collection_account.bank_name})",
            ]
        )
        if notice:
            content.append(f"Collection Notice: {notice}")
    elif vendor.bank:
        content.extend(
            [
                _line(),
                f"Payment Instructions (Pay Vendor-of-record): {vendor.bank.account_name} / {vendor.bank.account_number} ({vendor.bank.bank_name})",
            ]
        )

    if tx.notes:
        content.extend([_line(), f"Notes: {tx.notes}"])

    return "\n".join(content) + "\n"


def build_waybill_text(tx: Transaction, system: SystemProfile, operator: Entity) -> str:
    return (
        _header("WAYBILL", operator, tx.vendor_of_record, tx.source)
        + f"Waybill No: {tx.waybill_no} | Date: {tx.invoice_date}\n"
        + f"Linked Invoice: {tx.invoice_no}\n"
        + f"LPO No: {tx.lpo_no or 'N/A'} | LPO Date: {tx.lpo_date or 'N/A'}\n"
        + f"Run ID: {tx.run_id or 'N/A'} | Batch ID: {tx.batch_id or 'N/A'}\n"
        + _line()
        + "\n"
        + _party_block(tx)
        + _line()
        + "\n"
        + _item_table(tx.line_items)
        + f"Total Quantity: {sum(item.quantity for item in tx.line_items):,.2f}\n"
        + _line()
        + "\nReceiver Signature: _____________________\n"
    )


def build_weighing_ticket_text(tx: Transaction, system: SystemProfile, operator: Entity) -> str:
    gross_qty = sum(item.quantity for item in tx.line_items)
    return (
        _header("WEIGHING TICKET", operator, tx.vendor_of_record, tx.source)
        + f"Weighing Ticket No: {tx.weighing_no}\n"
        + f"Linked Waybill: {tx.waybill_no}\n"
        + f"LPO Ref: {tx.lpo_no or 'N/A'}\n"
        + f"Date: {tx.invoice_date}\n"
        + _line()
        + "\n"
        + f"Material Name: {tx.product_name}\n"
        + f"Material Code: {tx.product_code}\n"
        + f"Batch ID: {tx.batch_id}\n"
        + f"Run ID: {tx.run_id or 'N/A'}\n"
        + f"Truck No: {tx.truck_no}\n"
        + f"Driver Name: {tx.driver_name}\n"
        + f"Customer: {tx.buyer.name}\n"
        + _line()
        + "\n"
        + f"Net Weight: {gross_qty:,.2f} {tx.line_items[0].unit}\n"
        + f"Checked By: _____________________\n"
    )


def build_coa_text(tx: Transaction, system: SystemProfile, operator: Entity) -> str:
    rows = [
        _header("CERTIFICATE OF ANALYSIS / QC CERTIFICATE", operator, tx.vendor_of_record, tx.source),
        f"Certificate Type: {tx.coa_template_name or 'Certificate of Analysis'}",
        f"Document No: {tx.coa_document_no or 'N/A'} | Version: {tx.coa_version_no or 'N/A'}",
        f"COA No: {tx.coa_no}",
        f"Linked Invoice: {tx.invoice_no}",
        f"LPO Ref: {tx.lpo_no or 'N/A'}",
        f"Batch ID: {tx.batch_id}",
        f"Run ID: {tx.run_id or 'N/A'}",
        f"Product: {tx.product_name} ({tx.product_code})",
        f"Manufacture Date: {tx.manufacture_date}",
        f"Expiry Date: {tx.expiry_date}",
        f"LPO Date: {tx.lpo_date or 'N/A'}",
        _line(),
        f"Customer: {tx.buyer.name}",
        f"Processor: {tx.processor.name}",
        f"Order Type: {tx.order_type or 'N/A'} | Order Sub Type: {tx.order_sub_type or 'N/A'}",
        _line(),
        f"{'Parameter':<38}{'Standard':<26}{'Result':<26}",
        _line(),
    ]

    for parameter in tx.quality_parameters:
        rows.append(
            f"{parameter.get('parameter','')[:38]:<38}{parameter.get('standard','')[:26]:<26}{parameter.get('result','')[:26]:<26}"
        )

    rows.extend(
        [
            _line(),
            f"QC Statement: Product passed analysis for batch {tx.batch_id} based on sampled run {tx.run_id or 'N/A'}.",
            "QC Signature: _____________________",
        ]
    )
    return "\n".join(rows) + "\n"


def build_receipt_text(tx: Transaction, system: SystemProfile, operator: Entity) -> str:
    return (
        _header("PAYMENT RECEIPT", operator, tx.vendor_of_record, tx.source)
        + f"Receipt No: {tx.receipt_no} | Date: {tx.invoice_date}\n"
        + f"Received From: {tx.buyer.name}\n"
        + f"Against Invoice: {tx.invoice_no}\n"
        + f"Amount Received: {nfmt(tx.grand_total())} {tx.currency}\n"
        + f"Amount in Words: {amount_to_words_naira(tx.grand_total())}\n"
        + _line()
        + "\n"
        + f"Payment Method: Bank Transfer\n"
        + f"Status: {'Fully Paid' if tx.grand_total() > 0 else 'N/A'}\n"
        + "Prepared By: _____________________\n"
    )


def build_allocation_note_text(tx: Transaction, system: SystemProfile, operator: Entity) -> str:
    qty = sum(item.quantity for item in tx.line_items)
    return (
        _header("ALLOCATION NOTE (PILOT)", operator, tx.vendor_of_record, tx.source)
        + f"Allocation Reference: {tx.allocation_reference or tx.run_id}\n"
        + f"Run ID: {tx.run_id}\n"
        + f"Batch ID: {tx.batch_id}\n"
        + f"Product: {tx.product_name}\n"
        + f"Input/Output Quantity Credited to Merchant Principal: {qty:,.2f} {tx.line_items[0].unit}\n"
        + f"Processor: {tx.processor.name}\n"
        + f"Vendor-of-record: {tx.vendor_of_record.name}\n"
        + _line()
        + "\nStatement: The quantities listed above are allocated to the merchant principal and held/processed on its behalf under the processing arrangement.\n"
        + "Processor Signature: _____________________\n"
        + "Vendor-of-record Signature: _____________________\n"
    )


def build_murabaha_schedule_text(tx: Transaction, system: SystemProfile, operator: Entity) -> str:
    funder_name = tx.funder.name if tx.funder else "N/A"
    base_cost = tx.total_before_wht()
    markup = round(base_cost * 0.08, 2)
    deferred_price = round(base_cost + markup, 2)
    return (
        _header("MURABAHA DEAL SUMMARY", operator, tx.vendor_of_record, tx.source)
        + f"Vendor-of-record: {tx.vendor_of_record.name}\n"
        + f"Operator: {operator.name}\n"
        + f"Funder: {funder_name}\n"
        + f"Linked Invoice: {tx.invoice_no}\n"
        + f"Linked Run ID: {tx.run_id or 'N/A'}\n"
        + _line()
        + "\n"
        + f"Bank Purchase Cost: {nfmt(base_cost)} {tx.currency}\n"
        + f"Murabaha Markup (illustrative 8.00%): {nfmt(markup)} {tx.currency}\n"
        + f"Deferred Sale Price: {nfmt(deferred_price)} {tx.currency}\n"
        + f"Repayment Terms: {tx.payment_terms}\n"
        + _line()
        + "\n"
        + "Note: In production use, pull markup and tenor from approved bank term sheet.\n"
    )


def build_pack_text(document_texts: List[str], tx: Transaction, system: SystemProfile, operator: Entity) -> str:
    title = _header(f"DELIVERY PACK - {tx.invoice_no}", operator, tx.vendor_of_record, tx.source)
    sections = [title]
    for text in document_texts:
        sections.append(text)
        sections.append("\n" + _line("=") + "\n")
    return "\n".join(sections)
