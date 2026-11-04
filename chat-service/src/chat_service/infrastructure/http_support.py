"""Chat Service HTTP adapter 共用的安全 transport primitive。

這個 module 只處理 Infrastructure 層的網路細節：每次 request 都明確關閉 redirect，並
逐段讀取 response body。`Content-Length` 只能當快速拒絕提示，真正的上限仍由讀取後的
byte 數量決定；同時拒絕壓縮 response，避免 HTTPX 在大小檢查前先配置巨大的 decoded
chunk。Domain/Application 不會 import 這個 helper；它只被 Retrieval 與 Answer 的 HTTP
adapters 使用。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import httpx


class ResponseBodyTooLargeError(Exception):
    """下游 response 超過 adapter 設定的解壓後 byte 上限。"""


class UnsupportedResponseEncodingError(Exception):
    """拒絕可能在 byte limit 前觸發大量解壓縮的 response content encoding。"""


@dataclass(frozen=True, slots=True)
class BoundedHttpResponse:
    """只保留 status、headers 與已通過大小檢查的 bytes。"""

    status_code: int
    headers: httpx.Headers
    body: bytes


async def request_bounded(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: Mapping[str, str],
    json_body: object | None,
    request_timeout: httpx.Timeout | float,
    max_response_body_bytes: int,
) -> BoundedHttpResponse:
    """執行一次不跟隨 redirect 且有 bounded response body 的 HTTP request。

    函式強制使用 ``Accept-Encoding: identity``，並在讀取前拒絕非 identity 的
    ``Content-Encoding``；因此 ``aiter_bytes()`` 不會在 byte limit 前執行未知的解壓縮。
    函式刻意不解析錯誤 body，呼叫端可依 HTTP status 分類而不把下游可能含敏感資料的
    raw body 帶進 exception 或 log。
    """

    if max_response_body_bytes <= 0:
        raise ValueError("max_response_body_bytes must be positive")

    request_headers = {
        key: value for key, value in headers.items() if key.casefold() != "accept-encoding"
    }
    # 兩個下游都是內部 JSON API；禁止壓縮可避免 HTTPX 在 byte limit
    # 檢查前先產生一個可能遠大於 wire body 的 decoded chunk。
    request_headers["accept-encoding"] = "identity"

    async with client.stream(
        method,
        url,
        headers=request_headers,
        json=json_body,
        timeout=request_timeout,
        follow_redirects=False,
    ) as response:
        content_encoding = response.headers.get("content-encoding")
        if content_encoding is not None:
            encodings = [part.strip().casefold() for part in content_encoding.split(",")]
            if not encodings or any(encoding != "identity" for encoding in encodings):
                raise UnsupportedResponseEncodingError

        content_length = response.headers.get("content-length")
        if content_length is not None:
            try:
                content_length_exceeds_limit = int(content_length) > max_response_body_bytes
            except ValueError:
                # 不可信的 header 不直接拒絕；後面的 streamed byte count 仍是權威檢查。
                content_length_exceeds_limit = False
            if content_length_exceeds_limit:
                raise ResponseBodyTooLargeError

        body = bytearray()
        async for chunk in response.aiter_bytes():
            if len(body) + len(chunk) > max_response_body_bytes:
                raise ResponseBodyTooLargeError
            body.extend(chunk)

        return BoundedHttpResponse(
            status_code=response.status_code,
            headers=response.headers,
            body=bytes(body),
        )
