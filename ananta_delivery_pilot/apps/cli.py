from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from adapters.sqlite_repo import SQLiteRepo
from core.config import RuntimeConfig
from domain.automation import AutomationOrchestrator
from domain.services import Phase1Service
from .web_v2 import run_server_v2


def phase1_commands() -> set[str]:
    return {
        "init-db",
        "create-contract",
        "add-delivery",
        "mark-dispatched",
        "mark-delivered",
        "record-coa",
        "plan-deliveries",
        "materialize-delivery",
        "generate-pack",
        "mark-paid",
        "cancel-contract",
        "close-contract",
        "refresh-contract-state",
        "export-drep",
        "serve-v2",
        "auto-run",
        "exceptions",
        "auto-resume",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Phase 1 DREP CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init-db", help="Initialize SQLite schema and seed parties")

    create_contract = subparsers.add_parser("create-contract", help="Create contract from JSON")
    create_contract.add_argument("--input", required=True)
    create_contract.add_argument("--allow-placeholder-tin", action="store_true")

    add_delivery = subparsers.add_parser("add-delivery", help="Create delivery from JSON")
    add_delivery.add_argument("--input", required=True)

    mark_dispatched = subparsers.add_parser("mark-dispatched", help="Mark delivery dispatched")
    mark_dispatched.add_argument("--delivery-id", required=True)

    mark_delivered = subparsers.add_parser("mark-delivered", help="Mark delivery delivered")
    mark_delivered.add_argument("--delivery-id", required=True)

    record_coa = subparsers.add_parser("record-coa", help="Record COA rows for batch/run")
    record_coa.add_argument("--input", required=True)

    plan_deliveries = subparsers.add_parser("plan-deliveries", help="Auto-plan deliveries by lot policy")
    plan_deliveries.add_argument("--contract-id", required=True)
    plan_deliveries.add_argument("--start-date", default="")
    plan_deliveries.add_argument("--cadence", default="daily")
    plan_deliveries.add_argument("--max-lots-per-day", type=int, default=1)

    materialize_delivery = subparsers.add_parser("materialize-delivery", help="Create delivery from planned delivery row")
    materialize_delivery.add_argument("--planned-delivery-id", required=True)
    materialize_delivery.add_argument("--run-id", default="")
    materialize_delivery.add_argument("--batch-id", default="")
    materialize_delivery.add_argument("--qty-mt", type=float, default=None)

    generate_pack = subparsers.add_parser("generate-pack", help="Generate 4-doc pack for delivery")
    generate_pack.add_argument("--delivery-id", required=True)
    generate_pack.add_argument("--allow-placeholder-tin", action="store_true")
    generate_pack.add_argument("--skip-pdf", action="store_true")
    generate_pack.add_argument("--original-doc", action="append", default=[])

    mark_paid = subparsers.add_parser("mark-paid", help="Record payment allocation and generate receipt")
    mark_paid.add_argument("--input", required=True)
    mark_paid.add_argument("--allow-placeholder-tin", action="store_true")
    mark_paid.add_argument("--skip-pdf", action="store_true")

    cancel_contract = subparsers.add_parser("cancel-contract", help="Cancel contract/LPO")
    cancel_contract.add_argument("--contract-id", required=True)
    cancel_contract.add_argument("--reason", required=True)

    close_contract = subparsers.add_parser("close-contract", help="Close contract/LPO")
    close_contract.add_argument("--contract-id", required=True)
    close_contract.add_argument("--reason", required=True)

    refresh_contract_state = subparsers.add_parser(
        "refresh-contract-state",
        help="Refresh deterministic LPO states for an as-of date",
    )
    refresh_contract_state.add_argument("--as-of", required=True)

    export_drep = subparsers.add_parser("export-drep", help="Export DREP CSV views")
    export_drep.add_argument("--as-of", required=True)
    export_drep.add_argument("--out-dir", default="")

    serve_v2 = subparsers.add_parser("serve-v2", help="Run Phase2 web UI backed by Phase1 ledger")
    serve_v2.add_argument("--host", default="127.0.0.1")
    serve_v2.add_argument("--port", type=int, default=8865)

    auto_run = subparsers.add_parser("auto-run", help="Run automation-first STP orchestration")
    auto_run.add_argument("--input", required=True)
    auto_run.add_argument("--as-of", required=True)
    auto_run.add_argument("--dry-run", action="store_true")

    exceptions = subparsers.add_parser("exceptions", help="Manage automation exception queue")
    exceptions_sub = exceptions.add_subparsers(dest="exceptions_command", required=True)
    exceptions_list = exceptions_sub.add_parser("list", help="List open exceptions")
    exceptions_list.add_argument("--run-id", default="")

    exceptions_resolve = exceptions_sub.add_parser("resolve", help="Resolve one exception")
    exceptions_resolve.add_argument("--exception-id", required=True)
    exceptions_resolve.add_argument("--value", required=True)
    exceptions_resolve.add_argument("--note", required=True)

    auto_resume = subparsers.add_parser("auto-resume", help="Resume automation run after resolving exceptions")
    auto_resume.add_argument("--run-id", required=True)

    return parser


def _read_json(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _service(root_dir: Path) -> Phase1Service:
    config = RuntimeConfig.load(root_dir)
    repo = SQLiteRepo(config.state_dir / "drep.sqlite")
    return Phase1Service(config, repo)


def _automation(root_dir: Path) -> AutomationOrchestrator:
    config = RuntimeConfig.load(root_dir)
    repo = SQLiteRepo(config.state_dir / "drep.sqlite")
    service = Phase1Service(config, repo)
    return AutomationOrchestrator(config, repo, service)


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    root_dir = Path(__file__).resolve().parents[1]
    service = _service(root_dir)

    if args.command == "init-db":
        print(json.dumps(service.init_db(), indent=2))
        return

    if args.command == "create-contract":
        payload = _read_json(args.input)
        result = service.create_contract(payload, allow_placeholder_tin=bool(args.allow_placeholder_tin))
        print(json.dumps(result, indent=2))
        return

    if args.command == "add-delivery":
        payload = _read_json(args.input)
        result = service.add_delivery(payload)
        print(json.dumps(result, indent=2))
        return

    if args.command == "mark-dispatched":
        result = service.mark_dispatched(args.delivery_id)
        print(json.dumps(result, indent=2))
        return

    if args.command == "mark-delivered":
        result = service.mark_delivered(args.delivery_id)
        print(json.dumps(result, indent=2))
        return

    if args.command == "record-coa":
        payload = _read_json(args.input)
        result = service.record_coa(payload)
        print(json.dumps(result, indent=2))
        return

    if args.command == "plan-deliveries":
        result = service.plan_deliveries(
            contract_id=str(args.contract_id),
            start_date=str(args.start_date or "").strip() or None,
            cadence=str(args.cadence or "daily"),
            max_lots_per_day=int(args.max_lots_per_day),
        )
        print(json.dumps(result, indent=2))
        return

    if args.command == "materialize-delivery":
        result = service.materialize_delivery(
            planned_delivery_id=str(args.planned_delivery_id),
            run_id=str(args.run_id or "").strip() or None,
            batch_id=str(args.batch_id or "").strip() or None,
            qty_mt=args.qty_mt,
        )
        print(json.dumps(result, indent=2))
        return

    if args.command == "generate-pack":
        result = service.generate_pack(
            delivery_id=args.delivery_id,
            allow_placeholder_tin=bool(args.allow_placeholder_tin),
            skip_pdf=bool(args.skip_pdf),
            original_docs=list(args.original_doc or []),
        )
        print(json.dumps(result, indent=2))
        return

    if args.command == "mark-paid":
        payload = _read_json(args.input)
        result = service.mark_paid(
            payload,
            allow_placeholder_tin=bool(args.allow_placeholder_tin),
            skip_pdf=bool(args.skip_pdf),
        )
        print(json.dumps(result, indent=2))
        return

    if args.command == "cancel-contract":
        result = service.cancel_contract(contract_id=str(args.contract_id), reason=str(args.reason))
        print(json.dumps(result, indent=2))
        return

    if args.command == "close-contract":
        result = service.close_contract(contract_id=str(args.contract_id), reason=str(args.reason))
        print(json.dumps(result, indent=2))
        return

    if args.command == "refresh-contract-state":
        result = service.refresh_contract_state(as_of_date=str(args.as_of))
        print(json.dumps(result, indent=2))
        return

    if args.command == "export-drep":
        out_dir = Path(args.out_dir).expanduser() if args.out_dir else (root_dir / ".state" / "exports" / args.as_of)
        result = service.export_drep(as_of_date=args.as_of, out_dir=out_dir)
        print(json.dumps(result, indent=2))
        return

    if args.command == "serve-v2":
        run_server_v2(root_dir=root_dir, host=args.host, port=args.port)
        return

    if args.command == "auto-run":
        orchestrator = _automation(root_dir)
        payload = _read_json(args.input)
        result = orchestrator.auto_run(
            payload,
            as_of_date=str(args.as_of),
            dry_run=bool(args.dry_run),
        )
        print(json.dumps(result, indent=2))
        return

    if args.command == "exceptions":
        orchestrator = _automation(root_dir)
        if args.exceptions_command == "list":
            result = orchestrator.list_exceptions(run_id=(args.run_id or None))
            print(json.dumps({"exceptions": result}, indent=2))
            return
        if args.exceptions_command == "resolve":
            result = orchestrator.resolve_exception(
                exception_id=str(args.exception_id),
                value=str(args.value),
                note=str(args.note),
            )
            print(json.dumps(result, indent=2))
            return
        raise RuntimeError(f"Unsupported exceptions_command: {args.exceptions_command}")

    if args.command == "auto-resume":
        orchestrator = _automation(root_dir)
        result = orchestrator.auto_resume(str(args.run_id))
        print(json.dumps(result, indent=2))
        return

    raise RuntimeError(f"Unsupported command: {args.command}")
