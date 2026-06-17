from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote


REQUIRED_COLUMNS = {
    "app_name",
    "category",
    "google_play_package",
    "apple_app_id",
    "apple_slug",
    "active",
    "notes",
}


@dataclass(frozen=True)
class AppTarget:
    app_name: str
    category: str
    google_play_package: str
    apple_app_id: str
    apple_slug: str
    active: bool
    notes: str | None

    @property
    def google_play_url(self) -> str:
        package = quote(self.google_play_package, safe=".")
        return f"https://play.google.com/store/apps/details?id={package}&hl=en_US&gl=US"

    @property
    def apple_app_store_url(self) -> str:
        slug = quote(self.apple_slug, safe="-")
        return f"https://apps.apple.com/us/app/{slug}/id{self.apple_app_id}"


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
    google_play_package = row["google_play_package"].strip()
    apple_app_id = row["apple_app_id"].strip()
    apple_slug = row["apple_slug"].strip()
    if not app_name:
        raise ValueError(f"Row {row_number} is missing app_name")
    if not google_play_package:
        raise ValueError(f"Row {row_number} is missing google_play_package")
    if not apple_app_id.isdigit():
        raise ValueError(f"Row {row_number} has invalid apple_app_id: {apple_app_id}")
    if not apple_slug:
        raise ValueError(f"Row {row_number} is missing apple_slug")

    return AppTarget(
        app_name=app_name,
        category=row["category"].strip(),
        google_play_package=google_play_package,
        apple_app_id=apple_app_id,
        apple_slug=apple_slug,
        active=parse_bool(row["active"], row_number),
        notes=row.get("notes", "").strip() or None,
    )


def parse_bool(value: str, row_number: int) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y"}:
        return True
    if normalized in {"0", "false", "no", "n"}:
        return False
    raise ValueError(f"Row {row_number} has invalid active value: {value}")

