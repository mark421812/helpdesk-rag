"""以 Ed25519 compact JWS 簽發短效 Retrieval context attestation。"""

from __future__ import annotations

import base64
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import rfc8785
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from retrieval_service.domain.entities import Evidence, RetrievalQuery
from retrieval_service.domain.exceptions import DependencyUnavailableError
from retrieval_service.infrastructure.attestation.canonical import (
    context_digest,
    question_digest,
)

CONTEXT_SCHEMA_VERSION = "retrieval-context-v1"


class Ed25519ContextAttestor:
    """從唯讀 secret mount 載入私鑰並建立標準 compact JWS。

    ``clock`` 可在單元測試注入固定時間；正式組裝則使用 ``time.time``。
    """

    def __init__(
        self,
        *,
        private_key_path: str,
        key_id: str,
        ttl_seconds: int,
        issuer: str,
        audience: str,
        policy_version: str,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._private_key_path = Path(private_key_path)
        self._key_id = key_id
        self._ttl_seconds = ttl_seconds
        self._issuer = issuer
        self._audience = audience
        self._policy_version = policy_version
        self._clock = clock

    def issue(self, query: RetrievalQuery, evidence: tuple[Evidence, ...]) -> str:
        """只接受非空且全部明確允許外送的 evidence。"""

        if not evidence:
            raise DependencyUnavailableError("Cannot attest an empty generation context")
        if any(not item.governance.external_llm_allowed for item in evidence):
            raise DependencyUnavailableError(
                "Generation context attestation rejected forbidden evidence"
            )

        private_key = self._load_private_key()
        now = int(self._clock())
        protected_header: dict[str, Any] = {
            "alg": "EdDSA",
            "kid": self._key_id,
            "typ": "JWT",
        }
        claims: dict[str, Any] = {
            "iss": self._issuer,
            "aud": self._audience,
            "iat": now,
            "nbf": now,
            "exp": now + self._ttl_seconds,
            "jti": str(uuid4()),
            "context_schema_version": CONTEXT_SCHEMA_VERSION,
            "policy_version": self._policy_version,
            "question_sha256": question_digest(query),
            "context_sha256": context_digest(evidence),
        }
        encoded_header = _base64url(rfc8785.dumps(protected_header))
        encoded_claims = _base64url(rfc8785.dumps(claims))
        signing_input = f"{encoded_header}.{encoded_claims}".encode("ascii")
        signature = private_key.sign(signing_input)
        return f"{encoded_header}.{encoded_claims}.{_base64url(signature)}"

    def is_ready(self) -> bool:
        try:
            self._load_private_key()
        except DependencyUnavailableError:
            return False
        return True

    def _load_private_key(self) -> Ed25519PrivateKey:
        try:
            pem = self._private_key_path.read_bytes()
            loaded = serialization.load_pem_private_key(pem, password=None)
        except (OSError, ValueError, TypeError) as exc:
            raise DependencyUnavailableError(
                "Retrieval context signing key is unavailable"
            ) from exc
        if not isinstance(loaded, Ed25519PrivateKey):
            raise DependencyUnavailableError("Retrieval context signing key must be Ed25519")
        return loaded


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")
