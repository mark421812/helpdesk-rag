"""Strict cursor contract tests。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from chat_service.application.dtos import ConversationCursorPayload, MessageCursorPayload
from chat_service.domain.exceptions import InvalidCursorError
from chat_service.presentation.cursors import (
    decode_conversation_cursor,
    decode_message_cursor,
    encode_conversation_cursor,
    encode_message_cursor,
)


def test_conversation_cursor_round_trip_is_canonical() -> None:
    payload = ConversationCursorPayload(
        version=1,
        last_activity_at=datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=UTC),
        conversation_id=uuid4(),
    )
    encoded = encode_conversation_cursor(payload)
    assert len(encoded) <= 512
    assert "=" not in encoded
    assert decode_conversation_cursor(encoded) == payload


def test_message_cursor_round_trip_and_invalid_payloads() -> None:
    encoded = encode_message_cursor(MessageCursorPayload(version=1, ordinal=7))
    assert decode_message_cursor(encoded).ordinal == 7
    for invalid in ("", "=", "not-base64", encoded + "=", "eyJ2IjoxLCJvcmRpbmFsIjowfQ"):
        with pytest.raises(InvalidCursorError):
            decode_message_cursor(invalid)


def test_cursor_rejects_noncanonical_json() -> None:
    # Base64 for {"v": 1, "ordinal": 7}; spaces make the JSON non-canonical.
    with pytest.raises(InvalidCursorError):
        decode_message_cursor("eyJ2IjogMSwgIm9yZGluYWwiOiA3fQ")
