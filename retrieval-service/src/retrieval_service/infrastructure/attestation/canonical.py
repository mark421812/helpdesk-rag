"""建立 Retrieval 與 Answer 共同遵守的 canonical generation context。

這個模組的輸出規格屬於跨服務 contract。Answer Service 有自己的獨立實作，兩邊透過
相同 golden vectors 測試，而不是互相 import 原始碼，避免兩個服務在部署時耦合。
"""

from __future__ import annotations

import base64
import hashlib
from typing import Any

import rfc8785

from retrieval_service.domain.entities import Evidence, RetrievalQuery

QUESTION_DOMAIN_SEPARATOR = b"helpdesk-rag/question/v1\0"
CONTEXT_DOMAIN_SEPARATOR = b"helpdesk-rag/context/v1\0"


def canonical_question(query: RetrievalQuery) -> dict[str, object]:
    """建立問題的固定 JSON 形狀；None 會被保留為 JSON null。"""

    return {"language": query.language, "question": query.text}


def canonical_context(evidence: tuple[Evidence, ...]) -> dict[str, object]:
    """只序列化 Answer 會使用且必須受簽章保護的 evidence 欄位。"""

    return {"evidence": [_canonical_evidence(item) for item in evidence]}


def question_digest(query: RetrievalQuery) -> str:
    return _digest(QUESTION_DOMAIN_SEPARATOR, canonical_question(query))


def context_digest(evidence: tuple[Evidence, ...]) -> str:
    return _digest(CONTEXT_DOMAIN_SEPARATOR, canonical_context(evidence))


def canonical_json(value: Any) -> bytes:
    """使用 RFC 8785 JCS 產生跨語言、跨服務穩定的 UTF-8 bytes。"""

    return rfc8785.dumps(value)


def _canonical_evidence(item: Evidence) -> dict[str, object]:
    return {
        "rank": item.rank,
        "chunk_id": str(item.chunk_id),
        "document_id": str(item.document_id),
        "version_id": str(item.version_id),
        "content": item.content,
        "document_type": item.document_type,
        "token_count": item.token_count,
        "citation": {
            "title": item.citation.title,
            "section_path": item.citation.section_path,
            "page_number": item.citation.page_number,
        },
        "governance": {
            "confidentiality": item.governance.confidentiality.value,
            "language": item.governance.language,
            "external_llm_allowed": item.governance.external_llm_allowed,
        },
    }


def _digest(domain_separator: bytes, value: Any) -> str:
    digest = hashlib.sha256(domain_separator + canonical_json(value)).digest()
    # JWT/JWS 慣例使用無 padding 的 URL-safe base64。
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
