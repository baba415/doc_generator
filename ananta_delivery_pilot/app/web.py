from __future__ import annotations

import cgi
import datetime as dt
import html
import json
import shutil
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from string import Template
from tempfile import mkdtemp
from urllib.parse import parse_qs

from .generator import DeliveryPackGenerator
from .utils import read_json
from adapters.sqlite_repo import SQLiteRepo
from core.config import RuntimeConfig
from domain.automation import AutomationOrchestrator
from domain.services import Phase1Service


def _escape(value: object) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _options(options: list[tuple[str, str]], *, selected: str = "") -> str:
    rendered: list[str] = []
    for value, label in options:
        sel = " selected" if value == selected else ""
        rendered.append(f"<option value=\"{_escape(value)}\"{sel}>{_escape(label)}</option>")
    return "\n".join(rendered)


HTML_PAGE = Template("""<!doctype html>
<html>
<head>
  <meta charset=\"utf-8\" />
  <title>Ananta Delivery Pilot</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif; margin: 20px; }
    textarea { width: 100%; height: 360px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
    input[type=text], input[type=date], input[type=number], select { padding: 6px; width: 420px; max-width: 100%; }
    label { display: inline-block; width: 220px; }
    .row { margin: 10px 0; }
    button { padding: 8px 14px; }
    .note { color: #555; }
    .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px 18px; align-items: center; }
    .card { border: 1px solid #ddd; border-radius: 10px; padding: 14px; margin: 14px 0; }
    .card h3 { margin: 0 0 12px 0; }
    .muted { color: #777; font-size: 12px; }
    .danger { color: #b00020; }
    pre { background: #f6f6f6; padding: 12px; }
    details.card > summary { cursor: pointer; font-weight: 600; }
    details.card[open] > summary { margin-bottom: 12px; }
  </style>
</head>
<body>
  <h2>Ananta Delivery Pilot Generator</h2>
  <p class=\"note\">Form mode (recommended) supports entity dropdowns + original document uploads. JSON mode stays available for advanced use.</p>

  <div class=\"card\">
    <h3>Form Mode</h3>
    <form method=\"POST\" action=\"/generate-form\" enctype=\"multipart/form-data\">
      <div class=\"row\">
        <h4>Core (quick generate)</h4>
        <div class=\"grid\">
          <label>Buyer</label>
          <select name=\"buyer_id\">${buyer_options}</select>

          <label>Vendor-of-record (tax supplier)</label>
          <select name=\"vendor_of_record_id\">${vendor_options}</select>

          <label>Processor</label>
          <select name=\"processor_id\">${processor_options}</select>

          <label>Invoice Date</label>
          <input type=\"date\" name=\"invoice_date\" value=\"${invoice_date}\" required />

          <label>Due Date (optional)</label>
          <input type=\"date\" name=\"due_date\" value=\"${due_date}\" />

          <label>LPO No</label>
          <input type=\"text\" name=\"lpo_no\" value=\"\" />

          <label>LPO Date</label>
          <input type=\"date\" name=\"lpo_date\" value=\"\" />

          <label>Product Code (COA profile key)</label>
          <select name=\"product_code\">${product_code_options}</select>

          <label>Product Name</label>
          <input type=\"text\" name=\"product_name\" value=\"Refined Bleached Deodorized Soya Oil\" required />

          <label>Quantity</label>
          <input type=\"number\" name=\"item_quantity\" step=\"0.01\" value=\"30000\" required />

          <label>Unit Price</label>
          <input type=\"number\" name=\"item_unit_price\" step=\"0.01\" value=\"2270\" required />

          <label>Truck No</label>
          <input type=\"text\" name=\"truck_no\" value=\"\" />

          <label>Driver Name</label>
          <input type=\"text\" name=\"driver_name\" value=\"\" />

          <label>Driver Phone</label>
          <input type=\"text\" name=\"driver_phone\" value=\"\" />

          <label>Original factory docs / originals (upload)</label>
          <input type=\"file\" name=\"original_docs\" multiple />
        </div>
        <div class=\"muted\">Run/Batch IDs, traceability, taxes, and funding are under Advanced. Amount is computed as Quantity × Unit Price.</div>
      </div>

      <details class=\"card\">
        <summary>Advanced (optional)</summary>
        <div class=\"row grid\">
          <label>Source/Producer (traceability)</label>
          <select name=\"source_id\">${source_options}</select>

          <label>Payment Terms</label>
          <input type=\"text\" name=\"payment_terms\" value=\"14 days\" />

          <label>Mode</label>
          <select name=\"mode\">
            <option value=\"normal_trade\" selected>normal_trade</option>
            <option value=\"contract_processing_pilot\">contract_processing_pilot</option>
          </select>

          <label>Funding Mode</label>
          <select name=\"funding_mode\">${funding_options}</select>

          <label>Funder policy file (optional)</label>
          <input type=\"text\" name=\"policy\" value=\"islamic_bank_policy.json\" />

          <label>Funder (required for murabaha)</label>
          <select name=\"funder_id\">${funder_options}</select>

          <label>Producer Posture (Lane B2)</label>
          <input type=\"checkbox\" name=\"producer_posture\" value=\"1\" />

          <label>Use Collection Account</label>
          <input type=\"checkbox\" name=\"use_collection_account\" value=\"1\" checked />

          <label>Include originals in customer pack (metadata only)</label>
          <input type=\"checkbox\" name=\"include_originals_in_customer_pack\" value=\"1\" />

          <label>Mode C: generate internal draft invoice</label>
          <input type=\"checkbox\" name=\"supplier_draft_only\" value=\"1\" />
        </div>

        <div class=\"row\">
          <h4>Product details</h4>
          <div class=\"grid\">
            <label>Order Type</label>
            <input type=\"text\" name=\"order_type\" value=\"Raw Materials\" />

            <label>Order Sub Type</label>
            <input type=\"text\" name=\"order_sub_type\" value=\"Refined Oil\" />

            <label>Batch ID</label>
            <input type=\"text\" name=\"batch_id\" value=\"\" placeholder=\"Leave blank to auto-generate\" />

            <label>Run ID</label>
            <input type=\"text\" name=\"run_id\" value=\"\" placeholder=\"Leave blank to auto-generate\" />

            <label>Unit</label>
            <input type=\"text\" name=\"item_unit\" value=\"kgs\" />

            <label>Description</label>
            <input type=\"text\" name=\"item_description\" value=\"\" placeholder=\"Leave blank to auto-fill\" />

            <label>Tax Class</label>
            <input type=\"text\" name=\"item_tax_class\" value=\"agri_input\" />

            <label>Manufacture Date</label>
            <input type=\"date\" name=\"manufacture_date\" value=\"${invoice_date}\" />

            <label>Expiry Date</label>
            <input type=\"date\" name=\"expiry_date\" value=\"${expiry_date}\" />
          </div>
        </div>

        <div class=\"row\">
          <h4>Logistics</h4>
          <div class=\"grid\">
            <label>Delivery Terms</label>
            <input type=\"text\" name=\"delivery_terms\" value=\"DAP\" />

            <label>Packaging</label>
            <input type=\"text\" name=\"packaging\" value=\"Bulk\" />

            <label>Ship-to (name)</label>
            <input type=\"text\" name=\"ship_to_name\" value=\"\" placeholder=\"Leave blank to use buyer\" />

            <label>Ship-to (address)</label>
            <input type=\"text\" name=\"ship_to_address\" value=\"\" placeholder=\"Leave blank to use buyer\" />
          </div>
        </div>

        <div class=\"row\">
          <h4>Tax & Charges</h4>
          <div class=\"grid\">
            <label>Transport Charges</label>
            <input type=\"number\" name=\"transport_charges\" step=\"0.01\" value=\"0\" />

            <label>Installation Charges</label>
            <input type=\"number\" name=\"installation_charges\" step=\"0.01\" value=\"0\" />

            <label>VAT Rate (%)</label>
            <input type=\"number\" name=\"vat_rate\" step=\"0.01\" value=\"0\" />

            <label>Discount</label>
            <input type=\"number\" name=\"discount\" step=\"0.01\" value=\"0\" />

            <label>WHT Applicable</label>
            <input type=\"checkbox\" name=\"wht_applicable\" value=\"1\" />

            <label>WHT Rate (%)</label>
            <input type=\"number\" name=\"wht_rate\" step=\"0.01\" value=\"0\" />
          </div>
        </div>

        <div class=\"row\">
          <h4>Notes</h4>
          <textarea name=\"notes\"></textarea>
        </div>

        <div class=\"muted\">Uploaded originals are stored under <code>output/.../evidence/originals</code> and hashed into <code>manifest.json</code>.</div>
      </details>

      <button type=\"submit\">Generate</button>
    </form>
  </div>

  <div class=\"card\">
    <h3>JSON Mode</h3>
    <p class=\"muted\">Paste a transaction JSON payload.</p>
    <form method=\"POST\" action=\"/generate-json\">
      <div class=\"row\">
        <label>Funder policy file (optional): </label>
        <input type=\"text\" name=\"policy\" value=\"islamic_bank_policy.json\" />
      </div>
      <div class=\"row\">
        <textarea name=\"payload\">${payload}</textarea>
      </div>
      <button type=\"submit\">Generate</button>
    </form>
  </div>

  <div class=\"card\">
    <h3>Phase 1.5 STP (Automation Test)</h3>
    <p class=\"muted\">Simplified path: one click quick run with fresh IDs.</p>
    <form method=\"POST\" action=\"/stp-quick-run\" style=\"margin-bottom:10px;\">
      <div class=\"row\">
        <label>As-of Date</label>
        <input type=\"date\" name=\"as_of_date\" value=\"${stp_as_of_date}\" required />
      </div>
      <div class=\"row\">
        <label>Generate PDFs</label>
        <input type=\"checkbox\" name=\"generate_pdfs\" value=\"1\" checked />
      </div>
      <div class=\"row\">
        <button type=\"submit\">Quick Run (Latest + New IDs)</button>
      </div>
    </form>
    <details class=\"card\">
      <summary>Advanced STP Controls</summary>
      <p class=\"muted\">Use this panel for manual payload edits and exception operations.</p>
    <form method=\"POST\" action=\"/stp-load-latest-sample\" style=\"display:inline-block; margin-right:10px;\">
      <button type=\"submit\">Use Latest Real Sample</button>
    </form>
    <form method=\"POST\" action=\"/stp-load-latest-sample-unique\" style=\"display:inline-block;\">
      <button type=\"submit\">Use Latest + New IDs</button>
    </form>
    <p class=\"muted\">Current STP sample source: ${stp_sample_source}</p>
    <form method=\"POST\" action=\"/stp-auto-run\">
      <div class=\"row\">
        <label>As-of Date</label>
        <input type=\"date\" name=\"as_of_date\" value=\"${stp_as_of_date}\" required />
      </div>
      <div class=\"row\">
        <label>Dry Run</label>
        <input type=\"checkbox\" name=\"dry_run\" value=\"1\" />
      </div>
      <div class=\"row\">
        <textarea name=\"stp_payload\">${stp_payload}</textarea>
      </div>
      <button type=\"submit\">Run STP</button>
    </form>

    <details class=\"card\">
      <summary>Exceptions / Resume</summary>
      <form method=\"POST\" action=\"/stp-list-exceptions\">
        <div class=\"row\">
          <label>Run ID</label>
          <input type=\"text\" name=\"run_id\" placeholder=\"01...\" />
          <button type=\"submit\">List Open Exceptions</button>
        </div>
      </form>
      <form method=\"POST\" action=\"/stp-resolve-exception\">
        <div class=\"row\">
          <label>Exception ID</label>
          <input type=\"text\" name=\"exception_id\" placeholder=\"01...\" required />
        </div>
        <div class=\"row\">
          <label>Resolved Value (JSON or text)</label>
          <textarea name=\"resolved_value\">{}</textarea>
        </div>
        <div class=\"row\">
          <label>Note</label>
          <input type=\"text\" name=\"note\" value=\"Resolved from UI\" />
          <button type=\"submit\">Resolve Exception</button>
        </div>
      </form>
      <form method=\"POST\" action=\"/stp-auto-resume\">
        <div class=\"row\">
          <label>Run ID</label>
          <input type=\"text\" name=\"run_id\" placeholder=\"01...\" required />
          <button type=\"submit\">Resume STP Run</button>
        </div>
      </form>
    </details>
    </details>
  </div>

  ${result}
</body>
</html>""")


def run_server(root_dir: Path, host: str = "127.0.0.1", port: int = 8765) -> None:
    generator = DeliveryPackGenerator(root_dir)
    runtime_config = RuntimeConfig.load(root_dir)
    repo = SQLiteRepo(runtime_config.state_dir / "drep.sqlite")
    phase1 = Phase1Service(runtime_config, repo)
    orchestrator = AutomationOrchestrator(runtime_config, repo, phase1)
    phase1.init_db()
    sample_payload = read_json(root_dir / "data" / "sample_transaction_contract_processing.json")
    stp_sample_path = root_dir / "examples" / "stp" / "known_complete.json"
    stp_default_payload = read_json(stp_sample_path) if stp_sample_path.exists() else sample_payload
    system = generator.load_system_profile()

    registry = generator.entity_registry
    buyers = sorted(
        [(entity_id, entity.name) for entity_id, entity in registry.entities.items() if entity_id.startswith("buyer_")],
        key=lambda row: row[1].lower(),
    )
    processors = sorted(
        [(entity_id, entity.name) for entity_id, entity in registry.entities.items() if entity_id.startswith("processor_")],
        key=lambda row: row[1].lower(),
    )
    funders = [("", "-")] + sorted(
        [(entity_id, entity.name) for entity_id, entity in registry.entities.items() if entity_id.startswith("funder_")],
        key=lambda row: row[1].lower(),
    )
    vendors = sorted(
        [
            (entity_id, entity.name)
            for entity_id, entity in registry.entities.items()
            if not entity_id.startswith(("buyer_", "processor_", "funder_"))
        ],
        key=lambda row: row[1].lower(),
    )
    sources = sorted(
        [
            (entity_id, entity.name)
            for entity_id, entity in registry.entities.items()
            if not entity_id.startswith(("buyer_", "funder_"))
        ],
        key=lambda row: row[1].lower(),
    )

    product_codes = sorted([str(key).upper() for key in generator.coa_profiles.keys()])
    product_code_options = [(code, code) for code in product_codes] or [("RBDSO", "RBDSO")]

    funding_options = [
        ("none", "none"),
        ("murabaha_supplier_direct", "murabaha_supplier_direct"),
        ("direct_facility_to_merchant", "direct_facility_to_merchant"),
        ("controlled_disbursement_on_behalf_of_merchant", "controlled_disbursement_on_behalf_of_merchant"),
        ("receivables_finance", "receivables_finance"),
        ("hybrid_topup", "hybrid_topup"),
    ]

    def _load_latest_real_sample_payload() -> tuple[dict[str, object], str]:
        live_runs_dir = runtime_config.state_dir / "automation" / "live_runs"
        if live_runs_dir.exists():
            candidates = sorted(
                [path for path in live_runs_dir.rglob("real_input*.json") if path.is_file()],
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
            for candidate in candidates:
                try:
                    payload = read_json(candidate)
                    rel = candidate.relative_to(root_dir)
                    return payload, str(rel)
                except Exception:
                    continue
        default_source = str(stp_sample_path.relative_to(root_dir)) if stp_sample_path.exists() else "default sample payload"
        return stp_default_payload, default_source

    def _with_unique_ids(payload: dict[str, object]) -> dict[str, object]:
        unique = json.loads(json.dumps(payload))
        stamp = dt.datetime.utcnow().strftime("%Y%m%d%H%M%S")

        def suffix(value: str, prefix: str) -> str:
            base = value.strip() if value else prefix
            return f"{base}-U{stamp}"

        unique["lpo_no"] = suffix(str(unique.get("lpo_no") or unique.get("contract_ref") or ""), "LPO")
        unique["run_id"] = suffix(str(unique.get("run_id") or ""), "RUN")
        unique["delivery_ref"] = suffix(str(unique.get("delivery_ref") or ""), "DLV")

        vendor_id = str(unique.get("vendor_of_record_id") or "")
        product_code = str(unique.get("product_code") or "PRODUCT").upper()
        vendor_code = "VENDOR"
        if vendor_id:
            try:
                vendor_code = (registry.get(vendor_id).code or vendor_id).upper()
            except Exception:
                vendor_code = vendor_id.upper()
        unique["batch_id"] = f"{vendor_code}-{product_code}-{stamp}-01"

        payment = unique.get("payment")
        if isinstance(payment, dict):
            payment["external_reference"] = suffix(str(payment.get("external_reference") or ""), "PAY")
            unique["payment"] = payment

        return unique

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            today = dt.date.today()
            invoice_date = today.isoformat()
            due_date = (today + dt.timedelta(days=14)).isoformat()
            expiry_date = (today + dt.timedelta(days=365)).isoformat()
            payload = _escape(json.dumps(sample_payload, indent=2))
            stp_payload_dict, stp_sample_source = _load_latest_real_sample_payload()
            stp_payload = _escape(json.dumps(stp_payload_dict, indent=2))
            page = HTML_PAGE.substitute(
                payload=payload,
                stp_payload=stp_payload,
                stp_sample_source=_escape(stp_sample_source),
                stp_as_of_date=today.isoformat(),
                result="",
                buyer_options=_options(buyers, selected="buyer_nycil"),
                vendor_options=_options(vendors, selected="ananta_flows"),
                source_options=_options(sources, selected="ananta_flows"),
                processor_options=_options(processors, selected="processor_partner_refinery"),
                funder_options=_options(funders, selected=""),
                funding_options=_options(funding_options, selected="none"),
                product_code_options=_options(product_code_options, selected="RBDSO"),
                invoice_date=invoice_date,
                due_date=due_date,
                expiry_date=expiry_date,
            )
            self._send_html(page)

        def do_POST(self) -> None:  # noqa: N802
            if self.path == "/generate-json":
                self._handle_generate_json()
                return
            if self.path == "/generate-form":
                self._handle_generate_form()
                return
            if self.path == "/stp-auto-run":
                self._handle_stp_auto_run()
                return
            if self.path == "/stp-quick-run":
                self._handle_stp_quick_run()
                return
            if self.path == "/stp-list-exceptions":
                self._handle_stp_list_exceptions()
                return
            if self.path == "/stp-resolve-exception":
                self._handle_stp_resolve_exception()
                return
            if self.path == "/stp-auto-resume":
                self._handle_stp_auto_resume()
                return
            if self.path == "/stp-load-latest-sample":
                self._handle_stp_load_latest_sample()
                return
            if self.path == "/stp-load-latest-sample-unique":
                self._handle_stp_load_latest_sample_unique()
                return
            self.send_error(404)

        def log_message(self, format: str, *args) -> None:  # noqa: A003
            return

        def _send_html(self, content: str) -> None:
            encoded = content.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _render(
            self,
            *,
            payload_raw: str,
            result_html: str,
            stp_payload_raw: str | None = None,
            stp_as_of_date: str | None = None,
            stp_sample_source: str | None = None,
        ) -> None:
            today = dt.date.today()
            invoice_date = today.isoformat()
            due_date = (today + dt.timedelta(days=14)).isoformat()
            expiry_date = (today + dt.timedelta(days=365)).isoformat()
            if stp_payload_raw is None:
                payload_dict, source = _load_latest_real_sample_payload()
                stp_payload_raw = json.dumps(payload_dict, indent=2)
                if stp_sample_source is None:
                    stp_sample_source = source
            if stp_as_of_date is None:
                stp_as_of_date = today.isoformat()
            if stp_sample_source is None:
                stp_sample_source = "manual/custom"
            page = HTML_PAGE.substitute(
                payload=_escape(payload_raw),
                stp_payload=_escape(stp_payload_raw),
                stp_sample_source=_escape(stp_sample_source),
                stp_as_of_date=_escape(stp_as_of_date),
                result=result_html,
                buyer_options=_options(buyers, selected="buyer_nycil"),
                vendor_options=_options(vendors, selected="ananta_flows"),
                source_options=_options(sources, selected="ananta_flows"),
                processor_options=_options(processors, selected="processor_partner_refinery"),
                funder_options=_options(funders, selected=""),
                funding_options=_options(funding_options, selected="none"),
                product_code_options=_options(product_code_options, selected="RBDSO"),
                invoice_date=invoice_date,
                due_date=due_date,
                expiry_date=expiry_date,
            )
            self._send_html(page)

        def _handle_generate_json(self) -> None:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            fields = parse_qs(body)
            payload_raw = fields.get("payload", ["{}"])[0]
            policy = fields.get("policy", [""])[0].strip() or None

            try:
                payload_dict = json.loads(payload_raw)
                result = generator.generate(payload_dict, funder_policy_file=policy)
                result_payload = {
                    "output_dir": result.output_dir,
                    "manifest_path": result.manifest_path,
                    "documents": [doc.key for doc in result.documents],
                }
                result_html = (
                    "<h3>Generated</h3>"
                    f"<pre>{_escape(json.dumps(result_payload, indent=2))}</pre>"
                    "<h4>Payload</h4>"
                    f"<pre>{_escape(json.dumps(payload_dict, indent=2))}</pre>"
                )
            except Exception as error:  # pragma: no cover - runtime surface
                result_html = f"<h3 class='danger'>Error</h3><pre>{_escape(str(error))}</pre>"

            self._render(payload_raw=payload_raw, result_html=result_html)

        def _handle_generate_form(self) -> None:
            form = cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ={
                    "REQUEST_METHOD": "POST",
                    "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                },
            )

            def get(name: str, default: str = "") -> str:
                value = form.getfirst(name, default)
                return str(value).strip()

            def get_bool(name: str) -> bool:
                return bool(get(name))

            temp_dir: Path | None = None
            stored_originals: list[str] = []
            try:
                invoice_date = get("invoice_date")
                due_date = get("due_date")
                if not due_date:
                    try:
                        parsed = dt.date.fromisoformat(invoice_date)
                        due_date = (parsed + dt.timedelta(days=14)).isoformat()
                    except Exception:
                        due_date = invoice_date

                mode = get("mode", "normal_trade") or "normal_trade"
                funding_mode = get("funding_mode", "none") or "none"

                buyer_id = get("buyer_id")
                vendor_id = get("vendor_of_record_id")
                source_id = get("source_id")
                processor_id = get("processor_id")
                funder_id = get("funder_id")
                policy = get("policy") or None

                buyer_entity = registry.get(buyer_id) if buyer_id else None
                ship_to_name = get("ship_to_name")
                ship_to_address = get("ship_to_address")
                if not ship_to_name and buyer_entity:
                    ship_to_name = buyer_entity.name
                if not ship_to_address and buyer_entity:
                    ship_to_address = buyer_entity.address

                product_code = get("product_code").upper().strip()
                product_name = get("product_name")

                batch_id = get("batch_id")
                run_id = get("run_id")
                # Auto-generate if blank
                date_key = (invoice_date or "").replace("-", "") or dt.date.today().strftime("%Y%m%d")
                vendor_code = registry.get(vendor_id).code if vendor_id else "VENDOR"
                if not run_id:
                    run_id = f"RUN-{date_key}-01"
                if not batch_id and product_code:
                    batch_id = f"{vendor_code}-{product_code}-{date_key}-01"

                qty = float(get("item_quantity", "0") or 0)
                unit_price = float(get("item_unit_price", "0") or 0)
                amount = round(qty * unit_price, 2)
                item_unit = get("item_unit") or "kgs"
                item_description = get("item_description")
                if not item_description:
                    name_hint = product_name or product_code or "goods"
                    link_hint = run_id or batch_id or "RUN"
                    item_description = f"Supply of {name_hint} linked to {link_hint}"

                payload_dict = {
                    "invoice_date": invoice_date,
                    "due_date": due_date,
                    "currency": "NGN",
                    "payment_terms": get("payment_terms"),
                    "lpo_no": get("lpo_no"),
                    "lpo_date": get("lpo_date"),
                    "mode": mode,
                    "funding_mode": funding_mode,
                    "agreement_ref": get("agreement_ref"),
                    "order_type": get("order_type"),
                    "order_sub_type": get("order_sub_type"),
                    "product_name": product_name,
                    "product_code": product_code,
                    "batch_id": batch_id,
                    "run_id": run_id,
                    "manufacture_date": get("manufacture_date"),
                    "expiry_date": get("expiry_date"),
                    "delivery_terms": get("delivery_terms"),
                    "packaging": get("packaging"),
                    "truck_no": get("truck_no"),
                    "driver_name": get("driver_name"),
                    "driver_phone": get("driver_phone"),
                    "buyer_id": buyer_id,
                    "vendor_of_record_id": vendor_id,
                    "source_id": source_id,
                    "processor_id": processor_id,
                    "funder_id": funder_id,
                    "producer_posture": get_bool("producer_posture"),
                    "use_collection_account": get_bool("use_collection_account"),
                    "include_originals_in_customer_pack": get_bool("include_originals_in_customer_pack"),
                    "supplier_draft_only": get_bool("supplier_draft_only"),
                    "line_items": [
                        {
                            "description": item_description,
                            "quantity": qty,
                            "unit": item_unit,
                            "unit_price": unit_price,
                            "amount": amount,
                            "tax_class": get("item_tax_class") or "agri_input",
                        }
                    ],
                    "transport_charges": float(get("transport_charges", "0") or 0),
                    "installation_charges": float(get("installation_charges", "0") or 0),
                    "vat_rate": float(get("vat_rate", "0") or 0),
                    "discount": float(get("discount", "0") or 0),
                    "wht_applicable": get_bool("wht_applicable"),
                    "wht_rate": float(get("wht_rate", "0") or 0),
                    "notes": get("notes"),
                    "ship_to": {
                        "name": ship_to_name,
                        "address": ship_to_address,
                    },
                }

                # Store uploads in temp and pass paths through to generator (they will be copied into output evidence/originals)
                if "original_docs" in form:
                    files = form["original_docs"]
                    file_list = files if isinstance(files, list) else [files]
                    if any(getattr(item, "filename", "") for item in file_list):
                        temp_dir = Path(mkdtemp(prefix="ananta_upload_", dir="/tmp"))
                        for item in file_list:
                            filename = str(getattr(item, "filename", "") or "").strip()
                            if not filename:
                                continue
                            safe_name = "".join(ch for ch in filename if ch.isalnum() or ch in {"-", "_", ".", " "}).strip() or "upload"
                            dest = temp_dir / safe_name
                            with dest.open("wb") as handle:
                                shutil.copyfileobj(item.file, handle)
                            stored_originals.append(str(dest))
                        payload_dict["original_docs"] = stored_originals

                result = generator.generate(payload_dict, funder_policy_file=policy)
                result_payload = {
                    "output_dir": result.output_dir,
                    "manifest_path": result.manifest_path,
                    "documents": [doc.key for doc in result.documents],
                }
                result_html = (
                    "<h3>Generated</h3>"
                    f"<pre>{_escape(json.dumps(result_payload, indent=2))}</pre>"
                    "<h4>Payload</h4>"
                    f"<pre>{_escape(json.dumps(payload_dict, indent=2))}</pre>"
                )
                self._render(payload_raw=json.dumps(payload_dict, indent=2), result_html=result_html)
            except Exception as error:  # pragma: no cover - runtime surface
                extra = f"\nTemp upload dir: {temp_dir}" if temp_dir else ""
                result_html = f"<h3 class='danger'>Error</h3><pre>{_escape(str(error) + extra)}</pre>"
                self._render(payload_raw="{}", result_html=result_html)
            finally:
                if temp_dir and temp_dir.exists():
                    try:
                        shutil.rmtree(temp_dir)
                    except Exception:
                        pass

        def _parse_urlencoded_form(self) -> dict[str, list[str]]:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            return parse_qs(body)

        def _handle_stp_auto_run(self) -> None:
            fields = self._parse_urlencoded_form()
            payload_raw = fields.get("stp_payload", ["{}"])[0]
            as_of_date = fields.get("as_of_date", [dt.date.today().isoformat()])[0]
            dry_run = bool(fields.get("dry_run", [""]))
            try:
                payload_dict = json.loads(payload_raw)
                result = orchestrator.auto_run(payload_dict, as_of_date=as_of_date, dry_run=dry_run)
                result_html = "<h3>STP Run Result</h3>" f"<pre>{_escape(json.dumps(result, indent=2))}</pre>"
            except Exception as error:
                result_html = f"<h3 class='danger'>STP Error</h3><pre>{_escape(str(error))}</pre>"
            self._render(
                payload_raw=json.dumps(sample_payload, indent=2),
                result_html=result_html,
                stp_payload_raw=payload_raw,
                stp_as_of_date=as_of_date,
                stp_sample_source="manual/custom",
            )

        def _handle_stp_quick_run(self) -> None:
            fields = self._parse_urlencoded_form()
            as_of_date = fields.get("as_of_date", [dt.date.today().isoformat()])[0]
            generate_pdfs = bool(fields.get("generate_pdfs", [""]))
            try:
                payload_dict, source = _load_latest_real_sample_payload()
                payload_dict = _with_unique_ids(payload_dict)
                payload_dict["skip_pdf"] = not generate_pdfs
                payload_dict["allow_placeholder_tin"] = True
                result = orchestrator.auto_run(payload_dict, as_of_date=as_of_date, dry_run=False)

                pack = ((result.get("results") or {}).get("pack") or {}) if isinstance(result, dict) else {}
                output_dir = pack.get("output_dir")
                manifest_path = pack.get("manifest_path")
                summary = {
                    "run_id": result.get("run_id"),
                    "status": result.get("status"),
                    "output_dir": output_dir,
                    "manifest_path": manifest_path,
                    "open_exceptions": len(result.get("open_exceptions") or []),
                    "generate_pdfs": generate_pdfs,
                    "sample_source": source,
                }
                result_html = (
                    "<h3>STP Quick Run Result</h3>"
                    f"<pre>{_escape(json.dumps(summary, indent=2))}</pre>"
                    "<h4>Full Result</h4>"
                    f"<pre>{_escape(json.dumps(result, indent=2))}</pre>"
                )
                self._render(
                    payload_raw=json.dumps(sample_payload, indent=2),
                    result_html=result_html,
                    stp_payload_raw=json.dumps(payload_dict, indent=2),
                    stp_as_of_date=as_of_date,
                    stp_sample_source=f"{source} (unique IDs)",
                )
            except Exception as error:
                result_html = f"<h3 class='danger'>STP Quick Run Error</h3><pre>{_escape(str(error))}</pre>"
                self._render(
                    payload_raw=json.dumps(sample_payload, indent=2),
                    result_html=result_html,
                    stp_payload_raw=json.dumps(stp_default_payload, indent=2),
                    stp_as_of_date=as_of_date,
                    stp_sample_source="default",
                )

        def _handle_stp_list_exceptions(self) -> None:
            fields = self._parse_urlencoded_form()
            run_id = fields.get("run_id", [""])[0].strip() or None
            try:
                rows = orchestrator.list_exceptions(run_id=run_id)
                result_html = "<h3>Open Exceptions</h3>" f"<pre>{_escape(json.dumps({'run_id': run_id, 'exceptions': rows}, indent=2))}</pre>"
            except Exception as error:
                result_html = f"<h3 class='danger'>Exceptions Error</h3><pre>{_escape(str(error))}</pre>"
            self._render(
                payload_raw=json.dumps(sample_payload, indent=2),
                result_html=result_html,
                stp_payload_raw=json.dumps(stp_default_payload, indent=2),
                stp_as_of_date=dt.date.today().isoformat(),
                stp_sample_source="default",
            )

        def _handle_stp_resolve_exception(self) -> None:
            fields = self._parse_urlencoded_form()
            exception_id = fields.get("exception_id", [""])[0].strip()
            resolved_value = fields.get("resolved_value", [""])[0]
            note = fields.get("note", ["Resolved from UI"])[0]
            try:
                if not exception_id:
                    raise ValueError("exception_id is required")
                result = orchestrator.resolve_exception(exception_id=exception_id, value=resolved_value, note=note)
                result_html = "<h3>Exception Resolved</h3>" f"<pre>{_escape(json.dumps(result, indent=2))}</pre>"
            except Exception as error:
                result_html = f"<h3 class='danger'>Resolve Error</h3><pre>{_escape(str(error))}</pre>"
            self._render(
                payload_raw=json.dumps(sample_payload, indent=2),
                result_html=result_html,
                stp_payload_raw=json.dumps(stp_default_payload, indent=2),
                stp_as_of_date=dt.date.today().isoformat(),
                stp_sample_source="default",
            )

        def _handle_stp_auto_resume(self) -> None:
            fields = self._parse_urlencoded_form()
            run_id = fields.get("run_id", [""])[0].strip()
            try:
                if not run_id:
                    raise ValueError("run_id is required")
                result = orchestrator.auto_resume(run_id)
                result_html = "<h3>STP Resume Result</h3>" f"<pre>{_escape(json.dumps(result, indent=2))}</pre>"
            except Exception as error:
                result_html = f"<h3 class='danger'>Resume Error</h3><pre>{_escape(str(error))}</pre>"
            self._render(
                payload_raw=json.dumps(sample_payload, indent=2),
                result_html=result_html,
                stp_payload_raw=json.dumps(stp_default_payload, indent=2),
                stp_as_of_date=dt.date.today().isoformat(),
                stp_sample_source="default",
            )

        def _handle_stp_load_latest_sample(self) -> None:
            _ = self._parse_urlencoded_form()
            payload_dict, source = _load_latest_real_sample_payload()
            result_html = "<h3>Loaded Latest Real Sample</h3>" f"<pre>{_escape(source)}</pre>"
            self._render(
                payload_raw=json.dumps(sample_payload, indent=2),
                result_html=result_html,
                stp_payload_raw=json.dumps(payload_dict, indent=2),
                stp_as_of_date=dt.date.today().isoformat(),
                stp_sample_source=source,
            )

        def _handle_stp_load_latest_sample_unique(self) -> None:
            _ = self._parse_urlencoded_form()
            payload_dict, source = _load_latest_real_sample_payload()
            unique_payload = _with_unique_ids(payload_dict)
            result_html = (
                "<h3>Loaded Latest Real Sample + New IDs</h3>"
                f"<pre>{_escape(source)}</pre>"
            )
            self._render(
                payload_raw=json.dumps(sample_payload, indent=2),
                result_html=result_html,
                stp_payload_raw=json.dumps(unique_payload, indent=2),
                stp_as_of_date=dt.date.today().isoformat(),
                stp_sample_source=f"{source} (unique IDs)",
            )

    server = HTTPServer((host, port), Handler)
    print(f"Serving on http://{host}:{port}")
    server.serve_forever()
