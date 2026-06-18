from __future__ import annotations

import os
from pathlib import Path


DEFAULT_TARGETS = Path("data/targets/apple_apps.csv")
DEFAULT_RAW_ROOT = Path("data/raw/apple_rss")
DEFAULT_REPORTS_ROOT = Path("data/reports/apple_rss")
DEFAULT_WEB_REPORTS_ROOT = Path("data/reports/apple_web")
DEFAULT_COMPARE_RAW_ROOT = Path("data/raw/source_compare")
DEFAULT_COMPARE_REPORTS_ROOT = Path("data/reports/source_compare")
DEFAULT_DATABASE_URL = os.environ.get("APP_STORE_DATABASE_URL", "postgresql:///app_store_reviews")

DEFAULT_COUNTRY = "us"
DEFAULT_SORT_BY = "mostrecent"
DEFAULT_MAX_PAGES_PER_APP_COUNTRY = 10
DEFAULT_MAX_CONSECUTIVE_EMPTY_PAGES = 10
DEFAULT_REQUEST_DELAY_SECONDS = 1.0
DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_RETRY_DELAY_SECONDS = 5.0

PLATFORM = "apple_app_store"
SOURCE = "apple_itunes_customerreviews_rss"
