from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from adapters.lpo_parser import PARSER_VERSION, parse_lpo
from core.config import RuntimeConfig


class LpoParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.temp_dir = Path(tempfile.mkdtemp(prefix="lpo-parser-tests-"))
        shutil.copytree(self.repo_root / "config", self.temp_dir / "config")
        (self.temp_dir / ".state").mkdir(parents=True, exist_ok=True)
        (self.temp_dir / "output_v2").mkdir(parents=True, exist_ok=True)
        self.config = RuntimeConfig.load(self.temp_dir)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_known_json_normalizes_qty_and_entities(self) -> None:
        payload = {
            "lpo_no": "LPO-PARSER-001",
            "buyer_id": "buyer_nycil",
            "vendor_of_record_id": "ananta_flows",
            "source_id": "ananta_flows",
            "processor_id": "processor_partner_refinery",
            "product_code": "RBDSO",
            "expected_qty_mt": 150.0,
            "unit_price": 2270.0,
            "unit_price_basis": "KG",
            "issue_date": "2026-02-23",
        }
        path = self.temp_dir / "lpo.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        result = parse_lpo(path, config=self.config)
        fields = {field.field_name: field for field in result.fields}
        self.assertEqual(PARSER_VERSION, result.parser_version)
        self.assertEqual("LPO-PARSER-001", fields["lpo_no"].proposed_value)
        self.assertEqual("buyer_nycil", fields["buyer_id"].proposed_value)
        self.assertEqual("ananta_flows", fields["vendor_of_record_id"].proposed_value)
        self.assertEqual(150000, int(fields["expected_qty_kg"].proposed_value))
        self.assertEqual("150.000", str(fields["expected_qty_mt"].proposed_value))

    def test_unknown_entity_produces_low_confidence_suggestions(self) -> None:
        path = self.temp_dir / "lpo.txt"
        path.write_text(
            "\n".join(
                [
                    "LPO No: LPO-UNKNOWN-001",
                    "Buyer: Nycl Ltd",
                    "Supplier: Anata Flowz",
                    "Product: RBDSO",
                    "Quantity: 150",
                    "Unit Price: 2270",
                ]
            ),
            encoding="utf-8",
        )
        result = parse_lpo(path, config=self.config)
        fields = {field.field_name: field for field in result.fields}
        self.assertLess(float(fields["buyer_id"].confidence), 1.0)
        self.assertTrue(fields["buyer_id"].suggestions)


if __name__ == "__main__":
    unittest.main()
