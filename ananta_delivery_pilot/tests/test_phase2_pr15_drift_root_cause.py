from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from adapters.sqlite_repo import SQLiteRepo
from core.config import RuntimeConfig
from domain.automation import AutomationOrchestrator
from domain.services import Phase1Service


class Phase2Pr15DriftRootCauseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.temp_dir = Path(tempfile.mkdtemp(prefix="phase2-pr15-tests-"))
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

    def _write_drift_report(
        self,
        *,
        file_name: str,
        benchmark_version: str,
        gates: list[dict[str, object]],
    ) -> Path:
        payload = {
            "inputs": {
                "as_of_date": "2026-02-28",
                "lookback_window_days": 30,
                "benchmark_version": benchmark_version,
                "generated_at_utc": "2026-02-28T12:00:00Z",
                "benchmark_metrics_ref": "fixture",
                "live_metrics_ref": "fixture",
            },
            "gates": gates,
            "aggregate": {
                "drift_state": "ALERT",
                "recommendation": "BLOCK_PROMOTION",
                "blocking_reasons": [],
            },
        }
        path = self.temp_dir / file_name
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return path

    def _write_triage_status(self, *, file_name: str) -> Path:
        payload = {
            "as_of_date": "2026-02-28",
            "lookback_window_days": 30,
            "benchmark_version": "phase2.pr12.v1",
            "open_cases_total": 0,
            "open_cases_by_severity": {},
            "open_cases_by_gate": {},
            "open_cases_by_reason": {},
            "open_cases": [],
            "latest_triage_at_utc": "2026-02-28T12:15:00Z",
            "drift_state": "PASS",
            "recommendation": "NO_ACTION",
            "latest_report_json_path": "",
            "latest_report_md_path": "",
        }
        path = self.temp_dir / file_name
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return path

    def _create_drift_case(
        self,
        *,
        contract_id: str,
        gate_name: str,
        as_of_date: str,
        reason_code: str,
        comparisons: list[dict[str, object]],
        case_suffix: str,
    ) -> None:
        details = {
            "as_of_date": as_of_date,
            "lookback_window_days": 30,
            "benchmark_version": "phase2.pr12.v1",
            "generated_at_utc": f"{as_of_date}T12:00:00Z",
            "gate_name": gate_name,
            "drift_state": "ALERT",
            "reason_code": reason_code,
            "comparisons": comparisons,
            "report_json_path": "",
            "report_md_path": "",
            "blocking_reasons": [f"{gate_name}:{reason_code}"],
        }
        with self.repo.transaction() as conn:
            self.repo.create_or_get_exception_case(
                conn,
                autonomy_run_id="",
                action_intent_id=None,
                contract_id=contract_id,
                delivery_id=None,
                planned_delivery_id=None,
                case_type="DRIFT_MONITORING",
                severity="BLOCKER",
                reason_code=reason_code,
                details=details,
                idempotency_key=f"pr15-test::{gate_name}::{as_of_date}::{case_suffix}",
            )

    def _create_contract(self, lpo_no: str) -> str:
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
            "lpo_valid_to": "2026-03-31",
            "expected_total_qty": 30000.0,
            "lines": [
                {
                    "product_code": "RBDSO",
                    "description": "PR15 drift root-cause line",
                    "expected_qty": 30000.0,
                    "unit": "kgs",
                    "unit_price": 2270.0,
                }
            ],
        }
        result = self.service.create_contract(payload, allow_placeholder_tin=True)
        return str(result["contract_id"])

    def test_root_cause_report_is_deterministic_for_same_inputs(self) -> None:
        gates = [
            {
                "gate_name": "pr8",
                "drift_state": "ALERT",
                "reason_code": "drift_exceeds_threshold",
                "comparisons": [
                    {
                        "metric_name": "autoplan_zero_edit_common_case_rate",
                        "comparison_state": "alert",
                        "delta": -0.2,
                    }
                ],
            }
        ]
        drift_report_ref = self._write_drift_report(
            file_name="deterministic_drift_report.json",
            benchmark_version="phase2.pr12.v1",
            gates=gates,
        )
        triage_status_ref = self._write_triage_status(file_name="deterministic_triage_status.json")
        first = self.orchestrator.phase2_drift_root_cause(
            as_of_date="2026-02-28",
            lookback_window_days=30,
            benchmark_version="phase2.pr12.v1",
            out_dir=self.temp_dir / "root-cause-a",
            drift_report_ref=drift_report_ref,
            triage_status_ref=triage_status_ref,
            persist=False,
        )
        second = self.orchestrator.phase2_drift_root_cause(
            as_of_date="2026-02-28",
            lookback_window_days=30,
            benchmark_version="phase2.pr12.v1",
            out_dir=self.temp_dir / "root-cause-b",
            drift_report_ref=drift_report_ref,
            triage_status_ref=triage_status_ref,
            persist=False,
        )
        self.assertEqual(first["aggregate_reason_code"], second["aggregate_reason_code"])
        self.assertEqual(first["top_recurring_causes"], second["top_recurring_causes"])
        self.assertEqual(
            first["root_cause_report"]["gates"][0]["diagnosed_causes"][0]["root_cause_code"],
            second["root_cause_report"]["gates"][0]["diagnosed_causes"][0]["root_cause_code"],
        )

    def test_missing_telemetry_yields_insufficient_observability_data(self) -> None:
        drift_report_ref = self.temp_dir / "missing_telemetry_report.json"
        drift_report_ref.write_text(
            json.dumps(
                {
                    "inputs": {
                        "as_of_date": "2026-02-28",
                        "lookback_window_days": 30,
                        "benchmark_version": "phase2.pr12.v1",
                    },
                    "gates": [],
                    "aggregate": {},
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        triage_status_ref = self._write_triage_status(file_name="missing_telemetry_triage.json")
        result = self.orchestrator.phase2_drift_root_cause(
            as_of_date="2026-02-28",
            lookback_window_days=30,
            benchmark_version="phase2.pr12.v1",
            out_dir=self.temp_dir / "root-cause-missing",
            drift_report_ref=drift_report_ref,
            triage_status_ref=triage_status_ref,
            persist=False,
        )
        self.assertEqual("insufficient_observability_data", result["aggregate_reason_code"])
        self.assertEqual("INSUFFICIENT_DATA", result["aggregate_state"])

    def test_benchmark_mismatch_yields_benchmark_dataset_misalignment(self) -> None:
        drift_report_ref = self._write_drift_report(
            file_name="mismatch_drift_report.json",
            benchmark_version="phase2.pr12.v0",
            gates=[
                {
                    "gate_name": "pr8",
                    "drift_state": "MISMATCH",
                    "reason_code": "benchmark_version_mismatch",
                    "comparisons": [],
                }
            ],
        )
        triage_status_ref = self._write_triage_status(file_name="mismatch_triage_status.json")
        result = self.orchestrator.phase2_drift_root_cause(
            as_of_date="2026-02-28",
            lookback_window_days=30,
            benchmark_version="phase2.pr12.v1",
            out_dir=self.temp_dir / "root-cause-mismatch",
            drift_report_ref=drift_report_ref,
            triage_status_ref=triage_status_ref,
            persist=False,
        )
        self.assertEqual("benchmark_dataset_misalignment", result["aggregate_reason_code"])

    def test_recurrence_and_tiebreak_are_deterministic(self) -> None:
        contract_a = self._create_contract("PR15-CTR-A")
        contract_b = self._create_contract("PR15-CTR-B")
        comparisons_pr8 = [{"metric_name": "autoplan_zero_edit_common_case_rate", "comparison_state": "alert", "delta": -0.2}]
        comparisons_pr9 = [{"metric_name": "manual_transport_fields_per_delivery", "comparison_state": "alert", "delta": 0.8}]
        for day in ("2026-02-26", "2026-02-27", "2026-02-28"):
            self._create_drift_case(
                contract_id=contract_a,
                gate_name="pr8",
                as_of_date=day,
                reason_code="drift_exceeds_threshold",
                comparisons=comparisons_pr8,
                case_suffix=f"pr8-{day}",
            )
            self._create_drift_case(
                contract_id=contract_b,
                gate_name="pr9",
                as_of_date=day,
                reason_code="drift_exceeds_threshold",
                comparisons=comparisons_pr9,
                case_suffix=f"pr9-{day}",
            )
        drift_report_ref = self._write_drift_report(
            file_name="recurrence_drift_report.json",
            benchmark_version="phase2.pr12.v1",
            gates=[
                {
                    "gate_name": "pr8",
                    "drift_state": "ALERT",
                    "reason_code": "drift_exceeds_threshold",
                    "comparisons": comparisons_pr8,
                },
                {
                    "gate_name": "pr9",
                    "drift_state": "ALERT",
                    "reason_code": "drift_exceeds_threshold",
                    "comparisons": comparisons_pr9,
                },
            ],
        )
        triage_status_ref = self._write_triage_status(file_name="recurrence_triage_status.json")
        result = self.orchestrator.phase2_drift_root_cause(
            as_of_date="2026-02-28",
            lookback_window_days=30,
            benchmark_version="phase2.pr12.v1",
            out_dir=self.temp_dir / "root-cause-recurrence",
            drift_report_ref=drift_report_ref,
            triage_status_ref=triage_status_ref,
            persist=False,
        )
        top = result["top_recurring_causes"]
        self.assertGreaterEqual(len(top), 2)
        self.assertEqual("planning_policy_mismatch", top[0]["root_cause_code"])
        self.assertEqual("transport_assignment_instability", top[1]["root_cause_code"])
        gate_rows = result["root_cause_report"]["gates"]
        self.assertTrue(gate_rows[0]["diagnosed_causes"][0]["recurring"])
        self.assertTrue(gate_rows[1]["diagnosed_causes"][0]["recurring"])

    def test_root_cause_export_appends_audit_event(self) -> None:
        drift_report_ref = self._write_drift_report(
            file_name="event_drift_report.json",
            benchmark_version="phase2.pr12.v1",
            gates=[
                {
                    "gate_name": "pr10",
                    "drift_state": "WATCH",
                    "reason_code": "drift_within_watch_band",
                    "comparisons": [
                        {
                            "metric_name": "payment_suggestion_acceptance_rate",
                            "comparison_state": "watch",
                            "delta": -0.06,
                        }
                    ],
                }
            ],
        )
        triage_status_ref = self._write_triage_status(file_name="event_triage_status.json")
        result = self.orchestrator.phase2_drift_root_cause(
            as_of_date="2026-02-28",
            lookback_window_days=30,
            benchmark_version="phase2.pr12.v1",
            out_dir=self.temp_dir / "root-cause-event",
            drift_report_ref=drift_report_ref,
            triage_status_ref=triage_status_ref,
            persist=True,
        )
        self.assertTrue(result["ok"])
        event_row = self.repo.fetch_one(
            """
            SELECT event_type, payload_json
            FROM event_log
            WHERE event_type = 'PHASE2_DRIFT_ROOT_CAUSE_EXPORTED'
            ORDER BY created_at DESC
            LIMIT 1
            """
        )
        self.assertIsNotNone(event_row)
        payload = json.loads(str(event_row.get("payload_json") or "{}"))
        self.assertEqual("2026-02-28", payload.get("as_of_date"))
        self.assertEqual("phase2.pr12.v1", payload.get("benchmark_version"))


if __name__ == "__main__":
    unittest.main()
