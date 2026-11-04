"""Host-only checks for the local Docker dependency runtime.

The checks in this module deliberately inspect only Docker metadata.  They do
not print container environment values because that metadata can contain API
keys or other secrets.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_CONTAINER_ID_RE = re.compile(r"^[0-9a-fA-F]+$")


class RuntimeCheckFailure(RuntimeError):
    """A safe failure for a Docker runtime precondition."""


def _run_docker(
    command: list[str],
    *,
    project_root: Path,
    timeout: float = 15.0,
) -> subprocess.CompletedProcess[str]:
    """Run a Docker command while retaining stdout/stderr only in memory."""

    try:
        return subprocess.run(
            command,
            cwd=project_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeCheckFailure("Docker runtime inspection was unavailable or timed out") from exc


def _compose_network_name(project_root: Path) -> str:
    """Return the explicitly configured external Compose network name."""

    result = _run_docker(
        ["docker", "compose", "config", "--format", "json"], project_root=project_root
    )
    if result.returncode != 0:
        raise RuntimeCheckFailure("Docker Compose configuration could not be inspected")
    try:
        config = json.loads(result.stdout)
        networks = config.get("networks")
        default_network = networks.get("default") if isinstance(networks, dict) else None
        network_name = default_network.get("name") if isinstance(default_network, dict) else None
        is_external = default_network.get("external") if isinstance(default_network, dict) else None
    except (AttributeError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeCheckFailure("Docker Compose configuration was invalid") from exc
    if is_external is not True or not isinstance(network_name, str) or not network_name.strip():
        raise RuntimeCheckFailure("Docker Compose must use an existing external helpdesk network")
    return network_name.strip()


def _inspect_network(network_name: str, *, project_root: Path) -> None:
    """Ensure the configured external network exists without exposing metadata."""

    result = _run_docker(["docker", "network", "inspect", network_name], project_root=project_root)
    if result.returncode != 0:
        raise RuntimeCheckFailure("The external helpdesk network is unavailable")
    try:
        payload = json.loads(result.stdout)
        if (
            not isinstance(payload, list)
            or len(payload) != 1
            or not isinstance(payload[0], dict)
            or payload[0].get("Name") != network_name
        ):
            raise ValueError
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeCheckFailure("The external helpdesk network metadata was invalid") from exc


def _container_ids(*, project_root: Path) -> list[str]:
    """Find running Answer containers using Docker's service label."""

    result = _run_docker(
        [
            "docker",
            "ps",
            "--filter",
            "label=com.docker.compose.service=answer-generation-api",
            "--filter",
            "status=running",
            "--format",
            "{{.ID}}",
        ],
        project_root=project_root,
    )
    if result.returncode != 0:
        raise RuntimeCheckFailure("Answer container status could not be inspected")
    ids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not ids or any(_CONTAINER_ID_RE.fullmatch(container_id) is None for container_id in ids):
        raise RuntimeCheckFailure("A unique running Answer container was not found")
    return ids


def _load_container(container_id: str, *, project_root: Path) -> dict[str, Any]:
    """Read one container's JSON metadata, never returning raw command errors."""

    result = _run_docker(["docker", "inspect", container_id], project_root=project_root)
    if result.returncode != 0:
        raise RuntimeCheckFailure("Answer container metadata could not be inspected")
    try:
        payload = json.loads(result.stdout)
        details = payload[0]
    except (IndexError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeCheckFailure("Answer container metadata was invalid") from exc
    if not isinstance(details, dict):
        raise RuntimeCheckFailure("Answer container metadata was invalid")
    return details


def _is_answer_on_network(details: dict[str, Any], network_name: str) -> bool:
    """Identify the intended running service attached to the exact network."""

    config = details.get("Config")
    state = details.get("State")
    network_settings = details.get("NetworkSettings")
    labels = config.get("Labels") if isinstance(config, dict) else None
    networks = network_settings.get("Networks") if isinstance(network_settings, dict) else None
    if (
        not isinstance(labels, dict)
        or labels.get("com.docker.compose.service") != "answer-generation-api"
        or not isinstance(state, dict)
        or state.get("Running") is not True
        or not isinstance(networks, dict)
    ):
        return False
    network = networks.get(network_name)
    if not isinstance(network, dict):
        return False
    aliases = network.get("Aliases")
    return isinstance(aliases, list) and "answer-generation-api" in aliases


def _provider_environment(details: dict[str, Any]) -> dict[str, str]:
    """Parse environment entries while keeping values private to this module."""

    config = details.get("Config")
    raw_environment = config.get("Env") if isinstance(config, dict) else None
    if not isinstance(raw_environment, list):
        raise RuntimeCheckFailure("Answer container environment metadata was invalid")
    environment: dict[str, str] = {}
    for entry in raw_environment:
        if not isinstance(entry, str) or "=" not in entry:
            raise RuntimeCheckFailure("Answer container environment metadata was invalid")
        key, value = entry.split("=", 1)
        if not key or key in environment:
            raise RuntimeCheckFailure("Answer container environment metadata was invalid")
        environment[key] = value
    return environment


def require_fake_answer_provider(
    project_root: Path = PROJECT_ROOT,
    *,
    expected_network: str | None = None,
) -> None:
    """Require one running Answer container configured for the offline test provider.

    Any Docker error, ambiguous service selection, missing network attachment,
    or non-test provider configuration fails closed.  No environment value is
    included in an exception or printed to the terminal.
    """

    network_name = expected_network or _compose_network_name(project_root)
    if not isinstance(network_name, str) or not network_name.strip():
        raise RuntimeCheckFailure("The external helpdesk network name was invalid")
    _inspect_network(network_name, project_root=project_root)
    candidates: list[dict[str, Any]] = []
    for container_id in _container_ids(project_root=project_root):
        details = _load_container(container_id, project_root=project_root)
        if _is_answer_on_network(details, network_name):
            candidates.append(details)
    if len(candidates) != 1:
        raise RuntimeCheckFailure("Answer provider runtime selection was ambiguous or unavailable")
    environment = _provider_environment(candidates[0])
    if (
        environment.get("LLM_PROVIDER") != "fake"
        or environment.get("LLM_PROVIDER_USAGE_MODE") != "test"
    ):
        raise RuntimeCheckFailure("Answer provider must be the offline fake test provider")
