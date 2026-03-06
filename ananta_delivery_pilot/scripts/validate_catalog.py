#!/usr/bin/env python3
"""Per AUTHORITY_MAP v5 Rule 6: merchant may not own independently editable catalog."""

import json
import os
import re
import sys
from typing import Optional, Set


def load_merchant() -> Set[str]:
    with open("config/core_event_requirements.json", encoding="utf-8") as f:
        raw = json.load(f)
    if isinstance(raw, dict) and isinstance(raw.get("event_types"), dict):
        return set(raw["event_types"].keys())
    if isinstance(raw, dict):
        return set(raw.keys())
    raise ValueError("Unexpected merchant catalog format")


def load_core() -> Optional[Set[str]]:
    p = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "..",
        "..",
        "core",
        "src",
        "app",
        "api",
        "events",
        "eventCatalog.ts",
    )
    if not os.path.exists(p):
        print(f"WARNING: Core not found at {p}")
        return None

    with open(p, encoding="utf-8") as f:
        text = f.read()

    block = re.search(
        r"const\s+ALLOWED_CANON_EVENTS\s*=\s*new\s+Set<[^>]+>\s*\(\s*\[(.*?)\]\s*\)",
        text,
        flags=re.S,
    )
    if block:
        return set(re.findall(r'"([A-Z][A-Z_]+)"', block.group(1)))

    type_block = re.search(r"export\s+type\s+CanonEventName\s*=\s*(.*?);", text, flags=re.S)
    if type_block:
        return set(re.findall(r'"([A-Z][A-Z_]+)"', type_block.group(1)))

    print("WARNING: Could not parse core canonical event set")
    return None


def main() -> int:
    merchant = load_merchant()
    core = load_core()

    print(f"Merchant: {len(merchant)} events")
    if core is None:
        print("Core unavailable")
        return 0

    print(f"Core: {len(core)} events")

    drift = merchant - core
    if drift:
        print(f"\nDRIFT (merchant-only): {sorted(drift)}")
        return 1

    missing = core - merchant
    if missing:
        print(f"\nCore-only: {sorted(missing)}")

    print("\nNo merchant drift")
    return 0


if __name__ == "__main__":
    sys.exit(main())
