"""Phase 7 host-only preflight and Fake Provider safety checks."""

from __future__ import annotations

import io
import json
import os
import subprocess
from collections.abc import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from scripts import chat_e2e_test, local_runtime_checks, preflight

NETWORK_NAME = "helpdesk-rag"


def _completed(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["docker"], returncode=returncode, stdout=stdout, stderr="private docker error"
    )


def _container_details(
    *,
    provider: str = "fake",
    usage_mode: str = "test",
    network: str = NETWORK_NAME,
    aliases: list[str] | None = None,
    running: bool = True,
    extra_environment: list[str] | None = None,
) -> dict[str, object]:
    environment = [
        f"LLM_PROVIDER={provider}",
        f"LLM_PROVIDER_USAGE_MODE={usage_mode}",
        "NVIDIA_API_KEY=private-provider-key",
        *(extra_environment or []),
    ]
    return {
        "Config": {
            "Labels": {"com.docker.compose.service": "answer-generation-api"},
            "Env": environment,
        },
        "State": {"Running": running},
        "NetworkSettings": {
            "Networks": {network: {"Aliases": aliases or ["answer-generation-api"]}}
        },
    }


def _docker_runner(
    details_by_id: dict[str, dict[str, object]],
    *,
    network_name: str = NETWORK_NAME,
) -> Callable[..., subprocess.CompletedProcess[str]]:
    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if command[1:4] == ["compose", "config", "--format"]:
            return _completed(
                json.dumps({"networks": {"default": {"external": True, "name": network_name}}})
            )
        if command[1:3] == ["network", "inspect"]:
            return _completed(json.dumps([{"Name": network_name}]))
        if command[1] == "ps":
            return _completed("\n".join(details_by_id))
        if command[1] == "inspect":
            container_id = command[2]
            return _completed(json.dumps([details_by_id[container_id]]))
        raise AssertionError(f"unexpected docker command: {command[1:]}")

    return run


def test_fake_provider_runtime_check_accepts_one_running_attached_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    details = {"a1": _container_details()}
    monkeypatch.setattr(local_runtime_checks, "_run_docker", _docker_runner(details))

    local_runtime_checks.require_fake_answer_provider()


@pytest.mark.parametrize(
    "details",
    [
        {"a1": _container_details(provider="nvidia", extra_environment=["API_KEY=secret"])},
        {"a1": _container_details(usage_mode="production")},
        {"a1": _container_details(aliases=["other-service"])},
    ],
)
def test_fake_provider_runtime_check_fails_closed_without_leaking_environment(
    monkeypatch: pytest.MonkeyPatch,
    details: dict[str, dict[str, object]],
) -> None:
    monkeypatch.setattr(local_runtime_checks, "_run_docker", _docker_runner(details))

    with pytest.raises(local_runtime_checks.RuntimeCheckFailure) as raised:
        local_runtime_checks.require_fake_answer_provider()

    assert "secret" not in str(raised.value)
    assert "private-provider-key" not in str(raised.value)


def test_fake_provider_runtime_check_rejects_ambiguous_running_containers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    details = {"a1": _container_details(), "a2": _container_details()}
    monkeypatch.setattr(local_runtime_checks, "_run_docker", _docker_runner(details))

    with pytest.raises(local_runtime_checks.RuntimeCheckFailure):
        local_runtime_checks.require_fake_answer_provider()


class _Response:
    def __init__(self, status: int, body: bytes, content_type: str = "application/json") -> None:
        self.status = status
        self.headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(body)),
        }
        self._body = body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self, amount: int = -1) -> bytes:
        return self._body if amount < 0 else self._body[:amount]


class _Opener:
    def __init__(self, action: _Response | BaseException) -> None:
        self.action = action
        self.request: Request | None = None

    def open(self, request: Request, *, timeout: float) -> _Response:
        del timeout
        self.request = request
        if isinstance(self.action, BaseException):
            raise self.action
        return self.action


def test_preflight_kis_auth_requires_json_200_and_sends_key_only_in_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opener = _Opener(_Response(200, b'{"status":"ok"}'))
    monkeypatch.setattr(preflight, "build_opener", lambda *_: opener)

    preflight._check_kis_auth("http://127.0.0.1:8080", "test-service-key")

    assert opener.request is not None
    assert opener.request.full_url == "http://127.0.0.1:8080/api/v1/auth/check"
    assert opener.request.get_method() == "GET"
    assert any(
        name.lower() == "x-internal-api-key" and value == "test-service-key"
        for name, value in opener.request.header_items()
    )


@pytest.mark.parametrize(
    "action",
    [
        HTTPError(
            "http://127.0.0.1:8080/api/v1/auth/check",
            401,
            "unauthorized",
            {},
            io.BytesIO(b"private raw error body"),
        ),
        HTTPError(
            "http://127.0.0.1:8080/api/v1/auth/check",
            403,
            "forbidden",
            {},
            io.BytesIO(b"private raw error body"),
        ),
        HTTPError(
            "http://127.0.0.1:8080/api/v1/auth/check",
            302,
            "redirect",
            {"Location": "https://unexpected.example"},
            io.BytesIO(b"private redirect body"),
        ),
        URLError("private connection detail"),
        TimeoutError("private timeout detail"),
        _Response(200, b"not-json"),
        _Response(200, b'{"status":"ok"}', content_type="text/plain"),
    ],
)
def test_preflight_kis_auth_rejects_auth_errors_and_malformed_responses(
    monkeypatch: pytest.MonkeyPatch,
    action: _Response | BaseException,
) -> None:
    opener = _Opener(action)
    monkeypatch.setattr(preflight, "build_opener", lambda *_: opener)

    with pytest.raises(preflight.PreflightFailure) as raised:
        preflight._check_kis_auth("http://127.0.0.1:8080", "private-service-key")

    assert "private" not in str(raised.value)


def test_preflight_kis_auth_rejects_oversized_content_length(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _Response(200, b'{"status":"ok"}')
    response.headers["Content-Length"] = str(preflight.MAX_HTTP_BODY_BYTES + 1)
    opener = _Opener(response)
    monkeypatch.setattr(preflight, "build_opener", lambda *_: opener)

    with pytest.raises(preflight.PreflightFailure, match="body limit"):
        preflight._check_kis_auth("http://127.0.0.1:8080", "test-service-key")


def test_preflight_opt_out_does_not_call_kis_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preflight, "_read_env_file", lambda _: {"CHAT_E2E_ALLOW_KIS_WRITES": "0"})
    monkeypatch.setattr(
        preflight,
        "_check_kis_auth",
        lambda *_: pytest.fail("KIS auth must not run while E2E writes are disabled"),
    )

    preflight._check_e2e_configuration()


def test_preflight_opt_in_checks_kis_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        preflight,
        "_read_env_file",
        lambda _: {
            "CHAT_E2E_ALLOW_KIS_WRITES": "1",
            "CHAT_E2E_KIS_API_KEY": "test-service-key",
            "CHAT_E2E_KIS_BASE_URL": "http://127.0.0.1:8080",
        },
    )
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(preflight, "_check_kis_auth", lambda url, key: calls.append((url, key)))

    preflight._check_e2e_configuration()

    assert calls == [("http://127.0.0.1:8080", "test-service-key")]


def test_preflight_opt_in_rejects_blank_kis_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        preflight,
        "_read_env_file",
        lambda _: {
            "CHAT_E2E_ALLOW_KIS_WRITES": "1",
            "CHAT_E2E_KIS_API_KEY": "  ",
        },
    )

    with pytest.raises(preflight.PreflightFailure):
        preflight._check_e2e_configuration()


def test_fake_provider_runtime_check_rejects_missing_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(local_runtime_checks, "_run_docker", _docker_runner({}))

    with pytest.raises(local_runtime_checks.RuntimeCheckFailure):
        local_runtime_checks.require_fake_answer_provider()


def test_fake_provider_inspect_failure_does_not_emit_private_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    base_runner = _docker_runner({"a1": _container_details()})

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[1] == "inspect":
            return _completed("private docker stdout", returncode=1)
        return base_runner(command, **kwargs)

    monkeypatch.setattr(local_runtime_checks, "_run_docker", run)

    with pytest.raises(local_runtime_checks.RuntimeCheckFailure):
        local_runtime_checks.require_fake_answer_provider()

    captured = capsys.readouterr()
    assert "private docker stdout" not in captured.out
    assert "private docker error" not in captured.err


def test_e2e_fake_provider_failure_precedes_kis_write_and_chat_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        chat_e2e_test,
        "_read_env_file",
        lambda _: {
            "CHAT_E2E_ALLOW_KIS_WRITES": "1",
            "CHAT_E2E_KIS_API_KEY": "test-service-key",
            "CHAT_E2E_KIS_BASE_URL": "http://127.0.0.1:8080",
            "CHAT_E2E_CHAT_BASE_URL": "http://127.0.0.1:8120",
            "CHAT_E2E_LOCK_TIMEOUT_SECONDS": "60",
            "CHAT_E2E_JOB_TIMEOUT_SECONDS": "600",
            "CHAT_E2E_TURN_TIMEOUT_SECONDS": "180",
        },
    )
    descriptor = os.open(os.devnull, os.O_RDWR)
    monkeypatch.setattr(chat_e2e_test, "_acquire_lock", lambda _: descriptor)
    monkeypatch.setattr(chat_e2e_test, "_release_lock", os.close)
    monkeypatch.setattr(
        chat_e2e_test,
        "require_fake_answer_provider",
        lambda *_: (_ for _ in ()).throw(
            local_runtime_checks.RuntimeCheckFailure("fake provider is required")
        ),
    )
    monkeypatch.setattr(
        chat_e2e_test,
        "_check_kis_auth",
        lambda *_: pytest.fail("KIS auth must not run after the provider check fails"),
    )
    monkeypatch.setattr(
        chat_e2e_test,
        "_ensure_fixture",
        lambda *_: pytest.fail("KIS fixture write must not run after the provider check fails"),
    )
    monkeypatch.setattr(
        chat_e2e_test,
        "_request",
        lambda *_args, **_kwargs: pytest.fail(
            "Chat calls must not run after the provider check fails"
        ),
    )

    assert chat_e2e_test.main() == 1
