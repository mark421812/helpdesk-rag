"""以 in-process HTTP 驗證 Retrieval → Answer 的真實跨服務契約。

測試使用 synthetic candidates、暫存 Ed25519 key pair 與 Fake Provider，不啟動 Docker、
不連接 KIS/外部 LLM，也不寫入使用者的 PostgreSQL 或 Qdrant。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from retrieval_service.application.dtos import (
    KnowledgeSearchCriteria,
    KnowledgeSearchResult,
    RetrievalPolicy,
)
from retrieval_service.application.use_cases import RetrieveEvidence
from retrieval_service.bootstrap import Runtime as RetrievalRuntime
from retrieval_service.domain.entities import CandidateChunk
from retrieval_service.domain.enums import Confidentiality
from retrieval_service.infrastructure.attestation.ed25519 import Ed25519ContextAttestor
from retrieval_service.infrastructure.config import Settings as RetrievalSettings
from retrieval_service.infrastructure.identity.development import (
    DevelopmentHeaderIdentityProvider,
)
from retrieval_service.presentation.app import create_app as create_retrieval_app

from answer_generation_service.application.dtos import (
    GenerationPolicy,
    OutboundPolicyDecision,
    PromptMessage,
)
from answer_generation_service.application.prompt_builder import GroundedPromptBuilder
from answer_generation_service.application.use_cases import GenerateGroundedAnswer
from answer_generation_service.bootstrap import Runtime as AnswerRuntime
from answer_generation_service.infrastructure.attestation.ed25519_verifier import (
    Ed25519ContextAttestationVerifier,
)
from answer_generation_service.infrastructure.config import Settings as AnswerSettings
from answer_generation_service.infrastructure.llm.fake import FakeLLMProvider
from answer_generation_service.presentation.app import create_app as create_answer_app

QUESTION = "What is the synthetic remote-work allowance?"
ALLOWED_CONTENT = "Synthetic public policy: employees may work remotely two days per week."
RESTRICTED_CONTENT = "Synthetic restricted appendix that must never reach the provider."


@dataclass(slots=True)
class SyntheticKnowledgeSearch:
    candidates: tuple[CandidateChunk, ...]
    requests: list[KnowledgeSearchCriteria] = field(default_factory=list)

    async def search(self, criteria: KnowledgeSearchCriteria) -> KnowledgeSearchResult:
        self.requests.append(criteria)
        return KnowledgeSearchResult(
            candidates=self.candidates,
            model_id="synthetic-embedding-model",
            model_revision="contract-v1",
        )

    async def is_ready(self) -> bool:
        return True


class AllowSyntheticOutboundPolicy:
    def evaluate(self, question: object, context: object) -> OutboundPolicyDecision:
        return OutboundPolicyDecision(allowed=True, policy_version="synthetic-policy-v1")

    def is_ready(self) -> bool:
        return True


class FixedTokenCounter:
    def count_messages(self, messages: tuple[PromptMessage, ...]) -> int:
        return 100


def _candidate(*, content: str, score: float, external_llm_allowed: bool) -> CandidateChunk:
    return CandidateChunk(
        chunk_id=uuid4(),
        document_id=uuid4(),
        version_id=uuid4(),
        score=score,
        content=content,
        title="Synthetic Remote Work Policy",
        document_type="policy",
        section_path="Remote Work",
        page_number=3,
        confidentiality=(
            Confidentiality.PUBLIC if external_llm_allowed else Confidentiality.RESTRICTED
        ),
        language="en",
        external_llm_allowed=external_llm_allowed,
        token_count=20,
        chunk_index=0,
    )


def _write_key_pair(directory: Path) -> tuple[Path, Path]:
    private_key = Ed25519PrivateKey.generate()
    private_path = directory / "retrieval-private.pem"
    public_path = directory / "retrieval-public.pem"
    private_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    public_path.write_bytes(
        private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return private_path, public_path


def _retrieval_runtime(
    private_key_path: Path,
    search: SyntheticKnowledgeSearch,
) -> RetrievalRuntime:
    return RetrievalRuntime(
        knowledge_search=search,
        identity_provider=DevelopmentHeaderIdentityProvider(),
        context_attestor=Ed25519ContextAttestor(
            private_key_path=str(private_key_path),
            key_id="retrieval-contract-001",
            ttl_seconds=120,
            issuer="retrieval-service",
            audience="answer-generation-service",
            policy_version="retrieval-policy-v1",
        ),
        retrieve_evidence=RetrieveEvidence(
            search,
            RetrievalPolicy(
                candidate_multiplier=3,
                max_candidate_limit=50,
                max_context_tokens=3_000,
                max_chunks_per_document=3,
            ),
        ),
    )


def _answer_runtime(public_key_path: Path) -> tuple[AnswerRuntime, FakeLLMProvider]:
    provider = FakeLLMProvider()
    verifier = Ed25519ContextAttestationVerifier(
        public_key_paths={"retrieval-contract-001": str(public_key_path)},
        issuer="retrieval-service",
        audience="answer-generation-service",
        context_schema_version="retrieval-context-v1",
        clock_skew_seconds=30,
        max_token_lifetime_seconds=600,
    )
    outbound_policy = AllowSyntheticOutboundPolicy()
    use_case = GenerateGroundedAnswer(
        attestation_verifier=verifier,
        outbound_policy=outbound_policy,
        token_counter=FixedTokenCounter(),
        prompt_builder=GroundedPromptBuilder(),
        provider=provider,
        generation_policy=GenerationPolicy(
            max_context_tokens=3_000,
            max_prompt_tokens=12_000,
            max_output_tokens=1_024,
            max_answer_characters=20_000,
            temperature=0,
            prompt_version="grounded-answer-v1",
        ),
    )
    return (
        AnswerRuntime(
            provider=provider,
            attestation_verifier=verifier,
            outbound_policy=outbound_policy,
            generate_answer=use_case,
        ),
        provider,
    )


def _answer_body(retrieval: dict[str, Any]) -> dict[str, Any]:
    """模擬未來 Chat 的 allowlist projection，絕不轉送 restricted_evidence。"""

    if retrieval.get("status") != "evidence_found":
        raise ValueError("Retrieval did not approve external answer generation")
    attestation = retrieval.get("context_attestation")
    llm_context = retrieval.get("llm_context")
    if not isinstance(attestation, str) or not isinstance(llm_context, list) or not llm_context:
        raise ValueError("Retrieval returned no attested LLM context")
    return {
        "question": QUESTION,
        "language": "en",
        # Evidence item 本身可原樣保留（含 diagnostic score）；只能取 llm_context，不能取
        # restricted_evidence，也不能轉送 Retrieval response 的其他 top-level 欄位。
        "evidence": llm_context,
        "context_attestation": attestation,
    }


@pytest.mark.asyncio
async def test_retrieval_allowed_context_reaches_answer_but_restricted_content_does_not(
    tmp_path: Path,
) -> None:
    private_path, public_path = _write_key_pair(tmp_path)
    search = SyntheticKnowledgeSearch(
        candidates=(
            _candidate(
                content=RESTRICTED_CONTENT,
                score=0.99,
                external_llm_allowed=False,
            ),
            _candidate(
                content=ALLOWED_CONTENT,
                score=0.91,
                external_llm_allowed=True,
            ),
        )
    )
    retrieval_runtime = _retrieval_runtime(private_path, search)
    answer_runtime, provider = _answer_runtime(public_path)
    retrieval_app = create_retrieval_app(
        settings=RetrievalSettings(_env_file=None, api_internal_key="retrieval-key"),
        runtime=retrieval_runtime,
    )
    answer_app = create_answer_app(
        settings=AnswerSettings(_env_file=None, api_internal_key="answer-key"),
        runtime=answer_runtime,
    )

    async with retrieval_app.router.lifespan_context(retrieval_app):
        async with answer_app.router.lifespan_context(answer_app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=retrieval_app),
                base_url="http://retrieval.test",
            ) as retrieval_client:
                retrieval_response = await retrieval_client.post(
                    "/api/v1/retrieve",
                    json={"query": QUESTION, "language": "en", "limit": 2},
                    headers={
                        "x-internal-api-key": "retrieval-key",
                        "x-dev-user-id": "synthetic-user",
                        "x-request-id": "retrieval-answer-contract",
                    },
                )

            assert retrieval_response.status_code == 200
            retrieval_payload = retrieval_response.json()
            assert retrieval_payload["status"] == "evidence_found"
            assert [item["content"] for item in retrieval_payload["llm_context"]] == [
                ALLOWED_CONTENT
            ]
            assert [item["content"] for item in retrieval_payload["restricted_evidence"]] == [
                RESTRICTED_CONTENT
            ]
            assert retrieval_payload["llm_context"][0]["score"] == 0.91

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=answer_app),
                base_url="http://answer.test",
            ) as answer_client:
                answer_response = await answer_client.post(
                    "/api/v1/answers",
                    json=_answer_body(retrieval_payload),
                    headers={
                        "x-internal-api-key": "answer-key",
                        "x-request-id": "retrieval-answer-contract",
                    },
                )

    assert answer_response.status_code == 200, answer_response.text
    assert answer_response.json()["status"] == "answered"
    assert '"score"' not in answer_response.text
    assert len(provider.requests) == 1
    prompt = json.loads(provider.requests[0].messages[1].content)
    serialized_prompt = json.dumps(prompt, ensure_ascii=False)
    assert ALLOWED_CONTENT in serialized_prompt
    assert RESTRICTED_CONTENT not in serialized_prompt
    assert "score" not in serialized_prompt


@pytest.mark.asyncio
async def test_restricted_only_retrieval_is_not_projected_to_answer(tmp_path: Path) -> None:
    private_path, _ = _write_key_pair(tmp_path)
    search = SyntheticKnowledgeSearch(
        candidates=(
            _candidate(
                content=RESTRICTED_CONTENT,
                score=0.99,
                external_llm_allowed=False,
            ),
        )
    )
    retrieval_app = create_retrieval_app(
        settings=RetrievalSettings(_env_file=None, api_internal_key="retrieval-key"),
        runtime=_retrieval_runtime(private_path, search),
    )

    async with retrieval_app.router.lifespan_context(retrieval_app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=retrieval_app),
            base_url="http://retrieval.test",
        ) as retrieval_client:
            response = await retrieval_client.post(
                "/api/v1/retrieve",
                json={"query": QUESTION, "language": "en"},
                headers={
                    "x-internal-api-key": "retrieval-key",
                    "x-dev-user-id": "synthetic-user",
                },
            )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "external_generation_blocked"
    assert payload["llm_context"] == []
    assert payload["context_attestation"] is None
    with pytest.raises(ValueError, match="did not approve"):
        _answer_body(payload)
