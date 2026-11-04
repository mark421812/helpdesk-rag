"""Deterministic Query Processor v1。

這個 application service 不呼叫 LLM，也不讀取 conversation history。它保留 request 原文，
再依規格固定執行 CRLF/LF、Unicode NFC 與頭尾 trim；使用明確的處理順序是為了讓後續
request fingerprint、Retrieval query 與 Answer question 永遠使用同一個 processed value。
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from chat_service.domain.exceptions import DomainValidationError

MAX_QUESTION_LENGTH = 20_000
MAX_LANGUAGE_LENGTH = 20


@dataclass(frozen=True, slots=True)
class ProcessedQuery:
    """Query Processor 的 immutable result。"""

    original_question: str
    processed_query: str
    language: str | None


class QueryProcessor:
    """執行 Chat v1 的 deterministic question/language normalization。"""

    def process(self, question: str, language: str | None = None) -> ProcessedQuery:
        """依規格順序處理問題；不壓縮內部 whitespace，也不推測語言。"""

        self._validate_question(question)
        normalized_question = question.replace("\r\n", "\n")
        normalized_question = unicodedata.normalize("NFC", normalized_question)
        processed_query = normalized_question.strip()
        self._validate_processed_query(processed_query)
        normalized_language = self._normalize_language(language)
        return ProcessedQuery(
            original_question=question,
            processed_query=processed_query,
            language=normalized_language,
        )

    @staticmethod
    def _validate_question(question: str) -> None:
        """驗證 public request 原文，不把原文放入 exception。"""

        if not isinstance(question, str):
            raise DomainValidationError("question must be text")
        if len(question) < 1 or len(question) > MAX_QUESTION_LENGTH:
            raise DomainValidationError("question length is outside the allowed range")
        if not question.strip():
            raise DomainValidationError("question must not be blank")

    @staticmethod
    def _validate_processed_query(processed_query: str) -> None:
        """確保 normalization 不會把合法輸入變成空白或超過上限。"""

        if len(processed_query) < 1 or len(processed_query) > MAX_QUESTION_LENGTH:
            raise DomainValidationError("processed_query length is outside the allowed range")
        if not processed_query.strip():
            raise DomainValidationError("processed_query must not be blank")

    @staticmethod
    def _normalize_language(language: str | None) -> str | None:
        """language 只 trim；None 保持 None，不進行語言偵測或預設。"""

        if language is None:
            return None
        if not isinstance(language, str):
            raise DomainValidationError("language must be text")
        if len(language) < 1 or len(language) > MAX_LANGUAGE_LENGTH:
            raise DomainValidationError("language length is outside the allowed range")
        normalized = language.strip()
        if len(normalized) < 1 or len(normalized) > MAX_LANGUAGE_LENGTH:
            raise DomainValidationError("language length is outside the allowed range")
        return normalized
