from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from core.hashing import sha256_file
from core.ids import new_ulid
from core.time import utc_now_iso_z


def output_delivery_dir(output_v2_root: Path, *, vendor_code: str, invoice_no: str) -> Path:
    target = output_v2_root / vendor_code.upper() / invoice_no
    target.mkdir(parents=True, exist_ok=True)
    return target


def persist_evidence_original(
    *,
    source_path: Path,
    dest_dir: Path,
    order_index: int,
) -> dict[str, Any]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    destination = dest_dir / f"{order_index:02d}-{source_path.name}"
    shutil.copy2(source_path, destination)
    return {
        "evidence_id": new_ulid(),
        "source_path": str(source_path),
        "stored_path": str(destination),
        "sha256": sha256_file(destination),
        "captured_at": utc_now_iso_z(),
    }

