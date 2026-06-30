from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any


DEFAULT_EXPERIMENT_GROUPS = Path("docs/experiments/operating_model_target_groups.json")


def load_experiment_group_ids(group_name: str, group_path: Path = DEFAULT_EXPERIMENT_GROUPS) -> set[str]:
    if not group_name:
        return set()
    if not group_path.exists():
        raise FileNotFoundError(f"experiment group file not found: {group_path}")
    payload = json.loads(group_path.read_text(encoding="utf-8"))
    groups = payload.get("groups", {})
    if group_name not in groups:
        available = ", ".join(sorted(groups))
        raise ValueError(f"unknown experiment_group {group_name!r}; available groups: {available}")
    app_ids = groups[group_name].get("app_ids", [])
    if not app_ids:
        raise ValueError(f"experiment_group {group_name!r} has no app_ids")
    return {str(app_id) for app_id in app_ids}


def build_daily_matrix_rows(
    targets_path: Path,
    *,
    limit: int = 0,
    target_offset: int = 0,
    experiment_group: str = "",
    group_path: Path = DEFAULT_EXPERIMENT_GROUPS,
) -> list[dict[str, Any]]:
    limit = max(0, int(limit))
    target_offset = max(0, int(target_offset))
    group_ids = load_experiment_group_ids(experiment_group, group_path) if experiment_group else set()

    rows = []
    with targets_path.open(newline="", encoding="utf-8") as handle:
        active_index = 0
        for row in csv.DictReader(handle):
            if str(row.get("active", "")).strip().lower() != "true":
                continue
            app_id = str(row["apple_app_id"])
            rows.append(
                {
                    "target_offset": active_index,
                    "app_id": app_id,
                    "app_name": row["app_name"],
                }
            )
            active_index += 1

    if group_ids:
        active_ids = {row["app_id"] for row in rows}
        missing_ids = sorted(group_ids - active_ids)
        if missing_ids:
            raise ValueError(
                f"experiment_group {experiment_group!r} includes inactive or missing app_ids: "
                f"{', '.join(missing_ids)}"
            )
        rows = [row for row in rows if row["app_id"] in group_ids]

    rows = rows[target_offset:]
    if limit:
        rows = rows[:limit]
    return rows

