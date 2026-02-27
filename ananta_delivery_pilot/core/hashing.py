from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any


_ISO_WITH_OFFSET = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+\-]\d{2}:\d{2})$")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_text(payload: str) -> str:
    return sha256_bytes(payload.encode("utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(8192)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def normalize_decimal(value: Any, scale: str = "0.001") -> str:
    decimal_value = Decimal(str(value))
    quantized = decimal_value.quantize(Decimal(scale), rounding=ROUND_HALF_UP)
    return format(quantized, "f")


def normalize_timestamp(value: str) -> str:
    if _ISO_WITH_OFFSET.match(value):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_for_hash(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _normalize_for_hash(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, list):
        return [_normalize_for_hash(item) for item in value]
    if isinstance(value, tuple):
        return [_normalize_for_hash(item) for item in value]
    if isinstance(value, Decimal):
        return normalize_decimal(value)
    if isinstance(value, float):
        return normalize_decimal(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        if _ISO_WITH_OFFSET.match(value):
            return normalize_timestamp(value)
        return value
    return value


def canonical_json(value: Any) -> str:
    normalized = _normalize_for_hash(value)
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_json_sha256(value: Any) -> str:
    return sha256_text(canonical_json(value))
