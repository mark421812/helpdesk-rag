"""KIS Domain 的有限狀態集合；StrEnum 可直接安全序列化成字串。"""

from enum import StrEnum


class EmbeddingInputType(StrEnum):
    """E5 model 需要區分 query 與 passage/document prefix。"""

    QUERY = "query"
    DOCUMENT = "document"


class DocumentStatus(StrEnum):
    """公司文件的治理生命週期。"""

    DRAFT = "draft"
    REVIEWING = "reviewing"
    PUBLISHED = "published"
    EXPIRED = "expired"
    ARCHIVED = "archived"
    DELETED = "deleted"


class Confidentiality(StrEnum):
    """文件機密分級。"""

    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


class IndexingStatus(StrEnum):
    """文件版本與向量索引同步的狀態。"""

    PENDING = "pending"
    PROCESSING = "processing"
    INDEXED = "indexed"
    FAILED = "failed"
    DELETING = "deleting"
    DELETED = "deleted"


class JobType(StrEnum):
    """KIS background job 支援的操作類型。"""

    INDEX_VERSION = "index_version"
    SYNC_METADATA = "sync_metadata"
    DELETE_DOCUMENT = "delete_document"


class JobStatus(StrEnum):
    """對外工作狀態。"""

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class PrincipalType(StrEnum):
    """ACL principal 可以是單一使用者或群組。"""

    USER = "user"
    GROUP = "group"


class PermissionEffect(StrEnum):
    """ACL allow/deny 效果。"""

    ALLOW = "allow"
    DENY = "deny"


class OutboxStatus(StrEnum):
    """Durable outbox event 的處理狀態。"""

    PENDING = "pending"
    PROCESSING = "processing"
    PROCESSED = "processed"
    FAILED = "failed"
