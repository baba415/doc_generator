from __future__ import annotations

import base64
from html import escape
from pathlib import Path
from typing import Dict, List, Sequence

from .html_pdf import render_html_to_pdf
from .schemas import Entity, LineItem, SystemProfile, Transaction
from .utils import amount_to_words_naira, nfmt


def _resolve_asset(root_dir: Path, configured_path: str | None, fallback: str) -> Path | None:
    candidates: List[Path] = []
    if configured_path:
        configured = Path(configured_path)
        if configured.is_absolute():
            candidates.append(configured)
        else:
            candidates.append(root_dir / configured)
    candidates.append(root_dir / fallback)

    for candidate in candidates:
        if candidate.exists():
            suffix = candidate.suffix.lower()
            if suffix in {".jpg", ".jpeg", ".png"}:
                return candidate
            jpg = candidate.with_suffix(".jpg")
            png = candidate.with_suffix(".png")
            if jpg.exists():
                return jpg
            if png.exists():
                return png
    return None


def _image_data_uri(path: Path | None) -> str:
    if not path or not path.exists():
        return ""
    suffix = path.suffix.lower()
    mime = "image/jpeg"
    if suffix == ".png":
        mime = "image/png"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _safe(value: object) -> str:
    return escape(str(value if value is not None else "-"), quote=True)


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


def _line_items_html(items: Sequence[LineItem]) -> str:
    rows: List[str] = []
    for idx, item in enumerate(items, start=1):
        rows.append(
            (
                "<tr>"
                f"<td class='num'>{idx}</td>"
                f"<td>{_safe(item.description)}</td>"
                f"<td class='num'>{item.quantity:,.2f}</td>"
                f"<td class='center'>{_safe(item.unit)}</td>"
                f"<td class='num'>{item.unit_price:,.2f}</td>"
                f"<td class='num'>{item.amount:,.2f}</td>"
                "</tr>"
            )
        )
    if not rows:
        rows.append("<tr><td class='num'>1</td><td>-</td><td class='num'>0.00</td><td class='center'>-</td><td class='num'>0.00</td><td class='num'>0.00</td></tr>")
    return "".join(rows)


def _kv_table_html(title: str, rows: Sequence[tuple[str, str]]) -> str:
    body = "".join(
        f"<tr><th>{_safe(label)}</th><td>{_safe(value)}</td></tr>"
        for label, value in rows
    )
    return (
        "<section class='section'>"
        f"<h3>{_safe(title)}</h3>"
        "<table class='kv'>"
        f"{body}"
        "</table>"
        "</section>"
    )


def _paragraph_box_html(title: str, lines: Sequence[str]) -> str:
    body = "".join(f"<p>{_safe(line)}</p>" for line in lines if line)
    return (
        "<section class='section'>"
        f"<h3>{_safe(title)}</h3>"
        f"<div class='pbox'>{body or '<p>-</p>'}</div>"
        "</section>"
    )


def _signatures_html(
    labels: Sequence[str],
    seal_data_uri: str = "",
    signature_data_uri: str = "",
    signature_slot: int = 1,
) -> str:
    cols: List[str] = []
    for idx, label in enumerate(labels):
        sig_markup = ""
        if signature_data_uri and idx == signature_slot:
            sig_markup = f"<img class='sig-img' src='{signature_data_uri}' alt='signature' />"
        cols.append(
            "<div class='sig-col'>"
            f"{sig_markup}"
            "<div class='sig-line'></div>"
            f"<div class='sig-label'>{_safe(label)}</div>"
            "</div>"
        )
    seal_html = f"<img class='seal' src='{seal_data_uri}' alt='company seal' />" if seal_data_uri else ""
    return f"<section class='signatures'>{''.join(cols)}{seal_html}</section>"


def _document_html(
    root_dir: Path,
    system: SystemProfile,
    vendor: Entity,
    operator: Entity,
    source: Entity | None,
    title: str,
    reference: str,
    body_html: str,
    signatures_html: str = "",
    use_collection_account: bool = True,
    internal_watermark: str = "",
) -> str:
    logo = _image_data_uri(_resolve_asset(root_dir, system.logo_path, "assets/primary_logo.png"))
    seal = _image_data_uri(_resolve_asset(root_dir, system.seal_path, "assets/company_seal.png"))
    signature = _image_data_uri(_resolve_asset(root_dir, system.signature_path, "assets/authorized_signature.png"))

    logo_html = f"<img src='{logo}' alt='logo' />" if logo else f"<h2>{_safe(operator.name)}</h2>"
    signatures_markup = signatures_html
    if seal:
        signatures_markup = signatures_markup.replace("__SEAL__", seal)
    else:
        signatures_markup = signatures_markup.replace("<img class='seal' src='__SEAL__' alt='company seal' />", "")
        signatures_markup = signatures_markup.replace("__SEAL__", "")

    if signature:
        signatures_markup = signatures_markup.replace("__SIGNATURE__", signature)
    else:
        signatures_markup = signatures_markup.replace("<img class='sig-img' src='__SIGNATURE__' alt='signature' />", "")
        signatures_markup = signatures_markup.replace("__SIGNATURE__", "")

    css = """
    @page {
      size: A4;
      margin: 9mm;
    }
    html, body {
      margin: 0;
      padding: 0;
      background: #ffffff !important;
      color: #111827 !important;
      font-family: "Inter", "Segoe UI", Arial, sans-serif;
      font-size: 11px;
      line-height: 1.35;
      -webkit-print-color-adjust: exact;
      print-color-adjust: exact;
    }
    * { box-sizing: border-box; }
    :root {
      --ink: #0f172a;
      --muted: #475569;
      --line: #cfd8e3;
      --line-strong: #b7c4d4;
      --surface: #ffffff;
      --surface-soft: #f7fafc;
      --surface-strong: #eef3f8;
      --brand: #0a6f68;
    }
    .doc {
      min-height: 100%;
      position: relative;
    }
    .header {
      border: 1px solid var(--line-strong);
      border-radius: 10px;
      padding: 10px 13px;
      margin-bottom: 9px;
      background: var(--surface);
      box-shadow: 0 0 0 0.4px rgba(15, 23, 42, 0.04);
    }
    .watermark {
      position: fixed;
      inset: 0;
      display: flex;
      align-items: center;
      justify-content: center;
      pointer-events: none;
      z-index: 0;
    }
    .watermark span {
      transform: rotate(-25deg);
      font-size: 60px;
      font-weight: 800;
      color: rgba(15, 23, 42, 0.07);
      letter-spacing: 2px;
      text-transform: uppercase;
    }
    .header-top {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 16px;
      margin-bottom: 8px;
    }
    .brand img {
      height: 30px;
      width: auto;
      display: block;
    }
    .title {
      text-align: right;
      min-width: 260px;
    }
    .title h1 {
      margin: 0;
      font-size: 36px;
      color: var(--ink);
      letter-spacing: 0.2px;
      line-height: 1.06;
    }
    .title p {
      margin: 2px 0 0;
      font-size: 11px;
      color: var(--muted);
    }
    .meta-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
      border-top: 1px solid var(--line);
      padding-top: 9px;
      position: relative;
    }
    .meta-grid::before {
      content: "";
      position: absolute;
      top: -1px;
      left: 0;
      width: 110px;
      height: 2px;
      background: var(--brand);
    }
    .meta-col h4 {
      margin: 0 0 4px;
      font-size: 11px;
      color: #1f2937;
      font-weight: 700;
    }
    .meta-col p {
      margin: 0 0 2px;
      font-size: 10.5px;
      color: #334155;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .meta-source {
      margin-top: 8px;
      border-top: 1px solid var(--line);
      padding-top: 8px;
    }
    .meta-source h4 {
      margin: 0 0 4px;
      font-size: 11px;
      color: #1f2937;
      font-weight: 700;
    }
    .meta-source p {
      margin: 0 0 2px;
      font-size: 10.5px;
      color: #334155;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .section-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 9px;
      margin-bottom: 9px;
    }
    .section {
      border: 1px solid var(--line);
      border-radius: 7px;
      overflow: hidden;
      background: var(--surface);
      margin-bottom: 9px;
      box-shadow: 0 0 0 0.4px rgba(15, 23, 42, 0.02);
    }
    .section h3 {
      margin: 0;
      padding: 6px 9px;
      font-size: 11px;
      color: var(--ink);
      border-bottom: 1px solid var(--line);
      font-weight: 700;
      background: linear-gradient(180deg, #f8fbff 0%, #f2f7fc 100%);
    }
    table {
      width: 100%;
      border-collapse: collapse;
    }
    table.kv th, table.kv td {
      border: 1px solid #dbe2ec;
      padding: 4px 7px;
      font-size: 10.5px;
      vertical-align: top;
    }
    table.kv th {
      width: 42%;
      text-align: left;
      color: #1f2937;
      font-weight: 700;
      background: #fbfdff;
    }
    table.data th, table.data td {
      border: 1px solid #dbe2ec;
      padding: 4px 7px;
      font-size: 10.5px;
      vertical-align: top;
    }
    table.data th {
      text-align: left;
      background: #f7fafe;
      color: #1f2937;
      font-weight: 700;
    }
    .num { text-align: right; white-space: nowrap; }
    .center { text-align: center; }
    .pbox { padding: 6px 8px; }
    .pbox p {
      margin: 0 0 5px;
      font-size: 10.5px;
      color: #1f2937;
    }
    .totals-grid {
      display: grid;
      grid-template-columns: 1fr 280px;
      gap: 9px;
      margin-bottom: 9px;
    }
    .totals table td, .totals table th {
      border: 1px solid #dbe2ec;
      padding: 4px 7px;
      font-size: 10.5px;
    }
    .totals table th {
      text-align: left;
      width: 52%;
      background: #fbfdff;
      font-weight: 700;
    }
    .totals .grand th, .totals .grand td {
      font-weight: 700;
      font-size: 11px;
      background: #eef5fb;
    }
    .signatures {
      margin-top: 16px;
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 12px;
      align-items: end;
      position: relative;
      min-height: 94px;
    }
    .sig-col {
      position: relative;
      padding-top: 20px;
    }
    .sig-img {
      position: absolute;
      top: 0;
      left: 4px;
      height: 18px;
      width: auto;
    }
    .sig-line {
      border-bottom: 1px solid #94a3b8;
      margin-bottom: 5px;
    }
    .sig-label {
      font-size: 10px;
      color: #334155;
    }
    .seal {
      position: absolute;
      right: 2px;
      bottom: 0;
      width: 80px;
      height: auto;
      opacity: 0.98;
    }
    .footer {
      margin-top: 11px;
      border-top: 1px solid var(--line);
      padding-top: 6px;
      color: #475569;
      font-size: 9px;
    }
    .footer p {
      margin: 1px 0;
    }
    """

    payment_line = "-"
    notice_line = ""
    if use_collection_account and system.collection_account:
        payment_line = f"Collection Account: {system.collection_account.account_name} / {system.collection_account.account_number}"
        notice_line = _collection_notice(system, operator, vendor)
    elif vendor.bank:
        payment_line = f"Payment Account: {vendor.bank.account_name} / {vendor.bank.account_number}"

    watermark_html = (
        f"<div class='watermark'><span>{_safe(internal_watermark)}</span></div>" if internal_watermark else ""
    )

    return f"""<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width,initial-scale=1" />
    <style>{css}</style>
  </head>
  <body>
    {watermark_html}
    <main class="doc">
      <header class="header">
        <div class="header-top">
          <div class="brand">{logo_html}</div>
          <div class="title">
            <h1>{_safe(title)}</h1>
            <p>Reference: {_safe(reference)}</p>
          </div>
        </div>
        <div class="meta-grid">
          <div class="meta-col">
            <h4>Vendor-of-record (Tax Supplier)</h4>
            <p><strong>{_safe(vendor.name)}</strong></p>
            <p>TIN: {_safe(vendor.tin)} | RC: {_safe(vendor.rc_number)}</p>
            <p>{_safe(vendor.address)}, {_safe(vendor.city_state_country)}</p>
            <p>Web: {_safe(vendor.website)}</p>
          </div>
          <div class="meta-col">
            <h4>Operated by (Execution &amp; Collection Agent)</h4>
            <p><strong>{_safe(operator.name)}</strong></p>
            <p>TIN: {_safe(operator.tin)} | RC: {_safe(operator.rc_number or "-")}</p>
            <p>{_safe(payment_line)}</p>
            <p>Contacts: {_safe(" / ".join(operator.phones))}</p>
          </div>
        </div>
        <div class="meta-source">
          <h4>Source / Producer (Traceability only)</h4>
          <p><strong>{_safe(source.name if source else "-")}</strong></p>
          <p>TIN: {_safe(source.tin if source else "-")} | RC: {_safe(source.rc_number if source else "-")}</p>
        </div>
      </header>
      {body_html}
      {signatures_markup}
      <footer class="footer">
        <p>Vendor-of-record TIN: {_safe(vendor.tin)} | RC: {_safe(vendor.rc_number)} | Operator TIN: {_safe(operator.tin)}</p>
        <p>{_safe(payment_line)}</p>
        <p>{_safe(notice_line) if notice_line else ''}</p>
      </footer>
    </main>
  </body>
</html>"""


def render_invoice_pdf(
    root_dir: Path,
    tx: Transaction,
    system: SystemProfile,
    operator: Entity,
    tax: Dict[str, object],
    *,
    title: str = "Commercial Invoice",
    internal_watermark: str = "",
) -> bytes:
    left_rows = [
        ("Invoice No", tx.invoice_no),
        ("Invoice Date", tx.invoice_date),
        ("Due Date", tx.due_date),
        ("Agreement Ref", tx.agreement_ref or "N/A"),
        ("LPO No", tx.lpo_no or "N/A"),
        ("Funding Mode", tx.funding_mode),
    ]
    right_rows = [
        ("Buyer", tx.buyer.name),
        ("Ship To", tx.ship_to.name),
        ("LPO Date", tx.lpo_date or "N/A"),
        ("Run ID", tx.run_id or "N/A"),
        ("Batch ID", tx.batch_id or "N/A"),
        ("Payment Terms", tx.payment_terms or "N/A"),
    ]
    details_html = (
        "<div class='section-grid'>"
        f"{_kv_table_html('Document Details', left_rows)}"
        f"{_kv_table_html('Counterparty Details', right_rows)}"
        "</div>"
    )

    context_lines = [
        f"Buyer Address: {tx.buyer.address}",
        f"Ship To Address: {tx.ship_to.address}",
        f"Processor: {tx.processor.name} | Delivery Terms: {tx.delivery_terms} | Packaging: {tx.packaging}",
        f"Order Type: {tx.order_type or '-'} | Order Sub Type: {tx.order_sub_type or '-'}",
        f"Truck: {tx.truck_no} | Driver: {tx.driver_name} ({tx.driver_phone})",
    ]
    context_html = _paragraph_box_html("Operational Context", context_lines)

    items_html = (
        "<section class='section'>"
        "<h3>Line Items</h3>"
        "<table class='data'>"
        "<thead><tr><th style='width:34px;'>#</th><th>Description</th><th style='width:96px;' class='num'>Qty</th><th style='width:70px;' class='center'>Unit</th><th style='width:96px;' class='num'>Rate</th><th style='width:118px;' class='num'>Amount</th></tr></thead>"
        f"<tbody>{_line_items_html(tx.line_items)}</tbody>"
        "</table>"
        "</section>"
    )

    notes_lines = [
        f"Amount in words: {amount_to_words_naira(tx.grand_total())}",
        f"VAT Basis: {tax.get('vat_basis', '-')}",
        f"WHT Basis: {tax.get('wht_basis', '-')}",
        f"VAT Reason: {tax.get('vat_exemption_reason') or '-'}",
        f"WHT Reason: {tax.get('wht_exemption_reason') or '-'}",
        "Validation: Finance sign-off required where override fields are used.",
    ]
    notes_html = _paragraph_box_html("Tax and Settlement Notes", notes_lines)

    totals_html = (
        "<section class='section totals'>"
        "<h3>Payment Summary</h3>"
        "<table>"
        f"<tr><th>Subtotal</th><td class='num'>{nfmt(tx.subtotal())} {tx.currency}</td></tr>"
        f"<tr><th>Transport Charges</th><td class='num'>{nfmt(tx.transport_charges)} {tx.currency}</td></tr>"
        f"<tr><th>Installation Charges</th><td class='num'>{nfmt(tx.installation_charges)} {tx.currency}</td></tr>"
        f"<tr><th>VAT ({tax['vat_rate']}%)</th><td class='num'>{nfmt(tx.vat_amount())} {tx.currency}</td></tr>"
        f"<tr><th>Discount</th><td class='num'>{nfmt(tx.discount)} {tx.currency}</td></tr>"
        f"<tr><th>WHT ({tax['wht_rate']}%)</th><td class='num'>{nfmt(tx.wht_amount())} {tx.currency}</td></tr>"
        f"<tr class='grand'><th>TOTAL PAYABLE</th><td class='num'>{nfmt(tx.grand_total())} {tx.currency}</td></tr>"
        "</table>"
        "</section>"
    )

    vendor = tx.vendor_of_record
    payment_lines: List[str] = []
    if tx.use_collection_account and system.collection_account:
        payment_lines.extend(
            [
                f"Collection Account: {system.collection_account.account_name}",
                f"Bank: {system.collection_account.bank_name} | A/C No: {system.collection_account.account_number}",
                f"Payment Reference: {tx.invoice_no} / {tx.run_id or '-'}",
            ]
        )
        notice = _collection_notice(system, operator, vendor)
        if notice:
            payment_lines.append(notice)
    elif vendor.bank:
        payment_lines.extend(
            [
                f"Pay Vendor-of-record: {vendor.bank.account_name}",
                f"Bank: {vendor.bank.bank_name} | A/C No: {vendor.bank.account_number}",
                f"Payment Reference: {tx.invoice_no} / {tx.run_id or '-'}",
            ]
        )
    else:
        payment_lines.append("Payment Instructions: -")
    payment_html = _paragraph_box_html("Payment Instructions", payment_lines)

    body_html = (
        details_html
        + context_html
        + items_html
        + "<div class='totals-grid'>"
        + notes_html
        + totals_html
        + "</div>"
        + payment_html
    )

    signatures_html = _signatures_html(
        labels=(f"Prepared By ({operator.name})", f"Approved For {vendor.name}", "Buyer Confirmation"),
        seal_data_uri="__SEAL__",
        signature_data_uri="__SIGNATURE__",
        signature_slot=1,
    )
    html = _document_html(
        root_dir=root_dir,
        system=system,
        vendor=vendor,
        operator=operator,
        source=tx.source,
        title=title,
        reference=tx.invoice_no,
        body_html=body_html,
        signatures_html=signatures_html,
        use_collection_account=tx.use_collection_account,
        internal_watermark=internal_watermark,
    )
    return render_html_to_pdf(html, doc_key=f"invoice-{tx.invoice_no}")


def render_waybill_pdf(root_dir: Path, tx: Transaction, system: SystemProfile, operator: Entity) -> bytes:
    left_rows = [
        ("Waybill No", tx.waybill_no),
        ("Date", tx.invoice_date),
        ("Linked Invoice", tx.invoice_no),
        ("LPO No", tx.lpo_no or "N/A"),
        ("Run ID", tx.run_id or "N/A"),
        ("Batch ID", tx.batch_id or "N/A"),
    ]
    right_rows = [
        ("Customer", tx.buyer.name),
        ("LPO Date", tx.lpo_date or "N/A"),
        ("Delivery Terms", tx.delivery_terms),
        ("Packaging", tx.packaging),
        ("Truck No", tx.truck_no),
        ("Driver", f"{tx.driver_name} ({tx.driver_phone})"),
    ]
    details_html = (
        "<div class='section-grid'>"
        f"{_kv_table_html('Dispatch Reference', left_rows)}"
        f"{_kv_table_html('Transport Details', right_rows)}"
        "</div>"
    )

    ship_lines = [
        f"Ship To: {tx.ship_to.name}",
        f"Address: {tx.ship_to.address}",
        f"Contact: {tx.ship_to.contact_person or '-'} | Phone: {tx.ship_to.phone or '-'}",
    ]
    ship_html = _paragraph_box_html("Delivery Destination", ship_lines)

    items_html = (
        "<section class='section'>"
        "<h3>Dispatch Items</h3>"
        "<table class='data'>"
        "<thead><tr><th style='width:34px;'>#</th><th>Description</th><th style='width:96px;' class='num'>Qty</th><th style='width:70px;' class='center'>Unit</th><th style='width:96px;' class='num'>Rate</th><th style='width:118px;' class='num'>Amount</th></tr></thead>"
        f"<tbody>{_line_items_html(tx.line_items)}</tbody>"
        "</table>"
        f"<div class='pbox'><p><strong>Total Quantity:</strong> {sum(item.quantity for item in tx.line_items):,.2f} {tx.line_items[0].unit if tx.line_items else '-'}</p></div>"
        "</section>"
    )

    acknowledgement_lines = [
        "All quantities and package condition acknowledged by receiving party at point of delivery.",
        "Any discrepancy must be endorsed on this waybill at delivery point.",
    ]
    acknowledgement_html = _paragraph_box_html("Delivery Acknowledgement", acknowledgement_lines)

    body_html = details_html + ship_html + items_html + acknowledgement_html
    signatures_html = _signatures_html(
        labels=("Prepared By", "Dispatch Supervisor", "Receiver Signature"),
        seal_data_uri="",
        signature_data_uri="",
    )
    html = _document_html(
        root_dir=root_dir,
        system=system,
        vendor=tx.vendor_of_record,
        operator=operator,
        source=tx.source,
        title="Waybill",
        reference=tx.waybill_no,
        body_html=body_html,
        signatures_html=signatures_html,
        use_collection_account=tx.use_collection_account,
    )
    return render_html_to_pdf(html, doc_key=f"waybill-{tx.waybill_no}")


def render_weighing_ticket_pdf(root_dir: Path, tx: Transaction, system: SystemProfile, operator: Entity) -> bytes:
    total_qty = sum(item.quantity for item in tx.line_items)
    unit = tx.line_items[0].unit if tx.line_items else "kgs"
    detail_rows = [
        ("Ticket No", tx.weighing_no),
        ("Linked Waybill", tx.waybill_no),
        ("Date", tx.invoice_date),
        ("LPO Ref", tx.lpo_no or "N/A"),
        ("Material", f"{tx.product_name} ({tx.product_code})"),
        ("Batch / Run", f"{tx.batch_id} / {tx.run_id or 'N/A'}"),
        ("Truck / Driver", f"{tx.truck_no} / {tx.driver_name}"),
        ("Processor", tx.processor.name),
        ("Customer", tx.buyer.name),
    ]
    weight_rows = [
        ("Gross Weight", f"{total_qty:,.2f} {unit}"),
        ("Tare Weight", f"{0:,.2f} {unit}"),
        ("Net Weight", f"{total_qty:,.2f} {unit}"),
    ]
    notes_lines = [
        "This ticket confirms measured dispatch quantity tied to linked waybill and run record.",
        "Any correction requires scale officer endorsement and customer acknowledgement.",
    ]

    body_html = (
        _kv_table_html("Load and Product Details", detail_rows)
        + _kv_table_html("Weight Breakdown", weight_rows)
        + _paragraph_box_html("Ticket Notes", notes_lines)
    )
    signatures_html = _signatures_html(
        labels=("Scale Officer", "Operations Checker", "Customer Confirmation"),
        seal_data_uri="__SEAL__",
        signature_data_uri="",
    )
    html = _document_html(
        root_dir=root_dir,
        system=system,
        vendor=tx.vendor_of_record,
        operator=operator,
        source=tx.source,
        title="Weighing Ticket",
        reference=tx.weighing_no,
        body_html=body_html,
        signatures_html=signatures_html,
        use_collection_account=tx.use_collection_account,
    )
    return render_html_to_pdf(html, doc_key=f"weighing-{tx.weighing_no}")


def render_coa_pdf(root_dir: Path, tx: Transaction, system: SystemProfile, operator: Entity) -> bytes:
    quantity = sum(item.quantity for item in tx.line_items)
    unit = tx.line_items[0].unit if tx.line_items else "kgs"

    left_rows = [
        ("Certificate Type", tx.coa_template_name or "Certificate of Analysis"),
        ("Document No", tx.coa_document_no or "N/A"),
        ("Version No", tx.coa_version_no or "N/A"),
        ("COA No", tx.coa_no),
        ("Linked Invoice", tx.invoice_no),
        ("LPO No", tx.lpo_no or "N/A"),
        ("Product", tx.product_name),
        ("Material Identification Code", tx.material_identification_code or tx.product_code),
        ("Batch ID", tx.batch_id),
        ("Run ID", tx.run_id or "N/A"),
    ]
    right_rows = [
        ("Manufacture Date", tx.manufacture_date or "-"),
        ("Expiry Date", tx.expiry_date or "-"),
        ("LPO Date", tx.lpo_date or "N/A"),
        ("Order Type", tx.order_type or "N/A"),
        ("Order Sub Type", tx.order_sub_type or "N/A"),
        ("Processor", tx.processor.name),
        ("Customer", tx.buyer.name),
        ("Net Weight / Volume", f"{quantity:,.2f} {unit}"),
        ("Sample Source", "Production run sample"),
    ]

    parameters_rows = "".join(
        (
            "<tr>"
            f"<td>{_safe(item.get('parameter', '-'))}</td>"
            f"<td>{_safe(item.get('standard', '-'))}</td>"
            f"<td>{_safe(item.get('result', '-'))}</td>"
            "</tr>"
        )
        for item in tx.quality_parameters
    )
    if not parameters_rows:
        parameters_rows = "<tr><td>-</td><td>-</td><td>-</td></tr>"

    quality_html = (
        "<section class='section'>"
        "<h3>Quality Parameters</h3>"
        "<table class='data'>"
        "<thead><tr><th style='width:45%;'>Parameter</th><th style='width:28%;'>Standard</th><th>Result</th></tr></thead>"
        f"<tbody>{parameters_rows}</tbody>"
        "</table>"
        "</section>"
    )

    conclusion_lines = [
        f"QC Statement: Product meets tested parameters for batch {tx.batch_id}.",
        "Release Status: Passed for dispatch subject to normal storage and handling controls.",
    ]

    body_html = (
        "<div class='section-grid'>"
        f"{_kv_table_html('Batch Reference', left_rows)}"
        f"{_kv_table_html('Sampling Details', right_rows)}"
        "</div>"
        + quality_html
        + _paragraph_box_html("QC Conclusion", conclusion_lines)
    )
    signatures_html = _signatures_html(
        labels=("QC Analyst", "QC Approver", "Customer/Receiver"),
        seal_data_uri="__SEAL__",
        signature_data_uri="__SIGNATURE__",
        signature_slot=1,
    )
    html = _document_html(
        root_dir=root_dir,
        system=system,
        vendor=tx.vendor_of_record,
        operator=operator,
        source=tx.source,
        title=tx.coa_template_name or "Certificate of Analysis",
        reference=tx.coa_no,
        body_html=body_html,
        signatures_html=signatures_html,
        use_collection_account=tx.use_collection_account,
    )
    return render_html_to_pdf(html, doc_key=f"coa-{tx.coa_no}")


def render_receipt_pdf(root_dir: Path, tx: Transaction, system: SystemProfile, operator: Entity) -> bytes:
    receipt_rows = [
        ("Receipt No", tx.receipt_no),
        ("Receipt Date", tx.invoice_date),
        ("Received From", tx.buyer.name),
        ("Against Invoice", tx.invoice_no),
        ("LPO Ref", tx.lpo_no or "N/A"),
        ("Amount Received", f"{nfmt(tx.grand_total())} {tx.currency}"),
        ("Payment Method", "Bank Transfer"),
        ("Status", "Fully Paid"),
    ]
    allocation_rows = [
        ("Linked Waybill", tx.waybill_no),
        ("Linked Weighing Ticket", tx.weighing_no),
        ("Batch / Run", f"{tx.batch_id} / {tx.run_id or 'N/A'}"),
        ("Applied Amount", f"{nfmt(tx.grand_total())} {tx.currency}"),
    ]

    note_lines = [
        f"Amount in words: {amount_to_words_naira(tx.grand_total())}",
        "Receipt confirms settlement against linked invoice and delivery pack records.",
    ]
    body_html = (
        _kv_table_html("Receipt Details", receipt_rows)
        + _kv_table_html("Allocation", allocation_rows)
        + _paragraph_box_html("Receipt Note", note_lines)
    )
    signatures_html = _signatures_html(
        labels=("Prepared By", "Finance Approval", "Authorized Signatory"),
        seal_data_uri="__SEAL__",
        signature_data_uri="__SIGNATURE__",
        signature_slot=2,
    )
    html = _document_html(
        root_dir=root_dir,
        system=system,
        vendor=tx.vendor_of_record,
        operator=operator,
        source=tx.source,
        title="Payment Receipt",
        reference=tx.receipt_no,
        body_html=body_html,
        signatures_html=signatures_html,
        use_collection_account=tx.use_collection_account,
    )
    return render_html_to_pdf(html, doc_key=f"receipt-{tx.receipt_no}")


def render_allocation_note_pdf(root_dir: Path, tx: Transaction, system: SystemProfile, operator: Entity) -> bytes:
    quantity = sum(item.quantity for item in tx.line_items)
    unit = tx.line_items[0].unit if tx.line_items else "kgs"
    reference = tx.allocation_reference or tx.run_id or tx.invoice_no
    rows = [
        ("Allocation Ref", reference),
        ("Run ID", tx.run_id or "N/A"),
        ("Batch ID", tx.batch_id or "N/A"),
        ("Product", tx.product_name),
        ("Processor", tx.processor.name),
        ("Vendor-of-record", tx.vendor_of_record.name),
        ("Allocated Quantity", f"{quantity:,.2f} {unit}"),
    ]
    statement_lines = [
        "The quantity above is allocated to the merchant principal and held/processed on its behalf.",
        "This note supports pilot chain-of-custody and settlement evidence.",
        "Any adjustment must reference the same run ID and carry processor acknowledgement.",
    ]
    body_html = _kv_table_html("Allocation Details", rows) + _paragraph_box_html("Statement", statement_lines)
    signatures_html = _signatures_html(
        labels=("Processor Signatory", "Operator", "Vendor-of-record"),
        seal_data_uri="__SEAL__",
        signature_data_uri="",
    )
    html = _document_html(
        root_dir=root_dir,
        system=system,
        vendor=tx.vendor_of_record,
        operator=operator,
        source=tx.source,
        title="Allocation Note (Pilot)",
        reference=reference,
        body_html=body_html,
        signatures_html=signatures_html,
        use_collection_account=tx.use_collection_account,
    )
    return render_html_to_pdf(html, doc_key=f"allocation-{reference}")


def render_murabaha_schedule_pdf(root_dir: Path, tx: Transaction, system: SystemProfile, operator: Entity) -> bytes:
    base_cost = tx.total_before_wht()
    markup = round(base_cost * 0.08, 2)
    deferred_price = round(base_cost + markup, 2)
    rows = [
        ("Linked Invoice", tx.invoice_no),
        ("Funder", tx.funder.name if tx.funder else "N/A"),
        ("Vendor-of-record", tx.vendor_of_record.name),
        ("Operator", operator.name),
        ("Run ID", tx.run_id or "N/A"),
        ("Payment Terms", tx.payment_terms),
    ]
    finance_rows = [
        ("Bank Purchase Cost", f"{nfmt(base_cost)} {tx.currency}"),
        ("Markup (Illustrative 8.00%)", f"{nfmt(markup)} {tx.currency}"),
        ("Deferred Sale Price", f"{nfmt(deferred_price)} {tx.currency}"),
        ("Repayment Due", tx.due_date),
    ]
    timeline_lines = [
        "1. Supplier issues PFI to funder and funder settles supplier directly.",
        "2. Goods are delivered against linked run and delivery evidence pack.",
        "3. Merchant principal repays funder according to approved Murabaha terms.",
    ]
    body_html = (
        _kv_table_html("Deal Context", rows)
        + _kv_table_html("Financial Summary", finance_rows)
        + _paragraph_box_html("Execution Timeline", timeline_lines)
    )
    signatures_html = _signatures_html(
        labels=("Funder Confirmation", "Vendor-of-record", "Operator"),
        seal_data_uri="",
        signature_data_uri="",
    )
    html = _document_html(
        root_dir=root_dir,
        system=system,
        vendor=tx.vendor_of_record,
        operator=operator,
        source=tx.source,
        title="Murabaha Deal Summary",
        reference=f"MURABAHA-{tx.invoice_no}",
        body_html=body_html,
        signatures_html=signatures_html,
        use_collection_account=tx.use_collection_account,
    )
    return render_html_to_pdf(html, doc_key=f"murabaha-{tx.invoice_no}")


def render_pack_pdf(
    root_dir: Path,
    tx: Transaction,
    system: SystemProfile,
    operator: Entity,
    tax: Dict[str, object],
    *,
    entries: Sequence[str],
) -> bytes:

    summary_lines = [
        f"Mode: {tx.mode}",
        f"Funding Mode: {tx.funding_mode}",
        f"Buyer: {tx.buyer.name}",
        f"Batch / Run: {tx.batch_id or '-'} / {tx.run_id or '-'}",
        f"Invoice Value: {nfmt(tx.grand_total())} {tx.currency}",
        f"VAT Rate: {tax.get('vat_rate', 0)}% | WHT Rate: {tax.get('wht_rate', 0)}%",
    ]
    print_order_lines = [f"{idx}. {name}" for idx, name in enumerate(entries, start=1)]
    guidance_lines = [
        "This PDF contains an index page plus the full adopted document pack in the order above.",
        "Manifest hashes and machine-readable metadata are stored in manifest.json in this folder.",
    ]

    body_html = (
        _paragraph_box_html("Pack Summary", summary_lines)
        + _paragraph_box_html("Print Order", print_order_lines)
        + _paragraph_box_html("Guidance", guidance_lines)
    )
    signatures_html = _signatures_html(
        labels=("Pack Prepared By", "Pack Reviewed By", "Pack Approved By"),
        seal_data_uri="",
        signature_data_uri="",
    )
    html = _document_html(
        root_dir=root_dir,
        system=system,
        vendor=tx.vendor_of_record,
        operator=operator,
        source=tx.source,
        title=f"Delivery Pack Index - {tx.invoice_no}",
        reference=tx.invoice_no,
        body_html=body_html,
        signatures_html=signatures_html,
        use_collection_account=tx.use_collection_account,
    )
    return render_html_to_pdf(html, doc_key=f"pack-{tx.invoice_no}")
