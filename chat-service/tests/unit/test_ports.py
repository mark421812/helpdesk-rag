"""Phase 3 persistence adapters 必須實作的 core port shape tests。"""

from __future__ import annotations

from inspect import signature

from chat_service.application.ports import (
    ConversationRepositoryPort,
    PurgeRepositoryPort,
    TurnRepositoryPort,
)


def test_conversation_port_exposes_owner_locked_transactional_allocations() -> None:
    """Ordinal allocation 不可成為 SQLAlchemy adapter 的私有方法。"""

    assert hasattr(ConversationRepositoryPort, "get_for_owner_for_update")
    assert hasattr(ConversationRepositoryPort, "allocate_turn_and_user_message_ordinals")
    assert hasattr(ConversationRepositoryPort, "allocate_message_ordinal")
    assert hasattr(ConversationRepositoryPort, "record_terminal_activity")


def test_turn_port_uses_duration_for_database_time_stale_recovery() -> None:
    """Stale cutoff 由 DB time 計算，port 只收 duration seconds。"""

    parameters = signature(TurnRepositoryPort.recover_stale).parameters
    assert tuple(parameters) == ("self", "processing_stale_seconds")
    assert hasattr(TurnRepositoryPort, "get_for_owner_for_update")
    assert hasattr(TurnRepositoryPort, "add_assistant_message")
    assert tuple(signature(TurnRepositoryPort.add).parameters) == (
        "self",
        "turn",
        "user_message",
        "principal",
    )
    assert tuple(signature(TurnRepositoryPort.update).parameters) == (
        "self",
        "turn",
        "principal",
    )


def test_purge_preview_lookup_is_creator_scoped() -> None:
    """Creator mismatch 與不存在必須共用同一 owner-filtered lookup。"""

    assert not hasattr(PurgeRepositoryPort, "get_preview")
    assert hasattr(PurgeRepositoryPort, "get_preview_for_creator")
