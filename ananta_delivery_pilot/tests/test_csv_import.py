from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from adapters.sqlite_repo import SQLiteRepo
from app.csv_import import CsvImporter
from core.config import RuntimeConfig
from domain.event_ledger import canonical_hash


class CsvImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.temp_dir = Path(tempfile.mkdtemp(prefix="csv-import-tests-"))
        shutil.copytree(self.repo_root / "config", self.temp_dir / "config")
        (self.temp_dir / ".state").mkdir(parents=True, exist_ok=True)
        (self.temp_dir / "output_v2").mkdir(parents=True, exist_ok=True)
        self.config = RuntimeConfig.load(self.temp_dir)
        self.repo = SQLiteRepo(self.config.state_dir / "drep.sqlite")
        self.repo.init_db(self.config)
        self.fixtures_dir = self.repo_root / "tests" / "fixtures" / "csv_import"

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _fixture(self, name: str) -> Path:
        return self.fixtures_dir / name

    def _run_import(self, *, import_type: str, fixture_name: str, dry_run: bool = False):
        importer = CsvImporter(self.config, self.repo, dry_run=dry_run)
        return importer.import_file(import_type=import_type, file_path=self._fixture(fixture_name))

    def _run_import_file(self, *, import_type: str, file_path: Path, dry_run: bool = False):
        importer = CsvImporter(self.config, self.repo, dry_run=dry_run)
        return importer.import_file(import_type=import_type, file_path=file_path)

    def _count_rows(self, table: str) -> int:
        with self.repo.transaction() as conn:
            row = conn.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
            return int(row["count"] if row else 0)

    def _count_events(self, *, event_type: str | None = None) -> int:
        sql = "SELECT COUNT(*) AS count FROM event_log"
        params: tuple[object, ...] = ()
        if event_type:
            sql += " WHERE event_type = ?"
            params = (event_type,)
        with self.repo.transaction() as conn:
            row = conn.execute(sql, params).fetchone()
            return int(row["count"] if row else 0)

    def _file_sha256(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def test_successful_import_of_clean_csv_creates_events(self) -> None:
        summary = self._run_import(import_type="counterparties", fixture_name="counterparties_clean.csv")
        self.assertEqual(2, summary.total_rows)
        self.assertEqual(2, summary.imported)
        self.assertEqual(0, summary.rejected)
        self.assertEqual(0, summary.skipped_duplicates)
        with self.repo.transaction() as conn:
            row = conn.execute(
                "SELECT legal_name FROM parties WHERE party_id = ?",
                ("cp_csv_001",),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual("Counterparty CSV One", row["legal_name"])
        self.assertEqual(2, self._count_events(event_type="pilot:counterparty:registered"))

    def test_mixed_csv_imports_good_and_rejects_bad(self) -> None:
        summary = self._run_import(import_type="trades", fixture_name="trades_mixed.csv")
        self.assertEqual(4, summary.total_rows)
        self.assertEqual(2, summary.imported)
        self.assertEqual(2, summary.rejected)
        self.assertEqual(0, summary.skipped_duplicates)
        with self.repo.transaction() as conn:
            refs = {
                row["contract_ref"]
                for row in conn.execute(
                    "SELECT contract_ref FROM contracts WHERE contract_ref LIKE 'LPO-MIX-%'"
                ).fetchall()
            }
            self.assertEqual({"LPO-MIX-001", "LPO-MIX-004"}, refs)
        self.assertEqual(2, self._count_events(event_type="TERMS_SUBMITTED"))

    def test_summary_output_counts_match(self) -> None:
        cmd = [
            "python3",
            "scripts/csv_import.py",
            "--type",
            "trades",
            "--file",
            str(self._fixture("trades_mixed.csv")),
            "--root-dir",
            str(self.temp_dir),
        ]
        proc = subprocess.run(
            cmd,
            cwd=self.repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("Import Summary:", proc.stdout)
        self.assertIn("Total rows: 4", proc.stdout)
        self.assertIn("Imported: 2", proc.stdout)
        self.assertIn("Skipped (duplicates): 0", proc.stdout)
        self.assertIn("Rejected: 2", proc.stdout)
        self.assertIn("EVENTED MODE: All imports go through the event ledger.", proc.stderr)
        self.assertNotIn("PRE-EVENT-LEDGER MODE", proc.stderr)

    def test_event_content_hash_matches_canonical_payload(self) -> None:
        self._run_import(import_type="trades", fixture_name="trades_clean.csv")
        with self.repo.transaction() as conn:
            row = conn.execute(
                "SELECT payload_json, content_hash FROM event_log WHERE event_type = ? LIMIT 1",
                ("TERMS_SUBMITTED",),
            ).fetchone()
        self.assertIsNotNone(row)
        payload = json.loads(str(row["payload_json"]))
        self.assertEqual(canonical_hash(payload), row["content_hash"])

    def test_event_idempotency_key_uses_csv_file_hash_and_row_number(self) -> None:
        fixture = self._fixture("trades_clean.csv")
        summary = self._run_import(import_type="trades", fixture_name="trades_clean.csv")
        self.assertEqual(2, summary.imported)
        file_hash = self._file_sha256(fixture)
        with self.repo.transaction() as conn:
            keys = {
                row["idempotency_key"]
                for row in conn.execute(
                    "SELECT idempotency_key FROM event_log WHERE event_type = ?",
                    ("TERMS_SUBMITTED",),
                ).fetchall()
            }
        self.assertEqual({f"csv:{file_hash}:1", f"csv:{file_hash}:2"}, keys)

    def test_reimport_same_csv_is_idempotent(self) -> None:
        first = self._run_import(import_type="trades", fixture_name="trades_clean.csv")
        second = self._run_import(import_type="trades", fixture_name="trades_clean.csv")
        self.assertEqual(2, first.imported)
        self.assertEqual(0, second.imported)
        self.assertEqual(2, second.skipped_duplicates)
        self.assertEqual(2, self._count_events(event_type="TERMS_SUBMITTED"))

    def test_modified_csv_creates_new_events(self) -> None:
        fixture = self._fixture("trades_clean.csv")
        _ = self._run_import(import_type="trades", fixture_name="trades_clean.csv")
        before_events = self._count_events(event_type="TERMS_SUBMITTED")

        modified = self.temp_dir / "trades_clean_modified.csv"
        modified.write_text(
            fixture.read_text(encoding="utf-8").replace("30000,kgs,2270", "31000,kgs,2270"),
            encoding="utf-8",
        )

        summary = self._run_import_file(import_type="trades", file_path=modified)
        after_events = self._count_events(event_type="TERMS_SUBMITTED")
        self.assertEqual(2, summary.imported)
        self.assertGreater(after_events, before_events)

    def test_source_metadata_present_in_event_payload(self) -> None:
        fixture = self._fixture("counterparties_clean.csv")
        self._run_import(import_type="counterparties", fixture_name="counterparties_clean.csv")
        expected_hash = self._file_sha256(fixture)
        with self.repo.transaction() as conn:
            row = conn.execute(
                "SELECT payload_json FROM event_log WHERE event_type = ? ORDER BY created_at ASC LIMIT 1",
                ("pilot:counterparty:registered",),
            ).fetchone()
        self.assertIsNotNone(row)
        payload = json.loads(str(row["payload_json"]))
        self.assertEqual("csv_import", payload.get("source_system"))
        self.assertTrue(str(payload.get("source_ref", "")).endswith(":1"))
        self.assertEqual(expected_hash, payload.get("source_file_hash"))

    def test_schema_validation_rejects_bad_rows_without_event(self) -> None:
        importer = CsvImporter(self.config, self.repo, dry_run=False)
        original = importer._build_event_payload

        def _bad_payload(**kwargs):
            payload = original(**kwargs)
            payload["trade_id"] = "not-a-uuid"
            return payload

        importer._build_event_payload = _bad_payload  # type: ignore[method-assign]
        summary = importer.import_file(import_type="trades", file_path=self._fixture("trades_clean.csv"))
        self.assertEqual(2, summary.total_rows)
        self.assertEqual(0, summary.imported)
        self.assertEqual(2, summary.rejected)
        self.assertEqual(0, self._count_events(event_type="TERMS_SUBMITTED"))

    def test_duplicate_key_rows_are_skipped(self) -> None:
        summary = self._run_import(import_type="payments", fixture_name="payments_duplicates.csv")
        self.assertEqual(2, summary.total_rows)
        self.assertEqual(1, summary.imported)
        self.assertEqual(1, summary.skipped_duplicates)
        self.assertEqual(0, summary.rejected)
        self.assertEqual(1, self._count_events(event_type="pilot:payment:recorded"))

    def test_dry_run_mode_validates_without_writing_entities_or_events(self) -> None:
        before_contracts = self._count_rows("contracts")
        before_events = self._count_events()
        summary = self._run_import(import_type="trades", fixture_name="trades_clean.csv", dry_run=True)
        after_contracts = self._count_rows("contracts")
        after_events = self._count_events()
        self.assertEqual(2, summary.imported)
        self.assertEqual(before_contracts, after_contracts)
        self.assertEqual(before_events, after_events)
        self.assertTrue(any("Would create event TERMS_SUBMITTED" in item for item in summary.warnings))

    def test_unknown_entity_reference_rejects_only_bad_row(self) -> None:
        summary = self._run_import(import_type="trades", fixture_name="trades_unknown_entity_mixed.csv")
        self.assertEqual(2, summary.total_rows)
        self.assertEqual(1, summary.imported)
        self.assertEqual(1, summary.rejected)
        self.assertTrue(any(item.field == "buyer_id" for item in summary.rejected_rows))
        with self.repo.transaction() as conn:
            row = conn.execute(
                "SELECT contract_ref FROM contracts WHERE contract_ref = ?",
                ("LPO-UNK-001",),
            ).fetchone()
            self.assertIsNotNone(row)
        self.assertEqual(1, self._count_events(event_type="TERMS_SUBMITTED"))


if __name__ == "__main__":
    unittest.main()
