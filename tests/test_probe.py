import csv
from pathlib import Path

import pytest

from app_store_source_probe.probe import build_probe_report, extract_possible_review_count, scan_storefront_html
from app_store_source_probe.targets import active_targets, load_targets


def write_targets(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "app_name",
                "category",
                "google_play_package",
                "apple_app_id",
                "apple_slug",
                "active",
                "notes",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "app_name": "ChatGPT",
                "category": "ai_tools",
                "google_play_package": "com.openai.chatgpt",
                "apple_app_id": "6448311069",
                "apple_slug": "chatgpt",
                "active": "true",
                "notes": "fixture",
            }
        )
        writer.writerow(
            {
                "app_name": "Inactive",
                "category": "test",
                "google_play_package": "com.example.inactive",
                "apple_app_id": "123456789",
                "apple_slug": "inactive",
                "active": "false",
                "notes": "",
            }
        )


def test_load_targets_and_urls(tmp_path):
    path = tmp_path / "targets.csv"
    write_targets(path)

    targets = load_targets(path)
    active = active_targets(targets)

    assert len(targets) == 2
    assert len(active) == 1
    assert active[0].google_play_url == "https://play.google.com/store/apps/details?id=com.openai.chatgpt&hl=en_US&gl=US"
    assert active[0].apple_app_store_url == "https://apps.apple.com/us/app/chatgpt/id6448311069"


def test_load_targets_rejects_bad_apple_id(tmp_path):
    path = tmp_path / "targets.csv"
    write_targets(path)
    path.write_text(path.read_text(encoding="utf-8").replace("6448311069", "bad-id"), encoding="utf-8")

    with pytest.raises(ValueError, match="invalid apple_app_id"):
        load_targets(path)


def test_scan_storefront_html_detects_signals():
    html = """
    <html>
      <script type="application/ld+json">{"aggregateRating": {"ratingValue": "4.8"}}</script>
      <body>Ratings and Reviews. 12,345 reviews. See all reviews. 5 stars.</body>
    </html>
    """

    scan = scan_storefront_html(html)

    assert scan["has_review_marker"] is True
    assert scan["has_rating_marker"] is True
    assert scan["has_pagination_marker"] is True
    assert scan["has_structured_data_marker"] is True
    assert scan["has_access_control_marker"] is False
    assert scan["possible_review_count"] == 12345


def test_scan_storefront_html_detects_access_control():
    scan = scan_storefront_html("<html>captcha verify you are human</html>")

    assert scan["has_access_control_marker"] is True
    assert "do not retry" in scan["notes"]


def test_extract_possible_review_count_returns_max():
    assert extract_possible_review_count("10 ratings and 1,234 reviews") == 1234
    assert extract_possible_review_count("no counts here") is None


def test_build_probe_report_empty(tmp_path):
    report = build_probe_report(tmp_path / "targets.csv", [])

    assert report["summary"] == {}
    assert "hidden endpoints" in report["ethical_boundary"]
    assert "do not prove" in report["interpretation"]

