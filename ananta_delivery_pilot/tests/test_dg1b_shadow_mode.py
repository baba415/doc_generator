from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from adapters.sqlite_repo import SQLiteRepo
from core.config import RuntimeConfig
from core.hashing import sha256_file
from domain.automation import AutomationOrchestrator
from domain.rails_truth import (
    LocalNoopRailsAdapter,
    RailsTruthShadowEmitter,
    build_trust_idempotency_key,
    resolve_rails_truth_flags,
    validate_execute_drep_daily_payload,
    validate_execute_proof_export_payload,
)
from domain.services import Phase1Service


class DG1BShadowModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.temp_dir = Path(tempfile.mkdtemp(prefix="dg1b-shadow-tests-"))
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

    def _valid_daily_payload(self) -> dict[str, object]:
        return {
            "contract_version": "execute_drep_daily_v1",
            "as_of_date": "2026-03-01",
            "deliveries_ingested": 5,
            "trips_assigned": 5,
            "trips_finalized": 0,
            "board_close": {"red": 0, "yellow": 2, "green": 3, "total": 5},
            "hold_full_by_reason": {},
            "hold_partial_by_reason": {},
            "handshake_valid_count": 0,
            "handshake_total_finalized": 0,
            "handshake_valid_rate": 0.0,
            "open_incidents_count": 0,
            "go_no_go": "GO",
            "go_no_go_reasons": ["NO_FINALIZED_TRIPS"],
        }

    def _valid_export_payload(self, *, out_dir: Path) -> dict[str, object]:
        out_dir.mkdir(parents=True, exist_ok=True)
        manifest_payload = {
            "contract_version": "execute_proof_manifest_v1",
            "trip_id": "TRIP-DG1B-0001",
            "delivery_id": "DLV-DG1B-0001",
            "pack_status": "FINAL",
            "generated_at": "2026-03-01T00:00:00Z",
            "evidence": [],
            "summary": {
                "evidence_count": 0,
                "artifact_type_counts": {},
                "artifact_role_counts": {},
            },
        }
        manifest_path = out_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest_payload, indent=2, sort_keys=True), encoding="utf-8")
        manifest_sha = sha256_file(manifest_path)
        pdf_path = out_dir / "pack.pdf"
        pdf_path.write_bytes(b"%PDF-1.4\n%DG1B\n")
        pdf_sha = sha256_file(pdf_path)
        return {
            "export_contract_version": "execute_proof_export_v1",
            "trip_id": "TRIP-DG1B-0001",
            "pack_status": "FINAL",
            "manifest_contract_version": "execute_proof_manifest_v1",
            "manifest_path": str(manifest_path),
            "manifest_sha256": manifest_sha,
            "pdf_path": str(pdf_path),
            "pdf_sha256": pdf_sha,
        }

    def test_validator_pass_fail_matrix(self) -> None:
        daily = self._valid_daily_payload()
        self.assertTrue(validate_execute_drep_daily_payload(daily).ok)

        version_bad = dict(daily)
        version_bad["contract_version"] = "execute_drep_daily_v0"
        self.assertEqual(
            "EXEC_DREP_DAILY_VERSION_MISMATCH",
            validate_execute_drep_daily_payload(version_bad).reason_code,
        )

        order_bad = dict(daily)
        order_bad["go_no_go_reasons"] = ["NO_FINALIZED_TRIPS", "LOW_HANDSHAKE_VALID_RATE"]
        self.assertEqual(
            "EXEC_DREP_DAILY_REASON_ORDER_INVALID",
            validate_execute_drep_daily_payload(order_bad).reason_code,
        )

        blocked_bad = dict(daily)
        blocked_bad["go_no_go"] = "NO_GO"
        blocked_bad["go_no_go_reasons"] = ["NO_FINALIZED_TRIPS"]
        self.assertEqual(
            "EXEC_DREP_DAILY_GO_NO_GO_BLOCKED",
            validate_execute_drep_daily_payload(blocked_bad).reason_code,
        )

        export = self._valid_export_payload(out_dir=self.temp_dir / "validator")
        self.assertTrue(validate_execute_proof_export_payload(export).ok)

        export_version_bad = dict(export)
        export_version_bad["export_contract_version"] = "execute_proof_export_v0"
        self.assertEqual(
            "EXEC_PROOF_EXPORT_VERSION_MISMATCH",
            validate_execute_proof_export_payload(export_version_bad).reason_code,
        )

        export_draft_pdf_bad = dict(export)
        export_draft_pdf_bad["pack_status"] = "DRAFT"
        self.assertEqual(
            "EXEC_PROOF_EXPORT_DRAFT_PDF_INVALID",
            validate_execute_proof_export_payload(export_draft_pdf_bad).reason_code,
        )

        export_hash_bad = dict(export)
        export_hash_bad["manifest_sha256"] = "0" * 64
        self.assertEqual(
            "EXEC_PROOF_EXPORT_HASH_INVALID",
            validate_execute_proof_export_payload(export_hash_bad).reason_code,
        )

    def test_idempotency_keys_are_deterministic(self) -> None:
        proof_input = self.orchestrator._dg1b_contract_validation_inputs(
            as_of_date="2026-03-01",
            out_dir=self.temp_dir / "idempotency",
        )
        samples = proof_input["trust_action_payloads"]
        first = {
            action_name: build_trust_idempotency_key(action_name, payload)
            for action_name, payload in samples.items()
        }
        second = {
            action_name: build_trust_idempotency_key(action_name, payload)
            for action_name, payload in samples.items()
        }
        self.assertEqual(first, second)
        self.assertEqual(
            "drep:contract:create:ananta_flows:LPO-DG1B-0001",
            first["create-contract"],
        )

    def test_shadow_emission_is_reproducible(self) -> None:
        flags = resolve_rails_truth_flags(self.config.rails_truth_flags)
        emitter = RailsTruthShadowEmitter(
            flags=flags,
            adapter=LocalNoopRailsAdapter(rails_write_enabled=False),
        )
        payload = {
            "contract_id": "CTR-DG1B-0001",
            "lpo_no": "LPO-DG1B-0001",
            "buyer_id": "buyer_nycil",
            "vendor_of_record_id": "ananta_flows",
            "issue_date": "2026-03-01",
            "lpo_valid_from": "2026-03-01",
            "lpo_valid_to": "2026-03-01",
            "expected_total_qty_kg": 150000,
            "currency": "NGN",
            "unit_price_basis": "KG",
            "policy_pointers": {"policy_version": "phase1_6.v1"},
        }
        first = emitter.emit(
            action="create-contract",
            payload=payload,
            as_of_date="2026-03-01",
            occurred_at_utc="2026-03-01T00:00:00Z",
        )
        second = emitter.emit(
            action="create-contract",
            payload=payload,
            as_of_date="2026-03-01",
            occurred_at_utc="2026-03-01T00:00:00Z",
        )
        self.assertTrue(first["ok"])
        self.assertEqual(first, second)
        self.assertFalse(bool(first["decision_log"]["adapter_result"]["sent"]))

    def test_dg1b_shadow_proof_writes_required_artifacts(self) -> None:
        out_dir = self.temp_dir / ".state" / "phase2-proof" / "dg1b" / "20260301T120000Z"
        result = self.orchestrator.dg1b_shadow_proof(
            as_of_date="2026-03-01",
            out_dir=out_dir,
        )
        self.assertTrue(result["ok"])
        expected_files = {
            "dg1a_action_inventory.json",
            "dg1a_action_event_mapping.json",
            "dg1a_shadow_emit.log",
            "dg1a_execute_drep_daily_validation.json",
            "dg1a_execute_proof_export_validation.json",
            "dg1a_flag_snapshot.json",
            "dg1a_go_no_go_checklist.json",
        }
        self.assertTrue(expected_files.issubset({path.name for path in out_dir.iterdir()}))

        drep_validation = json.loads((out_dir / "dg1a_execute_drep_daily_validation.json").read_text(encoding="utf-8"))
        proof_validation = json.loads((out_dir / "dg1a_execute_proof_export_validation.json").read_text(encoding="utf-8"))
        self.assertTrue(drep_validation["pass"]["ok"])
        self.assertTrue(proof_validation["pass"]["ok"])
        self.assertEqual("NO_GO", result["final_decision"])


if __name__ == "__main__":
    unittest.main()
