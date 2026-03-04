"""Phase 1C — Event ledger support: validation, hashing, error types.

Three-tier event classification:
  TRANSITION    — changes entity state; atomic with state mutation
  PREP_EVIDENCE — attaches / updates evidence; atomic with evidence mutation
  PREP_NOTE     — adds a note; atomic with event append only

EnvelopeValidator validates payloads against config/core_event_requirements.json.
canonical_hash produces deterministic SHA-256 hashes for dedup.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Custom errors
# ---------------------------------------------------------------------------

class SchemaValidationError(Exception):
    """Raised when an event payload fails schema validation.

    Triggers rollback of the enclosing transaction (state unchanged).
    """

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__(f"Schema validation failed: {errors}")


class IdempotencyConflictError(Exception):
    """Raised when the same idempotency_key maps to a different content_hash.

    Indicates the caller sent a mutated payload for an already-recorded event.
    """

    def __init__(
        self, idempotency_key: str, existing_hash: str, new_hash: str
    ) -> None:
        self.idempotency_key = idempotency_key
        self.existing_hash = existing_hash
        self.new_hash = new_hash
        super().__init__(
            f"Idempotency conflict on key={idempotency_key!r}: "
            f"existing={existing_hash}, new={new_hash}"
        )


# ---------------------------------------------------------------------------
# Canonical hash  (invariant #7 — same envelope → same hash, always)
# ---------------------------------------------------------------------------

def canonical_hash(envelope: dict) -> str:
    """SHA-256 of sorted-keys compact JSON serialisation."""
    canonical = json.dumps(envelope, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Validation result
# ---------------------------------------------------------------------------

@dataclass
class ValidationResult:
    valid: bool
    catalog_match: bool = True  # False when event_type is not in catalog
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    core_requirements_ref: str = ""
    core_requirements_hash: str = ""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_MISSING = object()


def _get_nested(payload: dict, dotted_key: str) -> Any:
    """Retrieve a value from a nested dict using dot-notation path.

    "trade_id"              → payload["trade_id"]
    "payload.actor_org_id" → payload["payload"]["actor_org_id"]
    """
    parts = dotted_key.split(".")
    current: Any = payload
    for part in parts:
        if not isinstance(current, dict) or part not in current:
            return _MISSING
        current = current[part]
    return current


def _is_valid_uuid(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        uuid.UUID(value)
        return True
    except ValueError:
        return False


def _is_numeric(value: Any) -> bool:
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        try:
            float(value)
            return True
        except ValueError:
            return False
    return False


# ---------------------------------------------------------------------------
# EnvelopeValidator
# ---------------------------------------------------------------------------

class EnvelopeValidator:
    """Validates event payloads against config/core_event_requirements.json."""

    def __init__(
        self, requirements_path: str | Path = "config/core_event_requirements.json"
    ) -> None:
        path = Path(requirements_path)
        with path.open() as fh:
            catalog = json.load(fh)
        self._catalog: dict[str, Any] = catalog.get("event_types", {})
        self._ref: str = catalog.get("core_requirements_ref", "")
        self._hash: str = catalog.get("core_event_requirements_hash", "")

    @property
    def core_requirements_ref(self) -> str:
        return self._ref

    @property
    def core_requirements_hash(self) -> str:
        return self._hash

    def validate(self, event_type: str, payload: dict) -> ValidationResult:
        """Validate payload against catalog requirements for event_type.

        Returns ValidationResult:
          valid   — False if any required field is missing or format-invalid
          errors  — list of field-level errors (cause for rejection)
          warnings — list of warnings (unknown fields, unknown event types)
          core_requirements_ref / core_requirements_hash — provenance
        """
        result = ValidationResult(
            valid=True,
            core_requirements_ref=self._ref,
            core_requirements_hash=self._hash,
        )

        if event_type not in self._catalog:
            result.catalog_match = False
            result.warnings.append(
                f"Unknown event_type={event_type!r}; "
                "likely ignored — verify during Phase 3 sandbox replay"
            )
            return result

        spec = self._catalog[event_type]
        required_fields: list[str] = spec.get("required_fields", [])
        field_formats: dict[str, str] = spec.get("field_formats", {})

        # Flag unknown top-level keys as warnings
        known_top = {f.split(".")[0] for f in required_fields} | {
            f.split(".")[0] for f in field_formats
        }
        for key in payload:
            if key not in known_top:
                result.warnings.append(
                    f"Unknown field {key!r}; "
                    "likely ignored — verify during Phase 3 sandbox replay"
                )

        # Check each required field
        for dotted in required_fields:
            val = _get_nested(payload, dotted)
            if val is _MISSING or val is None or val == "":
                result.errors.append(f"Missing required field: {dotted!r}")
                result.valid = False
                continue

            fmt = field_formats.get(dotted, "")
            if not fmt:
                continue

            fmt_lower = fmt.lower()

            # UUID format checks
            if "uuid" in fmt_lower and "optional" not in fmt_lower:
                if not _is_valid_uuid(str(val)):
                    result.errors.append(
                        f"Field {dotted!r} must be a valid UUID, got {val!r}"
                    )
                    result.valid = False

            # Numeric checks
            elif "numeric" in fmt_lower:
                if not _is_numeric(val):
                    result.errors.append(
                        f"Field {dotted!r} must be numeric, got {val!r}"
                    )
                    result.valid = False
                elif "gt_0" in fmt_lower:
                    try:
                        if float(str(val)) <= 0:
                            result.errors.append(
                                f"Field {dotted!r} must be > 0, got {val!r}"
                            )
                            result.valid = False
                    except (ValueError, TypeError):
                        pass

        return result
