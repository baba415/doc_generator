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
                        "product_code": "RBDSO",
                        "description": "RBDSO route test",
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

    def _create_invoiced_delivery(self, *, suffix: str, delivery_date: str = "2026-02-23") -> dict[str, str]:
        delivery = self.service.add_delivery(
            {
                "contract_id": self.contract_id,
                "line_no": 1,
                "delivery_ref": f"DLV-UI-{suffix}",
                "run_id": f"RUN-UI-{suffix}",
                "batch_id": f"AFL-RBDSO-UI-{suffix}",
                "delivery_date": delivery_date,
                "delivered_qty": 30000,
                "unit": "kgs",
                "unit_price": 2270.0,
                "unit_price_basis": "KG",
            }
        )
        delivery_id = str(delivery["delivery_id"])
        self.service.mark_dispatched(delivery_id)
        self.service.mark_delivered(delivery_id)
        coa_payload = self.service.coa_template_for_delivery(delivery_id, default_result="PASS")
        self.service.record_coa(coa_payload)
        pack = self.service.generate_pack(
            delivery_id=delivery_id,
            allow_placeholder_tin=True,
            skip_pdf=True,
            original_docs=[],
        )
        return {
            "delivery_id": delivery_id,
            "invoice_no": str(pack["invoice_no"]),
            "sales_transaction_id": str(pack["sales_transaction_id"]),
        }

    def _write_drift_metrics_ref(
        self,
        *,
        file_name: str,
        benchmark_version: str,
        median_manual_fields_per_intake: float | None = 1.0,
        autoplan_zero_edit_common_case_rate: float | None = 1.0,
        manual_transport_fields_per_delivery: float | None = 0.5,
        doc_autolink_precision: float | None = 0.95,
        payment_suggestion_acceptance_rate: float | None = 0.80,
        auto_action_success_rate: float | None = 0.90,
    ) -> Path:
        payload = {
            "as_of_date": "2026-02-28",
            "lookback_window_days": 30,
            "benchmark_version": benchmark_version,
            "generated_at_utc": "2026-02-28T12:00:00Z",
            "median_manual_fields_per_intake": median_manual_fields_per_intake,
            "autoplan_zero_edit_common_case_rate": autoplan_zero_edit_common_case_rate,
            "manual_transport_fields_per_delivery": manual_transport_fields_per_delivery,
            "doc_autolink_precision": doc_autolink_precision,
            "payment_suggestion_acceptance_rate": payment_suggestion_acceptance_rate,
            "auto_action_success_rate": auto_action_success_rate,
        }
        path = self.temp_dir / file_name
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return path

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

    def test_intake_parse_applies_prior_correction_memory_for_same_buyer_product(self) -> None:
        try:
            port = _pick_free_port()
        except PermissionError:
            self.skipTest("socket bind not permitted in current sandbox")
        proc = _start_ui_server(repo_root=self.repo_root, root=self.temp_dir, port=port)
        try:
            _wait_for_route(port, "/v2/intake")
            first_lpo = self.temp_dir / "memory_lpo_first.json"
            first_lpo.write_text(
                json.dumps(
                    {
                        "lpo_no": "LPO-MEM-001",
                        "buyer_id": "buyer_nycil",
                        "vendor_of_record_id": "ananta_flows",
                        "source_id": "ananta_flows",
                        "processor_id": "processor_partner_refinery",
                        "product_code": "RBDPO",
                        "expected_qty_mt": 150,
                        "unit_price": 2270,
                        "unit_price_basis": "KG",
                        "issue_date": "2026-02-23",
                        "lpo_valid_from": "2026-02-23",
                        "lpo_valid_to": "2026-03-31",
                    }
                ),
                encoding="utf-8",
            )
            parse_html_1 = _post_multipart(
                port=port,
                path="/v2/intake/parse",
                fields={"allow_placeholder_tin": "1"},
                files={"lpo_originals": first_lpo},
            )
            run_id_1 = _hidden_value(parse_html_1, "intake_run_id")
            run_1 = self.repo.get_automation_run(run_id_1)
            assert run_1 is not None
            evidence_paths_1 = json.loads(run_1["normalized_input_json"]).get("evidence_paths") or []
            _post_form(
                port=port,
                path="/v2/intake/confirm",
                fields={
                    "intake_run_id": run_id_1,
                    "intake_evidence_paths_json": json.dumps(evidence_paths_1),
                    "lpo_no": "LPO-MEM-001",
                    "issue_date": "2026-02-23",
                    "lpo_valid_from": "2026-02-23",
                    "lpo_valid_to": "2026-03-31",
                    "buyer_id": "buyer_nycil",
                    "vendor_of_record_id": "ananta_flows",
                    "source_id": "ananta_flows",
                    "processor_id": "processor_partner_refinery",
                    "product_code": "RBDPO",
                    "expected_qty_mt": "150.000",
                    "expected_qty_kg": "150000",
                    "unit_price": "2300",
                    "unit_price_basis": "KG",
                    "currency": "NGN",
                    "start_date": "2026-02-23",
                    "cadence": "daily",
                    "max_lots_per_day": "1",
                    "tolerance_pct": "5.0",
                    "allow_placeholder_tin": "1",
                },
            )
            second_lpo = self.temp_dir / "memory_lpo_second.json"
            second_lpo.write_text(
                json.dumps(
                    {
                        "lpo_no": "LPO-MEM-002",
                        "buyer_id": "buyer_nycil",
                        "vendor_of_record_id": "ananta_flows",
                        "source_id": "ananta_flows",
                        "processor_id": "processor_partner_refinery",
                        "product_code": "RBDPO",
                        "expected_qty_mt": 150,
                        "unit_price": 2100,
                        "unit_price_basis": "KG",
                        "issue_date": "2026-02-24",
                        "lpo_valid_from": "2026-02-24",
                        "lpo_valid_to": "2026-03-31",
                    }
                ),
                encoding="utf-8",
            )
            parse_html_2 = _post_multipart(
                port=port,
                path="/v2/intake/parse",
                fields={"allow_placeholder_tin": "1"},
                files={"lpo_originals": second_lpo},
            )
            self.assertIn("2300", parse_html_2)
            run_id_2 = _hidden_value(parse_html_2, "intake_run_id")
            parser_rows = self.repo.fetch_all(
                """
                SELECT field_name, source_type, reason_code, decision, proposed_value
                FROM automation_decisions
                WHERE run_id = ? AND stage = 'intake_parser' AND field_name = 'unit_price'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (run_id_2,),
            )
            self.assertTrue(parser_rows)
            latest = parser_rows[0]
            self.assertEqual("correction_memory", str(latest["source_type"]))
            self.assertIn("correction_memory_applied", str(latest["reason_code"]))
            self.assertEqual("auto_applied", str(latest["decision"]))
            self.assertEqual("2300", str(latest["proposed_value"]))
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

    def test_settle_suggestions_render_and_ambiguity_routes_to_exceptions(self) -> None:
        self._create_invoiced_delivery(suffix="A")
        self._create_invoiced_delivery(suffix="B")
        try:
            port = _pick_free_port()
        except PermissionError:
            self.skipTest("socket bind not permitted in current sandbox")
        proc = _start_ui_server(repo_root=self.repo_root, root=self.temp_dir, port=port)
        try:
            _wait_for_route(port, f"/v2/contracts/{self.contract_id}/settle")
            suggest_html = _post_form(
                port=port,
                path=f"/v2/contracts/{self.contract_id}/settle/suggest",
                fields={
                    "as_of_date": "2026-02-23",
                    "payment_date": "2026-02-23",
                    "amount_received": "68100000.00",
                    "payment_method": "Bank Transfer",
                    "payment_reference": "INV-2026",
                },
            )
            self.assertIn("Settlement Copilot Suggestions", suggest_html)
            self.assertIn("decision_class=BLOCKER", suggest_html)
            suggestion_set_id = _hidden_value(suggest_html, "suggestion_set_id")
            suggestion_id = _hidden_value(suggest_html, "suggestion_id")
            self.assertTrue(suggestion_set_id)
            self.assertTrue(suggestion_id)

            routed_html = _post_form(
                port=port,
                path=f"/v2/contracts/{self.contract_id}/settle/apply-suggestion",
                fields={
                    "suggestion_set_id": suggestion_set_id,
                    "suggestion_id": suggestion_id,
                    "as_of_date": "2026-02-23",
                    "payment_date": "2026-02-23",
                    "payment_method": "Bank Transfer",
                    "payment_reference": "INV-2026",
                    "amount_received": "68100000.00",
                    "reason": "ambiguous allocation",
                },
            )
            self.assertIn("Exceptions Queue", routed_html)
            self.assertIn(self.contract_id, routed_html)
        finally:
            _stop_process(proc)

    def test_portfolio_gate_health_strip_renders_read_only_status(self) -> None:
        as_of_date = "2026-02-28"
        benchmark_version = "phase2.pr11.v1"
        self.orchestrator.seed_phase2_benchmark(
            as_of_date=as_of_date,
            benchmark_version=benchmark_version,
            reset=False,
            lookback_window_days=30,
        )
        self.orchestrator.phase2_gate_report(
            as_of_date=as_of_date,
            lookback_window_days=30,
            benchmark_version=benchmark_version,
            out_dir=self.temp_dir / "gate-report",
        )
        try:
            port = _pick_free_port()
        except PermissionError:
            self.skipTest("socket bind not permitted in current sandbox")
        proc = _start_ui_server(repo_root=self.repo_root, root=self.temp_dir, port=port)
        try:
            _wait_for_route(port, "/v2/portfolio")
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/v2/portfolio?as_of={as_of_date}&lookback_window_days=30&benchmark_version={benchmark_version}",
                timeout=3,
            ) as response:
                body = response.read().decode("utf-8")
            self.assertIn("Gate Health (PR8/PR9/PR10)", body)
            self.assertIn("<td>pr8</td>", body)
            self.assertIn("<td>pr9</td>", body)
            self.assertIn("<td>pr10</td>", body)
            self.assertIn("Latest gate report:", body)
            self.assertNotIn("name='waiver_id'", body)
            self.assertNotIn("name='owner_product'", body)
        finally:
            _stop_process(proc)

    def test_portfolio_drift_strip_renders_read_only_status(self) -> None:
        as_of_date = "2026-02-28"
        benchmark_version = "phase2.pr12.v1"
        self.orchestrator.seed_phase2_benchmark(
            as_of_date=as_of_date,
            benchmark_version=benchmark_version,
            reset=False,
            lookback_window_days=30,
        )
        self.orchestrator.phase2_drift_report(
            as_of_date=as_of_date,
            lookback_window_days=30,
            benchmark_version=benchmark_version,
            out_dir=self.temp_dir / "drift-report",
        )
        try:
            port = _pick_free_port()
        except PermissionError:
            self.skipTest("socket bind not permitted in current sandbox")
        proc = _start_ui_server(repo_root=self.repo_root, root=self.temp_dir, port=port)
        try:
            _wait_for_route(port, "/v2/portfolio")
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/v2/portfolio?as_of={as_of_date}&lookback_window_days=30&benchmark_version={benchmark_version}",
                timeout=3,
            ) as response:
                body = response.read().decode("utf-8")
            self.assertIn("Drift Monitor (PR8/PR9/PR10)", body)
            self.assertIn("<td>pr8</td>", body)
            self.assertIn("<td>pr9</td>", body)
            self.assertIn("<td>pr10</td>", body)
            self.assertIn("Latest drift report:", body)
            self.assertNotIn("name='waiver_id'", body)
            self.assertNotIn("name='policy_set_id'", body)
        finally:
            _stop_process(proc)

    def test_portfolio_drift_ops_summary_links_to_exceptions_filter(self) -> None:
        as_of_date = "2026-02-28"
        benchmark_version = "phase2.pr12.v1"
        benchmark_ref = self._write_drift_metrics_ref(
            file_name="ui-benchmark-drift-ops.json",
            benchmark_version=benchmark_version,
            median_manual_fields_per_intake=1.0,
        )
        live_ref = self._write_drift_metrics_ref(
            file_name="ui-live-drift-ops.json",
            benchmark_version=benchmark_version,
            median_manual_fields_per_intake=2.5,
        )
        self.orchestrator.phase2_drift_triage(
            as_of_date=as_of_date,
            lookback_window_days=30,
            benchmark_version=benchmark_version,
            out_dir=self.temp_dir / "drift-ops-triage",
            benchmark_metrics_ref=benchmark_ref,
            live_metrics_ref=live_ref,
        )
        try:
            port = _pick_free_port()
        except PermissionError:
            self.skipTest("socket bind not permitted in current sandbox")
        proc = _start_ui_server(repo_root=self.repo_root, root=self.temp_dir, port=port)
        try:
            _wait_for_route(port, "/v2/portfolio")
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/v2/portfolio?as_of={as_of_date}&lookback_window_days=30&benchmark_version={benchmark_version}",
                timeout=3,
            ) as response:
                body = response.read().decode("utf-8")
            self.assertIn("Drift Ops Summary", body)
            self.assertIn("Open Drift Cases", body)
            self.assertIn("case_type=DRIFT_MONITORING", body)
            self.assertNotIn("name='waiver_id'", body)
        finally:
            _stop_process(proc)

    def test_exceptions_filter_by_case_type_drift_monitoring(self) -> None:
        as_of_date = "2026-02-28"
        benchmark_version = "phase2.pr12.v1"
        benchmark_ref = self._write_drift_metrics_ref(
            file_name="ui-benchmark-exc-filter.json",
            benchmark_version=benchmark_version,
            autoplan_zero_edit_common_case_rate=0.95,
        )
        live_ref = self._write_drift_metrics_ref(
            file_name="ui-live-exc-filter.json",
            benchmark_version=benchmark_version,
            autoplan_zero_edit_common_case_rate=0.82,
        )
        self.orchestrator.phase2_drift_triage(
            as_of_date=as_of_date,
            lookback_window_days=30,
            benchmark_version=benchmark_version,
            out_dir=self.temp_dir / "drift-exc-filter",
            benchmark_metrics_ref=benchmark_ref,
            live_metrics_ref=live_ref,
        )
        try:
            port = _pick_free_port()
        except PermissionError:
            self.skipTest("socket bind not permitted in current sandbox")
        proc = _start_ui_server(repo_root=self.repo_root, root=self.temp_dir, port=port)
        try:
            _wait_for_route(port, "/v2/exceptions")
            query = urllib.parse.urlencode(
                {
                    "case_type": "DRIFT_MONITORING",
                    "status": "OPEN",
                    "as_of_date": as_of_date,
                }
            )
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/v2/exceptions?{query}", timeout=3) as response:
                body = response.read().decode("utf-8")
            self.assertIn("filter: case_type=DRIFT_MONITORING; status=OPEN", body)
            self.assertIn("DRIFT_MONITORING", body)
        finally:
            _stop_process(proc)

    def test_portfolio_drift_root_cause_summary_is_read_only(self) -> None:
        as_of_date = "2026-02-28"
        benchmark_version = "phase2.pr12.v1"
        benchmark_ref = self._write_drift_metrics_ref(
            file_name="ui-benchmark-root-cause.json",
            benchmark_version=benchmark_version,
            autoplan_zero_edit_common_case_rate=0.95,
        )
        live_ref = self._write_drift_metrics_ref(
            file_name="ui-live-root-cause.json",
            benchmark_version=benchmark_version,
            autoplan_zero_edit_common_case_rate=0.82,
        )
        drift_report = self.orchestrator.phase2_drift_report(
            as_of_date=as_of_date,
            lookback_window_days=30,
            benchmark_version=benchmark_version,
            out_dir=self.temp_dir / "root-cause-report",
            benchmark_metrics_ref=benchmark_ref,
            live_metrics_ref=live_ref,
            persist=False,
        )
        triage_status = self.orchestrator.phase2_drift_operations_snapshot(
            as_of_date=as_of_date,
            lookback_window_days=30,
            benchmark_version=benchmark_version,
        )
        triage_ref = self.temp_dir / "root-cause-triage-status.json"
        triage_ref.write_text(json.dumps(triage_status, indent=2, sort_keys=True), encoding="utf-8")
        self.orchestrator.phase2_drift_root_cause(
            as_of_date=as_of_date,
            lookback_window_days=30,
            benchmark_version=benchmark_version,
            out_dir=self.temp_dir / "root-cause-export",
            drift_report_ref=Path(str(drift_report["report_json_path"])),
            triage_status_ref=triage_ref,
            persist=True,
        )
        try:
            port = _pick_free_port()
        except PermissionError:
            self.skipTest("socket bind not permitted in current sandbox")
        proc = _start_ui_server(repo_root=self.repo_root, root=self.temp_dir, port=port)
        try:
            _wait_for_route(port, "/v2/portfolio")
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/v2/portfolio?as_of={as_of_date}&lookback_window_days=30&benchmark_version={benchmark_version}",
                timeout=3,
            ) as response:
                body = response.read().decode("utf-8")
            self.assertIn("Drift Root Cause Summary", body)
            self.assertIn("Open Drift Monitoring Exceptions", body)
            self.assertIn("aggregate_reason_code", body)
            self.assertNotIn("name='policy_set_id'", body)
            self.assertNotIn("Resolve Root Cause", body)
        finally:
            _stop_process(proc)

    def test_portfolio_operator_playbooks_panel_is_read_only(self) -> None:
        as_of_date = "2026-02-28"
        benchmark_version = "phase2.pr12.v1"
        benchmark_ref = self._write_drift_metrics_ref(
            file_name="ui-benchmark-playbooks.json",
            benchmark_version=benchmark_version,
            autoplan_zero_edit_common_case_rate=0.95,
        )
        live_ref = self._write_drift_metrics_ref(
            file_name="ui-live-playbooks.json",
            benchmark_version=benchmark_version,
            autoplan_zero_edit_common_case_rate=0.82,
        )
        drift_report = self.orchestrator.phase2_drift_report(
            as_of_date=as_of_date,
            lookback_window_days=30,
            benchmark_version=benchmark_version,
            out_dir=self.temp_dir / "playbooks-drift-report",
            benchmark_metrics_ref=benchmark_ref,
            live_metrics_ref=live_ref,
            persist=False,
        )
        triage_status = self.orchestrator.phase2_drift_operations_snapshot(
            as_of_date=as_of_date,
            lookback_window_days=30,
            benchmark_version=benchmark_version,
        )
        triage_ref = self.temp_dir / "playbooks-triage-status.json"
        triage_ref.write_text(json.dumps(triage_status, indent=2, sort_keys=True), encoding="utf-8")
        root_cause = self.orchestrator.phase2_drift_root_cause(
            as_of_date=as_of_date,
            lookback_window_days=30,
            benchmark_version=benchmark_version,
            out_dir=self.temp_dir / "playbooks-root-cause",
            drift_report_ref=Path(str(drift_report["report_json_path"])),
            triage_status_ref=triage_ref,
            persist=False,
        )
        self.orchestrator.phase2_operator_playbooks(
            as_of_date=as_of_date,
            lookback_window_days=30,
            benchmark_version=benchmark_version,
            out_dir=self.temp_dir / "playbooks-export",
            drift_report_ref=Path(str(drift_report["report_json_path"])),
            triage_status_ref=triage_ref,
            root_cause_report_ref=Path(str(root_cause["report_json_path"])),
            persist=True,
        )
        try:
            port = _pick_free_port()
        except PermissionError:
            self.skipTest("socket bind not permitted in current sandbox")
        proc = _start_ui_server(repo_root=self.repo_root, root=self.temp_dir, port=port)
        try:
            _wait_for_route(port, "/v2/portfolio")
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/v2/portfolio?as_of={as_of_date}&lookback_window_days=30&benchmark_version={benchmark_version}",
                timeout=3,
            ) as response:
                body = response.read().decode("utf-8")
            self.assertIn("Operator Playbooks", body)
            self.assertIn("PB_PLANNING_POLICY_REVIEW", body)
            self.assertIn("case_type=DRIFT_MONITORING", body)
            self.assertNotIn("Apply Playbook", body)
            self.assertNotIn("Execute Playbook", body)
            self.assertNotIn("name='playbook_code'", body)
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
