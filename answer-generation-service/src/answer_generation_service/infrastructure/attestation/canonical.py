"""Answer 端獨立實作的 RFC 8785 canonical generation context contract。"""

from __future__ import annotations

import base64
import hashlib
from typing import Any

import rfc8785

from answer_generation_service.domain.entities import ApprovedContext, ApprovedEvidence, Question

QUESTION_DOMAIN_SEPARATOR = b"helpdesk-rag/question/v1\0"
CONTEXT_DOMAIN_SEPARATOR = b"helpdesk-rag/context/v1\0"


def question_digest(question: Question) -> str:
    value = {"language": question.language, "question": question.text}
    return _digest(QUESTION_DOMAIN_SEPARATOR, value)


def context_digest(context: ApprovedContext) -> str:
    value = {"evidence": [_canonical_evidence(item) for item in context.evidence]}
    return _digest(CONTEXT_DOMAIN_SEPARATOR, value)


def _canonical_evidence(item: ApprovedEvidence) -> dict[str, object]:
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
    digest = hashlib.sha256(domain_separator + rfc8785.dumps(value)).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
