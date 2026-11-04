"""Ingestion Composition Root：組裝 parser、chunker、repositories、KIS client 與 use cases。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import cast
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from ingestion_service.application.ports import UnitOfWorkFactory
from ingestion_service.application.use_cases import (
    ClaimNextIngestionJob,
    GetIngestionJob,
    GetSourceItem,
    GetSyncRun,
    IngestFile,
    ListSourceItems,
    ProcessIngestionJob,
    RetryIngestionJob,
    ScanSource,
)
from ingestion_service.domain.entities import Source, utc_now
from ingestion_service.domain.enums import SourceType
from ingestion_service.infrastructure.chunking.structure_aware import (
    ChunkingConfig,
    StructureAwareChunker,
)
from ingestion_service.infrastructure.chunking.token_counter import (
    HuggingFaceTokenCounter,
    TokenCounter,
)
from ingestion_service.infrastructure.config import Settings, get_settings
from ingestion_service.infrastructure.knowledge_index.http_client import (
    KnowledgeIndexHttpClient,
)
from ingestion_service.infrastructure.metadata.yaml_resolver import YamlMetadataResolver
from ingestion_service.infrastructure.persistence.database import (
    create_engine,
    create_session_factory,
)
from ingestion_service.infrastructure.persistence.scan_lock import PostgresSourceScanLock
from ingestion_service.infrastructure.persistence.uow import SqlAlchemyUnitOfWorkFactory
from ingestion_service.infrastructure.sources.local_folder import LocalFolderScanner

_DEFAULT_SOURCE_ID = uuid5(NAMESPACE_URL, "ingestion:default-local-folder-source:v1")


class LazyHuggingFaceTokenCounter:
    """延遲載入 tokenizer，避免 API 與 scheduler 啟動時支付模型載入成本。"""

    def __init__(self, model_id: str, revision: str) -> None:
        self._model_id = model_id
        self._revision = revision
        self._delegate: TokenCounter | None = None

    def count(self, text: str) -> int:
        # ``is None`` 是判斷尚未初始化；第一次使用後快取 delegate 供後續重用。
        if self._delegate is None:
            self._delegate = HuggingFaceTokenCounter(
                self._model_id,
                self._revision,
            )
        return self._delegate.count(text)


@dataclass(slots=True)
class Runtime:
    """已完成依賴注入的應用執行環境，集中保存所有 use-case instance。

    Presentation、CLI、worker 與 scheduler 都拿同一種 Runtime，因而共用一致的商業流程。
    """

    settings: Settings
    engine: AsyncEngine
    uow_factory: UnitOfWorkFactory
    knowledge_index: KnowledgeIndexHttpClient
    scan_source: ScanSource
    ingest_file: IngestFile
    claim_next_job: ClaimNextIngestionJob
    process_job: ProcessIngestionJob
    retry_job: RetryIngestionJob
    get_item: GetSourceItem
    list_items: ListSourceItems
    get_job: GetIngestionJob
    get_run: GetSyncRun
    default_source_id: UUID = _DEFAULT_SOURCE_ID

    async def list_enabled_sources(self) -> tuple[Source, ...]:
        async with self.uow_factory() as uow:
            return tuple(await uow.sources.list_enabled())

    async def database_ready(self) -> bool:
        try:
            async with self.engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except Exception:
            return False
        return True

    async def readiness_checks(self) -> dict[str, bool]:
        checks = {
            "document_root": self.settings.document_root.is_dir(),
            "postgres": await self.database_ready(),
            "knowledge_index": False,
        }
        checks["knowledge_index"] = await self.knowledge_index.is_ready()
        return checks

    async def close(self) -> None:
        await self.knowledge_index.close()
        await self.engine.dispose()


def build_runtime(settings: Settings | None = None) -> Runtime:
    """在 composition root 建立 adapters，再注入只認識 ports 的 use cases。

    這裡是 Clean Architecture 允許同時看見 Application 與 Infrastructure 的最外層；
    Domain/Application 本身不應反向 import SQLAlchemy、httpx 或格式解析套件。
    """

    # Parser adapters 會 import 額外格式套件，放在函式內可讓輕量程序延後支付 import 成本。
    from ingestion_service.infrastructure.parsers.registry import create_default_parser_registry

    # ``a or b``：呼叫端有傳 settings 就使用它，否則從環境變數載入預設設定。
    resolved = settings or get_settings()
    engine = create_engine(resolved)
    session_factory = create_session_factory(engine)
    uow_factory = cast(UnitOfWorkFactory, SqlAlchemyUnitOfWorkFactory(session_factory))

    scanner = LocalFolderScanner(resolved.document_root)
    parser_registry = create_default_parser_registry()
    metadata_loader = YamlMetadataResolver(resolved.source_default_metadata)
    token_counter = LazyHuggingFaceTokenCounter(resolved.model_id, resolved.model_revision)
    chunker = StructureAwareChunker(
        token_counter,
        ChunkingConfig(
            target_tokens=resolved.chunk_target_tokens,
            max_tokens=resolved.chunk_max_tokens,
            overlap_tokens=resolved.chunk_overlap_tokens,
            model_max_tokens=resolved.model_max_sequence_length,
        ),
    )
    knowledge_index = KnowledgeIndexHttpClient(
        resolved.knowledge_index_url,
        api_key=resolved.knowledge_index_api_key,
        timeout_seconds=resolved.knowledge_index_timeout_seconds,
        readiness_timeout_seconds=resolved.knowledge_index_readiness_timeout_seconds,
        max_request_body_bytes=resolved.knowledge_index_max_request_body_bytes,
    )
    scan_lock = PostgresSourceScanLock(engine)

    # 下方是手動 Dependency Injection：constructor 明確列出每個 use case 的依賴。
    return Runtime(
        settings=resolved,
        engine=engine,
        uow_factory=uow_factory,
        knowledge_index=knowledge_index,
        scan_source=ScanSource(
            uow_factory,
            scanner,
            metadata_loader,
            scan_lock=scan_lock,
            missing_scans_before_delete=resolved.missing_scans_before_delete,
            max_missing_ratio=resolved.max_missing_ratio,
            max_job_attempts=resolved.worker_max_attempts,
        ),
        ingest_file=IngestFile(
            uow_factory,
            scanner,
            metadata_loader,
            scan_lock=scan_lock,
            max_job_attempts=resolved.worker_max_attempts,
        ),
        claim_next_job=ClaimNextIngestionJob(
            uow_factory,
            lease_for=timedelta(seconds=resolved.worker_lease_seconds),
        ),
        process_job=ProcessIngestionJob(
            uow_factory,
            parser_registry,
            chunker,
            metadata_loader,
            knowledge_index,
            max_job_attempts=resolved.worker_max_attempts,
            max_document_characters=resolved.max_document_characters,
            max_document_chunks=resolved.max_document_chunks,
            retry_base_delay=timedelta(seconds=resolved.worker_retry_base_seconds),
        ),
        retry_job=RetryIngestionJob(uow_factory),
        get_item=GetSourceItem(uow_factory),
        list_items=ListSourceItems(uow_factory),
        get_job=GetIngestionJob(uow_factory),
        get_run=GetSyncRun(uow_factory),
    )


async def ensure_default_source(runtime: Runtime) -> Source:
    """建立或同步由環境變數管理的預設 local-folder source。"""

    desired = _source_from_settings(runtime.settings)
    try:
        async with runtime.uow_factory() as uow:
            existing = await uow.sources.get(desired.id)
            if existing is None:
                await uow.sources.add(desired)
                await uow.commit()
                return desired
            _reconcile_source(existing, desired)
            await uow.sources.update(existing)
            await uow.commit()
            return existing
    except IntegrityError:
        # API、worker、scheduler 可能同時建立相同穩定 ID；唯一鍵衝突後重新讀取即可。
        async with runtime.uow_factory() as uow:
            existing = await uow.sources.get(desired.id)
            if existing is None:
                raise
            return existing


def _source_from_settings(settings: Settings) -> Source:
    return Source(
        id=_DEFAULT_SOURCE_ID,
        name=settings.default_source_name,
        source_type=SourceType.LOCAL_FOLDER,
        root_path=str(Path(settings.document_root).resolve(strict=True)),
        allowed_extensions=tuple(settings.allowed_extensions),
        max_file_size_bytes=settings.max_file_size_bytes,
        schedule_cron=f"{settings.schedule_minute} {settings.schedule_hour} * * *",
        timezone=settings.schedule_timezone,
    )


def _reconcile_source(existing: Source, desired: Source) -> None:
    existing.name = desired.name
    existing.source_type = desired.source_type
    existing.root_path = desired.root_path
    existing.allowed_extensions = desired.allowed_extensions
    existing.max_file_size_bytes = desired.max_file_size_bytes
    existing.schedule_cron = desired.schedule_cron
    existing.timezone = desired.timezone
    existing.updated_at = utc_now()
