"""Web Chat container smoke：只驗證頁面與 health，不建立 conversation 或呼叫 LLM。"""

from __future__ import annotations

import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def get_json(url: str) -> tuple[int, dict[str, object]]:
    """以標準庫檢查固定 localhost endpoint，避免 smoke 引入額外 runtime dependency。"""

    request = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=5) as response:
            body = response.read(100_000)
            payload = json.loads(body.decode("utf-8"))
            if not isinstance(payload, dict):
                raise RuntimeError("response is not an object")
            return response.status, payload
    except HTTPError as exc:
        if exc.code != 503:
            raise RuntimeError("web-chat endpoint unavailable") from exc
        try:
            payload = json.loads(exc.read(100_000).decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as parse_error:
            raise RuntimeError("web-chat endpoint unavailable") from parse_error
        if not isinstance(payload, dict):
            raise RuntimeError("web-chat endpoint unavailable") from None
        return exc.code, payload
    except (URLError, TimeoutError, ValueError) as exc:
        raise RuntimeError("web-chat endpoint unavailable") from exc


def main() -> int:
    """只做 read-only liveness/readiness 與頁面檢查。"""

    base_url = os.environ.get("WEB_CHAT_SMOKE_BASE_URL", "http://127.0.0.1:8130").rstrip("/")
    try:
        page_request = Request(f"{base_url}/", headers={"Accept": "text/html"})
        with urlopen(page_request, timeout=5) as response:
            page = response.read(100_000).decode("utf-8")
            if response.status != 200 or "Help Desk Demo" not in page:
                raise RuntimeError("page contract failed")
        live_status, live = get_json(f"{base_url}/health/live")
        if live_status != 200 or live.get("status") != "ok":
            raise RuntimeError("liveness contract failed")
        ready_status, ready = get_json(f"{base_url}/health/ready")
        if ready_status != 200 or ready.get("status") != "ok":
            raise RuntimeError("readiness contract failed")
    except (HTTPError, URLError, TimeoutError, UnicodeDecodeError, RuntimeError):
        print("WEB CHAT SMOKE FAILED", file=sys.stderr)
        return 1
    print("WEB CHAT SMOKE PASSED: page/live/readiness only")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
