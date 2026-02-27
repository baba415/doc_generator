from __future__ import annotations

from datetime import datetime, timezone


def utc_now_iso_z() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_today_iso() -> str:
    return datetime.now(tz=timezone.utc).date().isoformat()

