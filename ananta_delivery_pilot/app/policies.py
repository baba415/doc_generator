from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

from .schemas import Transaction
from .utils import read_json


@dataclass
class PolicyCheckResult:
    ok: bool
    violations: List[str]


class FunderPolicyEngine:
    def __init__(self, policies_dir: Path) -> None:
        self.policies_dir = policies_dir

    def load_policy(self, policy_file: str) -> Dict[str, Any]:
        return read_json(self.policies_dir / policy_file)

    def check_transaction(self, tx: Transaction, policy: Dict[str, Any]) -> PolicyCheckResult:
        violations: List[str] = []

        restricted_categories = set(policy.get("restricted_categories", []))
        transaction_categories = {item.tax_class for item in tx.line_items}
        blocked_categories = sorted(restricted_categories.intersection(transaction_categories))
        if blocked_categories:
            violations.append(f"Restricted categories in transaction: {', '.join(blocked_categories)}")

        allowed_modes = set(policy.get("allowed_funding_modes", []))
        if allowed_modes and tx.funding_mode not in allowed_modes:
            violations.append(f"Funding mode '{tx.funding_mode}' not allowed by funder policy")

        max_ticket = float(policy.get("max_ticket_value", 0.0))
        if max_ticket > 0 and tx.total_before_wht() > max_ticket:
            violations.append(
                f"Ticket value {tx.total_before_wht():,.2f} exceeds funder max {max_ticket:,.2f}"
            )

        max_tenor_days = int(policy.get("max_tenor_days", 0))
        if max_tenor_days > 0:
            # Lightweight check: expect payment terms to include integer days (e.g., "30 days")
            days = self._extract_days(tx.payment_terms)
            if days is not None and days > max_tenor_days:
                violations.append(f"Payment tenor {days} days exceeds funder max {max_tenor_days} days")

        return PolicyCheckResult(ok=len(violations) == 0, violations=violations)

    @staticmethod
    def _extract_days(text: str) -> int | None:
        digits = "".join(char for char in text if char.isdigit())
        if not digits:
            return None
        return int(digits)


class TaxDecisionEngine:
    def __init__(self, rules_path: Path) -> None:
        self.rules_path = rules_path
        self.rules = read_json(rules_path)

    def apply(self, tx: Transaction) -> Dict[str, Any]:
        # Conservative ruleset for pilot: explicit flags override defaults.
        vat_default_rate = float(self.rules.get("default_vat_rate", 7.5))
        wht_default_goods_rate = float(self.rules.get("default_wht_goods_rate", 2.0))

        vat_rate = tx.vat_rate if tx.vat_rate >= 0 else vat_default_rate
        if tx.wht_applicable:
            wht_rate = tx.wht_rate if tx.wht_rate >= 0 else wht_default_goods_rate
        else:
            wht_rate = 0.0

        vat_basis = "Configured transaction rule/override"
        if vat_rate == vat_default_rate and not tx.vat_exemption_reason:
            vat_basis = "Default VAT rule"

        wht_basis = "Configured transaction rule/override"
        if tx.wht_applicable and wht_rate == wht_default_goods_rate and not tx.wht_exemption_reason:
            wht_basis = "Default WHT goods rule"
        if not tx.wht_applicable:
            wht_basis = "Marked not applicable by transaction rule"

        outcome = {
            "vat_rate": vat_rate,
            "vat_basis": vat_basis,
            "vat_exemption_reason": tx.vat_exemption_reason,
            "wht_applicable": tx.wht_applicable,
            "wht_rate": wht_rate,
            "wht_basis": wht_basis,
            "wht_exemption_reason": tx.wht_exemption_reason,
        }

        return outcome
