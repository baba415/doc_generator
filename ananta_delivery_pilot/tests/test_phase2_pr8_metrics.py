from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from adapters.sqlite_repo import SQLiteRepo
from core.config import RuntimeConfig
from core.ids import new_ulid
from domain.automation import AutomationOrchestrator
from domain.services import Phase1Service


class Phase2Pr8MetricsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.temp_dir = Path(tempfile.mkdtemp(prefix="phase2-pr8-metrics-"))
        shutil.copytree(self.repo_root / "config", self.temp_dir / "config")
        (self.temp_dir / ".state").mkdir(parents=True, exist_ok=True)
        (self.temp_dir / "output_v2").mkdir(parents=True, exist_ok=True)
        self.config = RuntimeConfig.load(self.temp_dir)
        self.repo = SQLiteRepo(self.config.state_dir / "drep.sqlite")
        self.service = Phase1Service(self.config, self.repo)
        self.service.init_db()
        self.orchestrator = AutomationOrchestrator(self.config, self.repo, self.service)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_contract_with_plan(self, *, lpo_no: str, issue_date: str) -> str:
        contract = self.service.create_contract(
            {
                "contract_ref": lpo_no,
                "lpo_no": lpo_no,
                "lpo_date": issue_date,
                "buyer_id": "buyer_nycil",
                "vendor_of_record_id": "ananta_flows",
                "operator_id": "guildgate",
                "source_id": "ananta_flows",
                "processor_id": "processor_partner_refinery",
                "currency": "NGN",
                "issue_date": issue_date,
                "lpo_valid_from": issue_date,
                "lpo_valid_to": "2026-03-31",
                "expected_total_qty_kg": 150000,
                "expected_total_qty": 150.0,
                "expected_total_value": 340500000.0,
                "unit_price_basis": "KG",
                "lines": [
                    {
                        "product_code": "RBDPO",
                        "description": "PR8 metrics contract",
                        "expected_qty": 150000,
                        "unit": "kgs",
                        "unit_price": 2270.0,
                        "unit_price_basis": "KG",
                        "expected_value": 340500000.0,
                    }
                ],
            },
            allow_placeholder_tin=True,
        )
        contract_id = str(contract["contract_id"])
        self.service.plan_deliveries(
            contract_id=contract_id,
            start_date=issue_date,
            cadence="daily",
            max_lots_per_day=1,
        )
        return contract_id

    def _add_intake_decisions(
        self,
        *,
        as_of_date: str,
        parser_counts: dict[str, int],
        corrected_count: int,
        confirmed_count: int,
    ) -> None:
        run_id = new_ulid()
        with self.repo.transaction() as conn:
            self.repo.create_automation_run(
                conn,
                run_id=run_id,
                idempotency_key=f"pr8-metrics::{run_id}",
                as_of_date=as_of_date,
                dry_run=True,
                input_payload={"source": "web_v2_intake", "stage": "parse"},
            )
            for decision, count in parser_counts.items():
                for index in range(int(count)):
                    self.repo.add_automation_decision(
                        conn,
                        run_id=run_id,
                        stage="intake_parser",
                        field_name=f"field_{decision}_{index}",
                        required_flag=True,
                        proposed_value="value",
                        source_type="parser",
                        source_ref="fixture",
                        confidence=0.9,
                        decision=decision,
                        reason_code=f"fixture_{decision}",
                        rule_path="fixture.intake_parser",
                    )
            for index in range(int(corrected_count)):
                self.repo.add_automation_decision(
                    conn,
                    run_id=run_id,
                    stage="intake_confirm",
                    field_name=f"corrected_{index}",
                    required_flag=True,
                    proposed_value="value",
                    source_type="user_input",
                    source_ref="/v2/intake/confirm",
                    confidence=1.0,
                    decision="user_corrected",
                    reason_code="user_corrected",
                    rule_path="fixture.intake_confirm",
                )
            for index in range(int(confirmed_count)):
                self.repo.add_automation_decision(
                    conn,
                    run_id=run_id,
                    stage="intake_confirm",
                    field_name=f"confirmed_{index}",
                    required_flag=True,
                    proposed_value="value",
                    source_type="user_input",
                    source_ref="/v2/intake/confirm",
                    confidence=1.0,
                    decision="user_confirmed",
                    reason_code="user_confirmed",
                    rule_path="fixture.intake_confirm",
                )
            conn.execute(
                "UPDATE automation_runs SET status='COMPLETED', updated_at='2026-02-23T09:00:00Z' WHERE run_id = ?",
                (run_id,),
            )

    def test_pr8_metrics_are_computed_from_db_events(self) -> None:
        contract_auto = self._create_contract_with_plan(lpo_no="LPO-PR8-METRICS-001", issue_date="2026-02-20")
        contract_edited = self._create_contract_with_plan(lpo_no="LPO-PR8-METRICS-002", issue_date="2026-02-21")

        rows = self.repo.fetch_all(
            """
            SELECT planned_delivery_id
            FROM planned_deliveries
            WHERE contract_id = ?
            ORDER BY sequence_no ASC
            """,
            (contract_edited,),
        )
        self.assertTrue(rows)
        self.service.update_planned_delivery(
            planned_delivery_id=str(rows[0]["planned_delivery_id"]),
            planned_qty_mt=30.0,
            notes="web_v2_edit",
        )

        self._add_intake_decisions(
            as_of_date="2026-02-20",
            parser_counts={"auto_applied": 3, "needs_review": 1, "blocked": 1},
            corrected_count=2,
            confirmed_count=1,
        )
        self._add_intake_decisions(
            as_of_date="2026-02-21",
            parser_counts={"auto_applied": 2, "needs_review": 1, "blocked": 0},
            corrected_count=0,
            confirmed_count=2,
        )

        out_dir = self.temp_dir / "metrics"
        result = self.orchestrator.autonomy_metrics(
            as_of_date="2026-02-23",
            out_dir=out_dir,
            lookback_window_days=30,
            benchmark_version="phase2.pr8.v1",
        )
        self.assertTrue(result["ok"])
        metrics = result["metrics"]
        self.assertEqual(1.0, metrics["median_manual_fields_per_intake"])
        self.assertEqual(2, metrics["autoplan_common_case_total"])
        self.assertEqual(1, metrics["autoplan_zero_edit_common_case_count"])
        self.assertEqual(0.5, metrics["autoplan_zero_edit_common_case_rate"])
        self.assertEqual(
            {"auto_applied": 5, "needs_review": 2, "blocked": 1},
            metrics["intake_decision_distribution"],
        )
        self.assertFalse(metrics["autoplan_zero_edit_common_case_gate_pass"])
        self.assertFalse(metrics["pr8_gate_pass"])
        self.assertEqual("autoplan_zero_edit_common_case_failed", metrics["pr8_gate_reason_code"])
        self.assertEqual("phase2.pr8.v1", metrics["benchmark_version_expected_pr8"])
        self.assertTrue(metrics["benchmark_version_match_pr8"])
        self.assertNotEqual(contract_auto, contract_edited)
        payload = json.loads(Path(result["metrics_path"]).read_text(encoding="utf-8"))
        self.assertEqual(metrics["generated_at_utc"], payload["generated_at_utc"])

    def test_pr8_metrics_benchmark_version_mismatch_blocks_gate(self) -> None:
        self._create_contract_with_plan(lpo_no="LPO-PR8-MISMATCH-001", issue_date="2026-02-20")
        self._add_intake_decisions(
            as_of_date="2026-02-20",
            parser_counts={"auto_applied": 1, "needs_review": 0, "blocked": 0},
            corrected_count=0,
            confirmed_count=1,
        )
        result = self.orchestrator.autonomy_metrics(
            as_of_date="2026-02-23",
            out_dir=self.temp_dir / "metrics-mismatch",
            lookback_window_days=30,
            benchmark_version="phase2.pr7.v1",
        )
        metrics = result["metrics"]
        self.assertFalse(metrics["benchmark_version_match_pr8"])
        self.assertFalse(metrics["pr8_gate_pass"])
        self.assertEqual("benchmark_version_mismatch", metrics["pr8_gate_reason_code"])

    def test_pr8_metrics_generated_timestamp_uses_utc_canon(self) -> None:
        result = self.orchestrator.autonomy_metrics(
            as_of_date="2026-02-23",
            out_dir=self.temp_dir / "metrics-utc",
            lookback_window_days=30,
            benchmark_version="phase2.pr8.v1",
        )
        metrics = result["metrics"]
        generated = str(metrics["generated_at_utc"])
        self.assertTrue(generated.endswith("Z"))
        self.assertRegex(generated, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertFalse(metrics["pr8_gate_pass"])
        self.assertEqual("insufficient_intake_data", metrics["pr8_gate_reason_code"])


if __name__ == "__main__":
    unittest.main()
