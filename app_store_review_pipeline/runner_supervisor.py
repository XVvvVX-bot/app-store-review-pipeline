from __future__ import annotations

import json
import os
import plistlib
import pwd
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import requests

from app_store_review_pipeline.config import DEFAULT_DATABASE_URL, DEFAULT_TARGETS, WEB_CATALOG_SOURCE
from app_store_review_pipeline.postgres_database import (
    connect_postgres,
    outage_recovery_status_postgres,
    reconcile_stale_executions_postgres,
)
from app_store_review_pipeline.targets import active_targets, load_targets


DEFAULT_SUPERVISOR_STATE = Path.home() / ".local/state/app-store-review-pipeline/runner-supervisor.json"
DEFAULT_SUPERVISOR_CONFIG = Path.home() / ".config/app-store-review-pipeline/runner-supervisor.env"
DEFAULT_SUPERVISOR_LOG = Path.home() / "Library/Logs/app-store-runner-supervisor.log"
DEFAULT_LAUNCH_AGENT = Path.home() / "Library/LaunchAgents/com.sciencia.app-store-runner-supervisor.plist"
DEFAULT_SUPERVISOR_RUNTIME = Path.home() / ".local/share/app-store-review-pipeline-supervisor"
RUNNER_PLIST_GLOB = "actions.runner.XVvvVX-bot-app-store-review-pipeline*.plist"
RECOVERY_WORKFLOW = "app-store-daily-pipeline.yml"
RECOVERY_PRESSURE_MODE = "fixed"


@dataclass(frozen=True)
class SupervisorConfig:
    repository: str = "XVvvVX-bot/app-store-review-pipeline"
    repo_path: Path = Path.home() / "Documents/Sciencia AI/app-store-review-pipeline"
    database_url: str = DEFAULT_DATABASE_URL
    heartbeat_url: str = ""
    github_token: str = ""
    branch: str = "main"
    min_online_runners: int = 4
    outage_seconds: int = 600
    stable_seconds: int = 300
    stale_execution_hours: float = 6
    max_restart_attempts: int = 3
    max_recovery_attempts: int = 3
    max_backlog_apps: int = 10
    max_backlog_attempts: int = 3
    backlog_retry_minutes: int = 30
    auto_recover: bool = True

    @classmethod
    def from_file(cls, path: Path = DEFAULT_SUPERVISOR_CONFIG) -> "SupervisorConfig":
        values = read_env_file(path)
        return cls(
            repository=values.get("GITHUB_REPOSITORY", cls.repository),
            repo_path=Path(values.get("APP_STORE_REPO_PATH", str(cls.repo_path))).expanduser(),
            database_url=values.get("DATABASE_URL", cls.database_url),
            heartbeat_url=values.get("APP_STORE_HEARTBEAT_URL", ""),
            github_token=values.get("GH_TOKEN", ""),
            branch=values.get("GITHUB_BRANCH", cls.branch),
            min_online_runners=int(values.get("MIN_ONLINE_RUNNERS", cls.min_online_runners)),
            outage_seconds=int(values.get("OUTAGE_SECONDS", cls.outage_seconds)),
            stable_seconds=int(values.get("STABLE_SECONDS", cls.stable_seconds)),
            stale_execution_hours=float(values.get("STALE_EXECUTION_HOURS", cls.stale_execution_hours)),
            max_restart_attempts=int(values.get("MAX_RESTART_ATTEMPTS", cls.max_restart_attempts)),
            max_recovery_attempts=int(values.get("MAX_RECOVERY_ATTEMPTS", cls.max_recovery_attempts)),
            max_backlog_apps=int(values.get("MAX_BACKLOG_APPS", cls.max_backlog_apps)),
            max_backlog_attempts=int(values.get("MAX_BACKLOG_ATTEMPTS", cls.max_backlog_attempts)),
            backlog_retry_minutes=int(values.get("BACKLOG_RETRY_MINUTES", cls.backlog_retry_minutes)),
            auto_recover=parse_bool(values.get("AUTO_RECOVER", "true")),
        )


class RunnerSupervisor:
    def __init__(
        self,
        config: SupervisorConfig,
        *,
        state_path: Path = DEFAULT_SUPERVISOR_STATE,
        command_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        now: Callable[[], datetime] | None = None,
        http: Any = requests,
    ) -> None:
        self.config = config
        self.state_path = state_path
        self.command_runner = command_runner
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.http = http

    def run_once(self) -> dict[str, Any]:
        current = self.now().astimezone(timezone.utc).replace(microsecond=0)
        state = load_state(self.state_path)
        health = self.health_snapshot()
        state["last_checked_at"] = isoformat(current)
        state["last_health"] = health

        if not health["healthy"]:
            self.handle_unhealthy(state, health, current)
        else:
            self.handle_healthy(state, health, current)

        write_state(self.state_path, state)
        return {"state": state, "health": health}

    def health_snapshot(self) -> dict[str, Any]:
        postgres_ok = False
        postgres_error = ""
        try:
            with connect_postgres(self.config.database_url) as connection:
                connection.execute("SELECT 1").fetchone()
            postgres_ok = True
        except Exception as exc:
            postgres_error = type(exc).__name__

        runners: list[dict[str, Any]] = []
        github_ok = False
        github_error = ""
        try:
            payload = self.gh_json(["api", f"repos/{self.config.repository}/actions/runners?per_page=100"])
            runners = list(payload.get("runners") or [])
            github_ok = True
        except Exception as exc:
            github_error = type(exc).__name__
        relevant = [
            runner
            for runner in runners
            if "app-store-review-pipeline" in {str(label.get("name")) for label in runner.get("labels") or []}
        ]
        online = [runner for runner in relevant if runner.get("status") == "online"]
        services = self.runner_services()
        loaded_services = sum(1 for service in services if service["loaded"])
        healthy = (
            postgres_ok
            and github_ok
            and len(online) >= self.config.min_online_runners
            and loaded_services >= self.config.min_online_runners
        )
        return {
            "healthy": healthy,
            "postgres_ok": postgres_ok,
            "postgres_error": postgres_error,
            "github_ok": github_ok,
            "github_error": github_error,
            "online_runner_count": len(online),
            "registered_runner_count": len(relevant),
            "loaded_service_count": loaded_services,
            "configured_service_count": len(services),
            "offline_runners": [str(runner.get("name") or "") for runner in relevant if runner.get("status") != "online"],
            "services": services,
        }

    def runner_services(self) -> list[dict[str, Any]]:
        services = []
        for path in sorted((Path.home() / "Library/LaunchAgents").glob(RUNNER_PLIST_GLOB)):
            with path.open("rb") as handle:
                payload = plistlib.load(handle)
            label = str(payload.get("Label") or "")
            result = self.run_command(["launchctl", "print", f"gui/{os.getuid()}/{label}"])
            services.append({"label": label, "plist": str(path), "loaded": result.returncode == 0})
        return services

    def handle_unhealthy(self, state: dict[str, Any], health: dict[str, Any], current: datetime) -> None:
        state["healthy_since"] = None
        if not state.get("unhealthy_since"):
            state["unhealthy_since"] = isoformat(current)
            state["restart_attempts"] = 0
        attempts = int(state.get("restart_attempts") or 0)
        if attempts < self.config.max_restart_attempts:
            restarted = self.restart_unhealthy_services(health, force_offline=attempts >= 2)
            postgres_restart = self.restart_postgres() if health.get("postgres_ok") is False else "not_needed"
            state["restart_attempts"] = attempts + 1
            state["last_restart"] = {
                "at": isoformat(current),
                "services": restarted,
                "postgres": postgres_restart,
            }
        unhealthy_since = parse_time(state.get("unhealthy_since")) or current
        if (current - unhealthy_since).total_seconds() >= self.config.outage_seconds:
            if state.get("phase") not in {"outage", "manual_attention"}:
                state["phase"] = "outage"
                state["incident_id"] = state.get("incident_id") or incident_id(current)
                state["incident_opened_at"] = isoformat(current)
                self.heartbeat("fail", state, health)

    def handle_healthy(self, state: dict[str, Any], health: dict[str, Any], current: datetime) -> None:
        state["restart_attempts"] = 0
        if not state.get("healthy_since"):
            state["healthy_since"] = isoformat(current)
        phase = str(state.get("phase") or "idle")
        if phase in {"recovery_full", "recovery_backlog", "recovery_verify"}:
            self.advance_recovery(state, current)
            return
        if phase == "manual_attention":
            return

        recovery = outage_recovery_status_postgres(
            self.config.database_url,
            source=WEB_CATALOG_SOURCE,
            initialize_schema=False,
        )
        state["last_recovery_status"] = recovery
        needs_recovery = bool(
            phase == "outage"
            or state.get("unhealthy_since")
            or recovery["stale_36h"] > 0
            or recovery["backlogged_scope_count"] > 0
        )
        if not needs_recovery:
            state.update(phase="idle", unhealthy_since=None, incident_id=None, incident_opened_at=None)
            return
        healthy_since = parse_time(state.get("healthy_since")) or current
        if (current - healthy_since).total_seconds() < self.config.stable_seconds:
            state["phase"] = "stabilizing"
            return
        if not self.config.auto_recover:
            state["phase"] = "recovery_ready"
            return
        if self.nonstale_active_daily_runs(current):
            state["phase"] = "waiting_for_active_run"
            return
        pending_phase = str(state.pop("pending_recovery_phase", "") or "full")
        self.start_full_recovery(state, current, phase=pending_phase)

    def start_full_recovery(self, state: dict[str, Any], current: datetime, *, phase: str) -> None:
        attempt_key = f"{phase}_recovery_attempts"
        attempt = int(state.get(attempt_key) or 0) + 1
        if attempt > self.config.max_recovery_attempts:
            self.manual_attention(state, f"{phase}_recovery_attempt_limit", current)
            return
        state[attempt_key] = attempt
        reconcile = reconcile_stale_executions_postgres(
            self.config.database_url,
            source=WEB_CATALOG_SOURCE,
            stale_hours=self.config.stale_execution_hours,
            initialize_schema=False,
        )
        self.cancel_stale_daily_runs(current)
        state["last_reconciliation"] = reconcile
        state["incident_id"] = state.get("incident_id") or incident_id(current)
        token = f"outage:{state['incident_id']}:{phase}"
        run_id = self.dispatch_recovery(token=token, max_parallel=4)
        state.update(
            phase="recovery_full" if phase == "full" else "recovery_verify",
            current_run_id=str(run_id),
            current_dispatch_token=token,
            current_run_started_at=isoformat(current),
            unhealthy_since=None,
        )

    def advance_recovery(self, state: dict[str, Any], current: datetime) -> None:
        run_id = str(state.get("current_run_id") or "")
        if run_id:
            run = self.gh_json(["run", "view", run_id, "--repo", self.config.repository, "--json", "status,conclusion,url"])
            state["current_run"] = run
            if run.get("status") != "completed":
                return
            state["current_run_id"] = None

        phase = str(state.get("phase") or "")
        if phase in {"recovery_full", "recovery_verify"}:
            completed_run_id = str((state.get("current_run") or {}).get("databaseId") or run_id)
            status = outage_recovery_status_postgres(
                self.config.database_url,
                source=WEB_CATALOG_SOURCE,
                github_run_id=completed_run_id,
                initialize_schema=False,
            )
            state["last_recovery_status"] = status
            execution = status.get("execution") or {}
            if not execution_complete(execution):
                if not execution or str(execution.get("status") or "") in {"running", "cancelled"}:
                    self.retry_infrastructure_recovery(state, current, phase=phase)
                    return
                self.manual_attention(state, "full_recovery_did_not_complete_intended_scope", current)
                return
            backlog_apps = unique_backlog_apps(status.get("backlogged_scopes") or [])
            if phase == "recovery_verify":
                if backlog_apps:
                    self.manual_attention(state, "verification_left_backlogged_scopes", current)
                    return
                if not execution_monitor_verified(execution):
                    self.manual_attention(state, "verification_monitor_not_verified", current)
                    return
                self.resolve_recovery(state, current)
                return
            if not backlog_apps:
                if not execution_monitor_verified(execution):
                    self.manual_attention(state, "full_recovery_monitor_not_verified", current)
                    return
                self.resolve_recovery(state, current)
                return
            if len(backlog_apps) > self.config.max_backlog_apps:
                self.manual_attention(state, "backlog_app_limit_exceeded", current)
                return
            state.update(
                phase="recovery_backlog",
                backlog_queue=backlog_apps,
                backlog_attempts={},
                current_run=None,
                current_run_id=None,
            )

        if state.get("phase") == "recovery_backlog":
            self.advance_backlog(state, current)

    def retry_infrastructure_recovery(
        self,
        state: dict[str, Any],
        current: datetime,
        *,
        phase: str,
    ) -> None:
        recovery_phase = "verify" if phase == "recovery_verify" else "full"
        attempt_key = f"{recovery_phase}_recovery_attempts"
        attempts = int(state.get(attempt_key) or 1)
        state[attempt_key] = attempts
        if attempts >= self.config.max_recovery_attempts:
            self.manual_attention(state, f"{recovery_phase}_recovery_attempt_limit", current)
            return
        state.update(
            phase="stabilizing",
            pending_recovery_phase=recovery_phase,
            healthy_since=isoformat(current),
            current_run_id=None,
            current_run=None,
            current_dispatch_token=None,
        )

    def advance_backlog(self, state: dict[str, Any], current: datetime) -> None:
        queue = list(state.get("backlog_queue") or [])
        current_app = str(state.get("current_backlog_app") or "")
        if current_app and not state.get("current_run_id") and state.get("current_run"):
            status = outage_recovery_status_postgres(
                self.config.database_url,
                source=WEB_CATALOG_SOURCE,
                initialize_schema=False,
            )
            remaining = {str(row.get("app_id")) for row in status.get("backlogged_scopes") or []}
            if current_app not in remaining:
                queue = [app_id for app_id in queue if app_id != current_app]
                state["current_backlog_app"] = None
                state["not_before"] = None
            else:
                attempts = dict(state.get("backlog_attempts") or {})
                count = int(attempts.get(current_app) or 0)
                if count >= self.config.max_backlog_attempts:
                    self.manual_attention(state, f"backlog_attempt_limit:{current_app}", current)
                    return
                state["not_before"] = isoformat(current + timedelta(minutes=self.config.backlog_retry_minutes))
            state["backlog_queue"] = queue
            state["current_run"] = None

        if queue and not state.get("current_run_id") and not state.get("current_run"):
            status = outage_recovery_status_postgres(
                self.config.database_url,
                source=WEB_CATALOG_SOURCE,
                initialize_schema=False,
            )
            remaining = {str(row.get("app_id")) for row in status.get("backlogged_scopes") or []}
            queue = [app_id for app_id in queue if app_id in remaining]
            state["backlog_queue"] = queue
            if current_app and current_app not in remaining:
                state["current_backlog_app"] = None
                state["not_before"] = None

        if not queue:
            self.start_full_recovery(state, current, phase="verify")
            return
        not_before = parse_time(state.get("not_before"))
        if not_before and current < not_before:
            return
        if self.nonstale_active_daily_runs(current):
            return
        app_id = str(state.get("current_backlog_app") or queue[0])
        attempts = dict(state.get("backlog_attempts") or {})
        attempt = int(attempts.get(app_id) or 0) + 1
        attempts[app_id] = attempt
        target_offset = target_offset_for_app(self.config.repo_path / DEFAULT_TARGETS, app_id)
        token = f"outage:{state['incident_id']}:backlog:{app_id}:{attempt}"
        run_id = self.dispatch_recovery(
            token=token,
            limit=1,
            target_offset=target_offset,
            max_parallel=1,
            resume_backlog=True,
            time_budget=7200,
        )
        state.update(
            current_backlog_app=app_id,
            backlog_attempts=attempts,
            current_run_id=str(run_id),
            current_dispatch_token=token,
            current_run_started_at=isoformat(current),
            not_before=None,
        )

    def dispatch_recovery(
        self,
        *,
        token: str,
        limit: int = 0,
        target_offset: int = 0,
        max_parallel: int = 4,
        resume_backlog: bool = False,
        time_budget: int = 3600,
    ) -> str:
        fields = {
            "limit": str(limit),
            "target_offset": str(target_offset),
            "experiment_group": token,
            "max_parallel": str(max_parallel),
            "max_pages_per_app_country": "0",
            "pressure_ramp_mode": RECOVERY_PRESSURE_MODE,
            "start_page": "1",
            "resume_backlogged_scopes": str(resume_backlog).lower(),
            "review_limit": "20",
            "request_delay_seconds": "10",
            "request_delay_jitter_seconds": "5",
            "web_429_retries": "2",
            "web_429_retry_seconds": "300",
            "web_429_backoff_multiplier": "1.5",
            "web_429_retry_jitter_seconds": "60",
            "web_time_budget_seconds": str(time_budget),
            "web_scope_time_budget_seconds": str(time_budget),
            "web_429_cooldown_minutes": "0",
            "web_429_circuit_breaker_lookback_minutes": "720",
            "web_429_circuit_breaker_min_pages": "4",
            "web_429_circuit_breaker_max_rate": "0.5",
            "outage_recovery": "true",
        }
        args = ["workflow", "run", RECOVERY_WORKFLOW, "--repo", self.config.repository, "--ref", self.config.branch]
        for key, value in fields.items():
            args.extend(["-f", f"{key}={value}"])
        self.gh_json(args, expect_json=False)
        expected_title = f"Outage recovery {token}"
        for _ in range(10):
            payload = self.gh_json(
                [
                    "run",
                    "list",
                    "--repo",
                    self.config.repository,
                    "--workflow",
                    RECOVERY_WORKFLOW,
                    "--event",
                    "workflow_dispatch",
                    "--limit",
                    "20",
                    "--json",
                    "databaseId,displayTitle,status,createdAt",
                ]
            )
            match = next((row for row in payload if row.get("displayTitle") == expected_title), None)
            if match:
                return str(match["databaseId"])
            time.sleep(2)
        raise RuntimeError(f"Dispatched recovery run was not visible: {expected_title}")

    def restart_unhealthy_services(self, health: dict[str, Any], *, force_offline: bool) -> list[str]:
        restarted = []
        offline = set(health.get("offline_runners") or [])
        for service in health.get("services") or []:
            label = str(service["label"])
            runner_name = label.rsplit(".", 1)[-1]
            if service.get("loaded") and not (force_offline and runner_name in offline):
                continue
            domain = f"gui/{os.getuid()}"
            if not service.get("loaded"):
                self.run_command(["launchctl", "bootstrap", domain, str(service["plist"])])
            self.run_command(["launchctl", "kickstart", "-k", f"{domain}/{label}"])
            restarted.append(label)
        return restarted

    def restart_postgres(self) -> str:
        domain = f"gui/{os.getuid()}"
        label = "homebrew.mxcl.postgresql@16"
        loaded = self.run_command(["launchctl", "print", f"{domain}/{label}"]).returncode == 0
        if loaded:
            result = self.run_command(["launchctl", "kickstart", "-k", f"{domain}/{label}"])
            return "kickstarted" if result.returncode == 0 else "kickstart_failed"
        result = self.run_command(["brew", "services", "start", "postgresql@16"])
        return "started" if result.returncode == 0 else "start_failed"

    def cancel_stale_daily_runs(self, current: datetime) -> list[str]:
        cancelled = []
        for run in self.list_active_daily_runs():
            created = parse_time(run.get("createdAt"))
            if created and (current - created).total_seconds() >= self.config.stale_execution_hours * 3600:
                try:
                    self.gh_json(
                        ["run", "cancel", str(run["databaseId"]), "--repo", self.config.repository],
                        expect_json=False,
                    )
                except RuntimeError:
                    continue
                cancelled.append(str(run["databaseId"]))
        return cancelled

    def nonstale_active_daily_runs(self, current: datetime) -> list[dict[str, Any]]:
        output = []
        for run in self.list_active_daily_runs():
            created = parse_time(run.get("createdAt"))
            if created is None or (current - created).total_seconds() < self.config.stale_execution_hours * 3600:
                output.append(run)
        return output

    def list_active_daily_runs(self) -> list[dict[str, Any]]:
        payload = self.gh_json(
            [
                "run",
                "list",
                "--repo",
                self.config.repository,
                "--workflow",
                RECOVERY_WORKFLOW,
                "--limit",
                "30",
                "--json",
                "databaseId,status,conclusion,createdAt,displayTitle,event",
            ]
        )
        return [row for row in payload if row.get("status") in {"queued", "in_progress", "requested", "waiting"}]

    def resolve_recovery(self, state: dict[str, Any], current: datetime) -> None:
        state.update(
            phase="idle",
            resolved_at=isoformat(current),
            unhealthy_since=None,
            healthy_since=isoformat(current),
            current_run_id=None,
            current_backlog_app=None,
            backlog_queue=[],
            not_before=None,
        )

    def manual_attention(self, state: dict[str, Any], reason: str, current: datetime) -> None:
        state.update(phase="manual_attention", manual_attention_reason=reason, manual_attention_at=isoformat(current))
        self.heartbeat("log", state, state.get("last_health") or {})

    def heartbeat(self, action: str, state: dict[str, Any], health: dict[str, Any]) -> None:
        if not self.config.heartbeat_url:
            return
        base = self.config.heartbeat_url.rstrip("/")
        suffix = "/fail" if action == "fail" else "/log" if action == "log" else ""
        body = json.dumps(
            {
                "incident_id": state.get("incident_id"),
                "phase": state.get("phase"),
                "health": health,
                "repository": self.config.repository,
            },
            default=str,
        )
        try:
            self.http.post(f"{base}{suffix}", data=body, timeout=20)
        except Exception:
            pass

    def gh_json(self, args: list[str], *, expect_json: bool = True) -> Any:
        result = self.run_command(["gh", *args])
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or f"gh {' '.join(args)} failed")
        if not expect_json:
            return None
        return json.loads(result.stdout or "null")

    def run_command(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        env = None
        if args and args[0] == "gh" and self.config.github_token:
            env = {**os.environ, "GH_TOKEN": self.config.github_token}
        return self.command_runner(
            args,
            capture_output=True,
            text=True,
            cwd=self.config.repo_path,
            env=env,
        )


def execution_complete(execution: dict[str, Any]) -> bool:
    intended = int(execution.get("intended_scope_count") or 0)
    return (
        intended > 0
        and int(execution.get("completed_scope_count") or 0) == intended
        and int(execution.get("hard_failure_scope_count") or 0) == 0
    )


def execution_monitor_verified(execution: dict[str, Any]) -> bool:
    return str(execution.get("status") or "") in {"healthy", "degraded"}


def unique_backlog_apps(scopes: list[dict[str, Any]]) -> list[str]:
    return list(dict.fromkeys(str(scope.get("app_id")) for scope in scopes if scope.get("app_id")))


def target_offset_for_app(targets_path: Path, app_id: str) -> int:
    targets = active_targets(load_targets(targets_path))
    for offset, target in enumerate(targets):
        if target.apple_app_id == str(app_id):
            return offset
    raise ValueError(f"Active target app not found: {app_id}")


def install_runner_supervisor(
    *,
    repo_path: Path,
    config_path: Path = DEFAULT_SUPERVISOR_CONFIG,
    launch_agent_path: Path = DEFAULT_LAUNCH_AGENT,
    python_path: Path | None = None,
    runtime_path: Path = DEFAULT_SUPERVISOR_RUNTIME,
) -> dict[str, Any]:
    repo_path = repo_path.expanduser().resolve()
    source_python = (python_path or repo_path / ".venv/bin/python").expanduser().absolute()
    if not source_python.exists():
        raise FileNotFoundError(f"Supervisor Python does not exist: {source_python}")
    runtime = prepare_supervisor_runtime(
        repo_path=repo_path,
        source_python=source_python,
        runtime_path=runtime_path,
    )
    runtime_path = Path(runtime["runtime_path"])
    runtime_python = Path(runtime["python_path"])
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if not config_path.exists():
        config_path.write_text(
            "\n".join(
                [
                    "GITHUB_REPOSITORY=XVvvVX-bot/app-store-review-pipeline",
                    f"APP_STORE_REPO_PATH={runtime_path}",
                    "DATABASE_URL=postgresql:///app_store_reviews",
                    "APP_STORE_HEARTBEAT_URL=",
                    "GH_TOKEN=",
                    "GITHUB_BRANCH=main",
                    "MIN_ONLINE_RUNNERS=4",
                    "OUTAGE_SECONDS=600",
                    "STABLE_SECONDS=300",
                    "STALE_EXECUTION_HOURS=6",
                    "MAX_RESTART_ATTEMPTS=3",
                    "MAX_RECOVERY_ATTEMPTS=3",
                    "MAX_BACKLOG_APPS=10",
                    "MAX_BACKLOG_ATTEMPTS=3",
                    "BACKLOG_RETRY_MINUTES=30",
                    "AUTO_RECOVER=true",
                    "",
                ]
            ),
            encoding="utf-8",
        )
    else:
        update_env_value(config_path, "APP_STORE_REPO_PATH", str(runtime_path))
    config_values = read_env_file(config_path)
    if not config_values.get("GH_TOKEN"):
        token_result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
        )
        token = token_result.stdout.strip() if token_result.returncode == 0 else ""
        if not token:
            raise RuntimeError(
                "A GitHub token is required for the launchd supervisor; run gh auth login or set GH_TOKEN in the local config."
            )
        update_env_value(config_path, "GH_TOKEN", token)
    config_path.chmod(0o600)
    DEFAULT_SUPERVISOR_LOG.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "Label": "com.sciencia.app-store-runner-supervisor",
        "ProgramArguments": [
            str(runtime_python),
            str(runtime_path / "app_store_pipeline.py"),
            "runner-supervisor",
            "--config",
            str(config_path),
            "--state",
            str(DEFAULT_SUPERVISOR_STATE),
        ],
        "WorkingDirectory": str(runtime_path),
        "EnvironmentVariables": {
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "PYTHONUNBUFFERED": "1",
        },
        "RunAtLoad": True,
        "StartInterval": 60,
        "ThrottleInterval": 30,
        "ProcessType": "Interactive",
        "SessionCreate": True,
        "UserName": pwd.getpwuid(os.getuid()).pw_name,
        "StandardOutPath": str(DEFAULT_SUPERVISOR_LOG),
        "StandardErrorPath": str(DEFAULT_SUPERVISOR_LOG),
    }
    launch_agent_path.parent.mkdir(parents=True, exist_ok=True)
    with launch_agent_path.open("wb") as handle:
        plistlib.dump(payload, handle, sort_keys=True)
    domain = f"gui/{os.getuid()}"
    label = str(payload["Label"])
    subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], capture_output=True, text=True)
    bootstrap = subprocess.run(
        ["launchctl", "bootstrap", domain, str(launch_agent_path)],
        capture_output=True,
        text=True,
    )
    if bootstrap.returncode != 0:
        raise RuntimeError(bootstrap.stderr.strip() or "launchctl bootstrap failed")
    return {
        "installed": True,
        "label": label,
        "config_path": str(config_path),
        "launch_agent_path": str(launch_agent_path),
        "state_path": str(DEFAULT_SUPERVISOR_STATE),
        "log_path": str(DEFAULT_SUPERVISOR_LOG),
        **runtime,
    }


def prepare_supervisor_runtime(
    *,
    repo_path: Path,
    source_python: Path,
    runtime_path: Path = DEFAULT_SUPERVISOR_RUNTIME,
) -> dict[str, str]:
    runtime_path = runtime_path.expanduser().absolute()
    required = [
        repo_path / "app_store_pipeline.py",
        repo_path / "requirements.lock",
        repo_path / "app_store_review_pipeline",
        repo_path / DEFAULT_TARGETS,
    ]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise FileNotFoundError(f"Supervisor runtime sources are missing: {', '.join(missing)}")

    runtime_path.mkdir(parents=True, exist_ok=True)
    shutil.copy2(repo_path / "app_store_pipeline.py", runtime_path / "app_store_pipeline.py")
    shutil.copy2(repo_path / "requirements.lock", runtime_path / "requirements.lock")
    shutil.copytree(
        repo_path / "app_store_review_pipeline",
        runtime_path / "app_store_review_pipeline",
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
    )
    runtime_targets = runtime_path / DEFAULT_TARGETS
    runtime_targets.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(repo_path / DEFAULT_TARGETS, runtime_targets)

    runtime_python = runtime_path / ".venv/bin/python"
    if not runtime_python.exists():
        created = subprocess.run(
            [str(source_python), "-m", "venv", str(runtime_path / ".venv")],
            capture_output=True,
            text=True,
        )
        if created.returncode != 0:
            raise RuntimeError(created.stderr.strip() or "Supervisor virtualenv creation failed")
    installed = subprocess.run(
        [
            str(runtime_python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "-r",
            str(runtime_path / "requirements.lock"),
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "PIP_CONFIG_FILE": "/dev/null", "PIP_USER": "false"},
    )
    if installed.returncode != 0:
        raise RuntimeError(installed.stderr.strip() or "Supervisor dependency installation failed")
    return {
        "runtime_path": str(runtime_path),
        "python_path": str(runtime_python),
    }


def read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def update_env_value(path: Path, key: str, value: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    replacement = f"{key}={value}"
    updated = []
    replaced = False
    for line in lines:
        if line.strip().startswith(f"{key}="):
            updated.append(replacement)
            replaced = True
        else:
            updated.append(line)
    if not replaced:
        updated.append(replacement)
    path.write_text("\n".join(updated) + "\n", encoding="utf-8")


def parse_bool(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"phase": "idle", "restart_attempts": 0, "backlog_queue": [], "backlog_attempts": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"phase": "idle", "restart_attempts": 0, "backlog_queue": [], "backlog_attempts": {}}


def write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    temporary.replace(path)


def parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def isoformat(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def incident_id(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
