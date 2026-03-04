from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from adapters.sqlite_repo import SQLiteRepo
from core.config import RuntimeConfig
from core.time import utc_now_iso_z


_AMOUNT_Q = Decimal("0.01")


@dataclass(frozen=True)
class BankEntry:
    date: date
    reference: str
    description: str
    amount_ngn: Decimal
    entry_type: str


@dataclass(frozen=True)
class PaymentRecord:
    rails_trade_id: str
    payment_id: str
    payment_date: date
    expected_ngn: Decimal
    bank_reference: str


@dataclass(frozen=True)
class ReconciliationResult:
    report: dict[str, Any]
    report_path: Path
    summary_text: str


def _norm_ref(value: str) -> str:
    return str(value or "").strip().upper()


def _fmt_naira(amount: float) -> str:
    return f"₦{amount:,.2f}"


def _as_float(value: Decimal) -> float:
    return float(value.quantize(_AMOUNT_Q, rounding=ROUND_HALF_UP))


def _parse_amount(raw: str, field_name: str, row_number: int) -> Decimal:
    text = str(raw or "").strip()
    if not text:
        raise ValueError(f"Row {row_number}: {field_name} is required")
    try:
        return Decimal(text).quantize(_AMOUNT_Q, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        raise ValueError(f'Row {row_number}: invalid amount "{text}"') from None


def _parse_date(raw: str, field_name: str, row_number: int) -> date:
    text = str(raw or "").strip()
    if not text:
        raise ValueError(f"Row {row_number}: {field_name} is required")
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise ValueError(f'Row {row_number}: invalid date format "{text}"') from None


def _business_day_distance(left: date, right: date) -> int:
    if left == right:
        return 0
    step = 1 if right > left else -1
    cursor = left
    count = 0
    while cursor != right:
        cursor = cursor + timedelta(days=step)
        if cursor.weekday() < 5:
            count += 1
    return count


def _read_bank_statement(csv_path: Path) -> list[BankEntry]:
    if not csv_path.exists():
        raise FileNotFoundError(f"Bank statement not found: {csv_path}")
    entries: list[BankEntry] = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError("Bank statement CSV requires a header row")
        headers = {str(name or "").strip() for name in reader.fieldnames}
        required = {"date", "reference", "amount_ngn"}
        missing = sorted(required - headers)
        if missing:
            raise ValueError(f"Bank statement missing required column(s): {', '.join(missing)}")
        for row_number, raw_row in enumerate(reader, start=2):
            row = {str(k or "").strip(): str(v or "").strip() for k, v in raw_row.items() if k is not None}
            if not any(row.values()):
                continue
            bank_date = _parse_date(row.get("date", ""), "date", row_number)
            reference = str(row.get("reference", "")).strip()
            if not reference:
                raise ValueError(f"Row {row_number}: reference is required")
            amount = _parse_amount(row.get("amount_ngn", ""), "amount_ngn", row_number)
            entries.append(
                BankEntry(
                    date=bank_date,
                    reference=reference,
                    description=str(row.get("description", "")).strip(),
                    amount_ngn=amount,
                    entry_type=str(row.get("type", "")).strip(),
                )
            )
    return entries


def _load_payments(conn: Any) -> list[PaymentRecord]:
    rows = conn.execute(
        """
        SELECT
          p.payment_id,
          p.payment_date,
          p.amount_received,
          p.external_reference,
          p.idempotency_key,
          p.receipt_no,
          COALESCE((
            SELECT COALESCE(c.contract_ref, c.contract_id)
            FROM payment_allocations pa
            JOIN sales_transactions st ON st.sales_transaction_id = pa.sales_transaction_id
            JOIN contracts c ON c.contract_id = st.contract_id
            WHERE pa.payment_id = p.payment_id
            ORDER BY pa.created_at ASC
            LIMIT 1
          ), '') AS rails_trade_id
        FROM payments p
        ORDER BY p.payment_date ASC, p.payment_id ASC
        """
    ).fetchall()
    payments: list[PaymentRecord] = []
    for index, row in enumerate(rows, start=1):
        payment_date = _parse_date(row["payment_date"], "payment_date", index)
        expected_ngn = _parse_amount(str(row["amount_received"]), "amount_received", index)
        bank_reference = str(row["external_reference"] or row["idempotency_key"] or row["receipt_no"] or "").strip()
        payments.append(
            PaymentRecord(
                rails_trade_id=str(row["rails_trade_id"] or ""),
                payment_id=str(row["payment_id"]),
                payment_date=payment_date,
                expected_ngn=expected_ngn,
                bank_reference=bank_reference,
            )
        )
    return payments


def _choose_best_candidate(payment: PaymentRecord, candidates: list[int], bank_entries: list[BankEntry]) -> int:
    return sorted(
        candidates,
        key=lambda idx: (
            _business_day_distance(payment.payment_date, bank_entries[idx].date),
            abs((bank_entries[idx].date - payment.payment_date).days),
            bank_entries[idx].date.isoformat(),
            _norm_ref(bank_entries[idx].reference),
        ),
    )[0]


def _match_payment(
    payment: PaymentRecord,
    *,
    unmatched_bank_indexes: set[int],
    bank_entries: list[BankEntry],
) -> tuple[int | None, str | None]:
    amount_and_date = [
        idx
        for idx in unmatched_bank_indexes
        if bank_entries[idx].amount_ngn == payment.expected_ngn
        and _business_day_distance(payment.payment_date, bank_entries[idx].date) <= 2
    ]
    payment_ref = _norm_ref(payment.bank_reference)
    if payment_ref:
        ref_filtered = [idx for idx in amount_and_date if _norm_ref(bank_entries[idx].reference) == payment_ref]
        if ref_filtered:
            return _choose_best_candidate(payment, ref_filtered, bank_entries), None
        same_ref = [idx for idx in unmatched_bank_indexes if _norm_ref(bank_entries[idx].reference) == payment_ref]
        if same_ref:
            if any(_business_day_distance(payment.payment_date, bank_entries[idx].date) <= 2 for idx in same_ref):
                return None, "amount mismatch for matching reference"
            return None, "matching reference found outside date window"
        if amount_and_date:
            return None, "reference mismatch for amount/date candidate"
        return None, "no matching bank entry within date window"
    if amount_and_date:
        return _choose_best_candidate(payment, amount_and_date, bank_entries), None
    return None, "no matching bank entry within date window"


def reconcile_weekly(
    *,
    root_dir: Path,
    bank_statement_path: Path,
    week: str,
    dry_run: bool = False,
    db_path: Path | None = None,
) -> ReconciliationResult:
    config = RuntimeConfig.load(root_dir)
    repo = SQLiteRepo(db_path or (config.state_dir / "drep.sqlite"))
    repo.init_db(config)
    with repo._connect() as conn:  # noqa: SLF001
        payments = _load_payments(conn)
    bank_entries = _read_bank_statement(bank_statement_path)

    unmatched_bank_indexes = set(range(len(bank_entries)))
    matched: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    total_expected_ngn = Decimal("0.00")
    total_matched_ngn = Decimal("0.00")

    for payment in payments:
        total_expected_ngn += payment.expected_ngn
        match_idx, reason = _match_payment(
            payment,
            unmatched_bank_indexes=unmatched_bank_indexes,
            bank_entries=bank_entries,
        )
        if match_idx is None:
            unmatched.append(
                {
                    "rails_trade_id": payment.rails_trade_id,
                    "payment_id": payment.payment_id,
                    "expected_ngn": _as_float(payment.expected_ngn),
                    "received_ngn": 0.0,
                    "reason": reason or "no matching bank entry within date window",
                }
            )
            continue
        unmatched_bank_indexes.remove(match_idx)
        bank = bank_entries[match_idx]
        total_matched_ngn += bank.amount_ngn
        matched.append(
            {
                "rails_trade_id": payment.rails_trade_id,
                "payment_id": payment.payment_id,
                "expected_ngn": _as_float(payment.expected_ngn),
                "received_ngn": _as_float(bank.amount_ngn),
                "bank_reference": bank.reference,
                "match_date": bank.date.isoformat(),
            }
        )

    orphan_details: list[dict[str, Any]] = []
    for idx in sorted(
        unmatched_bank_indexes,
        key=lambda i: (bank_entries[i].date.isoformat(), _norm_ref(bank_entries[i].reference)),
    ):
        bank = bank_entries[idx]
        orphan_details.append(
            {
                "bank_reference": bank.reference,
                "amount_ngn": _as_float(bank.amount_ngn),
                "date": bank.date.isoformat(),
                "description": bank.description,
                "reason": "no matching pilot payment record",
            }
        )

    total_delta_ngn = total_expected_ngn - total_matched_ngn
    report = {
        "week": week,
        "generated_at": utc_now_iso_z(),
        "bank_statement_file": str(bank_statement_path),
        "matched_count": len(matched),
        "unmatched_count": len(unmatched),
        "orphan_bank_entries": len(orphan_details),
        "total_matched_amount_ngn": _as_float(total_matched_ngn),
        "total_delta_ngn": _as_float(total_delta_ngn),
        "matched": matched,
        "unmatched": unmatched,
        "orphan_bank_entries_detail": orphan_details,
    }

    out_dir = root_dir / "reconciliation"
    out_path = out_dir / f"{week}.json"
    if not dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=4), encoding="utf-8")

    summary = "\n".join(
        [
            f"Reconciliation: {week}",
            f"  Bank statement: {bank_statement_path}",
            "  ",
            f"  Matched:    {len(matched):>3} payments ({_fmt_naira(_as_float(total_matched_ngn))})",
            f"  Unmatched:  {len(unmatched):>3} payments",
            f"  Orphan:     {len(orphan_details):>3} bank entries",
            f"  Delta:      {_fmt_naira(_as_float(total_delta_ngn))}",
            "  ",
            f"  Report: {out_path.relative_to(root_dir)}",
        ]
    )
    return ReconciliationResult(report=report, report_path=out_path, summary_text=summary)

