from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from adapters.sqlite_repo import SQLiteRepo
from core.config import RuntimeConfig
from core.ids import new_ulid
from core.time import utc_now_iso_z
from domain.automation import AutomationOrchestrator
from domain.services import Phase1Service


class Phase2Pr9TransportDocsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.temp_dir = Path(tempfile.mkdtemp(prefix="phase2-pr9-tests-"))
        shutil.copytree(self.repo_root / "config", self.temp_dir / "config")
        (self.temp_dir / ".state").mkdir(parents=True, exist_ok=True)
        (self.temp_dir / "output_v2").mkdir(parents=True, exist_ok=True)
        self.config = RuntimeConfig.load(self.temp_dir)
        self.repo = SQLiteRepo(self.config.state_dir / "drep.sqlite")
        self.service = Phase1Service(self.config, self.repo)
        self.orchestrator = AutomationOrchestrator(self.config, self.repo, self.service)
        self.service.init_db()
        contract = self.service.create_contract(
            {
                "contract_ref": "LPO-PR9-001",
                "lpo_no": "LPO-PR9-001",
                "buyer_id": "buyer_nycil",
                "vendor_of_record_id": "ananta_flows",
                "operator_id": "guildgate",
                "source_id": "ananta_flows",
                "processor_id": "processor_partner_refinery",
                "currency": "NGN",
                "issue_date": "2026-02-23",
                "lpo_valid_from": "2026-02-23",
                "lpo_valid_to": "2026-03-31",
                "expected_total_qty_kg": 60000,
                "expected_total_qty": 60.0,
                "expected_total_value": 136200000.0,
                "unit_price_basis": "KG",
                "lines": [
                    {
                        "product_code": "RBDPO",
                        "description": "PR9 contract",
                        "expected_qty": 60000,
                        "unit": "kgs",
                        "unit_price": 2270.0,
                        "unit_price_basis": "KG",
                    }
                ],
            },
            allow_placeholder_tin=True,
        )
        self.contract_id = str(contract["contract_id"])

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _add_delivery(self, *, ref: str, run_id: str, batch_id: str) -> str:
        row = self.service.add_delivery(
            {
                "contract_id": self.contract_id,
                "line_no": 1,
                "delivery_ref": ref,
                "run_id": run_id,
                "batch_id": batch_id,
                "delivery_date": "2026-02-23",
                "delivered_qty": 30000,
                "unit": "kgs",
                "unit_price": 2270.0,
                "unit_price_basis": "KG",
            }
        )
        return str(row["delivery_id"])

    def test_capture_evidence_dedupes_by_contract_hash(self) -> None:
        self._add_delivery(ref="DLV-PR9-DEDUPE", run_id="RUN-PR9-A", batch_id="BATCH-PR9-A")
        source = self.temp_dir / "same-proof-waybill.pdf"
        source.write_bytes(b"same proof bytes")
        first = self.service.capture_evidence_original(contract_id=self.contract_id, source_path=source)
        second = self.service.capture_evidence_original(contract_id=self.contract_id, source_path=source)
        self.assertEqual(first["sha256"], second["sha256"])
        self.assertTrue(bool(second.get("deduped")))
        total = self.repo.fetch_one("SELECT COUNT(*) AS cnt FROM evidence_originals WHERE contract_id = ?", (self.contract_id,))
        self.assertEqual(1, int((total or {"cnt": 0})["cnt"] or 0))

    def test_document_completion_strong_match_autolinks(self) -> None:
        delivery_id = self._add_delivery(ref="DLV-PR9-STRONG", run_id="RUN-PR9-B", batch_id="BATCH-PR9-B")
        source = self.temp_dir / "waybill_RUN-PR9-B_BATCH-PR9-B.pdf"
        source.write_bytes(b"waybill strong")
        self.service.capture_evidence_original(contract_id=self.contract_id, source_path=source)
        result = self.service.document_completion_copilot(contract_id=self.contract_id, as_of_date="2026-02-23")
        self.assertTrue(result["ok"])
        self.assertEqual(1, int(result["auto_linked"]))
        evidence = self.repo.fetch_one(
            "SELECT * FROM evidence_originals WHERE contract_id = ? ORDER BY created_at DESC LIMIT 1",
            (self.contract_id,),
        )
        self.assertIsNotNone(evidence)
        assert evidence is not None
        self.assertEqual(delivery_id, str(evidence["delivery_id"]))
        self.assertEqual("AUTO_LINKED", str(evidence["link_status"]))
        self.assertEqual("waybill", str(evidence["doc_type"]))

    def test_document_completion_ambiguous_routes_blocker_exception(self) -> None:
        self._add_delivery(ref="DLV-PR9-AMB-1", run_id="RUN-PR9-C1", batch_id="BATCH-PR9-C1")
        self._add_delivery(ref="DLV-PR9-AMB-2", run_id="RUN-PR9-C2", batch_id="BATCH-PR9-C2")
        source = self.temp_dir / "waybill_scan_no_refs.pdf"
        source.write_bytes(b"ambiguous")
        self.service.capture_evidence_original(contract_id=self.contract_id, source_path=source)
        result = self.service.document_completion_copilot(contract_id=self.contract_id, as_of_date="2026-02-23")
        self.assertTrue(result["ok"])
        self.assertGreaterEqual(int(result["blocker_cases"]), 1)
        case = self.repo.fetch_one(
            """
            SELECT * FROM exception_cases
            WHERE contract_id = ? AND case_type = 'document_linkage' AND status = 'OPEN'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (self.contract_id,),
        )
        self.assertIsNotNone(case)
        assert case is not None
        self.assertEqual("doc_link_ambiguous", str(case["reason_code"]))
        evidence = self.repo.fetch_one(
            "SELECT * FROM evidence_originals WHERE contract_id = ? ORDER BY created_at DESC LIMIT 1",
            (self.contract_id,),
        )
        self.assertEqual("BLOCKED", str(evidence["link_status"]))

    def test_document_link_case_decision_can_apply_manual_link(self) -> None:
        delivery_id = self._add_delivery(ref="DLV-PR9-CASE", run_id="RUN-PR9-D", batch_id="BATCH-PR9-D")
        source = self.temp_dir / "doc_unknown.pdf"
        source.write_bytes(b"unknown")
        row = self.service.capture_evidence_original(contract_id=self.contract_id, source_path=source)
        evidence_id = str(row["evidence_id"])
        with self.repo.transaction() as conn:
            case = self.repo.create_or_get_exception_case(
                conn,
                autonomy_run_id=None,
                action_intent_id=None,
                contract_id=self.contract_id,
                delivery_id=None,
                planned_delivery_id=None,
                case_type="document_linkage",
                severity="REVIEW",
                reason_code="doc_link_partial",
                details={
                    "evidence_id": evidence_id,
                    "as_of_date": "2026-02-23",
                    "selected_candidate": {
                        "delivery_id": delivery_id,
                        "sales_transaction_id": "",
                        "sales_line_id": "",
                    },
                },
                idempotency_key=f"manual-doc-link::{evidence_id}",
            )
        decided = self.orchestrator.decide_case(
            case_id=str(case["exception_case_id"]),
            decision="APPROVE",
            reason="manual link approval",
            resume=False,
            dry_run_resume=True,
        )
        self.assertTrue(decided["ok"])
        evidence = self.repo.fetch_one("SELECT * FROM evidence_originals WHERE evidence_id = ?", (evidence_id,))
        self.assertIsNotNone(evidence)
        assert evidence is not None
        self.assertEqual("MANUAL_LINKED", str(evidence["link_status"]))
        self.assertEqual(delivery_id, str(evidence["delivery_id"]))

    def test_pr9_metrics_include_transport_and_doc_gate_fields(self) -> None:
        delivery_id = self._add_delivery(ref="DLV-PR9-METRIC", run_id="RUN-PR9-E", batch_id="BATCH-PR9-E")
        now = "2026-02-23T10:00:00Z"
        with self.repo.transaction() as conn:
            self.repo.upsert_delivery_transport_snapshot(
                conn,
                delivery_id=delivery_id,
                planned_delivery_id=None,
                transport_partner_id=None,
                transport_truck_id=None,
                transport_driver_id=None,
                partner_name="",
                truck_no="TRK-900",
                driver_name="Driver Nine",
                driver_phone="08099990000",
                source_type="test",
                source_ref="fixture",
                confidence=0.95,
                reason_code="transport_auto_applied",
                payload={"fixture": True},
            )
            conn.execute(
                "UPDATE delivery_transport_snapshot SET created_at = ? WHERE delivery_id = ?",
                (now, delivery_id),
            )
            case = self.repo.create_or_get_exception_case(
                conn,
                autonomy_run_id=None,
                action_intent_id=None,
                contract_id=self.contract_id,
                delivery_id=delivery_id,
                planned_delivery_id=None,
                case_type="transport_assignment",
                severity="REVIEW",
                reason_code="transport_low_confidence",
                details={"as_of_date": "2026-02-23"},
                idempotency_key=f"pr9-metric-case::{delivery_id}",
            )
            self.repo.add_decision_outcome(
                conn,
                exception_case_id=str(case["exception_case_id"]),
                human_decision_id=None,
                outcome_label="transport_suggestion_feedback",
                outcome_payload={"accepted": True},
            )
            conn.execute(
                """
                UPDATE decision_outcomes
                SET created_at = ?
                WHERE exception_case_id = ? AND outcome_label = 'transport_suggestion_feedback'
                """,
                (now, str(case["exception_case_id"])),
            )
            evidence_auto = new_ulid()
            evidence_fp = new_ulid()
            conn.execute(
                """
                INSERT INTO evidence_originals(
                    evidence_id, contract_id, delivery_id, sales_transaction_id, sales_line_id,
                    file_name, doc_type, link_status, link_confidence, link_reason_code, link_source, linked_at,
                    source_path, stored_path, sha256, captured_at, created_at, updated_at
                )
                VALUES(?, ?, ?, NULL, NULL, 'waybill_a.pdf', 'waybill', 'AUTO_LINKED', 0.95, 'strong_match', 'test', ?, '/tmp/a', '/tmp/a', ?, ?, ?, ?)
                """,
                (evidence_auto, self.contract_id, delivery_id, now, "sha-a", now, now, now),
            )
            conn.execute(
                """
                INSERT INTO evidence_originals(
                    evidence_id, contract_id, delivery_id, sales_transaction_id, sales_line_id,
                    file_name, doc_type, link_status, link_confidence, link_reason_code, link_source, linked_at,
                    source_path, stored_path, sha256, captured_at, created_at, updated_at
                )
                VALUES(?, ?, ?, NULL, NULL, 'waybill_b.pdf', 'waybill', 'AUTO_LINK_REJECTED', 0.60, 'manual_link_rejected', 'test', ?, '/tmp/b', '/tmp/b', ?, ?, ?, ?)
                """,
                (evidence_fp, self.contract_id, delivery_id, now, "sha-b", now, now, now),
            )
        result = self.orchestrator.autonomy_metrics(
            as_of_date="2026-02-23",
            out_dir=self.temp_dir / "metrics",
            lookback_window_days=30,
            benchmark_version="phase2.pr9.v1",
        )
        self.assertTrue(result["ok"])
        metrics = result["metrics"]
        self.assertIn("manual_transport_fields_per_delivery", metrics)
        self.assertIn("doc_autolink_precision", metrics)
        self.assertTrue(metrics["benchmark_version_match_pr9"])
        self.assertEqual(1, metrics["manual_transport_field_updates"])
        self.assertEqual(1, metrics["deliveries_with_transport_assignment"])
        self.assertEqual(1.0, metrics["manual_transport_fields_per_delivery"])
        self.assertEqual(0.5, metrics["doc_autolink_precision"])
        self.assertFalse(metrics["doc_autolink_precision_gate_pass"])
        self.assertFalse(metrics["pr9_gate_pass"])
        self.assertEqual("doc_autolink_precision_failed", metrics["pr9_gate_reason_code"])


if __name__ == "__main__":
    unittest.main()
