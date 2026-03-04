#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.reconciliation import reconcile_weekly


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Weekly reconciliation: match pilot payment records against bank statement."
    )
    parser.add_argument("--bank-statement", required=True, help="Path to bank statement CSV")
    parser.add_argument("--week", required=True, help="ISO week key, e.g. 2026-W10")
    parser.add_argument("--dry-run", action="store_true", help="Run matching but do not write JSON output")
    parser.add_argument(
        "--root-dir",
        default=str(ROOT_DIR),
        help="Runtime root containing config/ and .state/",
    )
    parser.add_argument(
        "--db-path",
        default="",
        help="Optional explicit SQLite path (defaults to <root-dir>/.state/drep.sqlite)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    root_dir = Path(args.root_dir).expanduser().resolve()
    bank_statement = Path(args.bank_statement).expanduser()
    if not bank_statement.is_absolute():
        bank_statement = (root_dir / bank_statement).resolve()
    db_path = Path(args.db_path).expanduser().resolve() if str(args.db_path).strip() else None

    try:
        result = reconcile_weekly(
            root_dir=root_dir,
            bank_statement_path=bank_statement,
            week=str(args.week).strip(),
            dry_run=bool(args.dry_run),
            db_path=db_path,
        )
    except Exception as exc:
        print(f"Reconciliation failed: {exc}", file=sys.stderr)
        return 1

    print(result.summary_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

