from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from adapters.sqlite_repo import SQLiteRepo
from core.config import RuntimeConfig, infer_buyer_group
from domain.automation import AutomationOrchestrator
from domain.services import Phase1Service


class Phase15AutomationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.temp_dir = Path(tempfile.mkdtemp(prefix="phase15-tests-"))
        shutil.copytree(self.repo_root / "config", self.temp_dir / "config")
        (self.temp_dir / ".state").mkdir(parents=True, exist_ok=True)
        (self.temp_dir / "output_v2").mkdir(parents=True, exist_ok=True)
        (self.temp_dir / "evidence").mkdir(parents=True, exist_ok=True)
        (self.temp_dir / "evidence" / "lpo_known.txt").write_text("lpo evidence", encoding="utf-8")
        (self.temp_dir / "evidence" / "waybill_source.txt").write_text("waybill evidence", encoding="utf-8")
        (self.temp_dir / "evidence" / "coa_source.txt").write_text("coa evidence", encoding="utf-8")

        self.config = RuntimeConfig.load(self.temp_dir)
        self.repo = SQLiteRepo(self.config.state_dir / "drep.sqlite")
        self.phase1 = Phase1Service(self.config, self.repo)
        self.phase1.init_db()
        self.orchestrator = AutomationOrchestrator(self.config, self.repo, self.phase1)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _full_coa_rows(self, buyer_id: str, product_code: str) -> list[dict[str, str]]:
        buyer_party = self.config.registry.get(buyer_id)
        buyer_group = infer_buyer_group(buyer_id, buyer_party.name)
        profile = self.config.coa_profiles[product_code]
        overrides = profile.get("buyer_overrides", {}) if isinstance(profile.get("buyer_overrides"), dict) else {}
        for key, override in overrides.items():
            key_norm = "".join(ch.lower() for ch in str(key) if ch.isalnum())
            buyer_norm = "".join(ch.lower() for ch in buyer_group if ch.isalnum())
            if key_norm and (key_norm == buyer_norm or key_norm in buyer_norm):
                if isinstance(override, dict):
                    profile = {**profile, **override}
                break
        return [{"parameter": row["parameter"], "result": "PASS"} for row in profile["quality_parameters"]]

    def _base_payload(self, lpo_no: str) -> dict[str, object]:
        return {
            "lpo_no": lpo_no,
            "lpo_date": "2026-02-23",
            "issue_date": "2026-02-23",
            "due_date": "2026-03-09",
            "buyer_id": "buyer_nycil",
            "vendor_of_record_id": "ananta_flows",
            "source_id": "ananta_flows",
            "processor_id": "processor_partner_refinery",
            "product_code": "RBDSO",
            "description": "Automation test supply",
            "expected_qty": 30000.0,
            "unit": "kgs",
            "unit_price": 2270.0,
            "delivery_date": "2026-02-23",
            "delivered_qty": 30000.0,
            "run_id": f"RUN-{lpo_no}",
            "batch_id": f"AFL-RBDSO-{lpo_no}",
            "truck_no": "T28162LA",
            "driver_name": "Idowu Atanda",
            "driver_phone": "08052803019",
            "allow_placeholder_tin": True,
            "skip_pdf": True,
            "evidence_files": [
                str(self.temp_dir / "evidence" / "lpo_known.txt"),
                str(self.temp_dir / "evidence" / "waybill_source.txt"),
                str(self.temp_dir / "evidence" / "coa_source.txt"),
            ],
        }

    def test_stp_happy_path_meets_kpi(self) -> None:
        payload = self._base_payload("STP-HAPPY-001")
        payload["coa_results"] = self._full_coa_rows("buyer_nycil", "RBDSO")
        payload["payment"] = {
            "payment_date": "2026-03-09",
            "payment_method": "Bank Transfer",
            "external_reference": "AUTO-HAPPY-PAY-001",
            "amount_received": 68100000.0,
        }
        result = self.orchestrator.auto_run(payload, as_of_date="2026-03-31", dry_run=False)
        self.assertEqual("COMPLETED", result["status"])
        self.assertIn("pack", result["results"])
        self.assertLessEqual(int(result["metrics"]["manual_interventions_count"]), 1)
        self.assertGreaterEqual(float(result["metrics"]["auto_population_rate"]), 0.9)

    def test_partial_evidence_creates_targeted_exceptions(self) -> None:
        payload = self._base_payload("STP-PARTIAL-001")
        payload["coa_results"] = [{"parameter": "Appearance", "result": "PASS"}]
        result = self.orchestrator.auto_run(payload, as_of_date="2026-03-31", dry_run=False)
        self.assertEqual("NEEDS_REVIEW", result["status"])
        self.assertLessEqual(int(result["metrics"]["manual_interventions_count"]), 3)
        open_types = {row["exception_type"] for row in result["open_exceptions"]}
        self.assertIn("coa_validation_failed", open_types)

    def test_unknown_entity_suggestions_block_only_critical(self) -> None:
        payload = self._base_payload("STP-UNKNOWN-001")
        payload.pop("buyer_id", None)
        payload.pop("vendor_of_record_id", None)
        payload["buyer_name"] = "NYCL LTD"
        payload["vendor_of_record_name"] = "Anata Flowz"
        payload["coa_results"] = self._full_coa_rows("buyer_nycil", "RBDSO")
        result = self.orchestrator.auto_run(payload, as_of_date="2026-03-31", dry_run=True)
        self.assertIn(result["status"], {"NEEDS_REVIEW", "FAILED"})
        identity_rows = [row for row in result["open_exceptions"] if row["exception_type"] == "identity_resolution"]
        self.assertTrue(identity_rows)
        suggestions = json.loads(identity_rows[0].get("suggestions_json") or "[]")
        self.assertTrue(isinstance(suggestions, list))

    def test_payment_auto_match_ambiguous_goes_to_exception(self) -> None:
        for idx in (1, 2):
            payload = self._base_payload(f"STP-AMB-{idx:03d}")
            payload["coa_results"] = self._full_coa_rows("buyer_nycil", "RBDSO")
            self.orchestrator.auto_run(payload, as_of_date="2026-03-31", dry_run=False)

        payload = self._base_payload("STP-AMB-003")
        payload["coa_results"] = self._full_coa_rows("buyer_nycil", "RBDSO")
        payload["payment"] = {
            "payment_date": "2026-03-09",
            "payment_method": "Bank Transfer",
            "external_reference": "AUTO-AMB-PAY-003",
            "amount_received": 68100000.0
        }
        result = self.orchestrator.auto_run(payload, as_of_date="2026-03-31", dry_run=False)
        open_types = {row["exception_type"] for row in result["open_exceptions"]}
        self.assertIn("ambiguous_payment_allocation", open_types)

    def test_materializes_all_due_lots_not_just_first(self) -> None:
        payload = self._base_payload("STP-MULTI-LOTS-001")
        payload["expected_qty"] = 150000.0
        payload["quantity"] = 150000.0
        payload["unit"] = "kgs"
        payload["max_lots_per_day"] = 2
        payload["delivery_date"] = "2026-02-23"
        payload["skip_pdf"] = True
        payload["coa_results"] = self._full_coa_rows("buyer_nycil", "RBDSO")
        result = self.orchestrator.auto_run(payload, as_of_date="2026-02-23", dry_run=False)
        self.assertEqual("COMPLETED", result["status"])
        deliveries = result["results"].get("deliveries_materialized") or []
        self.assertEqual(2, len(deliveries))
        packs = result["results"].get("packs") or []
        self.assertEqual(2, len(packs))


if __name__ == "__main__":
    unittest.main()
