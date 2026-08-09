from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app_store_review_pipeline.incidents import (
    coordinate_monitoring_incident,
    full_scope_recovery_complete,
    incident_marker,
)
from app_store_review_pipeline.notifications import build_monitoring_notification, send_monitoring_email
from app_store_review_pipeline.runner_supervisor import (
    RunnerSupervisor,
    SupervisorConfig,
    execution_complete,
    execution_monitor_verified,
    prepare_supervisor_runtime,
    reset_supervisor_recovery_state,
    target_offset_for_app,
    unique_backlog_apps,
    update_env_value,
)


def monitoring_summary(
    *,
    status: str = "failing",
    code: str = "runner_unavailable",
    event: str = "schedule",
    selected: int = 200,
    intended: int = 200,
    completed: int = 0,
    backlog: int = 0,
    hard_failure: int = 0,
    missing: int = 200,
) -> dict:
    severity = "failing" if status == "failing" else "degraded" if status == "degraded" else "healthy"
    summary = {
        "metadata": {
            "generated_at": "2026-08-09T20:00:00Z",
            "github_run_id": "123",
            "github_run_url": "https://github.com/example/repo/actions/runs/123",
            "github_event_name": event,
            "github_run_attempt": 1,
            "selected_count": selected,
        },
        "status": status,
        "alerts": [{"severity": severity, "code": code, "message": "fixture"}],
        "github": {"failed_jobs": []},
        "run_metrics": {
            "intended_scope_count": intended,
            "completed_scope_count": completed,
            "caught_up_scope_count": max(0, completed - backlog - hard_failure),
            "backlogged_scope_count": backlog,
            "hard_failure_scope_count": hard_failure,
            "missing_scope_count": missing,
            "page_count": completed,
            "review_rows": completed * 20,
            "reviews_inserted": completed,
            "reviews_updated": 0,
            "duplicates_skipped": completed * 19,
            "http_429_pages": 0,
            "other_non_200_pages": 0,
            "fetch_errors": 0,
        },
        "app_metrics": {"pressure_scopes": []},
        "stale_apps": [],
    }
    summary["notification"] = build_monitoring_notification(summary)
    return summary


class FakeIssueClient:
    def __init__(self, issues=None):
        self.issues = list(issues or [])
        self.comments = []
        self.closed = []

    def open_incidents(self):
        return [issue for issue in self.issues if issue.get("state", "open") == "open"]

    def create_issue(self, *, title, body):
        issue = {
            "number": len(self.issues) + 1,
            "title": title,
            "body": body,
            "html_url": f"https://github.com/example/repo/issues/{len(self.issues) + 1}",
            "state": "open",
        }
        self.issues.append(issue)
        return issue

    def comment(self, issue_number, body):
        self.comments.append((issue_number, body))

    def close(self, issue_number):
        self.closed.append(issue_number)
        for issue in self.issues:
            if issue["number"] == issue_number:
                issue["state"] = "closed"


def test_runner_outage_incident_uses_one_issue_and_heartbeat_email_owner():
    client = FakeIssueClient()
    summary = monitoring_summary()

    opened = coordinate_monitoring_incident(
        summary,
        repository="example/repo",
        token="fixture",
        client=client,
    )
    repeated = coordinate_monitoring_incident(
        summary,
        repository="example/repo",
        token="fixture",
        client=client,
    )

    assert opened["status"] == "opened"
    assert opened["email_action"] == "none"
    assert opened["reason"] == "runner_outage_owned_by_heartbeat"
    assert incident_marker("runner_outage") in client.issues[0]["body"]
    assert repeated["status"] == "updated"
    assert repeated["reason"] == "incident_already_open"
    assert len(client.issues) == 1
    assert len(client.comments) == 1


def test_pipeline_failure_sends_once_and_full_scope_recovery_closes_issue():
    client = FakeIssueClient()
    failure = monitoring_summary(code="change_accounting_mismatch")

    opened = coordinate_monitoring_incident(
        failure,
        repository="example/repo",
        token="fixture",
        client=client,
    )
    repeated = coordinate_monitoring_incident(
        failure,
        repository="example/repo",
        token="fixture",
        client=client,
    )
    recovered = monitoring_summary(
        status="degraded",
        code="high_duplicate_rate",
        event="workflow_dispatch",
        completed=200,
        missing=0,
    )
    resolved = coordinate_monitoring_incident(
        recovered,
        repository="example/repo",
        token="fixture",
        outage_recovery=True,
        recovery_incident_id="outage-fixture",
        recovery_phase="verify",
        client=client,
    )

    assert opened["email_action"] == "failure"
    assert repeated["email_action"] == "none"
    assert full_scope_recovery_complete(recovered)
    assert resolved["status"] == "resolved"
    assert resolved["email_action"] == "recovery"
    assert resolved["recovery_complete"] is True
    assert client.closed == [1]


def test_partial_or_backlogged_run_cannot_resolve_incident():
    issue = {
        "number": 7,
        "body": incident_marker("runner_outage"),
        "html_url": "https://github.com/example/repo/issues/7",
        "state": "open",
    }
    client = FakeIssueClient([issue])
    partial = monitoring_summary(
        status="degraded",
        code="backlogged_scopes",
        event="workflow_dispatch",
        completed=200,
        backlog=1,
        missing=0,
    )

    result = coordinate_monitoring_incident(
        partial,
        repository="example/repo",
        token="fixture",
        outage_recovery=True,
        client=client,
    )

    assert result["status"] == "no_change"
    assert result["recovery_complete"] is False
    assert client.closed == []


def test_incomplete_outage_recovery_reuses_open_incident_without_email():
    issue = {
        "number": 7,
        "body": incident_marker("runner_outage"),
        "html_url": "https://github.com/example/repo/issues/7",
        "state": "open",
    }
    client = FakeIssueClient([issue])

    result = coordinate_monitoring_incident(
        monitoring_summary(code="stale_active_apps"),
        repository="example/repo",
        token="fixture",
        outage_recovery=True,
        recovery_phase="backlog",
        client=client,
    )

    assert result["status"] == "updated"
    assert result["reason"] == "recovery_attempt_incomplete"
    assert result["email_action"] == "none"
    assert len(client.issues) == 1
    assert len(client.comments) == 1


def test_outage_recovery_without_open_issue_uses_heartbeat_owned_incident():
    client = FakeIssueClient()

    result = coordinate_monitoring_incident(
        monitoring_summary(code="stale_active_apps", event="workflow_dispatch"),
        repository="example/repo",
        token="fixture",
        outage_recovery=True,
        recovery_phase="full",
        client=client,
    )

    assert result["status"] == "opened"
    assert result["incident_key"] == "runner_outage"
    assert result["email_action"] == "none"
    assert result["reason"] == "runner_outage_owned_by_heartbeat"


def test_mixed_incident_recovery_sends_pipeline_recovery_email():
    issues = [
        {
            "number": 7,
            "body": incident_marker("runner_outage"),
            "html_url": "https://github.com/example/repo/issues/7",
            "state": "open",
        },
        {
            "number": 8,
            "body": incident_marker("pipeline_failure:fetch_error_rate"),
            "html_url": "https://github.com/example/repo/issues/8",
            "state": "open",
        },
    ]
    client = FakeIssueClient(issues)
    recovered = monitoring_summary(
        status="healthy",
        code="all_clear",
        event="workflow_dispatch",
        completed=200,
        missing=0,
    )

    result = coordinate_monitoring_incident(
        recovered,
        repository="example/repo",
        token="fixture",
        outage_recovery=True,
        client=client,
    )

    assert result["status"] == "resolved"
    assert result["email_action"] == "recovery"
    assert result["resolved_issue_count"] == 2
    assert client.closed == [7, 8]


def test_incident_coordination_failure_fails_open_to_email():
    class BrokenClient(FakeIssueClient):
        def open_incidents(self):
            raise RuntimeError("fixture")

    result = coordinate_monitoring_incident(
        monitoring_summary(),
        repository="example/repo",
        token="fixture",
        client=BrokenClient(),
    )

    assert result["status"] == "coordination_failed"
    assert result["email_action"] == "failure"
    assert result["email_notification"]["eligible"] is True


def test_recovery_incident_email_can_send_for_nonfailing_report(tmp_path):
    report_path = tmp_path / "report.json"
    incident_path = tmp_path / "incident.json"
    result_path = tmp_path / "result.json"
    report = monitoring_summary(status="healthy", code="all_clear", completed=200, missing=0)
    report_path.write_text(json.dumps(report), encoding="utf-8")
    incident_path.write_text(
        json.dumps(
            {
                "email_action": "recovery",
                "reason": "incident_resolved",
                "email_notification": {
                    "eligible": True,
                    "kind": "recovery",
                    "reason": "incident_resolved",
                    "subject": "Recovered",
                    "body": "Recovered body",
                    "fingerprint": "fixture",
                },
            }
        ),
        encoding="utf-8",
    )

    class FakeSmtp:
        sent = []

        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def ehlo(self):
            pass

        def starttls(self, **kwargs):
            pass

        def login(self, username, password):
            pass

        def send_message(self, message):
            self.sent.append(message)

    result = send_monitoring_email(
        report_path,
        incident_path=incident_path,
        result_path=result_path,
        environ={
            "APP_STORE_ALERT_SMTP_USERNAME": "alerts@example.com",
            "APP_STORE_ALERT_SMTP_APP_PASSWORD": "fixture",
            "APP_STORE_ALERT_EMAIL_FROM": "alerts@example.com",
            "APP_STORE_ALERT_EMAIL_TO": "operator@example.com",
        },
        smtp_factory=FakeSmtp,
    )

    assert result["status"] == "sent"
    assert result["reason"] == "recovery_alert_delivered"
    assert len(FakeSmtp.sent) == 1


def test_supervisor_waits_for_stability_before_dispatch(monkeypatch, tmp_path):
    current = [datetime(2026, 8, 9, 20, 0, tzinfo=timezone.utc)]
    config = SupervisorConfig(repo_path=tmp_path, stable_seconds=300)
    supervisor = RunnerSupervisor(config, state_path=tmp_path / "state.json", now=lambda: current[0])
    monkeypatch.setattr(supervisor, "health_snapshot", lambda: {"healthy": True})
    monkeypatch.setattr(
        "app_store_review_pipeline.runner_supervisor.outage_recovery_status_postgres",
        lambda *args, **kwargs: {
            "stale_24h": 10,
            "stale_36h": 10,
            "backlogged_scope_count": 0,
            "scope_count": 200,
            "backlogged_scopes": [],
            "execution": None,
        },
    )
    monkeypatch.setattr(supervisor, "nonstale_active_daily_runs", lambda now: [])
    monkeypatch.setattr(supervisor, "cancel_stale_daily_runs", lambda now: [])
    monkeypatch.setattr(
        "app_store_review_pipeline.runner_supervisor.reconcile_stale_executions_postgres",
        lambda *args, **kwargs: {"reconciled_count": 1, "executions": []},
    )
    dispatched = []
    monkeypatch.setattr(supervisor, "dispatch_recovery", lambda **kwargs: dispatched.append(kwargs) or "9001")

    first = supervisor.run_once()
    current[0] += timedelta(seconds=299)
    second = supervisor.run_once()
    current[0] += timedelta(seconds=2)
    third = supervisor.run_once()

    assert first["state"]["phase"] == "stabilizing"
    assert second["state"]["phase"] == "stabilizing"
    assert third["state"]["phase"] == "recovery_full"
    assert third["state"]["current_run_id"] == "9001"
    assert len(dispatched) == 1
    assert dispatched[0]["token"].endswith(":full:attempt-1")


def test_supervisor_marks_outage_once_after_threshold(monkeypatch, tmp_path):
    current = [datetime(2026, 8, 9, 20, 0, tzinfo=timezone.utc)]
    config = SupervisorConfig(repo_path=tmp_path, outage_seconds=600)
    supervisor = RunnerSupervisor(config, state_path=tmp_path / "state.json", now=lambda: current[0])
    health = {"healthy": False, "services": [], "offline_runners": []}
    monkeypatch.setattr(supervisor, "health_snapshot", lambda: health)
    monkeypatch.setattr(supervisor, "restart_unhealthy_services", lambda *args, **kwargs: [])
    heartbeats = []
    monkeypatch.setattr(supervisor, "heartbeat", lambda action, state, snapshot: heartbeats.append(action))

    supervisor.run_once()
    current[0] += timedelta(seconds=601)
    outage = supervisor.run_once()
    current[0] += timedelta(seconds=60)
    supervisor.run_once()

    assert outage["state"]["phase"] == "outage"
    assert heartbeats == ["fail"]


def test_recovery_dispatch_uses_valid_fixed_pressure_mode(monkeypatch, tmp_path):
    supervisor = RunnerSupervisor(
        SupervisorConfig(repo_path=tmp_path),
        state_path=tmp_path / "state.json",
        now=lambda: datetime(2026, 8, 9, 20, 0, tzinfo=timezone.utc),
    )
    calls = []

    def fake_gh_json(args, *, expect_json=True):
        calls.append((args, expect_json))
        if args[:2] == ["workflow", "run"]:
            return None
        return [
            {
                "databaseId": 8003,
                "displayTitle": "Outage recovery outage:fixture:full",
                "status": "completed",
                "createdAt": "2026-08-09T18:00:00Z",
            },
            {
                "databaseId": 9003,
                "displayTitle": "Outage recovery outage:fixture:full",
                "status": "queued",
                "createdAt": "2026-08-09T20:00:00Z",
            }
        ]

    monkeypatch.setattr(supervisor, "gh_json", fake_gh_json)

    run_id = supervisor.dispatch_recovery(token="outage:fixture:full")

    dispatch_args = calls[0][0]
    fields = [dispatch_args[index + 1] for index, value in enumerate(dispatch_args) if value == "-f"]
    assert run_id == "9003"
    assert "pressure_ramp_mode=fixed" in fields
    assert "outage_recovery=true" in fields
    assert all("pressure_ramp_mode=outage_recovery" != field for field in fields)


def test_completed_recovery_uses_database_monitor_verification(monkeypatch, tmp_path):
    current = datetime(2026, 8, 9, 20, 0, tzinfo=timezone.utc)
    supervisor = RunnerSupervisor(SupervisorConfig(repo_path=tmp_path), state_path=tmp_path / "state.json")
    monkeypatch.setattr(
        supervisor,
        "gh_json",
        lambda *args, **kwargs: {"status": "completed", "conclusion": "failure", "url": "fixture"},
    )
    status = {
        "stale_24h": 0,
        "stale_36h": 0,
        "backlogged_scope_count": 0,
        "scope_count": 200,
        "backlogged_scopes": [],
        "execution": {
            "status": "degraded",
            "intended_scope_count": 200,
            "completed_scope_count": 200,
            "hard_failure_scope_count": 0,
        },
    }
    monkeypatch.setattr(
        "app_store_review_pipeline.runner_supervisor.outage_recovery_status_postgres",
        lambda *args, **kwargs: status,
    )
    state = {"phase": "recovery_full", "current_run_id": "9004"}

    supervisor.advance_recovery(state, current)

    assert state["phase"] == "idle"
    assert state.get("manual_attention_reason") is None


def test_failing_database_monitor_status_blocks_recovery_resolution(monkeypatch, tmp_path):
    current = datetime(2026, 8, 9, 20, 0, tzinfo=timezone.utc)
    supervisor = RunnerSupervisor(SupervisorConfig(repo_path=tmp_path), state_path=tmp_path / "state.json")
    monkeypatch.setattr(
        supervisor,
        "gh_json",
        lambda *args, **kwargs: {"status": "completed", "conclusion": "failure", "url": "fixture"},
    )
    monkeypatch.setattr(
        "app_store_review_pipeline.runner_supervisor.outage_recovery_status_postgres",
        lambda *args, **kwargs: {
            "backlogged_scopes": [],
            "execution": {
                "status": "failing",
                "intended_scope_count": 200,
                "completed_scope_count": 200,
                "hard_failure_scope_count": 0,
            },
        },
    )
    state = {"phase": "recovery_full", "current_run_id": "9005"}

    supervisor.advance_recovery(state, current)

    assert state["phase"] == "manual_attention"
    assert state["manual_attention_reason"] == "full_recovery_monitor_not_verified"


def test_infrastructure_recovery_failure_gets_bounded_stability_retry(monkeypatch, tmp_path):
    current = datetime(2026, 8, 9, 20, 0, tzinfo=timezone.utc)
    supervisor = RunnerSupervisor(SupervisorConfig(repo_path=tmp_path), state_path=tmp_path / "state.json")
    monkeypatch.setattr(
        supervisor,
        "gh_json",
        lambda *args, **kwargs: {"status": "completed", "conclusion": "failure", "url": "fixture"},
    )
    monkeypatch.setattr(
        "app_store_review_pipeline.runner_supervisor.outage_recovery_status_postgres",
        lambda *args, **kwargs: {"backlogged_scopes": [], "execution": None},
    )
    state = {
        "phase": "recovery_full",
        "current_run_id": "9007",
        "full_recovery_attempts": 1,
    }

    supervisor.advance_recovery(state, current)

    assert state["phase"] == "stabilizing"
    assert state["pending_recovery_phase"] == "full"
    assert state["healthy_since"] == "2026-08-09T20:00:00Z"
    assert state["current_run_id"] is None


def test_infrastructure_recovery_stops_after_attempt_limit(monkeypatch, tmp_path):
    current = datetime(2026, 8, 9, 20, 0, tzinfo=timezone.utc)
    supervisor = RunnerSupervisor(
        SupervisorConfig(repo_path=tmp_path, max_recovery_attempts=1),
        state_path=tmp_path / "state.json",
    )
    state = {"phase": "recovery_full", "full_recovery_attempts": 1}

    supervisor.retry_infrastructure_recovery(state, current, phase="recovery_full")

    assert state["phase"] == "manual_attention"
    assert state["manual_attention_reason"] == "full_recovery_attempt_limit"


def test_backlog_retry_deadline_is_not_moved_on_every_tick(monkeypatch, tmp_path):
    current = datetime(2026, 8, 9, 20, 0, tzinfo=timezone.utc)
    config = SupervisorConfig(repo_path=tmp_path, backlog_retry_minutes=30)
    supervisor = RunnerSupervisor(config, state_path=tmp_path / "state.json")
    monkeypatch.setattr(
        "app_store_review_pipeline.runner_supervisor.outage_recovery_status_postgres",
        lambda *args, **kwargs: {
            "backlogged_scopes": [{"app_id": "222"}],
            "backlogged_scope_count": 1,
            "stale_24h": 0,
            "stale_36h": 0,
            "scope_count": 200,
            "execution": None,
        },
    )
    monkeypatch.setattr("app_store_review_pipeline.runner_supervisor.target_offset_for_app", lambda *args: 4)
    dispatched = []
    monkeypatch.setattr(supervisor, "dispatch_recovery", lambda **kwargs: dispatched.append(kwargs) or "9002")
    monkeypatch.setattr(supervisor, "nonstale_active_daily_runs", lambda now: [])
    state = {
        "phase": "recovery_backlog",
        "incident_id": "fixture",
        "backlog_queue": ["222"],
        "backlog_attempts": {"222": 1},
        "current_backlog_app": "222",
        "current_run_id": None,
        "current_run": {"status": "completed", "conclusion": "success"},
    }

    supervisor.advance_backlog(state, current)
    first_deadline = state["not_before"]
    supervisor.advance_backlog(state, current + timedelta(minutes=10))
    assert state["not_before"] == first_deadline
    assert dispatched == []
    supervisor.advance_backlog(state, current + timedelta(minutes=31))

    assert len(dispatched) == 1
    assert state["current_run_id"] == "9002"
    assert state["backlog_attempts"]["222"] == 2


def test_backlog_recovery_waits_for_active_daily_run(monkeypatch, tmp_path):
    current = datetime(2026, 8, 9, 20, 0, tzinfo=timezone.utc)
    supervisor = RunnerSupervisor(SupervisorConfig(repo_path=tmp_path), state_path=tmp_path / "state.json")
    monkeypatch.setattr(
        "app_store_review_pipeline.runner_supervisor.outage_recovery_status_postgres",
        lambda *args, **kwargs: {"backlogged_scopes": [{"app_id": "222"}]},
    )
    monkeypatch.setattr(supervisor, "nonstale_active_daily_runs", lambda now: [{"databaseId": 88}])
    dispatched = []
    monkeypatch.setattr(supervisor, "dispatch_recovery", lambda **kwargs: dispatched.append(kwargs) or "9006")
    state = {
        "phase": "recovery_backlog",
        "incident_id": "fixture",
        "backlog_queue": ["222"],
        "backlog_attempts": {},
    }

    supervisor.advance_backlog(state, current)

    assert dispatched == []
    assert state["backlog_queue"] == ["222"]


def test_recovery_helpers_preserve_scope_safety(tmp_path):
    targets = tmp_path / "targets.csv"
    targets.write_text(
        "app_name,category,apple_app_id,apple_slug,countries,active,notes\n"
        "First,test,111,first,us,true,\n"
        "Second,test,222,second,us,true,\n",
        encoding="utf-8",
    )

    assert target_offset_for_app(targets, "222") == 1
    assert unique_backlog_apps([{"app_id": "222"}, {"app_id": "222"}, {"app_id": "111"}]) == ["222", "111"]
    assert execution_complete({"intended_scope_count": 200, "completed_scope_count": 200, "hard_failure_scope_count": 0})
    assert not execution_complete({"intended_scope_count": 200, "completed_scope_count": 199, "hard_failure_scope_count": 0})
    assert execution_monitor_verified({"status": "healthy"})
    assert execution_monitor_verified({"status": "degraded"})
    assert not execution_monitor_verified({"status": "failing"})


def test_supervisor_runtime_is_deployed_outside_source_repo(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    source_python = repo / ".venv/bin/python"
    (repo / "app_store_review_pipeline").mkdir(parents=True)
    (repo / "data/targets").mkdir(parents=True)
    source_python.parent.mkdir(parents=True)
    source_python.write_text("fixture", encoding="utf-8")
    (repo / "app_store_pipeline.py").write_text("print('fixture')\n", encoding="utf-8")
    (repo / "requirements.lock").write_text("requests==2.32.5\n", encoding="utf-8")
    (repo / "app_store_review_pipeline/__init__.py").write_text("", encoding="utf-8")
    (repo / "data/targets/apple_apps.csv").write_text("app_name,apple_app_id\n", encoding="utf-8")

    def fake_run(args, **kwargs):
        if args[1:3] == ["-m", "venv"]:
            python = Path(args[3]) / "bin/python"
            python.parent.mkdir(parents=True)
            python.write_text("fixture", encoding="utf-8")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("app_store_review_pipeline.runner_supervisor.subprocess.run", fake_run)

    result = prepare_supervisor_runtime(
        repo_path=repo,
        source_python=source_python,
        runtime_path=runtime,
    )

    assert result["runtime_path"] == str(runtime)
    assert result["python_path"] == str(runtime / ".venv/bin/python")
    assert (runtime / "app_store_review_pipeline/__init__.py").exists()
    assert (runtime / "data/targets/apple_apps.csv").exists()


def test_supervisor_config_update_preserves_secrets(tmp_path):
    config = tmp_path / "supervisor.env"
    config.write_text(
        "APP_STORE_REPO_PATH=/old/path\nAPP_STORE_HEARTBEAT_URL=https://example.invalid/secret\n",
        encoding="utf-8",
    )

    update_env_value(config, "APP_STORE_REPO_PATH", "/new/runtime")

    assert config.read_text(encoding="utf-8") == (
        "APP_STORE_REPO_PATH=/new/runtime\n"
        "APP_STORE_HEARTBEAT_URL=https://example.invalid/secret\n"
    )


def test_manual_attention_reset_preserves_audit_reason(tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "phase": "manual_attention",
                "manual_attention_reason": "runner_capacity_api_failed",
                "current_run_id": "9008",
                "full_recovery_attempts": 3,
            }
        ),
        encoding="utf-8",
    )

    result = reset_supervisor_recovery_state(
        state_path,
        now=datetime(2026, 8, 9, 20, 0, tzinfo=timezone.utc),
    )
    state = json.loads(state_path.read_text(encoding="utf-8"))

    assert result["reset"] is True
    assert state["phase"] == "stabilizing"
    assert state["pending_recovery_phase"] == "full"
    assert state["current_run_id"] is None
    assert state["full_recovery_attempts"] == 0
    assert state["last_manual_attention_reason"] == "runner_capacity_api_failed"
    assert state["manual_attention_reset_at"] == "2026-08-09T20:00:00Z"


def test_workflow_keeps_recovery_within_dispatch_input_limit():
    workflow = Path(__file__).resolve().parents[1] / ".github/workflows/app-store-daily-pipeline.yml"
    text = workflow.read_text(encoding="utf-8")
    lines = text.splitlines()
    names = []
    in_inputs = False
    for line in lines:
        if line == "    inputs:":
            in_inputs = True
            continue
        if in_inputs and line and not line.startswith("      "):
            break
        if in_inputs and line.startswith("      ") and not line.startswith("        "):
            names.append(line.strip().rstrip(":"))

    assert len(names) <= 25
    assert "outage_recovery" in names
    assert "runner-gate:" in text
    assert "runner_unavailable" in text
    assert "secrets.APP_STORE_RUNNER_MONITOR_TOKEN || github.token" in text
    assert "runner_capacity_api_failed" in text
    assert "cancel-in-progress: ${{ github.event_name == 'workflow_dispatch' && inputs.outage_recovery }}" in text
