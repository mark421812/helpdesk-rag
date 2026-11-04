"""Phase 2 deterministic Query Processor 的精確輸出與 invalid path tests。"""

from __future__ import annotations

import pytest

from chat_service.application.query_processor import QueryProcessor
from chat_service.domain.exceptions import DomainValidationError


def test_processing_order_preserves_original_and_internal_whitespace() -> None:
    result = QueryProcessor().process("  A\r\nB  \u0065\u0301   C  ", "  zh-TW  ")
    assert result.original_question == "  A\r\nB  \u0065\u0301   C  "
    assert result.processed_query == "A\nB  é   C"
    assert result.language == "zh-TW"


def test_crlf_is_normalized_but_lone_carriage_return_is_not_rewritten() -> None:
    result = QueryProcessor().process("A\r\nB\rC")
    assert result.processed_query == "A\nB\rC"


def test_none_language_is_preserved_without_language_detection() -> None:
    result = QueryProcessor().process("company policy", None)
    assert result.language is None


@pytest.mark.parametrize("question", ["", "   ", "\n\t"])
def test_blank_question_is_rejected(question: str) -> None:
    with pytest.raises(DomainValidationError):
        QueryProcessor().process(question)


def test_question_and_processed_query_are_bounded() -> None:
    with pytest.raises(DomainValidationError):
        QueryProcessor().process("x" * 20_001)
    with pytest.raises(DomainValidationError):
        QueryProcessor().process(" " + ("x" * 20_000) + " ")


@pytest.mark.parametrize("language", ["", " ", "x" * 21, " x "])
def test_language_trim_and_length_rules(language: str) -> None:
    if language == " x ":
        assert QueryProcessor().process("question", language).language == "x"
        return
    with pytest.raises(DomainValidationError):
        QueryProcessor().process("question", language)
