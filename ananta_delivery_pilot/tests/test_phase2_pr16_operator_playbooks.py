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


class Phase2Pr16OperatorPlaybooksTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.temp_dir = Path(tempfile.mkdtemp(prefix="phase2-pr16-tests-"))
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

    def _write_drift_report(self, *, file_name: str, benchmark_version: str) -> Path:
        payload = {
            "inputs": {
                "as_of_date": "2026-02-28",
                "lookback_window_days": 30,
                "benchmark_version": benchmark_version,
                "generated_at_utc": "2026-02-28T12:00:00Z",
                "benchmark_metrics_ref": "benchmark-metrics.json",
                "live_metrics_ref": "live-metrics.json",
            },
            "gates": [],
            "aggregate": {
                "drift_state": "WATCH",
                "recommendation": "MONITOR",
                "blocking_reasons": [],
            },
        }
        path = self.temp_dir / file_name
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return path

    def _write_triage_status(self, *, file_name: str, benchmark_version: str) -> Path:
        payload = {
            "as_of_date": "2026-02-28",
            "lookback_window_days": 30,
            "benchmark_version": benchmark_version,
            "open_cases_total": 1,
            "open_cases_by_severity": {"REVIEW": 1},
            "open_cases_by_gate": {"pr8": 1},
            "open_cases_by_reason": {"drift_within_watch_band": 1},
            "open_cases": [],
            "latest_triage_at_utc": "2026-02-28T12:15:00Z",
            "drift_state": "WATCH",
            "recommendation": "MONITOR_AND_TRIAGE",
            "latest_report_json_path": "/tmp/triage.json",
            "latest_report_md_path": "/tmp/triage.md",
        }
        path = self.temp_dir / file_name
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return path

    def _write_root_cause_report(
        self,
        *,
        file_name: str,
        benchmark_version: str,
        top_recurring_causes: list[dict[str, object]],
        aggregate_state: str = "WATCH",
        aggregate_reason_code: str = "planning_policy_mismatch",
    ) -> Path:
        payload = {
            "inputs": {
                "as_of_date": "2026-02-28",
                "lookback_window_days": 30,
                "benchmark_version": benchmark_version,
                "generated_at_utc": "2026-02-28T12:30:00Z",
                "drift_report_ref": "/tmp/drift.json",
                "triage_status_ref": "/tmp/triage.json",
            },
            "gates": [],
            "aggregate": {
                "state": aggregate_state,
                "reason_code": aggregate_reason_code,
                "top_recurring_causes": top_recurring_causes,
                "recommended_manual_actions": [],
            },
        }
        path = self.temp_dir / file_name
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return path

    def test_playbook_mapping_and_ranking_are_deterministic(self) -> None:
        benchmark_version = "phase2.pr12.v1"
        drift_ref = self._write_drift_report(file_name="pr16-drift.json", benchmark_version=benchmark_version)
        triage_ref = self._write_triage_status(file_name="pr16-triage.json", benchmark_version=benchmark_version)
        root_ref = self._write_root_cause_report(
            file_name="pr16-root-cause.json",
            benchmark_version=benchmark_version,
            top_recurring_causes=[
                {
                    "root_cause_code": "document_linkage_instability",
                    "occurrence_count": 3,
                    "affected_contracts": 2,
                    "recurring": True,
                    "gates": ["pr9"],
                    "evidence_refs": ["/tmp/doc-linkage-ref-1.json"],
                },
                {
                    "root_cause_code": "settlement_matching_instability",
                    "occurrence_count": 3,
                    "affected_contracts": 2,
                    "recurring": True,
                    "gates": ["pr10"],
                    "evidence_refs": ["/tmp/settlement-ref-1.json"],
                },
            ],
            aggregate_state="WATCH",
            aggregate_reason_code="document_linkage_instability",
        )

        first = self.orchestrator.phase2_operator_playbooks(
            as_of_date="2026-02-28",
            lookback_window_days=30,
            benchmark_version=benchmark_version,
            out_dir=self.temp_dir / "playbooks-a",
            drift_report_ref=drift_ref,
            triage_status_ref=triage_ref,
            root_cause_report_ref=root_ref,
            persist=False,
        )
        second = self.orchestrator.phase2_operator_playbooks(
            as_of_date="2026-02-28",
            lookback_window_days=30,
            benchmark_version=benchmark_version,
            out_dir=self.temp_dir / "playbooks-b",
            drift_report_ref=drift_ref,
            triage_status_ref=triage_ref,
            root_cause_report_ref=root_ref,
            persist=False,
        )

        first_codes = [str(item.get("playbook_code") or "") for item in first["selected_playbooks"]]
        second_codes = [str(item.get("playbook_code") or "") for item in second["selected_playbooks"]]
        self.assertEqual(first_codes, second_codes)
        self.assertEqual(
            ["PB_DOCUMENT_LINKAGE_TUNING", "PB_MANUAL_OVERRIDE_REDUCTION", "PB_SETTLEMENT_MATCHING_REVIEW"],
            first_codes,
        )
        self.assertTrue(all(code.startswith("PB_") for code in first_codes))
        self.assertTrue((self.temp_dir / "playbooks-a" / "phase2_operator_playbooks_2026-02-28.json").exists())
        self.assertTrue((self.temp_dir / "playbooks-a" / "phase2_operator_playbooks_2026-02-28.md").exists())

    def test_missing_source_refs_falls_back_to_observability_recovery(self) -> None:
        result = self.orchestrator.phase2_operator_playbooks(
            as_of_date="2026-02-28",
            lookback_window_days=30,
            benchmark_version="phase2.pr12.v1",
            out_dir=self.temp_dir / "fallback",
            persist=False,
        )
        selected = result.get("selected_playbooks") if isinstance(result.get("selected_playbooks"), list) else []
        self.assertTrue(selected)
        self.assertEqual("PB_OBSERVABILITY_RECOVERY", str(selected[0].get("playbook_code") or ""))
        self.assertEqual("insufficient_observability_data", str(result.get("aggregate_reason_code") or ""))

    def test_default_path_persists_and_uses_triage_status_ref_artifact(self) -> None:
        out_dir = self.temp_dir / "default-path-ref"
        result = self.orchestrator.phase2_operator_playbooks(
            as_of_date="2026-02-28",
            lookback_window_days=30,
            benchmark_version="phase2.pr12.v1",
            out_dir=out_dir,
            persist=False,
        )
        self.assertTrue(result["ok"])
        report_path = Path(str(result["report_json_path"]))
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        inputs = payload.get("inputs") if isinstance(payload.get("inputs"), dict) else {}
        triage_status_ref = str(inputs.get("triage_status_ref") or "")
        drift_report_ref = str(inputs.get("drift_report_ref") or "")
        self.assertTrue(triage_status_ref.endswith("phase2_drift_status_2026-02-28.json"))
        self.assertNotEqual(drift_report_ref, triage_status_ref)
        self.assertTrue(Path(triage_status_ref).exists())

    def test_playbooks_export_appends_audit_event(self) -> None:
        benchmark_version = "phase2.pr12.v1"
        drift_ref = self._write_drift_report(file_name="event-drift.json", benchmark_version=benchmark_version)
        triage_ref = self._write_triage_status(file_name="event-triage.json", benchmark_version=benchmark_version)
        root_ref = self._write_root_cause_report(
            file_name="event-root-cause.json",
            benchmark_version=benchmark_version,
            top_recurring_causes=[
                {
                    "root_cause_code": "planning_policy_mismatch",
                    "occurrence_count": 2,
                    "affected_contracts": 2,
                    "recurring": True,
                    "gates": ["pr8"],
                    "evidence_refs": ["/tmp/pr8-root-ref.json"],
                }
            ],
            aggregate_state="WATCH",
            aggregate_reason_code="planning_policy_mismatch",
        )
        result = self.orchestrator.phase2_operator_playbooks(
            as_of_date="2026-02-28",
            lookback_window_days=30,
            benchmark_version=benchmark_version,
            out_dir=self.temp_dir / "event-out",
            drift_report_ref=drift_ref,
            triage_status_ref=triage_ref,
            root_cause_report_ref=root_ref,
            persist=True,
        )
        self.assertTrue(result["ok"])
        rows = self.repo.fetch_all(
            """
            SELECT event_type, payload_json
            FROM event_log
            WHERE event_type = 'PHASE2_OPERATOR_PLAYBOOKS_EXPORTED'
              AND as_of_date = ?
            ORDER BY created_at DESC
            """,
            ("2026-02-28",),
        )
        self.assertTrue(rows)
        payload = json.loads(str(rows[0]["payload_json"] or "{}"))
        self.assertEqual("phase2.pr12.v1", str(payload.get("benchmark_version") or ""))
        self.assertGreaterEqual(int(payload.get("selection_count") or 0), 1)

    def test_snapshot_reads_latest_exported_report(self) -> None:
        benchmark_version = "phase2.pr12.v1"
        drift_ref = self._write_drift_report(file_name="snapshot-drift.json", benchmark_version=benchmark_version)
        triage_ref = self._write_triage_status(file_name="snapshot-triage.json", benchmark_version=benchmark_version)
        root_ref = self._write_root_cause_report(
            file_name="snapshot-root-cause.json",
            benchmark_version=benchmark_version,
            top_recurring_causes=[
                {
                    "root_cause_code": "manual_override_concentration",
                    "occurrence_count": 4,
                    "affected_contracts": 3,
                    "recurring": True,
                    "gates": ["pr8", "pr9"],
                    "evidence_refs": ["/tmp/manual-override-ref.json"],
                }
            ],
            aggregate_state="WATCH",
            aggregate_reason_code="manual_override_concentration",
        )
        self.orchestrator.phase2_operator_playbooks(
            as_of_date="2026-02-28",
            lookback_window_days=30,
            benchmark_version=benchmark_version,
            out_dir=self.temp_dir / "snapshot-out",
            drift_report_ref=drift_ref,
            triage_status_ref=triage_ref,
            root_cause_report_ref=root_ref,
            persist=True,
        )
        snapshot = self.orchestrator.phase2_operator_playbooks_snapshot(
            as_of_date="2026-02-28",
            lookback_window_days=30,
            benchmark_version=benchmark_version,
        )
        self.assertEqual("phase2.pr12.v1", snapshot["benchmark_version"])
        self.assertEqual("manual_override_concentration", snapshot["aggregate_reason_code"])
        self.assertGreaterEqual(int(snapshot["selection_count"]), 1)
        self.assertTrue(str(snapshot["latest_report_json_path"]).endswith(".json"))


if __name__ == "__main__":
    unittest.main()
