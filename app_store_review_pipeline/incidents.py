from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


INCIDENT_LABEL = "pipeline-incident"
INCIDENT_MARKER_PREFIX = "app-store-pipeline-incident:"
RUNNER_OUTAGE_CODES = {"runner_unavailable", "runner_interrupted"}


class GitHubIssueClient:
    def __init__(
        self,
        repository: str,
        token: str,
        *,
        api_url: str = "https://api.github.com",
        session: Any = requests,
    ) -> None:
        if "/" not in repository:
            raise ValueError("repository must use owner/name form")
        if not token:
            raise ValueError("GitHub token is required for incident coordination")
        self.repository = repository
        self.api_url = api_url.rstrip("/")
        self.session = session
        self.headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self.session.request(
            method,
            f"{self.api_url}/repos/{self.repository}{path}",
            headers=self.headers,
            timeout=30,
            **kwargs,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"GitHub API {method} {path} returned {response.status_code}")
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def ensure_label(self) -> None:
        response = self.session.request(
            "POST",
            f"{self.api_url}/repos/{self.repository}/labels",
            headers=self.headers,
            timeout=30,
            json={"name": INCIDENT_LABEL, "color": "B60205", "description": "Open ingestion pipeline incident"},
        )
        if response.status_code not in {201, 422}:
            raise RuntimeError(f"GitHub label creation returned {response.status_code}")

    def open_incidents(self) -> list[dict[str, Any]]:
        rows = self.request("GET", f"/issues?state=open&labels={INCIDENT_LABEL}&per_page=100") or []
        return [row for row in rows if "pull_request" not in row]

    def create_issue(self, *, title: str, body: str) -> dict[str, Any]:
        self.ensure_label()
        return self.request(
            "POST",
            "/issues",
            json={"title": title, "body": body, "labels": [INCIDENT_LABEL]},
        )

    def comment(self, issue_number: int, body: str) -> None:
        self.request("POST", f"/issues/{issue_number}/comments", json={"body": body})

    def close(self, issue_number: int) -> None:
        self.request("PATCH", f"/issues/{issue_number}", json={"state": "closed", "state_reason": "completed"})


def coordinate_monitoring_incident(
    summary: dict[str, Any],
    *,
    repository: str,
    token: str,
    outage_recovery: bool = False,
    recovery_incident_id: str = "",
    recovery_phase: str = "",
    client: GitHubIssueClient | None = None,
) -> dict[str, Any]:
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    metadata = summary.get("metadata") or {}
    run_id = str(metadata.get("github_run_id") or "unknown")
    event_name = str(metadata.get("github_event_name") or "")
    status = str(summary.get("status") or "failing").lower()
    result: dict[str, Any] = {
        "status": "no_change",
        "email_action": "none",
        "reason": "no_incident_transition",
        "generated_at": generated_at,
        "github_run_id": run_id,
        "incident_key": "",
        "issue_number": None,
        "issue_url": "",
        "recovery_complete": False,
    }
    eligible_event = event_name == "schedule" or outage_recovery
    if not eligible_event:
        result["reason"] = "not_production_or_recovery_run"
        return result

    issue_client = client or GitHubIssueClient(repository, token)
    try:
        open_issues = issue_client.open_incidents()
        if status == "failing":
            if outage_recovery and open_issues:
                issue = open_issues[0]
                issue_client.comment(
                    int(issue["number"]),
                    render_incident_update(summary, heading="Recovery attempt incomplete"),
                )
                result.update(
                    status="updated",
                    reason="recovery_attempt_incomplete",
                    incident_key=incident_key_from_issue(issue),
                    issue_number=int(issue["number"]),
                    issue_url=str(issue.get("html_url") or ""),
                    recovery_phase=recovery_phase,
                )
                return result
            incident_key = "runner_outage" if outage_recovery else incident_key_for_summary(summary)
            result["incident_key"] = incident_key
            issue = find_incident(open_issues, incident_key)
            update = render_incident_update(summary, heading="Repeated failure observed")
            if issue:
                issue_client.comment(int(issue["number"]), update)
                result.update(
                    status="updated",
                    reason="incident_already_open",
                    issue_number=int(issue["number"]),
                    issue_url=str(issue.get("html_url") or ""),
                )
                return result

            issue = issue_client.create_issue(
                title=incident_title(summary, incident_key),
                body=render_incident_body(summary, incident_key=incident_key),
            )
            heartbeat_owned = incident_key == "runner_outage"
            result.update(
                status="opened",
                email_action="none" if heartbeat_owned else "failure",
                reason="runner_outage_owned_by_heartbeat" if heartbeat_owned else "incident_opened",
                issue_number=int(issue["number"]),
                issue_url=str(issue.get("html_url") or ""),
            )
            if not heartbeat_owned:
                result["email_notification"] = failure_email_notification(summary, result["issue_url"])
            return result

        recovery_complete = full_scope_recovery_complete(summary)
        result["recovery_complete"] = recovery_complete
        if not recovery_complete:
            result["reason"] = "run_did_not_prove_full_scope_recovery"
            return result
        if not open_issues:
            result["reason"] = "no_open_incident"
            return result

        issue_urls = []
        has_pipeline_incident = any(
            incident_key_from_issue(issue) != "runner_outage" for issue in open_issues
        )
        for issue in open_issues:
            issue_client.comment(
                int(issue["number"]),
                render_incident_update(summary, heading="Recovery verified"),
            )
            issue_client.close(int(issue["number"]))
            issue_urls.append(str(issue.get("html_url") or ""))
        result.update(
            status="resolved",
            email_action="recovery" if has_pipeline_incident else "none",
            reason="incident_resolved" if has_pipeline_incident else "runner_recovery_owned_by_heartbeat",
            incident_key=recovery_incident_id or "pipeline_recovery",
            issue_number=int(open_issues[0]["number"]),
            issue_url=issue_urls[0] if issue_urls else "",
            resolved_issue_count=len(open_issues),
            recovery_phase=recovery_phase,
        )
        if has_pipeline_incident:
            result["email_notification"] = recovery_email_notification(summary, issue_urls)
        return result
    except Exception as exc:
        result.update(
            status="coordination_failed",
            reason="github_incident_coordination_failed",
            error_type=type(exc).__name__,
        )
        if status == "failing":
            result["email_action"] = "failure"
            result["email_notification"] = failure_email_notification(summary, "")
        return result


def incident_key_for_summary(summary: dict[str, Any]) -> str:
    codes = [
        str(alert.get("code") or "")
        for alert in summary.get("alerts", [])
        if alert.get("severity") == "failing"
    ]
    if set(codes) & RUNNER_OUTAGE_CODES:
        return "runner_outage"
    primary = codes[0] if codes else "monitoring_failure"
    return f"pipeline_failure:{primary}"


def incident_marker(incident_key: str) -> str:
    return f"<!-- {INCIDENT_MARKER_PREFIX}{incident_key} -->"


def incident_key_from_issue(issue: dict[str, Any]) -> str:
    body = str(issue.get("body") or "")
    prefix = f"<!-- {INCIDENT_MARKER_PREFIX}"
    for line in body.splitlines():
        line = line.strip()
        if line.startswith(prefix) and line.endswith(" -->"):
            return line[len(prefix) : -4]
    return ""


def find_incident(issues: list[dict[str, Any]], incident_key: str) -> dict[str, Any] | None:
    return next((issue for issue in issues if incident_key_from_issue(issue) == incident_key), None)


def full_scope_recovery_complete(summary: dict[str, Any]) -> bool:
    if str(summary.get("status") or "").lower() == "failing":
        return False
    metadata = summary.get("metadata") or {}
    run = summary.get("run_metrics") or {}
    selected = int(metadata.get("selected_count") or 0)
    intended = int(run.get("intended_scope_count") or 0)
    completed = int(run.get("completed_scope_count") or 0)
    return (
        selected >= 100
        and intended > 0
        and completed == intended
        and int(run.get("missing_scope_count") or 0) == 0
        and int(run.get("hard_failure_scope_count") or 0) == 0
        and int(run.get("backlogged_scope_count") or 0) == 0
    )


def incident_title(summary: dict[str, Any], incident_key: str) -> str:
    run_id = str((summary.get("metadata") or {}).get("github_run_id") or "unknown")
    code = incident_key.split(":", 1)[-1]
    return f"Pipeline incident: {code} (opened by run {run_id})"


def render_incident_body(summary: dict[str, Any], *, incident_key: str) -> str:
    return "\n".join(
        [
            incident_marker(incident_key),
            "This issue is the durable coordination record for one ingestion incident.",
            "",
            render_incident_update(summary, heading="Incident opened"),
            "",
            "It closes only after a full-scope run completes every intended scope with no backlog, missing scope, or hard failure.",
        ]
    )


def render_incident_update(summary: dict[str, Any], *, heading: str) -> str:
    metadata = summary.get("metadata") or {}
    run = summary.get("run_metrics") or {}
    alerts = ", ".join(
        str(alert.get("code") or "")
        for alert in summary.get("alerts", [])
        if alert.get("severity") in {"failing", "degraded"}
    ) or "all_clear"
    return "\n".join(
        [
            f"### {heading}",
            f"- Run: [{metadata.get('github_run_id') or 'unknown'}]({metadata.get('github_run_url') or '#'})",
            f"- Status: `{summary.get('status') or 'unknown'}`",
            f"- Alerts: `{alerts}`",
            f"- Scopes: intended `{int(run.get('intended_scope_count') or 0)}`, completed `{int(run.get('completed_scope_count') or 0)}`, backlogged `{int(run.get('backlogged_scope_count') or 0)}`, hard failure `{int(run.get('hard_failure_scope_count') or 0)}`, missing `{int(run.get('missing_scope_count') or 0)}`",
            f"- Volume: pages `{int(run.get('page_count') or 0)}`, rows `{int(run.get('review_rows') or 0)}`, inserted `{int(run.get('reviews_inserted') or 0)}`, duplicates `{int(run.get('duplicates_skipped') or 0)}`",
        ]
    )


def failure_email_notification(summary: dict[str, Any], issue_url: str) -> dict[str, Any]:
    notification = dict(summary.get("notification") or {})
    body = str(notification.get("body") or "")
    if issue_url:
        body = f"{body}\nIncident: {issue_url}"
    notification.update(eligible=True, kind="failure", body=body, reason="incident_opened")
    return notification


def recovery_email_notification(summary: dict[str, Any], issue_urls: list[str]) -> dict[str, Any]:
    metadata = summary.get("metadata") or {}
    run = summary.get("run_metrics") or {}
    run_id = str(metadata.get("github_run_id") or "unknown")
    evidence = str(metadata.get("github_run_url") or "GitHub Actions run URL unavailable")
    issue_text = ", ".join(url for url in issue_urls if url) or "GitHub incident issue unavailable"
    subject = f"[App Store Review Pipeline] RECOVERED (run {run_id})"
    body = "\n".join(
        [
            "App Store Review Pipeline status: RECOVERED",
            "Reason: a full-scope verification run completed without missing, backlogged, or hard-failure scopes.",
            f"Scopes: completed={int(run.get('completed_scope_count') or 0)}, inserted={int(run.get('reviews_inserted') or 0)}, pages={int(run.get('page_count') or 0)}",
            f"Evidence: {evidence}",
            f"Incident: {issue_text}",
        ]
    )
    fingerprint = hashlib.sha256(f"recovery|{run_id}|{issue_text}".encode("utf-8")).hexdigest()[:20]
    return {
        "eligible": True,
        "kind": "recovery",
        "reason": "incident_resolved",
        "subject": subject,
        "body": body,
        "fingerprint": fingerprint,
    }


def write_incident_result(path: Path, result: dict[str, Any]) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result
