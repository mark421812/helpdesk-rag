# Chat Service

Chat Service 是 Help Desk RAG 的對話入口與 orchestration owner。它保存 conversation、turn、
user/assistant message 與安全 citation 歷史，接收可信的 development identity，依序呼叫
Retrieval Service 與 Answer Generation Service。Phase 7 補上本機 Docker、只讀 preflight、
非生成 smoke 與明確 opt-in 的完整 E2E；這仍不是 production-ready 版本。

## 責任與資料邊界

```text
Client（已有無登入 Web Chat Demo，8130）
  -> Chat API (8120)
      -> Retrieval Service (8100)
          -> Knowledge Index Service (8080)
      -> Answer Generation Service (8110)
          -> Fake Provider（本機預設）或核准的外部 provider
```

- Chat 的 PostgreSQL migration 與資料表只在 `chat` schema；Chat runtime 不直接連 KIS 的
  PostgreSQL、Qdrant 或文件資料夾。
- Retrieval 回傳的 `llm_context` 才能送往 Answer；`restricted_evidence` 永遠不會送出。
- Public response 只投影文件名稱、章節、頁碼與安全 citation，不回傳 path、URL、attestation、
  下游 raw error 或 API key。
- Chat history 永久保存，管理者以 preview-first 的 exact selector 執行 purge；smoke/E2E 只清理
  自己建立且記錄的 synthetic ID。
- 開發 identity 使用 `x-dev-user-id`、`x-dev-group-ids`、`x-dev-roles`；production 仍會在
  Entra JWT adapter 未實作時 fail closed，不能把 development headers 當正式驗證。

## Query Processor 與問答流程

已實作的 [`QueryProcessor`](./src/chat_service/application/query_processor.py) 是 Application
元件，不是獨立服務：驗證問題型別、非空與長度，統一 CRLF／LF、Unicode NFC、去除頭尾
空白；語言只 trim，不自行猜測。相同的 processed query 送往 Retrieval 與 Answer。

它沒有 LLM 改寫、意圖分類、子問題拆解或歷史指代補全。保存永久 conversation history，
不等於把歷史放進模型 Prompt；目前每題仍獨立檢索。

一次問答的流程：

1. 驗證 ownership、處理 query 與冪等鍵；相同 key／payload 可重播，不重複呼叫下游。
2. 短交易保存 accepted turn 與 user message，先 commit。
3. 等待 capacity，呼叫 Retrieval；等待與 HTTP 期間沒有持續開啟的 DB transaction。
4. 無證據或全部 restricted 時直接回業務結果；有 allowed evidence 才呼叫 Answer。
5. 新交易保存 terminal 結果，釋放 capacity。失敗也需可重播；Answer POST 不自動 retry。

完整 RAG 與六個專案分工請看 [`系統總 README`](../README.md)。

## Docker 與架構

`Dockerfile` 使用固定 digest 的 multi-architecture uv/Python base，依序 frozen install
`pyproject.toml`/`uv.lock`，最後以 non-root `app` 使用者啟動 Uvicorn。沒有寫死 `platform`、
GPU、Torch 或外部 LLM runtime；Intel 主機是 `linux/amd64`，Oracle Cloud ARM 主機是
`linux/arm64`。

`compose.yaml` 只有兩個服務：

- `migrate` 擁有唯一的 `build:`，執行 `alembic upgrade head` 後正常結束。
- `chat-api` 重用 `helpdesk/chat-service:local`，並以
  `service_completed_successfully` 等待 migration。兩者都使用 `init`、read-only root
  filesystem、`tmpfs /tmp`、`cap_drop: ALL` 與 `no-new-privileges`。

Compose 不建立 PostgreSQL 或 Qdrant；它加入已存在的 external `helpdesk-rag` network，並將
Chat API 只綁到 host `127.0.0.1:8120`。KIS Compose 提供 PostgreSQL/Qdrant，Retrieval 與
Answer Compose 提供其 HTTP aliases。

## 啟動順序

先在各 sibling project 建立共同 network 並依序啟動：

1. Knowledge Index Service 的 PostgreSQL、Qdrant、migration、API、worker 全部 healthy/ready。
2. Retrieval Service 使用同一個 KIS internal key，`/health/ready` 成功。
3. Answer Generation 使用 Fake Provider，載入與 Retrieval 配對的 public attestation key，
   `/health/ready` 成功；一般 smoke 不會啟用 NVIDIA live provider。`make preflight` 與
   `make smoke-e2e` 會透過 Docker metadata 確認唯一、正在執行且掛在 external network 的
   `answer-generation-api` 明確設定 `LLM_PROVIDER=fake` 與
   `LLM_PROVIDER_USAGE_MODE=test`；任何 Docker 錯誤、歧義或非 Fake 設定都會 fail closed。
4. 回到本目錄執行 Chat migration/API。

Ingestion Service 不是 Chat runtime 或 smoke 的前置條件。
上述 preflight／E2E 是 Fake-only 的安全驗收路徑。實際 Demo 可由 Answer Service 改用
Groq／NVIDIA，但這不會讓 Fake-only gate 接受 hosted provider；真實 Provider 的測試請依
[`Answer README`](../answer-generation-service/README.md#local-docker)。Chat 本身不保存 Provider key。

```bash
cd /Users/markhuang/Documents/Codex/2026-07-23/new-chat/chat-service
cp -n .env.example .env
make network
make preflight                 # 只讀檢查 network/PG/KIS/Retrieval/Answer/Fake Provider
docker compose up -d --build
make smoke                     # 不呼叫 Retrieval/Answer，不消耗 LLM
```

停止與診斷：

```bash
make ps
make logs
make down
```

`make down` 只停止 Chat Compose 服務，不刪除共用 PostgreSQL、Qdrant 或其他服務的 volume。

## API 摘要

- `GET /health/live`：process liveness。
- `GET /health/ready`：PostgreSQL、Retrieval、Answer 與 identity readiness；dependency 不可用
  時回 `503`，不以弱化檢查掩蓋故障。
- `POST/GET/PATCH /api/v1/conversations...`：建立、列出、讀取與封存自己的 conversation。
- `POST /api/v1/conversations/{conversation_id}/turns`：需要 UUID `Idempotency-Key`，回傳
  answered/failed terminal 或 `202 processing` polling handle。
- `GET .../turns/{turn_id}`、`GET .../messages`：查詢自己的永久歷史。
- `POST /api/v1/admin/conversation-purge-previews`、`POST /api/v1/admin/conversation-purges`：
  `helpdesk-admin` 專用 preview/execute hard purge。
- `GET /metrics`：固定低 cardinality Prometheus metrics，不包含 user、query、內容或 resource ID。

完整 schema、錯誤 mapping、ownership、idempotency、capacity 與 restricted evidence 契約請看
workspace 的 [`docs/chat-service-spec.md`](../docs/chat-service-spec.md)。

## Make targets

```text
lock lock-check install lint format typecheck test test-unit test-contract test-security
test-integration config build-native build-amd64 build-arm64 network preflight migrate smoke
smoke-e2e up down logs ps
```

除 `lock` 外，所有 uv sync/run targets 都使用 `--frozen`。常用檢查：

```bash
make install
make lock-check
make lint
make typecheck
make test-unit
make test-contract
make test-security
make test-integration       # Testcontainers PostgreSQL 16，不能以 SQLite 替代
make config
make build-native           # 依目前主機架構
make build-amd64            # Intel/CI 明確檢查
make build-arm64            # Intel 上可能需要 QEMU，Oracle ARM 上原生執行
```

## Smoke 與 E2E

`make smoke` 只做 Docker status、live/ready、missing identity、conversation CRUD、archive、
archived turn rejection 與 admin exact-ID purge；它不呼叫 Retrieval、Answer 或 LLM。

`make smoke-e2e` 是 host-only script，驗證真實
`Client -> Chat -> Retrieval -> Knowledge Index -> Answer/Fake Provider`、safe citation、永久
歷史與同一 `Idempotency-Key` replay。為避免誤刪資料，E2E 預設拒絕所有 KIS writes：

```bash
cp -n .env.e2e.example .env.e2e
# 僅在本機測試、確認 key 是 KIS API_INTERNAL_KEY 後，編輯 .env.e2e：
# CHAT_E2E_ALLOW_KIS_WRITES=1
# CHAT_E2E_KIS_API_KEY=<key>       # 不要放在命令列、Git 或 log
make preflight
make smoke-e2e
```

Script 在任何 fixture write 前使用該 key 呼叫 KIS `GET /api/v1/auth/check`，並取得 host-local
exclusive lock；lock 會持有到 exact conversation purge 與 exact KIS document soft-delete job
完成。固定 fixture identity 為 `source_type=chat_e2e`、
`source_item_id=chat-service/allowed-grounded-v1`，ACL 只允許 `all-employees`，內容是專用驗證碼，
不含公司文件。未設定 opt-in 時，安全失敗且不會建立、修改或刪除任何 KIS 資料。

E2E script 絕不把 `.env.e2e` 注入 `chat-api`/`migrate`，也不把 key、attestation、path、URL、
raw response body 或 restricted content 寫入輸出。Chat runtime 本身永遠不呼叫 KIS。

`make preflight` 在 E2E opt-in 開啟時，會先以 `.env.e2e` 的 host-only KIS service key 呼叫
`GET /api/v1/auth/check`；只有 HTTP 200 且 JSON `status=ok` 才會通過，401、redirect、錯誤
content type、無效 JSON 或超過 body limit 都會安全失敗。E2E opt-in 關閉時不會要求 key，
也不會發出這個 auth request。兩個腳本都不會輸出 key、Docker environment、raw error body
或其他敏感 metadata。

## 目前限制與部署提醒

- [`Web Chat Demo`](../web-chat/README.md) 已完成基本無登入畫面；Entra JWT、正式員工登入、
  SharePoint connector、OCR、管理後台與 production secret delivery 尚未完成。
- Groq／NVIDIA Provider 由 Answer Service 設定與管理；Trial 僅使用明確核准的測試資料。
  Fake Provider 是本機預設，CPU-only Oracle
  ARM 主機不需要 GPU。未做正式 30 人併發壓測或 production autoscaling。
- `make build-native` 不會重新解析套件；Docker dependency layer 由 frozen lock 控制。修改
  `pyproject.toml` 後必須明確執行 `make lock` 並審查 lock 變更。
- 正式部署前仍需替換 development keys、接入 Entra、設定 secrets、網路 ingress、TLS、backup、
  load test、監控告警與資料保留政策。

依 [`progress`](../docs/chat-service-progress.md) 的 2026-09-06 handoff，Chat Phase 1–7
工程範圍已 completed、Phase 7 independent review passed。這不等於正式部署或所有 RAG
問題的回答品質已通過驗收；README 整理也不會重設既有 phase gate。
