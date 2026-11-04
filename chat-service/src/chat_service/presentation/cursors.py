"""Strict、URL-safe keyset cursor codec。

Cursor 是不可信的 client input，因此 decode 會限制長度與字元集、拒絕 padding、驗證 UTF-8
與 canonical JSON，並把 timestamp/UUID/ordinal 轉回 Application DTO。它只描述 pagination
位置，不含 owner、query、文件或任何可用來繞過 repository owner filter 的資料。
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from datetime import UTC, datetime
from typing import Any, NoReturn
from uuid import UUID

from chat_service.application.dtos import ConversationCursorPayload, MessageCursorPayload
from chat_service.domain.exceptions import InvalidCursorError

MAX_CURSOR_LENGTH = 512
_URLSAFE_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def encode_conversation_cursor(payload: ConversationCursorPayload) -> str:
    """以固定欄位與 canonical JSON 編碼 conversation cursor。"""

    if not isinstance(payload, ConversationCursorPayload):
        raise InvalidCursorError("conversation cursor payload is invalid")
    raw = {
        "v": 1,
        "last_activity_at": _format_timestamp(payload.last_activity_at),
        "id": str(payload.conversation_id),
    }
    return _encode(raw)


def decode_conversation_cursor(value: str) -> ConversationCursorPayload:
    """解碼並完整驗證 conversation cursor。"""

    raw = _decode(value)
    if set(raw) != {"v", "last_activity_at", "id"}:
        _invalid()
    version = raw.get("v")
    timestamp = raw.get("last_activity_at")
    identifier = raw.get("id")
    if version != 1 or isinstance(version, bool):
        _invalid()
    if not isinstance(timestamp, str) or not timestamp.endswith("Z"):
        _invalid()
    parsed_time = _parse_timestamp(timestamp)
    if _format_timestamp(parsed_time) != timestamp:
        _invalid()
    if not isinstance(identifier, str):
        _invalid()
    try:
        parsed_id = UUID(identifier)
    except (ValueError, AttributeError, TypeError):
        _invalid()
    if str(parsed_id) != identifier:
        _invalid()
    return ConversationCursorPayload(
        version=1,
        last_activity_at=parsed_time,
        conversation_id=parsed_id,
    )


def encode_message_cursor(payload: MessageCursorPayload) -> str:
    """以固定欄位與 canonical JSON 編碼 message cursor。"""

    if not isinstance(payload, MessageCursorPayload):
        raise InvalidCursorError("message cursor payload is invalid")
    return _encode({"v": 1, "ordinal": payload.ordinal})


def decode_message_cursor(value: str) -> MessageCursorPayload:
    """解碼並完整驗證 message cursor。"""

    raw = _decode(value)
    if set(raw) != {"v", "ordinal"}:
        _invalid()
    version = raw.get("v")
    ordinal = raw.get("ordinal")
    if version != 1 or isinstance(version, bool):
        _invalid()
    if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal <= 0:
        _invalid()
    return MessageCursorPayload(version=1, ordinal=ordinal)


def _encode(payload: dict[str, object]) -> str:
    """canonical JSON → unpadded URL-safe base64，並套用共用長度上限。"""

    encoded_json = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    result = base64.urlsafe_b64encode(encoded_json).decode("ascii").rstrip("=")
    if len(result) > MAX_CURSOR_LENGTH:
        raise InvalidCursorError("cursor exceeds the maximum length")
    return result


def _decode(value: object) -> dict[str, Any]:
    """限制 base64 輸入並確認 decode 後仍是 canonical JSON object。"""

    if not isinstance(value, str) or not value or len(value) > MAX_CURSOR_LENGTH:
        _invalid()
    if "=" in value or _URLSAFE_RE.fullmatch(value) is None:
        _invalid()
    padding = "=" * ((4 - len(value) % 4) % 4)
    try:
        decoded = base64.b64decode(value + padding, altchars=b"-_", validate=True)
        text = decoded.decode("utf-8")
        parsed: Any = json.loads(
            text,
            parse_constant=lambda _constant: (_ for _ in ()).throw(ValueError()),
        )
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        _invalid()
    if not isinstance(parsed, dict):
        _invalid()
    canonical = json.dumps(
        parsed,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if canonical != text:
        _invalid()
    return parsed


def _parse_timestamp(value: str) -> datetime:
    """解析 UTC RFC3339 timestamp，不接受 naive datetime。"""

    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        _invalid()
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _invalid()
    return parsed.astimezone(UTC)


def _format_timestamp(value: datetime) -> str:
    """統一輸出 UTC ``Z`` 表示；保留 DB timestamp 的 microseconds。"""

    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidCursorError("cursor timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _invalid() -> NoReturn:
    """所有 decode 失敗共用同一安全錯誤，避免洩漏輸入內容。"""

    raise InvalidCursorError("invalid cursor")
