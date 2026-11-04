"""Ingestion Domain 的有限狀態、操作與治理列舉。"""

from enum import StrEnum


class SourceType(StrEnum):
    """可插拔的文件來源類型。"""

    LOCAL_FOLDER = "local_folder"


class SyncTrigger(StrEnum):
    """一次掃描是由哪個入口觸發。"""

    MANUAL = "manual"
    SCHEDULED = "scheduled"
    CLI = "cli"


class SyncRunStatus(StrEnum):
    """整次掃描 run 的狀態。"""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class SourceItemStatus(StrEnum):
    """單份來源文件在 ingestion pipeline 的生命週期。"""

    DISCOVERED = "discovered"
    PENDING = "pending"
    PARSING = "parsing"
    CHUNKING = "chunking"
    SUBMITTING = "submitting"
    WAITING_FOR_INDEX = "waiting_for_index"
    COMPLETED = "completed"
    MISSING = "missing"
    METADATA_MISSING = "metadata_missing"
    OCR_REQUIRED = "ocr_required"
    QUARANTINED = "quarantined"
    FAILED = "failed"
    DELETING = "deleting"
    DELETED = "deleted"


class IngestionJobType(StrEnum):
    """Background worker 可以執行的 job 類型。"""

    PROCESS_ITEM = "process_item"
    POLL_KNOWLEDGE_INDEX = "poll_knowledge_index"
    DELETE_ITEM = "delete_item"


class IngestionJobStatus(StrEnum):
    """資料庫工作佇列的狀態。"""

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class PendingOperation(StrEnum):
    """SourceItem 正等待 KIS 完成的遠端操作。"""

    SUBMIT_CONTENT = "submit_content"
    PATCH_METADATA = "patch_metadata"
    DELETE_DOCUMENT = "delete_document"


class Confidentiality(StrEnum):
    """與 KIS contract 對齊的文件機密分級。"""

    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


class DocumentStatus(StrEnum):
    """與 KIS contract 對齊的治理狀態。"""

    DRAFT = "draft"
    REVIEWING = "reviewing"
    PUBLISHED = "published"
    EXPIRED = "expired"
    ARCHIVED = "archived"


class PrincipalType(StrEnum):
    """ACL principal 類型。"""

    USER = "user"
    GROUP = "group"


class PermissionEffect(StrEnum):
    """ACL allow/deny 效果。"""

    ALLOW = "allow"
    DENY = "deny"


class KnowledgeIndexJobStatus(StrEnum):
    """KIS async job response 狀態。"""

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class KnowledgeIndexingStatus(StrEnum):
    """KIS 文件版本的索引狀態。"""

    PENDING = "pending"
    PROCESSING = "processing"
    INDEXED = "indexed"
    FAILED = "failed"
    DELETING = "deleting"
    DELETED = "deleted"
