from __future__ import annotations

import unittest

from apps.web_v2 import (
    intake_decision_for_confidence,
    intake_field_class,
    resolve_intake_confidence_matrix,
)


class Phase2Pr8IntakeLogicTests(unittest.TestCase):
    def test_intake_field_class_mapping(self) -> None:
        self.assertEqual("identity", intake_field_class("buyer_id"))
        self.assertEqual("quantity", intake_field_class("expected_qty_kg"))
        self.assertEqual("pricing", intake_field_class("unit_price_basis"))
        self.assertEqual("date", intake_field_class("lpo_valid_to"))
        self.assertEqual("identity", intake_field_class("unknown_field"))

    def test_confidence_matrix_resolution_uses_defaults_and_overrides(self) -> None:
        matrix = resolve_intake_confidence_matrix(
            {
                "intake": {
                    "confidence_matrix": {
                        "identity": {"auto_apply_min": 0.97},
                        "pricing": {"review_min": 0.8},
                    }
                }
            }
        )
        self.assertEqual(0.97, matrix["identity"]["auto_apply_min"])
        self.assertEqual(0.75, matrix["identity"]["review_min"])
        self.assertEqual(0.95, matrix["pricing"]["auto_apply_min"])
        self.assertEqual(0.8, matrix["pricing"]["review_min"])

    def test_decision_thresholds_are_deterministic(self) -> None:
        matrix = resolve_intake_confidence_matrix(None)
        decision_a = intake_decision_for_confidence(
            field_name="buyer_id",
            confidence=0.94,
            matrix=matrix,
        )
        decision_b = intake_decision_for_confidence(
            field_name="buyer_id",
            confidence=0.80,
            matrix=matrix,
        )
        decision_c = intake_decision_for_confidence(
            field_name="buyer_id",
            confidence=0.70,
            matrix=matrix,
        )
        self.assertEqual("auto_applied", decision_a[0])
        self.assertEqual("needs_review", decision_b[0])
        self.assertEqual("blocked", decision_c[0])


if __name__ == "__main__":
    unittest.main()
