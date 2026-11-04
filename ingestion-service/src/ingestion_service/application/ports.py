"""Ingestion Application ports。

Protocol 描述掃描、parser、chunker、metadata、KIS client、repository 與 Unit of Work 所需
行為。Application 依賴抽象介面，Infrastructure 實作它們。
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Protocol, Self
from uuid import UUID

from ingestion_service.application.dtos import (
    KnowledgeIndexDocumentSubmission,
    KnowledgeIndexJobResult,
    KnowledgeIndexMetadataPatch,
    KnowledgeIndexSubmissionResult,
)
from ingestion_service.domain.entities import (
    Chunk,
    DiscoveredFile,
    DocumentMetadata,
    IngestionJob,
    ParsedDocument,
    Source,
    SourceItem,
    SyncRun,
)
from ingestion_service.domain.enums import SourceItemStatus, SourceType


class SourceScanner(Protocol):
    """文件來源 port；實作者可以是本機資料夾，未來也可以是 SharePoint。"""

    def scan(self, source: Source) -> Sequence[DiscoveredFile]: ...

    def discover(self, source: Source, source_item_id: str) -> DiscoveredFile | None: ...


class SourceScanLock(Protocol):
    """確保同一 source 不會被兩個 API／scheduler 同時掃描。"""

    def acquire(self, source_id: UUID) -> AbstractAsyncContextManager[None]: ...


class DocumentParser(Protocol):
    """把特定檔案格式轉成與格式無關的 ParsedDocument。"""

    def parse(self, path: Path) -> ParsedDocument: ...


class ParserRegistry(Protocol):
    """依副檔名或 MIME type 選擇合適 parser。"""

    def get_parser(self, path: Path, mime_type: str | None = None) -> DocumentParser: ...


class DocumentChunker(Protocol):
    """把已解析文件切成 embedding 模型可接受的 chunks。"""

    def chunk(self, document: ParsedDocument) -> tuple[Chunk, ...]: ...


class MetadataLoader(Protocol):
    """讀取 metadata 並計算 metadata 來源指紋。"""

    def source_fingerprint(self, source: Source, discovered: DiscoveredFile) -> str: ...

    def load(
        self,
        source: Source,
        discovered: DiscoveredFile,
        parsed: ParsedDocument,
    ) -> DocumentMetadata: ...


class KnowledgeIndexClient(Protocol):
    """Ingestion 呼叫 KIS 所需的最小介面；不綁定 httpx。"""

    async def submit_document(
        self, submission: KnowledgeIndexDocumentSubmission
    ) -> KnowledgeIndexSubmissionResult: ...

    async def patch_metadata(
        self,
        document_id: UUID,
        patch: KnowledgeIndexMetadataPatch,
    ) -> KnowledgeIndexJobResult: ...

    async def delete_document(self, document_id: UUID) -> KnowledgeIndexJobResult: ...

    async def get_job(self, job_id: UUID) -> KnowledgeIndexJobResult: ...


class SourceRepository(Protocol):
    """Source aggregate 的 persistence port。"""

    async def get(self, source_id: UUID) -> Source | None: ...

    async def add(self, source: Source) -> None: ...

    async def update(self, source: Source) -> None: ...

    async def list_enabled(self, source_type: SourceType | None = None) -> Sequence[Source]: ...


class SourceItemRepository(Protocol):
    """SourceItem aggregate 的 persistence port。"""

    async def get(self, item_id: UUID) -> SourceItem | None: ...

    async def find_by_source_item_id(
        self, source_id: UUID, source_item_id: str
    ) -> SourceItem | None: ...

    async def add(self, item: SourceItem) -> None: ...

    async def update(self, item: SourceItem) -> None: ...

    async def list_for_source(self, source_id: UUID) -> Sequence[SourceItem]: ...

    async def list_items(
        self,
        *,
        source_id: UUID | None = None,
        status: SourceItemStatus | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> Sequence[SourceItem]: ...


class SyncRunRepository(Protocol):
    """掃描紀錄的 persistence port。"""

    async def get(self, run_id: UUID) -> SyncRun | None: ...

    async def add(self, run: SyncRun) -> None: ...

    async def update(self, run: SyncRun) -> None: ...


class IngestionJobRepository(Protocol):
    """資料庫工作佇列 port，包含原子化 claim 行為。"""

    async def get(self, job_id: UUID) -> IngestionJob | None: ...

    async def get_by_idempotency_key(self, idempotency_key: str) -> IngestionJob | None: ...

    async def add(self, job: IngestionJob) -> None: ...

    async def update(self, job: IngestionJob) -> None: ...

    async def claim_next(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_for: timedelta,
    ) -> IngestionJob | None: ...


class UnitOfWork(Protocol):
    """把多個 repository 寫入包在同一 database transaction。

    ``async with`` 會呼叫 ``__aenter__``／``__aexit__``；離開區塊時 adapter 可統一
    commit 或 rollback，這是 Clean Architecture 常見的 Unit of Work pattern。
    """

    sources: SourceRepository
    items: SourceItemRepository
    sync_runs: SyncRunRepository
    jobs: IngestionJobRepository

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


class UnitOfWorkFactory(Protocol):
    """可呼叫物件的介面；每次呼叫都必須建立一個新的 UnitOfWork。"""

    def __call__(self) -> UnitOfWork: ...
