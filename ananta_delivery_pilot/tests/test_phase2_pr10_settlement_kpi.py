from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from adapters.sqlite_repo import SQLiteRepo
from core.config import RuntimeConfig
from core.ids import new_ulid
from domain.automation import AutomationOrchestrator
from domain.services import Phase1Service


class Phase2Pr10SettlementKpiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.temp_dir = Path(tempfile.mkdtemp(prefix="phase2-pr10-tests-"))
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

    def _create_contract(self, *, lpo_no: str, issue_date: str = "2026-02-23") -> str:
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
                "expected_total_qty_kg": 60000,
                "expected_total_qty": 60.0,
                "expected_total_value": 136200000.0,
                "unit_price_basis": "KG",
                "lines": [
                    {
                        "product_code": "RBDSO",
                        "description": "PR10 settlement contract",
                        "expected_qty": 60000,
                        "unit": "kgs",
                        "unit_price": 2270.0,
                        "unit_price_basis": "KG",
                    }
                ],
            },
            allow_placeholder_tin=True,
        )
        return str(contract["contract_id"])

    def _create_invoiced_delivery(
        self,
        *,
        contract_id: str,
        suffix: str,
        delivered_qty_kg: int = 30000,
        delivery_date: str = "2026-02-23",
    ) -> dict[str, str]:
        delivery = self.service.add_delivery(
            {
                "contract_id": contract_id,
                "line_no": 1,
                "delivery_ref": f"DLV-PR10-{suffix}",
                "run_id": f"RUN-PR10-{suffix}",
                "batch_id": f"AFL-RBDSO-PR10-{suffix}",
                "delivery_date": delivery_date,
                "delivered_qty": delivered_qty_kg,
                "unit": "kgs",
                "unit_price": 2270.0,
                "unit_price_basis": "KG",
            }
        )
        delivery_id = str(delivery["delivery_id"])
        self.service.mark_dispatched(delivery_id)
        self.service.mark_delivered(delivery_id)
        coa_payload = self.service.coa_template_for_delivery(delivery_id, default_result="PASS")
        self.service.record_coa(coa_payload)
        pack = self.service.generate_pack(
            delivery_id=delivery_id,
            allow_placeholder_tin=True,
            skip_pdf=True,
            original_docs=[],
        )
        return {
            "delivery_id": delivery_id,
            "invoice_no": str(pack["invoice_no"]),
            "sales_transaction_id": str(pack["sales_transaction_id"]),
        }

    def _insert_intake_confirm_event(self, *, contract_id: str, as_of_date: str) -> None:
        with self.repo.transaction() as conn:
            self.repo.append_event(
                conn,
                entity_type="CONTRACT",
                entity_id=contract_id,
                event_type="INTAKE_CONFIRMED",
                as_of_date=as_of_date,
                payload={"fixture": True},
                source="test_pr10",
            )

    def test_settlement_suggestions_are_deterministic_and_auto_apply_safe(self) -> None:
        contract_id = self._create_contract(lpo_no="LPO-PR10-AUTO-001")
        invoice = self._create_invoiced_delivery(contract_id=contract_id, suffix="A")
        suggestion = self.service.settlement_suggest_allocations(
            contract_id=contract_id,
            as_of_date="2026-02-23",
            payment_reference=invoice["invoice_no"],
            amount_received=68100000.0,
            payment_date="2026-02-23",
            payment_method="Bank Transfer",
            dry_run=True,
        )
        replay = self.service.settlement_suggest_allocations(
            contract_id=contract_id,
            as_of_date="2026-02-23",
            payment_reference=invoice["invoice_no"],
            amount_received=68100000.0,
            payment_date="2026-02-23",
            payment_method="Bank Transfer",
            dry_run=True,
        )
        self.assertEqual("AUTO_APPLY", suggestion["decision_class"])
        self.assertEqual("auto_threshold_met", suggestion["reason_code"])
        self.assertEqual(suggestion["suggestion_set_id"], replay["suggestion_set_id"])
        self.assertEqual(
            suggestion["top_suggestion"]["suggestion_id"],
            replay["top_suggestion"]["suggestion_id"],
        )

        apply_result = self.service.settlement_apply_suggestion(
            contract_id=contract_id,
            suggestion_set_id=str(suggestion["suggestion_set_id"]),
            suggestion_id=str(suggestion["top_suggestion"]["suggestion_id"]),
            as_of_date="2026-02-23",
            decision="APPLY",
            reason="auto-safe",
            payment_reference="BANK-PR10-AUTO-001",
            payment_date="2026-02-23",
            payment_method="Bank Transfer",
            amount_received=68100000.0,
            allow_placeholder_tin=True,
            skip_pdf=True,
        )
        apply_replay = self.service.settlement_apply_suggestion(
            contract_id=contract_id,
            suggestion_set_id=str(suggestion["suggestion_set_id"]),
            suggestion_id=str(suggestion["top_suggestion"]["suggestion_id"]),
            as_of_date="2026-02-23",
            decision="APPLY",
            reason="auto-safe",
            payment_reference="BANK-PR10-AUTO-001",
            payment_date="2026-02-23",
            payment_method="Bank Transfer",
            amount_received=68100000.0,
            allow_placeholder_tin=True,
            skip_pdf=True,
        )
        self.assertEqual("APPLIED", apply_result["status"])
        self.assertEqual(apply_result, apply_replay)

        payment_row = self.repo.fetch_one("SELECT COUNT(*) AS cnt FROM payments", ())
        allocation_row = self.repo.fetch_one("SELECT COUNT(*) AS cnt FROM payment_allocations", ())
        receipt_row = self.repo.fetch_one("SELECT COUNT(*) AS cnt FROM documents WHERE doc_type = 'RECEIPT'", ())
        self.assertEqual(1, int((payment_row or {"cnt": 0})["cnt"] or 0))
        self.assertEqual(1, int((allocation_row or {"cnt": 0})["cnt"] or 0))
        self.assertEqual(1, int((receipt_row or {"cnt": 0})["cnt"] or 0))

    def test_settlement_conflict_routes_to_exception_case(self) -> None:
        contract_id = self._create_contract(lpo_no="LPO-PR10-AMB-001")
        self._create_invoiced_delivery(contract_id=contract_id, suffix="A")
        self._create_invoiced_delivery(contract_id=contract_id, suffix="B")
        suggestion = self.service.settlement_suggest_allocations(
            contract_id=contract_id,
            as_of_date="2026-02-23",
            payment_reference="INV-2026",
            amount_received=68100000.0,
            payment_date="2026-02-23",
            payment_method="Bank Transfer",
            dry_run=True,
        )
        self.assertEqual("BLOCKER", suggestion["decision_class"])
        self.assertEqual("multiple_candidate_conflict", suggestion["reason_code"])

        top = suggestion["top_suggestion"]
        result = self.service.settlement_apply_suggestion(
            contract_id=contract_id,
            suggestion_set_id=str(suggestion["suggestion_set_id"]),
            suggestion_id=str(top["suggestion_id"]),
            as_of_date="2026-02-23",
            decision="APPLY",
            reason="ambiguous should route",
            payment_reference="INV-2026",
            payment_date="2026-02-23",
            payment_method="Bank Transfer",
            amount_received=68100000.0,
            allow_placeholder_tin=True,
            skip_pdf=True,
        )
        self.assertEqual("EXCEPTION_ROUTED", result["status"])
        case = self.repo.fetch_one(
            """
            SELECT * FROM exception_cases
            WHERE exception_case_id = ?
            """,
            (str(result["exception_case_id"]),),
        )
        self.assertIsNotNone(case)
        assert case is not None
        self.assertEqual("settlement_allocation", str(case["case_type"]))
        self.assertEqual("multiple_candidate_conflict", str(case["reason_code"]))

    def test_withholding_gap_routes_withholding_evidence_missing(self) -> None:
        contract_id = self._create_contract(lpo_no="LPO-PR10-WHT-001")
        invoice = self._create_invoiced_delivery(contract_id=contract_id, suffix="WHT")
        with self.repo.transaction() as conn:
            conn.execute(
                "UPDATE sales_transactions SET expected_wht_amount = ? WHERE sales_transaction_id = ?",
                (5000.0, invoice["sales_transaction_id"]),
            )
        suggestion = self.service.settlement_suggest_allocations(
            contract_id=contract_id,
            as_of_date="2026-02-23",
            payment_reference=invoice["invoice_no"],
            amount_received=68097000.0,
            payment_date="2026-02-23",
            payment_method="Bank Transfer",
            dry_run=True,
        )
        self.assertEqual("BLOCKER", suggestion["decision_class"])
        self.assertEqual("withholding_evidence_missing", suggestion["reason_code"])

    def test_pr10_metrics_are_denominator_safe_and_version_guarded(self) -> None:
        result = self.orchestrator.autonomy_metrics(
            as_of_date="2026-02-28",
            out_dir=self.temp_dir / "metrics-empty",
            lookback_window_days=30,
            benchmark_version="phase2.pr10.v1",
        )
        metrics = result["metrics"]
        self.assertIsNone(metrics["touchless_rate"])
        self.assertEqual("insufficient_touchless_data", metrics["touchless_rate_reason_code"])
        self.assertIsNone(metrics["manual_inputs_per_delivery"])
        self.assertEqual("insufficient_manual_input_data", metrics["manual_inputs_per_delivery_reason_code"])
        self.assertIsNone(metrics["payment_suggestion_acceptance_rate"])
        self.assertEqual(
            "insufficient_payment_suggestion_data",
            metrics["payment_suggestion_acceptance_rate_reason_code"],
        )
        self.assertFalse(metrics["pr10_gate_pass"])
        self.assertEqual("insufficient_payment_suggestion_data", metrics["pr10_gate_reason_code"])

        mismatch = self.orchestrator.autonomy_metrics(
            as_of_date="2026-02-28",
            out_dir=self.temp_dir / "metrics-mismatch",
            lookback_window_days=30,
            benchmark_version="phase2.pr9.v1",
        )
        mismatch_metrics = mismatch["metrics"]
        self.assertFalse(mismatch_metrics["benchmark_version_match_pr10"])
        self.assertFalse(mismatch_metrics["pr10_gate_pass"])
        self.assertEqual("benchmark_version_mismatch", mismatch_metrics["pr10_gate_reason_code"])

    def test_pr10_trend_windows_use_utc_date_boundaries(self) -> None:
        contract_id = self._create_contract(lpo_no="LPO-PR10-TREND-001", issue_date="2026-02-15")
        invoice_prev = self._create_invoiced_delivery(
            contract_id=contract_id,
            suffix="PREV",
            delivery_date="2026-02-20",
        )
        self._insert_intake_confirm_event(
            contract_id=contract_id,
            as_of_date="2026-02-15",
        )
        # payment suggestion acceptance events (1 accepted, 1 routed) for deterministic acceptance rate
        with self.repo.transaction() as conn:
            self.repo.append_event(
                conn,
                entity_type="CONTRACT",
                entity_id=contract_id,
                event_type="SETTLEMENT_SUGGESTION_ACCEPTED",
                as_of_date="2026-02-28",
                payload={"fixture": "accepted"},
                source="test_pr10",
            )
            self.repo.append_event(
                conn,
                entity_type="CONTRACT",
                entity_id=contract_id,
                event_type="SETTLEMENT_SUGGESTION_ROUTED_EXCEPTION",
                as_of_date="2026-02-28",
                payload={"fixture": "routed"},
                source="test_pr10",
            )
            now = "2026-02-28T10:00:00Z"
            prev_case_id = new_ulid()
            current_case_id = new_ulid()
            conn.execute(
                """
                INSERT INTO exception_cases(
                    exception_case_id, autonomy_run_id, action_intent_id, contract_id, delivery_id, planned_delivery_id,
                    case_type, severity, status, reason_code, details_json, idempotency_key, created_at, updated_at, resolved_at
                )
                VALUES(?, NULL, NULL, ?, NULL, NULL, 'kpi_fixture', 'REVIEW', 'RESOLVED', 'fixture', '{}', ?, ?, ?, ?)
                """,
                (
                    prev_case_id,
                    contract_id,
                    f"pr10-prev-{prev_case_id}",
                    "2026-02-15T00:00:00Z",
                    now,
                    "2026-02-21T00:00:00Z",
                ),
            )
            conn.execute(
                """
                INSERT INTO exception_cases(
                    exception_case_id, autonomy_run_id, action_intent_id, contract_id, delivery_id, planned_delivery_id,
                    case_type, severity, status, reason_code, details_json, idempotency_key, created_at, updated_at, resolved_at
                )
                VALUES(?, NULL, NULL, ?, NULL, NULL, 'kpi_fixture', 'REVIEW', 'RESOLVED', 'fixture', '{}', ?, ?, ?, ?)
                """,
                (
                    current_case_id,
                    contract_id,
                    f"pr10-current-{current_case_id}",
                    "2026-02-22T00:00:00Z",
                    now,
                    "2026-02-27T00:00:00Z",
                ),
            )
            conn.execute(
                "UPDATE sales_transactions SET expected_wht_amount = 0 WHERE sales_transaction_id = ?",
                (invoice_prev["sales_transaction_id"],),
            )
        metrics = self.orchestrator.autonomy_metrics(
            as_of_date="2026-02-28",
            out_dir=self.temp_dir / "metrics-trend",
            lookback_window_days=30,
            benchmark_version="phase2.pr10.v1",
        )["metrics"]
        self.assertTrue(metrics["benchmark_version_match_pr10"])
        self.assertIsNotNone(metrics["exception_resolution_current_p95_hours"])
        self.assertIsNotNone(metrics["exception_resolution_previous_p95_hours"])
        self.assertEqual(0.5, metrics["payment_suggestion_acceptance_rate"])
        self.assertEqual("payment_suggestion_acceptance_rate_failed", metrics["pr10_gate_reason_code"])
        self.assertIn(
            metrics["exception_resolution_trend_state"],
            {"stable_or_improving", "worsening", "insufficient_data"},
        )


if __name__ == "__main__":
    unittest.main()
