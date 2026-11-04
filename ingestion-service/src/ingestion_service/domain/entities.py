"""Ingestion Domain entities 與來源／文件／job 狀態轉移規則。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ingestion_service.domain.enums import (
    Confidentiality,
    DocumentStatus,
    IngestionJobStatus,
    IngestionJobType,
    PendingOperation,
    PermissionEffect,
    PrincipalType,
    SourceItemStatus,
    SourceType,
    SyncRunStatus,
    SyncTrigger,
)
from ingestion_service.domain.exceptions import (
    LeaseLostError,
    MetadataMissingError,
    ValidationError,
)

DEFAULT_EXTENSIONS = (".docx", ".md", ".pdf", ".txt")
DEFAULT_MAX_FILE_SIZE_BYTES = 100 * 1024 * 1024


def utc_now() -> datetime:
    """回傳帶 UTC 時區的現在時間；避免 naive datetime 造成跨時區比較錯誤。"""

    return datetime.now(UTC)


def _required_blanks(values: dict[str, str | None]) -> tuple[str, ...]:
    """以 comprehension 找出 None 或空白欄位，並轉成不可變的 tuple。"""

    return tuple(name for name, value in values.items() if value is None or not value.strip())


@dataclass(frozen=True, slots=True)
class ParsedBlock:
    """Parser 產生的最小語意區塊，例如一段文字或一個標題下的內容。

    ``frozen=True`` 表示建立後不可重新指定欄位；``slots=True`` 可防止任意新增屬性，
    很適合當跨層傳遞、內容應保持不變的 value object。
    """

    content: str
    section_path: str | None = None
    page_number: int | None = None
    block_type: str = "paragraph"

    def __post_init__(self) -> None:
        if not self.content.strip():
            raise ValidationError("Parsed block content must not be blank")
        if self.page_number is not None and self.page_number <= 0:
            raise ValidationError("Parsed block page_number must be positive")
        if not self.block_type.strip():
            raise ValidationError("Parsed block block_type must not be blank")


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    """一份解析完成的文件；tuple 表示 blocks 有順序但不應被原地修改。"""

    blocks: tuple[ParsedBlock, ...]
    parser_name: str
    parser_version: str
    mime_type: str
    suggested_title: str | None = None

    def __post_init__(self) -> None:
        if not self.blocks:
            raise ValidationError("Parsed document must contain at least one block")
        blanks = _required_blanks(
            {
                "parser_name": self.parser_name,
                "parser_version": self.parser_version,
                "mime_type": self.mime_type,
            }
        )
        if blanks:
            raise ValidationError(f"Parsed document fields are blank: {', '.join(blanks)}")


@dataclass(frozen=True, slots=True)
class Chunk:
    """準備送往 Knowledge Index Service 的文字切片與引用定位資訊。"""

    content: str
    section_path: str | None
    page_number: int | None
    token_count: int

    def __post_init__(self) -> None:
        if not self.content.strip():
            raise ValidationError("Chunk content must not be blank")
        if self.page_number is not None and self.page_number <= 0:
            raise ValidationError("Chunk page_number must be positive")
        if self.token_count <= 0:
            raise ValidationError("Chunk token_count must be positive")


@dataclass(frozen=True, slots=True)
class AccessControlEntry:
    """單筆 ACL 規則：某類 principal 被允許或拒絕讀取文件。"""

    principal_type: PrincipalType
    principal_id: str
    effect: PermissionEffect = PermissionEffect.ALLOW

    def __post_init__(self) -> None:
        if not self.principal_id.strip():
            raise ValidationError("ACL principal_id must not be blank")
        if len(self.principal_id) > 512:
            raise ValidationError("ACL principal_id must not exceed 512 characters")


@dataclass(frozen=True, slots=True)
class DocumentMetadata:
    """已完成型別轉換與商業規則驗證的文件 metadata value object。"""

    title: str
    document_type: str
    owner_department: str
    document_owner: str
    version_number: str | None = None
    confidentiality: Confidentiality = Confidentiality.INTERNAL
    status: DocumentStatus = DocumentStatus.DRAFT
    language: str = "zh-TW"
    external_llm_allowed: bool = False
    source_url: str | None = None
    effective_at: date | None = None
    expires_at: date | None = None
    review_at: date | None = None
    custom_metadata: dict[str, Any] = field(default_factory=dict)
    acl: tuple[AccessControlEntry, ...] = ()

    def __post_init__(self) -> None:
        missing = _required_blanks(
            {
                "title": self.title,
                "document_type": self.document_type,
                "owner_department": self.owner_department,
                "document_owner": self.document_owner,
            }
        )
        if missing:
            raise MetadataMissingError(missing)
        if not self.language.strip():
            raise ValidationError("Metadata language must not be blank")
        length_limits = {
            "title": (self.title, 1000),
            "document_type": (self.document_type, 100),
            "owner_department": (self.owner_department, 255),
            "document_owner": (self.document_owner, 255),
            "version_number": (self.version_number, 100),
            "language": (self.language, 20),
        }
        oversized = [
            name
            for name, (value, limit) in length_limits.items()
            if value is not None and len(value) > limit
        ]
        if oversized:
            raise ValidationError(
                f"Metadata fields exceed Knowledge Index limits: {', '.join(oversized)}"
            )
        if len(self.acl) > 1000:
            raise ValidationError("Metadata ACL must not exceed 1000 entries")
        acl_principals = [
            (entry.principal_type, entry.principal_id, entry.effect) for entry in self.acl
        ]
        if len(acl_principals) != len(set(acl_principals)):
            raise ValidationError("Metadata ACL must not contain duplicate entries")
        if self.expires_at and self.effective_at and self.expires_at < self.effective_at:
            raise ValidationError("expires_at must not be before effective_at")
        if self.confidentiality in {
            Confidentiality.CONFIDENTIAL,
            Confidentiality.RESTRICTED,
        } and not any(entry.effect == PermissionEffect.ALLOW for entry in self.acl):
            raise ValidationError(
                "Confidential and restricted documents require at least one allow ACL"
            )

    def fingerprint(self) -> str:
        """建立穩定雜湊，用來辨識「內容沒變、只有 metadata 改變」。

        JSON 使用固定 key 順序與緊湊分隔符，ACL 也先排序；如此同一組資料即使原始
        YAML 排列不同，仍會得到同一個 SHA-256。
        """
        payload = {
            "title": self.title,
            "document_type": self.document_type,
            "owner_department": self.owner_department,
            "document_owner": self.document_owner,
            "version_number": self.version_number,
            "confidentiality": self.confidentiality.value,
            "status": self.status.value,
            "language": self.language,
            "external_llm_allowed": self.external_llm_allowed,
            "source_url": self.source_url,
            "effective_at": self.effective_at.isoformat() if self.effective_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "review_at": self.review_at.isoformat() if self.review_at else None,
            "custom_metadata": self.custom_metadata,
            "acl": sorted(
                (
                    {
                        "principal_type": entry.principal_type.value,
                        "principal_id": entry.principal_id,
                        "effect": entry.effect.value,
                    }
                    for entry in self.acl
                ),
                key=lambda value: (value["principal_type"], value["principal_id"], value["effect"]),
            ),
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(slots=True)
class Source:
    """一個可掃描的文件來源設定；目前 source_type 只有本機資料夾。"""

    id: UUID
    name: str
    root_path: str
    source_type: SourceType = SourceType.LOCAL_FOLDER
    enabled: bool = True
    allowed_extensions: tuple[str, ...] = DEFAULT_EXTENSIONS
    max_file_size_bytes: int = DEFAULT_MAX_FILE_SIZE_BYTES
    schedule_cron: str = "0 2 * * *"
    timezone: str = "Asia/Taipei"
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    last_successful_scan_at: datetime | None = None

    def __post_init__(self) -> None:
        blanks = _required_blanks(
            {"name": self.name, "root_path": self.root_path, "schedule_cron": self.schedule_cron}
        )
        if blanks:
            raise ValidationError(f"Source fields are blank: {', '.join(blanks)}")
        if self.max_file_size_bytes <= 0:
            raise ValidationError("max_file_size_bytes must be positive")
        # set comprehension 去重、lower 統一大小寫，再排序後轉 tuple 取得穩定結果。
        normalized = tuple(
            sorted(
                {
                    extension.lower() if extension.startswith(".") else f".{extension.lower()}"
                    for extension in self.allowed_extensions
                    if extension.strip()
                }
            )
        )
        if not normalized:
            raise ValidationError("Source must allow at least one extension")
        unsupported = set(normalized).difference(DEFAULT_EXTENSIONS)
        if unsupported:
            raise ValidationError(f"Unsupported extensions: {', '.join(sorted(unsupported))}")
        self.allowed_extensions = normalized
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValidationError(f"Unknown source timezone: {self.timezone}") from exc


@dataclass(frozen=True, slots=True)
class DiscoveredFile:
    """Scanner 從檔案系統觀察到的不可變快照，尚未代表它已解析或索引。"""

    source_item_id: str
    relative_path: str
    absolute_path: Path
    file_size: int
    modified_at: datetime

    def __post_init__(self) -> None:
        blanks = _required_blanks(
            {
                "source_item_id": self.source_item_id,
                "relative_path": self.relative_path,
            }
        )
        if blanks:
            raise ValidationError(f"Discovered file fields are blank: {', '.join(blanks)}")
        if self.absolute_path.name in {"", ".", ".."}:
            raise ValidationError("Discovered file path must reference a file")
        if self.file_size < 0:
            raise ValidationError("Discovered file_size must not be negative")

    @property
    def source_version(self) -> str:
        """用修改時間與大小形成低成本版本值；``@property`` 讓方法像欄位般讀取。"""

        return f"mtime:{self.modified_at.isoformat()};size:{self.file_size}"


@dataclass(slots=True)
class SourceItem:
    """追蹤一個來源檔案從發現、處理、等待索引到刪除的狀態。

    與 frozen value object 不同，這是具有生命週期的 entity，所以狀態轉移方法會修改
    自身欄位。所有轉移集中於此，Application 不需在各處重複拼裝狀態規則。
    """

    id: UUID
    source_id: UUID
    source_item_id: str
    source_path: str
    absolute_path: str
    source_version: str
    metadata_source_hash: str
    mime_type: str | None
    file_size: int
    modified_at: datetime
    lifecycle_generation: int = 0
    status: SourceItemStatus = SourceItemStatus.DISCOVERED
    missing_scan_count: int = 0
    last_seen_run_id: UUID | None = None
    last_seen_at: datetime | None = None
    last_successful_content_hash: str | None = None
    last_successful_metadata_hash: str | None = None
    pending_content_hash: str | None = None
    pending_metadata_hash: str | None = None
    pending_operation: PendingOperation | None = None
    restore_requested: bool = False
    knowledge_index_document_id: UUID | None = None
    knowledge_index_version_id: UUID | None = None
    knowledge_index_job_id: UUID | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    completed_at: datetime | None = None
    deleted_at: datetime | None = None

    def __post_init__(self) -> None:
        blanks = _required_blanks(
            {
                "source_item_id": self.source_item_id,
                "source_path": self.source_path,
                "absolute_path": self.absolute_path,
                "source_version": self.source_version,
                "metadata_source_hash": self.metadata_source_hash,
            }
        )
        if blanks:
            raise ValidationError(f"Source item fields are blank: {', '.join(blanks)}")
        if self.file_size < 0 or self.missing_scan_count < 0 or self.lifecycle_generation < 0:
            raise ValidationError("Source item sizes and counters must not be negative")

    @property
    def discovery_fingerprint(self) -> str:
        """內容版本與 metadata 來源雜湊的組合，用來判斷是否需重新排工作。"""

        return f"{self.source_version}:{self.metadata_source_hash}"

    def observe(
        self,
        discovered: DiscoveredFile,
        metadata_source_hash: str,
        run_id: UUID,
        observed_at: datetime,
    ) -> bool:
        """套用一次掃描結果，並回傳檔案內容或 metadata 是否發生變化。"""

        deletion_in_flight = (
            self.status == SourceItemStatus.DELETING
            or self.pending_operation == PendingOperation.DELETE_DOCUMENT
        )
        reappearing = self.status == SourceItemStatus.DELETED or (
            deletion_in_flight and not self.restore_requested
        )
        changed = (
            self.source_version != discovered.source_version
            or self.metadata_source_hash != metadata_source_hash
            or self.status in {SourceItemStatus.DELETED, SourceItemStatus.DELETING}
        )
        if reappearing:
            self.lifecycle_generation += 1
        self.source_path = discovered.relative_path
        self.absolute_path = str(discovered.absolute_path)
        self.source_version = discovered.source_version
        self.metadata_source_hash = metadata_source_hash
        self.file_size = discovered.file_size
        self.modified_at = discovered.modified_at
        self.missing_scan_count = 0
        self.last_seen_run_id = run_id
        self.last_seen_at = observed_at
        self.deleted_at = None
        if deletion_in_flight:
            # Let the remote deletion finish, then deterministically resubmit the reappeared file.
            self.restore_requested = True
            changed = True
        self.updated_at = observed_at
        if changed:
            self.status = SourceItemStatus.PENDING
            self.clear_error()
        elif self.status == SourceItemStatus.MISSING:
            self.status = SourceItemStatus.COMPLETED
        return changed

    def mark_missing(self, at: datetime) -> int:
        """將連續消失次數加一；第二次消失是否刪除由 ScanSource policy 決定。"""

        if self.status == SourceItemStatus.DELETED:
            return self.missing_scan_count
        self.missing_scan_count += 1
        self.status = SourceItemStatus.MISSING
        self.updated_at = at
        return self.missing_scan_count

    def begin(self, status: SourceItemStatus, at: datetime) -> None:
        """進入新的處理狀態，並清除上一次錯誤。"""

        self.status = status
        self.updated_at = at
        self.clear_error()

    def begin_delete(self, at: datetime) -> None:
        """在呼叫外部服務前先保存刪除意圖，避免程序中斷後無從恢復。"""
        self.begin(SourceItemStatus.DELETING, at)
        self.pending_operation = PendingOperation.DELETE_DOCUMENT
        self.pending_content_hash = None
        self.pending_metadata_hash = None
        self.knowledge_index_job_id = None

    def wait_for_index(
        self,
        *,
        operation: PendingOperation,
        content_hash: str | None,
        metadata_hash: str | None,
        document_id: UUID,
        version_id: UUID | None,
        job_id: UUID,
        at: datetime,
    ) -> None:
        """記錄非同步 KIS job；worker 之後會用 job_id 輪詢結果。"""

        self.status = (
            SourceItemStatus.DELETING
            if operation == PendingOperation.DELETE_DOCUMENT
            else SourceItemStatus.WAITING_FOR_INDEX
        )
        self.pending_operation = operation
        self.pending_content_hash = content_hash
        self.pending_metadata_hash = metadata_hash
        self.knowledge_index_document_id = document_id
        self.knowledge_index_version_id = version_id
        self.knowledge_index_job_id = job_id
        self.updated_at = at

    def complete_pending_operation(self, at: datetime) -> None:
        """依 pending operation 完成索引或 soft delete，並清除暫存欄位。"""

        was_missing = self.missing_scan_count > 0
        if self.pending_operation == PendingOperation.DELETE_DOCUMENT:
            if self.restore_requested:
                self.status = SourceItemStatus.MISSING if was_missing else SourceItemStatus.PENDING
            else:
                self.status = SourceItemStatus.DELETED
            self.deleted_at = None if self.restore_requested else at
            # Keep the remote identity so a reappearing file can be restored idempotently.
            self.last_successful_content_hash = None
            self.last_successful_metadata_hash = None
            self.restore_requested = False
        else:
            self.status = SourceItemStatus.MISSING if was_missing else SourceItemStatus.COMPLETED
            self.completed_at = at
            if self.pending_content_hash is not None:
                self.last_successful_content_hash = self.pending_content_hash
            if self.pending_metadata_hash is not None:
                self.last_successful_metadata_hash = self.pending_metadata_hash
        self.pending_content_hash = None
        self.pending_metadata_hash = None
        self.pending_operation = None
        self.knowledge_index_job_id = None
        if self.status == SourceItemStatus.COMPLETED:
            self.missing_scan_count = 0
        self.updated_at = at
        self.clear_error()

    def reset_failed_remote_job(self, at: datetime) -> None:
        """保留待重試的操作意圖，但釋放已失敗、不可再輪詢的 KIS job ID。"""
        if self.pending_operation is None:
            raise ValidationError("Cannot reset a remote job without a pending operation")
        self.knowledge_index_job_id = None
        self.updated_at = at

    def complete_without_remote_job(
        self,
        *,
        operation: PendingOperation,
        content_hash: str | None,
        metadata_hash: str | None,
        document_id: UUID | None,
        version_id: UUID | None,
        at: datetime,
    ) -> None:
        """處理 KIS 已同步完成、沒有遠端 job 可輪詢的情況。"""

        self.pending_operation = operation
        self.pending_content_hash = content_hash
        self.pending_metadata_hash = metadata_hash
        if document_id is not None:
            self.knowledge_index_document_id = document_id
        self.knowledge_index_version_id = version_id
        self.complete_pending_operation(at)

    def quarantine(self, status: SourceItemStatus, code: str, message: str, at: datetime) -> None:
        """隔離不可自動重試的文件，例如缺 metadata、需 OCR 或格式不合法。"""

        if status not in {
            SourceItemStatus.METADATA_MISSING,
            SourceItemStatus.OCR_REQUIRED,
            SourceItemStatus.QUARANTINED,
        }:
            raise ValidationError("Invalid quarantine status")
        self.status = status
        self.error_code = code
        self.error_message = message
        self.updated_at = at

    def fail(self, code: str, message: str, at: datetime) -> None:
        self.status = SourceItemStatus.FAILED
        self.error_code = code
        self.error_message = message
        self.updated_at = at

    def clear_error(self) -> None:
        self.error_code = None
        self.error_message = None


@dataclass(slots=True)
class SyncRun:
    """一次完整或單檔掃描的執行摘要，供 API/CLI 查詢。"""

    id: UUID
    source_id: UUID
    trigger: SyncTrigger
    status: SyncRunStatus = SyncRunStatus.RUNNING
    discovered_count: int = 0
    queued_count: int = 0
    missing_count: int = 0
    delete_queued_count: int = 0
    error_message: str | None = None
    started_at: datetime = field(default_factory=utc_now)
    completed_at: datetime | None = None

    def complete(
        self,
        *,
        discovered_count: int,
        queued_count: int,
        missing_count: int,
        delete_queued_count: int,
        at: datetime,
    ) -> None:
        self.status = SyncRunStatus.COMPLETED
        self.discovered_count = discovered_count
        self.queued_count = queued_count
        self.missing_count = missing_count
        self.delete_queued_count = delete_queued_count
        self.completed_at = at
        self.error_message = None

    def fail(self, message: str, at: datetime) -> None:
        self.status = SyncRunStatus.FAILED
        self.error_message = message
        self.completed_at = at


@dataclass(slots=True)
class IngestionJob:
    """PostgreSQL-backed 工作佇列項目，內含 lease、重試與退避狀態機。"""

    id: UUID
    source_id: UUID
    item_id: UUID
    job_type: IngestionJobType
    idempotency_key: str
    sync_run_id: UUID | None = None
    status: IngestionJobStatus = IngestionJobStatus.PENDING
    attempt_count: int = 0
    max_attempts: int = 5
    available_at: datetime = field(default_factory=utc_now)
    leased_by: str | None = None
    lease_expires_at: datetime | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    completed_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.idempotency_key.strip():
            raise ValidationError("Job idempotency_key must not be blank")
        if self.attempt_count < 0 or self.max_attempts <= 0:
            raise ValidationError("Job attempt counters are invalid")

    @property
    def is_terminal(self) -> bool:
        """完成與最終失敗都屬於 terminal，worker 不可再次 claim。"""

        return self.status in {IngestionJobStatus.COMPLETED, IngestionJobStatus.FAILED}

    def claim(self, worker_id: str, now: datetime, lease_for: timedelta) -> None:
        """讓一個 worker 在有限時間內取得 job 所有權。"""

        if not worker_id.strip():
            raise ValidationError("worker_id must not be blank")
        if self.is_terminal or self.available_at > now:
            raise LeaseLostError("Job is not claimable")
        if (
            self.status == IngestionJobStatus.PROCESSING
            and self.lease_expires_at is not None
            and self.lease_expires_at > now
        ):
            raise LeaseLostError("Job already has an active lease")
        self.status = IngestionJobStatus.PROCESSING
        self.leased_by = worker_id
        self.lease_expires_at = now + lease_for
        self.updated_at = now

    def assert_lease(self, worker_id: str, now: datetime) -> None:
        """在每次寫回結果前確認 lease 仍有效，避免兩個 worker 同時完成同一 job。"""

        if (
            self.status != IngestionJobStatus.PROCESSING
            or self.leased_by != worker_id
            or self.lease_expires_at is None
            or self.lease_expires_at <= now
        ):
            raise LeaseLostError("Worker does not own an active job lease")

    def complete(self, worker_id: str, now: datetime) -> None:
        self.assert_lease(worker_id, now)
        self.status = IngestionJobStatus.COMPLETED
        self.completed_at = now
        self.updated_at = now
        self.error_code = None
        self.error_message = None
        self._clear_lease()

    def defer(self, worker_id: str, now: datetime, delay: timedelta) -> None:
        self.assert_lease(worker_id, now)
        self.status = IngestionJobStatus.PENDING
        self.available_at = now + delay
        self.updated_at = now
        self._clear_lease()

    def record_failure(
        self,
        worker_id: str,
        now: datetime,
        *,
        code: str,
        message: str,
        retryable: bool,
        base_delay: timedelta,
    ) -> None:
        """記錄失敗；可重試時使用 1、2、4…倍的 exponential backoff。"""

        self.assert_lease(worker_id, now)
        self.attempt_count += 1
        self.error_code = code
        self.error_message = message
        self.updated_at = now
        if retryable and self.attempt_count < self.max_attempts:
            # ``**`` 是次方運算子；第 1 次失敗等待 1 倍，第 2 次 2 倍，以此類推。
            multiplier = 2 ** (self.attempt_count - 1)
            self.status = IngestionJobStatus.PENDING
            self.available_at = now + base_delay * multiplier
        else:
            self.status = IngestionJobStatus.FAILED
            self.completed_at = now
        self._clear_lease()

    def retry_manually(self, now: datetime) -> None:
        if self.status != IngestionJobStatus.FAILED:
            raise ValidationError("Only failed jobs can be retried manually")
        self.status = IngestionJobStatus.PENDING
        self.attempt_count = 0
        self.available_at = now
        self.completed_at = None
        self.error_code = None
        self.error_message = None
        self.updated_at = now
        self._clear_lease()

    def _clear_lease(self) -> None:
        self.leased_by = None
        self.lease_expires_at = None
