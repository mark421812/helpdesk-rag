#!/usr/bin/env python3
"""Read-only HTTP E2E for an already running Retrieval and Answer stack.

The script never writes documents or databases. Set E2E_ALLOWED_QUERY to a query that resolves
to an existing synthetic/public fixture whose external_llm_allowed value is true.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any
from uuid import uuid4


class E2EFailure(RuntimeError):
    """A cross-service contract or HTTP assertion failed."""


def _request(
    url: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any] | str]:
    encoded = None
    resolved_headers = {"accept": "application/json", **(headers or {})}
    if body is not None:
        encoded = json.dumps(
            body,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode()
        resolved_headers["content-type"] = "application/json"
    request = urllib.request.Request(
        url,
        data=encoded,
        headers=resolved_headers,
        method=method,
    )
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=90) as response:
            return response.status, _parse(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, _parse(exc.read())
    except OSError as exc:
        raise E2EFailure(f"Cannot connect to {url}: {type(exc).__name__}") from exc


def _parse(raw: bytes) -> dict[str, Any] | str:
    text = raw.decode("utf-8", errors="replace")
    try:
        value: Any = json.loads(text)
    except json.JSONDecodeError:
        return text
    return value if isinstance(value, dict) else text


def _required_environment(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise E2EFailure(
            f"{name} is required; use an existing synthetic/public allowed fixture only"
        )
    return value


def _answer_body(
    retrieval: dict[str, Any],
    *,
    question: str,
    language: str | None,
) -> dict[str, Any]:
    if retrieval.get("status") != "evidence_found":
        raise E2EFailure(
            "Retrieval did not return evidence_found; this read-only E2E requires an existing "
            "allowed fixture"
        )
    evidence = retrieval.get("llm_context")
    attestation = retrieval.get("context_attestation")
    if not isinstance(evidence, list) or not evidence or not isinstance(attestation, str):
        raise E2EFailure("Retrieval returned no attested llm_context")
    for item in evidence:
        if not isinstance(item, dict):
            raise E2EFailure("Retrieval llm_context contains a non-object item")
        governance = item.get("governance")
        if not isinstance(governance, dict) or governance.get("external_llm_allowed") is not True:
            raise E2EFailure("Retrieval placed forbidden evidence in llm_context")
    # This allowlist mapping is the future Chat Service's orchestration contract. In particular,
    # restricted_evidence and retrieval diagnostics are never copied to Answer Generation.
    return {
        "question": question,
        "language": language,
        "evidence": evidence,
        "context_attestation": attestation,
    }


def run() -> None:
    question = _required_environment("E2E_ALLOWED_QUERY")
    raw_language = os.getenv("E2E_LANGUAGE", "zh-TW").strip()
    language = raw_language or None
    retrieval_url = os.getenv("E2E_RETRIEVAL_URL", "http://127.0.0.1:8100").rstrip("/")
    answer_url = os.getenv("E2E_ANSWER_URL", "http://127.0.0.1:8110").rstrip("/")
    request_id = f"retrieval-answer-e2e-{uuid4()}"

    retrieval_status, retrieval_payload = _request(
        f"{retrieval_url}/api/v1/retrieve",
        method="POST",
        body={"query": question, "language": language},
        headers={
            "x-internal-api-key": os.getenv("E2E_RETRIEVAL_API_KEY", "local-development-only"),
            "x-dev-user-id": os.getenv("E2E_USER_ID", "synthetic-e2e-user"),
            "x-dev-group-ids": os.getenv("E2E_GROUP_IDS", "all-employees"),
            "x-request-id": request_id,
        },
    )
    if retrieval_status != 200 or not isinstance(retrieval_payload, dict):
        raise E2EFailure(f"Retrieval failed with HTTP {retrieval_status}")

    answer_status, answer_payload = _request(
        f"{answer_url}/api/v1/answers",
        method="POST",
        body=_answer_body(retrieval_payload, question=question, language=language),
        headers={
            "x-internal-api-key": os.getenv("E2E_ANSWER_API_KEY", "local-development-only"),
            "x-request-id": request_id,
        },
    )
    if answer_status != 200 or not isinstance(answer_payload, dict):
        raise E2EFailure(f"Answer Generation failed with HTTP {answer_status}")
    if answer_payload.get("status") not in {"answered", "unable_to_answer"}:
        raise E2EFailure("Answer Generation returned an unknown business status")
    serialized = json.dumps(answer_payload, ensure_ascii=False)
    if "source_path" in serialized or "source_url" in serialized:
        raise E2EFailure("Answer response leaked a physical source location")

    retrieval_diagnostics = retrieval_payload.get("retrieval")
    selected_count = (
        retrieval_diagnostics.get("selected_count")
        if isinstance(retrieval_diagnostics, dict)
        else None
    )
    summary = {
        "retrieval_status": retrieval_payload.get("status"),
        "selected_count": selected_count,
        "allowed_evidence_count": len(retrieval_payload.get("llm_context", [])),
        "restricted_evidence_count": len(retrieval_payload.get("restricted_evidence", [])),
        "answer_status": answer_payload.get("status"),
        "citation_count": len(answer_payload.get("citations", [])),
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    print("RETRIEVAL -> ANSWER HTTP E2E PASSED (read-only)")


def main() -> int:
    try:
        run()
    except (E2EFailure, json.JSONDecodeError, ValueError) as exc:
        print(f"RETRIEVAL -> ANSWER HTTP E2E FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
