#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.csv_import import SUPPORTED_IMPORT_TYPES, format_import_summary, run_csv_import


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


def _print_evented_mode_banner() -> None:
    print("EVENTED MODE: All imports go through the event ledger.", file=sys.stderr)
    print("  Contract: contracts/pilot_event_contract.md v0.1", file=sys.stderr)
    print("  Idempotency: csv:{file_sha256}:{row_number}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    file_path = Path(args.file).expanduser().resolve()
    root_dir = Path(args.root_dir).expanduser().resolve()
    db_path = Path(args.db_path).expanduser().resolve() if str(args.db_path).strip() else None
    _print_evented_mode_banner()

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
