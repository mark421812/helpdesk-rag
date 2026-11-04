"""Read-only checks for the local Help Desk service dependency graph.

The preflight command intentionally does not create data, call KIS write
endpoints, or print configuration values.  It is safe to run before every
local Chat Service startup.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

if TYPE_CHECKING:
    from scripts.local_runtime_checks import RuntimeCheckFailure, require_fake_answer_provider
else:
    _runtime_checks = importlib.import_module(
        "local_runtime_checks" if __package__ in {None, ""} else "scripts.local_runtime_checks"
    )
    RuntimeCheckFailure = _runtime_checks.RuntimeCheckFailure
    require_fake_answer_provider = _runtime_checks.require_fake_answer_provider

PROJECT_ROOT = Path(__file__).resolve().parents[1]

DEFAULTS = {
    "CHAT_PREFLIGHT_KIS_BASE_URL": "http://127.0.0.1:8080",
    "CHAT_PREFLIGHT_RETRIEVAL_BASE_URL": "http://127.0.0.1:8100",
    "CHAT_PREFLIGHT_ANSWER_BASE_URL": "http://127.0.0.1:8110",
}
MAX_HTTP_BODY_BYTES = 64_000


class PreflightFailure(RuntimeError):
    """A safe, user-facing preflight failure without raw command output."""


class _NoRedirectHandler(HTTPRedirectHandler):
    """Do not follow a dependency redirect to an unexpected host."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _read_env_file(path: Path) -> dict[str, str]:
    """Read simple KEY=VALUE entries without evaluating shell syntax."""

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


def _setting(name: str, env_file: dict[str, str] | None = None) -> str:
    """Resolve process environment first, then an optional local env file."""

    if name in os.environ:
        return os.environ[name]
    if env_file is not None and name in env_file:
        return env_file[name]
    return DEFAULTS.get(name, "")


def _run(command: list[str], *, timeout: float = 15.0) -> subprocess.CompletedProcess[str]:
    """Run a local diagnostic command while retaining output only in memory."""

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
        raise PreflightFailure(f"command unavailable or timed out: {command[0]}") from exc


def _compose_config() -> dict[str, Any]:
    result = _run(["docker", "compose", "config", "--format", "json"])
    if result.returncode != 0:
        raise PreflightFailure("docker compose config failed")
    try:
        config = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PreflightFailure("docker compose returned invalid configuration") from exc
    if not isinstance(config, dict):
        raise PreflightFailure("docker compose configuration is not an object")
    return config


def _check_external_network(config: dict[str, Any]) -> str:
    networks = config.get("networks")
    default_network = networks.get("default") if isinstance(networks, dict) else None
    if not isinstance(default_network, dict) or default_network.get("external") is not True:
        raise PreflightFailure("Compose default network must be an existing external network")
    network_name = default_network.get("name")
    if not isinstance(network_name, str) or not network_name.strip():
        raise PreflightFailure("Compose external network has no valid name")
    result = _run(["docker", "network", "inspect", network_name])
    if result.returncode != 0:
        raise PreflightFailure("external helpdesk network is not available")
    return network_name


def _check_postgres() -> None:
    result = _run(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            "label=com.docker.compose.service=postgres",
            "--format",
            "{{.ID}}",
        ]
    )
    if result.returncode != 0:
        raise PreflightFailure("cannot inspect the PostgreSQL container")
    container_ids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not container_ids:
        raise PreflightFailure("PostgreSQL 16 container was not found")

    for container_id in container_ids:
        inspect = _run(["docker", "inspect", container_id])
        if inspect.returncode != 0:
            continue
        try:
            payload = json.loads(inspect.stdout)
            details = payload[0]
            image = str(details.get("Config", {}).get("Image", ""))
            health = details.get("State", {}).get("Health", {}).get("Status")
            running = details.get("State", {}).get("Running") is True
        except (IndexError, KeyError, TypeError, json.JSONDecodeError):
            continue
        if "postgres:16" in image and running and health == "healthy":
            return
    raise PreflightFailure("PostgreSQL 16 container is not running and healthy")


def _safe_http_url(value: str, setting_name: str) -> str:
    normalized = value.strip().rstrip("/")
    try:
        parsed = urlparse(normalized)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise PreflightFailure(f"{setting_name} is not a valid HTTP URL") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65_535)
    ):
        raise PreflightFailure(f"{setting_name} must be a credential-free HTTP URL")
    if any(ord(character) <= 0x20 or ord(character) == 0x7F for character in normalized):
        raise PreflightFailure(f"{setting_name} contains a control character")
    return normalized


def _check_ready(base_url: str, setting_name: str) -> None:
    url = f"{_safe_http_url(base_url, setting_name)}/health/ready"
    request = Request(url, method="GET", headers={"Accept": "application/json"})
    opener = build_opener(ProxyHandler({}), _NoRedirectHandler())
    try:
        with opener.open(request, timeout=5) as response:
            status = response.status
            # The body is bounded and parsed only to verify the stable status field.  It is
            # deliberately never printed because a dependency may include diagnostics.
            if "application/json" not in response.headers.get("Content-Type", "").lower():
                raise PreflightFailure(
                    f"{setting_name} readiness returned an unexpected content type"
                )
            content_length = response.headers.get("Content-Length")
            if content_length is not None and (
                not content_length.isdigit() or int(content_length) > MAX_HTTP_BODY_BYTES
            ):
                raise PreflightFailure(f"{setting_name} readiness response exceeded the body limit")
            body = response.read(MAX_HTTP_BODY_BYTES + 1)
    except HTTPError as exc:
        raise PreflightFailure(f"{setting_name} readiness returned HTTP {exc.code}") from exc
    except (OSError, URLError) as exc:
        raise PreflightFailure(f"{setting_name} readiness could not be reached") from exc
    if status != 200:
        raise PreflightFailure(f"{setting_name} readiness returned HTTP {status}")
    if len(body) > MAX_HTTP_BODY_BYTES:
        raise PreflightFailure(f"{setting_name} readiness response exceeded the body limit")
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreflightFailure(f"{setting_name} readiness returned invalid JSON") from exc
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        raise PreflightFailure(f"{setting_name} readiness is not ok")


def _safe_local_http_url(value: str, setting_name: str) -> str:
    """Restrict host-only E2E dependency calls to loopback addresses."""

    normalized = _safe_http_url(value, setting_name)
    hostname = urlparse(normalized).hostname
    if hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise PreflightFailure(f"{setting_name} must be a host-local HTTP URL")
    return normalized


def _check_kis_auth(kis_url: str, key: str) -> None:
    """Verify the host-only KIS key before an opted-in E2E write can occur."""

    if not key.strip() or any(ord(character) < 0x21 or ord(character) == 0x7F for character in key):
        raise PreflightFailure("E2E KIS service key is invalid")
    url = f"{_safe_local_http_url(kis_url, 'CHAT_E2E_KIS_BASE_URL')}/api/v1/auth/check"
    request = Request(
        url,
        method="GET",
        headers={"Accept": "application/json", "x-internal-api-key": key},
    )
    opener = build_opener(ProxyHandler({}), _NoRedirectHandler())
    try:
        with opener.open(request, timeout=10) as response:
            if "application/json" not in response.headers.get("Content-Type", "").lower():
                raise PreflightFailure("KIS authentication returned an unexpected content type")
            content_length = response.headers.get("Content-Length")
            if content_length is not None and (
                not content_length.isdigit() or int(content_length) > MAX_HTTP_BODY_BYTES
            ):
                raise PreflightFailure("KIS authentication response exceeded the body limit")
            body = response.read(MAX_HTTP_BODY_BYTES + 1)
            status = response.status
    except HTTPError as exc:
        raise PreflightFailure(f"KIS authentication returned HTTP {exc.code}") from exc
    except (OSError, URLError) as exc:
        raise PreflightFailure("KIS authentication could not be reached") from exc
    if len(body) > MAX_HTTP_BODY_BYTES:
        raise PreflightFailure("KIS authentication response exceeded the body limit")
    if status != 200:
        raise PreflightFailure(f"KIS authentication returned HTTP {status}")
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreflightFailure("KIS authentication returned invalid JSON") from exc
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        raise PreflightFailure("KIS service-key authentication check failed")


def _check_e2e_configuration() -> None:
    values = _read_env_file(PROJECT_ROOT / ".env.e2e")
    allow = os.environ.get(
        "CHAT_E2E_ALLOW_KIS_WRITES", values.get("CHAT_E2E_ALLOW_KIS_WRITES", "0")
    )
    key = os.environ.get("CHAT_E2E_KIS_API_KEY", values.get("CHAT_E2E_KIS_API_KEY", ""))
    if allow.strip() not in {"0", "false", "False", "no", "off", ""}:
        if allow.strip() != "1":
            raise PreflightFailure("CHAT_E2E_ALLOW_KIS_WRITES must be 0 or 1")
        if not key.strip():
            raise PreflightFailure("E2E opt-in requires a non-empty KIS service key")
        if any(ord(character) < 0x21 or ord(character) == 0x7F for character in key):
            raise PreflightFailure("E2E KIS service key contains an invalid character")
        kis_url = os.environ.get(
            "CHAT_E2E_KIS_BASE_URL",
            values.get("CHAT_E2E_KIS_BASE_URL", DEFAULTS["CHAT_PREFLIGHT_KIS_BASE_URL"]),
        )
        _check_kis_auth(kis_url, key)
        print("E2E opt-in: enabled (service key present but hidden)")
        return
    print("E2E opt-in: disabled; no KIS writes will be attempted")


def main() -> int:
    """Run every read-only dependency and opt-in check."""

    env_file = _read_env_file(PROJECT_ROOT / ".env")
    try:
        config = _compose_config()
        network_name = _check_external_network(config)
        try:
            require_fake_answer_provider(PROJECT_ROOT, expected_network=network_name)
        except RuntimeCheckFailure as exc:
            raise PreflightFailure(
                "Answer provider runtime is not the offline fake test provider"
            ) from exc
        _check_postgres()
        _check_ready(_setting("CHAT_PREFLIGHT_KIS_BASE_URL", env_file), "KIS")
        _check_ready(_setting("CHAT_PREFLIGHT_RETRIEVAL_BASE_URL", env_file), "Retrieval")
        _check_ready(_setting("CHAT_PREFLIGHT_ANSWER_BASE_URL", env_file), "Answer")
        _check_e2e_configuration()
    except PreflightFailure as exc:
        print(f"PREFLIGHT FAILED: {exc}", file=sys.stderr)
        return 1
    print("PREFLIGHT PASSED: network, PostgreSQL 16, KIS, Retrieval, and Answer are ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
