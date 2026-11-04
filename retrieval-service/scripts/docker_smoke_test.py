#!/usr/bin/env python3
"""Read-only smoke test for the running retrieval-service container."""

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
POLL_INTERVAL_SECONDS = 3
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
    payload = json.loads(compose("config", "--format", "json").stdout)
    if not isinstance(payload, dict):
        raise SmokeTestFailure("Docker Compose config must be a JSON object")
    return payload


def published_port(configuration: dict[str, Any], service: str, default: int) -> int:
    services = configuration.get("services")
    if not isinstance(services, dict):
        return default
    service_config = services.get(service)
    if not isinstance(service_config, dict):
        return default
    ports = service_config.get("ports", [])
    if not ports or not isinstance(ports[0], dict):
        return default
    return int(ports[0].get("published", default))


def request(
    url: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 60,
) -> tuple[int, dict[str, Any] | str]:
    encoded = None
    resolved_headers = {"accept": "application/json", **(headers or {})}
    if body is not None:
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        resolved_headers["content-type"] = "application/json"
    http_request = urllib.request.Request(
        url,
        data=encoded,
        headers=resolved_headers,
        method=method,
    )
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(http_request, timeout=timeout) as response:
            raw_body = response.read().decode("utf-8", errors="replace")
            return response.status, _parse_body(raw_body)
    except urllib.error.HTTPError as exc:
        raw_body = exc.read().decode("utf-8", errors="replace")
        return exc.code, _parse_body(raw_body)
    except OSError as exc:
        reason = getattr(exc, "reason", exc)
        raise SmokeTestFailure(f"Cannot connect to {url}: {reason}") from exc


def _parse_body(raw_body: str) -> dict[str, Any] | str:
    if not raw_body:
        return {}
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        return raw_body
    return payload if isinstance(payload, dict) else raw_body


def wait_for_ready(url: str, label: str) -> dict[str, Any]:
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    last_message = ""
    while time.monotonic() < deadline:
        try:
            status, body = request(url, timeout=10)
            message = f"{label}: HTTP {status} {json.dumps(body, ensure_ascii=False)}"
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
        time.sleep(POLL_INTERVAL_SECONDS)
    raise SmokeTestFailure(f"{label} did not become ready in time")


def run() -> None:
    configuration = compose_configuration()
    service = configuration["services"]["retrieval-api"]
    environment = service.get("environment", {})
    knowledge_index_url = str(environment.get("KNOWLEDGE_INDEX_URL", ""))
    if knowledge_index_url != "http://knowledge-index-api:8080":
        raise SmokeTestFailure(
            "KNOWLEDGE_INDEX_URL must use the stable knowledge-index-api network alias"
        )

    api_port = published_port(configuration, "retrieval-api", 8100)
    api_base_url = os.getenv("SMOKE_API_BASE_URL", f"http://127.0.0.1:{api_port}")
    raw_key = environment.get("API_INTERNAL_KEY", "")
    api_key = "" if raw_key is None else str(raw_key)
    principal_headers = {
        "x-dev-user-id": os.getenv("SMOKE_USER_ID", "smoke-user"),
        "x-dev-group-ids": os.getenv("SMOKE_GROUP_IDS", "all-employees"),
    }
    authenticated_headers = dict(principal_headers)
    authenticated_headers["x-internal-api-key"] = api_key

    print("=== Docker Compose services ===", flush=True)
    print(compose("ps", "-a", check=False).stdout, flush=True)

    live = wait_for_ready(f"{api_base_url}/health/live", "Liveness")
    if live.get("checks", {}).get("process") is not True:
        raise SmokeTestFailure(f"Unexpected liveness response: {live}")

    ready = wait_for_ready(f"{api_base_url}/health/ready", "Readiness")
    checks = ready.get("checks")
    if not isinstance(checks, dict) or checks.get("knowledge_index") is not True:
        raise SmokeTestFailure(f"Readiness did not verify Knowledge Index: {ready}")

    wrong_service_key_status, _ = request(
        f"{api_base_url}/api/v1/retrieve",
        method="POST",
        body={"query": "smoke-rag-2026"},
        headers={**principal_headers, "x-internal-api-key": "definitely-wrong"},
    )
    if wrong_service_key_status != 401:
        raise SmokeTestFailure(
            "Retrieval accepted an invalid internal service key; configure API_INTERNAL_KEY"
        )

    missing_principal_status, _ = request(
        f"{api_base_url}/api/v1/retrieve",
        method="POST",
        body={"query": "smoke-rag-2026"},
        headers={"x-internal-api-key": api_key},
    )
    if missing_principal_status != 401:
        raise SmokeTestFailure(
            "Retrieval accepted a request without a verified development principal"
        )

    retrieval_status, result = request(
        f"{api_base_url}/api/v1/retrieve",
        method="POST",
        body={"query": os.getenv("SMOKE_QUERY", "smoke-rag-2026")},
        headers=authenticated_headers,
    )
    if retrieval_status != 200 or not isinstance(result, dict):
        raise SmokeTestFailure(
            f"Retrieval failed: HTTP {retrieval_status}, response_type={type(result).__name__}"
        )
    if result.get("status") not in {
        "evidence_found",
        "insufficient_evidence",
        "external_generation_blocked",
    }:
        raise SmokeTestFailure(f"Unexpected retrieval status value: {result.get('status')!r}")

    serialized = json.dumps(result, ensure_ascii=False)
    if "source_path" in serialized or "source_url" in serialized:
        raise SmokeTestFailure("Retrieval response leaked a physical source location")
    for evidence in result.get("llm_context", []):
        governance = evidence.get("governance", {})
        if governance.get("external_llm_allowed") is not True:
            raise SmokeTestFailure("Restricted evidence leaked into llm_context")
    for evidence in result.get("restricted_evidence", []):
        governance = evidence.get("governance", {})
        if governance.get("external_llm_allowed") is not False:
            raise SmokeTestFailure("Allowed evidence was mislabeled as restricted")
    llm_context = result.get("llm_context", [])
    attestation = result.get("context_attestation")
    if llm_context and not isinstance(attestation, str):
        raise SmokeTestFailure("Allowed context was returned without an attestation")
    if not llm_context and attestation is not None:
        raise SmokeTestFailure("Attestation was issued without allowed context")
    retrieval = result.get("retrieval", {})
    selected = retrieval.get("selected_count", 0) if isinstance(retrieval, dict) else 0
    require_evidence = os.getenv("SMOKE_REQUIRE_EVIDENCE", "0").casefold() in {
        "1",
        "true",
        "yes",
    }
    if require_evidence and selected < 1:
        raise SmokeTestFailure(
            "Seeded E2E mode requires evidence; run knowledge-index-service make smoke first"
        )

    metrics_status, metrics = request(f"{api_base_url}/metrics")
    if metrics_status != 200 or not isinstance(metrics, str):
        raise SmokeTestFailure("Metrics endpoint failed")
    if "retrieval_http_requests_total" not in metrics:
        raise SmokeTestFailure("Retrieval metrics are missing")

    summary = {
        "status": result.get("status"),
        "candidate_count": retrieval.get("candidate_count")
        if isinstance(retrieval, dict)
        else None,
        "selected_count": selected,
        "llm_context_count": len(result.get("llm_context", [])),
        "restricted_evidence_count": len(result.get("restricted_evidence", [])),
    }
    print("Retrieval summary: " + json.dumps(summary, sort_keys=True), flush=True)
    print("\nSMOKE TEST PASSED (read-only)", flush=True)


def diagnostics() -> None:
    print("\n=== Docker Compose diagnostics ===", flush=True)
    print(compose("ps", "-a", check=False).stdout, flush=True)
    print(compose("logs", "--no-color", "--tail", "200", "retrieval-api", check=False).stdout)


def main() -> int:
    try:
        run()
    except (SmokeTestFailure, OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        print(f"\nSMOKE TEST FAILED: {exc}", file=sys.stderr, flush=True)
        diagnostics()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
