from __future__ import annotations

from dataclasses import replace

import pytest

from answer_generation_service.domain.entities import ApprovedContext, Governance
from answer_generation_service.domain.enums import Confidentiality
from answer_generation_service.domain.exceptions import DomainValidationError
from tests.factories import context


def test_restricted_evidence_cannot_become_approved_domain_context() -> None:
    with pytest.raises(DomainValidationError, match="forbidden"):
        Governance(
            confidentiality=Confidentiality.RESTRICTED,
            language="zh-TW",
            external_llm_allowed=False,
        )


def test_context_rejects_duplicate_chunks() -> None:
    item = context().evidence[0]
    second = replace(item, rank=2)

    with pytest.raises(DomainValidationError, match="chunk_id"):
        ApprovedContext(evidence=(item, second))


def test_mixed_retrieval_ranks_may_be_non_contiguous_but_must_be_ascending() -> None:
    item = context().evidence[0]
    context_with_original_rank = ApprovedContext(evidence=(replace(item, rank=2),))

    assert context_with_original_rank.evidence[0].rank == 2
