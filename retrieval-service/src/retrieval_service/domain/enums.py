"""Domain 列舉。

``StrEnum`` 的成員同時是 enum 與字串，可避免程式四處散落容易拼錯的 magic strings。
"""

from enum import StrEnum


class Confidentiality(StrEnum):
    """文件機密等級。"""

    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


class DocumentStatus(StrEnum):
    """Statuses the employee retrieval API is allowed to request."""

    PUBLISHED = "published"


class RetrievalStatus(StrEnum):
    """Retrieval 對上層明確表達的三種結果。"""

    EVIDENCE_FOUND = "evidence_found"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    EXTERNAL_GENERATION_BLOCKED = "external_generation_blocked"
