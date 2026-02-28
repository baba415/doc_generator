from __future__ import annotations

import shutil
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from adapters.sqlite_repo import SQLiteRepo
from core.config import RuntimeConfig
from domain.services import Phase1Service


class Phase16PlanningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.temp_dir = Path(tempfile.mkdtemp(prefix="phase16-tests-"))
        shutil.copytree(self.repo_root / "config", self.temp_dir / "config")
        (self.temp_dir / ".state").mkdir(parents=True, exist_ok=True)
        (self.temp_dir / "output_v2").mkdir(parents=True, exist_ok=True)
        self.config = RuntimeConfig.load(self.temp_dir)
        self.repo = SQLiteRepo(self.config.state_dir / "drep.sqlite")
        self.service = Phase1Service(self.config, self.repo)
        self.service.init_db()

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_bulk_contract(self, *, qty_mt: float = 150.0, tolerance_pct: float | None = None, lpo_valid_to: str | None = "2026-03-31") -> str:
        payload = {
            "contract_ref": "LPO-PHASE16-001",
            "lpo_no": "LPO-PHASE16-001",
            "lpo_date": "2026-02-23",
            "buyer_id": "buyer_nycil",
            "vendor_of_record_id": "ananta_flows",
            "operator_id": "guildgate",
            "source_id": "ananta_flows",
            "processor_id": "processor_partner_refinery",
            "currency": "NGN",
            "issue_date": "2026-02-23",
            "lpo_valid_from": "2026-02-23",
            "lpo_valid_to": lpo_valid_to,
            "expected_total_qty": qty_mt,
            "expected_total_value": qty_mt * 2270.0,
            "lines": [
                {
                    "product_code": "RBDPO",
                    "description": "Bulk RBDPO",
                    "expected_qty": qty_mt,
                    "unit": "mt",
                    "unit_price": 2270.0,
                    "expected_value": qty_mt * 2270.0,
                }
            ],
        }
        if tolerance_pct is not None:
            payload["over_delivery_tolerance_pct"] = tolerance_pct
        contract = self.service.create_contract(payload, allow_placeholder_tin=True)
        return str(contract["contract_id"])

    def test_plan_splits_150mt_into_five_30mt_lots(self) -> None:
        contract_id = self._create_bulk_contract(qty_mt=150.0)
        result = self.service.plan_deliveries(contract_id=contract_id, start_date="2026-02-23", cadence="daily", max_lots_per_day=2)
        self.assertEqual(5, int(result["planned_count"]))
        planned = self.service.planned_rows(contract_id=contract_id)
        self.assertEqual(5, len(planned))
        for row in planned:
            self.assertEqual(30000, int(row["planned_qty_kg"]))

    def test_materialize_allows_qty_override_and_updates_kg(self) -> None:
        contract_id = self._create_bulk_contract(qty_mt=150.0)
        plan = self.service.plan_deliveries(contract_id=contract_id, start_date="2026-02-23", cadence="daily", max_lots_per_day=1)
        planned_delivery_id = str(plan["planned_deliveries"][0]["planned_delivery_id"])
        result = self.service.materialize_delivery(planned_delivery_id=planned_delivery_id, qty_mt=29.5)
        self.assertTrue(result["ok"])
        row = self.repo.fetch_one("SELECT * FROM planned_deliveries WHERE planned_delivery_id = ?", (planned_delivery_id,))
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(29500, int(row["materialized_qty_kg"]))
        self.assertEqual("SCHEDULED", str(row["status"]))

    def test_planning_blocks_when_window_too_short(self) -> None:
        contract_id = self._create_bulk_contract(qty_mt=150.0, lpo_valid_to="2026-02-23")
        with self.assertRaises(ValueError):
            self.service.plan_deliveries(contract_id=contract_id, start_date="2026-02-23", cadence="daily", max_lots_per_day=1)

    def test_materialize_blocks_when_contract_expired(self) -> None:
        contract_id = self._create_bulk_contract(qty_mt=30.0, lpo_valid_to="2026-02-23")
        plan = self.service.plan_deliveries(contract_id=contract_id, start_date="2026-02-23", cadence="daily", max_lots_per_day=1)
        planned_delivery_id = str(plan["planned_deliveries"][0]["planned_delivery_id"])
        with self.assertRaises(ValueError):
            self.service.materialize_delivery(planned_delivery_id=planned_delivery_id, as_of_date="2026-02-24")

    def test_tolerance_hierarchy_contract_overrides_buyer_default(self) -> None:
        contract_default = self._create_bulk_contract(qty_mt=30.0, tolerance_pct=None)
        contract_override = self._create_bulk_contract(qty_mt=30.0, tolerance_pct=2.5)
        row_default = self.repo.fetch_one("SELECT over_delivery_tolerance_pct FROM contracts WHERE contract_id = ?", (contract_default,))
        row_override = self.repo.fetch_one("SELECT over_delivery_tolerance_pct FROM contracts WHERE contract_id = ?", (contract_override,))
        self.assertIsNotNone(row_default)
        self.assertIsNotNone(row_override)
        assert row_default is not None
        assert row_override is not None
        self.assertEqual(5.0, float(row_default["over_delivery_tolerance_pct"]))
        self.assertEqual(2.5, float(row_override["over_delivery_tolerance_pct"]))

    def test_materialize_due_deliveries_handles_multiple_same_day(self) -> None:
        contract_id = self._create_bulk_contract(qty_mt=150.0)
        self.service.plan_deliveries(contract_id=contract_id, start_date="2026-02-23", cadence="daily", max_lots_per_day=2)
        result = self.service.materialize_due_deliveries(
            contract_id=contract_id,
            as_of_date="2026-02-23",
            auto_progress=True,
            auto_record_coa=False,
            auto_generate_pack=False,
        )
        self.assertTrue(result["ok"])
        processed = result.get("processed") or []
        self.assertEqual(2, len(processed))
        rows = self.repo.fetch_all(
            "SELECT status FROM deliveries WHERE contract_id = ? ORDER BY created_at ASC",
            (contract_id,),
        )
        self.assertEqual(2, len(rows))
        self.assertEqual("DELIVERED", rows[0]["status"])
        self.assertEqual("DELIVERED", rows[1]["status"])

    def test_approve_planned_deliveries_transitions_rows_to_scheduled(self) -> None:
        contract_id = self._create_bulk_contract(qty_mt=150.0)
        self.service.plan_deliveries(contract_id=contract_id, start_date="2026-02-23", cadence="daily", max_lots_per_day=1)
        result = self.service.approve_planned_deliveries(contract_id=contract_id)
        self.assertTrue(result["ok"])
        self.assertEqual(5, int(result["scheduled_count"]))
        rows = self.repo.fetch_all(
            "SELECT status FROM planned_deliveries WHERE contract_id = ?",
            (contract_id,),
        )
        self.assertTrue(rows)
        self.assertTrue(all(str(row["status"]).upper() == "SCHEDULED" for row in rows))

    def test_materialize_due_propagates_as_of_to_per_row_materialization(self) -> None:
        contract_id = self._create_bulk_contract(qty_mt=150.0)
        self.service.plan_deliveries(contract_id=contract_id, start_date="2026-02-23", cadence="daily", max_lots_per_day=2)
        target_as_of = "2026-02-23"
        with patch.object(self.service, "refresh_contract_state", wraps=self.service.refresh_contract_state) as refresh_mock:
            result = self.service.materialize_due_deliveries(
                contract_id=contract_id,
                as_of_date=target_as_of,
                auto_progress=False,
                auto_record_coa=False,
                auto_generate_pack=False,
            )
        self.assertTrue(result["ok"])
        due_count = len(result.get("processed") or [])
        self.assertEqual(2, due_count)
        self.assertGreaterEqual(len(refresh_mock.call_args_list), due_count + 1)
        for call in refresh_mock.call_args_list:
            self.assertEqual(target_as_of, call.kwargs.get("as_of_date"))

    def test_unit_price_basis_mt_computes_gross_deterministically(self) -> None:
        payload = {
            "contract_ref": "LPO-PRICE-BASIS-MT",
            "lpo_no": "LPO-PRICE-BASIS-MT",
            "buyer_id": "buyer_nycil",
            "vendor_of_record_id": "ananta_flows",
            "operator_id": "guildgate",
            "source_id": "ananta_flows",
            "processor_id": "processor_partner_refinery",
            "currency": "NGN",
            "issue_date": "2026-02-23",
            "expected_total_qty": 30.0,
            "expected_total_value": 68100000.0,
            "lines": [
                {
                    "product_code": "RBDSO",
                    "description": "RBDSO MT pricing",
                    "expected_qty": 30.0,
                    "unit": "mt",
                    "unit_price": 2270000.0,
                    "unit_price_basis": "MT",
                    "expected_value": 68100000.0,
                }
            ],
        }
        contract = self.service.create_contract(payload, allow_placeholder_tin=True)
        delivery = self.service.add_delivery(
            {
                "contract_id": contract["contract_id"],
                "line_no": 1,
                "delivery_ref": "PRICE-BASIS",
                "run_id": "RUN-PRICE-BASIS-01",
                "batch_id": "AFL-RBDSO-PRICE-BASIS-01",
                "delivery_date": "2026-02-23",
                "delivered_qty": 30.0,
                "unit": "mt",
                "unit_price": 2270000.0,
                "unit_price_basis": "MT",
            }
        )
        row = self.repo.fetch_one("SELECT gross_amount, unit_price_basis, delivered_qty_kg FROM deliveries WHERE delivery_id = ?", (delivery["delivery_id"],))
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual("MT", row["unit_price_basis"])
        self.assertEqual(30000, int(row["delivered_qty_kg"]))
        self.assertAlmostEqual(68100000.0, float(row["gross_amount"]), places=2)


if __name__ == "__main__":
    unittest.main()
