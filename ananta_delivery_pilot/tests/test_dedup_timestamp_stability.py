from __future__ import annotations

import shutil
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from adapters.sqlite_repo import SQLiteRepo
from core.config import RuntimeConfig
from core.ids import new_ulid
from core.time import utc_now_iso_z
from domain.event_ledger import IdempotencyConflictError


def _valid_terms_submitted_payload() -> dict:
    return {
        "trade_id": str(uuid.uuid4()),
        "payload": {
            "actor_org_id": str(uuid.uuid4()),
            "payment_terms": "NET30",
            "delivery_term": "DAP",
            "delivery_location": "Lagos, Nigeria",
        },
    }


class DedupTimestampStabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.temp_dir = Path(tempfile.mkdtemp(prefix="dedup-ts-tests-"))
        shutil.copytree(self.repo_root / "config", self.temp_dir / "config")
        (self.temp_dir / ".state").mkdir(parents=True, exist_ok=True)
        (self.temp_dir / "output_v2").mkdir(parents=True, exist_ok=True)
        self.config = RuntimeConfig.load(self.temp_dir)
        self.repo = SQLiteRepo(self.config.state_dir / "drep.sqlite")
        self.repo.init_db(self.config)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _insert_contract(self, lpo_state: str = "DRAFT") -> str:
        contract_id = new_ulid()
        now = utc_now_iso_z()
        with self.repo.transaction() as conn:
            conn.execute(
                """
                INSERT INTO contracts(
                    contract_id, contract_ref, lpo_no, lpo_date,
                    buyer_id, vendor_of_record_id, operator_id,
                    source_id, processor_id, lane, currency,
                    issue_date, due_date, due_terms,
                    expected_total_qty, expected_total_qty_kg,
                    expected_total_value, lpo_state, created_at, updated_at
                ) VALUES (
                    ?, 'LPO-DEDUP-TS', 'LPO-DEDUP-TS', '2026-01-01',
                    'buyer_nycil', 'ananta_flows', 'guildgate',
                    'ananta_flows', 'processor_partner_refinery', 'B', 'NGN',
                    '2026-01-01', '2026-03-01', '60 days',
                    30000.0, 30000000, 68100000.0, ?, ?, ?
                )
                """,
                (contract_id, lpo_state, now, now),
            )
        return contract_id

    def _insert_evidence(self, link_status: str = "UNLINKED") -> str:
        evidence_id = new_ulid()
        contract_id = self._insert_contract(lpo_state="DRAFT")
        now = utc_now_iso_z()
        with self.repo.transaction() as conn:
            conn.execute(
                """
                INSERT INTO evidence_originals(
                    evidence_id, contract_id, doc_type,
                    source_path, stored_path, sha256,
                    link_status, captured_at, created_at, updated_at
                ) VALUES (?, ?, 'other',
                    'test/source.pdf', 'test/stored.pdf',
                    'aabbcc0011223344aabbcc0011223344aabbcc0011223344aabbcc0011223344',
                    ?, ?, ?, ?)
                """,
                (evidence_id, contract_id, link_status, now, now, now),
            )
        return evidence_id

    def _contract_state(self, contract_id: str) -> tuple[str, str]:
        with self.repo.transaction() as conn:
            row = conn.execute(
                "SELECT lpo_state, updated_at FROM contracts WHERE contract_id = ?",
                (contract_id,),
            ).fetchone()
        return str(row["lpo_state"]), str(row["updated_at"])

    def _evidence_updated_at(self, evidence_id: str) -> str:
        with self.repo.transaction() as conn:
            row = conn.execute(
                "SELECT updated_at FROM evidence_originals WHERE evidence_id = ?",
                (evidence_id,),
            ).fetchone()
        return str(row["updated_at"])

    def _event_count_for_key(self, idempotency_key: str) -> int:
        with self.repo.transaction() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM event_log WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        return int(row["n"])

    def _snapshot_row_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        with self.repo.transaction() as conn:
            tables = [
                str(row["name"])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name ASC"
                ).fetchall()
            ]
            for table_name in tables:
                row = conn.execute(f"SELECT COUNT(*) AS n FROM {table_name}").fetchone()
                counts[table_name] = int(row["n"])
        return counts

    def test_dedup_retry_does_not_change_entity_timestamp(self) -> None:
        """Same idempotency_key + same payload → entity updated_at unchanged."""
        contract_id = self._insert_contract(lpo_state="DRAFT")
        payload = _valid_terms_submitted_payload()
        key = f"TERMS_SUBMITTED:{contract_id}:dedup-ts-entity"

        first = self.repo.apply_transition(
            event_type="TERMS_SUBMITTED",
            entity_type="contract",
            entity_id=contract_id,
            payload=payload,
            table="contracts",
            pk_column="contract_id",
            state_column="lpo_state",
            new_state="ACTIVE",
            idempotency_key=key,
        )
        _, updated_at_before = self._contract_state(contract_id)

        time.sleep(1.1)
        second = self.repo.apply_transition(
            event_type="TERMS_SUBMITTED",
            entity_type="contract",
            entity_id=contract_id,
            payload=payload,
            table="contracts",
            pk_column="contract_id",
            state_column="lpo_state",
            new_state="ACTIVE",
            idempotency_key=key,
        )
        _, updated_at_after = self._contract_state(contract_id)

        self.assertEqual(first["event_id"], second["event_id"])
        self.assertEqual(updated_at_before, updated_at_after)
        self.assertEqual(1, self._event_count_for_key(key))

    def test_dedup_retry_does_not_change_evidence_timestamp(self) -> None:
        """Same idempotency_key + same payload on PREP_EVIDENCE → updated_at unchanged."""
        evidence_id = self._insert_evidence(link_status="UNLINKED")
        payload = {"note": "dedup evidence payload"}
        key = f"DREP_EVIDENCE_ATTACHED:{evidence_id}:dedup-ts-evidence"

        first_updated_at = utc_now_iso_z()
        first = self.repo.apply_prep_evidence(
            event_type="DREP_EVIDENCE_ATTACHED",
            entity_type="evidence",
            entity_id=evidence_id,
            payload=payload,
            evidence_table="evidence_originals",
            evidence_pk_column="evidence_id",
            evidence_id=evidence_id,
            evidence_updates={"link_status": "LINKED", "updated_at": first_updated_at},
            idempotency_key=key,
        )
        updated_at_before = self._evidence_updated_at(evidence_id)

        time.sleep(1.1)
        second_updated_at = utc_now_iso_z()
        second = self.repo.apply_prep_evidence(
            event_type="DREP_EVIDENCE_ATTACHED",
            entity_type="evidence",
            entity_id=evidence_id,
            payload=payload,
            evidence_table="evidence_originals",
            evidence_pk_column="evidence_id",
            evidence_id=evidence_id,
            evidence_updates={"link_status": "LINKED", "updated_at": second_updated_at},
            idempotency_key=key,
        )
        updated_at_after = self._evidence_updated_at(evidence_id)

        self.assertEqual(first["event_id"], second["event_id"])
        self.assertEqual(updated_at_before, updated_at_after)
        self.assertEqual(1, self._event_count_for_key(key))

    def test_conflict_does_not_change_entity_state_or_timestamp(self) -> None:
        """Same key + different payload → state and updated_at unchanged after conflict."""
        contract_id = self._insert_contract(lpo_state="DRAFT")
        key = f"TERMS_SUBMITTED:{contract_id}:dedup-ts-conflict"

        payload_a = _valid_terms_submitted_payload()
        self.repo.apply_transition(
            event_type="TERMS_SUBMITTED",
            entity_type="contract",
            entity_id=contract_id,
            payload=payload_a,
            table="contracts",
            pk_column="contract_id",
            state_column="lpo_state",
            new_state="ACTIVE",
            idempotency_key=key,
        )
        state_before, updated_at_before = self._contract_state(contract_id)

        time.sleep(1.1)
        payload_b = _valid_terms_submitted_payload()
        with self.assertRaises(IdempotencyConflictError):
            self.repo.apply_transition(
                event_type="TERMS_SUBMITTED",
                entity_type="contract",
                entity_id=contract_id,
                payload=payload_b,
                table="contracts",
                pk_column="contract_id",
                state_column="lpo_state",
                new_state="EXPIRED",
                idempotency_key=key,
            )
        state_after, updated_at_after = self._contract_state(contract_id)

        self.assertEqual(state_before, state_after)
        self.assertEqual(updated_at_before, updated_at_after)
        self.assertEqual(1, self._event_count_for_key(key))

    def test_multiple_dedup_retries_zero_cumulative_drift(self) -> None:
        """10 retries of same idempotent call → updated_at must stay identical."""
        contract_id = self._insert_contract(lpo_state="DRAFT")
        payload = _valid_terms_submitted_payload()
        key = f"TERMS_SUBMITTED:{contract_id}:dedup-ts-multi"

        self.repo.apply_transition(
            event_type="TERMS_SUBMITTED",
            entity_type="contract",
            entity_id=contract_id,
            payload=payload,
            table="contracts",
            pk_column="contract_id",
            state_column="lpo_state",
            new_state="ACTIVE",
            idempotency_key=key,
        )
        _, baseline_updated_at = self._contract_state(contract_id)

        time.sleep(1.1)
        for _ in range(10):
            self.repo.apply_transition(
                event_type="TERMS_SUBMITTED",
                entity_type="contract",
                entity_id=contract_id,
                payload=payload,
                table="contracts",
                pk_column="contract_id",
                state_column="lpo_state",
                new_state="ACTIVE",
                idempotency_key=key,
            )
        _, final_updated_at = self._contract_state(contract_id)

        self.assertEqual(baseline_updated_at, final_updated_at)
        self.assertEqual(1, self._event_count_for_key(key))

    def test_dedup_does_not_create_extra_rows_anywhere(self) -> None:
        """Dedup retry creates zero new rows in any table."""
        contract_id = self._insert_contract(lpo_state="DRAFT")
        payload = _valid_terms_submitted_payload()
        key = f"TERMS_SUBMITTED:{contract_id}:dedup-ts-counts"

        self.repo.apply_transition(
            event_type="TERMS_SUBMITTED",
            entity_type="contract",
            entity_id=contract_id,
            payload=payload,
            table="contracts",
            pk_column="contract_id",
            state_column="lpo_state",
            new_state="ACTIVE",
            idempotency_key=key,
        )
        counts_after_first = self._snapshot_row_counts()

        time.sleep(1.1)
        self.repo.apply_transition(
            event_type="TERMS_SUBMITTED",
            entity_type="contract",
            entity_id=contract_id,
            payload=payload,
            table="contracts",
            pk_column="contract_id",
            state_column="lpo_state",
            new_state="ACTIVE",
            idempotency_key=key,
        )
        counts_after_retry = self._snapshot_row_counts()

        self.assertEqual(counts_after_first, counts_after_retry)


if __name__ == "__main__":
    unittest.main()

