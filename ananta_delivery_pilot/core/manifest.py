from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from .hashing import canonical_json_sha256
from .time import utc_now_iso_z


def build_manifest(
    *,
    delivery_id: str,
    sales_transaction_id: str,
    lpo_no: str,
    invoice_no: str,
    run_id: str,
    batch_id: str,
    buyer_id: str,
    vendor_of_record_id: str,
    docs: Iterable[dict[str, Any]],
    schema_version: str = "phase1.v1",
) -> dict[str, Any]:
    payload = {
        "schema_version": schema_version,
        "generated_at": utc_now_iso_z(),
        "delivery_id": delivery_id,
        "sales_transaction_id": sales_transaction_id,
        "lpo_no": lpo_no,
        "invoice_no": invoice_no,
        "run_id": run_id,
        "batch_id": batch_id,
        "buyer_id": buyer_id,
        "vendor_of_record_id": vendor_of_record_id,
        "documents": list(docs),
    }
    payload["content_sha256"] = canonical_json_sha256(payload)
    return payload


def write_manifest(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_to_pretty_json(payload), encoding="utf-8")


def _to_pretty_json(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True)

