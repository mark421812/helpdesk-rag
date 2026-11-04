from __future__ import annotations

import base64
import json
from pathlib import Path
from uuid import UUID

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from retrieval_service.domain.entities import Citation, Evidence, Governance, RetrievalQuery
from retrieval_service.domain.enums import Confidentiality
from retrieval_service.infrastructure.attestation.canonical import (
    context_digest,
    question_digest,
)
from retrieval_service.infrastructure.attestation.ed25519 import Ed25519ContextAttestor


def _evidence() -> tuple[Evidence, ...]:
    return (
        Evidence(
            rank=1,
            chunk_id=UUID("00000000-0000-0000-0000-000000000001"),
            document_id=UUID("00000000-0000-0000-0000-000000000002"),
            version_id=UUID("00000000-0000-0000-0000-000000000003"),
            score=0.91,
            content="員工每週可以遠端工作兩天。\n第二行保持原樣。",
            document_type="policy",
            token_count=20,
            citation=Citation(title="遠端工作辦法", section_path="第三條", page_number=3),
            governance=Governance(
                confidentiality=Confidentiality.INTERNAL,
                language="zh-TW",
                external_llm_allowed=True,
            ),
        ),
    )


def _write_private_key(path: Path) -> Ed25519PrivateKey:
    key = Ed25519PrivateKey.generate()
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return key


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def test_attestation_is_signed_and_binds_question_and_context(tmp_path: Path) -> None:
    private_key_path = tmp_path / "retrieval-private.pem"
    private_key = _write_private_key(private_key_path)
    query = RetrievalQuery(text="遠端工作規定？", limit=8, language="zh-TW")
    evidence = _evidence()
    attestor = Ed25519ContextAttestor(
        private_key_path=str(private_key_path),
        key_id="retrieval-test-001",
        ttl_seconds=120,
        issuer="retrieval-service",
        audience="answer-generation-service",
        policy_version="retrieval-policy-v1",
        clock=lambda: 1_786_406_400.0,
    )

    token = attestor.issue(query, evidence)

    encoded_header, encoded_claims, encoded_signature = token.split(".")
    header = json.loads(_decode(encoded_header))
    claims = json.loads(_decode(encoded_claims))
    private_key.public_key().verify(
        _decode(encoded_signature),
        f"{encoded_header}.{encoded_claims}".encode("ascii"),
    )
    assert header == {"alg": "EdDSA", "kid": "retrieval-test-001", "typ": "JWT"}
    assert claims["question_sha256"] == question_digest(query)
    assert claims["context_sha256"] == context_digest(evidence)
    assert claims["question_sha256"] == "0FW70HiyoPMhdorysd9lM66evzScKuAPDh-ji4knm6U"
    assert claims["context_sha256"] == "m5aWJF_Gm77TX9bCuaNhtenUYia5tVPn2UgrtNK6lAI"
    assert claims["exp"] - claims["iat"] == 120
    assert attestor.is_ready() is True


def test_digest_changes_when_signed_text_changes() -> None:
    original = RetrievalQuery(text="遠端工作規定？", limit=8, language="zh-TW")
    changed = RetrievalQuery(text="遠端工作規定？ ", limit=8, language="zh-TW")

    assert question_digest(original) != question_digest(changed)


def test_missing_private_key_is_not_ready(tmp_path: Path) -> None:
    attestor = Ed25519ContextAttestor(
        private_key_path=str(tmp_path / "missing.pem"),
        key_id="retrieval-test-001",
        ttl_seconds=120,
        issuer="retrieval-service",
        audience="answer-generation-service",
        policy_version="retrieval-policy-v1",
    )

    assert attestor.is_ready() is False
