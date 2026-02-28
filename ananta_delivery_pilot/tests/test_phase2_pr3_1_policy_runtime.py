from __future__ import annotations

import json
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


class Phase2PR31PolicyRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.temp_dir = Path(tempfile.mkdtemp(prefix="phase2-pr31-tests-"))
        shutil.copytree(self.repo_root / "config", self.temp_dir / "config")
        (self.temp_dir / ".state").mkdir(parents=True, exist_ok=True)
        (self.temp_dir / "output_v2").mkdir(parents=True, exist_ok=True)
        self.config = RuntimeConfig.load(self.temp_dir)
        self.repo = SQLiteRepo(self.config.state_dir / "drep.sqlite")
        self.phase1 = Phase1Service(self.config, self.repo)
        self.phase1.init_db()
        self.orchestrator = AutomationOrchestrator(self.config, self.repo, self.phase1)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_contract(
        self,
        *,
        lpo_no: str,
        qty_kg: int = 30000,
        master_contract_id: str | None = None,
    ) -> str:
        qty_mt = qty_kg / 1000.0
        payload = {
            "contract_ref": lpo_no,
            "lpo_no": lpo_no,
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
            "expected_total_qty": qty_mt,
            "expected_total_value": qty_kg * 2270.0,
            "lines": [
                {
                    "product_code": "RBDSO",
                    "description": "Policy runtime test line",
                    "expected_qty": qty_mt,
                    "unit": "mt",
                    "unit_price": 2270.0,
                    "unit_price_basis": "KG",
                }
            ],
        }
        if master_contract_id:
            payload["master_contract_id"] = master_contract_id
        result = self.phase1.create_contract(payload, allow_placeholder_tin=True)
        return str(result["contract_id"])

    def _insert_policy_set(
        self,
        *,
        scope: str,
        ref_id: str | None,
        version: str,
        policy: dict[str, object],
        is_active: int = 1,
        effective_from: str | None = None,
        effective_to: str | None = None,
    ) -> str:
        policy_set_id = new_ulid()
        now = utc_now_iso_z()
        with self.repo.transaction() as conn:
            conn.execute(
                """
                INSERT INTO policy_sets(
                    policy_set_id, policy_scope, scope_ref_id, version, policy_json,
                    is_active, effective_from, effective_to, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    policy_set_id,
                    scope,
                    ref_id,
                    version,
                    json.dumps(policy, sort_keys=True),
                    int(is_active),
                    effective_from,
                    effective_to,
                    now,
                    now,
                ),
            )
        return policy_set_id

    def _add_delivered_qty(self, *, contract_id: str, qty_kg: int) -> None:
        delivery = self.phase1.add_delivery(
            {
                "contract_id": contract_id,
                "line_no": 1,
                "delivery_ref": "POLICY-DELIVERY",
                "run_id": "RUN-POLICY-01",
                "batch_id": "AFL-RBDSO-POLICY-01",
                "delivery_date": "2026-02-23",
                "delivered_qty": qty_kg,
                "unit": "kgs",
                "unit_price": 2270.0,
                "unit_price_basis": "KG",
                "force_over_delivery_reason": "policy runtime gate evaluation setup",
            }
        )
        self.phase1.mark_dispatched(str(delivery["delivery_id"]))
        self.phase1.mark_delivered(str(delivery["delivery_id"]))

    def test_plan_deliveries_uses_db_policy_precedence_contract_over_master_buyer_global(self) -> None:
        master_contract_id = self._create_contract(lpo_no="PR31-LPO-MASTER")
        contract_id = self._create_contract(
            lpo_no="PR31-LPO-CHILD",
            qty_kg=150000,
            master_contract_id=master_contract_id,
        )
        global_set_id = self._insert_policy_set(
            scope="global",
            ref_id=None,
            version="1",
            policy={"planning": {"default_max_lots_per_day": 1}},
            effective_from="2026-01-01",
            effective_to="2026-12-31",
        )
        buyer_set_id = self._insert_policy_set(
            scope="buyer",
            ref_id="buyer_nycil",
            version="1",
            policy={"planning": {"default_max_lots_per_day": 2}},
            effective_from="2026-01-01",
            effective_to="2026-12-31",
        )
        master_set_id = self._insert_policy_set(
            scope="master_contract",
            ref_id=master_contract_id,
            version="1",
            policy={"planning": {"default_max_lots_per_day": 3}},
            effective_from="2026-01-01",
            effective_to="2026-12-31",
        )
        contract_set_id = self._insert_policy_set(
            scope="contract",
            ref_id=contract_id,
            version="1",
            policy={"planning": {"default_max_lots_per_day": 4}},
            effective_from="2026-01-01",
            effective_to="2026-12-31",
        )

        result = self.phase1.plan_deliveries(contract_id=contract_id, start_date="2026-02-23")
        self.assertEqual("db", result["policy_source"])
        self.assertEqual(5, int(result["planned_count"]))
        self.assertEqual(4, int(result["max_lots_per_day"]))
        self.assertEqual(
            [global_set_id, buyer_set_id, master_set_id, contract_set_id],
            list(result.get("policy_set_ids") or []),
        )

    def test_inactive_or_expired_db_policies_are_ignored(self) -> None:
        contract_id = self._create_contract(lpo_no="PR31-LPO-INACTIVE")
        self._insert_policy_set(
            scope="contract",
            ref_id=contract_id,
            version="inactive",
            policy={"planning": {"default_max_lots_per_day": 9}},
            is_active=0,
            effective_from="2026-01-01",
            effective_to="2026-12-31",
        )
        self._insert_policy_set(
            scope="contract",
            ref_id=contract_id,
            version="expired",
            policy={"planning": {"default_max_lots_per_day": 8}},
            is_active=1,
            effective_from="2025-01-01",
            effective_to="2025-12-31",
        )

        result = self.phase1.plan_deliveries(contract_id=contract_id, start_date="2026-02-23")
        self.assertEqual("config", result["policy_source"])
        self.assertTrue(str(result["policy_source_key"]).startswith("config:"))

    def test_run_autonomy_fallback_policy_when_db_policy_absent(self) -> None:
        contract_id = self._create_contract(lpo_no="PR31-LPO-FALLBACK")
        run = self.orchestrator.run_autonomy(as_of_date="2026-02-23", contract_id=contract_id, dry_run=True)
        self.assertTrue(run["ok"])
        intents = self.repo.fetch_all(
            "SELECT policy_version, payload_json, idempotency_key FROM action_intents WHERE contract_id = ? ORDER BY created_at ASC",
            (contract_id,),
        )
        self.assertEqual(5, len(intents))
        for row in intents:
            payload = json.loads(row["payload_json"] or "{}")
            policy = payload.get("policy", {})
            self.assertEqual("config", policy.get("policy_source"))
            self.assertTrue(str(policy.get("policy_source_key", "")).startswith("config:"))
            self.assertIn(str(policy.get("policy_source_key")), str(row["idempotency_key"]))
            self.assertEqual(str(policy.get("policy_version")), str(row["policy_version"]))

    def test_run_autonomy_idempotency_key_includes_db_policy_version_and_source(self) -> None:
        contract_id = self._create_contract(lpo_no="PR31-LPO-IDEMPOTENCY")
        self._insert_policy_set(
            scope="global",
            ref_id=None,
            version="db-v1",
            policy={"confidence": {"payment_auto_apply_min": 0.75}},
            effective_from="2026-01-01",
            effective_to="2026-12-31",
        )
        first = self.orchestrator.run_autonomy(as_of_date="2026-02-23", contract_id=contract_id, dry_run=True)
        self.assertTrue(first["ok"])
        first_intents = self.repo.fetch_all(
            "SELECT idempotency_key FROM action_intents WHERE contract_id = ?",
            (contract_id,),
        )
        first_keys = {row["idempotency_key"] for row in first_intents}
        self.assertEqual(5, len(first_keys))

        with self.repo.transaction() as conn:
            conn.execute("UPDATE policy_sets SET is_active = 0 WHERE policy_scope = 'global'")
        self._insert_policy_set(
            scope="global",
            ref_id=None,
            version="db-v2",
            policy={"confidence": {"payment_auto_apply_min": 0.72}},
            effective_from="2026-01-01",
            effective_to="2026-12-31",
        )

        second = self.orchestrator.run_autonomy(as_of_date="2026-02-23", contract_id=contract_id, dry_run=True)
        self.assertTrue(second["ok"])
        all_intents = self.repo.fetch_all(
            "SELECT idempotency_key FROM action_intents WHERE contract_id = ?",
            (contract_id,),
        )
        all_keys = {row["idempotency_key"] for row in all_intents}
        self.assertEqual(10, len(all_keys))
        self.assertTrue(any("db-v1" in key for key in all_keys))
        self.assertTrue(any("db-v2" in key for key in all_keys))

    def test_gate_thresholds_read_from_db_policy_runtime(self) -> None:
        contract_id = self._create_contract(lpo_no="PR31-LPO-GATE-THRESHOLD")
        self._add_delivered_qty(contract_id=contract_id, qty_kg=33000)
        self._insert_policy_set(
            scope="contract",
            ref_id=contract_id,
            version="tolerance-pass",
            policy={"over_delivery": {"buyer_overrides": {"buyer_nycil": 15.0}}},
            effective_from="2026-01-01",
            effective_to="2026-12-31",
        )
        run = self.orchestrator.run_autonomy(as_of_date="2026-02-23", contract_id=contract_id, dry_run=True)
        self.assertTrue(run["ok"])
        per_contract = run.get("contracts", [])
        self.assertEqual(1, len(per_contract))
        quantity_gate = per_contract[0]["gates"]["quantity_tolerance"]
        self.assertEqual("PASS", quantity_gate["status"])
        self.assertEqual(15.0, float(quantity_gate["details"]["tolerance_pct"]))


if __name__ == "__main__":
    unittest.main()
