#!/usr/bin/env python3
"""Generate replay readiness report from event_log.

Output: validation/replay_readiness_report.json

Usage:
    python scripts/proof_lite.py
    python scripts/proof_lite.py --db .state/drep.sqlite
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_catalog(config_dir: Path) -> dict:
    req_path = config_dir / "core_event_requirements.json"
    if not req_path.exists():
        return {}
    with req_path.open() as fh:
        return json.load(fh)


def build_report(rows: list[dict], catalog: dict) -> dict:
    ref = catalog.get("core_requirements_ref", "")
    cat_hash = catalog.get("core_event_requirements_hash", "")
    event_types_spec = catalog.get("event_types", {})

    total = len(rows)
    issues: list[dict] = []
    by_type: dict[str, dict] = {}
    known_obligations: set[str] = set()

    for spec in event_types_spec.values():
        for ob in spec.get("replay_obligations", []):
            if ob:
                known_obligations.add(ob)

    for row in rows:
        etype = row.get("event_type", "UNKNOWN")
        entry = by_type.setdefault(etype, {
            "alias": None,
            "count": 0,
            "warnings": 0,
            "errors": 0,
        })
        entry["count"] += 1

        spec = event_types_spec.get(etype)
        if spec is None:
            issues.append({
                "class": "UNKNOWN_FIELD",
                "event_type": etype,
                "message": f"event_type={etype!r} not in core catalog",
            })
            entry["warnings"] += 1
        else:
            alias = spec.get("pilot_alias")
            if entry["alias"] is None and alias:
                entry["alias"] = alias

            # Check validated_against_ref matches current catalog
            row_ref = row.get("validated_against_ref")
            if row_ref and row_ref != ref:
                issues.append({
                    "class": "NAMING_MISMATCH",
                    "event_type": etype,
                    "event_id": row.get("event_id"),
                    "message": (
                        f"Event validated against ref={row_ref!r}, "
                        f"current catalog ref={ref!r}"
                    ),
                })
                entry["warnings"] += 1

            # Check UUID format of event_id
            event_id = row.get("event_id", "")
            try:
                import uuid as _uuid
                _uuid.UUID(event_id)
            except (ValueError, AttributeError):
                issues.append({
                    "class": "UUID_FORMAT",
                    "event_type": etype,
                    "event_id": event_id,
                    "message": f"event_id={event_id!r} is not a valid UUID",
                })
                entry["errors"] += 1

    has_errors = any(e["errors"] > 0 for e in by_type.values())
    has_warnings = any(e["warnings"] > 0 for e in by_type.values())
    total_errors = sum(e["errors"] for e in by_type.values())
    total_count = max(total, 1)
    ready_count = total - total_errors
    readiness_pct = round(ready_count / total_count * 100.0, 1) if total > 0 else 100.0

    return {
        "generated_at": utc_now(),
        "core_requirements_ref": ref,
        "core_requirements_hash": cat_hash,
        "total_events": total,
        "summary": {
            "replay_ready": not has_errors,
            "has_warnings": has_warnings,
            "has_errors": has_errors,
            "replay_readiness_pct": readiness_pct,
        },
        "by_canonical_event_type": by_type,
        "issues": issues,
        "known_replay_obligations": sorted(known_obligations),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate replay readiness report")
    parser.add_argument(
        "--db",
        default=str(REPO_ROOT / ".state" / "drep.sqlite"),
        help="Path to SQLite database (default: .state/drep.sqlite)",
    )
    parser.add_argument(
        "--config",
        default=str(REPO_ROOT / "config"),
        help="Path to config directory (default: config/)",
    )
    parser.add_argument(
        "--output",
        default=str(REPO_ROOT / "validation" / "replay_readiness_report.json"),
        help="Output path for report JSON",
    )
    args = parser.parse_args()

    db_path = Path(args.db)
    config_dir = Path(args.config)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    catalog = load_catalog(config_dir)

    rows: list[dict] = []
    if db_path.exists():
        try:
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            try:
                raw = conn.execute(
                    "SELECT event_id, event_type, entity_type, entity_id, "
                    "validated_against_ref, validated_against_hash, replay_obligations, "
                    "idempotency_key "
                    "FROM event_log WHERE idempotency_key IS NOT NULL"
                ).fetchall()
                rows = [dict(r) for r in raw]
            except sqlite3.OperationalError:
                # event_log may not have the Phase 1C columns yet
                raw = conn.execute(
                    "SELECT event_id, event_type, entity_type, entity_id "
                    "FROM event_log"
                ).fetchall()
                rows = [dict(r) for r in raw]
            finally:
                conn.close()
        except sqlite3.DatabaseError as exc:
            print(f"Warning: could not read database: {exc}", file=sys.stderr)

    report = build_report(rows, catalog)

    with output_path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)

    print(f"Report written to: {output_path}")
    print(f"Total events: {report['total_events']}")
    print(f"Replay ready: {report['summary']['replay_ready']}")
    print(f"Readiness: {report['summary']['replay_readiness_pct']}%")


if __name__ == "__main__":
    main()
