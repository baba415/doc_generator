from __future__ import annotations

import csv
import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from adapters.sqlite_repo import SQLiteRepo
from core.config import RuntimeConfig, infer_lane
from core.ids import new_ulid
from core.time import utc_now_iso_z
from core.units import kg_to_mt_decimal, mt_to_kg_int

try:
    from core.ids import generate_pilot_uuid as _generate_pilot_uuid
except Exception:
    def _generate_pilot_uuid() -> str:
        # Phase 1A may be absent on older branches; use UUID v4 fallback.
        return str(uuid.uuid4())


SUPPORTED_IMPORT_TYPES = {"trades", "deliveries", "counterparties", "payments"}

KNOWN_COLUMNS: dict[str, set[str]] = {
    "counterparties": {
        "party_id",
        "legal_name",
        "code",
        "tin",
        "rc_number",
        "address",
        "city_state_country",
        "website",
        "phone",
        "email",
        "bank_name",
        "bank_account_name",
        "bank_account_number",
        "bank_currency",
        "aliases",
        "core_uuid",
    },
    "trades": {
        "contract_id",
        "contract_ref",
        "lpo_no",
        "lpo_date",
        "buyer_id",
        "vendor_of_record_id",
        "operator_id",
        "source_id",
        "processor_id",
        "currency",
        "issue_date",
        "lpo_valid_from",
        "lpo_valid_to",
        "due_date",
        "due_terms",
        "product_code",
        "description",
        "line_no",
        "expected_qty",
        "expected_total_qty",
        "expected_total_qty_kg",
        "expected_total_qty_unit",
        "unit",
        "unit_price",
        "unit_price_basis",
        "expected_value",
        "expected_total_value",
        "over_delivery_tolerance_pct",
        "notes",
        "core_uuid",
    },
    "deliveries": {
        "delivery_id",
        "contract_id",
        "line_no",
        "delivery_ref",
        "run_id",
        "batch_id",
        "delivery_date",
        "delivered_qty",
        "unit",
        "unit_price",
        "unit_price_basis",
        "gross_amount",
        "truck_no",
        "driver_name",
        "driver_phone",
        "notes",
        "status",
        "dispatched_at",
        "delivered_at",
        "invoiced_at",
        "paid_at",
        "force_over_delivery_reason",
        "procurement_doc_ref",
        "core_uuid",
    },
    "payments": {
        "payment_id",
        "vendor_of_record_id",
        "buyer_id",
        "payment_date",
        "amount_received",
        "currency",
        "payment_method",
        "external_reference",
        "idempotency_key",
        "receipt_no",
        "receipt_doc_id",
        "sales_transaction_id",
        "allocated_amount",
        "allocation_date",
        "notes",
        "core_uuid",
    },
}


class RowRejected(ValueError):
    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


class RowDuplicate(ValueError):
    pass


@dataclass(frozen=True)
class RejectedRow:
    row_number: int
    field: str
    message: str


@dataclass
class ImportSummary:
    file_path: str
    total_rows: int = 0
    imported: int = 0
    skipped_duplicates: int = 0
    rejected: int = 0
    rejected_rows: list[RejectedRow] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def format_import_summary(summary: ImportSummary) -> str:
    lines = [
        "Import Summary:",
        f"  File: {summary.file_path}",
        f"  Total rows: {summary.total_rows}",
        f"  Imported: {summary.imported}",
        f"  Skipped (duplicates): {summary.skipped_duplicates}",
        f"  Rejected: {summary.rejected}",
    ]
    if summary.rejected_rows:
        lines.append("")
        lines.append("  Rejected rows:")
        for item in summary.rejected_rows:
            lines.append(f"    Row {item.row_number}: {item.field} - {item.message}")
    if summary.warnings:
        lines.append("")
        lines.append("  Warnings:")
        for warning in summary.warnings:
            lines.append(f"    {warning}")
    return "\n".join(lines)


class CsvImporter:
    def __init__(self, config: RuntimeConfig, repo: SQLiteRepo, *, dry_run: bool = False) -> None:
        self.config = config
        self.repo = repo
        self.dry_run = dry_run
        self._columns_cache: dict[str, set[str]] = {}
        self._seen_party_ids: set[str] = set()
        self._seen_contract_ids: set[str] = set()
        self._seen_contract_refs: set[tuple[str, str]] = set()
        self._seen_delivery_ids: set[str] = set()
        self._seen_delivery_batch_keys: set[tuple[str, str, str]] = set()
        self._seen_payment_ids: set[str] = set()
        self._seen_payment_idempotency: set[str] = set()
        self._seen_payment_receipts: set[str] = set()

    def import_file(self, *, import_type: str, file_path: Path) -> ImportSummary:
        normalized_type = str(import_type or "").strip().lower()
        if normalized_type not in SUPPORTED_IMPORT_TYPES:
            raise ValueError(f"Unsupported import type: {import_type}")
        if not file_path.exists():
            raise FileNotFoundError(f"CSV file not found: {file_path}")

        # Ensure schema exists before processing rows.
        self.repo.init_db(self.config)
        summary = ImportSummary(file_path=str(file_path))

        with file_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                raise ValueError("Header row is required (first row must contain column names)")
            headers = [str(name or "").strip() for name in reader.fieldnames]
            if any(not name for name in headers):
                raise ValueError("Header row contains an empty column name")
            unknown_columns = [name for name in headers if name not in KNOWN_COLUMNS[normalized_type]]
            if unknown_columns:
                summary.warnings.append(
                    f"Unrecognized columns ignored: {', '.join(sorted(unknown_columns))}"
                )

            conn = self.repo._connect()
            try:
                for row_number, raw_row in enumerate(reader, start=2):
                    row = self._normalize_row(raw_row)
                    if self._is_blank_row(row):
                        continue
                    summary.total_rows += 1
                    conn.execute("BEGIN")
                    try:
                        self._import_one_row(conn, import_type=normalized_type, row=row)
                    except RowDuplicate:
                        conn.execute("ROLLBACK")
                        summary.skipped_duplicates += 1
                        continue
                    except RowRejected as exc:
                        conn.execute("ROLLBACK")
                        summary.rejected += 1
                        summary.rejected_rows.append(
                            RejectedRow(row_number=row_number, field=exc.field, message=str(exc))
                        )
                        continue
                    except sqlite3.IntegrityError as exc:
                        conn.execute("ROLLBACK")
                        message = str(exc)
                        if "UNIQUE constraint failed" in message:
                            summary.skipped_duplicates += 1
                        else:
                            summary.rejected += 1
                            summary.rejected_rows.append(
                                RejectedRow(row_number=row_number, field="row", message=message)
                            )
                        continue
                    except Exception as exc:
                        conn.execute("ROLLBACK")
                        summary.rejected += 1
                        summary.rejected_rows.append(
                            RejectedRow(row_number=row_number, field="row", message=str(exc))
                        )
                        continue
                    else:
                        if self.dry_run:
                            conn.execute("ROLLBACK")
                        else:
                            conn.execute("COMMIT")
                        summary.imported += 1
            finally:
                conn.close()
        return summary

    def _import_one_row(self, conn: sqlite3.Connection, *, import_type: str, row: dict[str, str]) -> None:
        if import_type == "counterparties":
            self._import_counterparty(conn, row)
            return
        if import_type == "trades":
            self._import_trade(conn, row)
            return
        if import_type == "deliveries":
            self._import_delivery(conn, row)
            return
        if import_type == "payments":
            self._import_payment(conn, row)
            return
        raise ValueError(f"Unsupported import type: {import_type}")

    def _import_counterparty(self, conn: sqlite3.Connection, row: dict[str, str]) -> None:
        party_id = self._required_text(row, "party_id")
        legal_name = self._required_text(row, "legal_name")
        if party_id in self._seen_party_ids or self._exists(conn, "parties", "party_id", party_id):
            raise RowDuplicate(f"party_id duplicate: {party_id}")

        now = utc_now_iso_z()
        aliases = self._parse_aliases(row.get("aliases", ""))
        core_uuid = self._resolve_core_uuid(conn, "parties", row.get("core_uuid", ""))
        payload = {
            "party_id": party_id,
            "legal_name": legal_name,
            "code": self._optional_text(row, "code"),
            "tin": self._optional_text(row, "tin"),
            "rc_number": self._optional_text(row, "rc_number"),
            "address": self._optional_text(row, "address"),
            "city_state_country": self._optional_text(row, "city_state_country"),
            "website": self._optional_text(row, "website"),
            "phone": self._optional_text(row, "phone"),
            "email": self._optional_text(row, "email"),
            "bank_name": self._optional_text(row, "bank_name"),
            "bank_account_name": self._optional_text(row, "bank_account_name"),
            "bank_account_number": self._optional_text(row, "bank_account_number"),
            "bank_currency": self._optional_text(row, "bank_currency") or "NGN",
            "aliases_json": self._json_dump(aliases),
            "core_uuid": core_uuid,
            "created_at": now,
            "updated_at": now,
        }
        self._insert_filtered(conn, "parties", payload)
        self._seen_party_ids.add(party_id)

    def _import_trade(self, conn: sqlite3.Connection, row: dict[str, str]) -> None:
        contract_ref = self._optional_text(row, "contract_ref") or self._optional_text(row, "lpo_no")
        if not contract_ref:
            raise RowRejected("contract_ref", "missing required field")
        lpo_no = self._optional_text(row, "lpo_no") or contract_ref
        contract_id = self._optional_text(row, "contract_id") or new_ulid()
        if contract_id in self._seen_contract_ids or self._exists(conn, "contracts", "contract_id", contract_id):
            raise RowDuplicate(f"contract_id duplicate: {contract_id}")
        contract_ref_key = (contract_ref, lpo_no)
        if contract_ref_key in self._seen_contract_refs:
            raise RowDuplicate(f"contract_ref duplicate: {contract_ref}/{lpo_no}")
        if conn.execute(
            "SELECT 1 FROM contracts WHERE contract_ref = ? AND lpo_no = ? LIMIT 1",
            contract_ref_key,
        ).fetchone():
            raise RowDuplicate(f"contract_ref duplicate: {contract_ref}/{lpo_no}")

        buyer_id = self._required_text(row, "buyer_id")
        vendor_of_record_id = self._required_text(row, "vendor_of_record_id")
        operator_default = str(self.config.system_profile.operator_entity_id or "guildgate").strip() or "guildgate"
        operator_id = self._optional_text(row, "operator_id") or operator_default
        source_id = self._nullable_text(row, "source_id")
        processor_id = self._nullable_text(row, "processor_id")

        self._require_party(conn, "buyer_id", buyer_id)
        self._require_party(conn, "vendor_of_record_id", vendor_of_record_id)
        self._require_party(conn, "operator_id", operator_id)
        if source_id:
            self._require_party(conn, "source_id", source_id)
        if processor_id:
            self._require_party(conn, "processor_id", processor_id)

        issue_date = self._parse_date(row, "issue_date", required=True)
        lpo_date = self._parse_date(row, "lpo_date", required=False)
        due_date = self._parse_date(row, "due_date", required=False)
        lpo_valid_from = self._parse_date(row, "lpo_valid_from", required=False) or issue_date
        lpo_valid_to = self._parse_date(row, "lpo_valid_to", required=False)

        product_code = self._required_text(row, "product_code").upper()
        description = self._required_text(row, "description")
        line_no = self._parse_positive_int(row, "line_no", default=1)
        expected_qty = self._parse_positive_float(row, "expected_qty")
        unit = self._optional_text(row, "unit") or "kgs"
        expected_qty_kg = self._qty_to_kg(expected_qty, unit)
        unit_price = self._parse_positive_float(row, "unit_price")
        unit_price_basis = self._normalize_unit_price_basis(
            self._optional_text(row, "unit_price_basis"),
            unit_hint=unit,
        )
        expected_value = self._optional_float(row, "expected_value")
        computed_expected_value = self._gross_amount_from_kg(
            quantity_kg=expected_qty_kg,
            unit_price=unit_price,
            unit_price_basis=unit_price_basis,
        )
        if expected_value is None:
            expected_value = computed_expected_value

        total_qty_kg = self._optional_int(row, "expected_total_qty_kg")
        if total_qty_kg is not None and total_qty_kg <= 0:
            raise RowRejected("expected_total_qty_kg", f"value <= 0 (got {total_qty_kg})")
        if total_qty_kg is None:
            expected_total_qty = self._optional_float(row, "expected_total_qty")
            if expected_total_qty is not None:
                if expected_total_qty <= 0:
                    raise RowRejected("expected_total_qty", f"value <= 0 (got {expected_total_qty})")
                total_unit = self._optional_text(row, "expected_total_qty_unit") or unit
                total_qty_kg = self._qty_to_kg(expected_total_qty, total_unit)
            else:
                total_qty_kg = expected_qty_kg

        expected_total_value = self._optional_float(row, "expected_total_value")
        if expected_total_value is None:
            expected_total_value = expected_value
        over_delivery_tolerance_pct = self._optional_float(row, "over_delivery_tolerance_pct")
        if over_delivery_tolerance_pct is None:
            over_delivery_tolerance_pct = 5.0

        now = utc_now_iso_z()
        core_uuid = self._resolve_core_uuid(conn, "contracts", row.get("core_uuid", ""))
        contract_payload = {
            "contract_id": contract_id,
            "master_contract_id": None,
            "contract_ref": contract_ref,
            "lpo_no": lpo_no,
            "lpo_date": lpo_date,
            "buyer_id": buyer_id,
            "vendor_of_record_id": vendor_of_record_id,
            "operator_id": operator_id,
            "source_id": source_id,
            "processor_id": processor_id,
            "lane": infer_lane(vendor_of_record_id),
            "currency": (self._optional_text(row, "currency") or "NGN").upper(),
            "issue_date": issue_date,
            "lpo_valid_from": lpo_valid_from,
            "lpo_valid_to": lpo_valid_to,
            "lpo_state": "ACTIVE",
            "due_date": due_date,
            "due_terms": self._optional_text(row, "due_terms"),
            "expected_total_qty": float(kg_to_mt_decimal(total_qty_kg)),
            "expected_total_qty_kg": total_qty_kg,
            "expected_total_value": expected_total_value,
            "over_delivery_tolerance_pct": over_delivery_tolerance_pct,
            "status": "OPEN",
            "notes": self._optional_text(row, "notes"),
            "core_uuid": core_uuid,
            "created_at": now,
            "updated_at": now,
        }
        self._insert_filtered(conn, "contracts", contract_payload)

        line_payload = {
            "contract_line_id": new_ulid(),
            "contract_id": contract_id,
            "line_no": line_no,
            "product_code": product_code,
            "description": description,
            "expected_qty": expected_qty,
            "delivered_qty": 0.0,
            "expected_qty_kg": expected_qty_kg,
            "delivered_qty_kg": 0,
            "unit": unit,
            "unit_price": unit_price,
            "unit_price_basis": unit_price_basis,
            "expected_value": expected_value,
            "created_at": now,
            "updated_at": now,
        }
        self._insert_filtered(conn, "contract_line_items", line_payload)

        self._seen_contract_ids.add(contract_id)
        self._seen_contract_refs.add(contract_ref_key)

    def _import_delivery(self, conn: sqlite3.Connection, row: dict[str, str]) -> None:
        delivery_id = self._optional_text(row, "delivery_id") or new_ulid()
        if delivery_id in self._seen_delivery_ids or self._exists(conn, "deliveries", "delivery_id", delivery_id):
            raise RowDuplicate(f"delivery_id duplicate: {delivery_id}")

        contract_id = self._required_text(row, "contract_id")
        contract = conn.execute(
            "SELECT * FROM contracts WHERE contract_id = ?",
            (contract_id,),
        ).fetchone()
        if not contract:
            raise RowRejected("contract_id", f'unknown entity "{contract_id}"')
        contract = dict(contract)

        line_no = self._parse_positive_int(row, "line_no", default=1)
        line = conn.execute(
            "SELECT * FROM contract_line_items WHERE contract_id = ? AND line_no = ?",
            (contract_id, line_no),
        ).fetchone()
        if not line:
            raise RowRejected("line_no", f'unknown entity "{line_no}" for contract "{contract_id}"')
        line = dict(line)

        run_id = self._required_text(row, "run_id")
        batch_id = self._required_text(row, "batch_id")
        batch_key = (contract_id, run_id, batch_id)
        if batch_key in self._seen_delivery_batch_keys:
            raise RowDuplicate(f"duplicate run_id/batch_id: {run_id}/{batch_id}")
        if conn.execute(
            "SELECT 1 FROM deliveries WHERE contract_id = ? AND run_id = ? AND batch_id = ? LIMIT 1",
            batch_key,
        ).fetchone():
            raise RowDuplicate(f"duplicate run_id/batch_id: {run_id}/{batch_id}")

        delivery_date = self._parse_date(row, "delivery_date", required=True)
        delivered_qty = self._parse_positive_float(row, "delivered_qty")
        unit = self._optional_text(row, "unit") or str(line.get("unit") or "kgs")
        delivered_qty_kg = self._qty_to_kg(delivered_qty, unit)
        unit_price = self._optional_float(row, "unit_price")
        if unit_price is None:
            unit_price = float(line.get("unit_price") or 0.0)
        if unit_price <= 0:
            raise RowRejected("unit_price", f"value <= 0 (got {unit_price})")
        unit_price_basis = self._normalize_unit_price_basis(
            self._optional_text(row, "unit_price_basis") or str(line.get("unit_price_basis") or ""),
            unit_hint=unit,
        )
        gross_amount = self._optional_float(row, "gross_amount")
        if gross_amount is None:
            gross_amount = self._gross_amount_from_kg(
                quantity_kg=delivered_qty_kg,
                unit_price=unit_price,
                unit_price_basis=unit_price_basis,
            )

        now = utc_now_iso_z()
        core_uuid = self._resolve_core_uuid(conn, "deliveries", row.get("core_uuid", ""))
        delivery_payload = {
            "delivery_id": delivery_id,
            "contract_id": contract_id,
            "contract_line_id": line["contract_line_id"],
            "delivery_ref": self._optional_text(row, "delivery_ref"),
            "run_id": run_id,
            "batch_id": batch_id,
            "delivery_date": delivery_date,
            "delivered_qty": delivered_qty,
            "delivered_qty_kg": delivered_qty_kg,
            "unit": unit,
            "unit_price": unit_price,
            "unit_price_basis": unit_price_basis,
            "gross_amount": gross_amount,
            "truck_no": self._nullable_text(row, "truck_no"),
            "driver_name": self._nullable_text(row, "driver_name"),
            "driver_phone": self._nullable_text(row, "driver_phone"),
            "notes": self._nullable_text(row, "notes"),
            "status": self._optional_text(row, "status") or "PLANNED",
            "dispatched_at": self._nullable_text(row, "dispatched_at"),
            "delivered_at": self._nullable_text(row, "delivered_at"),
            "invoiced_at": self._nullable_text(row, "invoiced_at"),
            "paid_at": self._nullable_text(row, "paid_at"),
            "over_delivery_override_reason": self._nullable_text(row, "force_over_delivery_reason"),
            "core_uuid": core_uuid,
            "created_at": now,
            "updated_at": now,
        }
        self._insert_filtered(conn, "deliveries", delivery_payload)

        procurement_payload = {
            "procurement_id": new_ulid(),
            "contract_id": contract_id,
            "delivery_id": delivery_id,
            "source_id": contract.get("source_id"),
            "processor_id": contract.get("processor_id"),
            "product_code": line.get("product_code"),
            "quantity": delivered_qty,
            "quantity_kg": delivered_qty_kg,
            "unit": unit,
            "unit_cost": unit_price,
            "gross_amount": gross_amount,
            "run_id": run_id,
            "batch_id": batch_id,
            "document_ref": self._nullable_text(row, "procurement_doc_ref"),
            "created_at": now,
            "updated_at": now,
        }
        self._insert_filtered(conn, "procurements", procurement_payload)

        conn.execute(
            """
            UPDATE contract_line_items
            SET delivered_qty = delivered_qty + ?, delivered_qty_kg = delivered_qty_kg + ?, updated_at = ?
            WHERE contract_line_id = ?
            """,
            (delivered_qty, delivered_qty_kg, now, line["contract_line_id"]),
        )
        self._seen_delivery_ids.add(delivery_id)
        self._seen_delivery_batch_keys.add(batch_key)

    def _import_payment(self, conn: sqlite3.Connection, row: dict[str, str]) -> None:
        payment_id = self._optional_text(row, "payment_id") or new_ulid()
        if payment_id in self._seen_payment_ids or self._exists(conn, "payments", "payment_id", payment_id):
            raise RowDuplicate(f"payment_id duplicate: {payment_id}")

        vendor_id = self._required_text(row, "vendor_of_record_id")
        buyer_id = self._required_text(row, "buyer_id")
        self._require_party(conn, "vendor_of_record_id", vendor_id)
        self._require_party(conn, "buyer_id", buyer_id)

        payment_date = self._parse_date(row, "payment_date", required=True)
        amount_received = self._parse_positive_float(row, "amount_received")
        idempotency_key = self._required_text(row, "idempotency_key")
        receipt_no = self._required_text(row, "receipt_no")

        if idempotency_key in self._seen_payment_idempotency:
            raise RowDuplicate(f"idempotency_key duplicate: {idempotency_key}")
        if receipt_no in self._seen_payment_receipts:
            raise RowDuplicate(f"receipt_no duplicate: {receipt_no}")
        if self._exists(conn, "payments", "idempotency_key", idempotency_key):
            raise RowDuplicate(f"idempotency_key duplicate: {idempotency_key}")
        if self._exists(conn, "payments", "receipt_no", receipt_no):
            raise RowDuplicate(f"receipt_no duplicate: {receipt_no}")

        now = utc_now_iso_z()
        core_uuid = self._resolve_core_uuid(conn, "payments", row.get("core_uuid", ""))
        payment_payload = {
            "payment_id": payment_id,
            "vendor_of_record_id": vendor_id,
            "buyer_id": buyer_id,
            "payment_date": payment_date,
            "amount_received": amount_received,
            "currency": (self._optional_text(row, "currency") or "NGN").upper(),
            "payment_method": self._optional_text(row, "payment_method") or "Bank Transfer",
            "external_reference": self._nullable_text(row, "external_reference"),
            "idempotency_key": idempotency_key,
            "receipt_no": receipt_no,
            "receipt_doc_id": self._nullable_text(row, "receipt_doc_id"),
            "core_uuid": core_uuid,
            "created_at": now,
            "updated_at": now,
        }
        self._insert_filtered(conn, "payments", payment_payload)

        sales_transaction_id = self._optional_text(row, "sales_transaction_id")
        if sales_transaction_id:
            if not self._exists(conn, "sales_transactions", "sales_transaction_id", sales_transaction_id):
                raise RowRejected("sales_transaction_id", f'unknown entity "{sales_transaction_id}"')
            allocated_amount = self._optional_float(row, "allocated_amount")
            if allocated_amount is None:
                allocated_amount = amount_received
            if allocated_amount <= 0:
                raise RowRejected("allocated_amount", f"value <= 0 (got {allocated_amount})")
            allocation_date = self._parse_date(row, "allocation_date", required=False) or payment_date
            allocation_payload = {
                "allocation_id": new_ulid(),
                "payment_id": payment_id,
                "sales_transaction_id": sales_transaction_id,
                "allocated_amount": allocated_amount,
                "allocation_date": allocation_date,
                "notes": self._optional_text(row, "notes"),
                "created_at": now,
            }
            self._insert_filtered(conn, "payment_allocations", allocation_payload)

        self._seen_payment_ids.add(payment_id)
        self._seen_payment_idempotency.add(idempotency_key)
        self._seen_payment_receipts.add(receipt_no)

    def _normalize_row(self, row: dict[str, Any]) -> dict[str, str]:
        cleaned: dict[str, str] = {}
        for key, value in row.items():
            if key is None:
                continue
            cleaned[str(key).strip()] = str(value or "").strip()
        return cleaned

    def _is_blank_row(self, row: dict[str, str]) -> bool:
        return all(not str(value or "").strip() for value in row.values())

    def _required_text(self, row: dict[str, str], field: str) -> str:
        value = str(row.get(field, "") or "").strip()
        if not value:
            raise RowRejected(field, "missing required field")
        return value

    def _optional_text(self, row: dict[str, str], field: str) -> str:
        return str(row.get(field, "") or "").strip()

    def _nullable_text(self, row: dict[str, str], field: str) -> str | None:
        value = self._optional_text(row, field)
        return value or None

    def _optional_float(self, row: dict[str, str], field: str) -> float | None:
        value = self._optional_text(row, field)
        if not value:
            return None
        try:
            return float(value)
        except Exception:
            raise RowRejected(field, f'not a valid number "{value}"') from None

    def _optional_int(self, row: dict[str, str], field: str) -> int | None:
        value = self._optional_text(row, field)
        if not value:
            return None
        try:
            return int(round(float(value)))
        except Exception:
            raise RowRejected(field, f'not a valid integer "{value}"') from None

    def _parse_positive_float(self, row: dict[str, str], field: str) -> float:
        raw = self._required_text(row, field)
        try:
            value = float(raw)
        except Exception:
            raise RowRejected(field, f'not a valid number "{raw}"') from None
        if value <= 0:
            raise RowRejected(field, f"value <= 0 (got {raw})")
        return value

    def _parse_positive_int(self, row: dict[str, str], field: str, *, default: int | None = None) -> int:
        raw = self._optional_text(row, field)
        if not raw:
            if default is None:
                raise RowRejected(field, "missing required field")
            return default
        try:
            value = int(round(float(raw)))
        except Exception:
            raise RowRejected(field, f'not a valid integer "{raw}"') from None
        if value <= 0:
            raise RowRejected(field, f"value <= 0 (got {raw})")
        return value

    def _parse_date(self, row: dict[str, str], field: str, *, required: bool) -> str | None:
        value = self._optional_text(row, field)
        if not value:
            if required:
                raise RowRejected(field, "missing required field")
            return None
        try:
            return date.fromisoformat(value).isoformat()
        except Exception:
            raise RowRejected(field, f'invalid date format "{value}"') from None

    def _parse_aliases(self, raw: str) -> list[str]:
        value = str(raw or "").strip()
        if not value:
            return []
        if value.startswith("[") and value.endswith("]"):
            try:
                parsed = json.loads(value)
            except Exception:
                parsed = []
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        splitter = "|" if "|" in value else ";"
        return [item.strip() for item in value.split(splitter) if item.strip()]

    def _json_dump(self, value: Any) -> str:
        return json.dumps(value, ensure_ascii=True)

    def _table_columns(self, conn: sqlite3.Connection, table_name: str) -> set[str]:
        if table_name in self._columns_cache:
            return self._columns_cache[table_name]
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        columns = {str(row["name"]) for row in rows}
        self._columns_cache[table_name] = columns
        return columns

    def _insert_filtered(self, conn: sqlite3.Connection, table: str, payload: dict[str, Any]) -> None:
        columns = self._table_columns(conn, table)
        filtered = {key: value for key, value in payload.items() if key in columns}
        if not filtered:
            raise ValueError(f"No valid columns found for table '{table}'")
        keys = list(filtered.keys())
        placeholders = ", ".join(["?"] * len(keys))
        sql = f"INSERT INTO {table}({', '.join(keys)}) VALUES({placeholders})"
        values = tuple(filtered[key] for key in keys)
        conn.execute(sql, values)

    def _exists(self, conn: sqlite3.Connection, table: str, column: str, value: str) -> bool:
        return bool(
            conn.execute(
                f"SELECT 1 FROM {table} WHERE {column} = ? LIMIT 1",
                (value,),
            ).fetchone()
        )

    def _require_party(self, conn: sqlite3.Connection, field: str, party_id: str) -> None:
        if not self._exists(conn, "parties", "party_id", party_id):
            raise RowRejected(field, f'unknown entity "{party_id}"')

    def _resolve_core_uuid(self, conn: sqlite3.Connection, table_name: str, provided_value: str) -> str | None:
        if "core_uuid" not in self._table_columns(conn, table_name):
            return None
        provided = str(provided_value or "").strip()
        return provided or _generate_pilot_uuid()

    def _qty_to_kg(self, quantity: float, unit: str) -> int:
        unit_norm = str(unit or "").strip().lower()
        if unit_norm in {"kg", "kgs", "kilogram", "kilograms"}:
            return int(round(quantity))
        if unit_norm in {"mt", "ton", "tons", "tonne", "tonnes"}:
            return mt_to_kg_int(quantity)
        return int(round(quantity))

    def _normalize_unit_price_basis(self, basis: str | None, *, unit_hint: str = "") -> str:
        basis_norm = str(basis or "").strip().upper()
        if not basis_norm:
            hint = str(unit_hint or "").strip().lower()
            basis_norm = "MT" if hint in {"mt", "ton", "tons", "tonne", "tonnes"} else "KG"
        if basis_norm not in {"KG", "MT"}:
            raise RowRejected("unit_price_basis", "must be KG or MT")
        return basis_norm

    def _gross_amount_from_kg(self, *, quantity_kg: int, unit_price: float, unit_price_basis: str) -> float:
        qty = Decimal(str(quantity_kg))
        price = Decimal(str(unit_price))
        basis = self._normalize_unit_price_basis(unit_price_basis)
        unit_price_kg = price if basis == "KG" else (price / Decimal("1000"))
        return float((qty * unit_price_kg).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def run_csv_import(
    *,
    import_type: str,
    file_path: Path,
    root_dir: Path,
    dry_run: bool = False,
    db_path: Path | None = None,
) -> ImportSummary:
    config = RuntimeConfig.load(root_dir)
    resolved_db_path = db_path or (config.state_dir / "drep.sqlite")
    repo = SQLiteRepo(resolved_db_path)
    importer = CsvImporter(config, repo, dry_run=dry_run)
    return importer.import_file(import_type=import_type, file_path=file_path)
