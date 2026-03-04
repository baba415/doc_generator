#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# TODO(Phase-1C): Migrate this script to the evented ingestion path.
# Currently writes directly to SQLite tables without event_log entries.
# After Phase 1C merges, all imports must go through apply_transition()
# or apply_prep_evidence() to ensure schema validation, content hashing,
# idempotency, and replay readiness.
# See: ananta-ops/ANANTA_BUILD_ROADMAP_V3_5.md → Phase 1C → Atomic Coupling

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.csv_import import SUPPORTED_IMPORT_TYPES, format_import_summary, run_csv_import


def _print_pre_event_warning() -> None:
    warning = """
╔══════════════════════════════════════════════════════════════════════╗
║  ⚠️  WARNING: PRE-EVENT-LEDGER MODE                                ║
║                                                                      ║
║  This script writes directly to SQLite WITHOUT creating events       ║
║  in the event_log. Imported data will NOT have:                      ║
║    - Content hashes                                                  ║
║    - Schema validation                                               ║
║    - Idempotency protection                                          ║
║    - Replay readiness                                                ║
║                                                                      ║
║  After Phase 1C merges, this script MUST be migrated to use the      ║
║  evented ingestion path (apply_transition / apply_prep_evidence).    ║
║                                                                      ║
║  Do NOT use this as the production ingestion path.                   ║
╚══════════════════════════════════════════════════════════════════════╝
"""
    print(warning, file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Import trade/counterparty/delivery/payment data from CSV into drep.sqlite"
    )
    parser.add_argument("--type", required=True, choices=sorted(SUPPORTED_IMPORT_TYPES))
    parser.add_argument("--file", required=True, help="Path to CSV file")
    parser.add_argument("--dry-run", action="store_true", help="Validate rows without writing to DB")
    parser.add_argument(
        "--root-dir",
        default=str(ROOT_DIR),
        help="Runtime root directory containing config/ and .state/",
    )
    parser.add_argument(
        "--db-path",
        default="",
        help="Optional explicit SQLite DB path (defaults to <root-dir>/.state/drep.sqlite)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    _print_pre_event_warning()
    parser = build_parser()
    args = parser.parse_args(argv)
    file_path = Path(args.file).expanduser().resolve()
    root_dir = Path(args.root_dir).expanduser().resolve()
    db_path = Path(args.db_path).expanduser().resolve() if str(args.db_path).strip() else None

    try:
        summary = run_csv_import(
            import_type=args.type,
            file_path=file_path,
            root_dir=root_dir,
            dry_run=bool(args.dry_run),
            db_path=db_path,
        )
    except Exception as exc:
        print(f"CSV import failed: {exc}", file=sys.stderr)
        return 1

    print(format_import_summary(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
