# Web Chat Demo Progress

## Scope

本專案只提供 localhost、無登入的展示介面；不修改其他服務，不新增 DB，不提供正式身份驗證。

## Tasks

| Task | Status | Notes |
| --- | --- | --- |
| 1. 專案基線、設定、健康檢查 | completed | FastAPI scaffold、strict settings、live/ready |
| 2. Chat HTTP gateway 與安全契約 | completed | conversation、turn submit、turn polling；server-side dev headers |
| 3. 單頁 UI 與 idempotency/polling state | completed | 原生 JS；無 storage、無 history restore |
| 4. Docker、測試與文件 | in_progress | Code/tests 完成；Docker daemon/build 與 full-stack smoke 待 host 驗證 |

## Evidence

本次執行證據：`uv sync --frozen --extra dev` exit 0；`uv lock --check` exit 0；Ruff check
與 format check exit 0；mypy exit 0；pytest exit 0（21 passed、0 failed、0 skipped、0 errors，
branch coverage 85%）；`make test-ui` exit 0（1 passed、20 deselected；UI contract 為靜態安全檢查）。
`docker compose config --quiet` exit 0。Docker image build 因目前執行環境無法寫入 Docker Desktop
buildx activity 檔案而 exit 1，尚未取得 container／跨架構／full-stack smoke 證據；不得把它標為 passed。
`make smoke` exit 1，原因是本環境沒有可用的 Chat Service readiness（Web 的 live 與頁面曾以本機
uvicorn 手動確認 200，ready 正確回 503）。
真實 Chat/Retrieval/Answer 串接尚未由本環境驗證，請在 Docker 可用的 host 依 README 命令執行。
