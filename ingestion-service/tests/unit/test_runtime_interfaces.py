from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError as PydanticValidationError

from ingestion_service.application.dtos import (
    IngestFileResult,
    ProcessJobResult,
    ScanSourceResult,
    SourceItemView,
)
from ingestion_service.bootstrap import Runtime
from ingestion_service.cli import build_parser, run_command
from ingestion_service.domain.entities import Source
from ingestion_service.domain.enums import (
    IngestionJobStatus,
    IngestionJobType,
    SourceItemStatus,
    SyncRunStatus,
    SyncTrigger,
)
from ingestion_service.healthcheck import heartbeat_is_fresh, refresh_database_heartbeat
from ingestion_service.infrastructure.config import Settings
from ingestion_service.presentation.app import create_app
from ingestion_service.scheduler import (
    needs_daily_catch_up,
    next_daily_run,
    run_catch_up_scans,
    run_scheduled_scans,
)
from ingestion_service.worker import process_next_job

NOW = datetime(2026, 8, 2, 8, 0, tzinfo=UTC)


class FakeUseCase:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def execute(self, *args: object, **kwargs: object) -> Any:
        self.calls.append((args, kwargs))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@dataclass
class FakeRuntime:
    settings: object
    default_source_id: UUID
    scan_source: FakeUseCase
    ingest_file: FakeUseCase
    claim_next_job: FakeUseCase
    process_job: FakeUseCase
    get_run: FakeUseCase
    get_item: FakeUseCase
    list_items: FakeUseCase
    get_job: FakeUseCase
    retry_job: FakeUseCase
    sources: tuple[Source, ...]
    ready: dict[str, bool]

    async def list_enabled_sources(self) -> tuple[Source, ...]:
        return self.sources

    async def readiness_checks(self) -> dict[str, bool]:
        return self.ready


def _item_view(source_id: UUID) -> SourceItemView:
    return SourceItemView(
        id=uuid4(),
        source_id=source_id,
        source_item_id="policies/leave.md",
        source_path="policies/leave.md",
        source_version="mtime:1;size:10",
        mime_type="text/markdown",
        file_size=10,
        status=SourceItemStatus.PENDING,
        missing_scan_count=0,
        knowledge_index_document_id=None,
        knowledge_index_version_id=None,
        knowledge_index_job_id=None,
        error_code=None,
        error_message=None,
        updated_at=NOW,
    )


def _fake_runtime(tmp_path) -> FakeRuntime:
    source_id = uuid4()
    item = _item_view(source_id)
    scan_result = ScanSourceResult(
        run_id=uuid4(),
        source_id=source_id,
        status=SyncRunStatus.COMPLETED,
        discovered_count=1,
        queued_count=1,
        missing_count=0,
        delete_queued_count=0,
    )
    ingest_result = IngestFileResult(
        run_id=uuid4(),
        item=item,
        job_id=uuid4(),
        queued=True,
    )
    return FakeRuntime(
        settings=SimpleNamespace(worker_poll_interval_seconds=0.01),
        default_source_id=source_id,
        scan_source=FakeUseCase(scan_result),
        ingest_file=FakeUseCase(ingest_result),
        claim_next_job=FakeUseCase(None),
        process_job=FakeUseCase(None),
        get_run=FakeUseCase(None),
        get_item=FakeUseCase(item),
        list_items=FakeUseCase((item,)),
        get_job=FakeUseCase(None),
        retry_job=FakeUseCase(None),
        sources=(Source(id=source_id, name="docs", root_path=str(tmp_path)),),
        ready={"document_root": True, "postgres": True, "knowledge_index": True},
    )


def test_api_key_health_and_source_scan(tmp_path) -> None:
    runtime = _fake_runtime(tmp_path)
    settings = Settings(document_root=tmp_path, api_internal_key="test-secret")
    app = create_app(settings=settings, runtime=cast(Runtime, runtime))

    with TestClient(app) as client:
        assert client.get("/health/live").status_code == 200
        assert client.get("/api/v1/sources").status_code == 401
        response = client.post(
            f"/api/v1/sources/{runtime.default_source_id}/sync",
            headers={"x-internal-api-key": "test-secret", "x-request-id": "request-123"},
        )
        opted_in = client.post(
            f"/api/v1/sources/{runtime.default_source_id}/sync?allow_mass_deletion=true",
            headers={"x-internal-api-key": "test-secret"},
        )

    assert response.status_code == 202
    assert response.headers["x-request-id"] == "request-123"
    assert response.json()["queued_count"] == 1
    assert runtime.scan_source.calls[0][0] == (
        runtime.default_source_id,
        SyncTrigger.MANUAL,
    )
    assert runtime.scan_source.calls[0][1] == {"allow_mass_deletion": False}
    assert opted_in.status_code == 202
    assert runtime.scan_source.calls[1][1] == {"allow_mass_deletion": True}


def test_file_ingest_endpoint_uses_relative_source_item_id(tmp_path) -> None:
    runtime = _fake_runtime(tmp_path)
    settings = Settings(document_root=tmp_path)
    app = create_app(settings=settings, runtime=cast(Runtime, runtime))

    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/sources/{runtime.default_source_id}/files/ingest",
            json={"source_item_id": "policies/leave.md"},
        )

    assert response.status_code == 202
    assert response.json()["item"]["source_item_id"] == "policies/leave.md"
    assert runtime.ingest_file.calls[0][0] == (
        runtime.default_source_id,
        "policies/leave.md",
        SyncTrigger.MANUAL,
    )


def test_file_ingest_rejects_oversized_source_item_id(tmp_path) -> None:
    runtime = _fake_runtime(tmp_path)
    settings = Settings(document_root=tmp_path, api_max_request_body_bytes=10_000)
    app = create_app(settings=settings, runtime=cast(Runtime, runtime))

    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/sources/{runtime.default_source_id}/files/ingest",
            json={"source_item_id": "x" * 4097},
        )

    assert response.status_code == 422
    assert runtime.ingest_file.calls == []


def test_api_rejects_large_body_without_affecting_health(tmp_path) -> None:
    runtime = _fake_runtime(tmp_path)
    settings = Settings(document_root=tmp_path, api_max_request_body_bytes=64)
    app = create_app(settings=settings, runtime=cast(Runtime, runtime))

    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/sources/{runtime.default_source_id}/files/ingest",
            content=b"x" * 65,
            headers={"content-type": "application/json", "x-request-id": "body-cap-test"},
        )
        health = client.request("GET", "/health/live", content=b"x" * 1024)

    assert response.status_code == 413
    assert response.headers["x-request-id"] == "body-cap-test"
    assert response.json()["request_id"] == "body-cap-test"
    assert runtime.ingest_file.calls == []
    assert health.status_code == 200


def test_api_streaming_body_limit_does_not_require_content_length(tmp_path) -> None:
    runtime = _fake_runtime(tmp_path)
    settings = Settings(document_root=tmp_path, api_max_request_body_bytes=64)
    app = create_app(settings=settings, runtime=cast(Runtime, runtime))

    def body_chunks():
        yield b"x" * 32
        yield b"x" * 33

    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/sources/{runtime.default_source_id}/files/ingest",
            content=body_chunks(),
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 413
    assert runtime.ingest_file.calls == []


def test_readiness_returns_503_when_dependency_is_down(tmp_path) -> None:
    runtime = _fake_runtime(tmp_path)
    runtime.ready["knowledge_index"] = False
    app = create_app(
        settings=Settings(document_root=tmp_path),
        runtime=cast(Runtime, runtime),
    )

    with TestClient(app) as client:
        response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"


def test_zero_missing_ratio_is_rejected(tmp_path) -> None:
    with pytest.raises(PydanticValidationError):
        Settings(document_root=tmp_path, max_missing_ratio=0)


def test_worker_lease_defaults_to_one_hour(tmp_path) -> None:
    assert Settings(document_root=tmp_path).worker_lease_seconds == 3600


def test_knowledge_index_url_defaults_to_fixed_network_alias(tmp_path) -> None:
    assert (
        Settings(document_root=tmp_path, _env_file=None).knowledge_index_url
        == "http://knowledge-index-api:8080"
    )


def test_production_requires_both_internal_api_keys(tmp_path) -> None:
    with pytest.raises(PydanticValidationError, match="API_INTERNAL_KEY"):
        Settings(
            document_root=tmp_path,
            app_env="production",
            knowledge_index_api_key="kis-secret",
        )
    with pytest.raises(PydanticValidationError, match="KNOWLEDGE_INDEX_API_KEY"):
        Settings(
            document_root=tmp_path,
            app_env="prod",
            api_internal_key="api-secret",
        )

    settings = Settings(
        document_root=tmp_path,
        app_env=" Production ",
        api_internal_key="api-secret",
        knowledge_index_api_key="kis-secret",
    )

    assert settings.app_env == "production"


@pytest.mark.asyncio
async def test_worker_claims_then_processes_one_job(tmp_path) -> None:
    runtime = _fake_runtime(tmp_path)
    job_id = uuid4()
    runtime.claim_next_job.result = SimpleNamespace(id=job_id)
    runtime.process_job.result = ProcessJobResult(
        job_id=job_id,
        item_id=uuid4(),
        job_type=IngestionJobType.PROCESS_ITEM,
        status=IngestionJobStatus.COMPLETED,
        item_status=SourceItemStatus.COMPLETED,
    )

    handled = await process_next_job(cast(Runtime, runtime), "worker-1")

    assert handled is True
    assert runtime.claim_next_job.calls[0][0] == ("worker-1",)
    assert runtime.process_job.calls[0][0] == (job_id, "worker-1")


def test_next_daily_run_uses_asia_taipei_wall_clock() -> None:
    before_schedule = datetime(2026, 8, 1, 17, 30, tzinfo=UTC)  # 01:30 in Taipei
    after_schedule = datetime(2026, 8, 1, 18, 30, tzinfo=UTC)  # 02:30 in Taipei

    same_day = next_daily_run(
        before_schedule,
        hour=2,
        minute=0,
        timezone="Asia/Taipei",
    )
    next_day = next_daily_run(
        after_schedule,
        hour=2,
        minute=0,
        timezone="Asia/Taipei",
    )

    assert same_day.isoformat() == "2026-08-02T02:00:00+08:00"
    assert next_day.isoformat() == "2026-08-03T02:00:00+08:00"


def test_daily_catch_up_depends_on_todays_success(tmp_path) -> None:
    source = Source(id=uuid4(), name="docs", root_path=str(tmp_path))
    before_schedule = datetime(2026, 8, 1, 17, 30, tzinfo=UTC)  # 01:30 in Taipei
    after_schedule = datetime(2026, 8, 1, 18, 30, tzinfo=UTC)  # 02:30 in Taipei

    assert not needs_daily_catch_up(
        source,
        before_schedule,
        hour=2,
        minute=0,
        timezone="Asia/Taipei",
    )
    assert needs_daily_catch_up(
        source,
        after_schedule,
        hour=2,
        minute=0,
        timezone="Asia/Taipei",
    )

    source.last_successful_scan_at = after_schedule
    assert not needs_daily_catch_up(
        source,
        after_schedule + timedelta(hours=1),
        hour=2,
        minute=0,
        timezone="Asia/Taipei",
    )


@pytest.mark.asyncio
async def test_catch_up_scans_only_sources_not_successful_today(tmp_path) -> None:
    runtime = _fake_runtime(tmp_path)
    due = runtime.sources[0]
    already_scanned = Source(
        id=uuid4(),
        name="docs-2",
        root_path=str(tmp_path),
        last_successful_scan_at=datetime(2026, 8, 1, 19, 0, tzinfo=UTC),
    )
    runtime.sources = (due, already_scanned)
    runtime.settings = SimpleNamespace(
        schedule_hour=2,
        schedule_minute=0,
        schedule_timezone="Asia/Taipei",
    )

    await run_catch_up_scans(
        cast(Runtime, runtime),
        now=datetime(2026, 8, 1, 20, 0, tzinfo=UTC),
    )

    assert len(runtime.scan_source.calls) == 1
    assert runtime.scan_source.calls[0][0][0] == due.id


@pytest.mark.asyncio
async def test_scheduled_scan_continues_after_one_source_fails(tmp_path) -> None:
    runtime = _fake_runtime(tmp_path)
    second = Source(id=uuid4(), name="docs-2", root_path=str(tmp_path))
    runtime.sources = (*runtime.sources, second)
    expected = runtime.scan_source.result

    class PerSourceScan(FakeUseCase):
        async def execute(
            self,
            source_id: UUID,
            trigger: SyncTrigger,
            *,
            allow_mass_deletion: bool,
        ) -> object:
            self.calls.append(((source_id, trigger), {"allow_mass_deletion": allow_mass_deletion}))
            if source_id == runtime.sources[0].id:
                raise RuntimeError("source unavailable")
            return expected

    runtime.scan_source = PerSourceScan(expected)
    results = await run_scheduled_scans(cast(Runtime, runtime))

    assert results == (expected,)
    assert len(runtime.scan_source.calls) == 2
    assert all(call[1] == {"allow_mass_deletion": False} for call in runtime.scan_source.calls)


def test_heartbeat_freshness_uses_mtime_and_max_age(tmp_path) -> None:
    heartbeat = Path(tmp_path) / "worker.heartbeat"
    heartbeat.touch()
    os.utime(heartbeat, (100.0, 100.0))

    assert heartbeat_is_fresh(heartbeat, max_age_seconds=30, now=125.0)
    assert not heartbeat_is_fresh(heartbeat, max_age_seconds=30, now=131.0)
    assert not heartbeat_is_fresh(heartbeat.with_suffix(".missing"), max_age_seconds=30)


@pytest.mark.asyncio
async def test_database_heartbeat_is_written_only_after_successful_probe(tmp_path) -> None:
    heartbeat = Path(tmp_path) / "worker.heartbeat"

    class HeartbeatRuntime:
        def __init__(self) -> None:
            self.ready = False

        async def database_ready(self) -> bool:
            return self.ready

    runtime = HeartbeatRuntime()
    assert not await refresh_database_heartbeat(cast(Runtime, runtime), heartbeat)
    assert not heartbeat.exists()

    runtime.ready = True
    assert await refresh_database_heartbeat(cast(Runtime, runtime), heartbeat)
    assert heartbeat.exists()


@pytest.mark.asyncio
async def test_cli_mass_deletion_requires_explicit_flag(tmp_path) -> None:
    runtime = _fake_runtime(tmp_path)
    args = build_parser().parse_args(["scan", "--allow-mass-deletion"])

    await run_command(args, cast(Runtime, runtime))

    assert runtime.scan_source.calls[0][1] == {"allow_mass_deletion": True}


@pytest.mark.asyncio
async def test_cli_ingest_defaults_to_managed_source(tmp_path) -> None:
    runtime = _fake_runtime(tmp_path)
    parser = build_parser()
    args = parser.parse_args(["ingest", "policies/leave.md"])

    result = await run_command(
        cast(argparse.Namespace, args),
        cast(Runtime, runtime),
    )

    assert result is runtime.ingest_file.result
    assert runtime.ingest_file.calls[0][0] == (
        runtime.default_source_id,
        "policies/leave.md",
        SyncTrigger.CLI,
    )
