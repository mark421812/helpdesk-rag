"""把 Ingestion DTO 映射成 Knowledge Index HTTP/JSON contract 的 async adapter。"""

from __future__ import annotations

import json as json_module
from datetime import date
from typing import Any
from uuid import UUID

import httpx

from ingestion_service.application.dtos import (
    KnowledgeIndexDocumentSubmission,
    KnowledgeIndexJobResult,
    KnowledgeIndexMetadataPatch,
    KnowledgeIndexSubmissionResult,
)
from ingestion_service.domain.entities import AccessControlEntry
from ingestion_service.domain.enums import KnowledgeIndexingStatus, KnowledgeIndexJobStatus
from ingestion_service.domain.exceptions import ExternalServiceError, ValidationError


class KnowledgeIndexHttpClient:
    """KnowledgeIndexClient port 的 httpx 非同步 adapter。

    Application 傳入 dataclass DTO；此處負責轉成 KIS 的 JSON schema、HTTP status 與錯誤
    語意，再把 response 轉回 DTO。如此 use case 不依賴 URL、header 或 httpx。
    """

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        timeout_seconds: float = 120,
        readiness_timeout_seconds: float = 5,
        max_request_body_bytes: int = 25_000_000,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if readiness_timeout_seconds <= 0:
            raise ValueError("readiness_timeout_seconds must be greater than zero")
        if max_request_body_bytes < 1:
            raise ValueError("max_request_body_bytes must be at least one")
        headers = {"accept": "application/json"}
        # 本機可不設 key；有設定時才附上內部服務驗證 header。
        if api_key:
            headers["x-internal-api-key"] = api_key
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=timeout_seconds,
            transport=transport,
        )
        self._readiness_timeout_seconds = readiness_timeout_seconds
        self._max_request_body_bytes = max_request_body_bytes

    async def submit_document(
        self,
        submission: KnowledgeIndexDocumentSubmission,
    ) -> KnowledgeIndexSubmissionResult:
        """將文件、metadata、ACL 與 chunks 映射為 KIS document submission。"""

        response = await self._request(
            "POST",
            "/api/v1/documents",
            json={
                "source_type": submission.source_type,
                "source_item_id": submission.source_item_id,
                "source_version": submission.source_version,
                "title": submission.title,
                "document_type": submission.document_type,
                "owner_department": submission.owner_department,
                "document_owner": submission.document_owner,
                "confidentiality": submission.confidentiality.value,
                "status": submission.status.value,
                "language": submission.language,
                "external_llm_allowed": submission.external_llm_allowed,
                "version_number": submission.version_number,
                "content_hash": submission.content_hash,
                "mime_type": submission.mime_type,
                "file_size": submission.file_size,
                "parser_name": submission.parser_name,
                "parser_version": submission.parser_version,
                "source_path": submission.source_path,
                "source_url": submission.source_url,
                "effective_at": _date_value(submission.effective_at),
                "expires_at": _date_value(submission.expires_at),
                "review_at": _date_value(submission.review_at),
                "custom_metadata": submission.custom_metadata,
                # list comprehension 將每個 Domain ACL value object 映射成 JSON object。
                "acl": [_acl_payload(entry) for entry in submission.acl],
                "chunks": [
                    {
                        "content": chunk.content,
                        "section_path": chunk.section_path,
                        "page_number": chunk.page_number,
                        "token_count": chunk.token_count,
                    }
                    for chunk in submission.chunks
                ],
            },
            expected_status=202,
        )
        return KnowledgeIndexSubmissionResult(
            document_id=_uuid(response, "document_id"),
            version_id=_uuid(response, "version_id"),
            job_id=_optional_uuid(response, "job_id"),
            indexing_status=KnowledgeIndexingStatus(_string(response, "indexing_status")),
            created=_boolean(response, "created"),
        )

    async def patch_metadata(
        self,
        document_id: UUID,
        patch: KnowledgeIndexMetadataPatch,
    ) -> KnowledgeIndexJobResult:
        response = await self._request(
            "PATCH",
            f"/api/v1/documents/{document_id}/metadata",
            json={
                "title": patch.title,
                "document_type": patch.document_type,
                "owner_department": patch.owner_department,
                "document_owner": patch.document_owner,
                "confidentiality": patch.confidentiality.value,
                "status": patch.status.value,
                "language": patch.language,
                "external_llm_allowed": patch.external_llm_allowed,
                "source_path": patch.source_path,
                "source_url": patch.source_url,
                "version_number": patch.version_number,
                "source_version": patch.source_version,
                "effective_at": _date_value(patch.effective_at),
                "expires_at": _date_value(patch.expires_at),
                "review_at": _date_value(patch.review_at),
                "custom_metadata": patch.custom_metadata,
                "acl": [_acl_payload(entry) for entry in patch.acl],
            },
            expected_status=202,
        )
        return _job_result(response)

    async def delete_document(self, document_id: UUID) -> KnowledgeIndexJobResult:
        response = await self._request(
            "DELETE",
            f"/api/v1/documents/{document_id}",
            expected_status=202,
        )
        return _job_result(response)

    async def get_job(self, job_id: UUID) -> KnowledgeIndexJobResult:
        response = await self._request(
            "GET",
            f"/api/v1/jobs/{job_id}",
            expected_status=200,
        )
        return _job_result(response)

    async def is_ready(self) -> bool:
        """同時檢查 KIS readiness 與目前 API key 是否通過 auth check。"""

        try:
            health_response = await self._client.get(
                "/health/ready",
                timeout=self._readiness_timeout_seconds,
            )
            if health_response.status_code != 200:
                return False
            auth_response = await self._client.get(
                "/api/v1/auth/check",
                timeout=self._readiness_timeout_seconds,
            )
        except httpx.HTTPError:
            return False
        if auth_response.status_code != 200:
            return False
        try:
            payload = auth_response.json()
        except ValueError:
            return False
        return isinstance(payload, dict) and payload.get("status") == "ok"

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        expected_status: int,
    ) -> dict[str, Any]:
        """集中處理 JSON 編碼、大小限制、HTTP 錯誤與 response contract 驗證。"""

        content: bytes | None = None
        headers: dict[str, str] | None = None
        if json is not None:
            try:
                content = json_module.dumps(
                    json,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            except (TypeError, ValueError) as exc:
                raise ValidationError("Knowledge Index request is not valid JSON") from exc
            if len(content) > self._max_request_body_bytes:
                raise ValidationError(
                    "Knowledge Index request body has "
                    f"{len(content)} bytes; limit is {self._max_request_body_bytes}"
                )
            headers = {"content-type": "application/json"}
        try:
            response = await self._client.request(
                method,
                path,
                content=content,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise ExternalServiceError(
                f"Knowledge Index request failed: {type(exc).__name__}",
                retryable=True,
            ) from exc
        if response.status_code != expected_status:
            # 暫時性 client status 與 server errors 可重試；其他 4xx 通常是永久 contract 錯誤。
            retryable = response.status_code in {408, 409, 425, 429} or response.status_code >= 500
            message = _error_message(response)
            raise ExternalServiceError(
                f"Knowledge Index returned HTTP {response.status_code}: {message}",
                retryable=retryable,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ExternalServiceError(
                "Knowledge Index returned invalid JSON",
                retryable=True,
            ) from exc
        if not isinstance(payload, dict):
            raise ExternalServiceError(
                "Knowledge Index response must be a JSON object",
                retryable=True,
            )
        return payload


def _job_result(payload: dict[str, Any]) -> KnowledgeIndexJobResult:
    return KnowledgeIndexJobResult(
        job_id=_uuid(payload, "job_id"),
        document_id=_uuid(payload, "document_id"),
        version_id=_optional_uuid(payload, "version_id"),
        status=KnowledgeIndexJobStatus(_string(payload, "status")),
        error_code=_optional_string(payload, "error_code"),
        error_message=_optional_string(payload, "error_message"),
    )


def _acl_payload(entry: AccessControlEntry) -> dict[str, str]:
    return {
        "principal_type": entry.principal_type.value,
        "principal_id": entry.principal_id,
        "effect": entry.effect.value,
    }


def _date_value(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _uuid(payload: dict[str, Any], field: str) -> UUID:
    try:
        return UUID(_string(payload, field))
    except ValueError as exc:
        raise ExternalServiceError(
            f"Knowledge Index response has invalid {field}",
            retryable=True,
        ) from exc


def _optional_uuid(payload: dict[str, Any], field: str) -> UUID | None:
    value = payload.get(field)
    if value is None:
        return None
    try:
        return UUID(str(value))
    except ValueError as exc:
        raise ExternalServiceError(
            f"Knowledge Index response has invalid {field}",
            retryable=True,
        ) from exc


def _string(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise ExternalServiceError(
            f"Knowledge Index response is missing {field}",
            retryable=True,
        )
    return value


def _optional_string(payload: dict[str, Any], field: str) -> str | None:
    value = payload.get(field)
    return value if isinstance(value, str) else None


def _boolean(payload: dict[str, Any], field: str) -> bool:
    value = payload.get(field)
    if not isinstance(value, bool):
        raise ExternalServiceError(
            f"Knowledge Index response is missing {field}",
            retryable=True,
        )
    return value


def _error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:500] or "no response body"
    if isinstance(payload, dict):
        message = payload.get("message") or payload.get("detail")
        if isinstance(message, str):
            return message[:500]
    return str(payload)[:500]
