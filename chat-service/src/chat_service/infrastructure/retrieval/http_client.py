"""Retrieval Service 的 async HTTP gateway adapter。

Adapter 將 Application 的 framework-free ``RetrievalRequest`` 投影成固定 allowlist JSON，並
只把通過 Pydantic 與 Domain invariants 的 response 映射回 ``RetrievalGatewayResult``。URL、
API key 與重試策略由 Infrastructure constructor 擁有；trusted ``Principal`` 只能用來建立
development identity headers。每次呼叫使用有界 body reader 與單一總 deadline，最多只對
連線/timeout/503/504 做一次重試，且不把 downstream raw body 帶入例外。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from math import isfinite
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import ValidationError

from chat_service.application.dtos import RetrievalGatewayResult, RetrievalRequest
from chat_service.application.ports import RetrievalGatewayPort
from chat_service.domain.entities import (
    AllowedEvidence,
    Citation,
    Governance,
    RestrictedEvidence,
)
from chat_service.domain.exceptions import DependencyError, DomainValidationError
from chat_service.infrastructure.http_support import (
    BoundedHttpResponse,
    ResponseBodyTooLargeError,
    UnsupportedResponseEncodingError,
    request_bounded,
)
from chat_service.infrastructure.retrieval.schemas import (
    RetrievalErrorSchema,
    RetrievalEvidenceSchema,
    RetrievalHealthSchema,
    RetrievalRequestSchema,
    RetrievalResponseSchema,
)

_RETRIEVE_PATH = "/api/v1/retrieve"
_READY_PATH = "/health/ready"
_TRANSIENT_STATUSES = frozenset({503, 504})
_MAX_RETRY_ATTEMPTS = 2  # first request + exactly one retry


class RetrievalGatewayError(DependencyError):
    """可由後續 Application/Presentation layer 分類的安全 adapter error。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int | None = None,
    ) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code, message)


class RetrievalGatewayConfigurationError(RetrievalGatewayError):
    """下游 URL/key/retry 設定不合法；不包含設定值本身。"""


class RetrievalHttpClient(RetrievalGatewayPort):
    """以共享 ``httpx.AsyncClient`` 呼叫 Retrieval，並執行完整 response contract 驗證。

    ``client`` 可省略以方便 standalone composition；正式 FastAPI lifespan 應建立並關閉
    adapter 所建立的 client，或注入由 lifespan 擁有的 client。adapter constructor 不會發出
    network request；每次 request 的 redirect protection 仍在 ``request_bounded`` 強制套用。
    """

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str,
        retrieval_limit: int = 8,
        development_identity_headers: bool = True,
        connect_timeout_seconds: float = 3.0,
        read_timeout_seconds: float = 55.0,
        total_timeout_seconds: float = 60.0,
        readiness_timeout_seconds: float = 5.0,
        max_response_body_bytes: int = 5_000_000,
        retry_backoff_seconds: float = 0.1,
        retry_max_backoff_seconds: float = 1.0,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = _validate_base_url(base_url)
        self._api_key = _validate_api_key(api_key)
        self._retrieval_limit = _validate_positive_bounded(retrieval_limit, 20, "retrieval_limit")
        self._development_identity_headers = development_identity_headers
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
        self._retry_backoff = _validate_non_negative(retry_backoff_seconds, "retry backoff")
        self._retry_max_backoff = _validate_non_negative(
            retry_max_backoff_seconds,
            "maximum retry backoff",
        )
        if self._retry_max_backoff < self._retry_backoff:
            raise RetrievalGatewayConfigurationError(
                "retrieval_configuration_error",
                "maximum retry backoff must not be smaller than retry backoff",
            )
        if self._connect_timeout > self._total_timeout:
            raise RetrievalGatewayConfigurationError(
                "retrieval_configuration_error",
                "connect timeout must not exceed total timeout",
            )
        if self._read_timeout > self._total_timeout:
            raise RetrievalGatewayConfigurationError(
                "retrieval_configuration_error",
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

    async def retrieve(self, request: RetrievalRequest) -> RetrievalGatewayResult:
        """送出一次 Retrieval，最多一次安全 transient retry。"""

        if request.limit != self._retrieval_limit:
            raise RetrievalGatewayConfigurationError(
                "retrieval_configuration_error",
                "request retrieval limit does not match configured limit",
            )
        try:
            body = RetrievalRequestSchema(
                query=request.query,
                limit=self._retrieval_limit,
                language=request.language,
            ).model_dump(mode="json")
        except ValidationError:
            raise RetrievalGatewayError(
                "retrieval_contract_error",
                "retrieval request violates the configured contract",
            ) from None

        headers = self._request_headers(request)
        try:
            async with asyncio.timeout(self._total_timeout):
                response = await self._request_with_retry(body, headers)
        except TimeoutError:
            raise RetrievalGatewayError(
                "retrieval_timeout",
                "Retrieval Service timed out",
            ) from None
        except ResponseBodyTooLargeError:
            raise RetrievalGatewayError(
                "retrieval_contract_error",
                "Retrieval Service response exceeded the size limit",
            ) from None
        except UnsupportedResponseEncodingError:
            raise RetrievalGatewayError(
                "retrieval_contract_error",
                "Retrieval Service returned an unsupported content encoding",
            ) from None
        except httpx.DecodingError:
            raise RetrievalGatewayError(
                "retrieval_contract_error",
                "Retrieval Service returned an undecodable response",
            ) from None

        if response.status_code != 200:
            raise self._map_error(response)
        payload = self._parse_json_object(response)
        try:
            parsed = RetrievalResponseSchema.model_validate(payload)
            if parsed.retrieval.selected_count > self._retrieval_limit:
                raise ValueError("selected evidence exceeds the configured retrieval limit")
            return _to_gateway_result(parsed)
        except (ValidationError, DomainValidationError, TypeError, ValueError, KeyError):
            raise RetrievalGatewayError(
                "retrieval_contract_error",
                "Retrieval Service response violates the contract",
            ) from None

    async def is_ready(self) -> bool:
        """以短 timeout 做 read-only readiness probe，不呼叫 retrieve。"""

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
            parsed = RetrievalHealthSchema.model_validate(self._parse_json_object(response))
            return parsed.status == "ok" and bool(parsed.checks) and all(parsed.checks.values())
        except (
            httpx.HTTPError,
            TimeoutError,
            ResponseBodyTooLargeError,
            UnsupportedResponseEncodingError,
            RetrievalGatewayError,
            ValidationError,
            ValueError,
        ):
            return False

    async def close(self) -> None:
        """若 client 由 adapter 建立，於 lifespan shutdown 釋放 connection pool。"""

        if self._owns_client:
            await self._client.aclose()

    def _request_headers(self, request: RetrievalRequest) -> dict[str, str]:
        """只從 trusted Principal 建立 development identity，絕不接受 body identity。"""

        if not _header_value_is_safe(request.request_id):
            raise RetrievalGatewayError(
                "retrieval_contract_error",
                "Retrieval request ID contains an invalid header character",
            )
        headers = {
            "accept": "application/json",
            "content-type": "application/json",
            "x-internal-api-key": self._api_key,
            "x-request-id": request.request_id,
        }
        if self._development_identity_headers:
            if not _header_value_is_safe(request.principal.subject) or any(
                not _header_value_is_safe(group_id) for group_id in request.principal.group_ids
            ):
                raise RetrievalGatewayError(
                    "retrieval_contract_error",
                    "Retrieval identity contains an invalid header character",
                )
            headers["x-dev-user-id"] = request.principal.subject
            if request.principal.group_ids:
                headers["x-dev-group-ids"] = ",".join(request.principal.group_ids)
        return headers

    async def _request_with_retry(
        self,
        body: Mapping[str, object],
        headers: Mapping[str, str],
    ) -> BoundedHttpResponse:
        """只對 connect/read timeout、transport failure、合法 503/504 envelope retry 一次。"""

        for attempt in range(_MAX_RETRY_ATTEMPTS):
            try:
                response = await request_bounded(
                    self._client,
                    "POST",
                    f"{self._base_url}{_RETRIEVE_PATH}",
                    headers=headers,
                    json_body=body,
                    request_timeout=self._timeout,
                    max_response_body_bytes=self._max_response_bytes,
                )
            except (httpx.ConnectTimeout, httpx.ReadTimeout):
                if attempt == 0:
                    await self._backoff(attempt)
                    continue
                raise RetrievalGatewayError(
                    "retrieval_timeout",
                    "Retrieval Service timed out",
                ) from None
            except httpx.ConnectError:
                if attempt == 0:
                    await self._backoff(attempt)
                    continue
                raise RetrievalGatewayError(
                    "retrieval_unavailable",
                    "Retrieval Service is unavailable",
                ) from None
            except httpx.TimeoutException:
                raise RetrievalGatewayError(
                    "retrieval_timeout",
                    "Retrieval Service timed out",
                ) from None
            except httpx.TransportError:
                raise RetrievalGatewayError(
                    "retrieval_unavailable",
                    "Retrieval Service is unavailable",
                ) from None

            if (
                response.status_code in _TRANSIENT_STATUSES
                and attempt == 0
                and _safe_error_code(response) is not None
            ):
                await self._backoff(attempt)
                continue
            return response

        raise RetrievalGatewayError(
            "retrieval_unavailable",
            "Retrieval Service is unavailable",
        )

    async def _backoff(self, attempt: int) -> None:
        """在外層 total deadline 內執行 capped backoff。"""

        delay = min(self._retry_max_backoff, self._retry_backoff * (2**attempt))
        if delay > 0:
            await asyncio.sleep(delay)

    def _parse_json_object(self, response: BoundedHttpResponse) -> dict[str, Any]:
        """只解析 application/json object；錯誤只回固定 contract 訊息。"""

        if not _is_json_content_type(response):
            raise RetrievalGatewayError(
                "retrieval_contract_error",
                "Retrieval Service returned an unexpected content type",
            )
        try:
            parsed: Any = json.loads(
                response.body.decode("utf-8"),
                parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
            )
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            raise RetrievalGatewayError(
                "retrieval_contract_error",
                "Retrieval Service returned invalid JSON",
            ) from None
        if not isinstance(parsed, dict):
            raise RetrievalGatewayError(
                "retrieval_contract_error",
                "Retrieval Service returned a non-object JSON value",
            )
        return parsed

    def _map_error(self, response: BoundedHttpResponse) -> RetrievalGatewayError:
        """依規格 status/error code 分類，永不包含 downstream raw body。"""

        status = response.status_code
        downstream_code = _safe_error_code(response)
        if status == 401:
            if downstream_code == "unauthorized_principal":
                return RetrievalGatewayError(
                    "retrieval_identity_rejected",
                    "Retrieval Service rejected the verified principal",
                    status_code=status,
                )
            return RetrievalGatewayError(
                "retrieval_auth_misconfigured",
                "Retrieval Service authentication is misconfigured",
                status_code=status,
            )
        if status in {400, 413, 422, 502}:
            return RetrievalGatewayError(
                "retrieval_contract_error",
                "Retrieval Service rejected the retrieval contract",
                status_code=status,
            )
        if status == 503:
            return RetrievalGatewayError(
                "retrieval_unavailable",
                "Retrieval Service is unavailable",
                status_code=status,
            )
        if status == 504:
            return RetrievalGatewayError(
                "retrieval_timeout",
                "Retrieval Service timed out",
                status_code=status,
            )
        return RetrievalGatewayError(
            "retrieval_service_error",
            "Retrieval Service returned an unexpected error",
            status_code=status,
        )


def _validate_base_url(value: str) -> str:
    """只允許固定 HTTP(S) URL，並拒絕 credentials/query/fragment。"""

    if not isinstance(value, str):
        raise RetrievalGatewayConfigurationError(
            "retrieval_configuration_error",
            "Retrieval base URL is invalid",
        )
    normalized = value.strip().rstrip("/")
    try:
        parsed = urlparse(normalized)
    except ValueError:
        raise RetrievalGatewayConfigurationError(
            "retrieval_configuration_error",
            "Retrieval base URL is invalid",
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
        raise RetrievalGatewayConfigurationError(
            "retrieval_configuration_error",
            "Retrieval base URL is invalid",
        )
    return normalized


def _validate_api_key(value: str) -> str:
    if not _header_value_is_safe(value) or not value.strip():
        raise RetrievalGatewayConfigurationError(
            "retrieval_configuration_error",
            "Retrieval API key must be non-blank printable ASCII",
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
        or not isfinite(float(value))
        or value <= 0
    ):
        raise RetrievalGatewayConfigurationError(
            "retrieval_configuration_error",
            f"{field_name} must be positive",
        )
    return float(value)


def _validate_non_negative(value: float, field_name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not isfinite(float(value))
        or value < 0
    ):
        raise RetrievalGatewayConfigurationError(
            "retrieval_configuration_error",
            f"{field_name} must be non-negative",
        )
    return float(value)


def _validate_positive_int(value: int, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RetrievalGatewayConfigurationError(
            "retrieval_configuration_error",
            f"{field_name} must be positive",
        )
    return value


def _validate_positive_bounded(value: int, maximum: int, field_name: str) -> int:
    result = _validate_positive_int(value, field_name)
    if result > maximum:
        raise RetrievalGatewayConfigurationError(
            "retrieval_configuration_error",
            f"{field_name} exceeds the configured maximum",
        )
    return result


def _safe_error_code(response: BoundedHttpResponse) -> str | None:
    """從 error envelope 只取 bounded machine code；任何 parse failure 都回 None。"""

    if not _is_json_content_type(response):
        return None
    try:
        payload = json.loads(response.body.decode("utf-8"))
        error = RetrievalErrorSchema.model_validate(payload)
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError, TypeError):
        return None
    return error.error


def _is_json_content_type(response: BoundedHttpResponse) -> bool:
    """只接受 application/json，可帶 charset 參數但不接受相似 MIME type。"""

    media_type: str = (
        str(response.headers.get("content-type", "")).split(";", 1)[0].strip().casefold()
    )
    return media_type == "application/json"


def _to_gateway_result(response: RetrievalResponseSchema) -> RetrievalGatewayResult:
    """把 strict transport model 映射為 Chat core；restricted 只保留安全 projection。"""

    allowed = tuple(_allowed_evidence(item) for item in response.llm_context)
    restricted = tuple(_restricted_evidence(item) for item in response.restricted_evidence)
    return RetrievalGatewayResult(
        status=response.status,
        llm_context=allowed,
        restricted_evidence=restricted,
        external_generation_blocked=response.external_generation_blocked,
        context_attestation=response.context_attestation,
        candidate_count=response.retrieval.candidate_count,
        selected_count=response.retrieval.selected_count,
        model_id=response.retrieval.model_id,
        model_revision=response.retrieval.model_revision,
    )


def _citation(item: RetrievalEvidenceSchema) -> Citation:
    return Citation(
        citation_id=f"R{item.rank}",
        chunk_id=item.chunk_id,
        document_id=item.document_id,
        version_id=item.version_id,
        title=item.citation.title,
        section_path=item.citation.section_path,
        page_number=item.citation.page_number,
    )


def _allowed_evidence(item: RetrievalEvidenceSchema) -> AllowedEvidence:
    return AllowedEvidence(
        rank=item.rank,
        chunk_id=item.chunk_id,
        document_id=item.document_id,
        version_id=item.version_id,
        score=item.score,
        content=item.content,
        document_type=item.document_type,
        token_count=item.token_count,
        citation=_citation(item),
        governance=Governance(
            confidentiality=item.governance.confidentiality,
            language=item.governance.language,
            external_llm_allowed=True,
        ),
    )


def _restricted_evidence(item: RetrievalEvidenceSchema) -> RestrictedEvidence:
    return RestrictedEvidence(
        rank=item.rank,
        chunk_id=item.chunk_id,
        document_id=item.document_id,
        version_id=item.version_id,
        citation=_citation(item),
    )
