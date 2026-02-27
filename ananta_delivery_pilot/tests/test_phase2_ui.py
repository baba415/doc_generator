from __future__ import annotations

import shutil
import signal
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

from adapters.sqlite_repo import SQLiteRepo
from core.config import RuntimeConfig
from domain.services import Phase1Service


class Phase2UiRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.temp_dir = Path(tempfile.mkdtemp(prefix="phase2-ui-tests-"))
        shutil.copytree(self.repo_root / "config", self.temp_dir / "config")
        (self.temp_dir / ".state").mkdir(parents=True, exist_ok=True)
        (self.temp_dir / "output_v2").mkdir(parents=True, exist_ok=True)
        self.config = RuntimeConfig.load(self.temp_dir)
        self.repo = SQLiteRepo(self.config.state_dir / "drep.sqlite")
        self.service = Phase1Service(self.config, self.repo)
        self.service.init_db()

        contract = self.service.create_contract(
            {
                "contract_ref": "LPO-UI-ROUTES-001",
                "lpo_no": "LPO-UI-ROUTES-001",
                "lpo_date": "2026-02-23",
                "buyer_id": "buyer_nycil",
                "vendor_of_record_id": "ananta_flows",
                "operator_id": "guildgate",
                "source_id": "ananta_flows",
                "processor_id": "processor_partner_refinery",
                "currency": "NGN",
                "issue_date": "2026-02-23",
                "lpo_valid_from": "2026-02-23",
                "lpo_valid_to": "2026-03-31",
                "expected_total_qty": 150.0,
                "expected_total_value": 340500000.0,
                "lines": [
                    {
                        "product_code": "RBDPO",
                        "description": "RBDPO route test",
                        "expected_qty": 150.0,
                        "unit": "mt",
                        "unit_price": 2270.0,
                        "unit_price_basis": "KG",
                        "expected_value": 340500000.0,
                    }
                ],
            },
            allow_placeholder_tin=True,
        )
        self.contract_id = str(contract["contract_id"])
        self.service.plan_deliveries(
            contract_id=self.contract_id,
            start_date="2026-02-23",
            cadence="daily",
            max_lots_per_day=2,
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_workflow_routes_smoke(self) -> None:
        try:
            port = _pick_free_port()
        except PermissionError:
            self.skipTest("socket bind not permitted in current sandbox")

        command = [
            "python3",
            "-c",
            (
                "from pathlib import Path; "
                "from apps.web_v2 import run_server_v2; "
                f"run_server_v2(Path(r'{str(self.temp_dir)}'), host='127.0.0.1', port={port})"
            ),
        ]
        proc = subprocess.Popen(
            command,
            cwd=self.repo_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            _wait_for_route(port, "/v2/portfolio")
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=3) as response:
                self.assertEqual(200, response.status)
                root_body = response.read().decode("utf-8")
                self.assertIn("Phase 2 (Ledger UI)", root_body)
            routes = [
                "/v2/portfolio",
                "/v2/intake",
                f"/v2/contracts/{self.contract_id}/plan",
                f"/v2/contracts/{self.contract_id}/execute",
                f"/v2/contracts/{self.contract_id}/settle",
                "/v2/exceptions",
            ]
            for route in routes:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}{route}", timeout=3) as response:
                    self.assertEqual(200, response.status)
                    body = response.read().decode("utf-8")
                    self.assertIn("Phase 2 (Ledger UI)", body)
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


def _wait_for_route(port: int, route: str) -> None:
    deadline = time.time() + 20
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{route}", timeout=2) as response:
                if response.status == 200:
                    return
        except Exception as error:  # noqa: BLE001
            last_error = error
            time.sleep(0.25)
    raise AssertionError(f"Unable to reach route {route}: {last_error}")


if __name__ == "__main__":
    unittest.main()
