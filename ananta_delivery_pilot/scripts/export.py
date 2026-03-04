#!/usr/bin/env python3
"""Export pilot state for core sandbox replay.

Produces four files in the output directory:
  id_map.json               — pilot entity → core_uuid mapping
  event_manifest.json       — full event ledger with core_uuid joined
  replay_readiness_report.json — proof-lite readiness report
  manifest_metadata.json    — export summary with SHA-256 integrity hashes

Usage:
  python scripts/export.py --output-dir exports/2026-03-04/
  python scripts/export.py --output-dir exports/2026-03-04/ --dry-run
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from domain.exporter import run_export  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export pilot state (id_map + event manifest) for core replay"
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Directory to write export files into",
    )
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
        "--dry-run",
        action="store_true",
        help="Print summary without writing files",
    )
    args = parser.parse_args()

    db_path = Path(args.db)
    config_dir = Path(args.config)
    output_dir = Path(args.output_dir)

    summary = run_export(db_path, config_dir, output_dir, dry_run=args.dry_run)

    print("\nExport Summary:")
    print(f"  Output: {summary['output_dir']}")
    print()
    print(f"  Entities: {summary['entity_count']} ({summary['uuid_coverage_pct']}% UUID coverage)")
    print(f"  Events:   {summary['event_count']} ({summary['replay_readiness_pct']}% replay ready)")
    print()
    print("  Files:")
    for fname in summary["files"]:
        if fname == "id_map.json":
            print(f"    {fname:<35} ({summary['entity_count']} entities)")
        elif fname == "event_manifest.json":
            print(f"    {fname:<35} ({summary['event_count']} events)")
        else:
            print(f"    {fname}")

    if summary["known_replay_obligations"]:
        print()
        print(f"  Replay obligations: {', '.join(summary['known_replay_obligations'])}")

    if summary["warnings"]:
        print()
        print("  Warnings:")
        for w in summary["warnings"]:
            print(f"    {w}")

    if summary["events_not_catalog_matched"]:
        print(f"    {summary['events_not_catalog_matched']} events not catalog-matched (pilot-internal types)")

    if summary["events_with_warnings"] and not summary["events_not_catalog_matched"]:
        print(f"    {summary['events_with_warnings']} events with validation warnings")

    if args.dry_run:
        print()
        print("  [dry-run] No files written.")
    else:
        print()
        print(f"  Written to: {output_dir}/")


if __name__ == "__main__":
    main()
