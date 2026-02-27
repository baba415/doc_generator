from __future__ import annotations

import cgi
import datetime as dt
import html
import json
import re
import shutil
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from tempfile import mkdtemp
from urllib.parse import parse_qs, quote_plus, urlparse

from adapters.sqlite_repo import SQLiteRepo
from core.config import RuntimeConfig
from core.ids import new_ulid
from core.time import utc_now_iso_z
from core.units import kg_to_mt_str, mt_to_kg_int
from domain.services import Phase1Service


def _escape(value: object) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _checkbox(name: str, label: str, checked: bool = True) -> str:
    checked_attr = " checked" if checked else ""
    return f"<label><input type='checkbox' name='{_escape(name)}' value='1'{checked_attr} /> {_escape(label)}</label>"


def _fmt_qty_mt_from_kg(value: object) -> str:
    return kg_to_mt_str(int(value or 0))


def _safe_filename(name: str) -> str:
    cleaned = "".join(ch for ch in name if ch.isalnum() or ch in {"-", "_", ".", " "}).strip()
    return cleaned or "upload"


def run_server_v2(root_dir: Path, host: str = "127.0.0.1", port: int = 8865) -> None:
    config = RuntimeConfig.load(root_dir)
    repo = SQLiteRepo(config.state_dir / "drep.sqlite")
    service = Phase1Service(config, repo)
    service.init_db()

    route_plan = re.compile(r"^/v2/contracts/([^/]+)/plan$")
    route_execute = re.compile(r"^/v2/contracts/([^/]+)/execute$")
    route_settle = re.compile(r"^/v2/contracts/([^/]+)/settle$")
    route_plan_rebuild = re.compile(r"^/v2/contracts/([^/]+)/plan/rebuild$")
    route_plan_update = re.compile(r"^/v2/contracts/([^/]+)/plan/update$")
    route_execute_due = re.compile(r"^/v2/contracts/([^/]+)/execute/materialize-due$")
    route_execute_one = re.compile(r"^/v2/contracts/([^/]+)/execute/materialize-one$")
    route_generate_pack = re.compile(r"^/v2/contracts/([^/]+)/execute/generate-pack$")
    route_settle_paid = re.compile(r"^/v2/contracts/([^/]+)/settle/mark-paid$")
    route_settle_export = re.compile(r"^/v2/contracts/([^/]+)/settle/export-drep$")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:  # noqa: A003
            return

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            route = parsed.path
            query = parse_qs(parsed.query)
            try:
                if route in {"/", "/v2"}:
                    self._redirect("/v2/portfolio")
                    return
                if route == "/v2/portfolio":
                    self._render_portfolio(query)
                    return
                if route == "/v2/intake":
                    self._render_intake(query)
                    return
                if route == "/v2/exceptions":
                    self._render_exceptions(query)
                    return

                match = route_plan.match(route)
                if match:
                    self._render_plan(match.group(1), query)
                    return

                match = route_execute.match(route)
                if match:
                    self._render_execute(match.group(1), query)
                    return

                match = route_settle.match(route)
                if match:
                    self._render_settle(match.group(1), query)
                    return

                self.send_error(404)
            except Exception as error:  # pragma: no cover - runtime path
                self._render_error(error)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            route = parsed.path
            try:
                if route == "/v2/init-db":
                    service.init_db()
                    self._flash_redirect("/v2/portfolio", "Database initialized")
                    return
                if route in {"/v2/intake", "/v2/intake-plan"}:
                    self._handle_intake()
                    return
                if route == "/v2/exceptions/resolve":
                    self._handle_exception_resolve()
                    return
                if route in {"/v2/materialize-run"}:
                    self._handle_materialize_legacy()
                    return
                if route in {"/v2/mark-paid"}:
                    self._handle_mark_paid_legacy()
                    return
                if route in {"/v2/export-drep"}:
                    self._handle_export_legacy()
                    return

                match = route_plan_rebuild.match(route)
                if match:
                    self._handle_plan_rebuild(match.group(1))
                    return
                match = route_plan_update.match(route)
                if match:
                    self._handle_plan_update(match.group(1))
                    return
                match = route_execute_due.match(route)
                if match:
                    self._handle_execute_materialize_due(match.group(1))
                    return
                match = route_execute_one.match(route)
                if match:
                    self._handle_execute_materialize_one(match.group(1))
                    return
                match = route_generate_pack.match(route)
                if match:
                    self._handle_execute_generate_pack(match.group(1))
                    return
                match = route_settle_paid.match(route)
                if match:
                    self._handle_settle_mark_paid(match.group(1))
                    return
                match = route_settle_export.match(route)
                if match:
                    self._handle_settle_export(match.group(1))
                    return
                self.send_error(404)
            except Exception as error:  # pragma: no cover - runtime path
                self._render_error(error)

        def _render_intake(self, query: dict[str, list[str]]) -> None:
            msg, level = self._msg(query)
            today = dt.date.today().isoformat()
            due = (dt.date.today() + dt.timedelta(days=14)).isoformat()
            buyers = self._entity_options(prefix="buyer_")
            vendors = [("guildgate", config.registry.get("guildgate").name), ("ananta_flows", config.registry.get("ananta_flows").name)]
            processors = self._entity_options(prefix="processor_")
            sources = self._entity_options(prefix="", exclude_prefixes=("buyer_", "funder_"))
            product_codes = sorted(
                {
                    *[str(code).upper() for code in config.coa_profiles.keys()],
                    *[str(code).upper() for code in (config.delivery_policies.get("products") or {}).keys()],
                }
            )
            body = [
                "<h2>Intake (upload/create LPO)</h2>",
                "<p class='muted'>Automation-first: one action creates contract and plans deliveries using lot policy.</p>",
                "<form method='POST' action='/v2/intake' enctype='multipart/form-data'>",
                "<div class='grid'>",
                "<label>LPO No / Contract Ref</label><input type='text' name='lpo_no' required />",
                f"<label>LPO Date</label><input type='date' name='lpo_date' value='{today}' />",
                f"<label>Issue Date</label><input type='date' name='issue_date' value='{today}' required />",
                f"<label>LPO Valid From</label><input type='date' name='lpo_valid_from' value='{today}' />",
                f"<label>LPO Valid To</label><input type='date' name='lpo_valid_to' value='{due}' />",
                f"<label>Buyer</label>{self._select('buyer_id', buyers, selected='buyer_nycil')}",
                f"<label>Vendor-of-record (Lane A/B)</label>{self._select('vendor_of_record_id', vendors, selected='ananta_flows')}",
                f"<label>Source / Producer</label>{self._select('source_id', sources, selected='ananta_flows')}",
                f"<label>Processor</label>{self._select('processor_id', processors, selected='processor_partner_refinery')}",
                f"<label>Product Code</label>{self._select('product_code', [(item, item) for item in product_codes], selected='RBDPO')}",
                "<label>Description</label><input type='text' name='description' value='Supply linked to contract' />",
                "<label>Expected Qty (MT)</label><input type='number' step='0.001' name='expected_qty_mt' value='150' />",
                "<label>Unit Price (per KG)</label><input type='number' step='0.01' name='unit_price' value='2270' />",
                "<label>Currency</label><input type='text' name='currency' value='NGN' />",
                f"<label>Plan Start Date</label><input type='date' name='start_date' value='{today}' />",
                "<label>Cadence</label><select name='cadence'><option value='daily'>daily</option><option value='manual'>manual</option></select>",
                "<label>Max Lots / Day</label><input type='number' min='1' name='max_lots_per_day' value='1' />",
                "<label>Tolerance %</label><input type='number' step='0.01' name='tolerance_pct' value='5.0' />",
                "<label>Original LPO / evidence files</label><input type='file' name='lpo_originals' multiple />",
                "<label>Options</label><div>"
                + _checkbox("allow_placeholder_tin", "Allow placeholder TIN (dev)", True)
                + "</div>",
                "</div>",
                "<div class='actions'><button type='submit'>Create Contract + Auto Plan</button></div>",
                "</form>",
            ]
            self._render_page("Intake", "".join(body), active="intake", msg=msg, level=level)

        def _render_portfolio(self, query: dict[str, list[str]]) -> None:
            msg, level = self._msg(query)
            rows = service.dashboard_rows(limit=300)
            plan_stats_rows = repo.fetch_all(
                """
                SELECT
                  contract_id,
                  COUNT(*) AS planned_total,
                  SUM(CASE WHEN status IN ('PLANNED','SCHEDULED') THEN 1 ELSE 0 END) AS open_plans
                FROM planned_deliveries
                GROUP BY contract_id
                """
            )
            sales_stats_rows = repo.fetch_all(
                """
                SELECT
                  contract_id,
                  COUNT(*) AS invoice_count,
                  SUM(outstanding_balance) AS outstanding_total
                FROM drep_sales
                GROUP BY contract_id
                """
            )
            plan_stats = {row["contract_id"]: row for row in plan_stats_rows}
            sales_stats = {row["contract_id"]: row for row in sales_stats_rows}

            table = [
                "<h2>Portfolio (workflow landing)</h2>",
                "<p class='muted'>Open LPOs with next actions: Plan, Execute, Settle.</p>",
                "<table><thead><tr><th>Contract</th><th>Buyer</th><th>Vendor</th><th>LPO State</th><th>Status</th><th>Expected (MT)</th><th>Delivered (MT)</th><th>Open Plans</th><th>Outstanding</th><th>Next Action</th></tr></thead><tbody>",
            ]
            for row in rows["contracts"]:
                contract_id = str(row["contract_id"])
                plan_stat = plan_stats.get(contract_id, {})
                sales_stat = sales_stats.get(contract_id, {})
                open_plans = int(plan_stat.get("open_plans") or 0)
                outstanding = float(sales_stat.get("outstanding_total") or 0.0)
                next_action = "Plan" if open_plans == 0 else ("Settle" if outstanding > 0 else "Execute")
                next_href = (
                    f"/v2/contracts/{contract_id}/plan"
                    if next_action == "Plan"
                    else (f"/v2/contracts/{contract_id}/settle" if next_action == "Settle" else f"/v2/contracts/{contract_id}/execute")
                )
                table.append(
                    "<tr>"
                    f"<td>{_escape(row.get('lpo_no') or row.get('contract_ref'))}</td>"
                    f"<td>{_escape(row.get('buyer_id'))}</td>"
                    f"<td>{_escape(row.get('vendor_of_record_id'))}</td>"
                    f"<td><span class='pill'>{_escape(row.get('lpo_state'))}</span></td>"
                    f"<td><span class='pill'>{_escape(row.get('status'))}</span></td>"
                    f"<td>{_escape(_fmt_qty_mt_from_kg(row.get('expected_total_qty_kg')))}</td>"
                    f"<td>{_escape(_fmt_qty_mt_from_kg(row.get('delivered_qty_total_kg')))}</td>"
                    f"<td>{_escape(open_plans)}</td>"
                    f"<td>{_escape(f'{outstanding:,.2f}')}</td>"
                    f"<td><a href='{_escape(next_href)}'>{_escape(next_action)}</a></td>"
                    "</tr>"
                )
            if not rows["contracts"]:
                table.append("<tr><td colspan='10' class='muted'>No contracts yet.</td></tr>")
            table.append("</tbody></table>")
            self._render_page("Portfolio", "".join(table), active="portfolio", msg=msg, level=level)

        def _render_plan(self, contract_id: str, query: dict[str, list[str]]) -> None:
            msg, level = self._msg(query)
            contract = self._contract(contract_id)
            planned_rows = service.planned_rows(contract_id=contract_id, limit=500)
            today = dt.date.today().isoformat()
            parts = [
                f"<h2>Plan - { _escape(contract.get('lpo_no') or contract_id) }</h2>",
                "<p class='muted'>Auto-split and schedule preview. Edit quantity/date only for exceptions.</p>",
                self._contract_summary(contract),
                f"<form method='POST' action='/v2/contracts/{_escape(contract_id)}/plan/rebuild'><div class='inline-grid'>"
                f"<label>Start Date <input type='date' name='start_date' value='{today}' /></label>"
                "<label>Cadence <select name='cadence'><option value='daily'>daily</option><option value='manual'>manual</option></select></label>"
                "<label>Max lots/day <input type='number' min='1' name='max_lots_per_day' value='1' /></label>"
                "<button type='submit'>Rebuild Plan</button>"
                "</div></form>",
                "<table><thead><tr><th>Seq</th><th>Planned Date</th><th>Planned Qty (MT)</th><th>Status</th><th>Delivery</th><th>Edit</th></tr></thead><tbody>",
            ]
            for row in planned_rows:
                planned_id = str(row["planned_delivery_id"])
                editable = str(row.get("status") or "").upper() in {"PLANNED", "SCHEDULED"}
                edit_form = "-"
                if editable:
                    edit_form = (
                        f"<form method='POST' action='/v2/contracts/{_escape(contract_id)}/plan/update' class='inline-form'>"
                        f"<input type='hidden' name='planned_delivery_id' value='{_escape(planned_id)}' />"
                        f"<input type='date' name='planned_date' value='{_escape(str(row.get('planned_date') or today))}' />"
                        f"<input type='number' step='0.001' name='planned_qty_mt' value='{_escape(_fmt_qty_mt_from_kg(row.get('planned_qty_kg')))}' />"
                        "<button type='submit'>Save</button>"
                        "</form>"
                    )
                parts.append(
                    "<tr>"
                    f"<td>{_escape(row.get('sequence_no'))}</td>"
                    f"<td>{_escape(row.get('planned_date'))}</td>"
                    f"<td>{_escape(_fmt_qty_mt_from_kg(row.get('planned_qty_kg')))}</td>"
                    f"<td><span class='pill'>{_escape(row.get('status'))}</span></td>"
                    f"<td>{_escape(row.get('delivery_id') or '-')}</td>"
                    f"<td>{edit_form}</td>"
                    "</tr>"
                )
            if not planned_rows:
                parts.append("<tr><td colspan='6' class='muted'>No planned deliveries yet.</td></tr>")
            parts.append("</tbody></table>")
            parts.append(
                f"<div class='actions'><a class='btn' href='/v2/contracts/{_escape(contract_id)}/execute'>Proceed to Execute</a></div>"
            )
            self._render_page("Plan", "".join(parts), active="plan", msg=msg, level=level)

        def _render_execute(self, contract_id: str, query: dict[str, list[str]]) -> None:
            msg, level = self._msg(query)
            contract = self._contract(contract_id)
            planned_rows = service.planned_rows(contract_id=contract_id, limit=500)
            deliveries = repo.fetch_all(
                """
                SELECT * FROM deliveries
                WHERE contract_id = ?
                ORDER BY delivery_date DESC, created_at DESC
                """,
                (contract_id,),
            )
            today = dt.date.today().isoformat()
            parts = [
                f"<h2>Execute - {_escape(contract.get('lpo_no') or contract_id)}</h2>",
                "<p class='muted'>Materialize due lots, progress states, capture evidence, and generate 4-doc pack.</p>",
                self._contract_summary(contract),
                f"<form method='POST' action='/v2/contracts/{_escape(contract_id)}/execute/materialize-due' enctype='multipart/form-data'>"
                "<div class='inline-grid'>"
                f"<label>As-of date <input type='date' name='as_of_date' value='{today}' /></label>"
                "<label>Run ID override <input type='text' name='run_id' value='' /></label>"
                "<label>Batch ID override <input type='text' name='batch_id' value='' /></label>"
                "<label>Evidence upload <input type='file' name='original_docs' multiple /></label>"
                "<label><input type='checkbox' name='auto_progress' value='1' checked /> Auto mark dispatched+delivered</label>"
                "<label><input type='checkbox' name='auto_record_coa' value='1' checked /> Auto record COA rows</label>"
                "<label><input type='checkbox' name='auto_generate_pack' value='1' checked /> Auto generate 4-pack</label>"
                "<label><input type='checkbox' name='allow_placeholder_tin' value='1' checked /> Allow placeholder TIN (dev)</label>"
                "<label><input type='checkbox' name='force_no_evidence' value='1' /> Override missing evidence gate</label>"
                "<button type='submit'>Materialize All Due Eligible Lots</button>"
                "</div></form>",
                "<h3>Planned Runboard</h3>",
                "<table><thead><tr><th>Seq</th><th>Date</th><th>Qty (MT)</th><th>Status</th><th>Delivery</th><th>Action</th></tr></thead><tbody>",
            ]
            for row in planned_rows:
                planned_id = str(row["planned_delivery_id"])
                status = str(row.get("status") or "")
                action = "-"
                if status.upper() in {"PLANNED", "SCHEDULED"}:
                    action = (
                        f"<form method='POST' action='/v2/contracts/{_escape(contract_id)}/execute/materialize-one' class='inline-form'>"
                        f"<input type='hidden' name='planned_delivery_id' value='{_escape(planned_id)}' />"
                        f"<input type='number' step='0.001' name='qty_mt' placeholder='qty mt override' />"
                        "<button type='submit'>Materialize</button>"
                        "</form>"
                    )
                parts.append(
                    "<tr>"
                    f"<td>{_escape(row.get('sequence_no'))}</td>"
                    f"<td>{_escape(row.get('planned_date'))}</td>"
                    f"<td>{_escape(_fmt_qty_mt_from_kg(row.get('planned_qty_kg')))}</td>"
                    f"<td><span class='pill'>{_escape(status)}</span></td>"
                    f"<td>{_escape(row.get('delivery_id') or '-')}</td>"
                    f"<td>{action}</td>"
                    "</tr>"
                )
            if not planned_rows:
                parts.append("<tr><td colspan='6' class='muted'>No planned rows available.</td></tr>")
            parts.append("</tbody></table>")

            parts.append("<h3>Materialized Deliveries</h3>")
            parts.append("<table><thead><tr><th>Delivery</th><th>Date</th><th>Status</th><th>Qty (MT)</th><th>Run</th><th>Batch</th><th>Pack</th></tr></thead><tbody>")
            for row in deliveries:
                can_pack = str(row.get("status") or "").upper() == "DELIVERED"
                pack_action = "-"
                if can_pack:
                    pack_action = (
                        f"<form method='POST' action='/v2/contracts/{_escape(contract_id)}/execute/generate-pack' class='inline-form'>"
                        f"<input type='hidden' name='delivery_id' value='{_escape(str(row['delivery_id']))}' />"
                        "<button type='submit'>Generate Pack</button>"
                        "</form>"
                    )
                parts.append(
                    "<tr>"
                    f"<td>{_escape(row.get('delivery_id'))}</td>"
                    f"<td>{_escape(row.get('delivery_date'))}</td>"
                    f"<td><span class='pill'>{_escape(row.get('status'))}</span></td>"
                    f"<td>{_escape(_fmt_qty_mt_from_kg(row.get('delivered_qty_kg')))}</td>"
                    f"<td>{_escape(row.get('run_id'))}</td>"
                    f"<td>{_escape(row.get('batch_id'))}</td>"
                    f"<td>{pack_action}</td>"
                    "</tr>"
                )
            if not deliveries:
                parts.append("<tr><td colspan='7' class='muted'>No deliveries materialized yet.</td></tr>")
            parts.append("</tbody></table>")
            parts.append(f"<div class='actions'><a class='btn' href='/v2/contracts/{_escape(contract_id)}/settle'>Proceed to Settle</a></div>")
            self._render_page("Execute", "".join(parts), active="execute", msg=msg, level=level)

        def _render_settle(self, contract_id: str, query: dict[str, list[str]]) -> None:
            msg, level = self._msg(query)
            contract = self._contract(contract_id)
            sales_rows = repo.fetch_all(
                """
                SELECT * FROM drep_sales
                WHERE contract_id = ?
                ORDER BY invoice_date DESC, invoice_no DESC
                """,
                (contract_id,),
            )
            today = dt.date.today().isoformat()
            sales_options = [(str(row["sales_transaction_id"]), f"{row['invoice_no']} | outstanding={row['outstanding_balance']}") for row in sales_rows]
            default_sale = sales_options[0][0] if sales_options else ""
            default_amount = float(sales_rows[0]["outstanding_balance"]) if sales_rows else 0.0
            parts = [
                f"<h2>Settle - {_escape(contract.get('lpo_no') or contract_id)}</h2>",
                "<p class='muted'>Receipt is generated only here on mark-paid.</p>",
                self._contract_summary(contract),
                "<h3>Invoices / Outstanding</h3>",
                "<table><thead><tr><th>Invoice</th><th>Amount Due</th><th>Paid</th><th>Certified Withheld</th><th>Outstanding</th><th>Due Date</th></tr></thead><tbody>",
            ]
            for row in sales_rows:
                parts.append(
                    "<tr>"
                    f"<td>{_escape(row.get('invoice_no'))}</td>"
                    f"<td>{_escape(row.get('amount_due'))}</td>"
                    f"<td>{_escape(row.get('amount_paid_to_date'))}</td>"
                    f"<td>{_escape(row.get('certified_withheld_amount'))}</td>"
                    f"<td>{_escape(row.get('outstanding_balance'))}</td>"
                    f"<td>{_escape(row.get('due_date'))}</td>"
                    "</tr>"
                )
            if not sales_rows:
                parts.append("<tr><td colspan='6' class='muted'>No invoices generated yet.</td></tr>")
            parts.append("</tbody></table>")
            parts.append(
                f"<form method='POST' action='/v2/contracts/{_escape(contract_id)}/settle/mark-paid'><div class='inline-grid'>"
                f"<label>Sales transaction {self._select('sales_transaction_id', sales_options, selected=default_sale)}</label>"
                f"<label>Payment Date <input type='date' name='payment_date' value='{today}' /></label>"
                f"<label>Amount Received <input type='number' step='0.01' name='amount_received' value='{default_amount:.2f}' /></label>"
                "<label>Payment Method <input type='text' name='payment_method' value='Bank Transfer' /></label>"
                "<label>External Reference <input type='text' name='external_reference' value='' /></label>"
                "<label><input type='checkbox' name='skip_pdf' value='1' /> Skip PDF (metadata-only)</label>"
                "<button type='submit'>Mark Paid + Generate Receipt</button>"
                "</div></form>"
            )
            parts.append(
                f"<form method='POST' action='/v2/contracts/{_escape(contract_id)}/settle/export-drep'><div class='inline-grid'>"
                f"<label>As-of Date <input type='date' name='as_of_date' value='{today}' /></label>"
                "<button type='submit'>Export DREP</button>"
                "</div></form>"
            )
            self._render_page("Settle", "".join(parts), active="settle", msg=msg, level=level)

        def _render_exceptions(self, query: dict[str, list[str]]) -> None:
            msg, level = self._msg(query)
            rows = repo.list_exceptions(status="OPEN")
            parts = [
                "<h2>Exceptions Queue</h2>",
                "<p class='muted'>Resolve blocker/review items and resume automation runs if needed.</p>",
                "<table><thead><tr><th>Created</th><th>Run</th><th>Stage</th><th>Type</th><th>Severity</th><th>Reason</th><th>Resolve</th></tr></thead><tbody>",
            ]
            for row in rows:
                parts.append(
                    "<tr>"
                    f"<td>{_escape(row.get('created_at'))}</td>"
                    f"<td>{_escape(row.get('run_id'))}</td>"
                    f"<td>{_escape(row.get('stage'))}</td>"
                    f"<td>{_escape(row.get('exception_type'))}</td>"
                    f"<td><span class='pill'>{_escape(row.get('severity'))}</span></td>"
                    f"<td>{_escape(row.get('reason'))}</td>"
                    "<td>"
                    f"<form method='POST' action='/v2/exceptions/resolve' class='inline-form'>"
                    f"<input type='hidden' name='exception_id' value='{_escape(str(row['exception_id']))}' />"
                    "<input type='text' name='value' placeholder='resolved value' required />"
                    "<input type='text' name='note' placeholder='note' required />"
                    "<button type='submit'>Resolve</button>"
                    "</form>"
                    "</td>"
                    "</tr>"
                )
            if not rows:
                parts.append("<tr><td colspan='7' class='muted'>No open exceptions.</td></tr>")
            parts.append("</tbody></table>")
            self._render_page("Exceptions", "".join(parts), active="exceptions", msg=msg, level=level)

        def _handle_intake(self) -> None:
            form = self._multipart()
            data = self._form_values(form)
            issue_date = data.get("issue_date") or dt.date.today().isoformat()
            expected_qty_mt = float(data.get("expected_qty_mt") or 0.0)
            if expected_qty_mt <= 0:
                raise ValueError("expected_qty_mt must be > 0")
            expected_qty_kg = mt_to_kg_int(expected_qty_mt)
            unit_price = float(data.get("unit_price") or 0.0)
            expected_total_value = round(expected_qty_kg * unit_price, 2)
            lpo_no = str(data.get("lpo_no") or "").strip()
            if not lpo_no:
                raise ValueError("lpo_no is required")

            payload = {
                "contract_ref": lpo_no,
                "lpo_no": lpo_no,
                "lpo_date": data.get("lpo_date") or None,
                "buyer_id": data.get("buyer_id"),
                "vendor_of_record_id": data.get("vendor_of_record_id"),
                "operator_id": config.system_profile.operator_entity_id or "guildgate",
                "source_id": data.get("source_id") or None,
                "processor_id": data.get("processor_id") or None,
                "currency": data.get("currency") or "NGN",
                "issue_date": issue_date,
                "lpo_valid_from": data.get("lpo_valid_from") or issue_date,
                "lpo_valid_to": data.get("lpo_valid_to") or None,
                "due_date": data.get("lpo_valid_to") or None,
                "due_terms": "14 days",
                "expected_total_qty": expected_qty_mt,
                "expected_total_qty_kg": expected_qty_kg,
                "expected_total_value": expected_total_value,
                "over_delivery_tolerance_pct": float(data.get("tolerance_pct") or 5.0),
                "unit_price_basis": "KG",
                "lines": [
                    {
                        "product_code": str(data.get("product_code") or "").upper(),
                        "description": data.get("description") or f"Supply linked to {lpo_no}",
                        "expected_qty": expected_qty_kg,
                        "unit": "kgs",
                        "unit_price": unit_price,
                        "unit_price_basis": "KG",
                    }
                ],
            }

            contract = service.create_contract(payload, allow_placeholder_tin=bool(data.get("allow_placeholder_tin")))
            evidence_count = self._capture_uploaded_files(
                form=form,
                field_name="lpo_originals",
                contract_id=str(contract["contract_id"]),
            )
            plan_result = service.plan_deliveries(
                contract_id=str(contract["contract_id"]),
                start_date=data.get("start_date") or issue_date,
                cadence=data.get("cadence") or "daily",
                max_lots_per_day=int(data.get("max_lots_per_day") or 1),
            )
            message = (
                f"Created contract {contract['contract_id']} and planned {plan_result['planned_count']} deliveries"
                + (f" ({evidence_count} evidence files captured)" if evidence_count else "")
            )
            self._flash_redirect(f"/v2/contracts/{contract['contract_id']}/plan", message)

        def _handle_plan_rebuild(self, contract_id: str) -> None:
            fields = self._urlencoded_fields()
            start_date = str(fields.get("start_date") or dt.date.today().isoformat()).strip()
            cadence = str(fields.get("cadence") or "daily").strip()
            max_lots = int(fields.get("max_lots_per_day") or 1)
            with repo.transaction() as conn:
                conn.execute(
                    """
                    DELETE FROM planned_deliveries
                    WHERE contract_id = ?
                      AND status IN ('PLANNED', 'SCHEDULED')
                      AND delivery_id IS NULL
                    """,
                    (contract_id,),
                )
            result = service.plan_deliveries(
                contract_id=contract_id,
                start_date=start_date,
                cadence=cadence,
                max_lots_per_day=max_lots,
            )
            self._flash_redirect(
                f"/v2/contracts/{contract_id}/plan",
                f"Plan rebuilt with {result['planned_count']} planned deliveries",
            )

        def _handle_plan_update(self, contract_id: str) -> None:
            fields = self._urlencoded_fields()
            planned_delivery_id = str(fields.get("planned_delivery_id") or "").strip()
            if not planned_delivery_id:
                raise ValueError("planned_delivery_id is required")
            planned_date = str(fields.get("planned_date") or "").strip() or None
            qty_raw = str(fields.get("planned_qty_mt") or "").strip()
            qty_mt = float(qty_raw) if qty_raw else None
            result = service.update_planned_delivery(
                planned_delivery_id=planned_delivery_id,
                planned_date=planned_date,
                planned_qty_mt=qty_mt,
                notes="web_v2_edit",
            )
            self._flash_redirect(
                f"/v2/contracts/{contract_id}/plan",
                f"Updated planned delivery {result['planned_delivery_id']}",
            )

        def _handle_execute_materialize_due(self, contract_id: str) -> None:
            form = self._multipart()
            data = self._form_values(form)
            uploaded = self._collect_temp_uploads(form=form, field_name="original_docs")
            try:
                existing_evidence = repo.fetch_one(
                    "SELECT COUNT(*) AS total FROM evidence_originals WHERE contract_id = ?",
                    (contract_id,),
                )
                has_existing = int(existing_evidence["total"] if existing_evidence else 0) > 0
                if not uploaded and not has_existing and not bool(data.get("force_no_evidence")):
                    self._record_ui_exception(
                        stage="execute.materialize_due",
                        exception_type="materialization_readiness_missing_evidence",
                        severity="BLOCKER",
                        field_name="original_docs",
                        proposed_value=[],
                        reason="Materialize blocked: missing required evidence (upload evidence or force override)",
                    )
                    self._flash_redirect(
                        f"/v2/contracts/{contract_id}/execute",
                        "Blocked: missing evidence for materialization",
                        level="error",
                    )
                    return

                for path in uploaded:
                    service.capture_evidence_original(contract_id=contract_id, source_path=path)

                result = service.materialize_due_deliveries(
                    contract_id=contract_id,
                    as_of_date=str(data.get("as_of_date") or dt.date.today().isoformat()).strip(),
                    run_id=str(data.get("run_id") or "").strip() or None,
                    batch_id=str(data.get("batch_id") or "").strip() or None,
                    auto_progress=bool(data.get("auto_progress")),
                    auto_record_coa=bool(data.get("auto_record_coa")),
                    auto_generate_pack=bool(data.get("auto_generate_pack")),
                    allow_placeholder_tin=bool(data.get("allow_placeholder_tin")),
                    original_docs=[str(path) for path in uploaded],
                )
                if not bool(result.get("ok")):
                    self._record_ui_exception(
                        stage="execute.materialize_due",
                        exception_type="materialization_blocked",
                        severity="BLOCKER",
                        field_name="planned_delivery_id",
                        proposed_value=result.get("blocked_planned_delivery_id"),
                        reason=str(result.get("error") or "Unknown materialization error"),
                    )
                    self._flash_redirect(
                        f"/v2/contracts/{contract_id}/execute",
                        f"Materialization blocked on {result.get('blocked_planned_delivery_id')}: {result.get('error')}",
                        level="error",
                    )
                    return
                processed_count = len(result.get("processed") or [])
                self._flash_redirect(
                    f"/v2/contracts/{contract_id}/execute",
                    f"Materialized {processed_count} planned deliveries",
                )
            finally:
                for path in uploaded:
                    try:
                        path.unlink(missing_ok=True)
                    except Exception:
                        pass

        def _handle_execute_materialize_one(self, contract_id: str) -> None:
            fields = self._urlencoded_fields()
            planned_delivery_id = str(fields.get("planned_delivery_id") or "").strip()
            if not planned_delivery_id:
                raise ValueError("planned_delivery_id is required")
            qty_raw = str(fields.get("qty_mt") or "").strip()
            qty_mt = float(qty_raw) if qty_raw else None
            materialized = service.materialize_delivery(
                planned_delivery_id=planned_delivery_id,
                qty_mt=qty_mt,
            )
            delivery_id = str(materialized["delivery_id"])
            service.mark_dispatched(delivery_id)
            service.mark_delivered(delivery_id)
            coa_payload = service.coa_template_for_delivery(delivery_id, default_result="PASS")
            service.record_coa(coa_payload)
            self._flash_redirect(
                f"/v2/contracts/{contract_id}/execute",
                f"Materialized delivery {delivery_id}",
            )

        def _handle_execute_generate_pack(self, contract_id: str) -> None:
            fields = self._urlencoded_fields()
            delivery_id = str(fields.get("delivery_id") or "").strip()
            if not delivery_id:
                raise ValueError("delivery_id is required")
            result = service.generate_pack(
                delivery_id=delivery_id,
                allow_placeholder_tin=True,
                skip_pdf=False,
                original_docs=[],
            )
            self._flash_redirect(
                f"/v2/contracts/{contract_id}/execute",
                f"Generated pack {result['invoice_no']}",
            )

        def _handle_settle_mark_paid(self, contract_id: str) -> None:
            fields = self._urlencoded_fields()
            sales_transaction_id = str(fields.get("sales_transaction_id") or "").strip()
            if not sales_transaction_id:
                raise ValueError("sales_transaction_id is required")
            amount_received = float(fields.get("amount_received") or 0.0)
            if amount_received <= 0:
                raise ValueError("amount_received must be > 0")
            external_reference = str(fields.get("external_reference") or "").strip() or f"WEB-{new_ulid()}"
            payload = {
                "payment_date": str(fields.get("payment_date") or dt.date.today().isoformat()).strip(),
                "payment_method": str(fields.get("payment_method") or "Bank Transfer").strip(),
                "external_reference": external_reference,
                "idempotency_key": external_reference,
                "amount_received": amount_received,
                "allocations": [
                    {
                        "sales_transaction_id": sales_transaction_id,
                        "allocated_amount": amount_received,
                        "notes": "web_v2_settle",
                    }
                ],
            }
            result = service.mark_paid(payload, allow_placeholder_tin=True, skip_pdf=bool(fields.get("skip_pdf")))
            self._flash_redirect(
                f"/v2/contracts/{contract_id}/settle",
                f"Payment recorded with receipt {result['receipt_no']}",
            )

        def _handle_settle_export(self, contract_id: str) -> None:
            fields = self._urlencoded_fields()
            as_of = str(fields.get("as_of_date") or dt.date.today().isoformat()).strip()
            out_dir = config.state_dir / "exports" / "web_v2" / as_of / dt.datetime.utcnow().strftime("%Y%m%d%H%M%S")
            result = service.export_drep(as_of_date=as_of, out_dir=out_dir)
            self._flash_redirect(
                f"/v2/contracts/{contract_id}/settle",
                f"DREP exported ({len(result['exports'])} files) for {as_of}",
            )

        def _handle_exception_resolve(self) -> None:
            fields = self._urlencoded_fields()
            exception_id = str(fields.get("exception_id") or "").strip()
            value = str(fields.get("value") or "").strip()
            note = str(fields.get("note") or "").strip()
            if not exception_id or not value or not note:
                raise ValueError("exception_id, value and note are required")
            repo.resolve_exception(exception_id=exception_id, value=value, note=note)
            self._flash_redirect("/v2/exceptions", "Exception resolved")

        def _handle_materialize_legacy(self) -> None:
            form = self._multipart()
            data = self._form_values(form)
            planned_delivery_id = str(data.get("planned_delivery_id") or "").strip()
            if not planned_delivery_id:
                raise ValueError("planned_delivery_id is required")
            qty_raw = str(data.get("qty_mt") or "").strip()
            qty_mt = float(qty_raw) if qty_raw else None
            materialized = service.materialize_delivery(planned_delivery_id=planned_delivery_id, qty_mt=qty_mt)
            delivery_id = str(materialized["delivery_id"])
            if bool(data.get("auto_progress")):
                service.mark_dispatched(delivery_id)
                service.mark_delivered(delivery_id)
            if bool(data.get("auto_record_coa")):
                coa_payload = service.coa_template_for_delivery(delivery_id, default_result="PASS")
                service.record_coa(coa_payload)
            if bool(data.get("auto_generate_pack")):
                service.generate_pack(
                    delivery_id=delivery_id,
                    allow_placeholder_tin=bool(data.get("allow_placeholder_tin")),
                    skip_pdf=False,
                    original_docs=[],
                )
            self._flash_redirect("/v2/portfolio", f"Legacy materialize completed for {delivery_id}")

        def _handle_mark_paid_legacy(self) -> None:
            fields = self._urlencoded_fields()
            sales_transaction_id = str(fields.get("sales_transaction_id") or "").strip()
            if not sales_transaction_id:
                raise ValueError("sales_transaction_id is required")
            amount_received = float(fields.get("amount_received") or 0.0)
            external_reference = str(fields.get("external_reference") or "").strip() or f"WEB-{new_ulid()}"
            result = service.mark_paid(
                {
                    "payment_date": str(fields.get("payment_date") or dt.date.today().isoformat()).strip(),
                    "payment_method": str(fields.get("payment_method") or "Bank Transfer").strip(),
                    "external_reference": external_reference,
                    "idempotency_key": external_reference,
                    "amount_received": amount_received,
                    "allocations": [{"sales_transaction_id": sales_transaction_id, "allocated_amount": amount_received}],
                },
                allow_placeholder_tin=True,
                skip_pdf=bool(fields.get("skip_pdf")),
            )
            self._flash_redirect("/v2/portfolio", f"Legacy mark-paid completed ({result['receipt_no']})")

        def _handle_export_legacy(self) -> None:
            fields = self._urlencoded_fields()
            as_of = str(fields.get("as_of_date") or dt.date.today().isoformat()).strip()
            out_dir = config.state_dir / "exports" / "web_v2" / as_of / dt.datetime.utcnow().strftime("%Y%m%d%H%M%S")
            service.export_drep(as_of_date=as_of, out_dir=out_dir)
            self._flash_redirect("/v2/portfolio", f"Legacy export completed for {as_of}")

        def _record_ui_exception(
            self,
            *,
            stage: str,
            exception_type: str,
            severity: str,
            field_name: str,
            proposed_value: object,
            reason: str,
        ) -> None:
            run_id = new_ulid()
            now = utc_now_iso_z()
            with repo.transaction() as conn:
                repo.create_automation_run(
                    conn,
                    run_id=run_id,
                    idempotency_key=f"ui-{run_id}",
                    as_of_date=dt.date.today().isoformat(),
                    dry_run=False,
                    input_payload={"source": "web_v2", "stage": stage},
                )
                repo.add_exception(
                    conn,
                    run_id=run_id,
                    stage=stage,
                    exception_type=exception_type,
                    severity=severity,
                    field_name=field_name,
                    proposed_value=proposed_value,
                    reason=reason,
                    suggestions=[],
                )
                repo.complete_automation_run(
                    conn,
                    run_id=run_id,
                    status="NEEDS_REVIEW",
                    normalized_payload={"source": "web_v2"},
                    metrics={"exception_count": 1},
                    started_at=now,
                    failure_reason=reason,
                )

        def _capture_uploaded_files(self, *, form: cgi.FieldStorage, field_name: str, contract_id: str) -> int:
            uploaded = self._collect_temp_uploads(form=form, field_name=field_name)
            count = 0
            try:
                for file_path in uploaded:
                    service.capture_evidence_original(contract_id=contract_id, source_path=file_path)
                    count += 1
            finally:
                for file_path in uploaded:
                    try:
                        file_path.unlink(missing_ok=True)
                    except Exception:
                        pass
            return count

        def _collect_temp_uploads(self, *, form: cgi.FieldStorage, field_name: str) -> list[Path]:
            if field_name not in form:
                return []
            files = form[field_name]
            items = files if isinstance(files, list) else [files]
            paths: list[Path] = []
            temp_dir: Path | None = None
            for item in items:
                filename = str(getattr(item, "filename", "") or "").strip()
                if not filename:
                    continue
                if temp_dir is None:
                    temp_dir = Path(mkdtemp(prefix="ananta_web_v2_upload_", dir="/tmp"))
                destination = temp_dir / _safe_filename(filename)
                with destination.open("wb") as handle:
                    shutil.copyfileobj(item.file, handle)
                paths.append(destination)
            return paths

        def _contract(self, contract_id: str) -> dict[str, object]:
            row = repo.fetch_one(
                """
                SELECT
                  c.*,
                  dc.expected_total_qty_kg,
                  dc.delivered_qty_total_kg
                FROM contracts c
                LEFT JOIN drep_contracts dc ON dc.contract_id = c.contract_id
                WHERE c.contract_id = ?
                """,
                (contract_id,),
            )
            if not row:
                raise ValueError(f"Unknown contract_id: {contract_id}")
            return row

        def _contract_summary(self, contract: dict[str, object]) -> str:
            return (
                "<div class='summary'>"
                f"<div><strong>Contract:</strong> {_escape(contract.get('contract_id'))}</div>"
                f"<div><strong>LPO:</strong> {_escape(contract.get('lpo_no'))}</div>"
                f"<div><strong>Buyer:</strong> {_escape(contract.get('buyer_id'))}</div>"
                f"<div><strong>Vendor:</strong> {_escape(contract.get('vendor_of_record_id'))}</div>"
                f"<div><strong>LPO State:</strong> <span class='pill'>{_escape(contract.get('lpo_state'))}</span></div>"
                f"<div><strong>Expected (MT):</strong> {_escape(_fmt_qty_mt_from_kg(contract.get('expected_total_qty_kg')))}</div>"
                f"<div><strong>Delivered (MT):</strong> {_escape(_fmt_qty_mt_from_kg(contract.get('delivered_qty_total_kg')))}</div>"
                "</div>"
            )

        def _entity_options(self, *, prefix: str, exclude_prefixes: tuple[str, ...] = ()) -> list[tuple[str, str]]:
            options: list[tuple[str, str]] = []
            for entity_id, entity in config.registry.entities.items():
                if prefix and not entity_id.startswith(prefix):
                    continue
                if exclude_prefixes and entity_id.startswith(exclude_prefixes):
                    continue
                options.append((entity_id, entity.name))
            options.sort(key=lambda item: item[1].lower())
            return options

        def _select(self, name: str, options: list[tuple[str, str]], *, selected: str = "") -> str:
            tags = [f"<select name='{_escape(name)}'>"]
            for value, label in options:
                selected_attr = " selected" if value == selected else ""
                tags.append(f"<option value='{_escape(value)}'{selected_attr}>{_escape(label)}</option>")
            tags.append("</select>")
            return "".join(tags)

        def _msg(self, query: dict[str, list[str]]) -> tuple[str, str]:
            msg = query.get("msg", [""])[0] if query else ""
            level = query.get("level", ["ok"])[0] if query else "ok"
            return str(msg), str(level or "ok")

        def _redirect(self, location: str) -> None:
            self.send_response(302)
            self.send_header("Location", location)
            self.end_headers()

        def _flash_redirect(self, path: str, message: str, *, level: str = "ok") -> None:
            self._redirect(f"{path}?msg={quote_plus(message)}&level={quote_plus(level)}")

        def _render_page(self, title: str, body_html: str, *, active: str, msg: str = "", level: str = "ok") -> None:
            alert = ""
            if msg:
                css_class = "err" if level == "error" else "ok"
                alert = f"<div class='alert {css_class}'>{_escape(msg)}</div>"
            nav = (
                "<nav>"
                f"{self._nav_link('/v2/portfolio', 'Portfolio', active == 'portfolio')}"
                f"{self._nav_link('/v2/intake', 'Intake', active == 'intake')}"
                f"{self._nav_link('/v2/exceptions', 'Exceptions', active == 'exceptions')}"
                "<form method='POST' action='/v2/init-db' style='display:inline-block;margin-left:auto'>"
                "<button type='submit'>Init DB</button>"
                "</form>"
                "</nav>"
            )
            page = (
                "<!doctype html><html><head><meta charset='utf-8' />"
                "<title>Ananta Delivery Pilot - Phase 2 (Ledger UI)</title>"
                "<style>"
                "body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;margin:20px;color:#111827}"
                "h2,h3{margin:0 0 10px 0} .muted{color:#6b7280;font-size:12px}"
                "nav{display:flex;gap:10px;align-items:center;margin-bottom:14px}"
                "nav a{padding:6px 10px;border:1px solid #d1d5db;border-radius:8px;text-decoration:none;color:#111827}"
                "nav a.active{background:#eef2ff;border-color:#a5b4fc}"
                ".alert{padding:10px 12px;border-radius:8px;margin-bottom:12px;font-size:13px}"
                ".alert.ok{background:#ecfdf5;color:#065f46;border:1px solid #a7f3d0}"
                ".alert.err{background:#fef2f2;color:#991b1b;border:1px solid #fecaca}"
                ".grid{display:grid;grid-template-columns:220px 1fr 220px 1fr;gap:8px 12px;align-items:center}"
                ".inline-grid{display:grid;grid-template-columns:repeat(4,minmax(220px,1fr));gap:8px 10px;align-items:end;margin:10px 0}"
                ".inline-form{display:flex;gap:6px;align-items:center}"
                "input[type=text],input[type=date],input[type=number],select{width:100%;padding:6px;box-sizing:border-box}"
                "button,.btn{padding:7px 12px;border:1px solid #d1d5db;background:#fff;border-radius:8px;text-decoration:none;color:#111827;cursor:pointer}"
                "table{width:100%;border-collapse:collapse;font-size:12px;margin-top:8px}"
                "th,td{border:1px solid #e5e7eb;padding:6px;text-align:left;vertical-align:top}"
                "th{background:#f9fafb} .pill{display:inline-block;padding:2px 8px;border:1px solid #d1d5db;border-radius:999px;font-size:11px}"
                ".summary{display:grid;grid-template-columns:repeat(4,minmax(180px,1fr));gap:8px;margin:10px 0;padding:10px;border:1px solid #e5e7eb;border-radius:8px;background:#fafafa}"
                ".actions{margin-top:10px}"
                "</style></head><body>"
                "<h1 style='margin:0 0 8px 0;font-size:22px'>Ananta Delivery Pilot - Phase 2 (Ledger UI)</h1>"
                f"{nav}{alert}<main><h2>{_escape(title)}</h2>{body_html}</main></body></html>"
            )
            self._send_html(page)

        def _render_error(self, error: Exception) -> None:
            body = (
                "<h2>Error</h2>"
                f"<pre style='white-space:pre-wrap;background:#f9fafb;border:1px solid #e5e7eb;padding:10px;border-radius:8px'>{_escape(str(error))}</pre>"
                "<p><a href='/v2/portfolio'>Back to portfolio</a></p>"
            )
            self._send_html(body, status=500)

        def _nav_link(self, href: str, label: str, active: bool) -> str:
            active_class = "active" if active else ""
            return f"<a href='{_escape(href)}' class='{active_class}'>{_escape(label)}</a>"

        def _send_html(self, page: str, *, status: int = 200) -> None:
            encoded = page.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _urlencoded_fields(self) -> dict[str, str]:
            content_length = int(self.headers.get("Content-Length", "0") or "0")
            body = self.rfile.read(content_length).decode("utf-8")
            parsed = parse_qs(body)
            return {key: values[0] for key, values in parsed.items()}

        def _multipart(self) -> cgi.FieldStorage:
            return cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ={
                    "REQUEST_METHOD": "POST",
                    "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                },
            )

        def _form_values(self, form: cgi.FieldStorage) -> dict[str, str]:
            values: dict[str, str] = {}
            if not getattr(form, "list", None):
                return values
            for item in form.list or []:
                if not getattr(item, "name", ""):
                    continue
                if item.filename:
                    continue
                values[item.name] = str(item.value or "").strip()
            return values

    server = HTTPServer((host, port), Handler)
    print(f"Phase2 UI serving on http://{host}:{port}")
    server.serve_forever()
