from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from adapters.sqlite_repo import SQLiteRepo
from app.csv_import import CsvImporter
from core.config import RuntimeConfig


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

    def _count_rows(self, table: str) -> int:
        with self.repo.transaction() as conn:
            row = conn.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
            return int(row["count"] if row else 0)

    def test_successful_import_of_clean_csv(self) -> None:
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

    def test_quantity_le_zero_row_rejected(self) -> None:
        summary = self._run_import(import_type="trades", fixture_name="trades_negative_qty.csv")
        self.assertEqual(1, summary.total_rows)
        self.assertEqual(0, summary.imported)
        self.assertEqual(1, summary.rejected)
        self.assertEqual(0, summary.skipped_duplicates)
        self.assertTrue(any(item.field == "expected_qty" for item in summary.rejected_rows))
        self.assertEqual(0, self._count_rows("contracts"))

    def test_duplicate_key_rows_are_skipped(self) -> None:
        summary = self._run_import(import_type="payments", fixture_name="payments_duplicates.csv")
        self.assertEqual(2, summary.total_rows)
        self.assertEqual(1, summary.imported)
        self.assertEqual(1, summary.skipped_duplicates)
        self.assertEqual(0, summary.rejected)
        with self.repo.transaction() as conn:
            rows = conn.execute(
                "SELECT payment_id FROM payments WHERE idempotency_key = ?",
                ("PAY-CSV-001",),
            ).fetchall()
            self.assertEqual(1, len(rows))

    def test_dry_run_mode_does_not_write_to_db(self) -> None:
        before = self._count_rows("contracts")
        summary = self._run_import(import_type="trades", fixture_name="trades_clean.csv", dry_run=True)
        after = self._count_rows("contracts")
        self.assertEqual(2, summary.imported)
        self.assertEqual(before, after)

    def test_utf8_bom_handling(self) -> None:
        summary = self._run_import(import_type="counterparties", fixture_name="counterparties_bom.csv")
        self.assertEqual(1, summary.total_rows)
        self.assertEqual(1, summary.imported)
        with self.repo.transaction() as conn:
            row = conn.execute(
                "SELECT code FROM parties WHERE party_id = ?",
                ("cp_bom_001",),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual("BOM1", row["code"])


if __name__ == "__main__":
    unittest.main()

