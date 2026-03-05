"""Phase 1C — Event ledger + merchant proof harness.

9 verification tests covering:
  1. TRANSITION success
  2. TRANSITION schema failure
  3. PREP_EVIDENCE success
  4. PREP_EVIDENCE schema failure
  5. Idempotency safe dedup
  6. Idempotency conflict
  7. Content hash determinism
  8. Replay obligations recorded
  9. Per-event catalog identity
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
import uuid
from pathlib import Path

from adapters.sqlite_repo import SQLiteRepo
from core.config import RuntimeConfig
from core.ids import new_ulid
from core.time import utc_now_iso_z
from domain.event_ledger import (
    EnvelopeValidator,
    IdempotencyConflictError,
    SchemaValidationError,
    canonical_hash,
)


def _valid_terms_submitted_payload() -> dict:
    """Minimal valid TERMS_SUBMITTED payload according to core_event_requirements."""
    return {
        "trade_id": str(uuid.uuid4()),
        "payload": {
            "actor_org_id": str(uuid.uuid4()),
            "payment_terms": "NET30",
            "delivery_term": "DAP",
            "delivery_location": "Lagos, Nigeria",
        },
    }


class Phase1CEventLedgerTests(unittest.TestCase):

    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.temp_dir = Path(tempfile.mkdtemp(prefix="phase1c-tests-"))
        shutil.copytree(self.repo_root / "config", self.temp_dir / "config")
        (self.temp_dir / ".state").mkdir(parents=True, exist_ok=True)
        (self.temp_dir / "output_v2").mkdir(parents=True, exist_ok=True)
        self.config = RuntimeConfig.load(self.temp_dir)
        self.repo = SQLiteRepo(self.config.state_dir / "drep.sqlite")
        self.repo.init_db(self.config)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # ------------------------------------------------------------------
    # Helper: insert a bare contract row for state-mutation tests
    # ------------------------------------------------------------------
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
                    expected_total_value,
                    lpo_state, created_at, updated_at
                ) VALUES (
                    ?, 'LPO-TEST-1C', 'LPO-1C-001', '2026-01-01',
                    'buyer_nycil', 'ananta_flows', 'guildgate',
                    'ananta_flows', 'processor_partner_refinery', 'B', 'NGN',
                    '2026-01-01', '2026-03-01', '60 days',
                    30000.0, 30000000, 68100000.0,
                    ?, ?, ?
                )
                """,
                (contract_id, lpo_state, now, now),
            )
        return contract_id

    # ------------------------------------------------------------------
    # Helper: insert a bare evidence_originals row
    # ------------------------------------------------------------------
    def _insert_evidence(self, link_status: str = "UNLINKED") -> str:
        evidence_id = new_ulid()
        now = utc_now_iso_z()
        contract_id = self._insert_contract()
        with self.repo.transaction() as conn:
            conn.execute(
                """
                INSERT INTO evidence_originals(
                    evidence_id, contract_id, doc_type,
                    source_path, stored_path, sha256,
                    link_status, captured_at, created_at
                ) VALUES (?, ?, 'other',
                    'test/placeholder.pdf', 'test/placeholder.pdf',
                    'aabbcc0011223344aabbcc0011223344aabbcc0011223344aabbcc0011223344',
                    ?, ?, ?)
                """,
                (evidence_id, contract_id, link_status, now, now),
            )
        return evidence_id

    # ------------------------------------------------------------------
    # Test 1: TRANSITION success — event persisted + state mutated atomically
    # ------------------------------------------------------------------
    def test_transition_success_event_persisted_and_state_mutated(self) -> None:
        contract_id = self._insert_contract(lpo_state="DRAFT")
        payload = _valid_terms_submitted_payload()

        result = self.repo.apply_transition(
            event_type="TERMS_SUBMITTED",
            entity_type="contract",
            entity_id=contract_id,
            payload=payload,
            table="contracts",
            pk_column="contract_id",
            state_column="lpo_state",
            new_state="ACTIVE",
            idempotency_key=f"TERMS_SUBMITTED:{contract_id}:test-001",
        )

        self.assertIn("event_id", result)
        self.assertFalse(result.get("deduped"))

        # Verify entity state was mutated
        with self.repo.transaction() as conn:
            row = conn.execute(
                "SELECT lpo_state FROM contracts WHERE contract_id = ?",
                (contract_id,),
            ).fetchone()
            ev = conn.execute(
                "SELECT event_type, content_hash FROM event_log WHERE event_id = ?",
                (result["event_id"],),
            ).fetchone()

        self.assertEqual(row["lpo_state"], "ACTIVE")
        self.assertIsNotNone(ev)
        self.assertEqual(ev["event_type"], "TERMS_SUBMITTED")
        self.assertIsNotNone(ev["content_hash"])

    # ------------------------------------------------------------------
    # Test 2: TRANSITION schema failure — rollback, JSONL written, state unchanged
    # ------------------------------------------------------------------
    def test_transition_schema_failure_rollback_jsonl_state_unchanged(self) -> None:
        contract_id = self._insert_contract(lpo_state="DRAFT")
        bad_payload: dict = {
            # trade_id deliberately omitted → required field missing
            "payload": {
                "actor_org_id": str(uuid.uuid4()),
                "payment_terms": "NET30",
                "delivery_term": "DAP",
                "delivery_location": "Lagos",
            }
        }

        with self.assertRaises(SchemaValidationError) as ctx:
            self.repo.apply_transition(
                event_type="TERMS_SUBMITTED",
                entity_type="contract",
                entity_id=contract_id,
                payload=bad_payload,
                table="contracts",
                pk_column="contract_id",
                state_column="lpo_state",
                new_state="ACTIVE",
                idempotency_key=f"TERMS_SUBMITTED:{contract_id}:test-002",
            )

        self.assertTrue(len(ctx.exception.errors) > 0)

        # State must remain unchanged
        with self.repo.transaction() as conn:
            row = conn.execute(
                "SELECT lpo_state FROM contracts WHERE contract_id = ?",
                (contract_id,),
            ).fetchone()
            count = conn.execute(
                "SELECT COUNT(*) as n FROM event_log WHERE entity_id = ?",
                (contract_id,),
            ).fetchone()

        self.assertEqual(row["lpo_state"], "DRAFT")
        self.assertEqual(count["n"], 0, "No event should be written on schema failure")

        # JSONL log must exist
        conflicts_path = self.repo.db_path.parent / "conflicts.jsonl"
        self.assertTrue(conflicts_path.exists(), "conflicts.jsonl not written")
        entries = [json.loads(line) for line in conflicts_path.read_text().splitlines() if line]
        self.assertTrue(
            any(e.get("event_type") == "TERMS_SUBMITTED" for e in entries)
        )

    # ------------------------------------------------------------------
    # Test 3: PREP_EVIDENCE success — evidence mutated + event persisted atomically
    # ------------------------------------------------------------------
    def test_prep_evidence_success_evidence_mutated_and_event_persisted(self) -> None:
        evidence_id = self._insert_evidence(link_status="UNLINKED")
        # Use a pilot-internal event type (not in catalog) → no required fields → passes
        payload = {"note": "evidence linked by operator"}

        result = self.repo.apply_prep_evidence(
            event_type="DREP_EVIDENCE_ATTACHED",
            entity_type="evidence",
            entity_id=evidence_id,
            payload=payload,
            evidence_table="evidence_originals",
            evidence_pk_column="evidence_id",
            evidence_id=evidence_id,
            evidence_updates={"link_status": "LINKED"},
            idempotency_key=f"DREP_EVIDENCE_ATTACHED:{evidence_id}:test-003",
        )

        self.assertIn("event_id", result)
        self.assertFalse(result.get("deduped"))

        with self.repo.transaction() as conn:
            row = conn.execute(
                "SELECT link_status FROM evidence_originals WHERE evidence_id = ?",
                (evidence_id,),
            ).fetchone()
            ev = conn.execute(
                "SELECT event_type FROM event_log WHERE event_id = ?",
                (result["event_id"],),
            ).fetchone()

        self.assertEqual(row["link_status"], "LINKED")
        self.assertIsNotNone(ev)
        self.assertEqual(ev["event_type"], "DREP_EVIDENCE_ATTACHED")

    # ------------------------------------------------------------------
    # Test 4: PREP_EVIDENCE schema failure — rollback, JSONL written, evidence unchanged
    # ------------------------------------------------------------------
    def test_prep_evidence_schema_failure_rollback_jsonl_evidence_unchanged(self) -> None:
        evidence_id = self._insert_evidence(link_status="UNLINKED")
        bad_payload: dict = {
            # EVIDENCE_SUBMITTED requires trade_id — omit it
            "payload": {"scope": "TRADE", "url": "https://example.com/doc.pdf"}
        }

        with self.assertRaises(SchemaValidationError) as ctx:
            self.repo.apply_prep_evidence(
                event_type="EVIDENCE_SUBMITTED",
                entity_type="evidence",
                entity_id=evidence_id,
                payload=bad_payload,
                evidence_table="evidence_originals",
                evidence_pk_column="evidence_id",
                evidence_id=evidence_id,
                evidence_updates={"link_status": "LINKED"},
                idempotency_key=f"EVIDENCE_SUBMITTED:{evidence_id}:test-004",
            )

        self.assertTrue(len(ctx.exception.errors) > 0)

        # Evidence must remain UNLINKED
        with self.repo.transaction() as conn:
            row = conn.execute(
                "SELECT link_status FROM evidence_originals WHERE evidence_id = ?",
                (evidence_id,),
            ).fetchone()
            count = conn.execute(
                "SELECT COUNT(*) as n FROM event_log WHERE entity_id = ?",
                (evidence_id,),
            ).fetchone()

        self.assertEqual(row["link_status"], "UNLINKED")
        self.assertEqual(count["n"], 0)

        # JSONL log must contain this rejection
        conflicts_path = self.repo.db_path.parent / "conflicts.jsonl"
        self.assertTrue(conflicts_path.exists())
        entries = [json.loads(line) for line in conflicts_path.read_text().splitlines() if line]
        self.assertTrue(
            any(e.get("event_type") == "EVIDENCE_SUBMITTED" for e in entries)
        )

    # ------------------------------------------------------------------
    # Test 5: Idempotency safe dedup — _check_idempotency returns "dedup" for same key+hash
    # ------------------------------------------------------------------
    def test_idempotency_safe_dedup_returns_existing_event(self) -> None:
        entity_id = new_ulid()
        payload = {"data": "test_payload_for_dedup"}
        c_hash = canonical_hash(payload)
        idem_key = f"DEDUP_TEST:{entity_id}:{c_hash}"

        # First write via _append_validated_event
        with self.repo.transaction() as conn:
            result1 = self.repo._append_validated_event(
                conn,
                event_type="DEDUP_TEST",
                entity_type="test",
                entity_id=entity_id,
                payload=payload,
                idempotency_key=idem_key,
                content_hash=c_hash,
                validated_against_ref="test-ref",
                validated_against_hash="test-hash",
            )

        # _check_idempotency must report "dedup" for same key + same hash
        status, val = self.repo._check_idempotency(idem_key, c_hash)
        self.assertEqual(status, "dedup")
        self.assertEqual(val, result1["event_id"])

        # _get_event_by_id returns same event_id with deduped=True
        result2 = self.repo._get_event_by_id(val)
        self.assertEqual(result1["event_id"], result2["event_id"])
        self.assertTrue(result2.get("deduped"))

        # Only 1 row in event_log
        with self.repo.transaction() as conn:
            count = conn.execute(
                "SELECT COUNT(*) as n FROM event_log WHERE idempotency_key = ?",
                (idem_key,),
            ).fetchone()
        self.assertEqual(count["n"], 1)

    # ------------------------------------------------------------------
    # Test 6: Idempotency conflict — _check_idempotency returns "conflict" for same key+different hash
    # ------------------------------------------------------------------
    def test_idempotency_conflict_raises_and_logs_jsonl(self) -> None:
        entity_id = new_ulid()
        idem_key = f"CONFLICT_TEST:{entity_id}:fixed_key"
        hash_1 = canonical_hash({"data": "payload_version_1"})
        hash_2 = canonical_hash({"data": "payload_version_2"})

        # First write with hash_1
        with self.repo.transaction() as conn:
            self.repo._append_validated_event(
                conn,
                event_type="CONFLICT_TEST",
                entity_type="test",
                entity_id=entity_id,
                payload={"data": "payload_version_1"},
                idempotency_key=idem_key,
                content_hash=hash_1,
                validated_against_ref="test-ref",
                validated_against_hash="test-hash",
            )

        # _check_idempotency must report "conflict" for same key + different hash
        status, val = self.repo._check_idempotency(idem_key, hash_2)
        self.assertEqual(status, "conflict")
        self.assertEqual(val, hash_1)  # val is the existing hash on conflict

        # Still only 1 row
        with self.repo.transaction() as conn:
            count = conn.execute(
                "SELECT COUNT(*) as n FROM event_log WHERE idempotency_key = ?",
                (idem_key,),
            ).fetchone()
        self.assertEqual(count["n"], 1)

    # ------------------------------------------------------------------
    # Test 7: Content hash determinism — same envelope → same hash (100 runs)
    # ------------------------------------------------------------------
    def test_content_hash_is_deterministic(self) -> None:
        envelope = {
            "trade_id": "aaa-bbb-ccc",
            "payload": {"payment_terms": "NET30", "amount": 50000, "currency": "NGN"},
            "meta": {"source": "pilot", "version": 1},
        }
        expected = canonical_hash(envelope)
        for i in range(99):
            self.assertEqual(
                canonical_hash(envelope),
                expected,
                f"canonical_hash produced different result on iteration {i + 2}",
            )

    # ------------------------------------------------------------------
    # Test 8: Replay obligations recorded correctly
    # ------------------------------------------------------------------
    def test_replay_obligations_recorded_on_event(self) -> None:
        # GATE_A_AUTHORIZED has replay_obligations: ["BUYER_CONFIRMED", "SELLER_CONFIRMED"]
        entity_id = new_ulid()
        payload = {"trade_id": str(uuid.uuid4())}
        c_hash = canonical_hash(payload)
        idem_key = f"GATE_A_AUTHORIZED:{entity_id}:{c_hash}"

        # Get obligations from the actual catalog
        validator = self.repo._validator
        self.assertIsNotNone(validator)
        expected_obligations = validator._catalog.get("GATE_A_AUTHORIZED", {}).get(
            "replay_obligations", []
        )

        with self.repo.transaction() as conn:
            result = self.repo._append_validated_event(
                conn,
                event_type="GATE_A_AUTHORIZED",
                entity_type="trade",
                entity_id=entity_id,
                payload=payload,
                idempotency_key=idem_key,
                content_hash=c_hash,
                validated_against_ref=validator.core_requirements_ref,
                validated_against_hash=validator.core_requirements_hash,
                replay_obligations=expected_obligations,
            )

        with self.repo.transaction() as conn:
            row = conn.execute(
                "SELECT replay_obligations FROM event_log WHERE event_id = ?",
                (result["event_id"],),
            ).fetchone()

        stored = json.loads(row["replay_obligations"])
        self.assertEqual(stored, expected_obligations)
        # Specifically for GATE_A_AUTHORIZED this must include BUYER_CONFIRMED and SELLER_CONFIRMED
        self.assertTrue(
            any("BUYER_CONFIRMED" in str(ob) for ob in stored),
            f"Expected BUYER_CONFIRMED in obligations, got: {stored}",
        )

    # ------------------------------------------------------------------
    # Test 9: Per-event catalog identity — validated_against_ref + hash populated
    # ------------------------------------------------------------------
    def test_per_event_catalog_identity_populated(self) -> None:
        contract_id = self._insert_contract(lpo_state="DRAFT")
        payload = _valid_terms_submitted_payload()

        result = self.repo.apply_transition(
            event_type="TERMS_SUBMITTED",
            entity_type="contract",
            entity_id=contract_id,
            payload=payload,
            table="contracts",
            pk_column="contract_id",
            state_column="lpo_state",
            new_state="ACTIVE",
            idempotency_key=f"TERMS_SUBMITTED:{contract_id}:test-009",
        )

        with self.repo.transaction() as conn:
            row = conn.execute(
                "SELECT validated_against_ref, validated_against_hash "
                "FROM event_log WHERE event_id = ?",
                (result["event_id"],),
            ).fetchone()

        validator = self.repo._validator
        self.assertIsNotNone(row["validated_against_ref"])
        self.assertIsNotNone(row["validated_against_hash"])
        self.assertEqual(row["validated_against_ref"], validator.core_requirements_ref)
        self.assertEqual(
            row["validated_against_hash"], validator.core_requirements_hash
        )
        # Must not be empty strings
        self.assertTrue(len(row["validated_against_ref"]) > 0)
        self.assertTrue(len(row["validated_against_hash"]) > 0)

    # ------------------------------------------------------------------
    # Test 10: Idempotency dedup via public API — same key + same payload → dedup
    # ------------------------------------------------------------------
    def test_idempotency_dedup_via_public_api(self) -> None:
        """Same idempotency_key + same payload → return existing event (dedup)."""
        contract_id = self._insert_contract(lpo_state="DRAFT")
        payload = _valid_terms_submitted_payload()
        key = f"TERMS_SUBMITTED:{contract_id}:test-idem-dedup"

        result1 = self.repo.apply_transition(
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

        # Second call with same key and same payload → dedup
        result2 = self.repo.apply_transition(
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

        self.assertEqual(result1["event_id"], result2["event_id"])
        self.assertFalse(result1.get("deduped"))
        self.assertTrue(result2.get("deduped"))

        # Only 1 row in event_log for this key
        with self.repo.transaction() as conn:
            count = conn.execute(
                "SELECT COUNT(*) as n FROM event_log WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
        self.assertEqual(count["n"], 1)

    # ------------------------------------------------------------------
    # Test 11: Idempotency conflict via public API — same key + different payload → error + JSONL
    # ------------------------------------------------------------------
    def test_idempotency_conflict_via_public_api(self) -> None:
        """Same idempotency_key + different payload → IdempotencyConflictError + JSONL written."""
        contract_id_a = self._insert_contract(lpo_state="DRAFT")
        contract_id_b = self._insert_contract(lpo_state="DRAFT")
        key = f"TERMS_SUBMITTED:shared-key:test-idem-conflict"

        payload_a = _valid_terms_submitted_payload()
        payload_b = _valid_terms_submitted_payload()  # different UUID in trade_id

        # First call with contract_id_a
        self.repo.apply_transition(
            event_type="TERMS_SUBMITTED",
            entity_type="contract",
            entity_id=contract_id_a,
            payload=payload_a,
            table="contracts",
            pk_column="contract_id",
            state_column="lpo_state",
            new_state="ACTIVE",
            idempotency_key=key,
        )

        # Second call: same key, different payload → IdempotencyConflictError
        with self.assertRaises(IdempotencyConflictError) as ctx:
            self.repo.apply_transition(
                event_type="TERMS_SUBMITTED",
                entity_type="contract",
                entity_id=contract_id_b,
                payload=payload_b,
                table="contracts",
                pk_column="contract_id",
                state_column="lpo_state",
                new_state="ACTIVE",
                idempotency_key=key,
            )

        err = ctx.exception
        self.assertEqual(err.idempotency_key, key)

        # JSONL conflict log must be written (outside transaction)
        conflicts_path = self.repo.db_path.parent / "conflicts.jsonl"
        self.assertTrue(conflicts_path.exists(), "conflicts.jsonl not written")
        entries = [json.loads(line) for line in conflicts_path.read_text().splitlines() if line]
        conflict_entries = [e for e in entries if e.get("idempotency_key") == key]
        self.assertEqual(len(conflict_entries), 1)
        self.assertEqual(conflict_entries[0]["existing_content_hash"], err.existing_hash)
        self.assertEqual(conflict_entries[0]["new_content_hash"], err.new_hash)

    # ------------------------------------------------------------------
    # Test 12: Phantom event prevention — nonexistent entity_id raises, no event appended
    # ------------------------------------------------------------------
    def test_transition_nonexistent_entity_raises_no_phantom_event(self) -> None:
        """apply_transition with bad entity_id → ValueError, no event in event_log."""
        nonexistent_id = "nonexistent-id-999"
        payload = _valid_terms_submitted_payload()

        with self.assertRaises(ValueError, msg="Expected ValueError for missing entity"):
            self.repo.apply_transition(
                event_type="TERMS_SUBMITTED",
                entity_type="contract",
                entity_id=nonexistent_id,
                payload=payload,
                table="contracts",
                pk_column="contract_id",
                state_column="lpo_state",
                new_state="ACTIVE",
                idempotency_key=f"TERMS_SUBMITTED:{nonexistent_id}:test-phantom",
            )

        # Verify no phantom event was appended
        with self.repo.transaction() as conn:
            events = conn.execute(
                "SELECT * FROM event_log WHERE entity_id = ?",
                (nonexistent_id,),
            ).fetchall()
        self.assertEqual(len(events), 0, "No event should be appended for a nonexistent entity")

    # ------------------------------------------------------------------
    # Test 13: Dedup causes zero truth table writes — updated_at unchanged
    # ------------------------------------------------------------------
    def test_dedup_causes_zero_truth_table_writes(self) -> None:
        """On idempotent retry, entity updated_at must not change and no new event appended."""
        import time
        contract_id = self._insert_contract(lpo_state="DRAFT")
        payload = _valid_terms_submitted_payload()
        key = f"TERMS_SUBMITTED:{contract_id}:dedup-zero-write"

        # First call — legitimate write
        result1 = self.repo.apply_transition(
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

        # Record updated_at after first call
        with self.repo.transaction() as conn:
            before = conn.execute(
                "SELECT updated_at FROM contracts WHERE contract_id = ?",
                (contract_id,),
            ).fetchone()["updated_at"]

        time.sleep(1)  # ensure clock advances so any spurious write would change updated_at

        # Second call — same key + same payload → dedup
        result2 = self.repo.apply_transition(
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

        self.assertEqual(result1["event_id"], result2["event_id"])
        self.assertTrue(result2.get("deduped"))

        with self.repo.transaction() as conn:
            after = conn.execute(
                "SELECT updated_at FROM contracts WHERE contract_id = ?",
                (contract_id,),
            ).fetchone()["updated_at"]
            count = conn.execute(
                "SELECT COUNT(*) as n FROM event_log WHERE idempotency_key = ?",
                (key,),
            ).fetchone()["n"]

        self.assertEqual(before, after, "updated_at must not change on dedup retry")
        self.assertEqual(count, 1, "Only 1 event_log row for deduplicated key")

    # ------------------------------------------------------------------
    # Test 14: Conflict causes zero truth table writes — entity state unchanged
    # ------------------------------------------------------------------
    def test_conflict_causes_zero_truth_table_writes(self) -> None:
        """On idempotency conflict, entity state and updated_at must not change."""
        contract_id = self._insert_contract(lpo_state="DRAFT")
        key = f"TERMS_SUBMITTED:{contract_id}:conflict-zero-write"

        payload_a = _valid_terms_submitted_payload()
        payload_b = _valid_terms_submitted_payload()  # different UUID → different hash

        # First call
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

        # Record state after first call
        with self.repo.transaction() as conn:
            snap = conn.execute(
                "SELECT lpo_state, updated_at FROM contracts WHERE contract_id = ?",
                (contract_id,),
            ).fetchone()
        state_after_first = snap["lpo_state"]
        updated_at_after_first = snap["updated_at"]

        # Second call: same key, different payload → conflict
        with self.assertRaises(IdempotencyConflictError):
            self.repo.apply_transition(
                event_type="TERMS_SUBMITTED",
                entity_type="contract",
                entity_id=contract_id,
                payload=payload_b,
                table="contracts",
                pk_column="contract_id",
                state_column="lpo_state",
                new_state="AUTHORIZED",
                idempotency_key=key,
            )

        with self.repo.transaction() as conn:
            snap2 = conn.execute(
                "SELECT lpo_state, updated_at FROM contracts WHERE contract_id = ?",
                (contract_id,),
            ).fetchone()
            count = conn.execute(
                "SELECT COUNT(*) as n FROM event_log WHERE idempotency_key = ?",
                (key,),
            ).fetchone()["n"]

        self.assertEqual(snap2["lpo_state"], state_after_first, "state must not change on conflict")
        self.assertEqual(snap2["updated_at"], updated_at_after_first, "updated_at must not change on conflict")
        self.assertEqual(count, 1, "event_log count must remain 1 after conflict")

        # Conflict JSONL must be written
        conflicts_path = self.repo.db_path.parent / "conflicts.jsonl"
        self.assertTrue(conflicts_path.exists())
        entries = [json.loads(line) for line in conflicts_path.read_text().splitlines() if line]
        self.assertTrue(any(e.get("idempotency_key") == key for e in entries))


# ===========================================================================
# Validator Explainability tests (tests 15–20)
# ===========================================================================

class ValidatorExplainabilityTests(unittest.TestCase):
    """Tests for ValidationExplanation + event_validation_log (Part 1 & 2)."""

    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.temp_dir = Path(tempfile.mkdtemp(prefix="explain-tests-"))
        shutil.copytree(self.repo_root / "config", self.temp_dir / "config")
        (self.temp_dir / ".state").mkdir(parents=True, exist_ok=True)
        self.config = RuntimeConfig.load(self.temp_dir)
        self.repo = SQLiteRepo(self.config.state_dir / "drep.sqlite")
        self.repo.init_db(self.config)
        self.validator = self.repo._validator

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _insert_contract(self) -> str:
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
                    expected_total_value,
                    lpo_state, created_at, updated_at
                ) VALUES (
                    ?, 'LPO-EXP-1', 'LPO-EXP-001', '2026-01-01',
                    'buyer_nycil', 'ananta_flows', 'guildgate',
                    'ananta_flows', 'processor_partner_refinery', 'B', 'NGN',
                    '2026-01-01', '2026-03-01', '60 days',
                    30000.0, 30000000, 68100000.0,
                    'ACTIVE', ?, ?
                )
                """,
                (contract_id, now, now),
            )
        return contract_id

    # ------------------------------------------------------------------
    # Test 15: Validator populates fields_missing for incomplete payload
    # ------------------------------------------------------------------
    def test_15_explanation_fields_missing(self) -> None:
        """validate() with missing required fields → explanation.fields_missing non-empty."""
        from domain.event_ledger import ValidationExplanation
        result = self.validator.validate("TERMS_SUBMITTED", {})
        self.assertFalse(result.valid)
        self.assertIsNotNone(result.explanation)
        exp = result.explanation
        self.assertIsInstance(exp, ValidationExplanation)
        self.assertGreater(len(exp.fields_missing), 0)
        self.assertIn("trade_id", exp.fields_missing)
        self.assertEqual(exp.schema_ok_reason, "missing_fields")
        self.assertTrue(exp.catalog_matched)
        self.assertEqual(exp.validator_version, "validator_v1")

    # ------------------------------------------------------------------
    # Test 16: Validator populates fields_invalid_format for bad UUID
    # ------------------------------------------------------------------
    def test_16_explanation_fields_invalid_format(self) -> None:
        """validate() with non-UUID trade_id → explanation.fields_invalid_format populated."""
        payload = {
            "trade_id": "not-a-uuid",
            "payload": {
                "actor_org_id": str(uuid.uuid4()),
                "payment_terms": "NET30",
                "delivery_term": "DAP",
                "delivery_location": "Lagos",
            },
        }
        result = self.validator.validate("TERMS_SUBMITTED", payload)
        self.assertFalse(result.valid)
        exp = result.explanation
        self.assertIsNotNone(exp)
        self.assertGreater(len(exp.fields_invalid_format), 0)
        field_names = [f["field"] for f in exp.fields_invalid_format]
        self.assertIn("trade_id", field_names)
        inv = next(f for f in exp.fields_invalid_format if f["field"] == "trade_id")
        self.assertEqual(inv["expected"], "uuid")
        self.assertEqual(inv["got"], "not-a-uuid")
        self.assertEqual(exp.schema_ok_reason, "format_errors")

    # ------------------------------------------------------------------
    # Test 17: Validator populates fields_unexpected for extra fields
    # ------------------------------------------------------------------
    def test_17_explanation_fields_unexpected(self) -> None:
        """Fields in payload not in catalog appear in explanation.fields_unexpected."""
        payload = {
            "trade_id": str(uuid.uuid4()),
            "mystery_field": "unexpected_value",
            "payload": {
                "actor_org_id": str(uuid.uuid4()),
                "payment_terms": "NET30",
                "delivery_term": "DAP",
                "delivery_location": "Lagos",
            },
        }
        result = self.validator.validate("TERMS_SUBMITTED", payload)
        exp = result.explanation
        self.assertIsNotNone(exp)
        self.assertIn("mystery_field", exp.fields_unexpected)

    # ------------------------------------------------------------------
    # Test 18: schema_ok=0 event has explanation in event_validation_log
    # ------------------------------------------------------------------
    def test_18_schema_ok_0_explanation_persisted(self) -> None:
        """Unknown event_type → schema_ok=0 → explanation written to event_validation_log."""
        contract_id = self._insert_contract()
        result = self.repo.apply_transition(
            event_type="PILOT:UNKNOWN_TYPE",   # not in catalog → schema_ok=0
            entity_type="trade",
            entity_id=contract_id,
            payload={"note": "unknown"},
            table="contracts",
            pk_column="contract_id",
            state_column="lpo_state",
            new_state="ACTIVE",
            idempotency_key=f"explain:unknown:{contract_id}",
        )
        event_id = result["event_id"]

        # event_log must show schema_ok=0
        conn = self.repo._connect()
        try:
            erow = conn.execute(
                "SELECT schema_ok FROM event_log WHERE event_id = ?", (event_id,)
            ).fetchone()
            vrow = conn.execute(
                "SELECT validator_version, catalog_hash, explanation_json "
                "FROM event_validation_log WHERE event_id = ?", (event_id,)
            ).fetchone()
        finally:
            conn.close()

        self.assertEqual(erow["schema_ok"], 0)
        self.assertIsNotNone(vrow, "event_validation_log must have a row for schema_ok=0 event")
        exp = json.loads(vrow["explanation_json"])
        self.assertEqual(exp["schema_ok_reason"], "not_in_catalog")
        self.assertFalse(exp["catalog_matched"])
        self.assertEqual(vrow["validator_version"], "validator_v1")
        self.assertIsNotNone(vrow["catalog_hash"])

    # ------------------------------------------------------------------
    # Test 19: schema_ok=1 event has NO row in event_validation_log
    # ------------------------------------------------------------------
    def test_19_schema_ok_1_no_validation_log(self) -> None:
        """Valid event → schema_ok=1 → NO row written to event_validation_log."""
        contract_id = self._insert_contract()
        result = self.repo.apply_transition(
            event_type="TERMS_SUBMITTED",
            entity_type="trade",
            entity_id=contract_id,
            payload=_valid_terms_submitted_payload(),
            table="contracts",
            pk_column="contract_id",
            state_column="lpo_state",
            new_state="ACTIVE",
            idempotency_key=f"explain:valid:{contract_id}",
        )
        event_id = result["event_id"]

        conn = self.repo._connect()
        try:
            erow = conn.execute(
                "SELECT schema_ok FROM event_log WHERE event_id = ?", (event_id,)
            ).fetchone()
            vcount = conn.execute(
                "SELECT COUNT(*) AS n FROM event_validation_log WHERE event_id = ?",
                (event_id,)
            ).fetchone()["n"]
        finally:
            conn.close()

        self.assertEqual(erow["schema_ok"], 1)
        self.assertEqual(vcount, 0, "No event_validation_log row for schema_ok=1 events")

    # ------------------------------------------------------------------
    # Test 20: Explanation includes validator_version and catalog_hash
    # ------------------------------------------------------------------
    def test_20_explanation_includes_version_and_hash(self) -> None:
        """ValidationExplanation always includes validator_version and catalog_hash."""
        # Unknown type
        result = self.validator.validate("UNKNOWN:TYPE", {})
        exp = result.explanation
        self.assertIsNotNone(exp)
        self.assertEqual(exp.validator_version, "validator_v1")
        self.assertIsNotNone(exp.catalog_hash)
        self.assertNotEqual(exp.catalog_hash, "")

        # Known type (valid payload)
        result2 = self.validator.validate("TERMS_SUBMITTED", _valid_terms_submitted_payload())
        exp2 = result2.explanation
        self.assertIsNotNone(exp2)
        self.assertEqual(exp2.validator_version, "validator_v1")
        self.assertEqual(exp2.catalog_hash, exp.catalog_hash)  # same catalog
