from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from adapters.sqlite_repo import SQLiteRepo
from core.config import RuntimeConfig
from core.ids import new_ulid
from core.time import utc_now_iso_z


class Phase2PR1FoundationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.temp_dir = Path(tempfile.mkdtemp(prefix="phase2-pr1-tests-"))
        shutil.copytree(self.repo_root / "config", self.temp_dir / "config")
        (self.temp_dir / ".state").mkdir(parents=True, exist_ok=True)
        (self.temp_dir / "output_v2").mkdir(parents=True, exist_ok=True)
        self.config = RuntimeConfig.load(self.temp_dir)
        self.repo = SQLiteRepo(self.config.state_dir / "drep.sqlite")
        self.repo.init_db(self.config)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_pr1_tables_indexes_and_triggers_exist_and_migrations_are_idempotent(self) -> None:
        # First init already completed in setUp; second init must be safe.
        self.repo.init_db(self.config)
        with self.repo.transaction() as conn:
            table_names = {
                row["name"]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
            }
            expected_tables = {
                "master_contracts",
                "contract_cycles",
                "policy_sets",
                "policy_overrides",
                "capacity_calendar",
                "gate_evaluations",
                "action_intents",
                "action_executions",
                "exception_cases",
                "human_decisions",
                "decision_features",
                "decision_outcomes",
                "event_log",
                "transport_partners",
                "transport_trucks",
                "transport_drivers",
                "truck_driver_assignments",
                "transport_compliance_docs",
                "transport_aliases",
                "delivery_transport_snapshot",
                "delivery_transport_suggestions",
                "users",
                "user_roles",
                "user_preferences",
            }
            self.assertTrue(expected_tables.issubset(table_names))

            index_names = {
                row["name"]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()
            }
            expected_indexes = {
                "idx_gate_eval_lookup",
                "idx_action_intent_lookup",
                "idx_exception_cases_status",
                "idx_truck_driver_effective",
                "idx_transport_alias_lookup",
                "idx_user_roles_user",
            }
            self.assertTrue(expected_indexes.issubset(index_names))

            trigger_names = {
                row["name"]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'").fetchall()
            }
            self.assertIn("trg_event_log_no_update", trigger_names)
            self.assertIn("trg_event_log_no_delete", trigger_names)

    def test_event_log_is_append_only(self) -> None:
        event_id = new_ulid()
        now = utc_now_iso_z()
        with self.repo.transaction() as conn:
            conn.execute(
                """
                INSERT INTO event_log(event_id, entity_type, entity_id, event_type, as_of_date, payload_json, source, created_at)
                VALUES(?, 'CONTRACT', 'contract-1', 'CREATED', '2026-02-27', '{}', 'test', ?)
                """,
                (event_id, now),
            )

        with self.assertRaises(sqlite3.DatabaseError):
            with self.repo.transaction() as conn:
                conn.execute("UPDATE event_log SET event_type = 'UPDATED' WHERE event_id = ?", (event_id,))

        with self.assertRaises(sqlite3.DatabaseError):
            with self.repo.transaction() as conn:
                conn.execute("DELETE FROM event_log WHERE event_id = ?", (event_id,))

    def test_action_intent_idempotency_key_unique(self) -> None:
        now = utc_now_iso_z()
        key = "contract-1|2026-03-01|materialize_due"
        with self.repo.transaction() as conn:
            conn.execute(
                """
                INSERT INTO action_intents(
                    action_intent_id, autonomy_run_id, intent_type, as_of_date, status, policy_version,
                    payload_json, idempotency_key, created_at, updated_at
                )
                VALUES(?, 'run-1', 'materialize_due', '2026-03-01', 'PENDING', 'v1', '{}', ?, ?, ?)
                """,
                (new_ulid(), key, now, now),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            with self.repo.transaction() as conn:
                conn.execute(
                    """
                    INSERT INTO action_intents(
                        action_intent_id, autonomy_run_id, intent_type, as_of_date, status, policy_version,
                        payload_json, idempotency_key, created_at, updated_at
                    )
                    VALUES(?, 'run-2', 'materialize_due', '2026-03-01', 'PENDING', 'v1', '{}', ?, ?, ?)
                    """,
                    (new_ulid(), key, now, now),
                )

    def test_effective_dated_transport_assignment_validity_check(self) -> None:
        now = utc_now_iso_z()
        partner_id = new_ulid()
        truck_id = new_ulid()
        driver_id = new_ulid()
        with self.repo.transaction() as conn:
            conn.execute(
                """
                INSERT INTO transport_partners(
                    transport_partner_id, legal_name, code, status, created_at, updated_at
                ) VALUES(?, 'Test Transport', 'TTP', 'ACTIVE', ?, ?)
                """,
                (partner_id, now, now),
            )
            conn.execute(
                """
                INSERT INTO transport_trucks(
                    transport_truck_id, transport_partner_id, truck_no, capacity_kg, status, created_at, updated_at
                ) VALUES(?, ?, 'TRK-001', 30000, 'ACTIVE', ?, ?)
                """,
                (truck_id, partner_id, now, now),
            )
            conn.execute(
                """
                INSERT INTO transport_drivers(
                    transport_driver_id, transport_partner_id, full_name, phone, status, created_at, updated_at
                ) VALUES(?, ?, 'Driver One', '08000000000', 'ACTIVE', ?, ?)
                """,
                (driver_id, partner_id, now, now),
            )

        with self.assertRaises(sqlite3.IntegrityError):
            with self.repo.transaction() as conn:
                conn.execute(
                    """
                    INSERT INTO truck_driver_assignments(
                        assignment_id, transport_truck_id, transport_driver_id, effective_from, effective_to,
                        is_primary, created_at, updated_at
                    )
                    VALUES(?, ?, ?, '2026-03-05', '2026-03-01', 1, ?, ?)
                    """,
                    (new_ulid(), truck_id, driver_id, now, now),
                )


if __name__ == "__main__":
    unittest.main()
