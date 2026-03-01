from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from adapters.sqlite_repo import SQLiteRepo
from core.config import RuntimeConfig
from core.ids import new_ulid
from domain.automation import AutomationOrchestrator, PR8_BENCHMARK_VERSION
from domain.services import Phase1Service


class Phase2Pr12Pr8GateClosureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.temp_dir = Path(tempfile.mkdtemp(prefix="phase2-pr12-tests-"))
        shutil.copytree(self.repo_root / "config", self.temp_dir / "config")
        (self.temp_dir / ".state").mkdir(parents=True, exist_ok=True)
        (self.temp_dir / "output_v2").mkdir(parents=True, exist_ok=True)
        self.config = RuntimeConfig.load(self.temp_dir)
        self.repo = SQLiteRepo(self.config.state_dir / "drep.sqlite")
        self.service = Phase1Service(self.config, self.repo)
        self.orchestrator = AutomationOrchestrator(self.config, self.repo, self.service)
        self.service.init_db()

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _add_intake_decisions(self, *, as_of_date: str, corrected_count: int) -> None:
        run_id = new_ulid()
        with self.repo.transaction() as conn:
            self.repo.create_automation_run(
                conn,
                run_id=run_id,
                idempotency_key=f"pr12-intake::{run_id}",
                as_of_date=as_of_date,
                dry_run=False,
                input_payload={"source": "test"},
            )
            for index in range(int(corrected_count)):
                self.repo.add_automation_decision(
                    conn,
                    run_id=run_id,
                    stage="intake_confirm",
                    field_name=f"f_{index}",
                    required_flag=True,
                    proposed_value="value",
                    source_type="user_input",
                    source_ref="/v2/intake/confirm",
                    confidence=1.0,
                    decision="user_corrected",
                    reason_code="user_corrected",
                    rule_path="test.intake_confirm",
                )
            conn.execute(
                "UPDATE automation_runs SET status='COMPLETED', updated_at='2026-02-28T11:00:00Z' WHERE run_id = ?",
                (run_id,),
            )

    def _create_contract_and_force_zero_edit_fail(self) -> None:
        contract = self.service.create_contract(
            {
                "contract_ref": "LPO-PR12-REASON-001",
                "lpo_no": "LPO-PR12-REASON-001",
                "lpo_date": "2026-02-20",
                "buyer_id": "buyer_nycil",
                "vendor_of_record_id": "ananta_flows",
                "operator_id": "guildgate",
                "source_id": "ananta_flows",
                "processor_id": "processor_partner_refinery",
                "currency": "NGN",
                "issue_date": "2026-02-20",
                "lpo_valid_from": "2026-02-20",
                "lpo_valid_to": "2026-03-31",
                "expected_total_qty_kg": 150000,
                "expected_total_qty": 150.0,
                "expected_total_value": 340500000.0,
                "unit_price_basis": "KG",
                "lines": [
                    {
                        "product_code": "RBDPO",
                        "description": "PR12 reason precedence",
                        "expected_qty": 150000,
                        "unit": "kgs",
                        "unit_price": 2270.0,
                        "unit_price_basis": "KG",
                    }
                ],
            },
            allow_placeholder_tin=True,
        )
        contract_id = str(contract["contract_id"])
        self.service.plan_deliveries(
            contract_id=contract_id,
            start_date="2026-02-20",
            cadence="daily",
            max_lots_per_day=1,
        )
        row = self.repo.fetch_one(
            """
            SELECT planned_delivery_id
            FROM planned_deliveries
            WHERE contract_id = ?
            ORDER BY sequence_no ASC
            LIMIT 1
            """,
            (contract_id,),
        )
        assert row is not None
        self.service.update_planned_delivery(
            planned_delivery_id=str(row["planned_delivery_id"]),
            notes="web_v2_edit",
        )

    def test_phase2_pr12_benchmark_pr8_gate_passes_without_waiver(self) -> None:
        self.orchestrator.seed_phase2_benchmark(
            as_of_date="2026-02-28",
            benchmark_version=PR8_BENCHMARK_VERSION,
            reset=True,
            lookback_window_days=30,
        )
        report = self.orchestrator.phase2_gate_report(
            as_of_date="2026-02-28",
            lookback_window_days=30,
            benchmark_version=PR8_BENCHMARK_VERSION,
            out_dir=self.temp_dir / "pr12-gate",
        )["gate_report"]
        pr8 = next(g for g in report["gates"] if g["gate_name"] == "pr8")
        self.assertTrue(pr8["pass"])
        self.assertEqual("pass", pr8["reason_code"])
        self.assertEqual("none", pr8["waiver_state"])
        self.assertNotIn("pr8:autoplan_zero_edit_common_case_failed", report["aggregate"]["blocking_reasons"])

    def test_seeded_common_case_plan_is_five_equal_lots_and_zero_edit(self) -> None:
        seed = self.orchestrator.seed_phase2_benchmark(
            as_of_date="2026-02-28",
            benchmark_version=PR8_BENCHMARK_VERSION,
            reset=True,
            lookback_window_days=30,
        )
        contract_id = str(seed["fixture_metadata"]["contract_ids"][0])
        planned_rows = self.repo.fetch_all(
            """
            SELECT planned_qty_kg, notes
            FROM planned_deliveries
            WHERE contract_id = ?
            ORDER BY sequence_no ASC
            """,
            (contract_id,),
        )
        self.assertEqual(5, len(planned_rows))
        self.assertTrue(all(int(row["planned_qty_kg"]) == 30000 for row in planned_rows))
        self.assertTrue(all("web_v2_edit" not in str(row.get("notes") or "").lower() for row in planned_rows))
        pr8 = self.orchestrator._pr8_intake_metrics(
            lookback_start_iso="2026-01-30",
            as_of_date="2026-02-28",
            benchmark_version=PR8_BENCHMARK_VERSION,
        )
        self.assertEqual(1, pr8["autoplan_common_case_total"])
        self.assertEqual(1, pr8["autoplan_zero_edit_common_case_count"])
        self.assertTrue(pr8["pr8_gate_pass"])

    def test_pr8_reason_code_precedence_is_deterministic(self) -> None:
        mismatch = self.orchestrator._pr8_intake_metrics(
            lookback_start_iso="2026-01-30",
            as_of_date="2026-02-28",
            benchmark_version="phase2.pr7.v1",
        )
        self.assertEqual("benchmark_version_mismatch", mismatch["pr8_gate_reason_code"])

        insufficient = self.orchestrator._pr8_intake_metrics(
            lookback_start_iso="2026-01-30",
            as_of_date="2026-02-28",
            benchmark_version=PR8_BENCHMARK_VERSION,
        )
        self.assertEqual("insufficient_intake_data", insufficient["pr8_gate_reason_code"])

        self._create_contract_and_force_zero_edit_fail()
        self._add_intake_decisions(as_of_date="2026-02-28", corrected_count=7)
        median_fail = self.orchestrator._pr8_intake_metrics(
            lookback_start_iso="2026-01-30",
            as_of_date="2026-02-28",
            benchmark_version=PR8_BENCHMARK_VERSION,
        )
        self.assertEqual("median_manual_fields_threshold_failed", median_fail["pr8_gate_reason_code"])
        self.assertFalse(median_fail["pr8_gate_pass"])


if __name__ == "__main__":
    unittest.main()
