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


class Phase2PR3TransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.temp_dir = Path(tempfile.mkdtemp(prefix="phase2-pr3-tests-"))
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

    def _create_contract_and_delivery(self, *, lpo_no: str, truck_no: str = "", driver_name: str = "") -> tuple[str, str]:
        contract = self.phase1.create_contract(
            {
                "contract_ref": lpo_no,
                "lpo_no": lpo_no,
                "buyer_id": "buyer_nycil",
                "vendor_of_record_id": "ananta_flows",
                "operator_id": "guildgate",
                "source_id": "ananta_flows",
                "processor_id": "processor_partner_refinery",
                "currency": "NGN",
                "issue_date": "2026-02-23",
                "lpo_valid_from": "2026-02-23",
                "lpo_valid_to": "2026-03-05",
                "expected_total_qty": 30000.0,
                "lines": [
                    {
                        "product_code": "RBDSO",
                        "description": "Transport test line",
                        "expected_qty": 30000.0,
                        "unit": "kgs",
                        "unit_price": 2270.0,
                    }
                ],
            },
            allow_placeholder_tin=True,
        )
        delivery = self.phase1.add_delivery(
            {
                "contract_id": contract["contract_id"],
                "line_no": 1,
                "delivery_ref": f"DLV-{lpo_no}",
                "run_id": f"RUN-{lpo_no}",
                "batch_id": f"BATCH-{lpo_no}",
                "delivery_date": "2026-02-23",
                "delivered_qty": 30000.0,
                "unit": "kgs",
                "unit_price": 2270.0,
                "truck_no": truck_no,
                "driver_name": driver_name,
                "driver_phone": "",
            }
        )
        return str(contract["contract_id"]), str(delivery["delivery_id"])

    def _seed_transport(self, *, expired: bool = False, second_assignment: bool = False) -> dict[str, str]:
        now = utc_now_iso_z()
        partner_id = new_ulid()
        truck_1 = new_ulid()
        driver_1 = new_ulid()
        truck_2 = new_ulid()
        driver_2 = new_ulid()
        with self.repo.transaction() as conn:
            conn.execute(
                """
                INSERT INTO transport_partners(transport_partner_id, legal_name, code, status, created_at, updated_at)
                VALUES(?, 'Partner A', 'PA', 'ACTIVE', ?, ?)
                """,
                (partner_id, now, now),
            )
            conn.execute(
                """
                INSERT INTO transport_trucks(transport_truck_id, transport_partner_id, truck_no, capacity_kg, status, created_at, updated_at)
                VALUES(?, ?, 'TRK-001', 30000, 'ACTIVE', ?, ?)
                """,
                (truck_1, partner_id, now, now),
            )
            conn.execute(
                """
                INSERT INTO transport_drivers(transport_driver_id, transport_partner_id, full_name, phone, status, created_at, updated_at)
                VALUES(?, ?, 'Driver One', '08011111111', 'ACTIVE', ?, ?)
                """,
                (driver_1, partner_id, now, now),
            )
            conn.execute(
                """
                INSERT INTO truck_driver_assignments(assignment_id, transport_truck_id, transport_driver_id, effective_from, effective_to, is_primary, created_at, updated_at)
                VALUES(?, ?, ?, '2026-01-01', NULL, 1, ?, ?)
                """,
                (new_ulid(), truck_1, driver_1, now, now),
            )
            conn.execute(
                """
                INSERT INTO transport_aliases(transport_alias_id, entity_type, entity_id, alias_text, normalized_alias, source, created_at)
                VALUES(?, 'DRIVER', ?, 'Idowu Atanda', 'idowuatanda', 'test', ?)
                """,
                (new_ulid(), driver_1, now),
            )
            conn.execute(
                """
                INSERT INTO transport_aliases(transport_alias_id, entity_type, entity_id, alias_text, normalized_alias, source, created_at)
                VALUES(?, 'TRUCK', ?, 'T28162LA', 't28162la', 'test', ?)
                """,
                (new_ulid(), truck_1, now),
            )
            if second_assignment:
                conn.execute(
                    """
                    INSERT INTO transport_trucks(transport_truck_id, transport_partner_id, truck_no, capacity_kg, status, created_at, updated_at)
                    VALUES(?, ?, 'TRK-002', 30000, 'ACTIVE', ?, ?)
                    """,
                    (truck_2, partner_id, now, now),
                )
                conn.execute(
                    """
                    INSERT INTO transport_drivers(transport_driver_id, transport_partner_id, full_name, phone, status, created_at, updated_at)
                    VALUES(?, ?, 'Driver Two', '08022222222', 'ACTIVE', ?, ?)
                    """,
                    (driver_2, partner_id, now, now),
                )
                conn.execute(
                    """
                    INSERT INTO truck_driver_assignments(assignment_id, transport_truck_id, transport_driver_id, effective_from, effective_to, is_primary, created_at, updated_at)
                    VALUES(?, ?, ?, '2026-01-01', NULL, 1, ?, ?)
                    """,
                    (new_ulid(), truck_2, driver_2, now, now),
                )
            if expired:
                conn.execute(
                    """
                    INSERT INTO transport_compliance_docs(
                        compliance_doc_id, entity_type, entity_id, doc_type, doc_ref, valid_from, valid_to, status, created_at, updated_at
                    ) VALUES(?, 'TRUCK', ?, 'ROADWORTHY', 'DOC-EXPIRED', '2025-01-01', '2026-01-31', 'ACTIVE', ?, ?)
                    """,
                    (new_ulid(), truck_1, now, now),
                )
        return {
            "partner_id": partner_id,
            "truck_1": truck_1,
            "driver_1": driver_1,
            "truck_2": truck_2,
            "driver_2": driver_2,
        }

    def test_driver_hint_auto_suggests_truck_and_phone(self) -> None:
        self._seed_transport()
        contract_id, delivery_id = self._create_contract_and_delivery(
            lpo_no="PR3-DRIVER-HINT",
            truck_no="",
            driver_name="Driver One",
        )
        outcome = self.orchestrator._resolve_transport_for_delivery(
            autonomy_run_id=new_ulid(),
            action_intent_id=None,
            contract_id=contract_id,
            delivery_id=delivery_id,
            planned_delivery_id=None,
            as_of_date="2026-02-23",
        )
        self.assertTrue(outcome["applied"])
        bundle = self.repo.get_delivery_bundle(delivery_id)
        self.assertEqual("TRK-001", bundle["truck_no"])
        self.assertEqual("Driver One", bundle["driver_name"])
        self.assertEqual("08011111111", bundle["driver_phone"])

    def test_truck_hint_auto_suggests_driver(self) -> None:
        self._seed_transport()
        contract_id, delivery_id = self._create_contract_and_delivery(
            lpo_no="PR3-TRUCK-HINT",
            truck_no="TRK-001",
            driver_name="",
        )
        outcome = self.orchestrator._resolve_transport_for_delivery(
            autonomy_run_id=new_ulid(),
            action_intent_id=None,
            contract_id=contract_id,
            delivery_id=delivery_id,
            planned_delivery_id=None,
            as_of_date="2026-02-23",
        )
        self.assertTrue(outcome["applied"])
        bundle = self.repo.get_delivery_bundle(delivery_id)
        self.assertEqual("Driver One", bundle["driver_name"])
        self.assertEqual("08011111111", bundle["driver_phone"])

    def test_expired_compliance_creates_blocker_exception_case(self) -> None:
        self._seed_transport(expired=True)
        contract_id, delivery_id = self._create_contract_and_delivery(
            lpo_no="PR3-EXPIRED-COMP",
            truck_no="TRK-001",
            driver_name="Driver One",
        )
        outcome = self.orchestrator._resolve_transport_for_delivery(
            autonomy_run_id=new_ulid(),
            action_intent_id=None,
            contract_id=contract_id,
            delivery_id=delivery_id,
            planned_delivery_id=None,
            as_of_date="2026-02-23",
        )
        self.assertFalse(outcome["applied"])
        self.assertEqual("BLOCKER", outcome["case_severity"])
        self.assertEqual("transport_compliance_expired", outcome["reason_code"])
        cases = self.repo.list_exception_cases(status="OPEN")
        self.assertTrue(any(row["reason_code"] == "transport_compliance_expired" for row in cases))

    def test_bundle_prefers_snapshot_over_mutated_delivery_fields(self) -> None:
        self._seed_transport()
        contract_id, delivery_id = self._create_contract_and_delivery(
            lpo_no="PR3-SNAPSHOT-PREF",
            truck_no="TRK-001",
            driver_name="Driver One",
        )
        self.orchestrator._resolve_transport_for_delivery(
            autonomy_run_id=new_ulid(),
            action_intent_id=None,
            contract_id=contract_id,
            delivery_id=delivery_id,
            planned_delivery_id=None,
            as_of_date="2026-02-23",
        )
        with self.repo.transaction() as conn:
            conn.execute(
                """
                UPDATE deliveries
                SET truck_no = 'MUTATED-TRUCK', driver_name = 'Mutated Driver', driver_phone = '000'
                WHERE delivery_id = ?
                """,
                (delivery_id,),
            )
        bundle = self.repo.get_delivery_bundle(delivery_id)
        self.assertEqual("TRK-001", bundle["truck_no"])
        self.assertEqual("Driver One", bundle["driver_name"])
        self.assertEqual("08011111111", bundle["driver_phone"])


if __name__ == "__main__":
    unittest.main()
