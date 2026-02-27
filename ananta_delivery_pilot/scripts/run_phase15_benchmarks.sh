#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

AS_OF="${1:-2026-03-31}"
OUT_DIR="$ROOT_DIR/.state/automation/benchmarks/$(date -u +%Y%m%d%H%M%S)"
mkdir -p "$OUT_DIR"

python3 run.py init-db >"$OUT_DIR/init_db.json"

python3 run.py auto-run --input examples/stp/known_complete.json --as-of "$AS_OF" >"$OUT_DIR/known_complete.json"
python3 run.py auto-run --input examples/stp/known_partial.json --as-of "$AS_OF" >"$OUT_DIR/known_partial.json"
python3 run.py auto-run --input examples/stp/unknown_entities.json --as-of "$AS_OF" --dry-run >"$OUT_DIR/unknown_entities.json"

python3 - <<PY
import json
from pathlib import Path

out_dir = Path("$OUT_DIR")
rows = []
for name in ("known_complete", "known_partial", "unknown_entities"):
    payload = json.loads((out_dir / f"{name}.json").read_text())
    metrics = payload.get("metrics", {})
    rows.append(
        {
            "scenario": name,
            "status": payload.get("status"),
            "stp_rate": metrics.get("stp_rate"),
            "auto_population_rate": metrics.get("auto_population_rate"),
            "manual_interventions_count": metrics.get("manual_interventions_count"),
            "exception_count_by_type": metrics.get("exception_count_by_type"),
            "end_to_end_duration_seconds": metrics.get("end_to_end_duration_seconds"),
        }
    )

summary = {"as_of": "$AS_OF", "rows": rows}
(out_dir / "kpi_report.json").write_text(json.dumps(summary, indent=2))

md = ["# Phase 1.5 KPI Report", "", f"- as_of: {summary['as_of']}", "", "| Scenario | Status | STP Rate | Auto Pop Rate | Manual | Duration(s) |", "|---|---:|---:|---:|---:|---:|"]
for row in rows:
    md.append(
        f"| {row['scenario']} | {row['status']} | {row['stp_rate']} | {row['auto_population_rate']} | {row['manual_interventions_count']} | {row['end_to_end_duration_seconds']} |"
    )
md.append("")
md.append("## Exception Breakdown")
for row in rows:
    md.append(f"- {row['scenario']}: {json.dumps(row['exception_count_by_type'], sort_keys=True)}")
(out_dir / "kpi_report.md").write_text("\\n".join(md) + "\\n")
print(json.dumps({"ok": True, "out_dir": str(out_dir), "kpi_report": str(out_dir / 'kpi_report.md')}, indent=2))
PY
