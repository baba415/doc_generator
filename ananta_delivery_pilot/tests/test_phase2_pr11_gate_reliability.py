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


class Phase2Pr11GateReliabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.temp_dir = Path(tempfile.mkdtemp(prefix="phase2-pr11-tests-"))
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

    def test_seed_phase2_benchmark_is_idempotent_without_reset(self) -> None:
        first = self.orchestrator.seed_phase2_benchmark(
            as_of_date="2026-02-28",
            benchmark_version="phase2.pr11.v1",
            reset=False,
            lookback_window_days=30,
        )
        second = self.orchestrator.seed_phase2_benchmark(
            as_of_date="2026-02-28",
            benchmark_version="phase2.pr11.v1",
            reset=False,
            lookback_window_days=30,
        )
        self.assertTrue(first["ok"])
        self.assertFalse(first["reused"])
        self.assertTrue(second["ok"])
        self.assertTrue(second["reused"])
        self.assertEqual(first["benchmark_run_id"], second["benchmark_run_id"])
        self.assertEqual(first["fixture_counts"], second["fixture_counts"])

    def test_run_phase2_benchmark_produces_non_zero_pr10_denominator(self) -> None:
        out_dir = self.temp_dir / "benchmark-run"
        result = self.orchestrator.run_phase2_benchmark(
            as_of_date="2026-02-28",
            lookback_window_days=30,
            benchmark_version="phase2.pr11.v1",
            out_dir=out_dir,
        )
        self.assertTrue(result["ok"])
        gate_report = result["gate_report"]
        pr10 = next(g for g in gate_report["gates"] if g["gate_name"] == "pr10")
        self.assertGreater(float(pr10["metrics"]["payment_suggestion_acceptance_rate"] or 0.0), 0.0)
        self.assertTrue(Path(result["report_json_path"]).exists())
        self.assertTrue(Path(result["report_md_path"]).exists())

    def test_gate_report_distinguishes_benchmark_mismatch_from_threshold_failure(self) -> None:
        self.orchestrator.seed_phase2_benchmark(
            as_of_date="2026-02-28",
            benchmark_version="phase2.pr11.v1",
            reset=False,
            lookback_window_days=30,
        )
        mismatch_report = self.orchestrator.phase2_gate_report(
            as_of_date="2026-02-28",
            lookback_window_days=30,
            benchmark_version="phase2.pr11.v2",
            out_dir=self.temp_dir / "mismatch",
        )["gate_report"]
        mismatch_reasons = {g["gate_name"]: g["reason_code"] for g in mismatch_report["gates"]}
        self.assertEqual("benchmark_version_mismatch", mismatch_reasons["pr8"])
        self.assertEqual("benchmark_version_mismatch", mismatch_reasons["pr9"])
        self.assertEqual("benchmark_version_mismatch", mismatch_reasons["pr10"])

        seeded_report = self.orchestrator.phase2_gate_report(
            as_of_date="2026-02-28",
            lookback_window_days=30,
            benchmark_version="phase2.pr11.v1",
            out_dir=self.temp_dir / "seeded",
        )["gate_report"]
        seeded_reasons = {g["gate_name"]: g["reason_code"] for g in seeded_report["gates"]}
        self.assertNotEqual("benchmark_version_mismatch", seeded_reasons["pr8"])
        self.assertNotEqual("benchmark_version_mismatch", seeded_reasons["pr9"])
        self.assertNotEqual("benchmark_version_mismatch", seeded_reasons["pr10"])

    def test_waiver_validation_valid_and_expired(self) -> None:
        self.orchestrator.seed_phase2_benchmark(
            as_of_date="2026-02-28",
            benchmark_version="phase2.pr11.v1",
            reset=False,
            lookback_window_days=30,
        )
        waiver_path = self.temp_dir / ".state" / "release-readiness" / "phase2_gate_waivers.json"
        waiver_path.parent.mkdir(parents=True, exist_ok=True)
        valid_waivers = {
            "waivers": [
                {
                    "waiver_id": "WV-PR8",
                    "gate_name": "pr8",
                    "reason": "temporary mismatch",
                    "owner_product": "prod-owner",
                    "owner_ops": "ops-owner",
                    "owner_engineering": "eng-owner",
                    "created_at_utc": "2026-02-27T00:00:00Z",
                    "expires_at_utc": "2026-03-15T00:00:00Z",
                    "fallback_plan": "feature flag off",
                    "active": True,
                },
                {
                    "waiver_id": "WV-PR9",
                    "gate_name": "pr9",
                    "reason": "temporary mismatch",
                    "owner_product": "prod-owner",
                    "owner_ops": "ops-owner",
                    "owner_engineering": "eng-owner",
                    "created_at_utc": "2026-02-27T00:00:00Z",
                    "expires_at_utc": "2026-03-15T00:00:00Z",
                    "fallback_plan": "feature flag off",
                    "active": True,
                },
                {
                    "waiver_id": "WV-PR10",
                    "gate_name": "pr10",
                    "reason": "temporary mismatch",
                    "owner_product": "prod-owner",
                    "owner_ops": "ops-owner",
                    "owner_engineering": "eng-owner",
                    "created_at_utc": "2026-02-27T00:00:00Z",
                    "expires_at_utc": "2026-03-15T00:00:00Z",
                    "fallback_plan": "feature flag off",
                    "active": True,
                },
            ]
        }
        waiver_path.write_text(json.dumps(valid_waivers, indent=2), encoding="utf-8")
        waived = self.orchestrator.phase2_gate_report(
            as_of_date="2026-02-28",
            lookback_window_days=30,
            benchmark_version="phase2.pr11.v2",
            waivers_path=waiver_path,
            out_dir=self.temp_dir / "waived",
        )
        self.assertEqual("WAIVED", waived["promotion_recommendation"])

        expired_waivers = dict(valid_waivers)
        expired_waivers["waivers"] = [dict(item) for item in valid_waivers["waivers"]]
        expired_waivers["waivers"][1]["expires_at_utc"] = "2026-02-01T00:00:00Z"
        waiver_path.write_text(json.dumps(expired_waivers, indent=2), encoding="utf-8")
        failed = self.orchestrator.phase2_gate_report(
            as_of_date="2026-02-28",
            lookback_window_days=30,
            benchmark_version="phase2.pr11.v3",
            waivers_path=waiver_path,
            out_dir=self.temp_dir / "waived-expired",
        )
        self.assertEqual("FAIL", failed["promotion_recommendation"])
        summary = failed["gate_report"]["waiver_validation"]
        self.assertGreater(int(summary["invalid_waiver_count"]), 0)

    def test_gate_report_outcomes_are_deterministic_for_same_inputs(self) -> None:
        self.orchestrator.seed_phase2_benchmark(
            as_of_date="2026-02-28",
            benchmark_version="phase2.pr11.v1",
            reset=False,
            lookback_window_days=30,
        )
        first = self.orchestrator.phase2_gate_report(
            as_of_date="2026-02-28",
            lookback_window_days=30,
            benchmark_version="phase2.pr11.v1",
            out_dir=self.temp_dir / "determinism-a",
        )["gate_report"]
        second = self.orchestrator.phase2_gate_report(
            as_of_date="2026-02-28",
            lookback_window_days=30,
            benchmark_version="phase2.pr11.v1",
            out_dir=self.temp_dir / "determinism-b",
        )["gate_report"]
        self.assertEqual(first["gates"], second["gates"])
        self.assertEqual(first["aggregate"], second["aggregate"])
        self.assertEqual(first["inputs"]["as_of_date"], second["inputs"]["as_of_date"])
        self.assertEqual(first["inputs"]["benchmark_version"], second["inputs"]["benchmark_version"])


if __name__ == "__main__":
    unittest.main()
