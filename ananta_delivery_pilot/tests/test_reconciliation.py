from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from adapters.sqlite_repo import SQLiteRepo
from app.reconciliation import reconcile_weekly
from core.config import RuntimeConfig
from core.time import utc_now_iso_z


class WeeklyReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.temp_dir = Path(tempfile.mkdtemp(prefix="reconcile-tests-"))
        shutil.copytree(self.repo_root / "config", self.temp_dir / "config")
        (self.temp_dir / ".state").mkdir(parents=True, exist_ok=True)
        (self.temp_dir / "output_v2").mkdir(parents=True, exist_ok=True)
        self.config = RuntimeConfig.load(self.temp_dir)
        self.repo = SQLiteRepo(self.config.state_dir / "drep.sqlite")
        self.repo.init_db(self.config)
        self.fixtures_dir = self.repo_root / "tests" / "fixtures" / "reconciliation"

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _fixture(self, name: str) -> Path:
        return self.fixtures_dir / name

    def _insert_payment(
        self,
        *,
        payment_id: str,
        payment_date: str,
        amount_ngn: float,
        reference: str,
        vendor_id: str = "ananta_flows",
        buyer_id: str = "buyer_nycil",
    ) -> None:
        now = utc_now_iso_z()
        with self.repo.transaction() as conn:
            conn.execute(
                """
                INSERT INTO payments(
                    payment_id, vendor_of_record_id, buyer_id, payment_date, amount_received, currency,
                    payment_method, external_reference, idempotency_key, receipt_no, created_at, updated_at
                )
                VALUES(?, ?, ?, ?, ?, 'NGN', 'Bank Transfer', ?, ?, ?, ?, ?)
                """,
                (
                    payment_id,
                    vendor_id,
                    buyer_id,
                    payment_date,
                    amount_ngn,
                    reference,
                    f"IDEMP-{payment_id}",
                    f"RCPT-{payment_id}",
                    now,
                    now,
                ),
            )

    def test_perfect_match_all_payments_match(self) -> None:
        self._insert_payment(payment_id="PAY-001", payment_date="2026-03-01", amount_ngn=5_000_000.0, reference="REF-001")
        self._insert_payment(payment_id="PAY-002", payment_date="2026-03-02", amount_ngn=3_000_000.0, reference="REF-002")

        result = reconcile_weekly(
            root_dir=self.temp_dir,
            bank_statement_path=self._fixture("bank_perfect.csv"),
            week="2026-W10",
        )
        report = result.report
        self.assertEqual(2, report["matched_count"])
        self.assertEqual(0, report["unmatched_count"])
        self.assertEqual(0, report["orphan_bank_entries"])
        self.assertEqual(8_000_000.0, report["total_matched_amount_ngn"])
        self.assertEqual(0.0, report["total_delta_ngn"])
        self.assertTrue(result.report_path.exists())
        loaded = json.loads(result.report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["matched_count"], loaded["matched_count"])

    def test_partial_match_counts_are_correct(self) -> None:
        self._insert_payment(payment_id="PAY-001", payment_date="2026-03-01", amount_ngn=5_000_000.0, reference="REF-001")
        self._insert_payment(payment_id="PAY-003", payment_date="2026-03-03", amount_ngn=4_000_000.0, reference="REF-003")

        result = reconcile_weekly(
            root_dir=self.temp_dir,
            bank_statement_path=self._fixture("bank_partial.csv"),
            week="2026-W11",
        )
        report = result.report
        self.assertEqual(1, report["matched_count"])
        self.assertEqual(1, report["unmatched_count"])
        self.assertEqual(1, report["orphan_bank_entries"])

    def test_orphan_bank_entries_detected(self) -> None:
        result = reconcile_weekly(
            root_dir=self.temp_dir,
            bank_statement_path=self._fixture("bank_orphan_only.csv"),
            week="2026-W12",
        )
        report = result.report
        self.assertEqual(0, report["matched_count"])
        self.assertEqual(0, report["unmatched_count"])
        self.assertEqual(1, report["orphan_bank_entries"])
        self.assertEqual(1, len(report["orphan_bank_entries_detail"]))
        self.assertEqual("no matching pilot payment record", report["orphan_bank_entries_detail"][0]["reason"])

    def test_date_tolerance_within_plus_minus_two_business_days(self) -> None:
        self._insert_payment(payment_id="PAY-TOL", payment_date="2026-03-01", amount_ngn=1_000_000.0, reference="REF-TOL")
        result = reconcile_weekly(
            root_dir=self.temp_dir,
            bank_statement_path=self._fixture("bank_tolerance.csv"),
            week="2026-W13",
        )
        report = result.report
        self.assertEqual(1, report["matched_count"])
        self.assertEqual("2026-03-03", report["matched"][0]["match_date"])

    def test_amount_mismatch_same_reference_marks_unmatched(self) -> None:
        self._insert_payment(payment_id="PAY-MIS", payment_date="2026-03-02", amount_ngn=2_000_000.0, reference="REF-MIS")
        result = reconcile_weekly(
            root_dir=self.temp_dir,
            bank_statement_path=self._fixture("bank_amount_mismatch.csv"),
            week="2026-W14",
        )
        report = result.report
        self.assertEqual(0, report["matched_count"])
        self.assertEqual(1, report["unmatched_count"])
        self.assertEqual(1, report["orphan_bank_entries"])
        self.assertIn("amount mismatch", report["unmatched"][0]["reason"])

    def test_output_json_schema_has_required_keys(self) -> None:
        self._insert_payment(payment_id="PAY-001", payment_date="2026-03-01", amount_ngn=5_000_000.0, reference="REF-001")
        result = reconcile_weekly(
            root_dir=self.temp_dir,
            bank_statement_path=self._fixture("bank_perfect.csv"),
            week="2026-W15",
        )
        report = result.report
        expected_keys = {
            "week",
            "generated_at",
            "bank_statement_file",
            "matched_count",
            "unmatched_count",
            "orphan_bank_entries",
            "total_matched_amount_ngn",
            "total_delta_ngn",
            "matched",
            "unmatched",
            "orphan_bank_entries_detail",
        }
        self.assertEqual(expected_keys, set(report.keys()))
        self.assertIsInstance(report["matched"], list)
        self.assertIsInstance(report["unmatched"], list)
        self.assertIsInstance(report["orphan_bank_entries_detail"], list)

    def test_dry_run_does_not_write_file(self) -> None:
        self._insert_payment(payment_id="PAY-001", payment_date="2026-03-01", amount_ngn=5_000_000.0, reference="REF-001")
        result = reconcile_weekly(
            root_dir=self.temp_dir,
            bank_statement_path=self._fixture("bank_perfect.csv"),
            week="2026-W16",
            dry_run=True,
        )
        self.assertFalse(result.report_path.exists())
        self.assertEqual(1, result.report["matched_count"])

    def test_terminal_summary_uses_naira_formatting(self) -> None:
        self._insert_payment(payment_id="PAY-001", payment_date="2026-03-01", amount_ngn=5_000_000.0, reference="REF-001")
        self._insert_payment(payment_id="PAY-002", payment_date="2026-03-02", amount_ngn=3_000_000.0, reference="REF-002")
        result = reconcile_weekly(
            root_dir=self.temp_dir,
            bank_statement_path=self._fixture("bank_perfect.csv"),
            week="2026-W17",
            dry_run=True,
        )
        self.assertIn("₦8,000,000.00", result.summary_text)
        self.assertIn("Reconciliation: 2026-W17", result.summary_text)
        self.assertIn("Delta:      ₦0.00", result.summary_text)


if __name__ == "__main__":
    unittest.main()

