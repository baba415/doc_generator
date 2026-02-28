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


class Phase2Pr7ExceptionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.temp_dir = Path(tempfile.mkdtemp(prefix="phase2-pr7-exc-"))
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
                "contract_ref": "LPO-PR7-EXC-001",
                "lpo_no": "LPO-PR7-EXC-001",
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
                "expected_total_qty": 30.0,
                "expected_total_value": 68_100_000.0,
                "lines": [
                    {
                        "product_code": "RBDSO",
                        "description": "PR7 exceptions contract",
                        "expected_qty": 30.0,
                        "unit": "mt",
                        "unit_price": 2270.0,
                        "unit_price_basis": "KG",
                    }
                ],
            },
            allow_placeholder_tin=True,
        )
        self.contract_id = str(contract["contract_id"])
        self.service.cancel_contract(contract_id=self.contract_id, reason="PR7 test cancel")
        self.orchestrator.run_autonomy(as_of_date="2026-02-23", contract_id=self.contract_id, dry_run=True)
        cases = self.repo.list_exception_cases(status="OPEN")
        target = next(row for row in cases if str(row.get("contract_id") or "") == self.contract_id)
        self.case_id = str(target["exception_case_id"])

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_exception_case_cards_include_sla_and_consequence_preview(self) -> None:
        cards = self.service.exception_case_cards(
            status="OPEN",
            as_of_date_utc="2026-02-23",
            contract_id=self.contract_id,
        )
        self.assertTrue(cards)
        card = cards[0]
        self.assertIn("sla_state", card)
        self.assertIn("sla_target_hours", card)
        preview = card.get("consequence_preview")
        self.assertIsInstance(preview, dict)
        assert isinstance(preview, dict)
        self.assertTrue(preview.get("resume_supported"))
        self.assertEqual("2026-02-23", str(preview.get("resume_as_of_date")))

    def test_decide_exception_case_requires_reason(self) -> None:
        with self.assertRaisesRegex(ValueError, "reason is required"):
            self.service.decide_exception_case(
                case_id=self.case_id,
                decision="APPROVE",
                reason="   ",
                resume=True,
                dry_run_resume=True,
            )

    def test_decision_writes_resume_audit_events(self) -> None:
        result = self.service.decide_exception_case(
            case_id=self.case_id,
            decision="OVERRIDE",
            reason="approve and resume",
            resume=True,
            dry_run_resume=True,
        )
        self.assertTrue(result["ok"])
        events = self.repo.fetch_all(
            """
            SELECT event_type
            FROM event_log
            WHERE entity_type = 'EXCEPTION_CASE'
              AND entity_id = ?
            ORDER BY created_at ASC
            """,
            (self.case_id,),
        )
        event_types = [str(row.get("event_type") or "") for row in events]
        self.assertIn("CASE_DECIDED", event_types)
        self.assertIn("CASE_RESUME_REQUESTED", event_types)
        self.assertIn("CASE_RESUME_COMPLETED", event_types)

        activity = self.service.exception_case_activity(case_id=self.case_id)
        activity_types = [str(item.get("event_type") or "") for item in activity]
        self.assertIn("CASE_RESUME_COMPLETED", activity_types)

    def test_manual_interactions_in_exceptions_rate_metric(self) -> None:
        self.service.decide_exception_case(
            case_id=self.case_id,
            decision="APPROVE",
            reason="kpi metric test",
            resume=False,
            dry_run_resume=True,
        )
        with self.repo.transaction() as conn:
            row = conn.execute(
                "SELECT human_decision_id FROM human_decisions WHERE exception_case_id = ? ORDER BY created_at DESC LIMIT 1",
                (self.case_id,),
            ).fetchone()
            assert row is not None
            human_decision_id = str(row["human_decision_id"])
            conn.execute(
                "UPDATE human_decisions SET decided_at = ?, created_at = ? WHERE human_decision_id = ?",
                ("2026-02-20T10:00:00Z", "2026-02-20T10:00:00Z", human_decision_id),
            )

            run_id = new_ulid()
            self.repo.create_automation_run(
                conn,
                run_id=run_id,
                idempotency_key=f"kpi-run::{run_id}",
                as_of_date="2026-02-20",
                dry_run=True,
                input_payload={"source": "pr7-kpi-test"},
            )
            self.repo.add_automation_decision(
                conn,
                run_id=run_id,
                stage="intake",
                field_name="buyer_id",
                required_flag=True,
                proposed_value="buyer_nycil",
                source_type="ui",
                source_ref="/v2/intake",
                confidence=1.0,
                decision="user_corrected",
                reason_code="user_corrected",
                rule_path="manual",
            )
            conn.execute(
                "UPDATE automation_decisions SET created_at = ? WHERE run_id = ?",
                ("2026-02-21T09:30:00Z", run_id),
            )

        out_dir = self.temp_dir / "kpi-metrics"
        result = self.orchestrator.autonomy_metrics(
            as_of_date="2026-02-23",
            out_dir=out_dir,
            lookback_window_days=30,
            benchmark_version="phase2.pr7.v1",
        )
        self.assertTrue(result["ok"])
        self.assertEqual(1, result["metrics"]["manual_interactions_via_exceptions"])
        self.assertEqual(1, result["metrics"]["manual_interactions_user_overrides"])
        self.assertEqual(2, result["metrics"]["manual_interactions_total"])
        self.assertEqual(0.5, result["metrics"]["manual_interactions_in_exceptions_rate"])
        self.assertFalse(result["metrics"]["manual_interactions_in_exceptions_gate_pass"])


if __name__ == "__main__":
    unittest.main()
