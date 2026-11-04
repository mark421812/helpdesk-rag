"""管理員兩階段 hard purge 的 SQLAlchemy repository adapter。

本 adapter 把 preview snapshot、鎖定順序與 content-free audit 留在同一個 persistence
邊界內。所有時間都以 PostgreSQL database clock 為準，方法只會 ``flush`` 而不會 ``commit``；
transaction 的成功或回滾仍由 Unit of Work 決定。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import delete, distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from chat_service.application.dtos import (
    ExecuteConversationPurgeCommand,
    PreviewConversationPurgeCommand,
    PurgePreviewResult,
    PurgeResult,
)
from chat_service.domain.entities import (
    Principal,
    PurgePreview,
    PurgeSelector,
    SelectedConversation,
)
from chat_service.domain.enums import PROCESSING_TURN_STATES
from chat_service.domain.exceptions import (
    ConversationBusyError,
    DomainValidationError,
    PurgePreviewConsumedError,
    PurgePreviewExpiredError,
    PurgePreviewNotFoundError,
    PurgePreviewStaleError,
    PurgeSelectionTooLargeError,
)
from chat_service.infrastructure.persistence.models import (
    ConversationModel,
    MessageModel,
    PurgeAuditModel,
    PurgePreviewModel,
    TurnModel,
)

_MAX_PREVIEW_SELECTION = 1_000
_SELECTION_PROBE_LIMIT = _MAX_PREVIEW_SELECTION + 1


def _utc_rfc3339(value: datetime) -> str:
    """產生 digest 使用的單一 UTC timestamp 表示，避免 session timezone 影響摘要。"""

    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _selector_json(selector: PurgeSelector) -> dict[str, Any]:
    """建立固定欄位的 canonical selector；conversation IDs 一律依 UUID 升冪。"""

    return {
        "conversation_ids": [
            str(conversation_id) for conversation_id in sorted(selector.conversation_ids, key=str)
        ],
        "created_before": (
            None if selector.created_before is None else _utc_rfc3339(selector.created_before)
        ),
        "owner_issuer": selector.owner_issuer,
        "owner_subject": selector.owner_subject,
    }


def _selected_json(selected: tuple[SelectedConversation, ...]) -> list[dict[str, Any]]:
    """只投影 purge snapshot 允許保存的 ID 與 row version。"""

    return [
        {
            "conversation_id": str(item.conversation_id),
            "row_version": item.row_version,
        }
        for item in sorted(selected, key=lambda item: str(item.conversation_id))
    ]


def _selection_digest(
    selector_json: dict[str, Any],
    selected_json: list[dict[str, Any]],
) -> str:
    """以 canonical JSON UTF-8 計算 selector 與 snapshot 的 deterministic SHA-256。"""

    payload = {
        "selected_conversations": selected_json,
        "selector": selector_json,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _parse_uuid(value: object, field_name: str) -> UUID:
    """解析 preview JSON 的 UUID，錯誤訊息不包含實際資料值。"""

    if not isinstance(value, str):
        raise DomainValidationError(f"{field_name} must be a UUID string")
    try:
        return UUID(value)
    except ValueError:
        raise DomainValidationError(f"{field_name} must be a UUID string") from None


def _parse_timestamp(value: object, field_name: str) -> datetime | None:
    """解析 canonical nullable RFC3339 timestamp。"""

    if value is None:
        return None
    if not isinstance(value, str):
        raise DomainValidationError(f"{field_name} must be an RFC3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise DomainValidationError(f"{field_name} must be an RFC3339 timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DomainValidationError(f"{field_name} must be timezone-aware")
    return parsed


def _selector_from_json(value: object) -> PurgeSelector:
    """把 DB JSONB 還原成 typed selector，並拒絕 schema 漂移。"""

    if not isinstance(value, dict) or set(value) != {
        "conversation_ids",
        "created_before",
        "owner_issuer",
        "owner_subject",
    }:
        raise DomainValidationError("stored purge selector has an invalid schema")
    raw = cast(dict[str, object], value)
    conversation_ids = raw["conversation_ids"]
    if not isinstance(conversation_ids, list):
        raise DomainValidationError("stored purge conversation_ids must be a list")
    owner_issuer = raw["owner_issuer"]
    owner_subject = raw["owner_subject"]
    if owner_issuer is not None and not isinstance(owner_issuer, str):
        raise DomainValidationError("stored purge owner_issuer must be text")
    if owner_subject is not None and not isinstance(owner_subject, str):
        raise DomainValidationError("stored purge owner_subject must be text")
    return PurgeSelector(
        conversation_ids=tuple(_parse_uuid(item, "conversation_id") for item in conversation_ids),
        owner_issuer=owner_issuer,
        owner_subject=owner_subject,
        created_before=_parse_timestamp(raw["created_before"], "created_before"),
    )


def _selected_from_json(value: object) -> tuple[SelectedConversation, ...]:
    """還原 content-free snapshot，要求固定欄位且依 UUID 升冪。"""

    if not isinstance(value, list):
        raise DomainValidationError("stored purge snapshot must be a list")
    selected: list[SelectedConversation] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"conversation_id", "row_version"}:
            raise DomainValidationError("stored purge snapshot has an invalid schema")
        raw = cast(dict[str, object], item)
        row_version = raw["row_version"]
        if isinstance(row_version, bool) or not isinstance(row_version, int):
            raise DomainValidationError("stored purge row_version must be an integer")
        selected.append(
            SelectedConversation(
                conversation_id=_parse_uuid(raw["conversation_id"], "conversation_id"),
                row_version=row_version,
            )
        )
    ordered = tuple(sorted(selected, key=lambda item: str(item.conversation_id)))
    if tuple(selected) != ordered:
        raise DomainValidationError("stored purge snapshot must be ordered by UUID")
    return ordered


def _preview_to_domain(model: PurgePreviewModel) -> PurgePreview:
    """將 ORM preview 映射到 framework-free Domain entity。"""

    return PurgePreview(
        id=model.id,
        selector=_selector_from_json(model.selector),
        selected=_selected_from_json(model.selected_conversations),
        selector_digest=model.selector_digest,
        conversation_count=model.conversation_count,
        message_count=model.message_count,
        creator=Principal(
            issuer=model.creator_issuer,
            subject=model.creator_subject,
        ),
        expires_at=model.expires_at,
        created_at=model.created_at,
        consumed_at=model.consumed_at,
    )


class SqlAlchemyPurgeRepository:
    """以 PostgreSQL locks 實作 preview-first、不可部分刪除的 hard purge。"""

    def __init__(
        self,
        session: AsyncSession,
        *,
        preview_ttl_seconds: int = 600,
    ) -> None:
        if (
            isinstance(preview_ttl_seconds, bool)
            or not isinstance(preview_ttl_seconds, int)
            or preview_ttl_seconds <= 0
        ):
            raise ValueError("preview_ttl_seconds must be a positive integer")
        self._session = session
        self._preview_ttl_seconds = preview_ttl_seconds

    async def create_preview(
        self,
        command: PreviewConversationPurgeCommand,
    ) -> PurgePreviewResult:
        """以 AND selector 探測至第 1,001 筆，建立不含 conversation 內容的 snapshot。"""

        selected, message_count, busy_count = await self._select_preview_snapshot(command.selector)
        if len(selected) > _MAX_PREVIEW_SELECTION:
            raise PurgeSelectionTooLargeError(
                "purge selector matched more than 1,000 conversations"
            )

        database_now = await self._database_now()
        selector_json = _selector_json(command.selector)
        selected_json = _selected_json(selected)
        model = PurgePreviewModel(
            id=uuid4(),
            selector=selector_json,
            selected_conversations=selected_json,
            selector_digest=_selection_digest(selector_json, selected_json),
            conversation_count=len(selected),
            message_count=message_count,
            creator_issuer=command.principal.issuer,
            creator_subject=command.principal.subject,
            expires_at=database_now + timedelta(seconds=self._preview_ttl_seconds),
            created_at=database_now,
        )
        self._session.add(model)
        await self._session.flush()
        return PurgePreviewResult(
            preview=_preview_to_domain(model),
            busy_count=busy_count,
        )

    async def get_preview_for_creator(
        self,
        preview_id: UUID,
        creator: Principal,
    ) -> PurgePreview | None:
        """以 preview ID 與完整 creator issuer+subject 查詢，避免 identity mismatch 洩漏。"""

        statement = select(PurgePreviewModel).where(
            PurgePreviewModel.id == preview_id,
            PurgePreviewModel.creator_issuer == creator.issuer,
            PurgePreviewModel.creator_subject == creator.subject,
        )
        model = await self._session.scalar(statement)
        return None if model is None else _preview_to_domain(model)

    async def execute(self, command: ExecuteConversationPurgeCommand) -> PurgeResult:
        """依 preview→conversation UUID 的固定順序鎖定並原子 audit、cascade、consume。"""

        preview_statement = (
            select(PurgePreviewModel)
            .where(
                PurgePreviewModel.id == command.preview_id,
                PurgePreviewModel.creator_issuer == command.principal.issuer,
                PurgePreviewModel.creator_subject == command.principal.subject,
            )
            .with_for_update()
        )
        preview_model = await self._session.scalar(preview_statement)
        if preview_model is None:
            raise PurgePreviewNotFoundError("purge preview was not found")

        # consumed 的判定優先於 expiry，讓並行 execute 的第二個請求得到穩定的 conflict。
        if preview_model.consumed_at is not None:
            raise PurgePreviewConsumedError("purge preview was already consumed")

        database_now = await self._database_now()
        if preview_model.expires_at <= database_now:
            raise PurgePreviewExpiredError("purge preview has expired")

        try:
            selector = _selector_from_json(preview_model.selector)
            preview_selected = _selected_from_json(preview_model.selected_conversations)
        except DomainValidationError:
            raise PurgePreviewStaleError("purge preview snapshot is invalid") from None

        preview_ids = tuple(item.conversation_id for item in preview_selected)
        locked_selected = await self._lock_selected_conversations(preview_ids)
        if locked_selected != preview_selected:
            raise PurgePreviewStaleError("purge preview no longer matches the selected rows")

        current_selected = await self._select_snapshot(selector)
        if len(current_selected) > _MAX_PREVIEW_SELECTION or current_selected != preview_selected:
            raise PurgePreviewStaleError("purge preview selection has changed")

        selector_json = _selector_json(selector)
        selected_json = _selected_json(current_selected)
        current_digest = _selection_digest(selector_json, selected_json)
        if current_digest != preview_model.selector_digest:
            raise PurgePreviewStaleError("purge preview digest no longer matches")

        busy_count = await self._busy_conversation_count(preview_ids)
        if busy_count:
            raise ConversationBusyError("selected conversations contain processing turns")

        message_count = await self._message_count(preview_ids)
        if (
            preview_model.conversation_count != len(current_selected)
            or preview_model.message_count != message_count
        ):
            raise PurgePreviewStaleError("purge preview counts no longer match")

        purge_id = uuid4()
        audit = PurgeAuditModel(
            id=purge_id,
            actor_issuer=command.principal.issuer,
            actor_subject=command.principal.subject,
            selector_digest=preview_model.selector_digest,
            conversation_count=len(current_selected),
            message_count=message_count,
            reason=command.reason,
            request_id=command.request_id,
            purged_at=database_now,
        )
        self._session.add(audit)
        if preview_ids:
            await self._session.execute(
                delete(ConversationModel).where(ConversationModel.id.in_(preview_ids))
            )
        preview_model.consumed_at = database_now
        await self._session.flush()
        return PurgeResult(
            purge_id=purge_id,
            conversation_count=len(current_selected),
            message_count=message_count,
            purged_at=database_now,
        )

    async def _database_now(self) -> datetime:
        """只使用目前 transaction 所見的 PostgreSQL database time。"""

        value = cast(datetime | None, await self._session.scalar(select(func.clock_timestamp())))
        if value is None:
            raise RuntimeError("database did not return its current time")
        return value

    def _selector_conditions(self, selector: PurgeSelector) -> tuple[Any, ...]:
        """把 selector 各欄組合成 SQL AND predicates，絕不省略 owner 的另一半。"""

        conditions: list[Any] = []
        if selector.conversation_ids:
            conditions.append(ConversationModel.id.in_(selector.conversation_ids))
        if selector.owner_issuer is not None:
            conditions.extend(
                (
                    ConversationModel.owner_issuer == selector.owner_issuer,
                    ConversationModel.owner_subject == selector.owner_subject,
                )
            )
        if selector.created_before is not None:
            conditions.append(ConversationModel.created_at < selector.created_before)
        return tuple(conditions)

    async def _select_snapshot(
        self,
        selector: PurgeSelector,
    ) -> tuple[SelectedConversation, ...]:
        """以 UUID 升冪取得 selector snapshot，LIMIT 1001 保留超量辨識能力。"""

        statement = (
            select(ConversationModel.id, ConversationModel.row_version)
            .where(*self._selector_conditions(selector))
            .order_by(ConversationModel.id.asc())
            .limit(_SELECTION_PROBE_LIMIT)
        )
        rows = (await self._session.execute(statement)).all()
        return tuple(
            SelectedConversation(conversation_id=row.id, row_version=row.row_version)
            for row in rows
        )

    async def _select_preview_snapshot(
        self,
        selector: PurgeSelector,
    ) -> tuple[tuple[SelectedConversation, ...], int, int]:
        """以單一 statement 取得 rows、message count 與 busy conversation count。

        PostgreSQL READ COMMITTED 在每個 statement 開始時建立 snapshot；把兩個 child 聚合寫成
        correlated subquery，可避免三次查詢各自看見不同已提交狀態。processing 使用 EXISTS，
        因此每個 selected conversation 最多只會對 busy count 貢獻一次。
        """

        processing_values = tuple(state.value for state in PROCESSING_TURN_STATES)
        message_count = (
            select(func.count())
            .select_from(MessageModel)
            .where(MessageModel.conversation_id == ConversationModel.id)
            .correlate(ConversationModel)
            .scalar_subquery()
            .label("message_count")
        )
        is_busy = (
            select(TurnModel.id)
            .where(
                TurnModel.conversation_id == ConversationModel.id,
                TurnModel.state.in_(processing_values),
            )
            .correlate(ConversationModel)
            .exists()
            .label("is_busy")
        )
        statement = (
            select(
                ConversationModel.id,
                ConversationModel.row_version,
                message_count,
                is_busy,
            )
            .where(*self._selector_conditions(selector))
            .order_by(ConversationModel.id.asc())
            .limit(_SELECTION_PROBE_LIMIT)
        )
        rows = (await self._session.execute(statement)).all()
        selected = tuple(
            SelectedConversation(conversation_id=row.id, row_version=row.row_version)
            for row in rows
        )
        return (
            selected,
            sum(int(row.message_count) for row in rows),
            sum(1 for row in rows if row.is_busy),
        )

    async def _lock_selected_conversations(
        self,
        conversation_ids: tuple[UUID, ...],
    ) -> tuple[SelectedConversation, ...]:
        """依 UUID 升冪鎖 selected conversations；空集合不產生無條件 SQL。"""

        if not conversation_ids:
            return ()
        statement = (
            select(ConversationModel.id, ConversationModel.row_version)
            .where(ConversationModel.id.in_(conversation_ids))
            .order_by(ConversationModel.id.asc())
            .with_for_update()
        )
        rows = (await self._session.execute(statement)).all()
        return tuple(
            SelectedConversation(conversation_id=row.id, row_version=row.row_version)
            for row in rows
        )

    async def _message_count(self, conversation_ids: tuple[UUID, ...]) -> int:
        """計算 selected conversations 的 message 數；空選取直接回零。"""

        if not conversation_ids:
            return 0
        statement = (
            select(func.count())
            .select_from(MessageModel)
            .where(MessageModel.conversation_id.in_(conversation_ids))
        )
        return int((await self._session.scalar(statement)) or 0)

    async def _busy_conversation_count(self, conversation_ids: tuple[UUID, ...]) -> int:
        """計算至少含一筆 processing turn 的 distinct conversation 數。"""

        if not conversation_ids:
            return 0
        processing_values = tuple(state.value for state in PROCESSING_TURN_STATES)
        statement = select(func.count(distinct(TurnModel.conversation_id))).where(
            TurnModel.conversation_id.in_(conversation_ids),
            TurnModel.state.in_(processing_values),
        )
        return int((await self._session.scalar(statement)) or 0)
