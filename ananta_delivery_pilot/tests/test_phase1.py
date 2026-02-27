from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

from adapters.sqlite_repo import SQLiteRepo, _resolve_coa_profile
from core.config import RuntimeConfig
from core.hashing import canonical_json_sha256
from domain.services import Phase1Service


class Phase1FlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.temp_dir = Path(tempfile.mkdtemp(prefix="phase1-tests-"))
        shutil.copytree(self.repo_root / "config", self.temp_dir / "config")
        (self.temp_dir / ".state").mkdir(parents=True, exist_ok=True)
        (self.temp_dir / "output_v2").mkdir(parents=True, exist_ok=True)
        self.config = RuntimeConfig.load(self.temp_dir)
        self.repo = SQLiteRepo(self.config.state_dir / "drep.sqlite")
        self.service = Phase1Service(self.config, self.repo)
        self.service.init_db()

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_contract(self, vendor_of_record_id: str, *, due_date: str = "2026-03-09") -> str:
        payload = {
            "contract_ref": f"LPO-{vendor_of_record_id.upper()}-01",
            "lpo_no": "202570410",
            "lpo_date": "2026-01-30",
            "buyer_id": "buyer_nycil",
            "vendor_of_record_id": vendor_of_record_id,
            "operator_id": "guildgate",
            "source_id": vendor_of_record_id,
            "processor_id": "processor_partner_refinery",
            "currency": "NGN",
            "issue_date": "2026-02-23",
            "due_date": due_date,
            "due_terms": "14 days",
            "expected_total_qty": 30000.0,
            "lines": [
                {
                    "product_code": "RBDSO",
                    "description": "Processing and supply of RBDSO linked to RUN-20260223-01",
                    "expected_qty": 30000.0,
                    "unit": "kgs",
                    "unit_price": 2270.0,
                }
            ],
        }
        result = self.service.create_contract(
            payload,
            allow_placeholder_tin=(vendor_of_record_id == "ananta_flows"),
        )
        return str(result["contract_id"])

    def _create_delivery(self, contract_id: str, run_suffix: str = "01") -> str:
        payload = {
            "contract_id": contract_id,
            "line_no": 1,
            "delivery_ref": f"DLV-{run_suffix}",
            "run_id": f"RUN-20260223-{run_suffix}",
            "batch_id": f"AFL-RBDSO-20260223-{run_suffix}",
            "delivery_date": "2026-02-23",
            "delivered_qty": 30000.0,
            "unit": "kgs",
            "unit_price": 2270.0,
            "truck_no": "T28162LA",
            "driver_name": "Idowu Atanda",
            "driver_phone": "08052803019",
        }
        result = self.service.add_delivery(payload)
        return str(result["delivery_id"])

    def _record_full_coa(self, delivery_id: str) -> None:
        bundle = self.repo.get_delivery_bundle(delivery_id)
        buyer_group = "NYCIL"
        product_code = "RBDSO"
        profile = _resolve_coa_profile(self.config.coa_profiles, buyer_group=buyer_group, product_code=product_code)
        rows = [
            {"parameter": row["parameter"], "result": "PASS"}
            for row in profile["quality_parameters"]
        ]
        self.service.record_coa(
            {
                "delivery_id": delivery_id,
                "results": rows,
            }
        )

    def test_manifest_and_skip_pdf_hash_semantics(self) -> None:
        contract_id = self._create_contract("ananta_flows")
        delivery_id = self._create_delivery(contract_id, "11")
        self.service.mark_dispatched(delivery_id)
        self.service.mark_delivered(delivery_id)
        self._record_full_coa(delivery_id)

        result = self.service.generate_pack(
            delivery_id=delivery_id,
            allow_placeholder_tin=True,
            skip_pdf=True,
        )
        self.assertEqual(4, len(result["documents"]))
        for doc in result["documents"]:
            self.assertIsNone(doc["pdf_sha256"])
            self.assertTrue(doc["content_sha256"])

        manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
        cloned = dict(manifest)
        expected_hash = cloned.pop("content_sha256")
        self.assertEqual(expected_hash, canonical_json_sha256(cloned))

        links = self.repo.fetch_all(
            """
            SELECT doc_id, sales_line_id FROM document_sales_links
            WHERE delivery_id = ?
            """,
            (delivery_id,),
        )
        self.assertEqual(4, len(links))

    def test_numbering_isolation_by_vendor(self) -> None:
        contract_a = self._create_contract("guildgate")
        delivery_a = self._create_delivery(contract_a, "21")
        self.service.mark_dispatched(delivery_a)
        self.service.mark_delivered(delivery_a)
        self._record_full_coa(delivery_a)
        result_a = self.service.generate_pack(delivery_id=delivery_a, allow_placeholder_tin=False, skip_pdf=True)

        contract_b = self._create_contract("ananta_flows")
        delivery_b = self._create_delivery(contract_b, "22")
        self.service.mark_dispatched(delivery_b)
        self.service.mark_delivered(delivery_b)
        self._record_full_coa(delivery_b)
        result_b = self.service.generate_pack(delivery_id=delivery_b, allow_placeholder_tin=True, skip_pdf=True)

        self.assertEqual("INV-2026-0001", result_a["invoice_no"])
        self.assertEqual("INV-2026-0001", result_b["invoice_no"])

    def test_delivery_state_transitions_valid_and_invalid(self) -> None:
        contract_id = self._create_contract("guildgate")
        delivery_id = self._create_delivery(contract_id, "31")
        with self.assertRaises(ValueError):
            self.service.mark_delivered(delivery_id)
        self.service.mark_dispatched(delivery_id)
        self.service.mark_delivered(delivery_id)
        with self.assertRaises(ValueError):
            self.service.mark_dispatched(delivery_id)

    def test_coa_required_rows_enforced(self) -> None:
        contract_id = self._create_contract("ananta_flows")
        delivery_id = self._create_delivery(contract_id, "41")
        self.service.mark_dispatched(delivery_id)
        self.service.mark_delivered(delivery_id)
        with self.assertRaises(ValueError):
            self.service.record_coa(
                {
                    "delivery_id": delivery_id,
                    "results": [{"parameter": "Appearance", "result": "PASS"}],
                }
            )

    def test_receipt_only_generated_on_mark_paid(self) -> None:
        contract_id = self._create_contract("ananta_flows")
        delivery_id = self._create_delivery(contract_id, "51")
        self.service.mark_dispatched(delivery_id)
        self.service.mark_delivered(delivery_id)
        self._record_full_coa(delivery_id)
        pack = self.service.generate_pack(delivery_id=delivery_id, allow_placeholder_tin=True, skip_pdf=True)
        sales_transaction_id = pack["sales_transaction_id"]

        receipts_before = self.repo.fetch_all(
            "SELECT * FROM documents WHERE sales_transaction_id = ? AND doc_type = 'RECEIPT'",
            (sales_transaction_id,),
        )
        self.assertEqual([], receipts_before)

        payment_payload = {
            "payment_date": "2026-03-09",
            "payment_method": "Bank Transfer",
            "external_reference": "REF-00051",
            "idempotency_key": "REF-00051",
            "amount_received": 68100000.0,
            "allocations": [
                {"sales_transaction_id": sales_transaction_id, "allocated_amount": 68100000.0}
            ],
        }
        result = self.service.mark_paid(payment_payload, allow_placeholder_tin=True, skip_pdf=True)
        self.assertTrue(result["receipt_no"].startswith("RCPT-INV-2026-0001"))

        receipts_after = self.repo.fetch_all(
            "SELECT * FROM documents WHERE sales_transaction_id = ? AND doc_type = 'RECEIPT'",
            (sales_transaction_id,),
        )
        self.assertEqual(1, len(receipts_after))

    def test_aging_determinism_with_as_of(self) -> None:
        contract_id = self._create_contract("guildgate", due_date="2026-03-01")
        delivery_id = self._create_delivery(contract_id, "61")
        self.service.mark_dispatched(delivery_id)
        self.service.mark_delivered(delivery_id)
        self._record_full_coa(delivery_id)
        self.service.generate_pack(delivery_id=delivery_id, allow_placeholder_tin=False, skip_pdf=True)

        out_dir_a = self.temp_dir / "exports_a"
        self.service.export_drep(as_of_date="2026-03-15", out_dir=out_dir_a)
        rows_a = _read_csv(out_dir_a / "drep_outstanding_payments.csv")
        self.assertEqual("1-30", rows_a[0]["aging_bucket"])

        out_dir_b = self.temp_dir / "exports_b"
        self.service.export_drep(as_of_date="2026-07-15", out_dir=out_dir_b)
        rows_b = _read_csv(out_dir_b / "drep_outstanding_payments.csv")
        self.assertEqual("90+", rows_b[0]["aging_bucket"])

    def test_sales_line_doc_linkage_integrity(self) -> None:
        contract_id = self._create_contract("guildgate")
        delivery_id = self._create_delivery(contract_id, "71")
        self.service.mark_dispatched(delivery_id)
        self.service.mark_delivered(delivery_id)
        self._record_full_coa(delivery_id)
        self.service.generate_pack(delivery_id=delivery_id, allow_placeholder_tin=False, skip_pdf=True)

        rows = self.repo.fetch_all("SELECT * FROM drep_sales_lines")
        self.assertEqual(1, len(rows))
        row = rows[0]
        self.assertTrue(row["waybill_doc_id"])
        self.assertTrue(row["weighing_doc_id"])
        self.assertTrue(row["coa_doc_id"])
        self.assertTrue(row["invoice_doc_id"])

    def test_legacy_serve_smoke(self) -> None:
        try:
            port = _pick_free_port()
        except PermissionError:
            self.skipTest("socket bind not permitted in current sandbox")
        env = dict(os.environ)
        proc = subprocess.Popen(
            ["python3", "run.py", "serve", "--host", "127.0.0.1", "--port", str(port)],
            cwd=self.repo_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        try:
            deadline = time.time() + 20
            last_error: Exception | None = None
            while time.time() < deadline:
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as response:
                        self.assertEqual(200, response.status)
                        break
                except Exception as error:  # noqa: BLE001
                    last_error = error
                    time.sleep(0.25)
            else:
                raise AssertionError(f"serve smoke failed: {last_error}")
        finally:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)

    def test_phase2_serve_smoke(self) -> None:
        try:
            port = _pick_free_port()
        except PermissionError:
            self.skipTest("socket bind not permitted in current sandbox")
        env = dict(os.environ)
        proc = subprocess.Popen(
            ["python3", "run.py", "serve-v2", "--host", "127.0.0.1", "--port", str(port)],
            cwd=self.repo_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        try:
            deadline = time.time() + 20
            last_error: Exception | None = None
            while time.time() < deadline:
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as response:
                        self.assertEqual(200, response.status)
                        body = response.read().decode("utf-8")
                        self.assertIn("Phase 2 (Ledger UI)", body)
                        break
                except Exception as error:  # noqa: BLE001
                    last_error = error
                    time.sleep(0.25)
            else:
                raise AssertionError(f"serve-v2 smoke failed: {last_error}")
        finally:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _read_csv(path: Path) -> list[dict[str, str]]:
    import csv

    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        return [dict(row) for row in reader]


if __name__ == "__main__":
    unittest.main()
