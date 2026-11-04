from __future__ import annotations

from uuid import UUID

from answer_generation_service.domain.entities import (
    ApprovedContext,
    ApprovedEvidence,
    Governance,
    Question,
    SafeCitation,
)
from answer_generation_service.domain.enums import Confidentiality


def question(text: str = "遠端工作規定？") -> Question:
    return Question(text=text, language="zh-TW")


def context(*, content: str = "員工每週可以遠端工作兩天。") -> ApprovedContext:
    return ApprovedContext(
        evidence=(
            ApprovedEvidence(
                rank=1,
                chunk_id=UUID("00000000-0000-0000-0000-000000000001"),
                document_id=UUID("00000000-0000-0000-0000-000000000002"),
                version_id=UUID("00000000-0000-0000-0000-000000000003"),
                content=content,
                document_type="policy",
                token_count=20,
                citation=SafeCitation(title="遠端工作辦法", section_path="第三條", page_number=3),
                governance=Governance(
                    confidentiality=Confidentiality.INTERNAL,
                    language="zh-TW",
                    external_llm_allowed=True,
                ),
            ),
        )
    )


def request_body(*, question_text: str = "遠端工作規定？") -> dict[str, object]:
    return {
        "question": question_text,
        "language": "zh-TW",
        "evidence": [
            {
                "rank": 1,
                "chunk_id": "00000000-0000-0000-0000-000000000001",
                "document_id": "00000000-0000-0000-0000-000000000002",
                "version_id": "00000000-0000-0000-0000-000000000003",
                "score": 0.91,
                "content": "員工每週可以遠端工作兩天。",
                "document_type": "policy",
                "token_count": 20,
                "citation": {
                    "title": "遠端工作辦法",
                    "section_path": "第三條",
                    "page_number": 3,
                },
                "governance": {
                    "confidentiality": "internal",
                    "language": "zh-TW",
                    "external_llm_allowed": True,
                },
            }
        ],
        "context_attestation": "signed-test-context",
    }
