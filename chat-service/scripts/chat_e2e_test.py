"""Opt-in end-to-end test for Chat -> Retrieval -> KIS -> Answer.

The script is deliberately host-only.  It never runs in the Chat container and
never gives the Chat runtime a KIS credential.  A fixed synthetic KIS document
is created only after an explicit opt-in and is soft-deleted by its exact ID in
the final cleanup block.
"""

from __future__ import annotations

import fcntl
import hashlib
import importlib
import json
import math
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import UUID, uuid4

if TYPE_CHECKING:
    from scripts.local_runtime_checks import RuntimeCheckFailure, require_fake_answer_provider
else:
    _runtime_checks = importlib.import_module(
        "local_runtime_checks" if __package__ in {None, ""} else "scripts.local_runtime_checks"
    )
    RuntimeCheckFailure = _runtime_checks.RuntimeCheckFailure
    require_fake_answer_provider = _runtime_checks.require_fake_answer_provider

PROJECT_ROOT = Path(__file__).resolve().parents[1]

LOCK_PATH = PROJECT_ROOT / ".chat-e2e.lock"
MAX_RESPONSE_BYTES = 1_000_000
QUESTION = "What is the chat-e2e-allowed-v1 verification code?"
FIXTURE_CONTENT = (
    "Synthetic Chat E2E fixture verification code: chat-e2e-allowed-v1. "
    "This is test data and contains no company information."
)
FIXTURE_SOURCE_TYPE = "chat_e2e"
FIXTURE_SOURCE_ITEM_ID = "chat-service/allowed-grounded-v1"
FORBIDDEN_PUBLIC_FIELDS = (
    "chunk_id",
    "document_id",
    "version_id",
    "source_path",
    "source_url",
    "context_attestation",
    "attestation",
)


class E2EFailure(RuntimeError):
    """A safe E2E failure without raw response bodies, keys, or evidence."""


class _NoRedirectHandler(HTTPRedirectHandler):
    """Prevent a local service from redirecting a request and its secret header."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


@dataclass(frozen=True, slots=True)
class HttpResult:
    status: int
    payload: Any


def _read_env_file(path: Path) -> dict[str, str]:
    """Read KEY=VALUE entries without evaluating shell syntax."""

    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key and all(character.isalnum() or character == "_" for character in key):
            values[key] = value
    return values


def _value(name: str, env_file: dict[str, str], default: str = "") -> str:
    return os.environ.get(name, env_file.get(name, default))


def _safe_local_url(value: str, name: str) -> str:
    normalized = value.strip().rstrip("/")
    try:
        parsed = urlparse(normalized)
        port = parsed.port
    except ValueError as exc:
        raise E2EFailure(f"{name} is not a valid HTTP URL") from exc
    hostname = parsed.hostname
    if (
        parsed.scheme not in {"http", "https"}
        or hostname not in {"localhost", "127.0.0.1", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65_535)
    ):
        raise E2EFailure(f"{name} must be a credential-free host-local HTTP URL")
    if any(ord(character) <= 0x20 or ord(character) == 0x7F for character in normalized):
        raise E2EFailure(f"{name} contains a control character")
    return normalized


def _decode_body(raw: bytes) -> Any:
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _request(
    base_url: str,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    timeout: float = 15.0,
) -> HttpResult:
    payload = None if body is None else json.dumps(body, separators=(",", ":")).encode()
    request_headers = {"Accept": "application/json", **(headers or {})}
    if payload is not None:
        request_headers["Content-Type"] = "application/json"
    request = Request(f"{base_url}{path}", data=payload, method=method, headers=request_headers)
    opener = build_opener(ProxyHandler({}), _NoRedirectHandler())
    try:
        with opener.open(request, timeout=timeout) as response:
            content_type = response.headers.get("Content-Type", "")
            content_length = response.headers.get("Content-Length")
            if (
                content_length is not None
                and content_length.isdigit()
                and int(content_length) > MAX_RESPONSE_BYTES
            ):
                raise E2EFailure("E2E response exceeded the configured body limit")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise E2EFailure("E2E response exceeded the configured body limit")
            if "application/json" not in content_type.lower():
                raise E2EFailure("E2E endpoint returned an unexpected content type")
            return HttpResult(response.status, _decode_body(raw))
    except HTTPError as exc:
        try:
            raw_error = exc.read(MAX_RESPONSE_BYTES + 1)
            payload_error = _decode_body(raw_error[:MAX_RESPONSE_BYTES])
        except OSError:
            payload_error = None
        return HttpResult(exc.code, payload_error)
    except (OSError, URLError) as exc:
        raise E2EFailure("E2E request could not be reached") from exc


def _expect(result: HttpResult, status: int, operation: str) -> dict[str, Any]:
    if result.status != status or not isinstance(result.payload, dict):
        raise E2EFailure(f"{operation} returned HTTP {result.status}")
    return result.payload


def _parse_uuid(payload: dict[str, Any], field: str, operation: str) -> UUID:
    try:
        value = UUID(str(payload[field]))
    except (KeyError, TypeError, ValueError) as exc:
        raise E2EFailure(f"{operation} returned an invalid {field}") from exc
    return value


def _poll_job(
    kis_url: str,
    key: str,
    job_id: UUID,
    *,
    timeout_seconds: float,
    operation: str,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        result = _request(
            kis_url,
            "GET",
            f"/api/v1/jobs/{job_id}",
            headers={"x-internal-api-key": key},
            timeout=10,
        )
        if result.status != 200 or not isinstance(result.payload, dict):
            raise E2EFailure(f"{operation} job status returned HTTP {result.status}")
        state = result.payload.get("status")
        if state == "completed":
            return
        if state == "failed":
            raise E2EFailure(f"{operation} job failed")
        # Knowledge Index Service exposes its JobStatus enum as pending/processing;
        # do not translate or broaden the upstream contract here.
        if state not in {"pending", "processing"}:
            raise E2EFailure(f"{operation} job returned an invalid status")
        time.sleep(2)
    raise E2EFailure(f"{operation} job did not complete before the timeout")


def _acquire_lock(timeout_seconds: float) -> int:
    if timeout_seconds <= 0 or timeout_seconds > 3_600:
        raise E2EFailure("CHAT_E2E_LOCK_TIMEOUT_SECONDS must be positive and bounded")
    try:
        descriptor = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600)
        os.fchmod(descriptor, 0o600)
    except OSError as exc:
        raise E2EFailure("could not create the host-local E2E lock") from exc
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return descriptor
        except BlockingIOError:
            time.sleep(0.2)
        except OSError as exc:
            os.close(descriptor)
            raise E2EFailure("could not acquire the host-local E2E lock") from exc
    os.close(descriptor)
    raise E2EFailure("another E2E run holds the host-local lock")


def _release_lock(descriptor: int) -> None:
    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _fixture_body() -> dict[str, Any]:
    chunk = {
        "content": FIXTURE_CONTENT,
        "section_path": "Synthetic verification",
        "page_number": None,
        "token_count": max(1, len(FIXTURE_CONTENT.split())),
    }
    canonical = json.dumps(
        {"parser_name": "chat-e2e", "parser_version": "1", "chunks": [chunk]},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "source_type": FIXTURE_SOURCE_TYPE,
        "source_item_id": FIXTURE_SOURCE_ITEM_ID,
        "source_version": "chat-e2e-v1",
        "title": "Chat E2E Synthetic Allowed Fixture",
        "document_type": "policy",
        "owner_department": "IT",
        "document_owner": "chat-e2e",
        "confidentiality": "public",
        "status": "published",
        "language": "en",
        "external_llm_allowed": True,
        "version_number": "1",
        "content_hash": hashlib.sha256(canonical).hexdigest(),
        "mime_type": "text/plain",
        "file_size": len(FIXTURE_CONTENT.encode("utf-8")),
        "parser_name": "chat-e2e",
        "parser_version": "1",
        "chunks": [chunk],
        "custom_metadata": {"purpose": "chat-e2e"},
        "acl": [{"principal_type": "group", "principal_id": "all-employees", "effect": "allow"}],
    }


def _ensure_fixture(kis_url: str, key: str, job_timeout: float) -> UUID:
    submitted = _expect(
        _request(
            kis_url,
            "POST",
            "/api/v1/documents",
            headers={"x-internal-api-key": key},
            body=_fixture_body(),
        ),
        202,
        "KIS synthetic fixture submit",
    )
    document_id = _parse_uuid(submitted, "document_id", "KIS synthetic fixture submit")
    raw_job_id = submitted.get("job_id")
    if raw_job_id is None:
        if submitted.get("indexing_status") != "indexed":
            raise E2EFailure("KIS synthetic fixture without a job is not indexed")
        return document_id
    job_id = _parse_uuid(submitted, "job_id", "KIS synthetic fixture submit")
    _poll_job(kis_url, key, job_id, timeout_seconds=job_timeout, operation="KIS fixture index")
    return document_id


def _check_kis_auth(kis_url: str, key: str) -> None:
    result = _request(
        kis_url,
        "GET",
        "/api/v1/auth/check",
        headers={"x-internal-api-key": key},
        timeout=10,
    )
    if (
        result.status != 200
        or not isinstance(result.payload, dict)
        or result.payload.get("status") != "ok"
    ):
        raise E2EFailure("KIS service-key authentication check failed")


def _chat_headers(user_id: str, groups: str) -> dict[str, str]:
    if not user_id.strip() or not groups.strip():
        raise E2EFailure("E2E identity settings must not be blank")
    for value, name in ((user_id, "CHAT_E2E_USER_ID"), (groups, "CHAT_E2E_GROUP_IDS")):
        if len(value) > 4_096 or any(
            ord(character) < 0x20 or ord(character) == 0x7F for character in value
        ):
            raise E2EFailure(f"{name} contains an invalid header value")
    return {"x-dev-user-id": user_id.strip(), "x-dev-group-ids": groups.strip()}


def _safe_public_payload(payload: dict[str, Any], operation: str) -> None:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    for field in FORBIDDEN_PUBLIC_FIELDS:
        if field in serialized:
            raise E2EFailure(f"{operation} exposed a forbidden evidence field")


def _wait_turn(
    chat_url: str,
    headers: dict[str, str],
    conversation_id: UUID,
    turn_id: UUID,
    timeout_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        result = _request(
            chat_url,
            "GET",
            f"/api/v1/conversations/{conversation_id}/turns/{turn_id}",
            headers=headers,
            timeout=15,
        )
        if result.status != 200 or not isinstance(result.payload, dict):
            raise E2EFailure(f"turn status returned HTTP {result.status}")
        state = result.payload.get("state")
        if state == "completed":
            if result.payload.get("outcome") not in {"answered", "unable_to_answer"}:
                raise E2EFailure("completed turn returned an invalid outcome")
            return result.payload
        if state in {"failed", "failed_unknown"}:
            raise E2EFailure("Chat turn failed")
        if state != "processing":
            raise E2EFailure("turn status returned an invalid state")
        time.sleep(2)
    raise E2EFailure("Chat turn did not complete before the timeout")


def _assert_answered(payload: dict[str, Any]) -> None:
    _safe_public_payload(payload, "Chat answer")
    if payload.get("state") != "completed" or payload.get("outcome") != "answered":
        raise E2EFailure("Chat did not produce an answered turn")
    assistant = payload.get("assistant_message")
    if not isinstance(assistant, dict) or not str(assistant.get("content", "")).strip():
        raise E2EFailure("Chat answered turn has no assistant message")
    citations = assistant.get("citations")
    if not isinstance(citations, list) or not citations:
        raise E2EFailure("Chat answer has no safe citations")
    for citation in citations:
        if not isinstance(citation, dict) or not citation.get("title"):
            raise E2EFailure("Chat answer contains an invalid safe citation")


def _cleanup_chat(chat_url: str, conversation_id: UUID) -> None:
    admin_headers = {
        "x-dev-user-id": "chat-e2e-admin",
        "x-dev-group-ids": "all-employees",
        "x-dev-roles": "helpdesk-admin",
    }
    preview = _expect(
        _request(
            chat_url,
            "POST",
            "/api/v1/admin/conversation-purge-previews",
            headers=admin_headers,
            body={"conversation_ids": [str(conversation_id)]},
        ),
        201,
        "Chat E2E conversation purge preview",
    )
    preview_id = _parse_uuid(preview, "preview_id", "Chat E2E conversation purge preview")
    _expect(
        _request(
            chat_url,
            "POST",
            "/api/v1/admin/conversation-purges",
            headers=admin_headers,
            body={
                "preview_id": str(preview_id),
                "reason": "Chat E2E synthetic conversation cleanup",
            },
        ),
        200,
        "Chat E2E conversation purge",
    )


def _cleanup_kis(kis_url: str, key: str, document_id: UUID, job_timeout: float) -> None:
    deleted = _expect(
        _request(
            kis_url,
            "DELETE",
            f"/api/v1/documents/{document_id}",
            headers={"x-internal-api-key": key},
        ),
        202,
        "KIS synthetic fixture delete",
    )
    raw_job_id = deleted.get("job_id")
    if raw_job_id is None:
        raise E2EFailure("KIS fixture delete did not return a job ID")
    job_id = _parse_uuid(deleted, "job_id", "KIS synthetic fixture delete")
    _poll_job(kis_url, key, job_id, timeout_seconds=job_timeout, operation="KIS fixture delete")


def main() -> int:
    """Run the complete opt-in E2E lifecycle and exact-ID cleanup."""

    env_file = _read_env_file(PROJECT_ROOT / ".env.e2e")
    allow = _value("CHAT_E2E_ALLOW_KIS_WRITES", env_file, "0").strip()
    if allow != "1":
        print(
            "E2E SAFE FAILURE: disabled; set CHAT_E2E_ALLOW_KIS_WRITES=1 in host-only .env.e2e",
            file=sys.stderr,
        )
        return 2
    kis_key = _value("CHAT_E2E_KIS_API_KEY", env_file).strip()
    if not kis_key:
        print("E2E SAFE FAILURE: opt-in requires a non-empty KIS service key", file=sys.stderr)
        return 2
    if any(ord(character) < 0x21 or ord(character) == 0x7F for character in kis_key):
        print("E2E SAFE FAILURE: KIS service key contains an invalid character", file=sys.stderr)
        return 2

    try:
        kis_url = _safe_local_url(
            _value("CHAT_E2E_KIS_BASE_URL", env_file, "http://127.0.0.1:8080"),
            "CHAT_E2E_KIS_BASE_URL",
        )
        chat_url = _safe_local_url(
            _value("CHAT_E2E_CHAT_BASE_URL", env_file, "http://127.0.0.1:8120"),
            "CHAT_E2E_CHAT_BASE_URL",
        )
        lock_timeout = float(_value("CHAT_E2E_LOCK_TIMEOUT_SECONDS", env_file, "60"))
        job_timeout = float(_value("CHAT_E2E_JOB_TIMEOUT_SECONDS", env_file, "600"))
        turn_timeout = float(_value("CHAT_E2E_TURN_TIMEOUT_SECONDS", env_file, "180"))
    except E2EFailure as exc:
        print(f"E2E SAFE FAILURE: {exc}", file=sys.stderr)
        return 2
    except (TypeError, ValueError):
        print("E2E SAFE FAILURE: timeout configuration is invalid", file=sys.stderr)
        return 2
    if any(
        not math.isfinite(value) or value <= 0 or value > 86_400
        for value in (lock_timeout, job_timeout, turn_timeout)
    ):
        print("E2E SAFE FAILURE: timeout configuration is invalid", file=sys.stderr)
        return 2

    descriptor: int | None = None
    document_id: UUID | None = None
    conversation_id: UUID | None = None
    primary_error: str | None = None
    cleanup_errors: list[str] = []
    try:
        descriptor = _acquire_lock(lock_timeout)
        # Never write a fixture or create a Chat conversation unless the local
        # Answer container is explicitly configured for the offline Fake Provider.
        try:
            require_fake_answer_provider(PROJECT_ROOT)
        except RuntimeCheckFailure as exc:
            raise E2EFailure(
                "Answer provider runtime is not the offline fake test provider"
            ) from exc
        # Authentication is checked while holding the same lock that protects the fixed fixture
        # identity, and it happens before the first KIS write.
        _check_kis_auth(kis_url, kis_key)
        document_id = _ensure_fixture(kis_url, kis_key, job_timeout)

        ready = _request(chat_url, "GET", "/health/ready", timeout=10)
        if (
            ready.status != 200
            or not isinstance(ready.payload, dict)
            or ready.payload.get("status") != "ok"
        ):
            raise E2EFailure("Chat readiness is not ok")
        employee_headers = _chat_headers(
            _value("CHAT_E2E_USER_ID", env_file, "chat-e2e-user"),
            _value("CHAT_E2E_GROUP_IDS", env_file, "all-employees"),
        )
        created = _expect(
            _request(
                chat_url,
                "POST",
                "/api/v1/conversations",
                headers=employee_headers,
                body={"title": "Chat E2E Synthetic Conversation"},
            ),
            201,
            "Chat E2E conversation create",
        )
        conversation_id = _parse_uuid(created, "conversation_id", "Chat E2E conversation create")

        idempotency_key = str(uuid4())
        turn_request = {"question": QUESTION, "language": "en"}
        first = _request(
            chat_url,
            "POST",
            f"/api/v1/conversations/{conversation_id}/turns",
            headers={**employee_headers, "Idempotency-Key": idempotency_key},
            body=turn_request,
            timeout=30,
        )
        if first.status not in {200, 202} or not isinstance(first.payload, dict):
            raise E2EFailure(f"Chat first turn returned HTTP {first.status}")
        first_turn_id = _parse_uuid(first.payload, "turn_id", "Chat first turn")
        first_terminal = (
            first.payload
            if first.status == 200 and first.payload.get("state") == "completed"
            else _wait_turn(
                chat_url, employee_headers, conversation_id, first_turn_id, turn_timeout
            )
        )
        _assert_answered(first_terminal)

        replay = _request(
            chat_url,
            "POST",
            f"/api/v1/conversations/{conversation_id}/turns",
            headers={**employee_headers, "Idempotency-Key": idempotency_key},
            body=turn_request,
            timeout=30,
        )
        if replay.status not in {200, 202} or not isinstance(replay.payload, dict):
            raise E2EFailure(f"Chat idempotent replay returned HTTP {replay.status}")
        replay_turn_id = _parse_uuid(replay.payload, "turn_id", "Chat idempotent replay")
        if replay_turn_id != first_turn_id:
            raise E2EFailure("Chat idempotent replay created a different turn")
        replay_terminal = (
            replay.payload
            if replay.status == 200 and replay.payload.get("state") == "completed"
            else _wait_turn(
                chat_url, employee_headers, conversation_id, replay_turn_id, turn_timeout
            )
        )
        _assert_answered(replay_terminal)

        messages = _expect(
            _request(
                chat_url,
                "GET",
                f"/api/v1/conversations/{conversation_id}/messages",
                headers=employee_headers,
            ),
            200,
            "Chat E2E messages",
        )
        _safe_public_payload(messages, "Chat E2E messages")
        items = messages.get("items")
        if not isinstance(items, list) or len(items) != 2:
            raise E2EFailure("idempotent replay produced duplicate messages")
    except E2EFailure as exc:
        primary_error = str(exc)
    except Exception:
        primary_error = "unexpected E2E failure"
    finally:
        # The lock remains held through both exact-ID cleanup operations.  Cleanup failures are
        # reported generically so a KIS key or fixture content can never reach terminal output.
        if descriptor is not None:
            if conversation_id is not None:
                try:
                    _cleanup_chat(chat_url, conversation_id)
                except Exception:
                    cleanup_errors.append("Chat synthetic conversation cleanup failed")
            if document_id is not None:
                try:
                    _cleanup_kis(kis_url, kis_key, document_id, job_timeout)
                except Exception:
                    cleanup_errors.append("KIS synthetic fixture cleanup failed")
            _release_lock(descriptor)

    if primary_error is not None:
        print(f"CHAT E2E FAILED: {primary_error}", file=sys.stderr)
        for cleanup_error in cleanup_errors:
            print(f"CHAT E2E CLEANUP FAILED: {cleanup_error}", file=sys.stderr)
        return 1
    if cleanup_errors:
        for cleanup_error in cleanup_errors:
            print(f"CHAT E2E CLEANUP FAILED: {cleanup_error}", file=sys.stderr)
        return 1
    print("CHAT E2E PASSED: grounded answer, safe citation, and idempotent replay")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
