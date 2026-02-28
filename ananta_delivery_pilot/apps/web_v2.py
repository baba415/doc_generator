from __future__ import annotations

"""Phase 2 workflow-first UI routes.

Primary command-center flow:
  /v2/portfolio -> run recommended -> exception-only intervention
Advanced pages:
  /v2/intake, /v2/contracts/{id}/plan, /v2/contracts/{id}/execute, /v2/contracts/{id}/settle
"""

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

from adapters.lpo_parser import PARSER_VERSION, parse_lpo
from adapters.sqlite_repo import SQLiteRepo
from core.config import RuntimeConfig
from core.hashing import canonical_json_sha256
from core.ids import new_ulid
from core.time import utc_now_iso_z, utc_today_iso
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


_INTAKE_FIELD_CLASS_MAP: dict[str, str] = {
    "buyer_id": "identity",
    "vendor_of_record_id": "identity",
    "source_id": "identity",
    "processor_id": "identity",
    "product_code": "identity",
    "lpo_no": "identity",
    "expected_qty_kg": "quantity",
    "expected_qty_mt": "quantity",
    "unit_price": "pricing",
    "unit_price_basis": "pricing",
    "lpo_date": "date",
    "issue_date": "date",
    "lpo_valid_from": "date",
    "lpo_valid_to": "date",
}

_INTAKE_DEFAULT_CONFIDENCE_MATRIX: dict[str, dict[str, float]] = {
    "identity": {"auto_apply_min": 0.93, "review_min": 0.75},
    "quantity": {"auto_apply_min": 0.95, "review_min": 0.75},
    "pricing": {"auto_apply_min": 0.95, "review_min": 0.75},
    "date": {"auto_apply_min": 0.93, "review_min": 0.75},
    "document_linkage": {"auto_apply_min": 0.93, "review_min": 0.75},
}


def intake_field_class(field_name: str) -> str:
    key = str(field_name or "").strip()
    return _INTAKE_FIELD_CLASS_MAP.get(key, "identity")


def resolve_intake_confidence_matrix(automation_thresholds: dict[str, object] | None) -> dict[str, dict[str, float]]:
    matrix = {name: dict(values) for name, values in _INTAKE_DEFAULT_CONFIDENCE_MATRIX.items()}
    root = automation_thresholds if isinstance(automation_thresholds, dict) else {}
    intake_cfg = root.get("intake") if isinstance(root.get("intake"), dict) else {}
    configured = intake_cfg.get("confidence_matrix") if isinstance(intake_cfg.get("confidence_matrix"), dict) else {}
    for field_class, thresholds in configured.items():
        if not isinstance(thresholds, dict):
            continue
        current = matrix.setdefault(str(field_class), {"auto_apply_min": 0.93, "review_min": 0.75})
        auto_min = thresholds.get("auto_apply_min")
        review_min = thresholds.get("review_min")
        if auto_min not in (None, ""):
            current["auto_apply_min"] = float(auto_min)
        if review_min not in (None, ""):
            current["review_min"] = float(review_min)
    return matrix


def intake_decision_for_confidence(
    *,
    field_name: str,
    confidence: float,
    matrix: dict[str, dict[str, float]],
) -> tuple[str, str, str, float, float]:
    field_class = intake_field_class(field_name)
    thresholds = matrix.get(field_class) or matrix.get("identity") or _INTAKE_DEFAULT_CONFIDENCE_MATRIX["identity"]
    auto_apply_min = float(thresholds.get("auto_apply_min", 0.93))
    review_min = float(thresholds.get("review_min", 0.75))
    if confidence >= auto_apply_min:
        return ("auto_applied", "auto_threshold_met", field_class, auto_apply_min, review_min)
    if confidence >= review_min:
        return ("needs_review", "review_threshold", field_class, auto_apply_min, review_min)
    return ("blocked", "below_review_threshold", field_class, auto_apply_min, review_min)


def run_server_v2(root_dir: Path, host: str = "127.0.0.1", port: int = 8865) -> None:
    config = RuntimeConfig.load(root_dir)
    repo = SQLiteRepo(config.state_dir / "drep.sqlite")
    service = Phase1Service(config, repo)
    service.init_db()

    route_plan = re.compile(r"^/v2/contracts/([^/]+)/plan$")
    route_execute = re.compile(r"^/v2/contracts/([^/]+)/execute$")
    route_settle = re.compile(r"^/v2/contracts/([^/]+)/settle$")
    route_intake_parse = re.compile(r"^/v2/intake/parse$")
    route_intake_confirm = re.compile(r"^/v2/intake/confirm$")
    route_plan_rebuild = re.compile(r"^/v2/contracts/([^/]+)/plan/rebuild$")
    route_plan_update = re.compile(r"^/v2/contracts/([^/]+)/plan/update$")
    route_plan_approve = re.compile(r"^/v2/contracts/([^/]+)/plan/approve$")
    route_execute_due = re.compile(r"^/v2/contracts/([^/]+)/execute/materialize-due$")
    route_execute_one = re.compile(r"^/v2/contracts/([^/]+)/execute/materialize-one$")
    route_generate_pack = re.compile(r"^/v2/contracts/([^/]+)/execute/generate-pack$")
    route_settle_paid = re.compile(r"^/v2/contracts/([^/]+)/settle/mark-paid$")
    route_settle_export = re.compile(r"^/v2/contracts/([^/]+)/settle/export-drep$")
    route_run_recommended = re.compile(r"^/v2/contracts/([^/]+)/run-recommended$")
    route_run_all_preview = re.compile(r"^/v2/run-all-eligible$")
    route_run_all_execute = re.compile(r"^/v2/run-all-eligible/execute$")

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
                if route == "/v2/workbench":
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
                if route == "/v2/intake":
                    self._handle_intake_parse()
                    return
                if route == "/v2/intake-plan":
                    self._handle_intake()
                    return
                if route_intake_parse.match(route):
                    self._handle_intake_parse()
                    return
                if route_intake_confirm.match(route):
                    self._handle_intake_confirm()
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
                match = route_plan_approve.match(route)
                if match:
                    self._handle_plan_approve(match.group(1))
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
                match = route_run_recommended.match(route)
                if match:
                    self._handle_run_recommended(match.group(1))
                    return
                if route_run_all_preview.match(route):
                    self._handle_run_all_preview()
                    return
                if route_run_all_execute.match(route):
                    self._handle_run_all_execute()
                    return
                if route == "/v2/exceptions/decide":
                    self._handle_exception_decide()
                    return
                self.send_error(404)
            except Exception as error:  # pragma: no cover - runtime path
                self._render_error(error)

        def _render_intake(self, query: dict[str, list[str]]) -> None:
            msg, level = self._msg(query)
            today = utc_today_iso()
            due = (dt.date.fromisoformat(today) + dt.timedelta(days=14)).isoformat()
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
                "<h2>Intake (parser-assisted)</h2>",
                "<p class='muted'>Default path: upload LPO, review critical fields, confirm. Manual entry is only for missing docs.</p>",
                "<form method='POST' action='/v2/intake/parse' enctype='multipart/form-data'>",
                "<div class='grid'>",
                "<label>LPO No / Contract Ref</label><input type='text' name='lpo_no' required />",
                f"<label>Buyer</label>{self._select('buyer_id', buyers, selected='buyer_nycil')}",
                f"<label>Vendor-of-record (Lane A/B)</label>{self._select('vendor_of_record_id', vendors, selected='ananta_flows')}",
                f"<label>Product Code</label>{self._select('product_code', [(item, item) for item in product_codes], selected='RBDPO')}",
                "<label>Expected Qty (MT)</label><input type='number' step='0.001' name='expected_qty_mt' value='150' />",
                "<label>Unit Price (per KG)</label><input type='number' step='0.01' name='unit_price' value='2270' />",
                "<label>Original LPO / evidence files</label><input type='file' name='lpo_originals' multiple />",
                "<label>Options</label><div>"
                + _checkbox("allow_placeholder_tin", "Allow placeholder TIN (dev)", True)
                + "</div>",
                "</div>",
                "<details class='advanced'><summary>Advanced intake fields</summary>",
                "<div class='grid'>",
                f"<label>LPO Date</label><input type='date' name='lpo_date' value='{today}' />",
                f"<label>Issue Date</label><input type='date' name='issue_date' value='{today}' required />",
                f"<label>LPO Valid From</label><input type='date' name='lpo_valid_from' value='{today}' />",
                f"<label>LPO Valid To</label><input type='date' name='lpo_valid_to' value='{due}' />",
                f"<label>Source / Producer</label>{self._select('source_id', sources, selected='ananta_flows')}",
                f"<label>Processor</label>{self._select('processor_id', processors, selected='processor_partner_refinery')}",
                "<label>Description</label><input type='text' name='description' value='Supply linked to contract' />",
                "<label>Currency</label><input type='text' name='currency' value='NGN' />",
                f"<label>Plan Start Date</label><input type='date' name='start_date' value='{today}' />",
                "<label>Cadence</label><select name='cadence'><option value='daily'>daily</option><option value='manual'>manual</option></select>",
                "<label>Max Lots / Day</label><input type='number' min='1' name='max_lots_per_day' value='1' />",
                "<label>Tolerance %</label><input type='number' step='0.01' name='tolerance_pct' value='5.0' />",
                "</div>",
                "</details>",
                "<div class='actions'>"
                "<button type='submit'>Parse LPO + Review</button>"
                "<button type='submit' formaction='/v2/intake/confirm'>Create Manual (No LPO)</button>"
                "</div>",
                "</form>",
            ]
            self._render_page("Intake", "".join(body), active="intake", msg=msg, level=level)

        def _render_portfolio(self, query: dict[str, list[str]]) -> None:
            msg, level = self._msg(query)
            as_of_date_utc = str((query.get("as_of_date") or [utc_today_iso()])[0] or utc_today_iso()).strip()
            max_contracts_per_run = int(str((query.get("max_contracts_per_run") or ["20"])[0] or "20"))
            max_actions_per_run = int(str((query.get("max_actions_per_run") or ["200"])[0] or "200"))
            benchmark_version = str((query.get("benchmark_version") or ["phase2.pr6.v1"])[0] or "phase2.pr6.v1").strip()
            queue_data = service.command_center_sections(as_of_date=as_of_date_utc, limit=300)
            sections = queue_data["sections"]
            rows = queue_data["rows"]
            preview_token = str((query.get("preview_token") or [""])[0] or "").strip()
            preview_result: dict[str, object] | None = None
            preview_stale = False
            if preview_token:
                preview_result = service.run_all_eligible(
                    as_of_date_utc=as_of_date_utc,
                    mode="preview",
                    benchmark_version=benchmark_version,
                    max_contracts_per_run=max_contracts_per_run,
                    max_actions_per_run=max_actions_per_run,
                )
                if str(preview_result.get("preview_token") or "") != preview_token:
                    preview_stale = True

            table = [
                "<h2>Command Center</h2>",
                "<p class='muted'>Primary path: run from here. Daily queueing and action windows use UTC date.</p>",
                "<form method='POST' action='/v2/run-all-eligible' class='inline-grid'>"
                f"<label>As-of (UTC) <input type='date' name='as_of_date' value='{_escape(as_of_date_utc)}' /></label>"
                f"<label>Benchmark Version <input type='text' name='benchmark_version' value='{_escape(benchmark_version)}' /></label>"
                f"<label>Max Contracts/Run <input type='number' min='1' name='max_contracts_per_run' value='{_escape(max_contracts_per_run)}' /></label>"
                f"<label>Max Actions/Run <input type='number' min='1' name='max_actions_per_run' value='{_escape(max_actions_per_run)}' /></label>"
                "<button type='submit'>Run All Eligible (Preview)</button>"
                "</form>",
            ]
            if preview_result:
                preview_rows = preview_result.get("skipped_contracts", [])
                preview_rows = preview_rows if isinstance(preview_rows, list) else []
                table.append(
                    "<details open><summary><strong>Run All Preview</strong></summary>"
                    f"<p class='muted'>Eligible: {_escape(preview_result.get('eligible_count'))}; "
                    f"Skipped: {_escape(len(preview_rows))}; "
                    f"preview_token={_escape(preview_result.get('preview_token'))}</p>"
                )
                if preview_stale:
                    table.append("<p class='muted' style='color:#b45309'>Preview token is stale for current queue snapshot. Generate preview again.</p>")
                else:
                    table.append(
                        "<form method='POST' action='/v2/run-all-eligible/execute' class='inline-grid'>"
                        f"<input type='hidden' name='as_of_date' value='{_escape(as_of_date_utc)}' />"
                        f"<input type='hidden' name='benchmark_version' value='{_escape(benchmark_version)}' />"
                        f"<input type='hidden' name='max_contracts_per_run' value='{_escape(max_contracts_per_run)}' />"
                        f"<input type='hidden' name='max_actions_per_run' value='{_escape(max_actions_per_run)}' />"
                        f"<input type='hidden' name='preview_token' value='{_escape(preview_result.get('preview_token'))}' />"
                        "<button type='submit'>Run All Eligible (Execute)</button>"
                        "</form>"
                    )
                if preview_rows:
                    table.append("<table><thead><tr><th>Skipped Contract</th><th>Reason</th></tr></thead><tbody>")
                    for skip in preview_rows:
                        table.append(
                            "<tr>"
                            f"<td>{_escape(skip.get('contract_id'))}</td>"
                            f"<td>{_escape(skip.get('reason_code'))}</td>"
                            "</tr>"
                        )
                    table.append("</tbody></table>")
                table.append("</details>")

            timeline_run_id = str((query.get("run_id") or [""])[0] or "").strip()
            timeline_contract_id = str((query.get("contract_id") or [""])[0] or "").strip()
            if timeline_run_id and timeline_contract_id:
                timeline_rows = service.command_center_timeline(
                    autonomy_run_id=timeline_run_id,
                    contract_id=timeline_contract_id,
                )
                table.append(
                    f"<h3>Autopilot Console (run { _escape(timeline_run_id) })</h3>"
                    f"<p class='muted'>Contract: {_escape(timeline_contract_id)}</p>"
                    "<table><thead><tr><th>Intent</th><th>Intent Status</th><th>Execution Status</th><th>Policy</th><th>Details</th></tr></thead><tbody>"
                )
                for row in timeline_rows:
                    response = row.get("response") if isinstance(row.get("response"), dict) else {}
                    error = row.get("error") if isinstance(row.get("error"), dict) else {}
                    details = response if response else error
                    table.append(
                        "<tr>"
                        f"<td>{_escape(row.get('intent_type'))}</td>"
                        f"<td>{_escape(row.get('intent_status'))}</td>"
                        f"<td>{_escape(row.get('execution_status'))}</td>"
                        f"<td>{_escape(row.get('policy_version'))}</td>"
                        f"<td><code>{_escape(json.dumps(details, sort_keys=True))}</code></td>"
                        "</tr>"
                    )
                if not timeline_rows:
                    table.append("<tr><td colspan='5' class='muted'>No timeline rows found for this run.</td></tr>")
                table.append("</tbody></table>")

            def _section_table(title: str, section_rows: list[dict[str, object]]) -> str:
                chunks = [
                    f"<h3>{_escape(title)} ({len(section_rows)})</h3>",
                    "<table><thead><tr><th>Contract</th><th>Buyer</th><th>LPO State</th><th>Expected/Delivered (MT)</th><th>Open Lots</th><th>Due Lots</th><th>Delivered Not Invoiced</th><th>Outstanding</th><th>Needs Decision</th><th>Action</th></tr></thead><tbody>",
                ]
                if not section_rows:
                    chunks.append("<tr><td colspan='10' class='muted'>No items.</td></tr>")
                    chunks.append("</tbody></table>")
                    return "".join(chunks)
                for row in section_rows:
                    chunks.append(self._portfolio_row_html(row, as_of_date_utc))
                chunks.append("</tbody></table>")
                return "".join(chunks)

            table.append(_section_table("Needs Decision", sections.get("NEEDS_DECISION", [])))
            table.append(_section_table("Due Actions", sections.get("DUE_ACTION", [])))
            table.append(_section_table("At Risk", sections.get("AT_RISK", [])))

            if not rows:
                table.append("<p class='muted'>No contracts yet.</p>")

            table.append(
                "<details class='advanced'><summary>Advanced</summary>"
                "<ul>"
                "<li><a href='/v2/intake'>Intake (manual/parser review)</a></li>"
                "<li><a href='/v2/exceptions'>Exceptions Inbox</a></li>"
                "</ul>"
                "</details>"
            )
            self._render_page("Portfolio", "".join(table), active="portfolio", msg=msg, level=level)

        def _render_plan(self, contract_id: str, query: dict[str, list[str]]) -> None:
            msg, level = self._msg(query)
            contract = self._contract(contract_id)
            planned_rows = service.planned_rows(contract_id=contract_id, limit=500)
            today = utc_today_iso()
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
                "<div class='actions'>"
                f"<form method='POST' action='/v2/contracts/{_escape(contract_id)}/plan/approve' class='inline-form'>"
                "<button type='submit'>Approve Plan + Continue</button>"
                "</form>"
                f"<a class='btn' href='/v2/contracts/{_escape(contract_id)}/execute'>Proceed to Execute</a>"
                "</div>"
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
            today = utc_today_iso()
            run_id = str((query.get("run_id") or [""])[0] or "").strip()
            run_timeline = service.command_center_timeline(
                autonomy_run_id=run_id,
                contract_id=contract_id,
            ) if run_id else []
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
            ]
            if run_id:
                parts.extend(
                    [
                        "<h3>Recommended Run Timeline</h3>",
                        "<table><thead><tr><th>Intent</th><th>Intent Status</th><th>Execution Status</th><th>Policy</th><th>Details</th></tr></thead><tbody>",
                    ]
                )
                for row in run_timeline:
                    response = row.get("response") if isinstance(row.get("response"), dict) else {}
                    error = row.get("error") if isinstance(row.get("error"), dict) else {}
                    details = response if response else error
                    parts.append(
                        "<tr>"
                        f"<td>{_escape(row.get('intent_type'))}</td>"
                        f"<td>{_escape(row.get('intent_status'))}</td>"
                        f"<td>{_escape(row.get('execution_status'))}</td>"
                        f"<td>{_escape(row.get('policy_version'))}</td>"
                        f"<td><code>{_escape(json.dumps(details, sort_keys=True))}</code></td>"
                        "</tr>"
                    )
                if not run_timeline:
                    parts.append("<tr><td colspan='5' class='muted'>No action-intent timeline found for this run.</td></tr>")
                parts.append("</tbody></table>")
            parts.extend(
                [
                "<h3>Planned Runboard</h3>",
                "<table><thead><tr><th>Seq</th><th>Date</th><th>Qty (MT)</th><th>Status</th><th>Delivery</th><th>Action</th></tr></thead><tbody>",
                ]
            )
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
            today = utc_today_iso()
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
            contract_filter = str((query.get("contract_id") or [""])[0] or "").strip()
            as_of_date_utc = str((query.get("as_of_date") or [utc_today_iso()])[0] or utc_today_iso()).strip()
            run_id = str((query.get("run_id") or [""])[0] or "").strip()
            focus_case_id = str((query.get("case_id") or [""])[0] or "").strip()
            cards = service.exception_case_cards(
                status="OPEN",
                as_of_date_utc=as_of_date_utc,
                contract_id=contract_filter or None,
            )
            grouped: dict[tuple[str, str], list[dict[str, object]]] = {}
            for card in cards:
                key = (str(card.get("severity") or "REVIEW"), str(card.get("reason_code") or "unspecified"))
                grouped.setdefault(key, []).append(card)
            parts = [
                "<h2>Exceptions Queue</h2>",
                "<p class='muted'>Decision inbox (approve/reject/override). This is the primary manual workspace.</p>",
            ]
            if run_id and contract_filter:
                timeline_rows = service.command_center_timeline(
                    autonomy_run_id=run_id,
                    contract_id=contract_filter,
                )
                parts.append(
                    f"<h3>Autopilot Console (run {_escape(run_id)})</h3>"
                    "<table><thead><tr><th>Intent</th><th>Intent Status</th><th>Execution Status</th><th>Details</th></tr></thead><tbody>"
                )
                for row in timeline_rows:
                    response = row.get("response") if isinstance(row.get("response"), dict) else {}
                    error = row.get("error") if isinstance(row.get("error"), dict) else {}
                    details = response if response else error
                    parts.append(
                        "<tr>"
                        f"<td>{_escape(row.get('intent_type'))}</td>"
                        f"<td>{_escape(row.get('intent_status'))}</td>"
                        f"<td>{_escape(row.get('execution_status'))}</td>"
                        f"<td><code>{_escape(json.dumps(details, sort_keys=True))}</code></td>"
                        "</tr>"
                    )
                if not timeline_rows:
                    parts.append("<tr><td colspan='4' class='muted'>No run timeline rows found.</td></tr>")
                parts.append("</tbody></table>")
            if grouped:
                severity_order = {"BLOCKER": 0, "REVIEW": 1}
                for (severity, reason_code), group_rows in sorted(
                    grouped.items(),
                    key=lambda item: (severity_order.get(item[0][0], 99), item[0][0], item[0][1]),
                ):
                    parts.append(
                        f"<h3>{_escape(severity)} · {_escape(reason_code)} ({len(group_rows)})</h3>"
                        "<table><thead><tr><th>Created (UTC)</th><th>Contract</th><th>Case Type</th><th>SLA</th><th>Consequence Preview</th><th>Decision</th></tr></thead><tbody>"
                    )
                    for row in group_rows:
                        details = row.get("details") if isinstance(row.get("details"), dict) else {}
                        preview = row.get("consequence_preview") if isinstance(row.get("consequence_preview"), dict) else {}
                        case_id = str(row.get("exception_case_id") or "")
                        sla_text = (
                            f"{_escape(row.get('sla_state'))} · age={_escape(row.get('age_hours'))}h / "
                            f"target={_escape(row.get('sla_target_hours'))}h"
                        )
                        preview_text = (
                            f"next={_escape(preview.get('expected_next_action'))}<br/>"
                            f"recommended={_escape(preview.get('recommended_decision'))}; "
                            f"open_cases={_escape(preview.get('open_cases_for_contract'))}; "
                            f"lpo_state={_escape(preview.get('contract_lpo_state'))}"
                        )
                        parts.append(
                            "<tr>"
                            f"<td>{_escape(row.get('created_at'))}</td>"
                            f"<td>{_escape(row.get('contract_id') or '-')}</td>"
                            f"<td>{_escape(row.get('case_type'))}</td>"
                            f"<td>{sla_text}</td>"
                            f"<td><code>{preview_text}</code><br/><span class='muted'>reason={_escape(json.dumps(details, sort_keys=True))}</span></td>"
                            "<td>"
                            f"<form method='POST' action='/v2/exceptions/decide' class='inline-form'>"
                            f"<input type='hidden' name='case_id' value='{_escape(case_id)}' />"
                            f"<input type='hidden' name='as_of_date' value='{_escape(as_of_date_utc)}' />"
                            f"<input type='hidden' name='contract_id' value='{_escape(str(row.get('contract_id') or ''))}' />"
                            f"<input type='hidden' name='run_id' value='{_escape(run_id)}' />"
                            "<select name='decision'>"
                            "<option value='APPROVE'>APPROVE</option>"
                            "<option value='REJECT'>REJECT</option>"
                            "<option value='OVERRIDE'>OVERRIDE</option>"
                            "</select>"
                            "<input type='text' name='reason' placeholder='reason' required />"
                            "<label><input type='checkbox' name='resume' value='1' checked /> Resume</label>"
                            "<label><input type='checkbox' name='dry_run_resume' value='1' /> Dry run resume</label>"
                            "<button type='submit'>Submit</button>"
                            "</form>"
                            "</td>"
                            "</tr>"
                        )
                        if focus_case_id and focus_case_id == case_id:
                            activity_rows = service.exception_case_activity(case_id=case_id)
                            parts.append(
                                "<tr><td colspan='6'>"
                                "<details open><summary>Case Activity</summary>"
                                "<table><thead><tr><th>At (UTC)</th><th>Event</th><th>Source</th><th>Payload</th></tr></thead><tbody>"
                            )
                            for event in activity_rows:
                                parts.append(
                                    "<tr>"
                                    f"<td>{_escape(event.get('created_at'))}</td>"
                                    f"<td>{_escape(event.get('event_type'))}</td>"
                                    f"<td>{_escape(event.get('source'))}</td>"
                                    f"<td><code>{_escape(json.dumps(event.get('payload') or {}, sort_keys=True))}</code></td>"
                                    "</tr>"
                                )
                            if not activity_rows:
                                parts.append("<tr><td colspan='4' class='muted'>No case activity events.</td></tr>")
                            parts.append("</tbody></table></details></td></tr>")
                    parts.append("</tbody></table>")
            else:
                parts.append("<p class='muted'>No open exception cases.</p>")

            legacy_rows = repo.list_exceptions(status="OPEN")
            if legacy_rows:
                parts.append("<details class='advanced'><summary>Advanced: Intake parser exception queue</summary>")
                parts.append(
                    "<table><thead><tr><th>Created</th><th>Run</th><th>Stage</th><th>Type</th><th>Severity</th><th>Reason</th><th>Resolve</th></tr></thead><tbody>"
                )
                for row in legacy_rows:
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
                parts.append("</tbody></table></details>")
            self._render_page("Exceptions", "".join(parts), active="exceptions", msg=msg, level=level)

        def _render_intake_review(
            self,
            *,
            prefill: dict[str, object],
            field_rows: list[dict[str, object]],
            run_id: str,
            evidence_paths: list[str],
            reused: bool = False,
        ) -> None:
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
            today = utc_today_iso()
            info = "Existing parse context reused (idempotent)." if reused else "Review auto-prefilled fields and confirm."
            row_index = {str(row.get("field_name") or ""): row for row in field_rows}

            def _badge(name: str) -> str:
                row = row_index.get(name) or {}
                confidence = float(row.get("confidence") or 0.0)
                decision = str(row.get("decision") or "needs_review")
                reason = str(row.get("reason_code") or "")
                return (
                    f"<span class='pill'>{_escape(decision)} | {confidence:.2f}</span>"
                    f"<span class='muted'> {_escape(reason)}</span>"
                )

            def _value(name: str, fallback: object = "") -> str:
                raw = prefill.get(name)
                if raw in (None, ""):
                    raw = fallback
                return str(raw if raw is not None else "")

            qty_kg_raw = _value("expected_qty_kg")
            if not qty_kg_raw:
                qty_mt_raw = _value("expected_qty_mt")
                if qty_mt_raw:
                    try:
                        qty_kg_raw = str(mt_to_kg_int(float(qty_mt_raw)))
                    except Exception:
                        qty_kg_raw = ""
            qty_mt_display = ""
            if qty_kg_raw:
                try:
                    qty_mt_display = kg_to_mt_str(int(float(qty_kg_raw)))
                except Exception:
                    qty_mt_display = _value("expected_qty_mt")

            def _registry_value(name: str) -> str:
                if name in {"buyer_id", "vendor_of_record_id", "source_id", "processor_id"}:
                    candidate = str(prefill.get(name) or "").strip()
                    if not candidate:
                        return "-"
                    entity = config.registry.entities.get(candidate)
                    if not entity:
                        return candidate
                    return f"{candidate} ({entity.name})"
                if name == "unit_price_basis":
                    return "KG"
                if name == "currency":
                    return "NGN"
                return "-"

            body = [
                "<h2>Intake Review</h2>",
                f"<p class='muted'>{_escape(info)}</p>",
                "<form method='POST' action='/v2/intake/confirm'>",
                f"<input type='hidden' name='intake_run_id' value='{_escape(run_id)}' />",
                f"<input type='hidden' name='intake_evidence_paths_json' value='{_escape(json.dumps(evidence_paths))}' />",
                f"<input type='hidden' name='expected_qty_mt' value='{_escape(qty_mt_display)}' />",
                "<h3>Critical Fields</h3>",
                "<div class='grid'>",
                f"<label>LPO No / Contract Ref</label><input type='text' name='lpo_no' value='{_escape(_value('lpo_no'))}' required />",
                f"<label>Confidence</label><div>{_badge('lpo_no')}</div>",
                f"<label>Buyer</label>{self._select('buyer_id', buyers, selected=_value('buyer_id'))}",
                f"<label>Confidence</label><div>{_badge('buyer_id')}</div>",
                f"<label>Vendor-of-record</label>{self._select('vendor_of_record_id', vendors, selected=_value('vendor_of_record_id', 'ananta_flows'))}",
                f"<label>Confidence</label><div>{_badge('vendor_of_record_id')}</div>",
                f"<label>Product Code</label>{self._select('product_code', [(item, item) for item in product_codes], selected=_value('product_code'))}",
                f"<label>Confidence</label><div>{_badge('product_code')}</div>",
                f"<label>Expected Qty (KG)</label><input type='number' min='1' step='1' name='expected_qty_kg' value='{_escape(qty_kg_raw)}' required />",
                f"<label>Confidence</label><div>{_badge('expected_qty_kg')} <span class='muted'>(display: {_escape(qty_mt_display)} MT)</span></div>",
                f"<label>Unit Price</label><input type='number' step='0.01' name='unit_price' value='{_escape(_value('unit_price'))}' required />",
                f"<label>Confidence</label><div>{_badge('unit_price')}</div>",
                "<label>Unit Price Basis</label><select name='unit_price_basis'>"
                f"<option value='KG'{' selected' if _value('unit_price_basis', 'KG').upper() == 'KG' else ''}>KG</option>"
                f"<option value='MT'{' selected' if _value('unit_price_basis').upper() == 'MT' else ''}>MT</option>"
                "</select>",
                f"<label>Confidence</label><div>{_badge('unit_price_basis')}</div>",
                "</div>",
                "<details class='advanced'><summary>Advanced intake fields</summary>",
                "<div class='grid'>",
                f"<label>LPO Date</label><input type='date' name='lpo_date' value='{_escape(_value('lpo_date'))}' />",
                f"<label>Confidence</label><div>{_badge('lpo_date')}</div>",
                f"<label>Issue Date</label><input type='date' name='issue_date' value='{_escape(_value('issue_date', today))}' required />",
                f"<label>Confidence</label><div>{_badge('issue_date')}</div>",
                f"<label>LPO Valid From</label><input type='date' name='lpo_valid_from' value='{_escape(_value('lpo_valid_from', _value('issue_date', today)))}' />",
                f"<label>Confidence</label><div>{_badge('lpo_valid_from')}</div>",
                f"<label>LPO Valid To</label><input type='date' name='lpo_valid_to' value='{_escape(_value('lpo_valid_to'))}' />",
                f"<label>Confidence</label><div>{_badge('lpo_valid_to')}</div>",
                f"<label>Source / Producer</label>{self._select('source_id', sources, selected=_value('source_id', 'ananta_flows'))}",
                f"<label>Confidence</label><div>{_badge('source_id')}</div>",
                f"<label>Processor</label>{self._select('processor_id', processors, selected=_value('processor_id', 'processor_partner_refinery'))}",
                f"<label>Confidence</label><div>{_badge('processor_id')}</div>",
                f"<label>Description</label><input type='text' name='description' value='{_escape(_value('description'))}' />",
                f"<label>Confidence</label><div>{_badge('description')}</div>",
                f"<label>Currency</label><input type='text' name='currency' value='{_escape(_value('currency', 'NGN'))}' />",
                f"<label>Confidence</label><div>{_badge('currency')}</div>",
                f"<label>Plan Start Date</label><input type='date' name='start_date' value='{_escape(_value('start_date', _value('issue_date', today)))}' />",
                "<label>Cadence</label><select name='cadence'><option value='daily'>daily</option><option value='manual'>manual</option></select>",
                "<label>Max Lots / Day</label><input type='number' min='1' name='max_lots_per_day' value='1' />",
                f"<label>Tolerance %</label><input type='number' step='0.01' name='tolerance_pct' value='{_escape(_value('tolerance_pct', 5.0))}' />",
                "<label>Options</label><div>"
                + _checkbox("allow_placeholder_tin", "Allow placeholder TIN (dev)", True)
                + "</div>",
                "</div>",
                "</details>",
                "<div class='actions'>"
                "<button type='submit'>Confirm Intake + Create Contract + Plan</button>"
                "<a class='btn' href='/v2/intake'>Back</a>"
                "</div>",
                "</form>",
                "<h3>Parser Diff + Decision Trace</h3>",
                "<table><thead><tr><th>Field</th><th>Extracted</th><th>Registry</th><th>Final</th><th>Confidence</th><th>Decision</th><th>Reason</th></tr></thead><tbody>",
            ]
            for row in field_rows:
                field_name = str(row.get("field_name") or "")
                body.append(
                    "<tr>"
                    f"<td>{_escape(field_name)}</td>"
                    f"<td>{_escape(row.get('proposed_value'))}</td>"
                    f"<td>{_escape(_registry_value(field_name))}</td>"
                    f"<td>{_escape(_value(field_name))}</td>"
                    f"<td>{_escape(row.get('confidence'))}</td>"
                    f"<td>{_escape(row.get('decision'))}</td>"
                    f"<td>{_escape(row.get('reason_code'))}</td>"
                    "</tr>"
                )
            if not field_rows:
                body.append("<tr><td colspan='7' class='muted'>No parser field rows available.</td></tr>")
            body.append("</tbody></table>")
            self._render_page("Intake Review", "".join(body), active="intake")

        def _handle_intake_parse(self) -> None:
            form = self._multipart()
            data = self._form_values(form)
            uploaded = self._collect_temp_uploads(form=form, field_name="lpo_originals")
            if not uploaded:
                self._flash_redirect("/v2/intake", "Upload an LPO file for parser-assisted intake.", level="error")
                return

            primary = uploaded[0]
            parsed = parse_lpo(primary, config=config, hints=data)
            idempotency_key = f"{parsed.file_sha256}+{parsed.parser_version}"
            existing = repo.get_automation_run_by_idempotency(idempotency_key)
            if existing:
                run_id = str(existing["run_id"])
                normalized = json.loads(existing.get("normalized_input_json") or "{}")
                prefill = normalized.get("prefill") if isinstance(normalized, dict) else None
                field_rows = normalized.get("field_rows") if isinstance(normalized, dict) else None
                evidence_paths = normalized.get("evidence_paths") if isinstance(normalized, dict) else None
                if not isinstance(prefill, dict):
                    prefill = self._intake_prefill_from_fields(fields=[field.as_dict() for field in parsed.fields], fallback=data)
                if not isinstance(field_rows, list):
                    field_rows = [field.as_dict() for field in parsed.fields]
                if not isinstance(evidence_paths, list):
                    evidence_paths = [str(path) for path in self._persist_intake_uploads(uploaded, run_id=run_id)]
                for path in uploaded:
                    try:
                        path.unlink(missing_ok=True)
                    except Exception:
                        pass
                self._render_intake_review(
                    prefill=prefill,
                    field_rows=field_rows,
                    run_id=run_id,
                    evidence_paths=[str(item) for item in evidence_paths],
                    reused=True,
                )
                return

            run_id = new_ulid()
            evidence_paths = [str(path) for path in self._persist_intake_uploads(uploaded, run_id=run_id)]
            for path in uploaded:
                try:
                    path.unlink(missing_ok=True)
                except Exception:
                    pass
            critical_fields = self._intake_critical_fields()
            confidence_matrix = self._intake_confidence_matrix()
            now = utc_now_iso_z()
            field_rows: list[dict[str, object]] = []
            exception_count = 0
            with repo.transaction() as conn:
                repo.create_automation_run(
                    conn,
                    run_id=run_id,
                    idempotency_key=idempotency_key,
                    as_of_date=utc_today_iso(),
                    dry_run=False,
                    input_payload={
                        "source": "web_v2_intake",
                        "stage": "intake_parser",
                        "uploaded_file": str(primary),
                        "file_sha256": parsed.file_sha256,
                        "parser_version": parsed.parser_version,
                    },
                )
                for field in parsed.fields:
                    confidence = float(field.confidence)
                    decision, threshold_reason, field_class, auto_apply_min, review_min = intake_decision_for_confidence(
                        field_name=field.field_name,
                        confidence=confidence,
                        matrix=confidence_matrix,
                    )
                    row = field.as_dict()
                    row["decision"] = decision
                    row["field_class"] = field_class
                    row["auto_apply_min"] = auto_apply_min
                    row["review_min"] = review_min
                    field_rows.append(row)
                    repo.add_automation_decision(
                        conn,
                        run_id=run_id,
                        stage="intake_parser",
                        field_name=field.field_name,
                        required_flag=field.field_name in critical_fields,
                        proposed_value=field.proposed_value,
                        source_type=field.source_type,
                        source_ref=field.source_ref,
                        confidence=confidence,
                        decision=decision,
                        reason_code=f"{field.reason_code}:{threshold_reason}",
                        rule_path=f"intake_parser.{field.field_name}",
                    )
                    if decision == "auto_applied":
                        continue
                    is_missing = self._is_missing_field_value(field.proposed_value)
                    is_critical = field.field_name in critical_fields
                    severity = "BLOCKER" if is_critical and (decision == "blocked" or is_missing) else "REVIEW"
                    repo.add_exception(
                        conn,
                        run_id=run_id,
                        stage="intake_parser",
                        exception_type="low_confidence_field",
                        severity=severity,
                        field_name=field.field_name,
                        proposed_value=field.proposed_value,
                        reason=(
                            f"{field.field_name} class={field_class} confidence={confidence:.2f} "
                            f"(auto>={auto_apply_min:.2f}, review>={review_min:.2f}, parser={field.reason_code})"
                        ),
                        suggestions=field.suggestions,
                    )
                    exception_count += 1
                prefill = self._intake_prefill_from_fields(fields=field_rows, fallback=data)
                repo.complete_automation_run(
                    conn,
                    run_id=run_id,
                    status="NEEDS_REVIEW" if exception_count else "COMPLETED",
                    normalized_payload={
                        "prefill": prefill,
                        "field_rows": field_rows,
                        "evidence_paths": evidence_paths,
                        "parser_result": parsed.to_dict(),
                    },
                    metrics={
                        "decision_count": len(field_rows),
                        "exception_count": exception_count,
                        "stage": "intake_parser",
                    },
                    started_at=now,
                    failure_reason=None,
                )
            self._render_intake_review(
                prefill=prefill,
                field_rows=field_rows,
                run_id=run_id,
                evidence_paths=evidence_paths,
                reused=False,
            )

        def _handle_intake_confirm(self) -> None:
            content_type = str(self.headers.get("Content-Type", "") or "")
            uploaded_paths: list[Path] = []
            if "multipart/form-data" in content_type:
                form = self._multipart()
                data = self._form_values(form)
                uploaded_paths = self._collect_temp_uploads(form=form, field_name="lpo_originals")
            else:
                data = self._urlencoded_fields()
            issue_date = data.get("issue_date") or utc_today_iso()
            expected_qty_kg_raw = str(data.get("expected_qty_kg") or "").strip()
            if expected_qty_kg_raw:
                expected_qty_kg = int(float(expected_qty_kg_raw))
            else:
                expected_qty_mt = float(data.get("expected_qty_mt") or 0.0)
                expected_qty_kg = mt_to_kg_int(expected_qty_mt)
            if expected_qty_kg <= 0:
                raise ValueError("expected_qty_mt must be > 0")
            unit_price = float(data.get("unit_price") or 0.0)
            if unit_price <= 0:
                raise ValueError("unit_price must be > 0")
            unit_price_basis = str(data.get("unit_price_basis") or "KG").strip().upper()
            if unit_price_basis not in {"KG", "MT"}:
                unit_price_basis = "KG"

            critical_values = {
                "lpo_no": str(data.get("lpo_no") or "").strip(),
                "buyer_id": str(data.get("buyer_id") or "").strip(),
                "vendor_of_record_id": str(data.get("vendor_of_record_id") or "").strip(),
                "product_code": str(data.get("product_code") or "").strip(),
                "expected_qty_kg": str(expected_qty_kg),
                "unit_price": str(unit_price),
                "unit_price_basis": str(unit_price_basis),
            }
            missing_critical = [name for name, value in critical_values.items() if not str(value or "").strip()]
            if missing_critical:
                raise ValueError(f"missing critical intake fields: {', '.join(missing_critical)}")

            qty_mt_value = float(expected_qty_kg) / 1000.0
            if unit_price_basis == "MT":
                expected_total_value = round(qty_mt_value * unit_price, 2)
            else:
                expected_total_value = round(expected_qty_kg * unit_price, 2)

            lpo_no = critical_values["lpo_no"]
            if not lpo_no:
                raise ValueError("lpo_no is required")

            payload = {
                "contract_ref": lpo_no,
                "lpo_no": lpo_no,
                "lpo_date": data.get("lpo_date") or None,
                "buyer_id": critical_values["buyer_id"],
                "vendor_of_record_id": critical_values["vendor_of_record_id"],
                "operator_id": config.system_profile.operator_entity_id or "guildgate",
                "source_id": data.get("source_id") or None,
                "processor_id": data.get("processor_id") or None,
                "currency": data.get("currency") or "NGN",
                "issue_date": issue_date,
                "lpo_valid_from": data.get("lpo_valid_from") or issue_date,
                "lpo_valid_to": data.get("lpo_valid_to") or None,
                "due_date": data.get("lpo_valid_to") or None,
                "due_terms": "14 days",
                "expected_total_qty": qty_mt_value,
                "expected_total_qty_kg": expected_qty_kg,
                "expected_total_value": expected_total_value,
                "over_delivery_tolerance_pct": float(data.get("tolerance_pct") or 5.0),
                "unit_price_basis": unit_price_basis,
                "lines": [
                    {
                        "product_code": str(critical_values["product_code"]).upper(),
                        "description": data.get("description") or f"Supply linked to {lpo_no}",
                        "expected_qty": expected_qty_kg,
                        "unit": "kgs",
                        "unit_price": unit_price,
                        "unit_price_basis": unit_price_basis,
                    }
                ],
            }
            contract = service.create_contract(payload, allow_placeholder_tin=bool(data.get("allow_placeholder_tin")))
            contract_id = str(contract["contract_id"])
            persisted_evidence = self._load_intake_evidence_paths(data=data) + [str(path) for path in uploaded_paths]
            evidence_count = 0
            try:
                for raw_path in persisted_evidence:
                    path = Path(str(raw_path)).expanduser()
                    if not path.is_absolute():
                        path = (config.root_dir / path).resolve()
                    if not path.exists():
                        continue
                    service.capture_evidence_original(contract_id=contract_id, source_path=path)
                    evidence_count += 1
            finally:
                for file_path in uploaded_paths:
                    try:
                        file_path.unlink(missing_ok=True)
                    except Exception:
                        pass
            data["expected_qty_kg"] = str(expected_qty_kg)
            self._record_intake_confirm_decisions(
                run_id=str(data.get("intake_run_id") or "").strip(),
                submitted=data,
            )
            start_date = data.get("start_date") or issue_date
            try:
                plan_result = service.plan_deliveries(
                    contract_id=contract_id,
                    start_date=start_date,
                    cadence=data.get("cadence") or "daily",
                    max_lots_per_day=int(data.get("max_lots_per_day") or 1),
                )
            except Exception as error:
                case = self._record_exception_case(
                    contract_id=contract_id,
                    case_type="planning_blocker",
                    severity="BLOCKER",
                    reason_code="planning_window_invalid",
                    details={
                        "as_of_date": utc_today_iso(),
                        "stage": "intake_confirm",
                        "start_date": str(start_date),
                        "cadence": str(data.get("cadence") or "daily"),
                        "max_lots_per_day": int(data.get("max_lots_per_day") or 1),
                        "error": str(error),
                    },
                )
                self._flash_redirect(
                    f"/v2/exceptions?contract_id={quote_plus(contract_id)}&case_id={quote_plus(str(case['exception_case_id']))}",
                    f"Planning blocked after intake confirm: {error}",
                    level="error",
                )
                return
            message = (
                f"Created contract {contract_id} and planned {plan_result['planned_count']} deliveries"
                + (f" ({evidence_count} evidence files captured)" if evidence_count else "")
            )
            self._flash_redirect(f"/v2/contracts/{contract_id}/plan", message)

        def _handle_intake(self) -> None:
            form = self._multipart()
            data = self._form_values(form)
            issue_date = data.get("issue_date") or utc_today_iso()
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
            contract_id = str(contract["contract_id"])
            evidence_count = self._capture_uploaded_files(
                form=form,
                field_name="lpo_originals",
                contract_id=contract_id,
            )
            try:
                plan_result = service.plan_deliveries(
                    contract_id=contract_id,
                    start_date=data.get("start_date") or issue_date,
                    cadence=data.get("cadence") or "daily",
                    max_lots_per_day=int(data.get("max_lots_per_day") or 1),
                )
            except Exception as error:
                case = self._record_exception_case(
                    contract_id=contract_id,
                    case_type="planning_blocker",
                    severity="BLOCKER",
                    reason_code="planning_window_invalid",
                    details={
                        "as_of_date": utc_today_iso(),
                        "stage": "manual_intake",
                        "start_date": str(data.get("start_date") or issue_date),
                        "cadence": str(data.get("cadence") or "daily"),
                        "max_lots_per_day": int(data.get("max_lots_per_day") or 1),
                        "error": str(error),
                    },
                )
                self._flash_redirect(
                    f"/v2/exceptions?contract_id={quote_plus(contract_id)}&case_id={quote_plus(str(case['exception_case_id']))}",
                    f"Planning blocked after manual intake: {error}",
                    level="error",
                )
                return
            message = (
                f"Created contract {contract_id} and planned {plan_result['planned_count']} deliveries"
                + (f" ({evidence_count} evidence files captured)" if evidence_count else "")
            )
            self._flash_redirect(f"/v2/contracts/{contract_id}/plan", message)

        def _handle_plan_rebuild(self, contract_id: str) -> None:
            fields = self._urlencoded_fields()
            start_date = str(fields.get("start_date") or utc_today_iso()).strip()
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
            try:
                result = service.plan_deliveries(
                    contract_id=contract_id,
                    start_date=start_date,
                    cadence=cadence,
                    max_lots_per_day=max_lots,
                )
            except Exception as error:
                case = self._record_exception_case(
                    contract_id=contract_id,
                    case_type="planning_blocker",
                    severity="BLOCKER",
                    reason_code="planning_window_invalid",
                    details={
                        "as_of_date": utc_today_iso(),
                        "start_date": start_date,
                        "cadence": cadence,
                        "max_lots_per_day": max_lots,
                        "error": str(error),
                    },
                )
                self._flash_redirect(
                    f"/v2/exceptions?contract_id={quote_plus(contract_id)}&case_id={quote_plus(str(case['exception_case_id']))}",
                    f"Plan rebuild blocked: {error}",
                    level="error",
                )
                return
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
            try:
                result = service.update_planned_delivery(
                    planned_delivery_id=planned_delivery_id,
                    planned_date=planned_date,
                    planned_qty_mt=qty_mt,
                    notes="web_v2_edit",
                )
            except Exception as error:
                case = self._record_exception_case(
                    contract_id=contract_id,
                    case_type="planning_override_blocked",
                    severity="BLOCKER",
                    reason_code="planned_row_update_invalid",
                    details={
                        "as_of_date": utc_today_iso(),
                        "planned_delivery_id": planned_delivery_id,
                        "planned_date": planned_date,
                        "planned_qty_mt": qty_mt,
                        "error": str(error),
                    },
                )
                self._flash_redirect(
                    f"/v2/exceptions?contract_id={quote_plus(contract_id)}&case_id={quote_plus(str(case['exception_case_id']))}",
                    f"Plan update blocked: {error}",
                    level="error",
                )
                return
            self._flash_redirect(
                f"/v2/contracts/{contract_id}/plan",
                f"Updated planned delivery {result['planned_delivery_id']}",
            )

        def _handle_plan_approve(self, contract_id: str) -> None:
            try:
                result = service.approve_planned_deliveries(contract_id=contract_id)
            except Exception as error:
                case = self._record_exception_case(
                    contract_id=contract_id,
                    case_type="planning_approval_blocked",
                    severity="BLOCKER",
                    reason_code="plan_approval_invalid",
                    details={
                        "as_of_date": utc_today_iso(),
                        "error": str(error),
                    },
                )
                self._flash_redirect(
                    f"/v2/exceptions?contract_id={quote_plus(contract_id)}&case_id={quote_plus(str(case['exception_case_id']))}",
                    f"Plan approval blocked: {error}",
                    level="error",
                )
                return
            self._flash_redirect(
                f"/v2/contracts/{contract_id}/execute",
                f"Plan approved (scheduled rows: {result['scheduled_count']})",
            )

        def _handle_execute_materialize_due(self, contract_id: str) -> None:
            form = self._multipart()
            data = self._form_values(form)
            uploaded = self._collect_temp_uploads(form=form, field_name="original_docs")
            try:
                as_of_date = str(data.get("as_of_date") or utc_today_iso()).strip()
                service.refresh_contract_state(as_of_date=as_of_date)
                contract_row = repo.fetch_one("SELECT lpo_state FROM contracts WHERE contract_id = ?", (contract_id,))
                if not contract_row or str(contract_row.get("lpo_state") or "").upper() != "ACTIVE":
                    self._record_ui_exception(
                        stage="execute.materialize_due",
                        exception_type="materialization_contract_not_active",
                        severity="BLOCKER",
                        field_name="lpo_state",
                        proposed_value=contract_row.get("lpo_state") if contract_row else None,
                        reason="Materialize blocked: contract must be ACTIVE after refresh",
                    )
                    self._flash_redirect(
                        f"/v2/contracts/{contract_id}/execute",
                        "Blocked: contract is not ACTIVE after refresh",
                        level="error",
                    )
                    return
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
                    as_of_date=as_of_date,
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
            as_of_date = utc_today_iso()
            service.refresh_contract_state(as_of_date=as_of_date)
            contract_row = repo.fetch_one("SELECT lpo_state FROM contracts WHERE contract_id = ?", (contract_id,))
            if not contract_row or str(contract_row.get("lpo_state") or "").upper() != "ACTIVE":
                self._record_ui_exception(
                    stage="execute.materialize_one",
                    exception_type="materialization_contract_not_active",
                    severity="BLOCKER",
                    field_name="lpo_state",
                    proposed_value=contract_row.get("lpo_state") if contract_row else None,
                    reason="Materialize blocked: contract must be ACTIVE after refresh",
                )
                self._flash_redirect(
                    f"/v2/contracts/{contract_id}/execute",
                    "Blocked: contract is not ACTIVE after refresh",
                    level="error",
                )
                return
            qty_raw = str(fields.get("qty_mt") or "").strip()
            qty_mt = float(qty_raw) if qty_raw else None
            materialized = service.materialize_delivery(
                planned_delivery_id=planned_delivery_id,
                qty_mt=qty_mt,
                as_of_date=as_of_date,
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
                "payment_date": str(fields.get("payment_date") or utc_today_iso()).strip(),
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
            as_of = str(fields.get("as_of_date") or utc_today_iso()).strip()
            out_dir = config.state_dir / "exports" / "web_v2" / as_of / dt.datetime.utcnow().strftime("%Y%m%d%H%M%S")
            result = service.export_drep(as_of_date=as_of, out_dir=out_dir)
            self._flash_redirect(
                f"/v2/contracts/{contract_id}/settle",
                f"DREP exported ({len(result['exports'])} files) for {as_of}",
            )

        def _handle_run_recommended(self, contract_id: str) -> None:
            fields = self._urlencoded_fields()
            as_of = str(fields.get("as_of_date") or utc_today_iso()).strip()
            cycle = service.run_recommended_cycle(
                contract_id=contract_id,
                as_of_date=as_of,
                dry_run=bool(fields.get("dry_run")),
            )
            run_id = str(cycle.get("autonomy_run_id") or "").strip()
            if cycle.get("ok"):
                message = "Run Recommended completed"
                if run_id:
                    message += f" (run {run_id})"
                target = f"/v2/contracts/{contract_id}/execute"
                if run_id:
                    target += f"?run_id={quote_plus(run_id)}"
                self._flash_redirect(target, message)
                return

            blocked = ", ".join(cycle.get("case_ids", []))
            target = f"/v2/exceptions?contract_id={quote_plus(contract_id)}"
            query_parts: list[str] = []
            if run_id:
                query_parts.append(f"run_id={quote_plus(run_id)}")
            if blocked:
                query_parts.append(f"case_ids={quote_plus(blocked)}")
            if query_parts:
                target += "&" + "&".join(query_parts)
            self._flash_redirect(
                target,
                f"Run Recommended blocked. Open cases: {blocked or 'none'}",
                level="error",
            )

        def _handle_run_all_preview(self) -> None:
            fields = self._urlencoded_fields()
            as_of = str(fields.get("as_of_date") or utc_today_iso()).strip()
            benchmark_version = str(fields.get("benchmark_version") or "phase2.pr6.v1").strip()
            max_contracts = int(str(fields.get("max_contracts_per_run") or "20"))
            max_actions = int(str(fields.get("max_actions_per_run") or "200"))
            preview = service.run_all_eligible(
                as_of_date_utc=as_of,
                mode="preview",
                benchmark_version=benchmark_version,
                max_contracts_per_run=max_contracts,
                max_actions_per_run=max_actions,
            )
            self._flash_redirect(
                "/v2/portfolio"
                f"?as_of_date={quote_plus(as_of)}"
                f"&benchmark_version={quote_plus(benchmark_version)}"
                f"&max_contracts_per_run={quote_plus(str(max_contracts))}"
                f"&max_actions_per_run={quote_plus(str(max_actions))}"
                f"&preview_token={quote_plus(str(preview.get('preview_token') or ''))}",
                f"Run-all preview ready (eligible={preview.get('eligible_count')}, skipped={len(preview.get('skipped_contracts', []))})",
            )

        def _handle_run_all_execute(self) -> None:
            fields = self._urlencoded_fields()
            as_of = str(fields.get("as_of_date") or utc_today_iso()).strip()
            benchmark_version = str(fields.get("benchmark_version") or "phase2.pr6.v1").strip()
            max_contracts = int(str(fields.get("max_contracts_per_run") or "20"))
            max_actions = int(str(fields.get("max_actions_per_run") or "200"))
            preview_token = str(fields.get("preview_token") or "").strip()
            result = service.run_all_eligible(
                as_of_date_utc=as_of,
                mode="execute",
                benchmark_version=benchmark_version,
                max_contracts_per_run=max_contracts,
                max_actions_per_run=max_actions,
                preview_token=preview_token,
            )
            summary = result.get("summary", {})
            self._flash_redirect(
                f"/v2/portfolio?as_of_date={quote_plus(as_of)}",
                "Run-all execute complete "
                f"(executed={summary.get('executed_count', 0)}, "
                f"already_applied={summary.get('already_applied_count', 0)}, "
                f"blocked={summary.get('blocked_count', 0)}, "
                f"skipped={summary.get('skipped_count', 0)})",
            )

        def _handle_exception_decide(self) -> None:
            fields = self._urlencoded_fields()
            case_id = str(fields.get("case_id") or "").strip()
            decision = str(fields.get("decision") or "").strip().upper()
            reason = str(fields.get("reason") or "").strip()
            if not case_id or decision not in {"APPROVE", "REJECT", "OVERRIDE"} or not reason:
                raise ValueError("case_id, decision(APPROVE|REJECT|OVERRIDE), and reason are required")
            result = service.decide_exception_case(
                case_id=case_id,
                decision=decision,
                reason=reason,
                resume=bool(fields.get("resume")),
                dry_run_resume=bool(fields.get("dry_run_resume")),
            )
            message = f"Case {case_id} resolved via {decision}"
            resume_result = result.get("resume_result")
            case_row = result.get("case") if isinstance(result.get("case"), dict) else {}
            contract_id = str(fields.get("contract_id") or case_row.get("contract_id") or "").strip()
            as_of_date = str(fields.get("as_of_date") or utc_today_iso()).strip()
            redirect_path = f"/v2/exceptions?as_of_date={quote_plus(as_of_date)}"
            if contract_id:
                redirect_path += f"&contract_id={quote_plus(contract_id)}"
            redirect_path += f"&case_id={quote_plus(case_id)}"
            if isinstance(resume_result, dict) and resume_result.get("autonomy_run_id"):
                message += f" (resume run {resume_result['autonomy_run_id']})"
                redirect_path += f"&run_id={quote_plus(str(resume_result['autonomy_run_id']))}"
            self._flash_redirect(redirect_path, message)

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
                    "payment_date": str(fields.get("payment_date") or utc_today_iso()).strip(),
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
            as_of = str(fields.get("as_of_date") or utc_today_iso()).strip()
            out_dir = config.state_dir / "exports" / "web_v2" / as_of / dt.datetime.utcnow().strftime("%Y%m%d%H%M%S")
            service.export_drep(as_of_date=as_of, out_dir=out_dir)
            self._flash_redirect("/v2/portfolio", f"Legacy export completed for {as_of}")

        def _intake_auto_apply_min(self) -> float:
            root = config.automation_thresholds if isinstance(config.automation_thresholds, dict) else {}
            intake = root.get("intake") if isinstance(root.get("intake"), dict) else {}
            return float(intake.get("field_auto_apply_min", 0.90))

        def _intake_confidence_matrix(self) -> dict[str, dict[str, float]]:
            return resolve_intake_confidence_matrix(config.automation_thresholds)

        def _intake_critical_fields(self) -> set[str]:
            return {
                "lpo_no",
                "buyer_id",
                "vendor_of_record_id",
                "product_code",
                "expected_qty_kg",
                "unit_price",
                "unit_price_basis",
            }

        def _is_missing_field_value(self, value: object) -> bool:
            if value is None:
                return True
            if isinstance(value, str):
                text = value.strip()
                if not text:
                    return True
                if text.lower() in {"none", "null", "nan"}:
                    return True
            return False

        def _intake_prefill_from_fields(self, *, fields: list[dict[str, object]], fallback: dict[str, str]) -> dict[str, object]:
            prefill: dict[str, object] = {key: value for key, value in fallback.items()}
            for row in fields:
                name = str(row.get("field_name") or "").strip()
                if not name:
                    continue
                value = row.get("proposed_value")
                if value in ("", None):
                    continue
                prefill[name] = value
            qty_kg = prefill.get("expected_qty_kg")
            if qty_kg in (None, "") and prefill.get("expected_qty_mt") not in (None, ""):
                try:
                    prefill["expected_qty_kg"] = mt_to_kg_int(float(str(prefill.get("expected_qty_mt"))))
                except Exception:
                    pass
            if not prefill.get("expected_qty_mt"):
                qty_kg = prefill.get("expected_qty_kg")
                if qty_kg not in (None, ""):
                    prefill["expected_qty_mt"] = kg_to_mt_str(int(float(str(qty_kg))))
            if not prefill.get("issue_date"):
                prefill["issue_date"] = utc_today_iso()
            if not prefill.get("lpo_valid_from"):
                prefill["lpo_valid_from"] = prefill.get("issue_date")
            if not prefill.get("start_date"):
                prefill["start_date"] = prefill.get("issue_date")
            if not prefill.get("unit_price_basis"):
                prefill["unit_price_basis"] = "KG"
            if not prefill.get("currency"):
                prefill["currency"] = "NGN"
            if not prefill.get("tolerance_pct"):
                prefill["tolerance_pct"] = 5.0
            return prefill

        def _persist_intake_uploads(self, uploaded: list[Path], *, run_id: str) -> list[Path]:
            if not uploaded:
                return []
            target_dir = config.state_dir / "intake_uploads" / run_id
            target_dir.mkdir(parents=True, exist_ok=True)
            saved: list[Path] = []
            for index, source in enumerate(uploaded, start=1):
                destination = target_dir / f"{index:02d}-{_safe_filename(source.name)}"
                destination.write_bytes(source.read_bytes())
                saved.append(destination)
            return saved

        def _load_intake_evidence_paths(self, *, data: dict[str, str]) -> list[str]:
            raw = str(data.get("intake_evidence_paths_json") or "").strip()
            if not raw:
                return []
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                return []
            if not isinstance(parsed, list):
                return []
            return [str(item) for item in parsed if str(item).strip()]

        def _record_intake_confirm_decisions(self, *, run_id: str, submitted: dict[str, str]) -> None:
            run_id = run_id.strip()
            if not run_id:
                return
            run = repo.get_automation_run(run_id)
            if not run:
                return
            parser_rows = repo.fetch_all(
                """
                SELECT field_name, proposed_value
                FROM automation_decisions
                WHERE run_id = ? AND stage = 'intake_parser'
                ORDER BY created_at ASC
                """,
                (run_id,),
            )
            proposed_map: dict[str, str] = {}
            for row in parser_rows:
                field_name = str(row.get("field_name") or "")
                if not field_name:
                    continue
                proposed_map[field_name] = str(row.get("proposed_value") or "")
            tracked_fields = sorted(set(self._intake_critical_fields()) | set(proposed_map.keys()))
            now = utc_now_iso_z()
            with repo.transaction() as conn:
                for field_name in tracked_fields:
                    final_value = str(submitted.get(field_name) or "").strip()
                    proposed_value = str(proposed_map.get(field_name) or "").strip()
                    if not final_value:
                        continue
                    decision = "user_confirmed" if final_value == proposed_value else "user_corrected"
                    repo.add_automation_decision(
                        conn,
                        run_id=run_id,
                        stage="intake_confirm",
                        field_name=field_name,
                        required_flag=field_name in self._intake_critical_fields(),
                        proposed_value=final_value,
                        source_type="user_input",
                        source_ref="/v2/intake/confirm",
                        confidence=1.0,
                        decision=decision,
                        reason_code=decision,
                        rule_path=f"intake_confirm.{field_name}",
                    )
                    conn.execute(
                        """
                        UPDATE exception_queue
                        SET status = 'RESOLVED',
                            resolved_value = ?,
                            resolution_note = 'intake_confirm',
                            resolved_at = ?
                        WHERE run_id = ?
                          AND stage = 'intake_parser'
                          AND field_name = ?
                          AND status = 'OPEN'
                        """,
                        (final_value, now, run_id, field_name),
                    )
                blockers = conn.execute(
                    """
                    SELECT COUNT(*) AS total
                    FROM exception_queue
                    WHERE run_id = ? AND status = 'OPEN' AND severity = 'BLOCKER'
                    """,
                    (run_id,),
                ).fetchone()
                next_status = "NEEDS_REVIEW" if int((blockers or {"total": 0})["total"] or 0) > 0 else "COMPLETED"
                conn.execute(
                    "UPDATE automation_runs SET status = ?, updated_at = ? WHERE run_id = ?",
                    (next_status, now, run_id),
                )

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
                    as_of_date=utc_today_iso(),
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

        def _record_exception_case(
            self,
            *,
            contract_id: str,
            case_type: str,
            severity: str,
            reason_code: str,
            details: dict[str, object],
        ) -> dict[str, object]:
            contract_id_norm = str(contract_id or "").strip() or None
            details_payload = dict(details)
            idempotency_key = canonical_json_sha256(
                {
                    "contract_id": contract_id_norm,
                    "case_type": case_type,
                    "severity": severity,
                    "reason_code": reason_code,
                    "details": details_payload,
                }
            )
            with repo.transaction() as conn:
                case = repo.create_or_get_exception_case(
                    conn,
                    autonomy_run_id=new_ulid(),
                    action_intent_id=None,
                    contract_id=contract_id_norm,
                    delivery_id=None,
                    planned_delivery_id=str(details_payload.get("planned_delivery_id") or "").strip() or None,
                    case_type=case_type,
                    severity=severity,
                    reason_code=reason_code,
                    details=details_payload,
                    idempotency_key=idempotency_key,
                )
                repo.append_event(
                    conn,
                    entity_type="EXCEPTION_CASE",
                    entity_id=str(case.get("exception_case_id") or ""),
                    event_type="CASE_CREATED",
                    as_of_date=str(details_payload.get("as_of_date") or utc_today_iso()),
                    payload=details_payload,
                    source="web_v2",
                )
                return case

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

        def _portfolio_row_html(self, row: dict[str, object], as_of_date_utc: str) -> str:
            contract_id = str(row.get("contract_id") or "")
            needs_decision = int(row.get("needs_decision_count") or 0)
            outstanding = float(row.get("outstanding_total") or 0.0)
            if needs_decision > 0:
                primary_action = (
                    f"<a class='btn' href='/v2/exceptions?contract_id={quote_plus(contract_id)}'>Resolve Exceptions</a>"
                )
            else:
                primary_action = (
                    f"<form method='POST' action='/v2/contracts/{_escape(contract_id)}/run-recommended' class='inline-form'>"
                    f"<input type='hidden' name='as_of_date' value='{_escape(as_of_date_utc)}' />"
                    "<button type='submit'>Run Recommended</button>"
                    "</form>"
                )
            advanced_links = (
                "<details class='advanced'>"
                "<summary>Advanced</summary>"
                f"<a href='/v2/contracts/{_escape(contract_id)}/plan'>Plan</a> · "
                f"<a href='/v2/contracts/{_escape(contract_id)}/execute'>Execute</a> · "
                f"<a href='/v2/contracts/{_escape(contract_id)}/settle'>Settle</a>"
                "</details>"
            )
            return (
                "<tr>"
                f"<td>{_escape(row.get('lpo_no') or row.get('contract_ref'))}<br/><span class='muted'>{_escape(contract_id)}</span></td>"
                f"<td>{_escape(row.get('buyer_id'))}<br/><span class='muted'>{_escape(row.get('vendor_of_record_id'))}</span></td>"
                f"<td><span class='pill'>{_escape(row.get('lpo_state'))}</span></td>"
                f"<td>{_escape(_fmt_qty_mt_from_kg(row.get('expected_total_qty_kg')))} / {_escape(_fmt_qty_mt_from_kg(row.get('delivered_qty_kg')))}</td>"
                f"<td>{_escape(row.get('open_planned_lots'))}</td>"
                f"<td>{_escape(row.get('due_planned_lots'))}</td>"
                f"<td>{_escape(row.get('delivered_not_invoiced'))}</td>"
                f"<td>{_escape(f'{outstanding:,.2f}')}</td>"
                f"<td><a href='/v2/exceptions?contract_id={quote_plus(contract_id)}'><span class='pill'>{_escape(needs_decision)}</span></a></td>"
                f"<td>{primary_action}{advanced_links}</td>"
                "</tr>"
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
            separator = "&" if "?" in path else "?"
            self._redirect(f"{path}{separator}msg={quote_plus(message)}&level={quote_plus(level)}")

        def _render_page(self, title: str, body_html: str, *, active: str, msg: str = "", level: str = "ok") -> None:
            alert = ""
            if msg:
                css_class = "err" if level == "error" else "ok"
                alert = f"<div class='alert {css_class}'>{_escape(msg)}</div>"
            nav = (
                "<nav>"
                f"{self._nav_link('/v2/portfolio', 'Command Center', active == 'portfolio')}"
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
                "details.advanced{margin-top:10px;border:1px solid #e5e7eb;padding:8px;border-radius:8px;background:#fcfcfd}"
                "details.advanced summary{cursor:pointer;font-weight:600}"
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
