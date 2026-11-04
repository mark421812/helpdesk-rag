"""Safe Docker smoke test for Chat Service health, CRUD, archive, and purge.

This script never calls Retrieval or Answer generation.  It creates one
synthetic conversation and purges only the exact ID returned by that create
call, so a failed run cannot use a broad selector to touch user data.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import UUID, uuid4

MAX_RESPONSE_BYTES = 512_000
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class SmokeFailure(RuntimeError):
    """A safe smoke-test failure without response bodies or secrets."""


class _NoRedirectHandler(HTTPRedirectHandler):
    """Do not follow a response redirect outside the host-local smoke target."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


@dataclass(frozen=True, slots=True)
class HttpResult:
    status: int
    payload: Any


def _run(command: list[str], *, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SmokeFailure(f"command unavailable or timed out: {command[0]}") from exc


def _compose_config() -> dict[str, Any]:
    result = _run(["docker", "compose", "config", "--format", "json"])
    if result.returncode != 0:
        raise SmokeFailure("docker compose config failed")
    try:
        config = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise SmokeFailure("docker compose returned invalid configuration") from exc
    if not isinstance(config, dict):
        raise SmokeFailure("docker compose configuration is not an object")
    return config


def _validate_compose_shape(config: dict[str, Any]) -> None:
    services = config.get("services")
    if not isinstance(services, dict) or set(services) != {"migrate", "chat-api"}:
        raise SmokeFailure("Compose must contain only migrate and chat-api")
    migrate = services.get("migrate")
    api = services.get("chat-api")
    if not isinstance(migrate, dict) or not isinstance(api, dict):
        raise SmokeFailure("Compose service definitions are invalid")
    if not isinstance(migrate.get("build"), dict) or "build" in api:
        raise SmokeFailure("only migrate may own the Chat image build")
    depends_on = api.get("depends_on", {})
    migration_dependency = depends_on.get("migrate") if isinstance(depends_on, dict) else None
    if not isinstance(migration_dependency, dict) or migration_dependency.get("condition") != (
        "service_completed_successfully"
    ):
        raise SmokeFailure("chat-api is missing the successful migration barrier")
    networks = config.get("networks")
    default_network = networks.get("default") if isinstance(networks, dict) else None
    if not isinstance(default_network, dict) or default_network.get("external") is not True:
        raise SmokeFailure("Compose default network must be external")
    api_networks = api.get("networks")
    api_default_network = api_networks.get("default") if isinstance(api_networks, dict) else None
    aliases = (
        api_default_network.get("aliases", []) if isinstance(api_default_network, dict) else []
    )
    if "chat-api" not in aliases:
        raise SmokeFailure("chat-api DNS alias is missing")
    ports = api.get("ports", [])
    if not isinstance(ports, list) or not any(
        isinstance(port, dict)
        and port.get("target") == 8120
        and str(port.get("published")) == "8120"
        and port.get("host_ip") == "127.0.0.1"
        for port in ports
    ):
        raise SmokeFailure("Chat API must publish only loopback port 8120")


def _safe_base_url(value: str) -> str:
    normalized = value.strip().rstrip("/")
    try:
        parsed = urlparse(normalized)
        port = parsed.port
    except ValueError as exc:
        raise SmokeFailure("CHAT_SMOKE_BASE_URL is not a valid HTTP URL") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65_535)
    ):
        raise SmokeFailure("CHAT_SMOKE_BASE_URL must be a credential-free HTTP URL")
    if any(ord(character) <= 0x20 or ord(character) == 0x7F for character in normalized):
        raise SmokeFailure("CHAT_SMOKE_BASE_URL contains a control character")
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
    timeout: float = 10.0,
) -> HttpResult:
    payload = None if body is None else json.dumps(body, separators=(",", ":")).encode()
    request_headers = {"Accept": "application/json", **(headers or {})}
    if payload is not None:
        request_headers["Content-Type"] = "application/json"
    request = Request(f"{base_url}{path}", data=payload, method=method, headers=request_headers)
    opener = build_opener(ProxyHandler({}), _NoRedirectHandler())
    try:
        with opener.open(request, timeout=timeout) as response:
            content_length = response.headers.get("Content-Length")
            if (
                content_length is not None
                and content_length.isdigit()
                and int(content_length) > MAX_RESPONSE_BYTES
            ):
                raise SmokeFailure("Chat response exceeded the configured smoke limit")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise SmokeFailure("Chat response exceeded the configured smoke limit")
            return HttpResult(response.status, _decode_body(raw))
    except HTTPError as exc:
        try:
            raw_error = exc.read(MAX_RESPONSE_BYTES + 1)
            payload_error = _decode_body(raw_error[:MAX_RESPONSE_BYTES])
        except OSError:
            payload_error = None
        return HttpResult(exc.code, payload_error)
    except (OSError, URLError) as exc:
        raise SmokeFailure("Chat request could not be reached") from exc


def _expect(result: HttpResult, expected_status: int, operation: str) -> dict[str, Any]:
    if result.status != expected_status or not isinstance(result.payload, dict):
        raise SmokeFailure(f"{operation} returned HTTP {result.status}")
    return result.payload


def _wait_ready(base_url: str) -> None:
    deadline = time.monotonic() + 90
    last_status: int | None = None
    while time.monotonic() < deadline:
        try:
            result = _request(base_url, "GET", "/health/ready", timeout=5)
            last_status = result.status
            if (
                result.status == 200
                and isinstance(result.payload, dict)
                and result.payload.get("status") == "ok"
            ):
                checks = result.payload.get("checks")
                if isinstance(checks, dict) and all(value is True for value in checks.values()):
                    return
        except SmokeFailure:
            time.sleep(2)
            continue
        time.sleep(2)
    raise SmokeFailure(f"Chat readiness did not become healthy (last HTTP {last_status})")


def _admin_headers() -> dict[str, str]:
    return {
        "x-dev-user-id": "chat-smoke-admin",
        "x-dev-group-ids": "all-employees",
        "x-dev-roles": "helpdesk-admin",
    }


def _employee_headers() -> dict[str, str]:
    return {"x-dev-user-id": "chat-smoke-user", "x-dev-group-ids": "all-employees"}


def _purge_exact(base_url: str, conversation_id: UUID, preview_id: UUID | None) -> bool:
    """Best-effort cleanup restricted to one known conversation ID."""

    try:
        headers = _admin_headers()
        selected_preview = preview_id
        if selected_preview is None:
            preview_result = _request(
                base_url,
                "POST",
                "/api/v1/admin/conversation-purge-previews",
                headers=headers,
                body={"conversation_ids": [str(conversation_id)]},
            )
            if preview_result.status != 201 or not isinstance(preview_result.payload, dict):
                return False
            raw_preview_id = preview_result.payload.get("preview_id")
            try:
                selected_preview = UUID(str(raw_preview_id))
            except (TypeError, ValueError):
                return False
        execute = _request(
            base_url,
            "POST",
            "/api/v1/admin/conversation-purges",
            headers=headers,
            body={
                "preview_id": str(selected_preview),
                "reason": "Docker smoke synthetic conversation cleanup",
            },
        )
        return execute.status == 200 and isinstance(execute.payload, dict)
    except Exception:
        return False


def main() -> int:
    """Run the non-generating Docker smoke flow."""

    conversation_id: UUID | None = None
    preview_id: UUID | None = None
    purged = False
    try:
        base_url = _safe_base_url(os.environ.get("CHAT_SMOKE_BASE_URL", "http://127.0.0.1:8120"))
        _validate_compose_shape(_compose_config())
        services = _run(["docker", "compose", "ps", "-a"])
        if services.returncode != 0:
            raise SmokeFailure("docker compose services are not inspectable")
        _wait_ready(base_url)
        live = _expect(_request(base_url, "GET", "/health/live"), 200, "liveness")
        if live.get("status") != "ok":
            raise SmokeFailure("liveness status is not ok")
        missing_identity = _request(base_url, "GET", "/api/v1/conversations")
        if missing_identity.status != 401:
            raise SmokeFailure("missing identity was not rejected")

        created = _expect(
            _request(
                base_url,
                "POST",
                "/api/v1/conversations",
                headers=_employee_headers(),
                body={"title": "Chat Docker Smoke"},
            ),
            201,
            "conversation create",
        )
        try:
            conversation_id = UUID(str(created["conversation_id"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise SmokeFailure("conversation create returned an invalid ID") from exc

        fetched = _expect(
            _request(
                base_url,
                "GET",
                f"/api/v1/conversations/{conversation_id}",
                headers=_employee_headers(),
            ),
            200,
            "conversation get",
        )
        if fetched.get("conversation_id") != str(conversation_id):
            raise SmokeFailure("conversation get returned the wrong ID")

        archived = _expect(
            _request(
                base_url,
                "PATCH",
                f"/api/v1/conversations/{conversation_id}",
                headers=_employee_headers(),
                body={"title": "Chat Docker Smoke Archived", "status": "archived"},
            ),
            200,
            "conversation archive",
        )
        if archived.get("status") != "archived":
            raise SmokeFailure("conversation archive did not persist archived status")

        rejected_turn = _request(
            base_url,
            "POST",
            f"/api/v1/conversations/{conversation_id}/turns",
            headers={**_employee_headers(), "Idempotency-Key": str(uuid4())},
            body={"question": "Synthetic smoke does not call generation."},
        )
        if rejected_turn.status != 409:
            raise SmokeFailure("archived conversation accepted a new turn")

        preview = _expect(
            _request(
                base_url,
                "POST",
                "/api/v1/admin/conversation-purge-previews",
                headers=_admin_headers(),
                body={"conversation_ids": [str(conversation_id)]},
            ),
            201,
            "purge preview",
        )
        try:
            preview_id = UUID(str(preview["preview_id"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise SmokeFailure("purge preview returned an invalid ID") from exc
        _expect(
            _request(
                base_url,
                "POST",
                "/api/v1/admin/conversation-purges",
                headers=_admin_headers(),
                body={
                    "preview_id": str(preview_id),
                    "reason": "Docker smoke synthetic conversation cleanup",
                },
            ),
            200,
            "purge execute",
        )
        purged = True
        if (
            _request(
                base_url,
                "GET",
                f"/api/v1/conversations/{conversation_id}",
                headers=_employee_headers(),
            ).status
            != 404
        ):
            raise SmokeFailure("purged conversation remained visible")
        metrics = _request(base_url, "GET", "/metrics")
        if metrics.status != 200:
            raise SmokeFailure("metrics endpoint did not respond")
        print("SMOKE TEST PASSED")
        return 0
    except SmokeFailure as exc:
        print(f"SMOKE TEST FAILED: {exc}", file=sys.stderr)
        return 1
    except Exception:
        print("SMOKE TEST FAILED: unexpected smoke-test error", file=sys.stderr)
        return 1
    finally:
        if conversation_id is not None and not purged:
            if not _purge_exact(base_url, conversation_id, preview_id):
                print("SMOKE CLEANUP FAILED: synthetic conversation may remain", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
