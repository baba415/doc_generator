from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.config import RuntimeConfig
from core.hashing import sha256_file
from core.units import kg_to_mt_str, mt_to_kg_int

try:  # pragma: no cover - import guard
    from pypdf import PdfReader
except Exception:  # pragma: no cover - optional dependency
    PdfReader = None  # type: ignore[assignment]


PARSER_VERSION = "phase2_0_1.v1"


_DATE_RE = re.compile(r"\b(20\d{2}-\d{2}-\d{2}|20\d{2}/\d{2}/\d{2}|\d{2}[-/]\d{2}[-/]20\d{2})\b")
_NUMBER_RE = re.compile(r"-?\d+(?:,\d{3})*(?:\.\d+)?")


@dataclass(frozen=True)
class ParsedField:
    field_name: str
    proposed_value: Any
    confidence: float
    source_type: str
    source_ref: str
    reason_code: str
    suggestions: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "field_name": self.field_name,
            "proposed_value": self.proposed_value,
            "confidence": round(float(self.confidence), 4),
            "source_type": self.source_type,
            "source_ref": self.source_ref,
            "reason_code": self.reason_code,
            "suggestions": self.suggestions,
        }


@dataclass(frozen=True)
class ParsedLpoResult:
    file_sha256: str
    parser_version: str
    fields: list[ParsedField]
    raw_extract_summary: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_sha256": self.file_sha256,
            "parser_version": self.parser_version,
            "fields": [field.as_dict() for field in self.fields],
            "raw_extract_summary": self.raw_extract_summary,
        }


def parse_lpo(
    path: Path,
    *,
    config: RuntimeConfig,
    hints: dict[str, Any] | None = None,
) -> ParsedLpoResult:
    source = path.expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"LPO file not found: {source}")
    hints = hints or {}
    raw_data, source_type, source_ref = _extract_payload(source)

    file_sha = sha256_file(source)
    normalized_fields: list[ParsedField] = []

    lpo_no = _coalesce(raw_data, hints, ["lpo_no", "lpo", "contract_ref", "po_no", "purchase_order_no"])
    normalized_fields.append(
        ParsedField(
            field_name="lpo_no",
            proposed_value=lpo_no,
            confidence=_confidence(lpo_no, high=0.98, low=0.55),
            source_type=source_type,
            source_ref=source_ref,
            reason_code="parser_lpo_no" if lpo_no else "missing_lpo_no",
            suggestions=[],
        )
    )

    for name in ("lpo_date", "issue_date", "lpo_valid_from", "lpo_valid_to"):
        date_value = _normalize_date(_coalesce(raw_data, hints, [name, _date_alias(name)]))
        normalized_fields.append(
            ParsedField(
                field_name=name,
                proposed_value=date_value,
                confidence=_confidence(date_value, high=0.93, low=0.6),
                source_type=source_type,
                source_ref=source_ref,
                reason_code=f"parser_{name}" if date_value else f"missing_{name}",
                suggestions=[],
            )
        )

    buyer_raw = _coalesce(raw_data, hints, ["buyer_id", "buyer_name", "buyer"])
    buyer = _resolve_entity(config, value=buyer_raw, role="buyer_")
    normalized_fields.append(buyer.as_field("buyer_id"))

    vendor_raw = _coalesce(raw_data, hints, ["vendor_of_record_id", "vendor_name", "supplier", "supplier_name"])
    vendor = _resolve_entity(config, value=vendor_raw, role="")
    normalized_fields.append(vendor.as_field("vendor_of_record_id"))

    source_raw = _coalesce(raw_data, hints, ["source_id", "source_name", "merchant_principal"])
    source_entity = _resolve_entity(config, value=source_raw, role="")
    normalized_fields.append(source_entity.as_field("source_id"))

    processor_raw = _coalesce(raw_data, hints, ["processor_id", "processor_name", "refinery", "processor"])
    processor = _resolve_entity(config, value=processor_raw, role="processor_")
    normalized_fields.append(processor.as_field("processor_id"))

    currency = str(_coalesce(raw_data, hints, ["currency"]) or "NGN").upper().strip()
    normalized_fields.append(
        ParsedField(
            field_name="currency",
            proposed_value=currency,
            confidence=0.95 if currency else 0.55,
            source_type=source_type,
            source_ref=source_ref,
            reason_code="parser_currency" if currency else "missing_currency",
            suggestions=[],
        )
    )

    product_code = str(_coalesce(raw_data, hints, ["product_code", "product", "material"]) or "").upper().strip()
    normalized_fields.append(
        ParsedField(
            field_name="product_code",
            proposed_value=product_code or None,
            confidence=_confidence(product_code, high=0.95, low=0.55),
            source_type=source_type,
            source_ref=source_ref,
            reason_code="parser_product_code" if product_code else "missing_product_code",
            suggestions=[],
        )
    )

    description = _coalesce(raw_data, hints, ["description", "item_description", "line_description"])
    normalized_fields.append(
        ParsedField(
            field_name="description",
            proposed_value=description,
            confidence=_confidence(description, high=0.9, low=0.6),
            source_type=source_type,
            source_ref=source_ref,
            reason_code="parser_description" if description else "missing_description",
            suggestions=[],
        )
    )

    qty_value = _coalesce(raw_data, hints, ["expected_qty", "quantity", "qty", "expected_total_qty", "expected_qty_mt"])
    qty_unit = str(_coalesce(raw_data, hints, ["unit", "qty_unit", "expected_qty_unit"]) or "MT").strip().upper()
    quantity_kg = _normalize_qty_to_kg(qty_value, qty_unit)
    qty_mt = kg_to_mt_str(quantity_kg) if quantity_kg > 0 else None
    qty_conf = 0.96 if quantity_kg > 0 else 0.55
    qty_reason = "parser_quantity" if quantity_kg > 0 else "missing_quantity"
    normalized_fields.append(
        ParsedField(
            field_name="expected_qty_mt",
            proposed_value=qty_mt,
            confidence=qty_conf,
            source_type=source_type,
            source_ref=source_ref,
            reason_code=qty_reason,
            suggestions=[],
        )
    )
    normalized_fields.append(
        ParsedField(
            field_name="expected_qty_kg",
            proposed_value=quantity_kg if quantity_kg > 0 else None,
            confidence=qty_conf,
            source_type="normalized",
            source_ref=source_ref,
            reason_code=qty_reason,
            suggestions=[],
        )
    )

    unit_price_value = _coalesce(raw_data, hints, ["unit_price", "price", "rate", "unit_rate"])
    unit_price = _normalize_float(unit_price_value)
    unit_price_basis = str(_coalesce(raw_data, hints, ["unit_price_basis", "price_basis"]) or "").strip().upper()
    if unit_price_basis not in {"KG", "MT"}:
        unit_price_basis = "MT" if qty_unit in {"MT", "TON", "TONNE", "TONS", "TONNES"} and unit_price and unit_price > 10000 else "KG"
    price_conf = 0.95 if unit_price and unit_price > 0 else 0.55
    price_reason = "parser_unit_price" if unit_price and unit_price > 0 else "missing_unit_price"
    normalized_fields.append(
        ParsedField(
            field_name="unit_price",
            proposed_value=unit_price if unit_price and unit_price > 0 else None,
            confidence=price_conf,
            source_type=source_type,
            source_ref=source_ref,
            reason_code=price_reason,
            suggestions=[],
        )
    )
    normalized_fields.append(
        ParsedField(
            field_name="unit_price_basis",
            proposed_value=unit_price_basis,
            confidence=0.9 if unit_price_basis else 0.6,
            source_type="normalized",
            source_ref=source_ref,
            reason_code="normalized_unit_price_basis",
            suggestions=[],
        )
    )

    raw_summary = {
        "path": str(source),
        "source_type": source_type,
        "source_ref": source_ref,
        "keys": sorted(raw_data.keys()),
    }
    return ParsedLpoResult(
        file_sha256=file_sha,
        parser_version=PARSER_VERSION,
        fields=normalized_fields,
        raw_extract_summary=raw_summary,
    )


@dataclass(frozen=True)
class _EntityResolution:
    field_name: str
    value: str | None
    confidence: float
    source_type: str
    source_ref: str
    reason_code: str
    suggestions: list[dict[str, Any]]

    def as_field(self, field_name: str) -> ParsedField:
        return ParsedField(
            field_name=field_name,
            proposed_value=self.value,
            confidence=self.confidence,
            source_type=self.source_type,
            source_ref=self.source_ref,
            reason_code=self.reason_code,
            suggestions=self.suggestions,
        )


def _resolve_entity(config: RuntimeConfig, *, value: Any, role: str) -> _EntityResolution:
    raw = str(value or "").strip()
    if not raw:
        return _EntityResolution(
            field_name=role,
            value=None,
            confidence=0.0,
            source_type="registry_match",
            source_ref="missing",
            reason_code="missing_entity",
            suggestions=[],
        )
    if raw in config.registry.entities and (not role or raw.startswith(role)):
        return _EntityResolution(
            field_name=role,
            value=raw,
            confidence=1.0,
            source_type="registry_match",
            source_ref="exact_id",
            reason_code="exact_id",
            suggestions=[],
        )
    resolved = config.registry.resolve_id(raw)
    if resolved and (not role or resolved.startswith(role)):
        return _EntityResolution(
            field_name=role,
            value=resolved,
            confidence=0.94,
            source_type="registry_match",
            source_ref="alias",
            reason_code="alias_match",
            suggestions=[],
        )
    suggestions = _entity_suggestions(config=config, raw=raw, role=role)
    candidate = suggestions[0]["entity_id"] if suggestions else None
    confidence = float(suggestions[0]["confidence"]) if suggestions else 0.0
    decision_conf = min(confidence, 0.79)
    return _EntityResolution(
        field_name=role,
        value=candidate,
        confidence=decision_conf,
        source_type="registry_match",
        source_ref="fuzzy",
        reason_code="fuzzy_match" if candidate else "no_match",
        suggestions=suggestions,
    )


def _entity_suggestions(config: RuntimeConfig, *, raw: str, role: str) -> list[dict[str, Any]]:
    from difflib import SequenceMatcher

    query = _norm(raw)
    scored: list[tuple[float, str]] = []
    for entity_id, entity in config.registry.entities.items():
        if role and not entity_id.startswith(role):
            continue
        names = [entity.name, *list(entity.aliases or [])]
        best = 0.0
        for name in names:
            candidate = _norm(name)
            if not candidate:
                continue
            score = SequenceMatcher(a=query, b=candidate).ratio()
            if candidate in query or query in candidate:
                score = max(score, 0.86)
            best = max(best, score)
        if best > 0:
            scored.append((best, entity_id))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [
        {
            "entity_id": entity_id,
            "label": config.registry.get(entity_id).name,
            "confidence": round(float(score), 4),
        }
        for score, entity_id in scored[:3]
    ]


def _extract_payload(path: Path) -> tuple[dict[str, Any], str, str]:
    suffix = path.suffix.lower()
    if suffix == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        return _flatten_payload(payload), "parser_json", f"{path}#json"
    if suffix in {".txt", ".csv"}:
        text = path.read_text(encoding="utf-8", errors="ignore")
        return _extract_key_values(text), "parser_text", f"{path}#text"
    if suffix == ".pdf":
        text = _extract_pdf_text(path)
        return _extract_key_values(text), "parser_text", f"{path}#pdf"
    text = path.read_text(encoding="utf-8", errors="ignore")
    return _extract_key_values(text), "filename_hint", f"{path}#fallback"


def _extract_pdf_text(path: Path) -> str:
    if PdfReader is None:
        return ""
    try:
        reader = PdfReader(str(path))
    except Exception:
        return ""
    pages: list[str] = []
    for page in reader.pages[:4]:
        try:
            pages.append(page.extract_text() or "")
        except Exception:
            continue
    return "\n".join(pages)


def _extract_key_values(text: str) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in lines:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key_norm = _norm(key)
        value = value.strip()
        if not value:
            continue
        payload[key_norm] = value

    payload.setdefault("lpo_no", _regex_pick(text, r"\b(?:LPO|PO)\s*(?:NO|NUMBER|#)?\s*[:\-]?\s*([A-Z0-9\-\/]+)"))
    payload.setdefault("buyer_name", _regex_pick(text, r"\bBUYER\s*[:\-]?\s*([A-Z0-9 \-().,&]+)"))
    payload.setdefault("supplier_name", _regex_pick(text, r"\b(?:SUPPLIER|VENDOR)\s*[:\-]?\s*([A-Z0-9 \-().,&]+)"))
    payload.setdefault("product_code", _regex_pick(text, r"\b(RBDPO|RBDPKO|RBDSO|RBDSFNO|CPKO)\b"))
    payload.setdefault("quantity", _regex_number(text, r"\bQTY(?:UANTITY)?\s*[:\-]?\s*([0-9,]+(?:\.[0-9]+)?)"))
    payload.setdefault("unit_price", _regex_number(text, r"\b(?:PRICE|RATE)\s*[:\-]?\s*(?:NGN|₦)?\s*([0-9,]+(?:\.[0-9]+)?)"))
    payload.setdefault("lpo_date", _regex_date(text))
    payload.setdefault("issue_date", payload.get("lpo_date"))
    return {key: value for key, value in payload.items() if value not in (None, "")}


def _regex_pick(text: str, pattern: str) -> str | None:
    match = re.search(pattern, text, flags=re.IGNORECASE)
    if not match:
        return None
    value = str(match.group(1) or "").strip()
    return value or None


def _regex_number(text: str, pattern: str) -> float | None:
    picked = _regex_pick(text, pattern)
    if picked is None:
        return None
    return _normalize_float(picked)


def _regex_date(text: str) -> str | None:
    match = _DATE_RE.search(text)
    if not match:
        return None
    return _normalize_date(match.group(1))


def _flatten_payload(payload: Any, prefix: str = "") -> dict[str, Any]:
    flat: dict[str, Any] = {}
    if isinstance(payload, dict):
        for key, value in payload.items():
            next_prefix = f"{prefix}.{key}" if prefix else str(key)
            flat.update(_flatten_payload(value, next_prefix))
    elif isinstance(payload, list):
        for idx, value in enumerate(payload):
            flat.update(_flatten_payload(value, f"{prefix}[{idx}]"))
        if payload and isinstance(payload[0], dict):
            flat.update(_flatten_payload(payload[0], f"{prefix}[0]"))
    else:
        flat[_norm(prefix)] = payload
    return flat


def _coalesce(raw_data: dict[str, Any], hints: dict[str, Any], keys: list[str]) -> Any:
    for key in keys:
        key_norm = _norm(key)
        if key in hints and hints.get(key) not in (None, ""):
            return hints[key]
        if key_norm in raw_data and raw_data.get(key_norm) not in (None, ""):
            return raw_data[key_norm]
        for raw_key, value in raw_data.items():
            if key_norm and key_norm in _norm(raw_key) and value not in (None, ""):
                return value
    return None


def _normalize_date(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    text = text.replace("/", "-")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    if re.fullmatch(r"\d{2}-\d{2}-\d{4}", text):
        day, month, year = text.split("-")
        return f"{year}-{month}-{day}"
    return None


def _normalize_float(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    match = _NUMBER_RE.search(text)
    if not match:
        return None
    normalized = match.group(0).replace(",", "")
    try:
        return float(normalized)
    except ValueError:
        return None


def _normalize_qty_to_kg(value: Any, unit_hint: str) -> int:
    quantity = _normalize_float(value)
    if quantity is None or quantity <= 0:
        return 0
    unit = str(unit_hint or "").strip().upper()
    if unit in {"KG", "KGS", "KILOGRAM", "KILOGRAMS"}:
        return int(round(quantity))
    return mt_to_kg_int(quantity)


def _date_alias(field_name: str) -> str:
    aliases = {
        "lpo_date": "date",
        "issue_date": "invoice_date",
        "lpo_valid_from": "valid_from",
        "lpo_valid_to": "valid_to",
    }
    return aliases.get(field_name, field_name)


def _confidence(value: Any, *, high: float, low: float) -> float:
    if value in (None, "", 0, 0.0):
        return low
    return high


def _norm(value: Any) -> str:
    return "".join(ch.lower() for ch in str(value or "") if ch.isalnum())
