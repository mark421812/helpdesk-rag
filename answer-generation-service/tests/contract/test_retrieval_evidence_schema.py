from __future__ import annotations

from copy import deepcopy

import pytest
from pydantic import ValidationError

from answer_generation_service.application.prompt_builder import GroundedPromptBuilder
from answer_generation_service.infrastructure.attestation.canonical import context_digest
from answer_generation_service.presentation.app import _command
from answer_generation_service.presentation.schemas import GenerateAnswerRequest
from tests.factories import request_body


def _parsed_body(*, score: object = 0.91) -> GenerateAnswerRequest:
    body = request_body()
    evidence = body["evidence"]
    assert isinstance(evidence, list)
    item = evidence[0]
    assert isinstance(item, dict)
    item["score"] = score
    return GenerateAnswerRequest.model_validate(body)


def test_answer_accepts_retrieval_score_but_drops_it_before_the_domain() -> None:
    parsed = _parsed_body(score=0.91)

    assert parsed.evidence[0].score == 0.91
    command = _command(parsed)
    assert not hasattr(command.context.evidence[0], "score")


def test_retrieval_score_does_not_change_attestation_digest_or_prompt() -> None:
    first = _command(_parsed_body(score=0.91))
    second = _command(_parsed_body(score=-0.25))

    # Application 收到的 command 完全相同，因此 score 無法影響後續回答流程。
    assert first == second
    assert context_digest(first.context) == context_digest(second.context)
    first_messages = GroundedPromptBuilder().build(first.question, first.context)
    second_messages = GroundedPromptBuilder().build(second.question, second.context)
    assert first_messages == second_messages
    assert '"score"' not in first_messages[1].content


@pytest.mark.parametrize("score", [float("nan"), float("inf"), float("-inf"), "0.91", True])
def test_retrieval_score_must_be_a_finite_json_number(score: object) -> None:
    with pytest.raises(ValidationError):
        _parsed_body(score=score)


def test_retrieval_score_is_required_by_the_versioned_evidence_contract() -> None:
    body = deepcopy(request_body())
    evidence = body["evidence"]
    assert isinstance(evidence, list)
    item = evidence[0]
    assert isinstance(item, dict)
    item.pop("score")

    with pytest.raises(ValidationError):
        GenerateAnswerRequest.model_validate(body)
