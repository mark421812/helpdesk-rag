"""U0 opt-in 隔離 KIS persistence audit；絕不連正式 DB/Qdrant 或外部模型。

重用 local immutable images，只讀模型 cache；全新 internal network、tmpfs DB，
沒有 host ports。API/Worker 分時運行。只對已取得的 exact ID + 本次 label 清理。
不繼承任何正式 Env/key，不 pull/build，不 stop/restart 現有服務，不寫 corpus。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from audit_parsers import MODEL_ID, MODEL_REVISION
from docker_parser_audit import CACHE_PATH, CONTAINER_ROOT, cache_volume
from runner import validate

ROOT = Path(__file__).resolve().parent
LABEL = "helpdesk.u0.isolated-run"
ID = re.compile(r"[0-9a-f]{64}")
IMAGE = re.compile(r"sha256:[0-9a-f]{64}")


class AuditFailure(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def command(
    *args: str,
    incoming: str | None = None,
    timeout: int = 30,
    limit: int = 2_000_000,
    allowed_codes: tuple[int, ...] = (0,),
) -> str:
    result = subprocess.run(
        ["docker", *args],
        input=incoming,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    # 不公開 stderr / argv：它們可能包含 generated test secret 或 payload。
    if result.returncode not in allowed_codes or len(result.stdout.encode()) > limit:
        raise AuditFailure("docker_command_failed")
    return result.stdout.strip()


def image_for(project: str, service: str) -> dict:
    ids = command(
        "ps",
        "-q",
        "--filter",
        f"label=com.docker.compose.project={project}",
        "--filter",
        f"label=com.docker.compose.service={service}",
    ).split()
    if len(ids) != 1:
        raise AuditFailure("image_source_unavailable")
    # 沒有 Config.Env；不讀取任何正式服務 secret。
    metadata = json.loads(
        command(
            "inspect",
            "--format",
            '{"image_id":{{json .Image}},"user":{{json .Config.User}},'
            '"mounts":{{json .Mounts}}}',
            ids[0],
        )
    )
    if not IMAGE.fullmatch(metadata["image_id"]):
        raise AuditFailure("immutable_image_required")
    return metadata


def owned(metadata: dict, run_id: str) -> bool:
    """刪除/停止前只信 inspect exact ID 與 run label，名稱或 wildcard 不算授權。"""
    return metadata.get("labels", {}).get(LABEL) == run_id


def base_flags(run_id: str, role: str, memory: int, network: str) -> list[str]:
    if not re.fullmatch(r"[a-f0-9]{32}", run_id) or not re.fullmatch(r"[a-z-]+", role):
        raise AuditFailure("invalid_run_identity")
    if network != "none" and not ID.fullmatch(network):
        raise AuditFailure("exact_network_id_required")
    return [
        "--name",
        f"rag-u0-kis-{run_id}-{role}",
        "--label",
        f"{LABEL}={run_id}",
        "--pull=never",
        "--network",
        network,
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--no-healthcheck",
        "--log-driver=none",
        "--restart=no",
        f"--memory={memory}m",
        "--cpus=1",
        "--pids-limit=256",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=64m,mode=1777",
    ]


class Resources:
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.network: str | None = None
        self.containers: list[str] = []
        self.audit: list[dict] = []

    def inspect(self, container_id: str) -> dict:
        if container_id not in self.containers or not ID.fullmatch(container_id):
            raise AuditFailure("unowned_resource")
        value = json.loads(
            command(
                "inspect",
                "--format",
                '{"labels":{{json .Config.Labels}},"running":{{json .State.Running}},'
                '"oom":{{json .State.OOMKilled}},"exit":{{json .State.ExitCode}},'
                '"user":{{json .Config.User}},'
                '"readonly":{{json .HostConfig.ReadonlyRootfs}},'
                '"ports":{{json .HostConfig.PortBindings}},"mounts":{{json .Mounts}},'
                '"network_mode":{{json .HostConfig.NetworkMode}},'
                '"log_driver":{{json .HostConfig.LogConfig.Type}},'
                '"networks":{{json .NetworkSettings.Networks}}}',
                container_id,
            )
        )
        if not owned(value, self.run_id):
            raise AuditFailure("ownership_label_mismatch")
        if value["running"] and value["network_mode"] != "none":
            attached = [n["NetworkID"] for n in value["networks"].values()]
            if attached != [self.network]:
                raise AuditFailure("unexpected_runtime_network")
        return value

    def create(
        self,
        role: str,
        image: str,
        memory: int,
        args: list[str],
        invocation: list[str],
        *,
        network: str | None = None,
        user: str = "app",
        stdin: bool = False,
    ) -> str:
        if not IMAGE.fullmatch(image) or user.split(":")[0] in ("0", "root", ""):
            raise AuditFailure("image_or_user_invalid")
        net = network or self.network
        if net is None:
            raise AuditFailure("network_required")
        flags = base_flags(self.run_id, role, memory, net)
        if stdin:
            flags.append("--interactive")
        container_id = command(
            "create", *flags, "--user", user, *args, image, *invocation
        )
        if not ID.fullmatch(container_id):
            raise AuditFailure("container_id_invalid")
        self.containers.append(container_id)
        state = self.inspect(container_id)
        if (
            not state["readonly"]
            or state["ports"]
            or state["log_driver"] != "none"
            or state["user"].split(":")[0] in ("0", "root")
        ):
            raise AuditFailure("container_isolation_failed")
        for mount in state["mounts"]:
            if mount["Type"] in ("volume", "bind") and mount["RW"]:
                raise AuditFailure("persistent_write_mount_forbidden")
        # create 尚未建立 endpoint，NetworkID 可能空白；先驗證精確 NetworkMode。
        # running 時 inspect 會再次驗證真正 attached NetworkID。
        if state["network_mode"] != net:
            raise AuditFailure("unexpected_network")
        self.audit.append(
            {
                "role": role,
                "container_id": container_id,
                "image_id": image,
                "user": state["user"],
                "read_only": True,
                "host_ports": 0,
                "log_driver": state["log_driver"],
                "memory_limit_mib": memory,
                "network": net,
            }
        )
        return container_id

    def start(self, container_id: str) -> None:
        self.inspect(container_id)
        command("start", container_id)
        self.inspect(container_id)

    def stop(self, container_id: str) -> None:
        state = self.inspect(container_id)
        if state["running"]:
            command("stop", "--time", "15", container_id, timeout=30)
        state = self.inspect(container_id)
        if state["oom"]:
            raise AuditFailure("isolated_container_oom")

    def run(
        self,
        container_id: str,
        incoming: dict | None = None,
        timeout: int = 240,
        *,
        expect_json: bool = True,
    ) -> dict:
        self.inspect(container_id)
        flags = ["start", "--attach"]
        if incoming is not None:
            flags.append("--interactive")
        outgoing = command(
            *flags,
            container_id,
            incoming=json.dumps(incoming, ensure_ascii=False) if incoming else None,
            timeout=timeout,
            allowed_codes=(0, 2) if expect_json else (0,),
        )
        state = self.inspect(container_id)
        if not expect_json:
            if state["oom"] or state["exit"] != 0:
                raise AuditFailure("isolated_process_failed")
            return {"exit_code": state["exit"]}
        value = json.loads(outgoing)
        if value.get("status") == "BLOCKED":
            check = value.get("check") or "isolated_probe_blocked"
            raise AuditFailure(
                check if re.fullmatch(r"[a-z_]+", check) else "isolated_probe_blocked"
            )
        if state["oom"] or state["exit"] != 0:
            raise AuditFailure("isolated_process_failed")
        return value

    def cleanup(self) -> list[str]:
        failures = []
        for container_id in reversed(self.containers):
            try:
                self.inspect(container_id)
                # 測試 tmpfs 已授權可丟棄；不使用 -v、不移除 cache volume。
                command("rm", "--force", container_id)
            except (OSError, ValueError, AuditFailure, subprocess.TimeoutExpired):
                failures.append(container_id)
        if self.network:
            try:
                meta = json.loads(
                    command(
                        "network",
                        "inspect",
                        "--format",
                        '{"labels":{{json .Labels}},"containers":{{json .Containers}}}',
                        self.network,
                    )
                )
                if not owned(meta, self.run_id) or meta["containers"]:
                    raise AuditFailure("network_cleanup_not_safe")
                command("network", "rm", self.network)
            except (OSError, ValueError, AuditFailure, subprocess.TimeoutExpired):
                failures.append(self.network)
        return failures

    def failure_diagnostics(self) -> list[str]:
        """只讀 OOM 狀態；所有測試容器 log driver 停用，不讀 raw log。"""
        categories = set()
        for container_id in self.containers:
            try:
                state = self.inspect(container_id)
                if state["oom"]:
                    categories.add("oom_killed")
            except (OSError, ValueError, AuditFailure, subprocess.TimeoutExpired):
                categories.add("diagnostic_unavailable")
        return sorted(categories)


def environment(run_id: str) -> dict[str, str]:
    return {
        "U0_RUN_ID": run_id,
        "APP_ENV": "production",
        "API_INTERNAL_KEY": secrets.token_hex(24),
        "QDRANT_API_KEY": secrets.token_hex(24),
        "DATABASE_URL": f"postgresql+asyncpg://u0_only:{secrets.token_hex(24)}@postgres:5432/rag_u0",
        "DATABASE_POOL_SIZE": "2",
        "DATABASE_MAX_OVERFLOW": "0",
        "QDRANT_URL": "http://qdrant:6333",
        "QDRANT_COLLECTION": "rag_u0_fixture",
        "MODEL_ID": MODEL_ID,
        "MODEL_REVISION": MODEL_REVISION,
        "MODEL_DEVICE": "cpu",
        "TORCH_NUM_THREADS": "1",
        "HF_HOME": CACHE_PATH,
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "LOG_LEVEL": "WARNING",
    }


def env_flags(settings: dict[str, str]) -> list[str]:
    return [
        part for key, value in settings.items() for part in ("--env", f"{key}={value}")
    ]


def mounted_tools() -> list[str]:
    if "," in str(ROOT):
        raise AuditFailure("unsupported_bind_path")
    return ["--mount", f"type=bind,source={ROOT},target={CONTAINER_ROOT},readonly"]


def cache_mount(metadata: dict) -> list[str]:
    return [
        "--mount",
        f"type=volume,source={cache_volume(metadata)},target={CACHE_PATH},readonly",
    ]


def stage(name: str) -> None:
    print(json.dumps({"stage": name}), flush=True)


def execute(resources: Resources, report: dict) -> None:
    validate()
    # 避免意外指向遠端 daemon。只接受本機 Unix socket，不讀 config secrets。
    endpoint = os.environ.get("DOCKER_HOST") or command(
        "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"
    )
    if not endpoint.startswith("unix:///"):
        raise AuditFailure("local_docker_required")
    images = {
        "ingestion": image_for("ingestion-service", "ingestion-worker"),
        "kis": image_for("knowledge-index-service", "api"),
        "postgres": image_for("knowledge-index-service", "postgres"),
        "qdrant": image_for("knowledge-index-service", "qdrant"),
    }
    report["images"] = {key: value["image_id"] for key, value in images.items()}
    settings = environment(resources.run_id)
    offline = {
        k: v
        for k, v in settings.items()
        if k
        in (
            "HF_HOME",
            "HF_HUB_OFFLINE",
            "TRANSFORMERS_OFFLINE",
            "PYTHONDONTWRITEBYTECODE",
        )
    }
    stage("parse_chunk_metadata_no_network")
    parser_id = resources.create(
        "parser",
        images["ingestion"]["image_id"],
        512,
        env_flags(offline)
        + mounted_tools()
        + cache_mount(images["ingestion"])
        + ["--entrypoint", "python"],
        [f"{CONTAINER_ROOT}/index_fixtures.py"],
        network="none",
    )
    bundle = resources.run(parser_id)
    if len(bundle["fixtures"]) != 40:
        raise AuditFailure("fixture_collection_incomplete")
    stage("create_private_tmpfs_stores")
    resources.network = command(
        "network",
        "create",
        "--internal",
        "--label",
        f"{LABEL}={resources.run_id}",
        f"rag-u0-kis-{resources.run_id}",
    )
    net = json.loads(
        command(
            "network",
            "inspect",
            "--format",
            '{"internal":{{json .Internal}},"labels":{{json .Labels}}}',
            resources.network,
        )
    )
    if (
        not ID.fullmatch(resources.network)
        or not net["internal"]
        or not owned(net, resources.run_id)
    ):
        raise AuditFailure("private_network_invalid")
    # postgres:16-alpine 的 postgres UID 由 image id -u 驗證，不猜測 UID。
    uid = command(
        "run",
        "--rm",
        "--pull=never",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--entrypoint",
        "id",
        images["postgres"]["image_id"],
        "-u",
        "postgres",
    )
    if not re.fullmatch(r"[1-9][0-9]{0,5}", uid):
        raise AuditFailure("postgres_user_invalid")
    password = settings["DATABASE_URL"].split(":", 2)[2].split("@", 1)[0]
    pg = resources.create(
        "postgres",
        images["postgres"]["image_id"],
        192,
        [
            "--network-alias",
            "postgres",
            "--env",
            "POSTGRES_USER=u0_only",
            "--env",
            "POSTGRES_DB=rag_u0",
            "--env",
            f"POSTGRES_PASSWORD={password}",
            "--tmpfs",
            f"/var/lib/postgresql/data:rw,nosuid,nodev,size=64m,uid={uid},gid={uid},mode=0700",
            "--tmpfs",
            f"/var/run/postgresql:rw,nosuid,nodev,size=4m,uid={uid},gid={uid},mode=0755",
        ],
        ["postgres", "-c", "shared_buffers=32MB", "-c", "max_connections=30"],
        user=f"{uid}:{uid}",
    )
    qd = resources.create(
        "qdrant",
        images["qdrant"]["image_id"],
        256,
        [
            "--network-alias",
            "qdrant",
            "--env",
            f"QDRANT__SERVICE__API_KEY={settings['QDRANT_API_KEY']}",
            "--env",
            "QDRANT__STORAGE__WAL__WAL_CAPACITY_MB=4",
            "--env",
            "QDRANT__STORAGE__OPTIMIZERS__DEFAULT_SEGMENT_NUMBER=1",
            "--tmpfs",
            "/qdrant/storage:rw,nosuid,nodev,size=192m,uid=1000,gid=1000",
            "--tmpfs",
            "/qdrant/snapshots:rw,nosuid,nodev,size=64m,uid=1000,gid=1000",
        ],
        [],
        user=images["qdrant"]["user"],
    )
    resources.start(pg)
    resources.start(qd)
    report["isolated_storage_config"] = {
        "postgres_tmpfs_mib": 64,
        "qdrant_tmpfs_mib": 192,
        "qdrant_wal_capacity_mb": 4,
        "qdrant_default_segment_number": 1,
        "note": "functional_test_only_not_production_performance",
    }
    deadline = time.monotonic() + 60
    while True:
        try:
            command(
                "exec",
                pg,
                "pg_isready",
                "-h",
                "127.0.0.1",
                "-U",
                "u0_only",
                "-d",
                "rag_u0",
            )
            break
        except AuditFailure:
            if time.monotonic() >= deadline:
                raise AuditFailure("isolated_postgres_not_ready") from None
            time.sleep(1)
    shared = env_flags(settings) + mounted_tools() + cache_mount(images["kis"])
    stage("alembic_upgrade_isolated_database")
    migration = resources.create(
        "migration",
        images["kis"]["image_id"],
        256,
        shared,
        ["alembic", "upgrade", "head"],
    )
    report["migration"] = resources.run(migration, expect_json=False)
    api = resources.create(
        "api",
        images["kis"]["image_id"],
        1280,
        shared + ["--network-alias", "kis-api"],
        [
            "uvicorn",
            "knowledge_index_service.presentation.app:app",
            "--host",
            "0.0.0.0",
            "--port",
            "8080",
            "--log-level",
            "warning",
            "--no-access-log",
        ],
    )
    resources.start(api)
    stage("submit_40_synthetic_documents")
    submit_id = resources.create(
        "submit",
        images["kis"]["image_id"],
        256,
        shared + ["--entrypoint", "python"],
        [f"{CONTAINER_ROOT}/kis_persistence_probe.py", "submit"],
        stdin=True,
    )
    receipts = resources.run(submit_id, bundle)
    bundle.update(receipts)
    resources.stop(api)
    stage("worker_real_cpu_embedding_api_stopped")
    worker = resources.create(
        "worker",
        images["kis"]["image_id"],
        1280,
        shared,
        ["python", "-m", "knowledge_index_service.worker"],
    )
    resources.start(worker)
    wait_id = resources.create(
        "wait",
        images["kis"]["image_id"],
        256,
        shared + ["--entrypoint", "python"],
        [f"{CONTAINER_ROOT}/kis_persistence_probe.py", "wait"],
        stdin=True,
    )
    report["indexing"] = resources.run(wait_id, bundle, timeout=660)
    resources.stop(worker)
    stage("verify_canonical_vector_acl_replay_worker_stopped")
    resources.start(api)
    verify_id = resources.create(
        "verify",
        images["kis"]["image_id"],
        256,
        shared + ["--entrypoint", "python"],
        [f"{CONTAINER_ROOT}/kis_persistence_probe.py", "verify"],
        stdin=True,
    )
    verified = resources.run(verify_id, bundle, timeout=360)
    resources.stop(api)
    report.update(verified)
    report["chunking_config"] = bundle["chunking_config"]
    report["chunking_config_source"] = bundle["config_source"]


def main() -> int:
    if os.environ.get("U0_ALLOW_ISOLATED_WRITES") != "1":
        print(
            json.dumps({"status": "BLOCKED", "check": "isolated_write_opt_in_required"})
        )
        return 2
    run_id = uuid4().hex
    resources = Resources(run_id)
    started = time.monotonic()
    report = {
        "run_id": run_id,
        "captured_at": datetime.now(UTC).isoformat(),
        "stage": "isolated_kis_persistence_not_answer_quality",
        "quality_gate": "BLOCKED",
        "company_data_touched": False,
        "external_llm_calls": 0,
        "model_downloads": 0,
        "production_files_changed": 0,
        "dataset_manifest_sha256": hashlib.sha256(
            (ROOT / "manifest.json").read_bytes()
        ).hexdigest(),
        "tools_sha256": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in (
                "isolated_kis_audit.py",
                "index_fixtures.py",
                "kis_persistence_probe.py",
            )
        },
    }
    try:
        execute(resources, report)
        exit_code = 0
    except (
        OSError,
        ValueError,
        KeyError,
        AuditFailure,
        subprocess.TimeoutExpired,
    ) as exc:
        report.update(
            {
                "status": "BLOCKED",
                "error_type": type(exc).__name__,
                "check": exc.code
                if isinstance(exc, AuditFailure)
                else "audit_environment_or_contract",
            }
        )
        report["diagnostic_categories"] = resources.failure_diagnostics()
        exit_code = 2
    finally:
        stage("exact_id_cleanup")
        cleanup_failures = resources.cleanup()
        report["cleanup"] = {
            "status": "FAILED" if cleanup_failures else "PASSED",
            "unremoved_exact_ids": cleanup_failures,
            "created_containers": len(resources.containers),
            "network_created": resources.network is not None,
        }
        report["containers"] = resources.audit
    if cleanup_failures:
        report["status"] = "BLOCKED"
        exit_code = 2
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    report["exit_code"] = exit_code
    destination = ROOT / "runtime" / f"u0-kis-persistence-{run_id}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    summary = {k: v for k, v in report.items() if k not in ("mapping", "containers")}
    summary["report_path"] = str(destination)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
