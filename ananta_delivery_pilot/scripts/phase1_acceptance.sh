#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

usage() {
  cat <<'EOF'
Usage:
  ./scripts/phase1_acceptance.sh [--with-pdf] [--as-of YYYY-MM-DD]

Default behavior:
  - Runs full Phase 1 acceptance sequence with --skip-pdf
  - Runs tests and legacy validate smoke
  - Exports DREP CSVs

Options:
  --with-pdf         Render real PDFs (requires playwright-cli)
  --as-of <date>     As-of date for export-drep (default: 2026-03-31)
  -h, --help         Show this help
EOF
}

WITH_PDF=0
AS_OF_DATE="2026-03-31"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --with-pdf)
      WITH_PDF=1
      shift
      ;;
    --as-of)
      [[ $# -ge 2 ]] || { echo "Missing value for --as-of" >&2; exit 1; }
      AS_OF_DATE="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if [[ $WITH_PDF -eq 1 ]]; then
  command -v playwright-cli >/dev/null 2>&1 || {
    echo "playwright-cli is required for --with-pdf" >&2
    exit 1
  }
fi

RUN_ID="$(date -u +%Y%m%d%H%M%S)"
RUN_DIR="$ROOT_DIR/.state/acceptance-run/$RUN_ID"
mkdir -p "$RUN_DIR"

if [[ $WITH_PDF -eq 1 ]]; then
  PACK_ARGS=()
  PAY_ARGS=()
else
  PACK_ARGS=(--skip-pdf)
  PAY_ARGS=(--skip-pdf)
fi

echo "==> Acceptance run: $RUN_ID"
echo "    Run dir: $RUN_DIR"
echo "    With PDF: $WITH_PDF"
echo "    Export as-of: $AS_OF_DATE"

echo "==> 1) Tests"
./scripts/test_default.sh | tee "$RUN_DIR/tests.log"

echo "==> 2) Legacy smoke (validate)"
python3 run.py validate --transaction data/sample_transaction_contract_processing.json > "$RUN_DIR/legacy_validate.json"

if [[ $WITH_PDF -eq 1 ]]; then
  echo "==> 3) Legacy smoke (generate)"
  python3 run.py generate --transaction data/sample_transaction_contract_processing.json > "$RUN_DIR/legacy_generate.json"
else
  echo "==> 3) Legacy generate skipped (use --with-pdf to enable)"
fi

echo "==> 4) Phase 1 setup"
python3 run.py init-db > "$RUN_DIR/init_db.json"
cp data/phase1_contract.json "$RUN_DIR/contract.json"
cp data/phase1_delivery.json "$RUN_DIR/delivery.json"

python3 - <<PY
import json
from pathlib import Path
p = Path("$RUN_DIR/contract.json")
data = json.loads(p.read_text())
data["contract_ref"] = "ACCEPT-$RUN_ID"
data["lpo_no"] = "ACCEPT-$RUN_ID"
data["vendor_of_record_id"] = "ananta_flows"
data["source_id"] = "ananta_flows"
p.write_text(json.dumps(data, indent=2))
PY

python3 run.py create-contract --input "$RUN_DIR/contract.json" --allow-placeholder-tin > "$RUN_DIR/create_contract.json"
CONTRACT_ID="$(python3 - <<PY
import json
from pathlib import Path
print(json.loads(Path("$RUN_DIR/create_contract.json").read_text())["contract_id"])
PY
)"

python3 - <<PY
import json
from pathlib import Path
p = Path("$RUN_DIR/delivery.json")
data = json.loads(p.read_text())
data["contract_id"] = "$CONTRACT_ID"
data["delivery_ref"] = "ACCEPT-DLV-$RUN_ID"
data["run_id"] = "RUN-ACCEPT-$RUN_ID"
data["batch_id"] = "AFL-RBDSO-ACCEPT-$RUN_ID"
p.write_text(json.dumps(data, indent=2))
PY

python3 run.py add-delivery --input "$RUN_DIR/delivery.json" > "$RUN_DIR/add_delivery.json"
DELIVERY_ID="$(python3 - <<PY
import json
from pathlib import Path
print(json.loads(Path("$RUN_DIR/add_delivery.json").read_text())["delivery_id"])
PY
)"

python3 run.py mark-dispatched --delivery-id "$DELIVERY_ID" > "$RUN_DIR/mark_dispatched.json"
python3 run.py mark-delivered --delivery-id "$DELIVERY_ID" > "$RUN_DIR/mark_delivered.json"

echo "==> 5) Record COA"
python3 - <<PY
import json
from pathlib import Path
root = Path("$ROOT_DIR")
profile = json.loads((root / "config" / "coa_profiles.json").read_text())["RBDSO"]["quality_parameters"]
payload = {
    "delivery_id": "$DELIVERY_ID",
    "results": [{"parameter": row["parameter"], "result": "PASS"} for row in profile]
}
(Path("$RUN_DIR") / "coa.json").write_text(json.dumps(payload, indent=2))
PY
python3 run.py record-coa --input "$RUN_DIR/coa.json" > "$RUN_DIR/record_coa.json"

echo "==> 6) Generate pack"
python3 run.py generate-pack --delivery-id "$DELIVERY_ID" --allow-placeholder-tin "${PACK_ARGS[@]}" > "$RUN_DIR/generate_pack.json"
SALES_TRANSACTION_ID="$(python3 - <<PY
import json
from pathlib import Path
print(json.loads(Path("$RUN_DIR/generate_pack.json").read_text())["sales_transaction_id"])
PY
)"
INVOICE_NO="$(python3 - <<PY
import json
from pathlib import Path
print(json.loads(Path("$RUN_DIR/generate_pack.json").read_text())["invoice_no"])
PY
)"

echo "==> 7) Mark paid"
python3 - <<PY
import json
from pathlib import Path
payload = {
  "payment_date": "2026-03-09",
  "payment_method": "Bank Transfer",
  "external_reference": "ACCEPT-PAY-$RUN_ID",
  "idempotency_key": "ACCEPT-PAY-$RUN_ID",
  "amount_received": 68100000.0,
  "allocations": [
    {
      "sales_transaction_id": "$SALES_TRANSACTION_ID",
      "allocated_amount": 68100000.0,
      "notes": "Acceptance full settlement"
    }
  ]
}
(Path("$RUN_DIR") / "payment.json").write_text(json.dumps(payload, indent=2))
PY
python3 run.py mark-paid --input "$RUN_DIR/payment.json" --allow-placeholder-tin "${PAY_ARGS[@]}" > "$RUN_DIR/mark_paid.json"

echo "==> 8) Export DREP"
python3 run.py export-drep --as-of "$AS_OF_DATE" --out-dir "$RUN_DIR/exports" > "$RUN_DIR/export_drep.json"

echo "==> 9) Write summary"
python3 - <<PY
import json
from pathlib import Path
run_dir = Path("$RUN_DIR")
summary = {
    "run_id": "$RUN_ID",
    "with_pdf": bool($WITH_PDF),
    "as_of_date": "$AS_OF_DATE",
    "contract_id": "$CONTRACT_ID",
    "delivery_id": "$DELIVERY_ID",
    "sales_transaction_id": "$SALES_TRANSACTION_ID",
    "invoice_no": "$INVOICE_NO",
    "generate_pack": json.loads((run_dir / "generate_pack.json").read_text()),
    "mark_paid": json.loads((run_dir / "mark_paid.json").read_text()),
    "export_drep": json.loads((run_dir / "export_drep.json").read_text())
}
(run_dir / "summary.json").write_text(json.dumps(summary, indent=2))

md = [
    "# Phase 1 Acceptance Run",
    "",
    f"- run_id: {summary['run_id']}",
    f"- with_pdf: {summary['with_pdf']}",
    f"- as_of_date: {summary['as_of_date']}",
    f"- contract_id: {summary['contract_id']}",
    f"- delivery_id: {summary['delivery_id']}",
    f"- sales_transaction_id: {summary['sales_transaction_id']}",
    f"- invoice_no: {summary['invoice_no']}",
    f"- output_dir: {summary['generate_pack'].get('output_dir')}",
    f"- receipt_path: {summary['mark_paid'].get('receipt_path')}",
    "",
    "## Export files",
]
for item in summary["export_drep"]["exports"]:
    md.append(f"- {item['path']} (rows={item['rows']})")
(run_dir / "summary.md").write_text("\\n".join(md) + "\\n")
PY

echo "==> Acceptance complete"
echo "    Summary JSON: $RUN_DIR/summary.json"
echo "    Summary MD:   $RUN_DIR/summary.md"
