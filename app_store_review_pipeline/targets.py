from __future__ import annotations

import csv
from pathlib import Path

from app_store_review_pipeline.config import DEFAULT_COUNTRY
from app_store_review_pipeline.models import AppTarget
from app_store_review_pipeline.utils import parse_bool


REQUIRED_COLUMNS = {
    "app_name",
    "category",
    "apple_app_id",
    "apple_slug",
    "countries",
    "active",
    "notes",
}


def load_targets(path: Path) -> list[AppTarget]:
    if not path.exists():
        raise FileNotFoundError(f"Target file does not exist: {path}")

    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"Target file is missing columns: {', '.join(sorted(missing))}")
        return [target_from_row(row, row_number) for row_number, row in enumerate(reader, start=2)]


def active_targets(targets: list[AppTarget]) -> list[AppTarget]:
    return [target for target in targets if target.active]


def target_from_row(row: dict[str, str], row_number: int) -> AppTarget:
    app_name = row["app_name"].strip()
    apple_app_id = row["apple_app_id"].strip()
    apple_slug = row["apple_slug"].strip()
    countries = parse_countries(row.get("countries", ""))

    if not app_name:
        raise ValueError(f"Row {row_number} is missing app_name")
    if not apple_app_id.isdigit():
        raise ValueError(f"Row {row_number} has invalid apple_app_id: {apple_app_id}")
    if not apple_slug:
        raise ValueError(f"Row {row_number} is missing apple_slug")

    return AppTarget(
        app_name=app_name,
        category=row["category"].strip(),
        apple_app_id=apple_app_id,
        apple_slug=apple_slug,
        countries=countries,
        active=parse_bool(row["active"], row_number),
        notes=row.get("notes", "").strip() or None,
    )


def parse_countries(value: str) -> tuple[str, ...]:
    raw = value.strip() or DEFAULT_COUNTRY
    normalized = raw.replace(",", "|")
    countries = tuple(country.strip().lower() for country in normalized.split("|") if country.strip())
    return countries or (DEFAULT_COUNTRY,)
