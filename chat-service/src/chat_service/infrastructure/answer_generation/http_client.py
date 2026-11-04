"""Answer Generation Service 的 async HTTP gateway adapter。

這個 adapter 只接受 Application 的 ``AnswerGenerationRequest``，並以 allowlist 建立 Answer
body：question、language、allowed evidence 與 opaque attestation。它不可能接收
``RestrictedEvidence``，也不會解析、保存或重簽 attestation。Answer 沒有 idempotency
contract，因此本 adapter 對 POST 絕不自動 retry；所有 HTTP/JSON/schema/invariant 錯誤
都轉成不含 raw body、URL 或 secret 的 typed adapter error。
"""

from __future__ import annotations

import asyncio
import json
import math
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import ValidationError

from chat_service.application.dtos import AnswerGenerationRequest, AnswerGenerationResult
from chat_service.application.ports import AnswerGenerationGatewayPort
from chat_service.domain.entities import (
    AllowedEvidence,
    AnswerPart,
    Citation,
    GenerationDiagnostics,
)
from chat_service.domain.exceptions import DependencyError, DomainValidationError
from chat_service.infrastructure.answer_generation.schemas import (
    AllowedEvidenceRequestSchema,
    AnswerErrorSchema,
    AnswerHealthSchema,
    AnswerRequestSchema,
    AnswerResponseSchema,
)
from chat_service.infrastructure.http_support import (
    BoundedHttpResponse,
    ResponseBodyTooLargeError,
    UnsupportedResponseEncodingError,
    request_bounded,
)

_ANSWER_PATH = "/api/v1/answers"
_READY_PATH = "/health/ready"
_MAX_RETRY_AFTER_SECONDS = 86_400


class AnswerGenerationGatewayError(DependencyError):
    """可由後續 Application/Presentation layer 分類的安全 adapter error。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int | None = None,
        retry_after_seconds: int | None = None,
    ) -> None:
        self.code = code
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        super().__init__(code, message, retry_after_seconds=retry_after_seconds)


class AnswerGenerationGatewayConfigurationError(AnswerGenerationGatewayError):
    """下游 URL/key/timeout 設定不合法；不包含設定值本身。"""


class AnswerGenerationHttpClient(AnswerGenerationGatewayPort):
    """呼叫 Answer Generation 的 strict、non-retrying HTTP adapter。"""

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str,
        connect_timeout_seconds: float = 3.0,
        read_timeout_seconds: float = 85.0,
        total_timeout_seconds: float = 90.0,
        readiness_timeout_seconds: float = 5.0,
        max_response_body_bytes: int = 524_288,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = _validate_base_url(base_url)
        self._api_key = _validate_api_key(api_key)
        self._connect_timeout = _validate_positive(connect_timeout_seconds, "connect timeout")
        self._read_timeout = _validate_positive(read_timeout_seconds, "read timeout")
        self._total_timeout = _validate_positive(total_timeout_seconds, "total timeout")
        self._readiness_timeout = _validate_positive(
            readiness_timeout_seconds,
            "readiness timeout",
        )
        self._max_response_bytes = _validate_positive_int(
            max_response_body_bytes,
            "max response body bytes",
        )
        if self._connect_timeout > self._total_timeout:
            raise AnswerGenerationGatewayConfigurationError(
                "answer_configuration_error",
                "connect timeout must not exceed total timeout",
            )
        if self._read_timeout > self._total_timeout:
            raise AnswerGenerationGatewayConfigurationError(
                "answer_configuration_error",
                "read timeout must not exceed total timeout",
            )

        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )
        self._timeout = httpx.Timeout(
            connect=self._connect_timeout,
            read=self._read_timeout,
            write=self._connect_timeout,
            pool=self._connect_timeout,
        )

    async def generate(self, request: AnswerGenerationRequest) -> AnswerGenerationResult:
        """送出一個 allowlist Answer request；此方法永遠不 retry POST。"""

        try:
            payload = _request_payload(request)
        except (ValidationError, DomainValidationError, TypeError, ValueError):
            raise AnswerGenerationGatewayError(
                "answer_contract_error",
                "Answer request violates the configured contract",
            ) from None
        if not _header_value_is_safe(request.request_id):
            raise AnswerGenerationGatewayError(
                "answer_contract_error",
                "Answer request ID contains an invalid header character",
            )

        headers = {
            "accept": "application/json",
            "content-type": "application/json",
            "x-internal-api-key": self._api_key,
            "x-request-id": request.request_id,
        }
        try:
            async with asyncio.timeout(self._total_timeout):
                response = await request_bounded(
                    self._client,
                    "POST",
                    f"{self._base_url}{_ANSWER_PATH}",
                    headers=headers,
                    json_body=payload,
                    request_timeout=self._timeout,
                    max_response_body_bytes=self._max_response_bytes,
                )
        except httpx.TimeoutException:
            raise AnswerGenerationGatewayError(
                "provider_timeout",
                "Answer Generation Service timed out",
            ) from None
        except httpx.TransportError:
            raise AnswerGenerationGatewayError(
                "provider_unavailable",
                "Answer Generation Service is unavailable",
            ) from None
        except TimeoutError:
            raise AnswerGenerationGatewayError(
                "provider_timeout",
                "Answer Generation Service timed out",
            ) from None
        except ResponseBodyTooLargeError:
            raise AnswerGenerationGatewayError(
                "answer_contract_error",
                "Answer Generation Service response exceeded the size limit",
            ) from None
        except UnsupportedResponseEncodingError:
            raise AnswerGenerationGatewayError(
                "answer_contract_error",
                "Answer Generation Service returned an unsupported content encoding",
            ) from None
        except httpx.DecodingError:
            raise AnswerGenerationGatewayError(
                "answer_contract_error",
                "Answer Generation Service returned an undecodable response",
            ) from None

        if response.status_code != 200:
            raise self._map_error(response)
        payload_object = self._parse_json_object(response)
        try:
            parsed = AnswerResponseSchema.model_validate(payload_object)
            return _to_generation_result(parsed)
        except (ValidationError, DomainValidationError, TypeError, ValueError, KeyError):
            raise AnswerGenerationGatewayError(
                "answer_contract_error",
                "Answer Generation Service response violates the contract",
            ) from None

    async def is_ready(self) -> bool:
        """以短 timeout 執行 read-only readiness；不呼叫 generation endpoint。"""

        try:
            async with asyncio.timeout(self._readiness_timeout):
                response = await request_bounded(
                    self._client,
                    "GET",
                    f"{self._base_url}{_READY_PATH}",
                    headers={"accept": "application/json"},
                    json_body=None,
                    request_timeout=self._readiness_timeout,
                    max_response_body_bytes=self._max_response_bytes,
                )
            if response.status_code != 200:
                return False
            health = AnswerHealthSchema.model_validate(self._parse_json_object(response))
            return health.status == "ok" and bool(health.checks) and all(health.checks.values())
        except (
            httpx.HTTPError,
            TimeoutError,
            ResponseBodyTooLargeError,
            UnsupportedResponseEncodingError,
            AnswerGenerationGatewayError,
            ValidationError,
            ValueError,
        ):
            return False

    async def close(self) -> None:
        """若 client 由 adapter 建立，於 lifespan shutdown 釋放 connection pool。"""

        if self._owns_client:
            await self._client.aclose()

    def _parse_json_object(self, response: BoundedHttpResponse) -> dict[str, Any]:
        """只解析 JSON object；不把 raw response 放入錯誤文字。"""

        if not _is_json_content_type(response):
            raise AnswerGenerationGatewayError(
                "answer_contract_error",
                "Answer Generation Service returned an unexpected content type",
            )
        try:
            parsed: Any = json.loads(
                response.body.decode("utf-8"),
                parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
            )
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            raise AnswerGenerationGatewayError(
                "answer_contract_error",
                "Answer Generation Service returned invalid JSON",
            ) from None
        if not isinstance(parsed, dict):
            raise AnswerGenerationGatewayError(
                "answer_contract_error",
                "Answer Generation Service returned a non-object JSON value",
            )
        return parsed

    def _map_error(self, response: BoundedHttpResponse) -> AnswerGenerationGatewayError:
        """依下游 status 與安全 error code 分類，不解析或回傳 raw body。"""

        status = response.status_code
        downstream_code = _safe_error_code(response)
        if status == 401:
            return AnswerGenerationGatewayError(
                "answer_auth_misconfigured",
                "Answer Generation Service authentication is misconfigured",
                status_code=status,
            )
        if status == 403 and downstream_code == "context_attestation_invalid":
            return AnswerGenerationGatewayError(
                "answer_context_rejected",
                "Answer Generation Service rejected the context attestation",
                status_code=status,
            )
        if status == 403 and downstream_code == "outbound_policy_blocked":
            return AnswerGenerationGatewayError(
                "outbound_policy_blocked",
                "Answer Generation Service blocked outbound content by policy",
                status_code=status,
            )
        if status in {413, 422}:
            return AnswerGenerationGatewayError(
                "answer_contract_error",
                "Answer Generation Service rejected the request contract",
                status_code=status,
            )
        if status == 429:
            return AnswerGenerationGatewayError(
                "generation_busy",
                "Answer Generation Service is temporarily busy",
                status_code=status,
                retry_after_seconds=_retry_after_seconds(response.headers),
            )
        if status == 502:
            return AnswerGenerationGatewayError(
                "provider_contract_error",
                "The configured generation provider returned an invalid response",
                status_code=status,
            )
        if status == 503:
            return AnswerGenerationGatewayError(
                "provider_unavailable",
                "The configured generation provider is unavailable",
                status_code=status,
            )
        if status == 504:
            return AnswerGenerationGatewayError(
                "provider_timeout",
                "The configured generation provider timed out",
                status_code=status,
            )
        return AnswerGenerationGatewayError(
            "answer_service_error",
            "Answer Generation Service returned an unexpected error",
            status_code=status,
        )


def _validate_base_url(value: str) -> str:
    if not isinstance(value, str):
        raise AnswerGenerationGatewayConfigurationError(
            "answer_configuration_error",
            "Answer Generation base URL is invalid",
        )
    normalized = value.strip().rstrip("/")
    try:
        parsed = urlparse(normalized)
    except ValueError:
        raise AnswerGenerationGatewayConfigurationError(
            "answer_configuration_error",
            "Answer Generation base URL is invalid",
        ) from None
    try:
        parsed_port = parsed.port
    except ValueError:
        parsed_port = None
        port_is_invalid = True
    else:
        port_is_invalid = parsed_port is not None and not 1 <= parsed_port <= 65_535
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port_is_invalid
    ):
        raise AnswerGenerationGatewayConfigurationError(
            "answer_configuration_error",
            "Answer Generation base URL is invalid",
        )
    return normalized


def _validate_api_key(value: str) -> str:
    if not _header_value_is_safe(value) or not value.strip():
        raise AnswerGenerationGatewayConfigurationError(
            "answer_configuration_error",
            "Answer Generation API key must be non-blank printable ASCII",
        )
    return value


def _header_value_is_safe(value: str) -> bool:
    """只接受 HTTPX 可安全編碼的 printable ASCII header value。"""

    return (
        isinstance(value, str)
        and bool(value)
        and all(0x20 <= ord(character) <= 0x7E for character in value)
    )


def _validate_positive(value: float, field_name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or value <= 0
    ):
        raise AnswerGenerationGatewayConfigurationError(
            "answer_configuration_error",
            f"{field_name} must be positive",
        )
    return float(value)


def _validate_positive_int(value: int, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise AnswerGenerationGatewayConfigurationError(
            "answer_configuration_error",
            f"{field_name} must be positive",
        )
    return value


def _request_payload(request: AnswerGenerationRequest) -> dict[str, Any]:
    """建立唯一合法的 Answer allowlist projection，保留 evidence 原順序與值。"""

    payload = AnswerRequestSchema(
        question=request.question,
        language=request.language,
        evidence=[
            AllowedEvidenceRequestSchema.model_validate(_allowed_evidence_payload(item))
            for item in request.evidence
        ],
        context_attestation=request.context_attestation,
    )
    return payload.model_dump(mode="json")


def _allowed_evidence_payload(item: AllowedEvidence) -> dict[str, Any]:
    """由 dataclass 欄位明確建 JSON，避免 `asdict` 把未規範欄位送往下游。"""

    # ``AnswerGenerationRequest`` 的 tuple annotation 不是 runtime type guard。
    # 在 adapter 邊界再次確認 concrete Domain entity，避免呼叫端誤把
    # ``RestrictedEvidence`` 或其他 duck-typed object 投影成可外送內容。
    if not isinstance(item, AllowedEvidence):
        raise ValueError("answer evidence must be allowed evidence")

    return {
        "rank": item.rank,
        "chunk_id": item.chunk_id,
        "document_id": item.document_id,
        "version_id": item.version_id,
        "score": item.score,
        "content": item.content,
        "document_type": item.document_type,
        "token_count": item.token_count,
        "citation": {
            "title": item.citation.title,
            "section_path": item.citation.section_path,
            "page_number": item.citation.page_number,
        },
        "governance": {
            "confidentiality": item.governance.confidentiality,
            "language": item.governance.language,
            "external_llm_allowed": item.governance.external_llm_allowed,
        },
    }


def _to_generation_result(response: AnswerResponseSchema) -> AnswerGenerationResult:
    """把已驗證 response 映射成 Chat Domain result，不暴露 transport model。"""

    parts = tuple(
        AnswerPart(text=part.text, citation_ids=tuple(part.citation_ids))
        for part in response.answer_parts
    )
    citations = tuple(
        Citation(
            citation_id=citation.citation_id,
            chunk_id=citation.chunk_id,
            document_id=citation.document_id,
            version_id=citation.version_id,
            title=citation.title,
            section_path=citation.section_path,
            page_number=citation.page_number,
        )
        for citation in response.citations
    )
    diagnostics = GenerationDiagnostics(
        provider=response.generation.provider,
        model=response.generation.model,
        prompt_version=response.generation.prompt_version,
        finish_reason=response.generation.finish_reason,
        prompt_tokens=response.generation.prompt_tokens,
        completion_tokens=response.generation.completion_tokens,
    )
    return AnswerGenerationResult(
        status=response.status,
        answer_parts=parts,
        citations=citations,
        diagnostics=diagnostics,
    )


def _safe_error_code(response: BoundedHttpResponse) -> str | None:
    """只讀取 bounded error code，任何 malformed body 都安全視為未知。"""

    if not _is_json_content_type(response):
        return None
    try:
        payload = json.loads(response.body.decode("utf-8"))
        error = AnswerErrorSchema.model_validate(payload)
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError, TypeError, ValueError):
        return None
    return error.error


def _is_json_content_type(response: BoundedHttpResponse) -> bool:
    """只接受 application/json，可帶 charset 參數但不接受相似 MIME type。"""

    media_type: str = (
        str(response.headers.get("content-type", "")).split(";", 1)[0].strip().casefold()
    )
    return media_type == "application/json"


def _retry_after_seconds(headers: httpx.Headers) -> int | None:
    """驗證 Retry-After 的整數或 HTTP-date，並限制最大等待提示。"""

    raw = headers.get("retry-after")
    if raw is None:
        return None
    raw = raw.strip()
    value: int | None
    try:
        value = int(raw)
    except ValueError:
        value = None
    if value is not None:
        if value < 0 or value > _MAX_RETRY_AFTER_SECONDS:
            return None
        return value
    try:
        parsed = parsedate_to_datetime(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        value = math.ceil((parsed - datetime.now(UTC)).total_seconds())
        if value < 0:
            return 0
        if value > _MAX_RETRY_AFTER_SECONDS:
            return None
        return value
    except (TypeError, ValueError, OverflowError, IndexError):
        return None
