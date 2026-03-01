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


class Phase2Pr14DriftOperationsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.temp_dir = Path(tempfile.mkdtemp(prefix="phase2-pr14-tests-"))
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

    def _write_metrics_ref(
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

    def test_drift_triage_opens_drift_monitoring_cases_for_alert(self) -> None:
        benchmark_ref = self._write_metrics_ref(
            file_name="benchmark-alert.json",
            benchmark_version="phase2.pr12.v1",
            median_manual_fields_per_intake=1.0,
        )
        live_ref = self._write_metrics_ref(
            file_name="live-alert.json",
            benchmark_version="phase2.pr12.v1",
            median_manual_fields_per_intake=2.4,
        )
        result = self.orchestrator.phase2_drift_triage(
            as_of_date="2026-02-28",
            lookback_window_days=30,
            benchmark_version="phase2.pr12.v1",
            out_dir=self.temp_dir / "triage-alert",
            benchmark_metrics_ref=benchmark_ref,
            live_metrics_ref=live_ref,
        )
        self.assertTrue(result["ok"])
        self.assertGreaterEqual(int(result["cases_opened"]), 1)
        open_cases = self.repo.list_exception_cases(status="OPEN", case_type="DRIFT_MONITORING")
        self.assertTrue(open_cases)
        self.assertTrue(any(str(row.get("reason_code") or "") == "drift_exceeds_threshold" for row in open_cases))

    def test_drift_triage_replay_does_not_duplicate_cases(self) -> None:
        benchmark_ref = self._write_metrics_ref(
            file_name="benchmark-replay.json",
            benchmark_version="phase2.pr12.v1",
            manual_transport_fields_per_delivery=0.6,
        )
        live_ref = self._write_metrics_ref(
            file_name="live-replay.json",
            benchmark_version="phase2.pr12.v1",
            manual_transport_fields_per_delivery=1.7,
        )
        first = self.orchestrator.phase2_drift_triage(
            as_of_date="2026-02-28",
            lookback_window_days=30,
            benchmark_version="phase2.pr12.v1",
            out_dir=self.temp_dir / "triage-replay-a",
            benchmark_metrics_ref=benchmark_ref,
            live_metrics_ref=live_ref,
        )
        second = self.orchestrator.phase2_drift_triage(
            as_of_date="2026-02-28",
            lookback_window_days=30,
            benchmark_version="phase2.pr12.v1",
            out_dir=self.temp_dir / "triage-replay-b",
            benchmark_metrics_ref=benchmark_ref,
            live_metrics_ref=live_ref,
        )
        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        open_cases = self.repo.list_exception_cases(status="OPEN", case_type="DRIFT_MONITORING")
        self.assertEqual(len({str(row.get("idempotency_key") or "") for row in open_cases}), len(open_cases))

    def test_drift_triage_pass_auto_resolves_prior_open_cases(self) -> None:
        benchmark_alert_ref = self._write_metrics_ref(
            file_name="benchmark-resolve-alert.json",
            benchmark_version="phase2.pr12.v1",
            autoplan_zero_edit_common_case_rate=0.95,
        )
        live_alert_ref = self._write_metrics_ref(
            file_name="live-resolve-alert.json",
            benchmark_version="phase2.pr12.v1",
            autoplan_zero_edit_common_case_rate=0.80,
        )
        self.orchestrator.phase2_drift_triage(
            as_of_date="2026-02-28",
            lookback_window_days=30,
            benchmark_version="phase2.pr12.v1",
            out_dir=self.temp_dir / "triage-resolve-alert",
            benchmark_metrics_ref=benchmark_alert_ref,
            live_metrics_ref=live_alert_ref,
        )
        self.assertTrue(self.repo.list_exception_cases(status="OPEN", case_type="DRIFT_MONITORING"))

        benchmark_pass_ref = self._write_metrics_ref(
            file_name="benchmark-resolve-pass.json",
            benchmark_version="phase2.pr12.v1",
            autoplan_zero_edit_common_case_rate=0.95,
        )
        live_pass_ref = self._write_metrics_ref(
            file_name="live-resolve-pass.json",
            benchmark_version="phase2.pr12.v1",
            autoplan_zero_edit_common_case_rate=0.95,
        )
        result = self.orchestrator.phase2_drift_triage(
            as_of_date="2026-02-28",
            lookback_window_days=30,
            benchmark_version="phase2.pr12.v1",
            out_dir=self.temp_dir / "triage-resolve-pass",
            benchmark_metrics_ref=benchmark_pass_ref,
            live_metrics_ref=live_pass_ref,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(0, int(result["open_cases_by_severity"].get("BLOCKER", 0)))
        resolved_cases = self.repo.list_exception_cases(status="RESOLVED", case_type="DRIFT_MONITORING")
        self.assertTrue(resolved_cases)
        details = json.loads(str(resolved_cases[0].get("details_json") or "{}"))
        self.assertEqual("drift_cleared", str(details.get("resolution_reason_code") or ""))

    def test_drift_operations_snapshot_returns_read_only_summary(self) -> None:
        benchmark_ref = self._write_metrics_ref(
            file_name="benchmark-snapshot.json",
            benchmark_version="phase2.pr12.v1",
            payment_suggestion_acceptance_rate=0.80,
        )
        live_ref = self._write_metrics_ref(
            file_name="live-snapshot.json",
            benchmark_version="phase2.pr12.v1",
            payment_suggestion_acceptance_rate=0.72,
        )
        self.orchestrator.phase2_drift_triage(
            as_of_date="2026-02-28",
            lookback_window_days=30,
            benchmark_version="phase2.pr12.v1",
            out_dir=self.temp_dir / "triage-snapshot",
            benchmark_metrics_ref=benchmark_ref,
            live_metrics_ref=live_ref,
        )
        snapshot = self.orchestrator.phase2_drift_operations_snapshot(
            as_of_date="2026-02-28",
            lookback_window_days=30,
            benchmark_version="phase2.pr12.v1",
        )
        self.assertIn("open_cases_by_gate", snapshot)
        self.assertIn("open_cases_by_severity", snapshot)
        self.assertIn("latest_triage_at_utc", snapshot)
        self.assertIn("latest_report_md_path", snapshot)
        self.assertEqual("phase2.pr12.v1", snapshot["benchmark_version"])


if __name__ == "__main__":
    unittest.main()
