from __future__ import annotations

from pathlib import Path

from adapters.sqlite_repo import SQLiteRepo


def export_drep_views(repo: SQLiteRepo, *, as_of_date: str, out_dir: Path) -> list[dict[str, object]]:
    out_dir.mkdir(parents=True, exist_ok=True)
    repo.set_as_of_date(as_of_date)
    exports: list[dict[str, object]] = []
    for view_name in (
        "drep_contracts",
        "drep_procurement",
        "drep_sales",
        "drep_sales_lines",
        "drep_outstanding_payments",
        "drep_delivery_plan_status",
    ):
        exports.append(repo.export_view_csv(view_name, out_dir / f"{view_name}.csv"))
    return exports
