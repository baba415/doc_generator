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


class Phase2Pr13DriftMonitoringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.temp_dir = Path(tempfile.mkdtemp(prefix="phase2-pr13-tests-"))
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

    def test_drift_reason_precedence_uses_benchmark_version_mismatch_first(self) -> None:
        benchmark_ref = self._write_metrics_ref(
            file_name="benchmark-mismatch.json",
            benchmark_version="phase2.pr12.v0",
            median_manual_fields_per_intake=None,
        )
        live_ref = self._write_metrics_ref(
            file_name="live-for-mismatch.json",
            benchmark_version="phase2.pr12.v1",
        )
        result = self.orchestrator.phase2_drift_report(
            as_of_date="2026-02-28",
            lookback_window_days=30,
            benchmark_version="phase2.pr12.v1",
            out_dir=self.temp_dir / "drift-mismatch",
            benchmark_metrics_ref=benchmark_ref,
            live_metrics_ref=live_ref,
            persist=False,
        )
        gates = result["drift_report"]["gates"]
        self.assertEqual({"benchmark_version_mismatch"}, {g["reason_code"] for g in gates})
        self.assertEqual("MISMATCH", result["drift_report"]["aggregate"]["drift_state"])
        self.assertEqual("BLOCK_PROMOTION", result["drift_report"]["aggregate"]["recommendation"])

    def test_drift_report_insufficient_benchmark_data_reason(self) -> None:
        benchmark_ref = self._write_metrics_ref(
            file_name="benchmark-insufficient.json",
            benchmark_version="phase2.pr12.v1",
            manual_transport_fields_per_delivery=None,
        )
        live_ref = self._write_metrics_ref(
            file_name="live-insufficient.json",
            benchmark_version="phase2.pr12.v1",
            manual_transport_fields_per_delivery=0.6,
        )
        result = self.orchestrator.phase2_drift_report(
            as_of_date="2026-02-28",
            lookback_window_days=30,
            benchmark_version="phase2.pr12.v1",
            out_dir=self.temp_dir / "drift-insufficient",
            benchmark_metrics_ref=benchmark_ref,
            live_metrics_ref=live_ref,
            persist=False,
        )
        by_gate = {row["gate_name"]: row for row in result["drift_report"]["gates"]}
        self.assertEqual("insufficient_benchmark_data", by_gate["pr9"]["reason_code"])
        self.assertEqual("INSUFFICIENT_DATA", by_gate["pr9"]["drift_state"])
        self.assertEqual("INSUFFICIENT_DATA", result["drift_report"]["aggregate"]["drift_state"])

    def test_drift_report_insufficient_live_data_reason(self) -> None:
        benchmark_ref = self._write_metrics_ref(
            file_name="benchmark-live-missing.json",
            benchmark_version="phase2.pr12.v1",
            manual_transport_fields_per_delivery=0.6,
        )
        live_ref = self._write_metrics_ref(
            file_name="live-missing.json",
            benchmark_version="phase2.pr12.v1",
            manual_transport_fields_per_delivery=None,
        )
        result = self.orchestrator.phase2_drift_report(
            as_of_date="2026-02-28",
            lookback_window_days=30,
            benchmark_version="phase2.pr12.v1",
            out_dir=self.temp_dir / "drift-live-missing",
            benchmark_metrics_ref=benchmark_ref,
            live_metrics_ref=live_ref,
            persist=False,
        )
        by_gate = {row["gate_name"]: row for row in result["drift_report"]["gates"]}
        self.assertEqual("insufficient_live_data", by_gate["pr9"]["reason_code"])
        self.assertEqual("INSUFFICIENT_DATA", by_gate["pr9"]["drift_state"])
        self.assertEqual("INSUFFICIENT_DATA", result["drift_report"]["aggregate"]["drift_state"])

    def test_drift_watch_and_alert_states_are_deterministic(self) -> None:
        benchmark_ref = self._write_metrics_ref(
            file_name="benchmark-watch-alert.json",
            benchmark_version="phase2.pr12.v1",
            median_manual_fields_per_intake=2.0,
            autoplan_zero_edit_common_case_rate=0.95,
            manual_transport_fields_per_delivery=0.6,
            doc_autolink_precision=0.94,
            payment_suggestion_acceptance_rate=0.80,
            auto_action_success_rate=0.92,
        )
        live_ref = self._write_metrics_ref(
            file_name="live-watch-alert.json",
            benchmark_version="phase2.pr12.v1",
            median_manual_fields_per_intake=3.4,
            autoplan_zero_edit_common_case_rate=0.88,
            manual_transport_fields_per_delivery=0.6,
            doc_autolink_precision=0.94,
            payment_suggestion_acceptance_rate=0.74,
            auto_action_success_rate=0.88,
        )
        result = self.orchestrator.phase2_drift_report(
            as_of_date="2026-02-28",
            lookback_window_days=30,
            benchmark_version="phase2.pr12.v1",
            out_dir=self.temp_dir / "drift-watch-alert",
            benchmark_metrics_ref=benchmark_ref,
            live_metrics_ref=live_ref,
            persist=False,
        )
        by_gate = {row["gate_name"]: row for row in result["drift_report"]["gates"]}
        self.assertEqual("drift_exceeds_threshold", by_gate["pr8"]["reason_code"])
        self.assertEqual("ALERT", by_gate["pr8"]["drift_state"])
        self.assertEqual("drift_within_watch_band", by_gate["pr10"]["reason_code"])
        self.assertEqual("WATCH", by_gate["pr10"]["drift_state"])
        self.assertEqual("ALERT", result["drift_report"]["aggregate"]["drift_state"])
        self.assertEqual("BLOCK_PROMOTION", result["drift_report"]["aggregate"]["recommendation"])

    def test_drift_report_uses_default_thresholds_when_config_missing(self) -> None:
        drift_cfg = self.temp_dir / "config" / "drift_thresholds.json"
        drift_cfg.unlink()
        config = RuntimeConfig.load(self.temp_dir)
        repo = SQLiteRepo(config.state_dir / "drep.sqlite")
        service = Phase1Service(config, repo)
        orchestrator = AutomationOrchestrator(config, repo, service)
        benchmark_ref = self._write_metrics_ref(
            file_name="benchmark-default-thresholds.json",
            benchmark_version="phase2.pr12.v1",
            auto_action_success_rate=0.95,
        )
        live_ref = self._write_metrics_ref(
            file_name="live-default-thresholds.json",
            benchmark_version="phase2.pr12.v1",
            auto_action_success_rate=0.89,
        )
        result = orchestrator.phase2_drift_report(
            as_of_date="2026-02-28",
            lookback_window_days=30,
            benchmark_version="phase2.pr12.v1",
            out_dir=self.temp_dir / "drift-default-thresholds",
            benchmark_metrics_ref=benchmark_ref,
            live_metrics_ref=live_ref,
            persist=False,
        )
        self.assertEqual("default", result["threshold_source"])
        self.assertTrue(result["thresholds_path"])
        threshold_payload = json.loads(Path(result["thresholds_path"]).read_text(encoding="utf-8"))
        self.assertEqual("default", threshold_payload["source"])
        by_gate = {row["gate_name"]: row for row in result["drift_report"]["gates"]}
        self.assertEqual("drift_within_watch_band", by_gate["pr10"]["reason_code"])


if __name__ == "__main__":
    unittest.main()
