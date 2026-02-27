from __future__ import annotations

import argparse
import json
from pathlib import Path

from .generator import DeliveryPackGenerator
from .utils import read_json, write_json
from .web import run_server


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Ananta Delivery Pilot Generator")
    subparsers = parser.add_subparsers(dest="command", required=True)

    gen = subparsers.add_parser("generate", help="Generate documents from transaction JSON")
    gen.add_argument("--transaction", required=True, help="Path to transaction JSON")
    gen.add_argument("--policy", required=False, help="Funder policy filename under config/funders")

    val = subparsers.add_parser("validate", help="Validate a transaction JSON without generating PDFs")
    val.add_argument("--transaction", required=True, help="Path to transaction JSON")
    val.add_argument("--policy", required=False, help="Funder policy filename under config/funders")

    batch = subparsers.add_parser("generate-samples", help="Generate all sample transactions")
    batch.add_argument("--policy", required=False, default="islamic_bank_policy.json")

    web = subparsers.add_parser("serve", help="Run local web UI")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8765)

    entities = subparsers.add_parser("entities", help="Manage entity registry")
    entities_sub = entities.add_subparsers(dest="entities_command", required=True)
    entities_sub.add_parser("list", help="List entities")

    upsert = entities_sub.add_parser("upsert", help="Add/update an entity in config/entities.local.json")
    upsert.add_argument("--entity-id", required=True)
    upsert.add_argument("--legal-name", required=True)
    upsert.add_argument("--code", default="")
    upsert.add_argument("--rc-number", default="")
    upsert.add_argument("--tin", default="")
    upsert.add_argument("--address", default="")
    upsert.add_argument("--city-state-country", default="")
    upsert.add_argument("--website", default="")
    upsert.add_argument("--phone", action="append", default=[])
    upsert.add_argument("--email", action="append", default=[])
    upsert.add_argument("--alias", action="append", default=[])

    upsert.add_argument("--bank-name", default="")
    upsert.add_argument("--bank-account-name", default="")
    upsert.add_argument("--bank-account-number", default="")
    upsert.add_argument("--bank-currency", default="NGN")

    return parser


def _derive_code(name: str) -> str:
    raw = "".join(ch for ch in (name or "").upper() if ch.isalnum())
    return (raw[:10] or "VENDOR").upper()


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    generator = DeliveryPackGenerator(root)

    if args.command == "generate":
        result = generator.generate_from_file(Path(args.transaction), funder_policy_file=args.policy)
        print(json.dumps({
            "output_dir": result.output_dir,
            "manifest_path": result.manifest_path,
            "documents": [doc.key for doc in result.documents],
        }, indent=2))
        return

    if args.command == "validate":
        result = generator.validate_from_file(Path(args.transaction), funder_policy_file=args.policy)
        print(json.dumps(result, indent=2))
        return

    if args.command == "generate-samples":
        sample_dir = root / "data"
        generated = []
        for sample in sorted(sample_dir.glob("sample_transaction_*.json")):
            policy = args.policy if "murabaha" in sample.name else None
            result = generator.generate_from_file(sample, funder_policy_file=policy)
            generated.append({"sample": sample.name, "output_dir": result.output_dir})
        print(json.dumps({"generated": generated}, indent=2))
        return

    if args.command == "entities":
        local_path = root / "config" / "entities.local.json"
        if not local_path.exists():
            write_json(local_path, {"schema_version": "1.0", "entities": {}})

        if args.entities_command == "list":
            registry = generator.entity_registry
            rows = []
            for entity_id, entity in sorted(registry.entities.items()):
                rows.append(
                    {
                        "entity_id": entity_id,
                        "code": entity.code or "",
                        "legal_name": entity.name,
                        "rc_number": entity.rc_number or "",
                        "tin": entity.tin or "",
                    }
                )
            print(json.dumps({"entities": rows}, indent=2))
            return

        if args.entities_command == "upsert":
            payload = read_json(local_path)
            entities = payload.get("entities", {}) if isinstance(payload, dict) else {}
            if not isinstance(entities, dict):
                entities = {}

            entity_id = str(args.entity_id).strip()
            current = entities.get(entity_id, {})
            if not isinstance(current, dict):
                current = {}

            legal_name = str(args.legal_name).strip()
            code = str(args.code).strip() or current.get("code") or _derive_code(legal_name)
            updated = {
                **current,
                "code": code,
                "legal_name": legal_name,
            }

            for key, value in (
                ("rc_number", args.rc_number),
                ("tin", args.tin),
                ("address", args.address),
                ("city_state_country", args.city_state_country),
                ("website", args.website),
            ):
                val = str(value).strip()
                if val:
                    updated[key] = val

            phones = [str(item).strip() for item in (args.phone or []) if str(item).strip()]
            if phones:
                updated["phones"] = phones

            emails = [str(item).strip() for item in (args.email or []) if str(item).strip()]
            if emails:
                updated["emails"] = emails

            aliases = [str(item).strip() for item in (args.alias or []) if str(item).strip()]
            if aliases:
                updated["aliases"] = sorted({*aliases, *[str(item).strip() for item in current.get("aliases", []) if str(item).strip()]})

            bank_name = str(args.bank_name).strip()
            bank_account_name = str(args.bank_account_name).strip()
            bank_account_number = str(args.bank_account_number).strip()
            if bank_name or bank_account_name or bank_account_number:
                bank = dict(current.get("bank") or {}) if isinstance(current.get("bank"), dict) else {}
                if bank_name:
                    bank["bank_name"] = bank_name
                if bank_account_name:
                    bank["account_name"] = bank_account_name
                if bank_account_number:
                    bank["account_number"] = bank_account_number
                bank_currency = str(args.bank_currency).strip() or "NGN"
                bank["currency"] = bank_currency
                updated["bank"] = bank

            entities[entity_id] = updated
            write_json(local_path, {"schema_version": payload.get("schema_version", "1.0"), "entities": entities})
            print(json.dumps({"ok": True, "entity_id": entity_id, "written_to": str(local_path)}, indent=2))
            return

        raise RuntimeError(f"Unknown entities_command: {args.entities_command}")

    if args.command == "serve":
        run_server(root_dir=root, host=args.host, port=args.port)
        return


if __name__ == "__main__":
    main()
