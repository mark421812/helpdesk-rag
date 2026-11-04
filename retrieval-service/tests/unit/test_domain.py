from dataclasses import fields
from uuid import uuid4

import pytest

from retrieval_service.domain.entities import (
    Citation,
    Evidence,
    Governance,
    PrincipalContext,
    RetrievalResult,
)
from retrieval_service.domain.enums import Confidentiality, RetrievalStatus
from retrieval_service.domain.exceptions import DomainValidationError


def _evidence(*, rank: int, external_llm_allowed: bool) -> Evidence:
    return Evidence(
        rank=rank,
        chunk_id=uuid4(),
        document_id=uuid4(),
        version_id=uuid4(),
        score=0.9,
        content="公司規章內容",
        document_type="policy",
        token_count=10,
        citation=Citation(
            title="遠端工作規章",
            section_path="出勤",
            page_number=2,
        ),
        governance=Governance(
            confidentiality=Confidentiality.INTERNAL,
            language="zh-TW",
            external_llm_allowed=external_llm_allowed,
        ),
    )


def test_citation_contract_cannot_expose_a_filesystem_path() -> None:
    assert {field.name for field in fields(Citation)} == {
        "title",
        "section_path",
        "page_number",
    }


def test_principal_rejects_duplicate_groups() -> None:
    with pytest.raises(DomainValidationError, match="duplicates"):
        PrincipalContext(user_id="employee-1", group_ids=("group-1", "group-1"))


def test_principal_always_requires_a_user_to_preserve_personal_deny_rules() -> None:
    with pytest.raises(DomainValidationError, match="user_id"):
        PrincipalContext(user_id="   ")


def test_result_rejects_restricted_content_in_llm_context() -> None:
    with pytest.raises(DomainValidationError, match="forbidden"):
        RetrievalResult(
            status=RetrievalStatus.EVIDENCE_FOUND,
            llm_context=(_evidence(rank=1, external_llm_allowed=False),),
            restricted_evidence=(),
            candidate_count=1,
            model_id="model",
            model_revision="revision",
        )


def test_result_requires_blocked_status_when_only_restricted_evidence_exists() -> None:
    with pytest.raises(DomainValidationError, match="external_generation_blocked"):
        RetrievalResult(
            status=RetrievalStatus.EVIDENCE_FOUND,
            llm_context=(),
            restricted_evidence=(_evidence(rank=1, external_llm_allowed=False),),
            candidate_count=1,
            model_id="model",
            model_revision="revision",
        )


def test_result_requires_each_evidence_collection_to_be_rank_ordered() -> None:
    with pytest.raises(DomainValidationError, match="ordered by ascending rank"):
        RetrievalResult(
            status=RetrievalStatus.EVIDENCE_FOUND,
            llm_context=(
                _evidence(rank=2, external_llm_allowed=True),
                _evidence(rank=1, external_llm_allowed=True),
            ),
            restricted_evidence=(),
            candidate_count=2,
            model_id="model",
            model_revision="revision",
        )
