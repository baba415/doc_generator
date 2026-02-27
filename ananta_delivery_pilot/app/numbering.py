from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Dict

from .utils import ensure_dir, read_json, write_json


class NumberStore:
    def __init__(self, state_path: Path) -> None:
        self.state_path = state_path
        ensure_dir(state_path.parent)
        if not state_path.exists():
            write_json(
                state_path,
                {
                    "invoice_by_vendor_year": {},
                    "receipt_by_vendor_year": {},
                    "coa_revision": {},
                },
            )
        else:
            state = read_json(state_path)
            if "invoice_by_vendor_year" not in state:
                migrated = {
                    "invoice_by_vendor_year": {"DEFAULT": state.get("invoice_by_year", {})},
                    "receipt_by_vendor_year": {"DEFAULT": state.get("receipt_by_year", {})},
                    "coa_revision": state.get("coa_revision", {}),
                }
                write_json(state_path, migrated)

    def _state(self) -> Dict[str, Dict[str, int]]:
        return read_json(self.state_path)

    def _save(self, state: Dict[str, Dict[str, int]]) -> None:
        write_json(self.state_path, state)

    def next_invoice_no(self, vendor_code: str, year: str | None = None) -> str:
        state = self._state()
        year_value = year or str(datetime.utcnow().year)
        vendor = (vendor_code or "DEFAULT").upper()
        vendor_map = state["invoice_by_vendor_year"].setdefault(vendor, {})
        counter = int(vendor_map.get(year_value, 0)) + 1
        vendor_map[year_value] = counter
        self._save(state)
        return f"INV-{year_value}-{counter:04d}"

    def next_receipt_no(self, vendor_code: str, invoice_no: str, year: str | None = None) -> str:
        state = self._state()
        year_value = year or str(datetime.utcnow().year)
        vendor = (vendor_code or "DEFAULT").upper()
        vendor_map = state["receipt_by_vendor_year"].setdefault(vendor, {})
        counter = int(vendor_map.get(year_value, 0)) + 1
        vendor_map[year_value] = counter
        self._save(state)
        return f"RCPT-{invoice_no}-{counter:02d}"

    def waybill_no(self, invoice_no: str, seq: int = 1) -> str:
        return f"WB-{invoice_no}-{seq:02d}"

    def weighing_no(self, waybill_no: str, seq: int = 1) -> str:
        return f"WT-{waybill_no}-{seq:02d}"

    def next_coa_no(self, batch_id: str) -> str:
        state = self._state()
        revision_map = state["coa_revision"]
        key = batch_id or "UNSPECIFIED"
        revision = int(revision_map.get(key, 0)) + 1
        revision_map[key] = revision
        self._save(state)
        return f"COA-{key}-R{revision}"
