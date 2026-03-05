"""Phase 1.5 — Export command tests (8 verification tests)."""
from __future__ import annotations

import hashlib
import json
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
from domain.exporter import run_export


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ExportTests(unittest.TestCase):

    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.temp_dir = Path(tempfile.mkdtemp(prefix="export-tests-"))
        shutil.copytree(self.repo_root / "config", self.temp_dir / "config")
        (self.temp_dir / ".state").mkdir(parents=True, exist_ok=True)
        (self.temp_dir / "output_v2").mkdir(parents=True, exist_ok=True)
        self.config = RuntimeConfig.load(self.temp_dir)
        self.repo = SQLiteRepo(self.config.state_dir / "drep.sqlite")
        self.repo.init_db(self.config)
        self.output_dir = self.temp_dir / "export_out"
        self.db_path = self.config.state_dir / "drep.sqlite"
        self.config_dir = self.temp_dir / "config"

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _insert_contract(self, lpo_state: str = "ACTIVE") -> str:
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
                    expected_total_qty, expected_total_qty_kg, expected_total_value,
                    lpo_state, created_at, updated_at
                ) VALUES (
                    ?, 'LPO-EXP-1', 'LPO-EXP-001', '2026-01-01',
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

    def _apply_event(self, contract_id: str, idem_suffix: str) -> dict:
        payload = {
            "trade_id": str(uuid.uuid4()),
            "payload": {
                "actor_org_id": str(uuid.uuid4()),
                "payment_terms": "NET30",
                "delivery_term": "DAP",
                "delivery_location": "Lagos, Nigeria",
            },
        }
        return self.repo.apply_transition(
            event_type="TERMS_SUBMITTED",
            entity_type="contract",
            entity_id=contract_id,
            payload=payload,
            table="contracts",
            pk_column="contract_id",
            state_column="lpo_state",
            new_state="ACTIVE",
            idempotency_key=f"TERMS_SUBMITTED:{contract_id}:{idem_suffix}",
        )

    # ------------------------------------------------------------------
    # Test 1: Export produces all 4 files
    # ------------------------------------------------------------------
    def test_export_produces_all_four_files(self) -> None:
        """run_export writes id_map, event_manifest, replay_readiness_report, manifest_metadata."""
        self._insert_contract()
        run_export(self.db_path, self.config_dir, self.output_dir)

        expected = [
            "id_map.json",
            "event_manifest.json",
            "replay_readiness_report.json",
            "manifest_metadata.json",
        ]
        for fname in expected:
            path = self.output_dir / fname
            self.assertTrue(path.exists(), f"{fname} not written")
            data = json.loads(path.read_text())
            self.assertIsInstance(data, dict, f"{fname} is not a JSON object")

    # ------------------------------------------------------------------
    # Test 2: id_map covers all entities with core_uuid
    # ------------------------------------------------------------------
    def test_id_map_covers_all_entities(self) -> None:
        """Every entity with a core_uuid appears in id_map entity_maps."""
        contract_id = self._insert_contract()
        run_export(self.db_path, self.config_dir, self.output_dir)

        id_map = json.loads((self.output_dir / "id_map.json").read_text())

        # Find the contract we inserted in the trades section
        trades = id_map["entity_maps"]["trades"]
        pilot_ids = [e["pilot_id"] for e in trades]
        self.assertIn(contract_id, pilot_ids, "Inserted contract missing from id_map trades")

        # core_id must always be null (assigned by core during replay)
        for group in id_map["entity_maps"].values():
            for entity in group:
                self.assertIsNone(entity["core_id"], "core_id must be null in pilot export")

    # ------------------------------------------------------------------
    # Test 3: Event manifest events are ordered by created_at ascending per entity
    # ------------------------------------------------------------------
    def test_event_manifest_events_ordered_by_created_at(self) -> None:
        """Within each entity, events are sorted oldest-first (replay order)."""
        contract_id = self._insert_contract()
        self._apply_event(contract_id, "order-001")
        time.sleep(0.1)
        # Apply a second event using a pilot-internal type (no schema required)
        with self.repo.transaction() as conn:
            from core.ids import generate_pilot_uuid
            from core.time import utc_now_iso_z as now
            import json as _json
            conn.execute(
                "INSERT INTO event_log(event_id, event_type, entity_type, entity_id, "
                "payload_json, source, created_at) VALUES (?, ?, ?, ?, ?, 'phase1c', ?)",
                (generate_pilot_uuid(), "PILOT_NOTE", "contract", contract_id, "{}", now()),
            )

        run_export(self.db_path, self.config_dir, self.output_dir)

        manifest = json.loads((self.output_dir / "event_manifest.json").read_text())
        key = f"contract:{contract_id}"
        self.assertIn(key, manifest["events_by_entity"])
        events = manifest["events_by_entity"][key]
        self.assertGreaterEqual(len(events), 2)

        created_ats = [e["created_at"] for e in events]
        self.assertEqual(created_ats, sorted(created_ats), "Events not in ascending created_at order")

    # ------------------------------------------------------------------
    # Test 4: Event manifest includes core_uuid joined from entity table
    # ------------------------------------------------------------------
    def test_event_manifest_includes_core_uuid(self) -> None:
        """core_uuid on each event is joined from the entity table, not stored on event_log."""
        contract_id = self._insert_contract()
        self._apply_event(contract_id, "uuid-join-001")
        run_export(self.db_path, self.config_dir, self.output_dir)

        # Get the actual core_uuid for this contract
        with self.repo.transaction() as conn:
            row = conn.execute(
                "SELECT core_uuid FROM contracts WHERE contract_id = ?",
                (contract_id,),
            ).fetchone()
        expected_uuid = row["core_uuid"] if row else None

        manifest = json.loads((self.output_dir / "event_manifest.json").read_text())
        key = f"contract:{contract_id}"
        self.assertIn(key, manifest["events_by_entity"])
        events = manifest["events_by_entity"][key]
        self.assertTrue(len(events) > 0)

        for event in events:
            self.assertIn("core_uuid", event)
            self.assertEqual(
                event["core_uuid"], expected_uuid,
                "core_uuid on event does not match entity table"
            )

    # ------------------------------------------------------------------
    # Test 5: manifest_metadata SHA-256 hashes are correct
    # ------------------------------------------------------------------
    def test_manifest_metadata_sha256_hashes_correct(self) -> None:
        """SHA-256 hashes in manifest_metadata match actual file contents."""
        self._insert_contract()
        run_export(self.db_path, self.config_dir, self.output_dir)

        metadata = json.loads((self.output_dir / "manifest_metadata.json").read_text())
        file_entries = {f["name"]: f["sha256"] for f in metadata["files"]}

        for fname, recorded_hash in file_entries.items():
            actual_hash = _sha256_file(self.output_dir / fname)
            self.assertEqual(
                actual_hash, recorded_hash,
                f"SHA-256 mismatch for {fname}: recorded={recorded_hash}, actual={actual_hash}"
            )

    # ------------------------------------------------------------------
    # Test 6: Dry run writes no files
    # ------------------------------------------------------------------
    def test_dry_run_writes_no_files(self) -> None:
        """--dry-run computes everything but writes nothing to disk."""
        self._insert_contract()
        run_export(self.db_path, self.config_dir, self.output_dir, dry_run=True)

        self.assertFalse(
            self.output_dir.exists(),
            "Output directory must not be created in dry-run mode"
        )

    # ------------------------------------------------------------------
    # Test 7: Zero-UUID-coverage warning logged for entity without core_uuid
    # ------------------------------------------------------------------
    def test_zero_uuid_coverage_warning(self) -> None:
        """Entity with no core_uuid triggers a warning in export summary."""
        contract_id = new_ulid()
        now = utc_now_iso_z()
        # Insert contract WITHOUT core_uuid (null)
        with self.repo.transaction() as conn:
            conn.execute(
                """
                INSERT INTO contracts(
                    contract_id, contract_ref, lpo_no, lpo_date,
                    buyer_id, vendor_of_record_id, operator_id,
                    source_id, processor_id, lane, currency,
                    issue_date, due_date, due_terms,
                    expected_total_qty, expected_total_qty_kg, expected_total_value,
                    lpo_state, core_uuid, created_at, updated_at
                ) VALUES (
                    ?, 'LPO-NOUUID', 'LPO-NU-001', '2026-01-01',
                    'buyer_nycil', 'ananta_flows', 'guildgate',
                    'ananta_flows', 'processor_partner_refinery', 'B', 'NGN',
                    '2026-01-01', '2026-03-01', '60 days',
                    30000.0, 30000000, 68100000.0,
                    'DRAFT', NULL, ?, ?
                )
                """,
                (contract_id, now, now),
            )

        summary = run_export(self.db_path, self.config_dir, self.output_dir)

        warning_texts = " ".join(summary["warnings"])
        self.assertTrue(
            any(contract_id in w for w in summary["warnings"]),
            f"Expected warning for {contract_id} with no core_uuid, got: {summary['warnings']}"
        )

        # id_map should reflect entities_without_uuid > 0
        id_map = json.loads((self.output_dir / "id_map.json").read_text())
        self.assertGreater(id_map["summary"]["entities_without_uuid"], 0)

    # ------------------------------------------------------------------
    # Test 8: Export on empty DB produces valid JSON with zero counts
    # ------------------------------------------------------------------
    def test_export_on_empty_db_produces_valid_json(self) -> None:
        """Export against a non-existent DB produces valid zero-count JSON (not an error)."""
        # Point to a DB file that doesn't exist — exporter must handle gracefully
        empty_db = self.temp_dir / ".state" / "nonexistent.sqlite"
        # Deliberately do NOT create or init this DB

        empty_output = self.temp_dir / "empty_export"
        summary = run_export(empty_db, self.config_dir, empty_output)

        self.assertEqual(summary["entity_count"], 0)
        self.assertEqual(summary["event_count"], 0)
        self.assertEqual(summary["uuid_coverage_pct"], 100.0)
        self.assertEqual(summary["replay_readiness_pct"], 100.0)

        # All 4 files present and valid JSON
        for fname in ["id_map.json", "event_manifest.json",
                      "replay_readiness_report.json", "manifest_metadata.json"]:
            path = empty_output / fname
            self.assertTrue(path.exists(), f"{fname} missing on empty DB export")
            data = json.loads(path.read_text())
            self.assertIsInstance(data, dict)

    # ------------------------------------------------------------------
    # Test 9: Proof-lite and export report identical replay_readiness_pct
    # ------------------------------------------------------------------
    def test_9_proof_lite_and_export_agree_on_readiness(self) -> None:
        """build_replay_readiness_report and proof_lite.build_report must give same readiness %."""
        import sys
        from pathlib import Path as _Path
        repo_root = _Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(repo_root))
        from scripts.proof_lite import build_report, load_catalog
        from domain.exporter import build_replay_readiness_report

        contract_id = self._insert_contract()
        self._apply_event(contract_id, "agree-1")
        # Also insert an unknown event type to get schema_ok=0
        unknown_payload = {"note": "unknown event"}
        self.repo.apply_transition(
            event_type="PILOT:UNKNOWN_AGREE",
            entity_type="contract",
            entity_id=contract_id,
            payload=unknown_payload,
            table="contracts",
            pk_column="contract_id",
            state_column="lpo_state",
            new_state="ACTIVE",
            idempotency_key=f"agree-unknown:{contract_id}",
        )

        # Get exporter readiness
        export_report = build_replay_readiness_report(self.db_path, self.config_dir)
        export_pct = export_report["summary"]["replay_readiness_pct"]

        # Get proof_lite readiness
        import sqlite3 as _sqlite3
        conn = _sqlite3.connect(str(self.db_path))
        conn.row_factory = _sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            "SELECT event_id, event_type, schema_ok, validated_against_ref, "
            "validated_against_hash, created_at FROM event_log WHERE idempotency_key IS NOT NULL"
        ).fetchall()]
        conn.close()
        catalog = load_catalog(self.config_dir)
        pl_report = build_report(rows, catalog)
        pl_pct = pl_report["summary"]["replay_readiness_pct"]

        self.assertEqual(export_pct, pl_pct,
                         f"Exporter={export_pct}% vs proof_lite={pl_pct}% must match")

    # ------------------------------------------------------------------
    # Test 10: Epoch-aware metrics: post_epoch_events differs from total
    # ------------------------------------------------------------------
    def test_10_epoch_aware_metrics_present(self) -> None:
        """build_replay_readiness_report includes post_epoch_events and epoch readiness %."""
        from domain.exporter import build_replay_readiness_report, EPOCH_TIMESTAMP

        contract_id = self._insert_contract()
        self._apply_event(contract_id, "epoch-1")

        report = build_replay_readiness_report(self.db_path, self.config_dir)

        self.assertIn("post_epoch_events", report)
        self.assertIn("epoch_timestamp", report)
        self.assertEqual(report["epoch_timestamp"], EPOCH_TIMESTAMP)
        self.assertIn("replay_readiness_pct_post_epoch", report["summary"])
        self.assertIsInstance(report["summary"]["replay_readiness_pct_post_epoch"], float)

    # ------------------------------------------------------------------
    # Test 11: Diagnostic — existing schema_ok=0 events have useful explanation
    # ------------------------------------------------------------------
    def test_11_diagnostic_schema_ok_0_has_explanation(self) -> None:
        """Schema_ok=0 events (unknown types) get explanation in event_validation_log."""
        import sqlite3 as _sqlite3

        contract_id = self._insert_contract()
        self.repo.apply_transition(
            event_type="TERMS_AUTHORIZED",   # known pilot type, may be unknown in catalog
            entity_type="contract",
            entity_id=contract_id,
            payload={"note": "terms authorized"},
            table="contracts",
            pk_column="contract_id",
            state_column="lpo_state",
            new_state="ACTIVE",
            idempotency_key=f"diagnostic:TERMS_AUTHORIZED:{contract_id}",
        )

        conn = _sqlite3.connect(str(self.db_path))
        conn.row_factory = _sqlite3.Row
        rows = conn.execute(
            "SELECT e.event_type, e.schema_ok, v.explanation_json "
            "FROM event_log e "
            "LEFT JOIN event_validation_log v ON e.event_id = v.event_id "
            "WHERE e.idempotency_key LIKE 'diagnostic:%'"
        ).fetchall()
        conn.close()

        self.assertGreater(len(rows), 0)
        for row in rows:
            if row["schema_ok"] == 0:
                # Must have explanation
                self.assertIsNotNone(
                    row["explanation_json"],
                    f"schema_ok=0 event '{row['event_type']}' has no explanation"
                )
                exp = json.loads(row["explanation_json"])
                self.assertIn("schema_ok_reason", exp)
                self.assertIn("validator_version", exp)
