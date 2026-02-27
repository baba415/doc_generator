from __future__ import annotations

from typing import Any


TIN_PLACEHOLDER_VALUES = {"", "TIN_PENDING", "TIN-PENDING", "PENDING", "PENDING_TIN"}


def ensure_vendor_tin(tin: str, *, allow_placeholder_tin: bool) -> None:
    normalized = (tin or "").strip().upper()
    if normalized in TIN_PLACEHOLDER_VALUES and not allow_placeholder_tin:
        raise ValueError("Missing vendor-of-record TIN (or placeholder TIN without --allow-placeholder-tin)")


def ensure_run_and_batch(*, run_id: str, batch_id: str) -> None:
    if not run_id.strip():
        raise ValueError("run_id is required")
    if not batch_id.strip():
        raise ValueError("batch_id is required")


def ensure_within_overdelivery_tolerance(
    *,
    delivered_qty: float,
    expected_qty: float,
    tolerance_pct: float,
    force_reason: str | None,
) -> None:
    if expected_qty <= 0:
        return
    max_qty = expected_qty * (1.0 + (tolerance_pct / 100.0))
    if delivered_qty > max_qty and not (force_reason or "").strip():
        raise ValueError(
            f"Over-delivery {delivered_qty:.2f} exceeds tolerance {tolerance_pct:.2f}% over expected {expected_qty:.2f}; use force override with reason."
        )


def ensure_required_coa_rows(coa_rows: list[dict[str, Any]], profile_rows: list[dict[str, Any]]) -> None:
    normalized_rows = {
        _normalize(row.get("parameter", "")): row
        for row in coa_rows
        if _normalize(row.get("parameter", ""))
    }
    missing: list[str] = []
    for profile_row in profile_rows:
        parameter = str(profile_row.get("parameter", "")).strip()
        key = _normalize(parameter)
        if not key:
            continue
        row = normalized_rows.get(key)
        if not row:
            missing.append(parameter)
            continue
        if str(row.get("result", "")).strip() == "":
            missing.append(parameter)
    if missing:
        raise ValueError(f"Missing COA results for required rows: {', '.join(missing)}")


def _normalize(value: str) -> str:
    return "".join(ch.lower() for ch in str(value) if ch.isalnum())

