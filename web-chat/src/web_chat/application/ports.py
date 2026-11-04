"""Application ports for the only permitted runtime dependency: Chat Service HTTP."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from web_chat.application.dtos import GatewayResult


class ChatGatewayPort(Protocol):
    """Web layer需要的最小 Chat Service contract；下游細節由 infrastructure adapter 實作。"""

    async def create_conversation(self, *, title: str | None, request_id: str) -> GatewayResult:
        """建立一個由 server-side demo identity 擁有的 conversation。"""

    async def submit_turn(
        self,
        *,
        conversation_id: UUID,
        idempotency_key: str,
        question: str,
        language: str | None,
        request_id: str,
    ) -> GatewayResult:
        """送出單輪問題；adapter 不會 retry POST。"""

    async def get_turn(
        self,
        *,
        conversation_id: UUID,
        turn_id: UUID,
        request_id: str,
    ) -> GatewayResult:
        """查詢既有 turn，不接受任意 URL。"""

    async def readiness(self, *, request_id: str) -> bool:
        """以短 timeout 檢查 Chat Service readiness。"""
