#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


PROVIDERS = [
    {
        "name": "42matters",
        "secret_env": "APP_STORE_42MATTERS_TOKEN",
        "command": "compare-42matters",
        "token_flag": "--access-token",
        "env_value": "APP_STORE_42MATTERS_TOKEN",
        "args": [
            ("--provider-days", "provider_days"),
            ("--provider-page-limit", "provider_page_limit"),
            ("--provider-request-limit", "provider_42matters_request_limit"),
            ("--provider-request-delay-seconds", "provider_42matters_request_delay_seconds"),
        ],
    },
    {
        "name": "apptweak",
        "secret_env": "APP_STORE_APPTWEAK_TOKEN",
        "command": "compare-apptweak",
        "token_flag": "--api-token",
        "env_value": "APP_STORE_APPTWEAK_TOKEN",
        "args": [
            ("--provider-page-limit", "provider_page_limit"),
            ("--provider-request-limit", "provider_large_request_limit"),
            ("--provider-request-delay-seconds", "provider_request_delay_seconds"),
        ],
    },
    {
        "name": "appfigures",
        "secret_env": "APP_STORE_APPFIGURES_TOKEN",
        "command": "compare-appfigures",
        "token_flag": "--access-token",
        "env_value": "APP_STORE_APPFIGURES_TOKEN",
        "args": [
            ("--provider-page-limit", "provider_page_limit"),
            ("--provider-request-limit", "provider_large_request_limit"),
            ("--provider-request-delay-seconds", "provider_request_delay_seconds"),
        ],
    },
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run all configured licensed-provider comparison POCs.")
    parser.add_argument("--output-dir", type=Path, default=Path("data/reports/provider_matrix"))
    parser.add_argument("--limit", default="10")
    parser.add_argument("--provider-days", default="30")
    parser.add_argument("--provider-page-limit", default="2")
    parser.add_argument("--provider-42matters-request-limit", default="100")
    parser.add_argument("--provider-large-request-limit", default="500")
    parser.add_argument("--provider-42matters-request-delay-seconds", default="0.4")
    parser.add_argument("--provider-request-delay-seconds", default="1")
    parser.add_argument("--rss-request-delay-seconds", default="0.5")
    parser.add_argument("--timeout-seconds", default="20")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    matrix: dict[str, Any] = {
        "generated_at_epoch_seconds": time.time(),
        "providers": [],
        "configured_provider_count": 0,
        "successful_provider_count": 0,
        "failed_provider_count": 0,
        "missing_secret_provider_count": 0,
        "settings": vars(args) | {"output_dir": str(args.output_dir)},
    }

    for provider in PROVIDERS:
        entry = run_provider(provider, args)
        matrix["providers"].append(entry)
    matrix["configured_provider_count"] = sum(1 for row in matrix["providers"] if row["configured"])
    matrix["successful_provider_count"] = sum(1 for row in matrix["providers"] if row["status"] == "success")
    matrix["failed_provider_count"] = sum(1 for row in matrix["providers"] if row["status"] == "failed")
    matrix["missing_secret_provider_count"] = sum(1 for row in matrix["providers"] if row["status"] == "missing_secret")
    matrix["source_decision"] = build_source_decision(matrix)

    summary_path = args.output_dir / "provider_matrix_summary.json"
    report_path = args.output_dir / "provider_matrix_report.md"
    matrix["markdown_report_path"] = str(report_path)
    summary_path.write_text(json.dumps(matrix, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report_path.write_text(render_markdown_report(matrix), encoding="utf-8")
    print(json.dumps({"summary_path": str(summary_path), **matrix}, indent=2, sort_keys=True))
    return 1 if matrix["failed_provider_count"] else 0


def run_provider(provider: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    token = os.environ.get(provider["env_value"], "")
    entry: dict[str, Any] = {
        "provider": provider["name"],
        "secret_env": provider["secret_env"],
        "configured": bool(token),
        "status": "missing_secret" if not token else "pending",
        "returncode": None,
        "duration_seconds": None,
        "stdout_path": None,
        "stderr_path": None,
    }
    if not token:
        return entry

    command = [
        sys.executable,
        "app_store_pipeline.py",
        provider["command"],
        "--limit",
        args.limit,
        "--timeout-seconds",
        args.timeout_seconds,
        "--rss-request-delay-seconds",
        args.rss_request_delay_seconds,
        provider["token_flag"],
        token,
    ]
    for flag, attr in provider["args"]:
        command.extend([flag, str(getattr(args, attr))])

    started = time.monotonic()
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    entry["duration_seconds"] = time.monotonic() - started
    entry["returncode"] = completed.returncode
    entry["status"] = "success" if completed.returncode == 0 else "failed"
    stdout_path = args.output_dir / f"{provider['name']}_stdout.json"
    stderr_path = args.output_dir / f"{provider['name']}_stderr.txt"
    stdout_path.write_text(completed.stdout, encoding="utf-8")
    stderr_path.write_text(completed.stderr, encoding="utf-8")
    entry["stdout_path"] = str(stdout_path)
    entry["stderr_path"] = str(stderr_path)
    try:
        parsed = json.loads(completed.stdout)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        entry["comparison_report_path"] = parsed.get("output")
        comparison = parsed.get("comparison")
        if isinstance(comparison, dict):
            entry["candidate_passes_replacement_gate"] = comparison.get("candidate_passes_replacement_gate")
            entry["candidate_passes_same_order_stability_gate"] = comparison.get(
                "candidate_passes_same_order_stability_gate"
            )
            entry["provider_to_rss_review_ratio"] = comparison.get("provider_to_rss_review_ratio")
            entry["provider_all_pages_ok"] = comparison.get("provider_all_pages_ok")
            entry["provider_volume_gap_likely_configuration_limited"] = comparison.get(
                "provider_volume_gap_likely_configuration_limited"
            )
            entry["provider_additional_pages_per_row_needed_for_rss_parity"] = comparison.get(
                "provider_additional_pages_per_row_needed_for_rss_parity"
            )
            entry["provider_reported_total_reviews"] = comparison.get("provider_reported_total_reviews")
            entry["provider_reported_total_reviews_at_or_above_rss"] = comparison.get(
                "provider_reported_total_reviews_at_or_above_rss"
            )
    return entry


def build_source_decision(matrix: dict[str, Any]) -> dict[str, Any]:
    providers = matrix.get("providers") or []
    successful = [row for row in providers if row.get("status") == "success"]
    configured = [row for row in providers if row.get("configured")]
    replacement_candidates = [
        row for row in successful if row.get("candidate_passes_replacement_gate") is True
    ]
    if replacement_candidates:
        winner = max(replacement_candidates, key=lambda row: float(row.get("provider_to_rss_review_ratio") or 0))
        return {
            "status": "replacement_candidate_found",
            "selected_provider": winner.get("provider"),
            "replacement_candidate_count": len(replacement_candidates),
            "recommended_next_action": (
                "Repeat the winning provider comparison on a larger target window, then implement a provider "
                "ingestion mode only after contract and refresh-cadence review."
            ),
        }
    if not configured:
        return {
            "status": "needs_provider_secret",
            "selected_provider": None,
            "missing_secret_envs": [row.get("secret_env") for row in providers if row.get("status") == "missing_secret"],
            "recommended_next_action": (
                "Configure one licensed-provider token secret, then rerun App Store Provider Matrix Compare."
            ),
        }
    failed = [row for row in providers if row.get("status") == "failed"]
    if configured and not successful:
        return {
            "status": "configured_provider_runs_failed",
            "selected_provider": None,
            "failed_providers": [row.get("provider") for row in failed],
            "recommended_next_action": "Inspect provider stdout/stderr artifacts and fix authentication or API usage.",
        }
    config_limited = [
        row
        for row in successful
        if row.get("provider_volume_gap_likely_configuration_limited") is True
        or int(row.get("provider_additional_pages_per_row_needed_for_rss_parity") or 0) > 0
    ]
    if config_limited:
        best = max(config_limited, key=lambda row: float(row.get("provider_to_rss_review_ratio") or 0))
        return {
            "status": "needs_deeper_provider_run",
            "selected_provider": best.get("provider"),
            "recommended_next_action": (
                "Rerun the selected provider with a higher provider_page_limit before rejecting it as too shallow."
            ),
        }
    same_order = [
        row for row in successful if row.get("candidate_passes_same_order_stability_gate") is True
    ]
    if same_order:
        best = max(same_order, key=lambda row: float(row.get("provider_to_rss_review_ratio") or 0))
        return {
            "status": "same_order_but_not_replacement",
            "selected_provider": best.get("provider"),
            "recommended_next_action": (
                "Keep the provider as a possible supplement, but do not replace RSS without higher volume or inventory evidence."
            ),
        }
    return {
        "status": "no_provider_met_gate",
        "selected_provider": None,
        "recommended_next_action": (
            "Do not replace RSS from this run; evaluate another provider, a larger plan tier, or another source category."
        ),
    }


def render_markdown_report(matrix: dict[str, Any]) -> str:
    decision = matrix.get("source_decision") or {}
    lines = [
        "# App Store Provider Matrix Report",
        "",
        f"Decision: **{decision.get('status', 'unknown')}**",
        "",
        f"Selected provider: `{decision.get('selected_provider') or 'none'}`",
        "",
        f"Recommended next action: {decision.get('recommended_next_action') or 'Review provider results.'}",
        "",
        "## Summary",
        "",
        f"- Configured providers: `{matrix.get('configured_provider_count', 0)}`",
        f"- Successful providers: `{matrix.get('successful_provider_count', 0)}`",
        f"- Failed providers: `{matrix.get('failed_provider_count', 0)}`",
        f"- Missing-secret providers: `{matrix.get('missing_secret_provider_count', 0)}`",
        "",
        "## Provider Results",
        "",
        "| Provider | Status | Configured | Replacement Gate | RSS Ratio | Pages OK | Gap Config-Limited | Report |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for provider in matrix.get("providers") or []:
        lines.append(
            "| {provider} | {status} | {configured} | {replacement} | {ratio} | {pages_ok} | {gap} | {report} |".format(
                provider=provider.get("provider"),
                status=provider.get("status"),
                configured="yes" if provider.get("configured") else "no",
                replacement=bool_label(provider.get("candidate_passes_replacement_gate")),
                ratio=format_ratio(provider.get("provider_to_rss_review_ratio")),
                pages_ok=bool_label(provider.get("provider_all_pages_ok")),
                gap=bool_label(provider.get("provider_volume_gap_likely_configuration_limited")),
                report=f"`{provider.get('comparison_report_path')}`" if provider.get("comparison_report_path") else "",
            )
        )
    missing = [
        provider.get("secret_env")
        for provider in matrix.get("providers") or []
        if provider.get("status") == "missing_secret"
    ]
    if missing:
        lines.extend(
            [
                "",
                "## Missing Secrets",
                "",
                *[f"- `{secret}`" for secret in missing],
            ]
        )
    failed = [
        provider
        for provider in matrix.get("providers") or []
        if provider.get("status") == "failed"
    ]
    if failed:
        lines.extend(["", "## Failed Providers", ""])
        for provider in failed:
            lines.append(
                f"- `{provider.get('provider')}` returned `{provider.get('returncode')}`. "
                f"Stdout: `{provider.get('stdout_path')}`. Stderr: `{provider.get('stderr_path')}`."
            )
    lines.extend(
        [
            "",
            "## Decision Status Meaning",
            "",
            "- `replacement_candidate_found`: at least one provider beat RSS with clean pages.",
            "- `needs_deeper_provider_run`: provider inventory likely exists, but the POC page cap was too shallow.",
            "- `same_order_but_not_replacement`: provider volume is useful, but not enough to replace RSS.",
            "- `needs_provider_secret`: no licensed-provider token is configured yet.",
            "- `configured_provider_runs_failed`: configured provider commands failed.",
            "- `no_provider_met_gate`: do not replace RSS from this matrix run.",
            "",
        ]
    )
    return "\n".join(lines)


def bool_label(value: Any) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return ""


def format_ratio(value: Any) -> str:
    if value is None:
        return ""
    try:
        return f"{float(value):.3f}"
    except (TypeError, ValueError):
        return str(value)


if __name__ == "__main__":
    raise SystemExit(main())
