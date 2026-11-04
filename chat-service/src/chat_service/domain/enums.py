"""Chat Service 核心使用的有限狀態集合。

這些列舉只使用 Python 標準庫，讓 Domain 不依賴 FastAPI、Pydantic 或資料庫。`StrEnum`
同時保留字串序列化能力與型別安全，避免把規格中的狀態值散落成容易拼錯的 magic string。
"""

from enum import StrEnum


class ConversationStatus(StrEnum):
    """Conversation 可被員工讀取的兩種狀態。"""

    ACTIVE = "active"
    ARCHIVED = "archived"


class InternalTurnState(StrEnum):
    """資料庫允許保存的七種 turn state。"""

    ACCEPTED = "accepted"
    WAITING_FOR_CAPACITY = "waiting_for_capacity"
    RETRIEVING = "retrieving"
    GENERATING = "generating"
    COMPLETED = "completed"
    FAILED = "failed"
    FAILED_UNKNOWN = "failed_unknown"


class PublicTurnState(StrEnum):
    """對外隱藏內部 processing phase 後的四種狀態。"""

    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    FAILED_UNKNOWN = "failed_unknown"


class MessageRole(StrEnum):
    """Conversation message 的有限角色。"""

    USER = "user"
    ASSISTANT = "assistant"


class ChatOutcome(StrEnum):
    """Chat 對員工公開的業務結果。"""

    ANSWERED = "answered"
    UNABLE_TO_ANSWER = "unable_to_answer"
    EXTERNAL_GENERATION_BLOCKED = "external_generation_blocked"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    OUTBOUND_POLICY_BLOCKED = "outbound_policy_blocked"


class RetrievalStatus(StrEnum):
    """Retrieval Service 對 Chat 的三種 HTTP 200 業務結果。"""

    EVIDENCE_FOUND = "evidence_found"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    EXTERNAL_GENERATION_BLOCKED = "external_generation_blocked"


class AnswerStatus(StrEnum):
    """Answer Generation Service 的正常回答結果。"""

    ANSWERED = "answered"
    UNABLE_TO_ANSWER = "unable_to_answer"


class Confidentiality(StrEnum):
    """Retrieval/Answer contract 使用的文件機密等級。"""

    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


PROCESSING_TURN_STATES = frozenset(
    {
        InternalTurnState.ACCEPTED,
        InternalTurnState.WAITING_FOR_CAPACITY,
        InternalTurnState.RETRIEVING,
        InternalTurnState.GENERATING,
    }
)


def project_public_turn_state(state: InternalTurnState) -> PublicTurnState:
    """把內部 phase 投影成 public state；processing 不會成為持久化 state。"""

    if state in PROCESSING_TURN_STATES:
        return PublicTurnState.PROCESSING
    return PublicTurnState(state.value)
