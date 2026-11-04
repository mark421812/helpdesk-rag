"""只信任本機 key ring 的 Ed25519 compact JWS 驗證器。"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from answer_generation_service.domain.entities import ApprovedContext, Question
from answer_generation_service.domain.exceptions import UnauthorizedContextError
from answer_generation_service.infrastructure.attestation.canonical import (
    context_digest,
    question_digest,
)

EXPECTED_HEADER_FIELDS = {"alg", "kid", "typ"}
EXPECTED_CLAIM_FIELDS = {
    "iss",
    "aud",
    "iat",
    "nbf",
    "exp",
    "jti",
    "context_schema_version",
    "policy_version",
    "question_sha256",
    "context_sha256",
}


class Ed25519ContextAttestationVerifier:
    def __init__(
        self,
        *,
        public_key_paths: dict[str, str],
        issuer: str,
        audience: str,
        context_schema_version: str,
        accepted_policy_versions: tuple[str, ...] = ("retrieval-policy-v1",),
        active_prompt_version: str = "grounded-answer-v1",
        policy_prompt_bindings: dict[str, tuple[str, ...]] | None = None,
        clock_skew_seconds: int,
        max_token_lifetime_seconds: int,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._public_key_paths = {key_id: Path(path) for key_id, path in public_key_paths.items()}
        self._issuer = issuer
        self._audience = audience
        self._context_schema_version = context_schema_version
        self._accepted_policy_versions = frozenset(accepted_policy_versions)
        if not self._accepted_policy_versions or any(
            not version.strip() for version in self._accepted_policy_versions
        ):
            raise ValueError("accepted_policy_versions must contain non-blank versions")
        self._active_prompt_version = active_prompt_version
        self._policy_prompt_bindings = policy_prompt_bindings or {
            "retrieval-policy-v1": ("grounded-answer-v1",)
        }
        self._clock_skew_seconds = clock_skew_seconds
        self._max_token_lifetime_seconds = max_token_lifetime_seconds
        self._clock = clock

    def verify(
        self,
        question: Question,
        context: ApprovedContext,
        token: str,
    ) -> None:
        try:
            encoded_header, encoded_claims, encoded_signature = token.split(".")
        except ValueError as exc:
            raise UnauthorizedContextError("Invalid context attestation") from exc

        header = self._decode_json(encoded_header)
        if set(header) != EXPECTED_HEADER_FIELDS:
            raise UnauthorizedContextError("Invalid context attestation")
        if header.get("alg") != "EdDSA" or header.get("typ") != "JWT":
            raise UnauthorizedContextError("Invalid context attestation")
        key_id = header.get("kid")
        if not isinstance(key_id, str) or key_id not in self._public_key_paths:
            raise UnauthorizedContextError("Unknown context attestation key")

        public_key = self._load_public_key(key_id)
        try:
            signature = _decode_base64url(encoded_signature)
            public_key.verify(
                signature,
                f"{encoded_header}.{encoded_claims}".encode("ascii"),
            )
        except (InvalidSignature, ValueError, binascii.Error) as exc:
            raise UnauthorizedContextError("Invalid context attestation") from exc

        claims = self._decode_json(encoded_claims)
        if set(claims) != EXPECTED_CLAIM_FIELDS:
            raise UnauthorizedContextError("Invalid context attestation claims")
        self._validate_claims(claims)
        if not hmac.compare_digest(
            _required_string(claims, "question_sha256"), question_digest(question)
        ):
            raise UnauthorizedContextError("Question does not match context attestation")
        if not hmac.compare_digest(
            _required_string(claims, "context_sha256"), context_digest(context)
        ):
            raise UnauthorizedContextError("Evidence does not match context attestation")

        try:
            UUID(_required_string(claims, "jti"))
        except ValueError as exc:
            raise UnauthorizedContextError("Invalid context attestation token ID") from exc

    def is_ready(self) -> bool:
        if not self._public_key_paths:
            return False
        try:
            for key_id in self._public_key_paths:
                self._load_public_key(key_id)
        except UnauthorizedContextError:
            return False
        return True

    def _validate_claims(self, claims: dict[str, Any]) -> None:
        if _required_string(claims, "iss") != self._issuer:
            raise UnauthorizedContextError("Invalid context attestation issuer")
        if _required_string(claims, "aud") != self._audience:
            raise UnauthorizedContextError("Invalid context attestation audience")
        if _required_string(claims, "context_schema_version") != self._context_schema_version:
            raise UnauthorizedContextError("Unsupported context attestation contract")
        policy_version = _required_string(claims, "policy_version")
        if policy_version not in self._accepted_policy_versions:
            raise UnauthorizedContextError("Unsupported retrieval policy version")
        if self._active_prompt_version not in self._policy_prompt_bindings.get(policy_version, ()):
            raise UnauthorizedContextError("Retrieval policy does not approve the active prompt")
        _required_string(claims, "question_sha256")
        _required_string(claims, "context_sha256")
        issued_at = _required_int(claims, "iat")
        not_before = _required_int(claims, "nbf")
        expires_at = _required_int(claims, "exp")
        now = int(self._clock())
        skew = self._clock_skew_seconds
        if issued_at > now + skew or not_before > now + skew or expires_at < now - skew:
            raise UnauthorizedContextError("Context attestation is not currently valid")
        if expires_at <= issued_at or expires_at - issued_at > self._max_token_lifetime_seconds:
            raise UnauthorizedContextError("Context attestation lifetime is invalid")

    @staticmethod
    def _decode_json(encoded: str) -> dict[str, Any]:
        try:
            payload: Any = json.loads(_decode_base64url(encoded))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, binascii.Error) as exc:
            raise UnauthorizedContextError("Invalid context attestation") from exc
        if not isinstance(payload, dict) or not all(isinstance(key, str) for key in payload):
            raise UnauthorizedContextError("Invalid context attestation")
        return payload

    def _load_public_key(self, key_id: str) -> Ed25519PublicKey:
        try:
            pem = self._public_key_paths[key_id].read_bytes()
            loaded = serialization.load_pem_public_key(pem)
        except (KeyError, OSError, ValueError, TypeError) as exc:
            raise UnauthorizedContextError("Context attestation key is unavailable") from exc
        if not isinstance(loaded, Ed25519PublicKey):
            raise UnauthorizedContextError("Context attestation key must be Ed25519")
        return loaded


def _decode_base64url(value: str) -> bytes:
    if not value or any(character.isspace() for character in value):
        raise ValueError("invalid base64url")
    return base64.b64decode(
        value + "=" * (-len(value) % 4),
        altchars=b"-_",
        validate=True,
    )


def _required_string(claims: dict[str, Any], name: str) -> str:
    value = claims.get(name)
    if not isinstance(value, str) or not value.strip():
        raise UnauthorizedContextError("Invalid context attestation claims")
    return value


def _required_int(claims: dict[str, Any], name: str) -> int:
    value = claims.get(name)
    if type(value) is not int:
        raise UnauthorizedContextError("Invalid context attestation claims")
    return value
