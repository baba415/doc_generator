"""Phase 1.5 — Pilot state exporter for core sandbox replay.

Produces four files in the output directory:
  id_map.json               — pilot entity → core_uuid mapping
  event_manifest.json       — full event ledger with core_uuid joined
  replay_readiness_report.json — proof-lite style readiness report
  manifest_metadata.json    — export summary with SHA-256 hashes
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Unified replay-readiness definition (shared with proof_lite.py)
# ---------------------------------------------------------------------------

# Phase 1C merge date — events before this epoch may have weaker validation
EPOCH_TIMESTAMP = "2026-03-04T00:00:00Z"


def is_replay_ready(event_row: dict) -> bool:
    """Single canonical definition of replay-readiness.

    Used by BOTH build_replay_readiness_report (here) and proof_lite.py.
    An event is replay-ready when:
      - schema_ok=1 (passed catalog validation)
      - validated_against_hash is not None (provenance recorded)
    """
    return (
        event_row.get("schema_ok") == 1
        and event_row.get("validated_against_hash") is not None
    )


# ---------------------------------------------------------------------------
# Entity table registry
# ---------------------------------------------------------------------------

# Maps export group name → (table, pk_column, state_column | None)
_ENTITY_GROUPS: dict[str, tuple[str, str, str | None]] = {
    "trades":         ("contracts",         "contract_id",       "lpo_state"),
    "deliveries":     ("deliveries",        "delivery_id",       "status"),
    "settlements":    ("payments",          "payment_id",        None),
    "exceptions":     ("exception_cases",   "exception_case_id", "status"),
    "evidence":       ("evidence_originals","evidence_id",       "link_status"),
    "counterparties": ("parties",           "party_id",          None),
}

# Maps event_log.entity_type → (table, pk_column) for core_uuid lookup
_EVENT_ENTITY_LOOKUP: dict[str, tuple[str, str]] = {
    "contract":    ("contracts",          "contract_id"),
    "trade":       ("contracts",          "contract_id"),
    "delivery":    ("deliveries",         "delivery_id"),
    "payment":     ("payments",           "payment_id"),
    "settlement":  ("payments",           "payment_id"),
    "evidence":    ("evidence_originals", "evidence_id"),
    "exception":   ("exception_cases",    "exception_case_id"),
    "counterparty":("parties",            "party_id"),
    "party":       ("parties",            "party_id"),
}

EXPORT_VERSION = "1.0.0"
PILOT_NAME = "ananta-delivery-pilot"

# Fix 2: proper singularization (rstrip("s") corrupts "counterparties" → "counterpartie")
_SINGULAR_MAP: dict[str, str] = {
    "trades":         "trade",
    "deliveries":     "delivery",
    "settlements":    "settlement",
    "exceptions":     "exception",
    "evidence":       "evidence",
    "counterparties": "counterparty",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _load_catalog(config_dir: Path) -> dict:
    req_path = config_dir / "core_event_requirements.json"
    if not req_path.exists():
        return {}
    with req_path.open() as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Core export functions
# ---------------------------------------------------------------------------

def build_id_map(
    db_path: Path,
    config_dir: Path,
    *,
    warnings: list[str],
) -> dict[str, Any]:
    """Build id_map.json content."""
    catalog = _load_catalog(config_dir)
    generated_at = _utc_now()

    entity_maps: dict[str, list[dict]] = {}
    total_entities = 0
    entities_with_uuid = 0

    if not db_path.exists():
        # Empty DB case — return zero-count map
        for group in _ENTITY_GROUPS:
            entity_maps[group] = []
        return {
            "export_version": EXPORT_VERSION,
            "generated_at": generated_at,
            "core_requirements_ref": catalog.get("core_requirements_ref", ""),
            "core_requirements_hash": catalog.get("core_event_requirements_hash", ""),
            "entity_maps": entity_maps,
            "summary": {
                "total_entities": 0,
                "entities_with_uuid": 0,
                "entities_without_uuid": 0,
                "uuid_coverage_pct": 100.0,
            },
        }

    conn = _connect(db_path)
    try:
        # Per-entity event count + last event time
        try:
            event_stats: dict[str, dict] = {}
            rows = conn.execute(
                "SELECT entity_id, COUNT(*) as cnt, MAX(created_at) as last_at "
                "FROM event_log GROUP BY entity_id"
            ).fetchall()
            for r in rows:
                event_stats[r["entity_id"]] = {
                    "event_count": r["cnt"],
                    "last_event_at": r["last_at"],
                }
        except sqlite3.OperationalError:
            event_stats = {}

        for group, (table, pk_col, state_col) in _ENTITY_GROUPS.items():
            try:
                cols = f"{pk_col}, core_uuid"
                if state_col:
                    cols += f", {state_col}"
                rows = conn.execute(f"SELECT {cols} FROM {table}").fetchall()
            except sqlite3.OperationalError:
                entity_maps[group] = []
                continue

            group_entries = []
            for row in rows:
                pilot_id = row[pk_col]
                core_uuid = row["core_uuid"]
                state = row[state_col] if state_col and state_col in row.keys() else None
                stats = event_stats.get(pilot_id, {})

                total_entities += 1
                if core_uuid:
                    entities_with_uuid += 1
                else:
                    warnings.append(
                        f"Entity {group}/{pilot_id} has no core_uuid — "
                        "Phase 1A backfill may be incomplete"
                    )

                group_entries.append({
                    "pilot_id": pilot_id,
                    "core_uuid": core_uuid,
                    "core_id": None,
                    "entity_type": _SINGULAR_MAP.get(group, group),
                    "current_state": state,
                    "event_count": stats.get("event_count", 0),
                    "last_event_at": stats.get("last_event_at"),
                })
            entity_maps[group] = group_entries
    finally:
        conn.close()

    entities_without_uuid = total_entities - entities_with_uuid
    coverage = (
        round(entities_with_uuid / total_entities * 100.0, 1)
        if total_entities > 0 else 100.0
    )

    return {
        "export_version": EXPORT_VERSION,
        "generated_at": generated_at,
        "core_requirements_ref": catalog.get("core_requirements_ref", ""),
        "core_requirements_hash": catalog.get("core_event_requirements_hash", ""),
        "entity_maps": entity_maps,
        "summary": {
            "total_entities": total_entities,
            "entities_with_uuid": entities_with_uuid,
            "entities_without_uuid": entities_without_uuid,
            "uuid_coverage_pct": coverage,
        },
    }


def build_event_manifest(
    db_path: Path,
    config_dir: Path,
) -> dict[str, Any]:
    """Build event_manifest.json content."""
    catalog = _load_catalog(config_dir)
    event_types_spec = catalog.get("event_types", {})
    ref = catalog.get("core_requirements_ref", "")
    cat_hash = catalog.get("core_event_requirements_hash", "")
    generated_at = _utc_now()

    if not db_path.exists():
        return {
            "export_version": EXPORT_VERSION,
            "generated_at": generated_at,
            "core_requirements_ref": ref,
            "core_requirements_hash": cat_hash,
            "total_events": 0,
            "events_by_entity": {},
            "replay_summary": {
                "events_replay_ready": 0,
                "events_with_warnings": 0,
                "events_with_errors": 0,
                "events_not_catalog_matched": 0,
                "replay_readiness_pct": 100.0,
                "known_replay_obligations": [],
            },
        }

    conn = _connect(db_path)
    try:
        # Build core_uuid lookup cache per (table, pk) to avoid N+1 queries
        uuid_cache: dict[tuple[str, str], str | None] = {}
        for table, pk_col in set(_EVENT_ENTITY_LOOKUP.values()):
            try:
                rows = conn.execute(
                    f"SELECT {pk_col}, core_uuid FROM {table}"
                ).fetchall()
                for r in rows:
                    uuid_cache[(table, r[pk_col])] = r["core_uuid"]
            except sqlite3.OperationalError:
                pass

        # Fetch all events ordered by entity then created_at
        try:
            raw = conn.execute(
                "SELECT event_id, event_type, entity_type, entity_id, "
                "rails_trade_id, payload_json, content_hash, idempotency_key, "
                "schema_ok, validated_against_ref, validated_against_hash, "
                "replay_obligations, created_at "
                "FROM event_log "
                "ORDER BY entity_type, entity_id, created_at ASC"
            ).fetchall()
        except sqlite3.OperationalError:
            raw = conn.execute(
                "SELECT event_id, event_type, entity_type, entity_id, "
                "payload_json, created_at "
                "FROM event_log ORDER BY entity_type, entity_id, created_at ASC"
            ).fetchall()
    finally:
        conn.close()

    events_by_entity: dict[str, list[dict]] = defaultdict(list)
    total_events = 0
    events_replay_ready = 0
    events_with_warnings = 0
    events_with_errors = 0
    events_not_catalog_matched = 0
    known_obligations: set[str] = set()

    for spec in event_types_spec.values():
        for ob in spec.get("replay_obligations", []):
            if ob:
                known_obligations.add(ob)

    for row in raw:
        row_dict = dict(row)
        total_events += 1

        entity_type = row_dict.get("entity_type", "")
        entity_id = row_dict.get("entity_id", "")
        event_type = row_dict.get("event_type", "")
        schema_ok = row_dict.get("schema_ok", 1)

        # Look up core_uuid
        lookup = _EVENT_ENTITY_LOOKUP.get(entity_type)
        core_uuid: str | None = None
        if lookup:
            table, pk_col = lookup
            core_uuid = uuid_cache.get((table, entity_id))

        # Parse payload
        try:
            payload = json.loads(row_dict.get("payload_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            payload = {}

        # Parse replay_obligations
        try:
            obligations = json.loads(row_dict.get("replay_obligations") or "[]")
        except (json.JSONDecodeError, TypeError):
            obligations = []

        catalog_match = bool(schema_ok)
        in_catalog = event_type in event_types_spec

        if not in_catalog:
            events_not_catalog_matched += 1
            events_with_warnings += 1
        elif not catalog_match:
            events_with_errors += 1
        else:
            events_replay_ready += 1

        event_entry = {
            "event_id": row_dict.get("event_id"),
            "event_type": event_type,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "core_uuid": core_uuid,
            "rails_trade_id": row_dict.get("rails_trade_id"),
            "payload": payload,
            "content_hash": row_dict.get("content_hash"),
            "idempotency_key": row_dict.get("idempotency_key"),
            "schema_ok": schema_ok,
            "catalog_match": catalog_match,
            "validated_against_ref": row_dict.get("validated_against_ref"),
            "validated_against_hash": row_dict.get("validated_against_hash"),
            "replay_obligations": obligations,
            "created_at": row_dict.get("created_at"),
        }

        key = f"{entity_type}:{entity_id}"
        events_by_entity[key].append(event_entry)

    readiness_pct = (
        round(events_replay_ready / total_events * 100.0, 1)
        if total_events > 0 else 100.0
    )

    return {
        "export_version": EXPORT_VERSION,
        "generated_at": generated_at,
        "core_requirements_ref": ref,
        "core_requirements_hash": cat_hash,
        "total_events": total_events,
        "events_by_entity": dict(events_by_entity),
        "replay_summary": {
            "events_replay_ready": events_replay_ready,
            "events_with_warnings": events_with_warnings,
            "events_with_errors": events_with_errors,
            "events_not_catalog_matched": events_not_catalog_matched,
            "replay_readiness_pct": readiness_pct,
            "known_replay_obligations": sorted(known_obligations),
        },
    }


def build_replay_readiness_report(
    db_path: Path,
    config_dir: Path,
) -> dict[str, Any]:
    """Build replay_readiness_report.json (proof-lite logic inlined)."""
    catalog = _load_catalog(config_dir)
    ref = catalog.get("core_requirements_ref", "")
    cat_hash = catalog.get("core_event_requirements_hash", "")
    event_types_spec = catalog.get("event_types", {})
    generated_at = _utc_now()

    rows: list[dict] = []
    if db_path.exists():
        conn = _connect(db_path)
        try:
            try:
                raw = conn.execute(
                    "SELECT event_id, event_type, entity_type, entity_id, "
                    "validated_against_ref, validated_against_hash, schema_ok, created_at "
                    "FROM event_log"
                ).fetchall()
                rows = [dict(r) for r in raw]
            except sqlite3.OperationalError:
                raw = conn.execute(
                    "SELECT event_id, event_type, entity_type, entity_id "
                    "FROM event_log"
                ).fetchall()
                rows = [dict(r) for r in raw]
        finally:
            conn.close()

    known_obligations: set[str] = set()
    for spec in event_types_spec.values():
        for ob in spec.get("replay_obligations", []):
            if ob:
                known_obligations.add(ob)

    total = len(rows)
    post_epoch = [r for r in rows if (r.get("created_at") or "") >= EPOCH_TIMESTAMP]
    by_type: dict[str, dict] = {}
    issues: list[dict] = []

    # Unified definition: is_replay_ready() — schema_ok=1 AND validated_against_hash set.
    # Not-in-catalog events are NOT ready (consistent with build_event_manifest).
    ready_all = sum(1 for r in rows if is_replay_ready(r))
    ready_post_epoch = sum(1 for r in post_epoch if is_replay_ready(r))

    for row in rows:
        etype = row.get("event_type", "UNKNOWN")
        entry = by_type.setdefault(etype, {"count": 0, "warnings": 0, "errors": 0})
        entry["count"] += 1

        spec = event_types_spec.get(etype)
        if spec is None:
            issues.append({"class": "UNKNOWN_FIELD", "event_type": etype,
                           "message": f"event_type={etype!r} not in core catalog"})
            entry["errors"] += 1
        elif not is_replay_ready(row):
            issues.append({"class": "NOT_REPLAY_READY", "event_type": etype,
                           "message": f"schema_ok=0 or missing validated_against_hash"})
            entry["errors"] += 1

    total_errors = sum(e["errors"] for e in by_type.values())
    total_warnings = sum(e["warnings"] for e in by_type.values())
    readiness_pct_all = round(ready_all / max(total, 1) * 100.0, 1) if total > 0 else 100.0
    readiness_pct_post = round(
        ready_post_epoch / max(len(post_epoch), 1) * 100.0, 1
    ) if post_epoch else 100.0

    return {
        "generated_at": generated_at,
        "core_requirements_ref": ref,
        "core_requirements_hash": cat_hash,
        "total_events": total,
        "post_epoch_events": len(post_epoch),
        "epoch_timestamp": EPOCH_TIMESTAMP,
        "summary": {
            "replay_ready": total_errors == 0,
            "has_warnings": total_warnings > 0,
            "has_errors": total_errors > 0,
            "replay_readiness_pct": readiness_pct_all,
            "replay_readiness_pct_post_epoch": readiness_pct_post,
            "replay_ready_count": ready_all,
            "replay_ready_post_epoch_count": ready_post_epoch,
        },
        "by_canonical_event_type": by_type,
        "issues": issues,
        "known_replay_obligations": sorted(known_obligations),
    }


def build_manifest_metadata(
    *,
    generated_at: str,
    id_map: dict,
    event_manifest: dict,
    replay_report: dict,
    file_hashes: dict[str, str],
) -> dict[str, Any]:
    """Build manifest_metadata.json content.

    manifest_metadata.json is the trust root of the export package.
    It contains SHA-256 hashes of the other three files but cannot
    contain its own hash (a file cannot hash itself). Core verifies
    the other files against this document; this document's integrity
    is established by the export channel (e.g. signed delivery, S3
    object ETag, or out-of-band checksum).
    """
    total_entities = id_map["summary"]["total_entities"]
    total_events = event_manifest["total_events"]
    replay_readiness_pct = event_manifest["replay_summary"]["replay_readiness_pct"]
    uuid_coverage_pct = id_map["summary"]["uuid_coverage_pct"]

    return {
        "export_version": EXPORT_VERSION,
        "generated_at": generated_at,
        "pilot_name": PILOT_NAME,
        "core_requirements_ref": id_map.get("core_requirements_ref", ""),
        "core_requirements_hash": id_map.get("core_requirements_hash", ""),
        # manifest_metadata.json is the trust root — it cannot self-hash.
        # Its integrity is established by the export delivery channel.
        "note": "manifest_metadata.json is the trust root and does not self-hash",
        "files": [
            {"name": name, "sha256": sha}
            for name, sha in file_hashes.items()
        ],
        "entity_count": total_entities,
        "event_count": total_events,
        "replay_readiness_pct": replay_readiness_pct,
        "uuid_coverage_pct": uuid_coverage_pct,
    }


# ---------------------------------------------------------------------------
# Top-level runner
# ---------------------------------------------------------------------------

def run_export(
    db_path: Path,
    config_dir: Path,
    output_dir: Path,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run the full export. Returns a summary dict.

    If dry_run=True, computes everything but writes nothing.
    """
    warnings: list[str] = []
    generated_at = _utc_now()

    id_map = build_id_map(db_path, config_dir, warnings=warnings)
    event_manifest = build_event_manifest(db_path, config_dir)
    replay_report = build_replay_readiness_report(db_path, config_dir)

    # Serialize to bytes for SHA-256
    id_map_bytes = json.dumps(id_map, indent=2).encode()
    event_manifest_bytes = json.dumps(event_manifest, indent=2).encode()
    replay_report_bytes = json.dumps(replay_report, indent=2).encode()

    file_hashes = {
        "id_map.json":                   _sha256_bytes(id_map_bytes),
        "event_manifest.json":           _sha256_bytes(event_manifest_bytes),
        "replay_readiness_report.json":  _sha256_bytes(replay_report_bytes),
    }

    metadata = build_manifest_metadata(
        generated_at=generated_at,
        id_map=id_map,
        event_manifest=event_manifest,
        replay_report=replay_report,
        file_hashes=file_hashes,
    )

    summary = {
        "output_dir": str(output_dir),
        "dry_run": dry_run,
        "entity_count": id_map["summary"]["total_entities"],
        "uuid_coverage_pct": id_map["summary"]["uuid_coverage_pct"],
        "event_count": event_manifest["total_events"],
        "replay_readiness_pct": event_manifest["replay_summary"]["replay_readiness_pct"],
        "events_not_catalog_matched": event_manifest["replay_summary"]["events_not_catalog_matched"],
        "events_with_warnings": event_manifest["replay_summary"]["events_with_warnings"],
        "known_replay_obligations": event_manifest["replay_summary"]["known_replay_obligations"],
        "warnings": warnings,
        "files": list(file_hashes.keys()) + ["manifest_metadata.json"],
    }

    if not dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "id_map.json").write_bytes(id_map_bytes)
        (output_dir / "event_manifest.json").write_bytes(event_manifest_bytes)
        (output_dir / "replay_readiness_report.json").write_bytes(replay_report_bytes)
        metadata_bytes = json.dumps(metadata, indent=2).encode()
        (output_dir / "manifest_metadata.json").write_bytes(metadata_bytes)
        summary["manifest_metadata"] = metadata

    return summary
