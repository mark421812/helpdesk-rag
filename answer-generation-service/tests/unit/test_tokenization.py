from __future__ import annotations

from answer_generation_service.application.dtos import PromptMessage
from answer_generation_service.infrastructure.tokenization import ConservativeTokenCounter


def test_token_upper_bound_counts_every_utf8_byte() -> None:
    messages = (PromptMessage(role="system", content="A中"),)

    assert ConservativeTokenCounter().count_messages(messages) == 4 + len("A中".encode())
