from __future__ import annotations

import base64
import time
from pathlib import Path
from uuid import uuid4

import pytest
import rfc8785
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from answer_generation_service.domain.exceptions import UnauthorizedContextError
from answer_generation_service.infrastructure.attestation.canonical import (
    context_digest,
    question_digest,
)
from answer_generation_service.infrastructure.attestation.ed25519_verifier import (
    Ed25519ContextAttestationVerifier,
)
from tests.factories import context, question


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _key_pair(tmp_path: Path) -> tuple[Ed25519PrivateKey, Path]:
    private_key = Ed25519PrivateKey.generate()
    public_path = tmp_path / "retrieval-public.pem"
    public_path.write_bytes(
        private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return private_key, public_path


def _token(
    private_key: Ed25519PrivateKey,
    *,
    now: int,
    key_id: str = "retrieval-test-001",
    header_extra: dict[str, object] | None = None,
    question_hash: str | None = None,
    context_hash: str | None = None,
    policy_version: str = "retrieval-policy-v1",
) -> str:
    header = {"alg": "EdDSA", "kid": key_id, "typ": "JWT", **(header_extra or {})}
    claims = {
        "iss": "retrieval-service",
        "aud": "answer-generation-service",
        "iat": now,
        "nbf": now,
        "exp": now + 120,
        "jti": str(uuid4()),
        "context_schema_version": "retrieval-context-v1",
        "policy_version": policy_version,
        "question_sha256": question_hash or question_digest(question()),
        "context_sha256": context_hash or context_digest(context()),
    }
    encoded_header = _base64url(rfc8785.dumps(header))
    encoded_claims = _base64url(rfc8785.dumps(claims))
    signing_input = f"{encoded_header}.{encoded_claims}".encode("ascii")
    return f"{encoded_header}.{encoded_claims}.{_base64url(private_key.sign(signing_input))}"


def _verifier(public_path: Path, now: int) -> Ed25519ContextAttestationVerifier:
    return Ed25519ContextAttestationVerifier(
        public_key_paths={"retrieval-test-001": str(public_path)},
        issuer="retrieval-service",
        audience="answer-generation-service",
        context_schema_version="retrieval-context-v1",
        accepted_policy_versions=("retrieval-policy-v1",),
        clock_skew_seconds=30,
        max_token_lifetime_seconds=600,
        clock=lambda: float(now),
    )


def test_answer_digest_matches_retrieval_golden_vector() -> None:
    golden_context = context(content="員工每週可以遠端工作兩天。\n第二行保持原樣。")

    assert question_digest(question()) == "0FW70HiyoPMhdorysd9lM66evzScKuAPDh-ji4knm6U"
    assert context_digest(golden_context) == "m5aWJF_Gm77TX9bCuaNhtenUYia5tVPn2UgrtNK6lAI"


def test_valid_token_is_verified(tmp_path: Path) -> None:
    private_key, public_path = _key_pair(tmp_path)
    now = int(time.time())

    verified = _verifier(public_path, now).verify(
        question(), context(), _token(private_key, now=now)
    )

    assert verified is None


def test_unaccepted_retrieval_policy_version_is_rejected(tmp_path: Path) -> None:
    private_key, public_path = _key_pair(tmp_path)
    now = int(time.time())

    with pytest.raises(UnauthorizedContextError, match="policy version"):
        _verifier(public_path, now).verify(
            question(),
            context(),
            _token(private_key, now=now, policy_version="retired-retrieval-policy"),
        )


def test_signed_policy_must_approve_the_active_prompt_version(tmp_path: Path) -> None:
    private_key, public_path = _key_pair(tmp_path)
    now = int(time.time())
    verifier = Ed25519ContextAttestationVerifier(
        public_key_paths={"retrieval-test-001": str(public_path)},
        issuer="retrieval-service",
        audience="answer-generation-service",
        context_schema_version="retrieval-context-v1",
        accepted_policy_versions=("retrieval-policy-v1",),
        active_prompt_version="grounded-answer-v2",
        policy_prompt_bindings={"retrieval-policy-v1": ("grounded-answer-v1",)},
        clock_skew_seconds=30,
        max_token_lifetime_seconds=600,
        clock=lambda: float(now),
    )

    with pytest.raises(UnauthorizedContextError, match="active prompt"):
        verifier.verify(question(), context(), _token(private_key, now=now))


def test_modified_context_is_rejected(tmp_path: Path) -> None:
    private_key, public_path = _key_pair(tmp_path)
    now = int(time.time())

    with pytest.raises(UnauthorizedContextError, match="Evidence"):
        _verifier(public_path, now).verify(
            question(),
            context(content="被修改的內容"),
            _token(private_key, now=now),
        )


def test_unknown_key_and_embedded_key_header_are_rejected(tmp_path: Path) -> None:
    private_key, public_path = _key_pair(tmp_path)
    now = int(time.time())
    verifier = _verifier(public_path, now)

    with pytest.raises(UnauthorizedContextError):
        verifier.verify(question(), context(), _token(private_key, now=now, key_id="attacker"))
    with pytest.raises(UnauthorizedContextError):
        verifier.verify(
            question(),
            context(),
            _token(private_key, now=now, header_extra={"jwk": {"kty": "OKP"}}),
        )


def test_expired_token_is_rejected(tmp_path: Path) -> None:
    private_key, public_path = _key_pair(tmp_path)
    token = _token(private_key, now=1_000)

    with pytest.raises(UnauthorizedContextError, match="currently valid"):
        _verifier(public_path, 2_000).verify(question(), context(), token)
