from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from adapters.sqlite_repo import SQLiteRepo
from apps.cli import build_parser
from core.config import RuntimeConfig
from domain.automation import AutomationOrchestrator
from domain.services import Phase1Service


class Phase2PR2AutonomyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.temp_dir = Path(tempfile.mkdtemp(prefix="phase2-pr2-tests-"))
        shutil.copytree(self.repo_root / "config", self.temp_dir / "config")
        (self.temp_dir / ".state").mkdir(parents=True, exist_ok=True)
        (self.temp_dir / "output_v2").mkdir(parents=True, exist_ok=True)
        self.config = RuntimeConfig.load(self.temp_dir)
        self.repo = SQLiteRepo(self.config.state_dir / "drep.sqlite")
        self.phase1 = Phase1Service(self.config, self.repo)
        self.phase1.init_db()
        self.orchestrator = AutomationOrchestrator(self.config, self.repo, self.phase1)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_contract(self, lpo_no: str = "PR2-LPO-001") -> str:
        payload = {
            "contract_ref": lpo_no,
            "lpo_no": lpo_no,
            "lpo_date": "2026-02-23",
            "buyer_id": "buyer_nycil",
            "vendor_of_record_id": "ananta_flows",
            "operator_id": "guildgate",
            "source_id": "ananta_flows",
            "processor_id": "processor_partner_refinery",
            "currency": "NGN",
            "issue_date": "2026-02-23",
            "lpo_valid_from": "2026-02-23",
            "lpo_valid_to": "2026-03-05",
            "due_date": "2026-03-09",
            "expected_total_qty": 30000.0,
            "lines": [
                {
                    "product_code": "RBDSO",
                    "description": "PR2 automation test line",
                    "expected_qty": 30000.0,
                    "unit": "kgs",
                    "unit_price": 2270.0,
                }
            ],
        }
        result = self.phase1.create_contract(payload, allow_placeholder_tin=True)
        return str(result["contract_id"])

    def test_run_autonomy_writes_gates_intents_executions_and_events(self) -> None:
        contract_id = self._create_contract("PR2-LPO-001")
        proof = self.temp_dir / "proof_evidence.txt"
        proof.write_text("evidence", encoding="utf-8")
        self.phase1.capture_evidence_original(contract_id=contract_id, source_path=proof)

        run = self.orchestrator.run_autonomy(as_of_date="2026-02-23", contract_id=contract_id, dry_run=True)
        self.assertTrue(run["ok"])
        self.assertEqual(1, run["summary"]["contracts_processed"])

        gates = self.repo.fetch_all(
            "SELECT * FROM gate_evaluations WHERE autonomy_run_id = ?",
            (run["autonomy_run_id"],),
        )
        self.assertEqual(7, len(gates))

        intents = self.repo.fetch_all(
            "SELECT * FROM action_intents WHERE contract_id = ? AND as_of_date = ?",
            (contract_id, "2026-02-23"),
        )
        self.assertEqual(5, len(intents))

        executions = self.repo.fetch_all(
            """
            SELECT ae.* FROM action_executions ae
            JOIN action_intents ai ON ai.action_intent_id = ae.action_intent_id
            WHERE ai.contract_id = ? AND ai.as_of_date = ?
            """,
            (contract_id, "2026-02-23"),
        )
        self.assertEqual(5, len(executions))

        events = self.repo.fetch_all(
            "SELECT event_type FROM event_log WHERE entity_type = 'AUTONOMY_RUN' AND entity_id = ?",
            (run["autonomy_run_id"],),
        )
        event_types = {row["event_type"] for row in events}
        self.assertIn("RUN_STARTED", event_types)
        self.assertIn("RUN_COMPLETED", event_types)

    def test_intent_idempotency_replay_keeps_single_intent_set(self) -> None:
        contract_id = self._create_contract("PR2-LPO-REPLAY")
        self.orchestrator.run_autonomy(as_of_date="2026-02-23", contract_id=contract_id, dry_run=True)
        self.orchestrator.run_autonomy(as_of_date="2026-02-23", contract_id=contract_id, dry_run=True)

        intents = self.repo.fetch_all(
            "SELECT * FROM action_intents WHERE contract_id = ? AND as_of_date = ?",
            (contract_id, "2026-02-23"),
        )
        self.assertEqual(5, len(intents))
        keys = {row["idempotency_key"] for row in intents}
        self.assertEqual(5, len(keys))

        executions = self.repo.fetch_all(
            """
            SELECT ae.* FROM action_executions ae
            JOIN action_intents ai ON ai.action_intent_id = ae.action_intent_id
            WHERE ai.contract_id = ? AND ai.as_of_date = ?
            """,
            (contract_id, "2026-02-23"),
        )
        self.assertEqual(5, len(executions))

    def test_gate_failure_creates_case_and_list_cases(self) -> None:
        contract_id = self._create_contract("PR2-LPO-CANCELLED")
        self.phase1.cancel_contract(contract_id=contract_id, reason="Test cancel")
        run = self.orchestrator.run_autonomy(as_of_date="2026-02-23", contract_id=contract_id, dry_run=True)
        self.assertTrue(run["ok"])

        cases = self.orchestrator.list_cases(status="OPEN")
        rows = [row for row in cases["cases"] if row.get("contract_id") == contract_id]
        self.assertTrue(rows)
        self.assertTrue(any(row["reason_code"] == "gate_failed" for row in rows))

    def test_decide_case_resolve_and_resume(self) -> None:
        contract_id = self._create_contract("PR2-LPO-DECIDE")
        self.phase1.cancel_contract(contract_id=contract_id, reason="Test cancel")
        self.orchestrator.run_autonomy(as_of_date="2026-02-23", contract_id=contract_id, dry_run=True)
        open_cases = self.repo.list_exception_cases(status="OPEN")
        target = next(row for row in open_cases if row["contract_id"] == contract_id)

        decision = self.orchestrator.decide_case(
            case_id=str(target["exception_case_id"]),
            decision="APPROVE",
            reason="Override for test",
            resume=True,
            dry_run_resume=True,
        )
        self.assertTrue(decision["ok"])
        self.assertEqual("RESOLVED", decision["case"]["status"])
        self.assertEqual("APPROVE", decision["decision"]["decision"])
        self.assertIsNotNone(decision["resume_result"])

    def test_autonomy_metrics_writes_json(self) -> None:
        contract_id = self._create_contract("PR2-LPO-METRICS")
        self.orchestrator.run_autonomy(as_of_date="2026-02-23", contract_id=contract_id, dry_run=True)
        out_dir = self.temp_dir / "metrics"
        result = self.orchestrator.autonomy_metrics(
            as_of_date="2026-02-23",
            out_dir=out_dir,
            lookback_window_days=30,
            benchmark_version="phase2.pr7.v1",
        )
        self.assertTrue(result["ok"])
        metrics_path = Path(result["metrics_path"])
        self.assertTrue(metrics_path.exists())
        payload = json.loads(metrics_path.read_text(encoding="utf-8"))
        self.assertEqual("2026-02-23", payload["as_of_date"])
        self.assertEqual(30, payload["lookback_window_days"])
        self.assertEqual("phase2.pr7.v1", payload["benchmark_version"])
        self.assertIn("touchless_rate", payload)
        self.assertIn("manual_interactions_in_exceptions_rate", payload)

    def test_cli_parser_supports_phase2_pr2_commands(self) -> None:
        parser = build_parser()
        parsed = parser.parse_args(["run-autonomy", "--as-of", "2026-03-31", "--dry-run"])
        self.assertEqual("run-autonomy", parsed.command)
        parsed = parser.parse_args(["list-cases", "--status", "OPEN"])
        self.assertEqual("list-cases", parsed.command)
        parsed = parser.parse_args(["decide-case", "--case-id", "c1", "--decision", "APPROVE", "--reason", "ok"])
        self.assertEqual("decide-case", parsed.command)
        parsed = parser.parse_args(["autonomy-metrics", "--as-of", "2026-03-31"])
        self.assertEqual("autonomy-metrics", parsed.command)
        self.assertEqual(30, parsed.lookback_window_days)
        self.assertEqual("phase2.pr10.v1", parsed.benchmark_version)
        parsed = parser.parse_args(
            [
                "phase2-drift-report",
                "--as-of",
                "2026-03-31",
                "--benchmark-version",
                "phase2.pr12.v1",
                "--out-dir",
                "/tmp/phase2-drift",
            ]
        )
        self.assertEqual("phase2-drift-report", parsed.command)
        self.assertEqual(30, parsed.lookback_window_days)

    def test_non_dry_run_autonomy_executes_intents_with_evidence_present(self) -> None:
        contract_id = self._create_contract("PR2-LPO-NONDRY")
        evidence = self.temp_dir / "evidence_non_dry.txt"
        evidence.write_text("proof", encoding="utf-8")
        self.phase1.capture_evidence_original(contract_id=contract_id, source_path=evidence)

        run = self.orchestrator.run_autonomy(
            as_of_date="2026-02-23",
            contract_id=contract_id,
            dry_run=False,
        )
        self.assertTrue(run["ok"])
        self.assertGreaterEqual(int(run["summary"]["intents_executed"]), 1)

        deliveries = self.repo.fetch_all(
            "SELECT * FROM deliveries WHERE contract_id = ?",
            (contract_id,),
        )
        self.assertGreaterEqual(len(deliveries), 1)
        linked = self.repo.fetch_all(
            """
            SELECT l.link_id
            FROM delivery_coa_links l
            JOIN deliveries d ON d.delivery_id = l.delivery_id
            WHERE d.contract_id = ?
            """,
            (contract_id,),
        )
        self.assertGreaterEqual(len(linked), 1)
        executions = self.repo.fetch_all(
            """
            SELECT ae.status
            FROM action_executions ae
            JOIN action_intents ai ON ai.action_intent_id = ae.action_intent_id
            WHERE ai.contract_id = ? AND ai.as_of_date = ?
            """,
            (contract_id, "2026-02-23"),
        )
        self.assertTrue(any(str(row["status"]).upper() == "SUCCESS" for row in executions))


if __name__ == "__main__":
    unittest.main()
