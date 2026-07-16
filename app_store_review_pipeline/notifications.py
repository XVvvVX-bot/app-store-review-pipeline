from __future__ import annotations

import hashlib
import json
import os
import re
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable


DEFAULT_NOTIFICATION_RESULT = Path("data/reports/monitoring/notification_result.json")
DEFAULT_NOTIFICATION_PREVIEW = Path("data/reports/monitoring/notification_preview.eml")


def build_monitoring_notification(summary: dict[str, Any]) -> dict[str, Any]:
    metadata = summary.get("metadata") or {}
    run = summary.get("run_metrics") or {}
    failing_alerts = [alert for alert in summary.get("alerts", []) if alert.get("severity") == "failing"]
    codes = [str(alert.get("code") or "monitoring_failure") for alert in failing_alerts]
    primary = failing_alerts[0] if failing_alerts else {}
    run_id = str(metadata.get("github_run_id") or "unknown")
    event_name = str(metadata.get("github_event_name") or "")
    run_attempt = int(metadata.get("github_run_attempt") or 1)
    eligible = summary.get("status") == "failing" and event_name == "schedule" and run_attempt == 1
    affected_scopes = select_affected_scopes(summary)
    primary_code = str(primary.get("code") or "monitoring_failure")
    primary_message = str(primary.get("message") or "The ingestion monitor classified the run as failing.")
    subject = f"[App Store Review Pipeline] FAILING: {primary_code} (run {run_id})"
    metrics = {
        "pages": int(run.get("page_count") or 0),
        "rows": int(run.get("review_rows") or 0),
        "inserted": int(run.get("reviews_inserted") or 0),
        "duplicates": int(run.get("duplicates_skipped") or 0),
        "http_429": int(run.get("http_429_pages") or 0),
        "other_non_200": int(run.get("other_non_200_pages") or 0),
        "fetch_errors": int(run.get("fetch_errors") or 0),
    }
    body = render_notification_body(
        primary_code=primary_code,
        primary_message=primary_message,
        affected_scopes=affected_scopes,
        metrics=metrics,
        github_run_url=str(metadata.get("github_run_url") or ""),
        generated_at=str(metadata.get("generated_at") or ""),
    )
    fingerprint_input = "|".join([run_id, *sorted(codes)])
    return {
        "eligible": eligible,
        "reason": "failing_scheduled_first_attempt" if eligible else notification_skip_reason(summary),
        "primary_code": primary_code,
        "failing_codes": codes,
        "affected_scopes": affected_scopes,
        "key_metrics": metrics,
        "subject": subject,
        "body": body,
        "fingerprint": hashlib.sha256(fingerprint_input.encode("utf-8")).hexdigest()[:20],
    }


def notification_skip_reason(summary: dict[str, Any]) -> str:
    metadata = summary.get("metadata") or {}
    if summary.get("status") != "failing":
        return "status_not_failing"
    if str(metadata.get("github_event_name") or "") != "schedule":
        return "not_scheduled_production_run"
    if int(metadata.get("github_run_attempt") or 1) != 1:
        return "rerun_attempt"
    return "not_eligible"


def select_affected_scopes(summary: dict[str, Any], *, limit: int = 3) -> list[dict[str, Any]]:
    failing_codes = {
        str(alert.get("code") or "")
        for alert in summary.get("alerts", [])
        if alert.get("severity") == "failing"
    }
    if "workflow_failure" in failing_codes:
        return [
            {
                "app_name": row.get("name") or "required GitHub job",
                "country": "n/a",
                "reason": row.get("conclusion") or "failed",
            }
            for row in (summary.get("github") or {}).get("failed_jobs", [])[:limit]
        ]
    if failing_codes & {"stale_apps_36h", "postgres_ingestion_stale_36h"}:
        return [
            {
                "app_id": row.get("app_id"),
                "app_name": row.get("app_name"),
                "country": row.get("country"),
                "reason": row.get("last_terminal_reason") or "stale",
                "hours_since_completed": row.get("hours_since_completed"),
            }
            for row in summary.get("stale_apps", [])[:limit]
        ]
    pressure_codes = {"excessive_http_429", "fetch_error_rate", "backlog_terminal_rate"}
    if not failing_codes & pressure_codes:
        return []
    return [
        {
            "app_id": row.get("app_id"),
            "app_name": row.get("app_name"),
            "country": row.get("country"),
            "reason": row.get("terminal_reason") or "source_pressure",
            "pages": row.get("page_count"),
            "http_429": row.get("http_429_pages"),
            "fetch_errors": row.get("fetch_error_pages"),
        }
        for row in (summary.get("app_metrics") or {}).get("pressure_scopes", [])[:limit]
    ]


def render_notification_body(
    *,
    primary_code: str,
    primary_message: str,
    affected_scopes: list[dict[str, Any]],
    metrics: dict[str, int],
    github_run_url: str,
    generated_at: str,
) -> str:
    scope_text = ", ".join(
        f"{scope.get('app_name') or scope.get('app_id') or 'unknown'} ({scope.get('country') or 'n/a'})"
        for scope in affected_scopes
    ) or "pipeline-wide or unavailable"
    metric_text = ", ".join(f"{key}={value}" for key, value in metrics.items())
    return "\n".join(
        [
            "App Store Review Pipeline status: FAILING",
            f"Reason: {primary_code} - {primary_message}",
            f"Affected scope: {scope_text}",
            f"Key metrics: {metric_text}",
            "Why it matters: operator attention is required before relying on the next refresh.",
            f"Evidence: {github_run_url or 'GitHub Actions run URL unavailable'}",
            f"Detected: {generated_at or 'unknown'}",
        ]
    )


def send_monitoring_email(
    report_path: Path,
    *,
    result_path: Path = DEFAULT_NOTIFICATION_RESULT,
    preview_path: Path = DEFAULT_NOTIFICATION_PREVIEW,
    dry_run: bool = False,
    force: bool = False,
    environ: dict[str, str] | None = None,
    smtp_factory: Callable[..., Any] = smtplib.SMTP,
) -> dict[str, Any]:
    summary = json.loads(report_path.read_text(encoding="utf-8"))
    notification = summary.get("notification") or build_monitoring_notification(summary)
    result = {
        "status": "skipped",
        "reason": notification.get("reason"),
        "fingerprint": notification.get("fingerprint"),
        "recipient_count": 0,
        "subject": notification.get("subject"),
    }
    if not notification.get("eligible") and not force:
        return write_notification_result(result_path, result)
    if summary.get("status") != "failing":
        result["reason"] = "force_requires_failing_report"
        return write_notification_result(result_path, result)

    env = dict(os.environ if environ is None else environ)
    recipients = parse_recipients(env.get("APP_STORE_ALERT_EMAIL_TO", ""))
    username = env.get("APP_STORE_ALERT_SMTP_USERNAME", "").strip()
    password = env.get("APP_STORE_ALERT_SMTP_APP_PASSWORD", "")
    from_address = env.get("APP_STORE_ALERT_EMAIL_FROM", "").strip() or username
    host = env.get("APP_STORE_ALERT_SMTP_HOST", "smtp.gmail.com").strip() or "smtp.gmail.com"
    port = int(env.get("APP_STORE_ALERT_SMTP_PORT", "587") or 587)
    result["recipient_count"] = len(recipients)
    if not recipients or not username or not password or not from_address:
        result.update(status="not_configured", reason="missing_email_secrets")
        return write_notification_result(result_path, result)

    message = EmailMessage()
    message["Subject"] = str(notification.get("subject") or "App Store Review Pipeline failing")
    message["From"] = from_address
    message["To"] = ", ".join(recipients)
    message.set_content(str(notification.get("body") or ""))

    if dry_run:
        preview_path.parent.mkdir(parents=True, exist_ok=True)
        preview_path.write_bytes(message.as_bytes())
        result.update(status="dry_run", reason="preview_written", preview_path=str(preview_path))
        return write_notification_result(result_path, result)

    try:
        with smtp_factory(host, port, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
            smtp.login(username, password)
            smtp.send_message(message)
    except Exception as exc:
        result.update(status="failed", reason="smtp_delivery_failed", error_type=type(exc).__name__)
        write_notification_result(result_path, result)
        raise
    result.update(status="sent", reason="failing_alert_delivered")
    return write_notification_result(result_path, result)


def parse_recipients(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"[,;]", value) if part.strip()]


def write_notification_result(path: Path, result: dict[str, Any]) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result
