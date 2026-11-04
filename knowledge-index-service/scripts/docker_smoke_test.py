#!/usr/bin/env python3
"""Exercise the complete Docker Compose indexing and retrieval path."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional, Union

PROJECT_ROOT = Path(__file__).resolve().parents[1]
POLL_INTERVAL_SECONDS = 5
STARTUP_TIMEOUT_SECONDS = int(os.getenv("SMOKE_STARTUP_TIMEOUT_SECONDS", "900"))
JOB_TIMEOUT_SECONDS = int(os.getenv("SMOKE_JOB_TIMEOUT_SECONDS", "600"))


class SmokeTestFailure(RuntimeError):
    """Raised when an end-to-end assertion fails."""


def parse_response_body(raw_body: str) -> Union[dict[str, Any], str]:
    if not raw_body:
        return {}
    try:
        return json.loads(raw_body)
    except json.JSONDecodeError:
        return raw_body


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
    return json.loads(result.stdout)


def published_port(configuration: dict[str, Any], service: str, default: int) -> int:
    ports = configuration["services"][service].get("ports", [])
    if not ports:
        return default
    return int(ports[0]["published"])


def request(
    method: str,
    url: str,
    *,
    body: Optional[dict[str, Any]] = None,
    api_key: str = "",
    timeout: int = 60,
) -> tuple[int, Union[dict[str, Any], str]]:
    headers = {"accept": "application/json"}
    data = None
    if body is not None:
        headers["content-type"] = "application/json"
        data = json.dumps(body, ensure_ascii=False).encode()
    if api_key:
        headers["x-internal-api-key"] = api_key

    http_request = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method=method,
    )
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(http_request, timeout=timeout) as response:
            raw_body = response.read().decode()
            return response.status, parse_response_body(raw_body)
    except urllib.error.HTTPError as exc:
        raw_body = exc.read().decode()
        return exc.code, parse_response_body(raw_body)
    except OSError as exc:
        reason = getattr(exc, "reason", exc)
        raise SmokeTestFailure(f"Cannot connect to {url}: {reason}") from exc


def wait_for_health(url: str, service_name: str) -> Union[dict[str, Any], str]:
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    last_message = ""
    while time.monotonic() < deadline:
        try:
            status, body = request("GET", url, timeout=30)
            message = f"{service_name}: HTTP {status} {body}"
            if message != last_message:
                print(message, flush=True)
                last_message = message
            if status == 200:
                return body
        except SmokeTestFailure as exc:
            message = str(exc)
            if message != last_message:
                print(message, flush=True)
                last_message = message
        time.sleep(POLL_INTERVAL_SECONDS)
    raise SmokeTestFailure(
        f"{service_name} did not become healthy within {STARTUP_TIMEOUT_SECONDS} seconds"
    )


def wait_for_readiness(api_base_url: str) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    last_reported_status: Optional[tuple[int, str]] = None
    while time.monotonic() < deadline:
        try:
            status, body = request("GET", f"{api_base_url}/health/ready", timeout=30)
            current_status = (status, json.dumps(body, ensure_ascii=False, sort_keys=True))
            if current_status != last_reported_status:
                print(f"Readiness: HTTP {status} {current_status[1]}", flush=True)
                last_reported_status = current_status
            if status == 200:
                return
        except SmokeTestFailure as exc:
            message = (0, str(exc))
            if message != last_reported_status:
                print(str(exc), flush=True)
                last_reported_status = message
        time.sleep(POLL_INTERVAL_SECONDS)
    raise SmokeTestFailure(f"API did not become ready within {STARTUP_TIMEOUT_SECONDS} seconds")


def wait_for_job(api_base_url: str, api_key: str, job_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + JOB_TIMEOUT_SECONDS
    last_status = ""
    while time.monotonic() < deadline:
        status, body = request(
            "GET",
            f"{api_base_url}/api/v1/jobs/{job_id}",
            api_key=api_key,
        )
        if status != 200 or not isinstance(body, dict):
            raise SmokeTestFailure(f"Job lookup failed: HTTP {status} {body}")
        job_status = str(body.get("status", ""))
        if job_status != last_status:
            print(
                f"Index job {job_id}: {job_status} "
                f"(attempt {body.get('attempt_count')}/{body.get('max_attempts')})",
                flush=True,
            )
            last_status = job_status
        if job_status == "completed":
            return body
        if job_status == "failed":
            raise SmokeTestFailure(
                f"Index job failed: {body.get('error_code')}: {body.get('error_message')}"
            )
        time.sleep(POLL_INTERVAL_SECONDS)
    raise SmokeTestFailure(f"Index job did not complete within {JOB_TIMEOUT_SECONDS} seconds")


def diagnostic_logs() -> None:
    print("\n=== Docker Compose diagnostics ===", flush=True)
    for arguments in (
        ("ps", "-a"),
        ("logs", "--no-color", "--tail", "200", "migrate", "api", "worker", "qdrant"),
    ):
        try:
            print(compose(*arguments, check=False).stdout, flush=True)
        except OSError as exc:
            print(f"Unable to collect {' '.join(arguments)}: {exc}", flush=True)


def run() -> None:
    configuration = compose_configuration()
    api_environment = configuration["services"]["api"].get("environment", {})
    api_key = str(api_environment.get("API_INTERNAL_KEY", ""))
    api_port = published_port(configuration, "api", 8080)
    qdrant_port = published_port(configuration, "qdrant", 6333)
    api_base_url = os.getenv("SMOKE_API_BASE_URL", f"http://127.0.0.1:{api_port}")
    qdrant_base_url = os.getenv(
        "SMOKE_QDRANT_BASE_URL",
        f"http://127.0.0.1:{qdrant_port}",
    )

    print("=== Docker Compose services ===", flush=True)
    print(compose("ps", "-a", check=False).stdout, flush=True)

    wait_for_health(f"{api_base_url}/health/live", "Liveness")
    wait_for_health(f"{qdrant_base_url}/healthz", "Qdrant")

    wait_for_readiness(api_base_url)

    content = "Helpdesk smoke test 規章：員工每週可以遠端工作兩天。驗證代碼 smoke-rag-2026。"
    content_hash = hashlib.sha256(content.encode()).hexdigest()
    submission = {
        "source_type": "smoke_test",
        "source_item_id": "docker/full-index-and-search",
        "source_version": "1",
        "title": "Docker Smoke Test 遠端工作規章",
        "document_type": "policy",
        "owner_department": "IT",
        "document_owner": "helpdesk-smoke-test",
        "confidentiality": "internal",
        "status": "published",
        "language": "zh-TW",
        "external_llm_allowed": False,
        "version_number": "1",
        "content_hash": content_hash,
        "mime_type": "text/plain",
        "file_size": len(content.encode()),
        "parser_name": "smoke-test",
        "parser_version": "1",
        "source_path": "/smoke-test/remote-work.txt",
        "custom_metadata": {"purpose": "automated-smoke-test"},
        "acl": [
            {
                "principal_type": "group",
                "principal_id": "all-employees",
                "effect": "allow",
            }
        ],
        "chunks": [
            {
                "content": content,
                "section_path": "遠端工作",
                "token_count": max(1, len(content.encode("utf-8"))),
            }
        ],
    }
    submit_status, submit_body = request(
        "POST",
        f"{api_base_url}/api/v1/documents",
        body=submission,
        api_key=api_key,
        timeout=120,
    )
    if submit_status != 202 or not isinstance(submit_body, dict):
        raise SmokeTestFailure(f"Document submission failed: HTTP {submit_status} {submit_body}")
    print(
        "Submission: " + json.dumps(submit_body, ensure_ascii=False, sort_keys=True),
        flush=True,
    )

    job_id = submit_body.get("job_id")
    if job_id:
        wait_for_job(api_base_url, api_key, str(job_id))

    search_status, search_body = request(
        "POST",
        f"{api_base_url}/api/v1/search",
        body={
            "query": "員工每週可以遠端工作幾天？",
            "limit": 5,
            "filters": {
                "group_ids": ["all-employees"],
                "statuses": ["published"],
                "language": "zh-TW",
            },
        },
        api_key=api_key,
        timeout=120,
    )
    if search_status != 200 or not isinstance(search_body, dict):
        raise SmokeTestFailure(f"Search failed: HTTP {search_status} {search_body}")

    document_id = str(submit_body["document_id"])
    matching_items = [
        item for item in search_body.get("items", []) if str(item.get("document_id")) == document_id
    ]
    if not matching_items:
        raise SmokeTestFailure(
            "Search did not return the indexed smoke-test document: "
            + json.dumps(search_body, ensure_ascii=False)
        )
    print(
        "Search match: " + json.dumps(matching_items[0], ensure_ascii=False, sort_keys=True),
        flush=True,
    )
    print("\nSMOKE TEST PASSED", flush=True)


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
