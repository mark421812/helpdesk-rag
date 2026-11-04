#!/usr/bin/env python3
"""Offline Docker smoke：以 development private key 簽 synthetic context。"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIVATE_KEY = PROJECT_ROOT.parent / "retrieval-service/secrets/dev/retrieval-private.pem"
STARTUP_TIMEOUT_SECONDS = int(os.getenv("SMOKE_STARTUP_TIMEOUT_SECONDS", "900"))
POLL_INTERVAL_SECONDS = 2


class SmokeTestFailure(RuntimeError):
    pass


def compose(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", *arguments],
        cwd=PROJECT_ROOT,
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def request(
    url: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any] | str]:
    encoded = None
    resolved_headers = {"accept": "application/json", **(headers or {})}
    if body is not None:
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        resolved_headers["content-type"] = "application/json"
    http_request = urllib.request.Request(
        url, data=encoded, headers=resolved_headers, method=method
    )
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(http_request, timeout=90) as response:
            return response.status, _parse(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, _parse(exc.read())
    except OSError as exc:
        raise SmokeTestFailure(f"Cannot connect to {url}: {exc}") from exc


def _parse(raw: bytes) -> dict[str, Any] | str:
    decoded = raw.decode("utf-8", errors="replace")
    try:
        payload = json.loads(decoded)
    except json.JSONDecodeError:
        return decoded
    return payload if isinstance(payload, dict) else decoded


def _canonical(value: object) -> bytes:
    # 此 smoke fixture 只含 RFC 8785 與 Python sorted JSON 表示相同的字串/整數/null/bool。
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest(separator: bytes, value: object) -> str:
    return _base64url(hashlib.sha256(separator + _canonical(value)).digest())


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def signed_body(question: str) -> dict[str, Any]:
    evidence = [
        {
            "rank": 1,
            # Retrieval 的 live response 會包含相似度分數。Answer 只在 API
            # contract 邊界驗證此欄位，之後不把它送入 prompt 或回應。
            "score": 0.99,
            "chunk_id": "00000000-0000-0000-0000-000000000001",
            "document_id": "00000000-0000-0000-0000-000000000002",
            "version_id": "00000000-0000-0000-0000-000000000003",
            "content": "Synthetic policy: employees may work remotely two days per week.",
            "document_type": "policy",
            "token_count": 15,
            "citation": {
                "title": "Synthetic Remote Work Policy",
                "section_path": "Remote Work",
                "page_number": 3,
            },
            "governance": {
                "confidentiality": "public",
                "language": "en",
                "external_llm_allowed": True,
            },
        }
    ]
    now = int(time.time())
    header = {"alg": "EdDSA", "kid": "retrieval-dev-001", "typ": "JWT"}
    claims = {
        "iss": "retrieval-service",
        "aud": "answer-generation-service",
        "iat": now,
        "nbf": now,
        "exp": now + 120,
        "jti": str(uuid4()),
        "context_schema_version": "retrieval-context-v1",
        "policy_version": "retrieval-policy-v1",
        "question_sha256": _digest(
            b"helpdesk-rag/question/v1\0",
            {"language": "en", "question": question},
        ),
        # Retrieval 的 attestation schema 不把 ranking score 納入可信內容；
        # score 可能因索引狀態改變，也不應影響 evidence 的內容簽章。
        "context_sha256": _digest(
            b"helpdesk-rag/context/v1\0",
            {
                "evidence": [
                    {key: value for key, value in item.items() if key != "score"}
                    for item in evidence
                ]
            },
        ),
    }
    encoded_header = _base64url(_canonical(header))
    encoded_claims = _base64url(_canonical(claims))
    signing_input = f"{encoded_header}.{encoded_claims}".encode("ascii")
    with tempfile.TemporaryDirectory(prefix="answer-smoke-") as temp_directory:
        input_path = Path(temp_directory) / "signing-input.bin"
        signature_path = Path(temp_directory) / "signature.bin"
        input_path.write_bytes(signing_input)
        subprocess.run(
            [
                "openssl",
                "pkeyutl",
                "-sign",
                "-rawin",
                "-inkey",
                str(PRIVATE_KEY),
                "-in",
                str(input_path),
                "-out",
                str(signature_path),
            ],
            check=True,
            capture_output=True,
        )
        signature = signature_path.read_bytes()
    return {
        "question": question,
        "language": "en",
        "evidence": evidence,
        "context_attestation": f"{encoded_header}.{encoded_claims}.{_base64url(signature)}",
    }


def wait_ready(url: str) -> dict[str, Any]:
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            status, payload = request(url)
            print(f"Readiness: HTTP {status} {payload}", flush=True)
            if status == 200 and isinstance(payload, dict):
                return payload
        except SmokeTestFailure as exc:
            print(exc, flush=True)
        time.sleep(POLL_INTERVAL_SECONDS)
    raise SmokeTestFailure("Answer Generation did not become ready in time")


def run() -> None:
    if not PRIVATE_KEY.is_file():
        raise SmokeTestFailure("Development private key is missing; run make keys-dev")
    configuration = json.loads(compose("config", "--format", "json").stdout)
    service = configuration["services"]["answer-generation-api"]
    provider = str(service["environment"].get("LLM_PROVIDER", ""))
    nvidia_live_mode = os.getenv("RUN_LIVE_NVIDIA_SMOKE", "0") == "1"
    groq_live_mode = os.getenv("RUN_LIVE_GROQ_SMOKE", "0") == "1"
    if nvidia_live_mode and groq_live_mode:
        raise SmokeTestFailure("Only one live Provider smoke mode may be enabled")
    expected_provider = (
        "nvidia_hosted" if nvidia_live_mode else "groq_hosted" if groq_live_mode else "fake"
    )
    if provider != expected_provider:
        raise SmokeTestFailure(
            f"Smoke mode requires LLM_PROVIDER={expected_provider}; compose has {provider!r}"
        )
    raw_port = service["ports"][0].get("published", 8110)
    base_url = os.getenv("SMOKE_API_BASE_URL", f"http://127.0.0.1:{raw_port}")
    api_key = str(service["environment"]["API_INTERNAL_KEY"])

    print("=== Docker Compose services ===", flush=True)
    print(compose("ps", "-a", check=False).stdout, flush=True)
    live_status, live = request(f"{base_url}/health/live")
    if live_status != 200:
        raise SmokeTestFailure(f"Liveness failed: {live}")
    ready = wait_ready(f"{base_url}/health/ready")
    if not all(ready.get("checks", {}).values()):
        raise SmokeTestFailure(f"Readiness checks failed: {ready}")

    wrong_key_status, _ = request(
        f"{base_url}/api/v1/answers",
        method="POST",
        body=signed_body("What is the synthetic remote work rule?"),
        headers={"x-internal-api-key": "wrong"},
    )
    if wrong_key_status != 401:
        raise SmokeTestFailure("Invalid service key was accepted")

    valid_body = signed_body("What is the synthetic remote work rule?")
    success_status, success = request(
        f"{base_url}/api/v1/answers",
        method="POST",
        body=valid_body,
        headers={"x-internal-api-key": api_key, "x-request-id": "answer-smoke"},
    )
    if success_status != 200 or not isinstance(success, dict):
        raise SmokeTestFailure(f"Grounded generation failed: HTTP {success_status} {success}")
    if success.get("status") != "answered" or len(success.get("citations", [])) != 1:
        raise SmokeTestFailure(f"Unexpected grounded answer: {success}")
    generation = success.get("generation")
    actual_generation_provider = (
        generation.get("provider") if isinstance(generation, dict) else None
    )
    expected_generation_provider = (
        "nvidia_hosted_nim" if nvidia_live_mode else "groq" if groq_live_mode else "fake"
    )
    if actual_generation_provider != expected_generation_provider:
        raise SmokeTestFailure(
            "Smoke response came from an unexpected Provider: "
            f"expected {expected_generation_provider!r}, got {actual_generation_provider!r}"
        )
    serialized = json.dumps(success)
    if "source_path" in serialized or "source_url" in serialized:
        raise SmokeTestFailure("Answer leaked a physical source location")

    tampered = dict(valid_body)
    tampered["question"] = "Tampered question"
    tampered_status, _ = request(
        f"{base_url}/api/v1/answers",
        method="POST",
        body=tampered,
        headers={"x-internal-api-key": api_key},
    )
    if tampered_status != 403:
        raise SmokeTestFailure("Tampered signed context was accepted")

    expect_policy_block = os.getenv("EXPECT_OUTBOUND_POLICY_BLOCK", "1") == "1"
    if expect_policy_block:
        sensitive_status, sensitive = request(
            f"{base_url}/api/v1/answers",
            method="POST",
            body=signed_body("password=synthetic-secret-value"),
            headers={"x-internal-api-key": api_key},
        )
        if sensitive_status != 403 or not isinstance(sensitive, dict):
            raise SmokeTestFailure("Sensitive question was not blocked")
        if sensitive.get("error") != "outbound_policy_blocked":
            raise SmokeTestFailure(f"Unexpected policy error: {sensitive}")
    else:
        # Scanner 關閉時不產生敏感探針，尤其不能把探針送往 live 外部 Provider。
        print("Outbound policy block probe: skipped by explicit opt-out", flush=True)

    metrics_status, metrics = request(f"{base_url}/metrics")
    if metrics_status != 200 or not isinstance(metrics, str):
        raise SmokeTestFailure("Metrics endpoint failed")
    if "answer_generation_http_requests_total" not in metrics:
        raise SmokeTestFailure("Answer Generation metrics are missing")
    print("Answer summary: " + json.dumps(success, ensure_ascii=False), flush=True)
    smoke_mode = (
        "NVIDIA Hosted Provider, external API used"
        if nvidia_live_mode
        else "Groq Hosted Provider, external API used"
        if groq_live_mode
        else "Fake Provider, no external API"
    )
    print(f"\nSMOKE TEST PASSED ({smoke_mode})", flush=True)


def diagnostics() -> None:
    print("\n=== Docker Compose diagnostics ===", flush=True)
    print(compose("ps", "-a", check=False).stdout, flush=True)
    print(
        compose("logs", "--no-color", "--tail", "200", "answer-generation-api", check=False).stdout,
        flush=True,
    )


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
