from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path

from adapters.sqlite_repo import SQLiteRepo
from core.config import RuntimeConfig
from core.ids import generate_pilot_uuid, new_ulid
from core.time import utc_now_iso_z


ENTITY_TABLES = [
    "parties",
    "contracts",
    "deliveries",
    "payments",
    "evidence_originals",
    "exception_cases",
]

PK_MAP = {
    "parties": "party_id",
    "contracts": "contract_id",
    "deliveries": "delivery_id",
    "payments": "payment_id",
    "evidence_originals": "evidence_id",
    "exception_cases": "exception_case_id",
}


class Phase1AIdentityBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.temp_dir = Path(tempfile.mkdtemp(prefix="phase1a-tests-"))
        shutil.copytree(self.repo_root / "config", self.temp_dir / "config")
        (self.temp_dir / ".state").mkdir(parents=True, exist_ok=True)
        (self.temp_dir / "output_v2").mkdir(parents=True, exist_ok=True)
        self.config = RuntimeConfig.load(self.temp_dir)
        self.repo = SQLiteRepo(self.config.state_dir / "drep.sqlite")
        self.repo.init_db(self.config)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # ------------------------------------------------------------------
    # Test 1: core_uuid column exists in every entity table after init_db
    # ------------------------------------------------------------------
    def test_core_uuid_column_present_in_all_entity_tables(self) -> None:
        with self.repo.transaction() as conn:
            for table in ENTITY_TABLES:
                cols = {
                    row["name"]
                    for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
                }
                self.assertIn(
                    "core_uuid",
                    cols,
                    f"core_uuid column missing from table '{table}'",
                )

    # ------------------------------------------------------------------
    # Test 2: _add_column_if_missing is idempotent (second init_db is safe)
    # ------------------------------------------------------------------
    def test_init_db_is_idempotent_core_uuid_not_duplicated(self) -> None:
        # Running init_db a second time must not raise or corrupt the schema.
        self.repo.init_db(self.config)
        with self.repo.transaction() as conn:
            for table in ENTITY_TABLES:
                pragma = conn.execute(f"PRAGMA table_info({table})").fetchall()
                uuid_cols = [row["name"] for row in pragma if row["name"] == "core_uuid"]
                self.assertEqual(
                    len(uuid_cols),
                    1,
                    f"Table '{table}' has {len(uuid_cols)} core_uuid columns after double init",
                )

    # ------------------------------------------------------------------
    # Test 3: backfill populates NULL core_uuid values on existing rows
    # ------------------------------------------------------------------
    def test_backfill_assigns_uuid_to_rows_with_null_core_uuid(self) -> None:
        now = utc_now_iso_z()
        party_id = new_ulid()

        # Insert a party without core_uuid using a raw connection bypassing generation.
        with self.repo.transaction() as conn:
            conn.execute(
                "INSERT INTO parties(party_id, legal_name, created_at, updated_at) "
                "VALUES(?, 'Test Backfill Party', ?, ?)",
                (party_id, now, now),
            )
            # Explicitly null it out (COALESCE in seed may have set it).
            conn.execute(
                "UPDATE parties SET core_uuid = NULL WHERE party_id = ?",
                (party_id,),
            )

        # Run backfill via _apply_schema_migrations path (second init_db call).
        self.repo.init_db(self.config)

        with self.repo.transaction() as conn:
            row = conn.execute(
                "SELECT core_uuid FROM parties WHERE party_id = ?",
                (party_id,),
            ).fetchone()
        self.assertIsNotNone(row["core_uuid"], "Backfill did not assign core_uuid")
        self._assert_valid_uuid4(row["core_uuid"], "backfilled core_uuid")

    # ------------------------------------------------------------------
    # Test 4: backfill is idempotent — existing UUIDs are never overwritten
    # ------------------------------------------------------------------
    def test_backfill_preserves_existing_core_uuids(self) -> None:
        now = utc_now_iso_z()
        party_id = new_ulid()
        original_uuid = generate_pilot_uuid()

        with self.repo.transaction() as conn:
            conn.execute(
                "INSERT INTO parties(party_id, legal_name, core_uuid, created_at, updated_at) "
                "VALUES(?, 'Preserve UUID Party', ?, ?, ?)",
                (party_id, original_uuid, now, now),
            )

        # Run init_db again (triggers backfill).
        self.repo.init_db(self.config)

        with self.repo.transaction() as conn:
            row = conn.execute(
                "SELECT core_uuid FROM parties WHERE party_id = ?",
                (party_id,),
            ).fetchone()
        self.assertEqual(
            row["core_uuid"],
            original_uuid,
            "Backfill overwrote an existing core_uuid",
        )

    # ------------------------------------------------------------------
    # Test 5: new contract creation includes a non-null core_uuid
    # ------------------------------------------------------------------
    def test_new_contract_has_core_uuid(self) -> None:
        from domain.services import Phase1Service

        service = Phase1Service(self.config, self.repo)
        service.init_db()

        payload = {
            "contract_ref": "LPO-TEST-001",
            "lpo_no": "TEST-001",
            "lpo_date": "2026-03-04",
            "buyer_id": "buyer_nycil",
            "vendor_of_record_id": "ananta_flows",
            "operator_id": "guildgate",
            "source_id": "ananta_flows",
            "processor_id": "processor_partner_refinery",
            "currency": "NGN",
            "issue_date": "2026-03-04",
            "due_date": "2026-03-18",
            "due_terms": "14 days",
            "expected_total_qty": 30000.0,
            "lines": [
                {
                    "product_code": "RBDSO",
                    "description": "Test line",
                    "expected_qty": 30000.0,
                    "unit": "kgs",
                    "unit_price": 2270.0,
                }
            ],
        }
        result = service.create_contract(payload, allow_placeholder_tin=True)
        contract_id = result["contract_id"]

        with self.repo.transaction() as conn:
            row = conn.execute(
                "SELECT core_uuid FROM contracts WHERE contract_id = ?",
                (contract_id,),
            ).fetchone()
        self.assertIsNotNone(row, "Contract row not found")
        self.assertIsNotNone(row["core_uuid"], "New contract missing core_uuid")
        self._assert_valid_uuid4(row["core_uuid"], "contract core_uuid")

    # ------------------------------------------------------------------
    # Test 6: generate_pilot_uuid returns a valid UUID v4
    # ------------------------------------------------------------------
    def test_generate_pilot_uuid_returns_valid_uuid4(self) -> None:
        for _ in range(20):
            uid = generate_pilot_uuid()
            self._assert_valid_uuid4(uid, "generate_pilot_uuid()")

    # ------------------------------------------------------------------
    # Test 7: generate_pilot_uuid produces unique values
    # ------------------------------------------------------------------
    def test_generate_pilot_uuid_values_are_unique(self) -> None:
        generated = {generate_pilot_uuid() for _ in range(100)}
        self.assertEqual(len(generated), 100, "UUID collisions detected")

    # ------------------------------------------------------------------
    # Helper
    # ------------------------------------------------------------------
    def _assert_valid_uuid4(self, value: str, label: str) -> None:
        try:
            parsed = uuid.UUID(value)
        except ValueError:
            self.fail(f"{label} is not a valid UUID: {value!r}")
        self.assertEqual(
            parsed.version,
            4,
            f"{label} is UUID version {parsed.version}, expected 4",
        )
