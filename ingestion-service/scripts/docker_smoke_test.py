#!/usr/bin/env python3
"""Read-only smoke test for the running ingestion-service Docker stack."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
POLL_INTERVAL_SECONDS = 5
STARTUP_TIMEOUT_SECONDS = int(os.getenv("SMOKE_STARTUP_TIMEOUT_SECONDS", "900"))


class SmokeTestFailure(RuntimeError):
    """Raised when a smoke-test assertion fails."""


def compose(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", *arguments],
        cwd=PROJECT_ROOT,
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def compose_configuration() -> dict[str, Any]:
    result = compose("config", "--format", "json")
    payload = json.loads(result.stdout)
    if not isinstance(payload, dict):
        raise SmokeTestFailure("Docker Compose config must be a JSON object")
    return payload


def published_port(configuration: dict[str, Any], service: str, default: int) -> int:
    services = configuration.get("services")
    if not isinstance(services, dict) or service not in services:
        raise SmokeTestFailure(f"Docker Compose service {service!r} is missing")
    service_config = services[service]
    if not isinstance(service_config, dict):
        raise SmokeTestFailure(f"Docker Compose service {service!r} is invalid")
    ports = service_config.get("ports", [])
    if not ports:
        return default
    first_port = ports[0]
    if not isinstance(first_port, dict) or "published" not in first_port:
        return default
    return int(first_port["published"])


def parse_response_body(raw_body: str) -> dict[str, Any] | list[Any] | str:
    if not raw_body:
        return {}
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        return raw_body
    return payload if isinstance(payload, dict) or isinstance(payload, list) else raw_body


def request(
    url: str,
    *,
    api_key: str = "",
    timeout: int = 30,
) -> tuple[int, dict[str, Any] | list[Any] | str]:
    headers = {"accept": "application/json"}
    if api_key:
        headers["x-internal-api-key"] = api_key
    http_request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(http_request, timeout=timeout) as response:
            raw_body = response.read().decode("utf-8", errors="replace")
            payload = parse_response_body(raw_body)
            return response.status, payload
    except urllib.error.HTTPError as exc:
        raw_body = exc.read().decode("utf-8", errors="replace")
        return exc.code, parse_response_body(raw_body)
    except OSError as exc:
        reason = getattr(exc, "reason", exc)
        raise SmokeTestFailure(f"Cannot connect to {url}: {reason}") from exc


def wait_for_healthy(url: str, label: str) -> dict[str, Any]:
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    last_message = ""
    while time.monotonic() < deadline:
        try:
            status, body = request(url)
            rendered = json.dumps(body, ensure_ascii=False, sort_keys=True)
            message = f"{label}: HTTP {status} {rendered}"
            if message != last_message:
                print(message, flush=True)
                last_message = message
            if status == 200 and isinstance(body, dict):
                return body
        except SmokeTestFailure as exc:
            message = str(exc)
            if message != last_message:
                print(message, flush=True)
                last_message = message
        fail_if_compose_role_crashed()
        time.sleep(POLL_INTERVAL_SECONDS)
    raise SmokeTestFailure(
        f"{label} did not become healthy within {STARTUP_TIMEOUT_SECONDS} seconds"
    )


def compose_ps_rows() -> list[dict[str, Any]]:
    raw_output = compose("ps", "-a", "--format", "json", check=False).stdout.strip()
    if not raw_output:
        return []
    try:
        payload = json.loads(raw_output)
    except json.JSONDecodeError:
        rows: list[dict[str, Any]] = []
        for line in raw_output.splitlines():
            value = json.loads(line)
            if isinstance(value, dict):
                rows.append(value)
        return rows
    if isinstance(payload, dict):
        return [payload]
    if isinstance(payload, list) and all(isinstance(value, dict) for value in payload):
        return payload
    raise SmokeTestFailure("Docker Compose ps returned an unexpected JSON structure")


def fail_if_compose_role_crashed() -> None:
    rows = compose_ps_rows()
    by_service = {str(row.get("Service", "")): row for row in rows}
    for service in ("ingestion-api", "ingestion-worker", "ingestion-scheduler"):
        row = by_service.get(service)
        if row is None:
            continue
        state = str(row.get("State", "")).casefold()
        health = str(row.get("Health", "")).casefold()
        if state in {"dead", "exited", "restarting"} or health == "unhealthy":
            raise SmokeTestFailure(
                f"Docker Compose role {service!r} failed: "
                f"state={state or 'unknown'}, health={health or 'none'}, "
                f"exit_code={row.get('ExitCode')}"
            )

    migrate = by_service.get("migrate")
    if migrate is None or str(migrate.get("State", "")).casefold() != "exited":
        return
    try:
        exit_code = int(migrate.get("ExitCode", 1))
    except (TypeError, ValueError):
        exit_code = 1
    if exit_code != 0:
        raise SmokeTestFailure(f"Migration container failed with exit code {exit_code}")


def wait_for_compose_roles() -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    last_report = ""
    while time.monotonic() < deadline:
        rows = compose_ps_rows()
        by_service = {str(row.get("Service", "")): row for row in rows}
        report = json.dumps(
            {
                service: {
                    "state": row.get("State"),
                    "health": row.get("Health"),
                    "exit_code": row.get("ExitCode"),
                }
                for service, row in sorted(by_service.items())
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        if report != last_report:
            print(f"Compose roles: {report}", flush=True)
            last_report = report
        fail_if_compose_role_crashed()

        running_roles = ("ingestion-api", "ingestion-worker", "ingestion-scheduler")
        roles_ready = all(
            service in by_service
            and str(by_service[service].get("State", "")).casefold() == "running"
            and str(by_service[service].get("Health", "")).casefold() == "healthy"
            for service in running_roles
        )
        migrate = by_service.get("migrate")
        try:
            migration_exit_code = int(migrate.get("ExitCode", 1)) if migrate else 1
        except (TypeError, ValueError):
            migration_exit_code = 1
        migration_ready = (
            migrate is not None
            and str(migrate.get("State", "")).casefold() == "exited"
            and migration_exit_code == 0
        )
        if roles_ready and migration_ready:
            return
        time.sleep(POLL_INTERVAL_SECONDS)
    raise SmokeTestFailure(
        "Docker Compose API, worker, scheduler and migration did not become healthy in time"
    )


def verify_runtime_dependencies() -> None:
    result = compose(
        "exec",
        "-T",
        "ingestion-api",
        "python",
        "-c",
        "import docx, pypdf, transformers, yaml; print('runtime dependencies: ok')",
        check=False,
    )
    output = result.stdout.strip()
    if output:
        print(output, flush=True)
    if result.returncode != 0:
        raise SmokeTestFailure("PDF, DOCX, tokenizer or YAML runtime dependency import failed")


def diagnostic_logs() -> None:
    print("\n=== Docker Compose diagnostics ===", flush=True)
    for arguments in (
        ("ps", "-a"),
        (
            "logs",
            "--no-color",
            "--tail",
            "200",
            "migrate",
            "ingestion-api",
            "ingestion-worker",
            "ingestion-scheduler",
        ),
    ):
        try:
            print(compose(*arguments, check=False).stdout, flush=True)
        except OSError as exc:
            print(f"Unable to collect {' '.join(arguments)}: {exc}", flush=True)


def run() -> None:
    configuration = compose_configuration()
    services = configuration["services"]
    api_config = services["ingestion-api"]
    environment = api_config.get("environment", {})
    raw_api_key = environment.get("API_INTERNAL_KEY", "")
    api_key = "" if raw_api_key is None else str(raw_api_key)
    api_port = published_port(configuration, "ingestion-api", 8090)
    api_base_url = os.getenv("SMOKE_API_BASE_URL", f"http://127.0.0.1:{api_port}")

    print("=== Docker Compose services ===", flush=True)
    print(compose("ps", "-a", check=False).stdout, flush=True)

    live = wait_for_healthy(f"{api_base_url}/health/live", "Liveness")
    if live.get("checks", {}).get("process") is not True:
        raise SmokeTestFailure(f"Unexpected liveness response: {live}")

    ready = wait_for_healthy(f"{api_base_url}/health/ready", "Readiness")
    checks = ready.get("checks")
    expected_checks = {"document_root", "postgres", "knowledge_index"}
    if not isinstance(checks, dict) or not expected_checks.issubset(checks):
        raise SmokeTestFailure(f"Readiness response is missing checks: {ready}")
    if not all(checks.get(name) is True for name in expected_checks):
        raise SmokeTestFailure(f"Readiness checks are not all true: {ready}")

    wait_for_compose_roles()
    verify_runtime_dependencies()

    source_status, sources = request(
        f"{api_base_url}/api/v1/sources",
        api_key=api_key,
    )
    if source_status != 200 or not isinstance(sources, list) or not sources:
        raise SmokeTestFailure(f"Source lookup failed: HTTP {source_status} {sources}")
    default_source = sources[0]
    if not isinstance(default_source, dict):
        raise SmokeTestFailure(f"Source response is invalid: {default_source}")
    if default_source.get("source_type") != "local_folder":
        raise SmokeTestFailure(f"Unexpected default source: {default_source}")
    extensions = set(default_source.get("allowed_extensions", []))
    if not {".pdf", ".docx", ".txt", ".md"}.issubset(extensions):
        raise SmokeTestFailure(f"Default source is missing supported extensions: {default_source}")
    print(
        "Default source: " + json.dumps(default_source, ensure_ascii=False, sort_keys=True),
        flush=True,
    )

    metrics_status, metrics = request(f"{api_base_url}/metrics")
    if (
        metrics_status != 200
        or not isinstance(metrics, str)
        or "ingestion_http_requests_total" not in metrics
    ):
        raise SmokeTestFailure(f"Metrics endpoint failed: HTTP {metrics_status}")

    print("\nSMOKE TEST PASSED (read-only; no source scan was triggered)", flush=True)


def main() -> int:
    try:
        run()
    except (SmokeTestFailure, OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        print(f"\nSMOKE TEST FAILED: {exc}", file=sys.stderr, flush=True)
        diagnostic_logs()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
