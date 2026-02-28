from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from adapters.sqlite_repo import SQLiteRepo
from core.config import RuntimeConfig
from core.ids import new_ulid
from core.time import utc_now_iso_z
from domain.services import Phase1Service


class Phase2Pr6ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.temp_dir = Path(tempfile.mkdtemp(prefix="phase2-pr6-service-"))
        shutil.copytree(self.repo_root / "config", self.temp_dir / "config")
        (self.temp_dir / ".state").mkdir(parents=True, exist_ok=True)
        (self.temp_dir / "output_v2").mkdir(parents=True, exist_ok=True)
        self.config = RuntimeConfig.load(self.temp_dir)
        self.repo = SQLiteRepo(self.config.state_dir / "drep.sqlite")
        self.service = Phase1Service(self.config, self.repo)
        self.service.init_db()

        self.contract_due = self._create_contract("LPO-PR6-DUE-001", issue_date="2026-02-20", valid_to="2026-03-31")
        self.contract_due_2 = self._create_contract("LPO-PR6-DUE-002", issue_date="2026-02-20", valid_to="2026-03-31")
        self.contract_not_due = self._create_contract("LPO-PR6-NOTDUE-001", issue_date="2026-02-20", valid_to="2026-03-31")
        self.contract_expired = self._create_contract("LPO-PR6-EXPIRED-001", issue_date="2026-01-01", valid_to="2026-02-01")
        self.contract_exception = self._create_contract("LPO-PR6-EXC-001", issue_date="2026-02-20", valid_to="2026-03-31")

        self.service.plan_deliveries(
            contract_id=self.contract_due,
            start_date="2026-02-20",
            cadence="daily",
            max_lots_per_day=1,
        )
        self.service.plan_deliveries(
            contract_id=self.contract_due_2,
            start_date="2026-02-20",
            cadence="daily",
            max_lots_per_day=1,
        )
        self.service.plan_deliveries(
            contract_id=self.contract_not_due,
            start_date="2026-03-25",
            cadence="daily",
            max_lots_per_day=1,
        )
        self.service.plan_deliveries(
            contract_id=self.contract_expired,
            start_date="2026-01-01",
            cadence="daily",
            max_lots_per_day=1,
        )
        self.service.plan_deliveries(
            contract_id=self.contract_exception,
            start_date="2026-02-20",
            cadence="daily",
            max_lots_per_day=1,
        )
        self.service.refresh_contract_state(as_of_date="2026-02-23")
        self._insert_exception_case(contract_id=self.contract_exception)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_contract(self, lpo_no: str, *, issue_date: str, valid_to: str) -> str:
        payload = {
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
            "lpo_valid_to": valid_to,
            "expected_total_qty": 30.0,
            "expected_total_value": 68_100_000.0,
            "lines": [
                {
                    "product_code": "RBDSO",
                    "description": f"Contract {lpo_no}",
                    "expected_qty": 30.0,
                    "unit": "mt",
                    "unit_price": 2270.0,
                    "unit_price_basis": "KG",
                }
            ],
        }
        created = self.service.create_contract(payload, allow_placeholder_tin=True)
        return str(created["contract_id"])

    def _insert_exception_case(self, *, contract_id: str) -> None:
        now = utc_now_iso_z()
        with self.repo.transaction() as conn:
            conn.execute(
                """
                INSERT INTO exception_cases(
                    exception_case_id, contract_id, case_type, severity, status,
                    reason_code, details_json, idempotency_key, created_at, updated_at
                ) VALUES(?, ?, 'manual_review', 'BLOCKER', 'OPEN', ?, ?, ?, ?, ?)
                """,
                (
                    new_ulid(),
                    contract_id,
                    "OPEN_EXCEPTION_CASES",
                    json.dumps({"source": "test_phase2_pr6_service"}, sort_keys=True),
                    f"test-exception::{contract_id}",
                    now,
                    now,
                ),
            )

    def _seed_successful_intents(self, *, contract_id: str, as_of_date: str) -> None:
        intent_types = [
            "plan_deliveries",
            "materialize_due",
            "auto_progress",
            "generate_pack",
            "export_drep",
        ]
        with self.repo.transaction() as conn:
            for intent_type in intent_types:
                intent = self.repo.create_or_get_action_intent(
                    conn,
                    autonomy_run_id="RUN-EXISTING",
                    intent_type=intent_type,
                    contract_id=contract_id,
                    delivery_id=None,
                    planned_delivery_id=None,
                    as_of_date=as_of_date,
                    scheduled_at=utc_now_iso_z(),
                    policy_version="phase2.pr6.v1",
                    payload={"source": "test", "intent_type": intent_type},
                    idempotency_key=f"existing::{contract_id}::{intent_type}::{as_of_date}",
                )
                self.repo.set_action_intent_status(
                    conn,
                    action_intent_id=str(intent["action_intent_id"]),
                    status="EXECUTED",
                )
                self.repo.add_action_execution(
                    conn,
                    action_intent_id=str(intent["action_intent_id"]),
                    status="SUCCESS",
                    idempotency_key=f"existing-exec::{contract_id}::{intent_type}::{as_of_date}",
                    request_payload={"intent_type": intent_type},
                    response_payload={"ok": True},
                )

    def test_command_center_sections_classification(self) -> None:
        sections = self.service.command_center_sections(as_of_date="2026-02-23", limit=200)["sections"]
        needs_decision_ids = {str(row["contract_id"]) for row in sections["NEEDS_DECISION"]}
        due_action_ids = {str(row["contract_id"]) for row in sections["DUE_ACTION"]}
        at_risk_ids = {str(row["contract_id"]) for row in sections["AT_RISK"]}
        self.assertIn(self.contract_exception, needs_decision_ids)
        self.assertIn(self.contract_due, due_action_ids)
        self.assertIn(self.contract_expired, at_risk_ids)
        self.assertNotIn(self.contract_not_due, due_action_ids)

    def test_run_all_preview_is_idempotent_and_execute_requires_token(self) -> None:
        preview_a = self.service.run_all_eligible(
            as_of_date_utc="2026-02-23",
            mode="preview",
            benchmark_version="phase2.pr6.v1",
            max_contracts_per_run=20,
            max_actions_per_run=200,
        )
        preview_b = self.service.run_all_eligible(
            as_of_date_utc="2026-02-23",
            mode="preview",
            benchmark_version="phase2.pr6.v1",
            max_contracts_per_run=20,
            max_actions_per_run=200,
        )
        self.assertEqual(preview_a["preview_token"], preview_b["preview_token"])
        with self.assertRaisesRegex(ValueError, "preview_token is required"):
            self.service.run_all_eligible(
                as_of_date_utc="2026-02-23",
                mode="execute",
                benchmark_version="phase2.pr6.v1",
                max_contracts_per_run=20,
                max_actions_per_run=200,
            )
        with self.assertRaisesRegex(ValueError, "preview_token mismatch"):
            self.service.run_all_eligible(
                as_of_date_utc="2026-02-23",
                mode="execute",
                benchmark_version="phase2.pr6.v1",
                max_contracts_per_run=20,
                max_actions_per_run=200,
                preview_token="invalid",
            )

    def test_run_all_execute_caps_and_skip_reasons(self) -> None:
        preview = self.service.run_all_eligible(
            as_of_date_utc="2026-02-23",
            mode="preview",
            benchmark_version="phase2.pr6.v1",
            max_contracts_per_run=2,
            max_actions_per_run=5,
        )
        with patch.object(
            self.service,
            "run_recommended_cycle",
            return_value={"ok": True, "autonomy_run_id": "RUN-MOCK", "case_ids": []},
        ) as cycle_mock:
            executed = self.service.run_all_eligible(
                as_of_date_utc="2026-02-23",
                mode="execute",
                benchmark_version="phase2.pr6.v1",
                max_contracts_per_run=2,
                max_actions_per_run=5,
                preview_token=str(preview["preview_token"]),
            )
        self.assertTrue(executed["ok"])
        self.assertEqual(1, cycle_mock.call_count)
        summary = executed["summary"]
        self.assertEqual(1, int(summary["executed_count"]))
        reason_codes = {str(item["reason_code"]) for item in executed["skipped_contracts"]}
        self.assertIn("OPEN_EXCEPTION_CASES", reason_codes)
        self.assertIn("ACTION_CAP_REACHED", reason_codes)

    def test_run_all_execute_marks_already_applied_without_reinvoking_cycle(self) -> None:
        preview = self.service.run_all_eligible(
            as_of_date_utc="2026-02-23",
            mode="preview",
            benchmark_version="phase2.pr6.v1",
            max_contracts_per_run=20,
            max_actions_per_run=200,
        )
        self._seed_successful_intents(contract_id=self.contract_due, as_of_date="2026-02-23")
        with patch.object(
            self.service,
            "run_recommended_cycle",
            return_value={"ok": True, "autonomy_run_id": "RUN-NEVER", "case_ids": []},
        ) as cycle_mock:
            result = self.service.run_all_eligible(
                as_of_date_utc="2026-02-23",
                mode="execute",
                benchmark_version="phase2.pr6.v1",
                max_contracts_per_run=20,
                max_actions_per_run=200,
                preview_token=str(preview["preview_token"]),
            )
        status_by_contract = {str(item["contract_id"]): str(item["status"]) for item in result["executed_contracts"]}
        self.assertEqual("ALREADY_APPLIED", status_by_contract[self.contract_due])
        called_contract_ids = [str(call.kwargs.get("contract_id") or "") for call in cycle_mock.mock_calls]
        self.assertNotIn(self.contract_due, called_contract_ids)

    def test_run_all_execute_is_idempotent_on_replay(self) -> None:
        preview = self.service.run_all_eligible(
            as_of_date_utc="2026-02-23",
            mode="preview",
            benchmark_version="phase2.pr6.v1",
            max_contracts_per_run=1,
            max_actions_per_run=200,
        )
        with patch.object(
            self.service,
            "run_recommended_cycle",
            return_value={"ok": True, "autonomy_run_id": "RUN-ONCE", "case_ids": []},
        ) as cycle_mock:
            first = self.service.run_all_eligible(
                as_of_date_utc="2026-02-23",
                mode="execute",
                benchmark_version="phase2.pr6.v1",
                max_contracts_per_run=1,
                max_actions_per_run=200,
                preview_token=str(preview["preview_token"]),
            )
            second = self.service.run_all_eligible(
                as_of_date_utc="2026-02-23",
                mode="execute",
                benchmark_version="phase2.pr6.v1",
                max_contracts_per_run=1,
                max_actions_per_run=200,
                preview_token=str(preview["preview_token"]),
            )
        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertEqual(1, cycle_mock.call_count)
        self.assertFalse(bool(first.get("idempotent_replay")))
        self.assertTrue(bool(second.get("idempotent_replay")))

    def test_run_all_execute_retries_when_only_failed_history_exists(self) -> None:
        preview = self.service.run_all_eligible(
            as_of_date_utc="2026-02-23",
            mode="preview",
            benchmark_version="phase2.pr6.v1",
            max_contracts_per_run=20,
            max_actions_per_run=200,
        )
        with self.repo.transaction() as conn:
            intent = self.repo.create_or_get_action_intent(
                conn,
                autonomy_run_id="RUN-FAILED",
                intent_type="materialize_due",
                contract_id=self.contract_due,
                delivery_id=None,
                planned_delivery_id=None,
                as_of_date="2026-02-23",
                scheduled_at=utc_now_iso_z(),
                policy_version="phase2.pr6.v1",
                payload={"source": "test"},
                idempotency_key=f"failed::{self.contract_due}::materialize_due::2026-02-23",
            )
            self.repo.set_action_intent_status(
                conn,
                action_intent_id=str(intent["action_intent_id"]),
                status="FAILED",
            )
            self.repo.add_action_execution(
                conn,
                action_intent_id=str(intent["action_intent_id"]),
                status="FAILED",
                idempotency_key=f"failed-exec::{self.contract_due}::materialize_due::2026-02-23",
                request_payload={"intent_type": "materialize_due"},
                response_payload={"ok": False},
                error_payload={"reason": "failed-history"},
            )
        with patch.object(
            self.service,
            "run_recommended_cycle",
            return_value={"ok": True, "autonomy_run_id": "RUN-RETRY", "case_ids": []},
        ) as cycle_mock:
            result = self.service.run_all_eligible(
                as_of_date_utc="2026-02-23",
                mode="execute",
                benchmark_version="phase2.pr6.v1",
                max_contracts_per_run=20,
                max_actions_per_run=200,
                preview_token=str(preview["preview_token"]),
            )
        status_by_contract = {str(item["contract_id"]): str(item["status"]) for item in result["executed_contracts"]}
        self.assertEqual("SUCCESS", status_by_contract[self.contract_due])
        called_contract_ids = [str(call.kwargs.get("contract_id") or "") for call in cycle_mock.mock_calls]
        self.assertIn(self.contract_due, called_contract_ids)


if __name__ == "__main__":
    unittest.main()
