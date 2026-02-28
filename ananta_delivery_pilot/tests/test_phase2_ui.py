from __future__ import annotations

import json
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.parse
import urllib.request
from pathlib import Path

from adapters.sqlite_repo import SQLiteRepo
from core.config import RuntimeConfig
from domain.automation import AutomationOrchestrator
from domain.services import Phase1Service


class Phase2UiRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.temp_dir = Path(tempfile.mkdtemp(prefix="phase2-ui-tests-"))
        shutil.copytree(self.repo_root / "config", self.temp_dir / "config")
        (self.temp_dir / ".state").mkdir(parents=True, exist_ok=True)
        (self.temp_dir / "output_v2").mkdir(parents=True, exist_ok=True)
        self.config = RuntimeConfig.load(self.temp_dir)
        self.repo = SQLiteRepo(self.config.state_dir / "drep.sqlite")
        self.service = Phase1Service(self.config, self.repo)
        self.orchestrator = AutomationOrchestrator(self.config, self.repo, self.service)
        self.service.init_db()

        contract = self.service.create_contract(
            {
                "contract_ref": "LPO-UI-ROUTES-001",
                "lpo_no": "LPO-UI-ROUTES-001",
                "lpo_date": "2026-02-23",
                "buyer_id": "buyer_nycil",
                "vendor_of_record_id": "ananta_flows",
                "operator_id": "guildgate",
                "source_id": "ananta_flows",
                "processor_id": "processor_partner_refinery",
                "currency": "NGN",
                "issue_date": "2026-02-23",
                "lpo_valid_from": "2026-02-23",
                "lpo_valid_to": "2026-03-31",
                "expected_total_qty": 150.0,
                "expected_total_value": 340500000.0,
                "lines": [
                    {
                        "product_code": "RBDPO",
                        "description": "RBDPO route test",
                        "expected_qty": 150.0,
                        "unit": "mt",
                        "unit_price": 2270.0,
                        "unit_price_basis": "KG",
                        "expected_value": 340500000.0,
                    }
                ],
            },
            allow_placeholder_tin=True,
        )
        self.contract_id = str(contract["contract_id"])
        self.service.plan_deliveries(
            contract_id=self.contract_id,
            start_date="2026-02-23",
            cadence="daily",
            max_lots_per_day=2,
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_workflow_routes_smoke(self) -> None:
        try:
            port = _pick_free_port()
        except PermissionError:
            self.skipTest("socket bind not permitted in current sandbox")

        proc = _start_ui_server(repo_root=self.repo_root, root=self.temp_dir, port=port)
        try:
            _wait_for_route(port, "/v2/portfolio")
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=3) as response:
                self.assertEqual(200, response.status)
                root_body = response.read().decode("utf-8")
                self.assertIn("Phase 2 (Ledger UI)", root_body)
            routes = [
                "/v2/portfolio",
                "/v2/workbench",
                "/v2/intake",
                f"/v2/contracts/{self.contract_id}/plan",
                f"/v2/contracts/{self.contract_id}/execute",
                f"/v2/contracts/{self.contract_id}/settle",
                "/v2/exceptions",
            ]
            for route in routes:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}{route}", timeout=3) as response:
                    self.assertEqual(200, response.status)
                    body = response.read().decode("utf-8")
                    self.assertIn("Phase 2 (Ledger UI)", body)
                    if route in {"/v2/portfolio", "/v2/workbench"}:
                        self.assertIn("Command Center", body)
                    if route.endswith("/execute"):
                        self.assertIn("Transport Copilot", body)
                        self.assertIn("Document Completion Copilot", body)
        finally:
            _stop_process(proc)

    def test_intake_page_simplified_with_advanced_section(self) -> None:
        try:
            port = _pick_free_port()
        except PermissionError:
            self.skipTest("socket bind not permitted in current sandbox")
        proc = _start_ui_server(repo_root=self.repo_root, root=self.temp_dir, port=port)
        try:
            _wait_for_route(port, "/v2/intake")
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/v2/intake", timeout=3) as response:
                self.assertEqual(200, response.status)
                body = response.read().decode("utf-8")
                self.assertIn("Advanced intake fields", body)
                self.assertIn("Parse LPO + Review", body)
                self.assertIn("Critical fields", body.lower())
        finally:
            _stop_process(proc)

    def test_intake_parse_prefill_known_format_and_confirm_trace(self) -> None:
        try:
            port = _pick_free_port()
        except PermissionError:
            self.skipTest("socket bind not permitted in current sandbox")
        proc = _start_ui_server(repo_root=self.repo_root, root=self.temp_dir, port=port)
        try:
            _wait_for_route(port, "/v2/intake")
            lpo_file = self.temp_dir / "known_lpo.json"
            lpo_payload = {
                "lpo_no": "LPO-UI-PARSE-001",
                "lpo_date": "2026-02-23",
                "issue_date": "2026-02-23",
                "lpo_valid_from": "2026-02-23",
                "lpo_valid_to": "2026-03-31",
                "buyer_id": "buyer_nycil",
                "vendor_of_record_id": "ananta_flows",
                "source_id": "ananta_flows",
                "processor_id": "processor_partner_refinery",
                "product_code": "RBDSO",
                "description": "UI parser contract",
                "expected_qty_mt": 150,
                "unit_price": 2270,
                "unit_price_basis": "KG",
                "currency": "NGN",
            }
            lpo_file.write_text(json.dumps(lpo_payload), encoding="utf-8")
            parse_html = _post_multipart(
                port=port,
                path="/v2/intake/parse",
                fields={"allow_placeholder_tin": "1"},
                files={"lpo_originals": lpo_file},
            )
            self.assertIn("Intake Review", parse_html)
            self.assertIn("LPO-UI-PARSE-001", parse_html)
            self.assertIn("Parser Diff + Decision Trace", parse_html)
            self.assertIn("Expected Qty (KG)", parse_html)
            run_id = _hidden_value(parse_html, "intake_run_id")
            self.assertTrue(run_id)

            run = self.repo.get_automation_run(run_id)
            self.assertIsNotNone(run)
            assert run is not None
            normalized = json.loads(run["normalized_input_json"] or "{}")
            evidence_paths = normalized.get("evidence_paths") or []
            self.assertTrue(isinstance(evidence_paths, list) and evidence_paths)

            confirm_fields = {
                "intake_run_id": run_id,
                "intake_evidence_paths_json": json.dumps(evidence_paths),
                "lpo_no": "LPO-UI-PARSE-001",
                "lpo_date": "2026-02-23",
                "issue_date": "2026-02-23",
                "lpo_valid_from": "2026-02-23",
                "lpo_valid_to": "2026-03-31",
                "buyer_id": "buyer_nycil",
                "vendor_of_record_id": "ananta_flows",
                "source_id": "ananta_flows",
                "processor_id": "processor_partner_refinery",
                "product_code": "RBDSO",
                "description": "UI parser contract corrected",
                "expected_qty_mt": "150.000",
                "expected_qty_kg": "150000",
                "unit_price": "2270",
                "unit_price_basis": "KG",
                "currency": "NGN",
                "start_date": "2026-02-23",
                "cadence": "daily",
                "max_lots_per_day": "1",
                "tolerance_pct": "5.0",
                "allow_placeholder_tin": "1",
            }
            confirm_html = _post_form(port=port, path="/v2/intake/confirm", fields=confirm_fields)
            self.assertIn("Plan -", confirm_html)

            decision_rows = self.repo.fetch_all(
                "SELECT * FROM automation_decisions WHERE run_id = ? AND stage = 'intake_confirm'",
                (run_id,),
            )
            self.assertTrue(decision_rows)
            self.assertIn(
                "user_corrected",
                {str(row.get("decision") or "") for row in decision_rows},
            )
        finally:
            _stop_process(proc)

    def test_intake_parse_idempotent_and_exception_routing(self) -> None:
        try:
            port = _pick_free_port()
        except PermissionError:
            self.skipTest("socket bind not permitted in current sandbox")
        proc = _start_ui_server(repo_root=self.repo_root, root=self.temp_dir, port=port)
        try:
            _wait_for_route(port, "/v2/intake")
            lpo_file = self.temp_dir / "low_conf_lpo.txt"
            lpo_file.write_text(
                "\n".join(
                    [
                        "LPO No: LPO-LOW-CONF-001",
                        "Buyer: Unknown Buyer LLC",
                        "Supplier: Unknown Supplier",
                        "Quantity: 150",
                    ]
                ),
                encoding="utf-8",
            )
            first_html = _post_multipart(
                port=port,
                path="/v2/intake/parse",
                fields={},
                files={"lpo_originals": lpo_file},
            )
            run_id_1 = _hidden_value(first_html, "intake_run_id")
            self.assertTrue(run_id_1)
            open_1 = self.repo.list_exceptions(run_id=run_id_1, status="OPEN")
            self.assertTrue(open_1)
            self.assertTrue(any(str(row.get("severity")) == "BLOCKER" for row in open_1))

            second_html = _post_multipart(
                port=port,
                path="/v2/intake/parse",
                fields={},
                files={"lpo_originals": lpo_file},
            )
            run_id_2 = _hidden_value(second_html, "intake_run_id")
            self.assertEqual(run_id_1, run_id_2)
            open_2 = self.repo.list_exceptions(run_id=run_id_2, status="OPEN")
            self.assertEqual(len(open_1), len(open_2))
        finally:
            _stop_process(proc)

    def test_plan_rebuild_block_routes_to_exceptions(self) -> None:
        contract = self.service.create_contract(
            {
                "contract_ref": "LPO-UI-PLAN-BLOCK-001",
                "lpo_no": "LPO-UI-PLAN-BLOCK-001",
                "lpo_date": "2026-02-23",
                "buyer_id": "buyer_nycil",
                "vendor_of_record_id": "ananta_flows",
                "operator_id": "guildgate",
                "source_id": "ananta_flows",
                "processor_id": "processor_partner_refinery",
                "currency": "NGN",
                "issue_date": "2026-02-23",
                "lpo_valid_from": "2026-02-23",
                "lpo_valid_to": "2026-02-23",
                "expected_total_qty": 150.0,
                "expected_total_value": 340500000.0,
                "lines": [
                    {
                        "product_code": "RBDPO",
                        "description": "RBDPO short window",
                        "expected_qty": 150.0,
                        "unit": "mt",
                        "unit_price": 2270.0,
                        "unit_price_basis": "KG",
                        "expected_value": 340500000.0,
                    }
                ],
            },
            allow_placeholder_tin=True,
        )
        contract_id = str(contract["contract_id"])
        try:
            port = _pick_free_port()
        except PermissionError:
            self.skipTest("socket bind not permitted in current sandbox")
        proc = _start_ui_server(repo_root=self.repo_root, root=self.temp_dir, port=port)
        try:
            _wait_for_route(port, f"/v2/contracts/{contract_id}/plan")
            response_html = _post_form(
                port=port,
                path=f"/v2/contracts/{contract_id}/plan/rebuild",
                fields={
                    "start_date": "2026-02-23",
                    "cadence": "daily",
                    "max_lots_per_day": "1",
                },
            )
            self.assertIn("Exceptions Queue", response_html)
            self.assertIn(contract_id, response_html)
            self.assertIn("planning_window_invalid", response_html)
        finally:
            _stop_process(proc)

    def test_run_recommended_executes_contract_cycle_and_shows_timeline(self) -> None:
        evidence = self.temp_dir / "evidence_cmd_center.txt"
        evidence.write_text("evidence", encoding="utf-8")
        self.service.capture_evidence_original(contract_id=self.contract_id, source_path=evidence)
        try:
            port = _pick_free_port()
        except PermissionError:
            self.skipTest("socket bind not permitted in current sandbox")
        proc = _start_ui_server(repo_root=self.repo_root, root=self.temp_dir, port=port)
        try:
            _wait_for_route(port, "/v2/portfolio")
            html_body = _post_form(
                port=port,
                path=f"/v2/contracts/{self.contract_id}/run-recommended",
                fields={"as_of_date": "2026-02-23"},
            )
            self.assertIn("Recommended Run Timeline", html_body)
            intents = self.repo.fetch_all(
                "SELECT * FROM action_intents WHERE contract_id = ? AND as_of_date = ?",
                (self.contract_id, "2026-02-23"),
            )
            self.assertGreaterEqual(len(intents), 1)
            export_dir = self.temp_dir / ".state" / "exports" / "command_center" / "2026-02-23" / self.contract_id
            self.assertTrue(export_dir.exists())
        finally:
            _stop_process(proc)

    def test_run_recommended_block_redirects_to_exceptions(self) -> None:
        self.service.cancel_contract(contract_id=self.contract_id, reason="blocked-path-ui")
        try:
            port = _pick_free_port()
        except PermissionError:
            self.skipTest("socket bind not permitted in current sandbox")
        proc = _start_ui_server(repo_root=self.repo_root, root=self.temp_dir, port=port)
        try:
            _wait_for_route(port, "/v2/portfolio")
            html_body = _post_form(
                port=port,
                path=f"/v2/contracts/{self.contract_id}/run-recommended",
                fields={"as_of_date": "2026-02-23"},
            )
            self.assertIn("Exceptions Queue", html_body)
            self.assertIn(self.contract_id, html_body)
        finally:
            _stop_process(proc)

    def test_run_all_preview_and_execute_from_portfolio(self) -> None:
        evidence = self.temp_dir / "evidence_run_all.txt"
        evidence.write_text("evidence", encoding="utf-8")
        self.service.capture_evidence_original(contract_id=self.contract_id, source_path=evidence)
        try:
            port = _pick_free_port()
        except PermissionError:
            self.skipTest("socket bind not permitted in current sandbox")
        proc = _start_ui_server(repo_root=self.repo_root, root=self.temp_dir, port=port)
        try:
            _wait_for_route(port, "/v2/portfolio")
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/v2/portfolio", timeout=3) as response:
                initial_html = response.read().decode("utf-8")
            self.assertNotIn("Run All Eligible (Execute)", initial_html)
            preview_html = _post_form(
                port=port,
                path="/v2/run-all-eligible",
                fields={
                    "as_of_date": "2026-02-23",
                    "benchmark_version": "phase2.pr6.v1",
                    "max_contracts_per_run": "20",
                    "max_actions_per_run": "200",
                },
            )
            self.assertIn("Run All Preview", preview_html)
            preview_token = _hidden_value(preview_html, "preview_token")
            self.assertTrue(preview_token)
            execute_html = _post_form(
                port=port,
                path="/v2/run-all-eligible/execute",
                fields={
                    "as_of_date": "2026-02-23",
                    "benchmark_version": "phase2.pr6.v1",
                    "max_contracts_per_run": "20",
                    "max_actions_per_run": "200",
                    "preview_token": preview_token,
                },
            )
            self.assertIn("Command Center", execute_html)
            self.assertIn("Run-all execute complete", execute_html)
            intents = self.repo.fetch_all(
                "SELECT * FROM action_intents WHERE contract_id = ? AND as_of_date = ?",
                (self.contract_id, "2026-02-23"),
            )
            self.assertGreaterEqual(len(intents), 1)
        finally:
            _stop_process(proc)

    def test_exceptions_inbox_decide_and_resume(self) -> None:
        self.service.cancel_contract(contract_id=self.contract_id, reason="ui test case")
        run = self.orchestrator.run_autonomy(as_of_date="2026-02-23", contract_id=self.contract_id, dry_run=True)
        self.assertTrue(run["ok"])
        cases = self.repo.list_exception_cases(status="OPEN")
        target_case = next(row for row in cases if str(row.get("contract_id") or "") == self.contract_id)
        try:
            port = _pick_free_port()
        except PermissionError:
            self.skipTest("socket bind not permitted in current sandbox")
        proc = _start_ui_server(repo_root=self.repo_root, root=self.temp_dir, port=port)
        try:
            _wait_for_route(port, "/v2/exceptions")
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/v2/exceptions", timeout=3) as response:
                inbox_html = response.read().decode("utf-8")
            self.assertIn("Consequence Preview", inbox_html)
            body = _post_form(
                port=port,
                path="/v2/exceptions/decide",
                fields={
                    "case_id": str(target_case["exception_case_id"]),
                    "decision": "APPROVE",
                    "reason": "approved in ui test",
                    "resume": "1",
                    "dry_run_resume": "1",
                },
            )
            self.assertIn("Exceptions Queue", body)
            updated = self.repo.get_exception_case(str(target_case["exception_case_id"]))
            self.assertIsNotNone(updated)
            assert updated is not None
            self.assertEqual("RESOLVED", str(updated["status"]))
            decision_rows = self.repo.fetch_all(
                "SELECT * FROM human_decisions WHERE exception_case_id = ?",
                (str(target_case["exception_case_id"]),),
            )
            self.assertTrue(decision_rows)
        finally:
            _stop_process(proc)


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_route(port: int, route: str) -> None:
    deadline = time.time() + 20
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{route}", timeout=2) as response:
                if response.status == 200:
                    return
        except Exception as error:  # noqa: BLE001
            last_error = error
            time.sleep(0.25)
    raise AssertionError(f"Unable to reach route {route}: {last_error}")


def _start_ui_server(*, repo_root: Path, root: Path, port: int) -> subprocess.Popen[bytes]:
    command = [
        "python3",
        "-c",
        (
            "from pathlib import Path; "
            "from apps.web_v2 import run_server_v2; "
            f"run_server_v2(Path(r'{str(root)}'), host='127.0.0.1', port={port})"
        ),
    ]
    return subprocess.Popen(
        command,
        cwd=repo_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _stop_process(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _post_form(*, port: int, path: str, fields: dict[str, str]) -> str:
    payload = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.read().decode("utf-8")


def _post_multipart(*, port: int, path: str, fields: dict[str, str], files: dict[str, Path]) -> str:
    boundary = f"----phase2-ui-{int(time.time() * 1000)}"
    chunks: list[bytes] = []
    for key, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode("utf-8"))
        chunks.append(f"{value}\r\n".encode("utf-8"))
    for field_name, path_obj in files.items():
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(
            (
                f'Content-Disposition: form-data; name="{field_name}"; filename="{path_obj.name}"\r\n'
                "Content-Type: application/octet-stream\r\n\r\n"
            ).encode("utf-8")
        )
        chunks.append(path_obj.read_bytes())
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(chunks)
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.read().decode("utf-8")


def _hidden_value(html_text: str, name: str) -> str:
    match = re.search(rf"name=['\"]{re.escape(name)}['\"] value=['\"]([^'\"]*)['\"]", html_text)
    return str(match.group(1)) if match else ""


if __name__ == "__main__":
    unittest.main()
