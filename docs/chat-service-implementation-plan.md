# Chat Service v1 分階段施工計畫

> 本計畫將 [`chat-service-spec.md`](./chat-service-spec.md) 拆成低階 Agent 可安全完成的有界任務。  
> Agent 每次只能執行一個 Phase，完成驗收、更新 progress 後必須停止。

文件優先順序固定為：`chat-service-spec.md`（規範性需求）→ 本施工計畫（階段切分）→
`chat-service/AGENTS.md`（執行與安全規則）→ `chat-service-progress.md`（完成證據）。若內容衝突，
Agent 必須停止衝突部分並回報，不可自行選擇版本。

## 共同執行規則

每階段開始前：

1. 完整讀取 `docs/chat-service-spec.md`、本檔與 `docs/chat-service-progress.md`。
2. 讀取 `chat-service/AGENTS.md`。
3. 重新檢查本階段會使用的現有 Retrieval/Answer 契約，不得只靠記憶。
4. 確認 progress 的 `next_eligible_phase` 等於本次 Phase；Phase 3、5、7 還必須遵守 review gate。
5. 若有衝突、前階段錯誤或不明確的破壞性決策，停止並報告，不要猜。

每階段完成後：

1. 執行本 Phase 的驗收命令。
2. 只在有實際證據時將 progress 的實作狀態標為 `completed`。
3. 寫入逐檔 manifest、測試命令與 exit code、pass/fail/skip 數量、偏離與阻擋。Workspace
   根目錄不是 Git repository，不可把 `git diff` 當成唯一交付證據。
4. Phase 3、5、7 完成後將 `review_status` 設為 `pending`，不得前進；強模型 review 通過並將其
   設為 `passed` 後才可更新 `next_eligible_phase`。其他 Phase 的 `review_status` 為 `not_required`。
5. 停止，等待 code review 或下一個 Phase 指令。

## Phase 1：Scaffold 與工程基線

### 目標

建立可 install、lint、typecheck、test 的最小專案架構，但不實作業務功能。

### 必做

- 建立 spec 要求的 package/test/scripts/migrations 目錄與 `__init__.py`。
- 建立 `pyproject.toml`、`uv.lock`、`py.typed`。
- 建立 `.gitignore`、`.dockerignore`、`.env.example`。
- 建立規格第 16 節的完整 `Settings`，包含全部欄位、型別、範圍、跨欄位 timeout/stale
  驗證與 production fail-closed 驗證；不得只做之後需要推翻的空殼。
- 建立 structlog 基礎、request context。
- 建立 Ruff/mypy/pytest/coverage 設定。
- 建立可逐階段擴充的 `Makefile`；本階段至少有 `lock`、`lock-check`、`install`、`lint`、
  `format`、`typecheck`、`test`、`test-unit`；除 `lock` 外全部 `uv sync/run` 使用 frozen lock。
- 移植並調整 Retrieval 的 AST architecture test。
- 建立最小 `README.md`，標明尚未完成功能。

### 不做

- 不建 DB table、HTTP adapter、FastAPI business route、Docker Compose。
- 不建 fake production identity。

### 驗收

```bash
cd /Users/markhuang/Documents/Codex/2026-07-23/new-chat/chat-service
make install
make lock-check
make lint
make typecheck
make test-unit
```

必測：Settings 預設值/範圍、production 禁止 dev identity/development keys/wildcard CORS、
architecture boundary。

## Phase 2：Domain、DTO、Ports 與 Query Processor

### 目標

建立 framework-free core 與完整 domain invariants。

### 必做

- Domain enums/exceptions/entities：Principal、Conversation、ChatTurn、Message、Citation、purge 物件。
- Application DTOs 與規格列出的所有 ports。
- Query Processor v1 的 CRLF/LF、NFC、trim 規則。
- 明確實作 internal turn state → public state 投影、message ordinal allocation 所需 domain invariant，
  以及 strict cursor payload DTO；不得把公開 `processing` 存成另一個未規範 DB state。
- 本 Phase 不建立 orchestration use-case classes；Conversation CRUD/SubmitTurn/GetTurn/ListMessages
  的完整 use cases 由 Phase 5 實作，避免先產生之後必須改寫的空殼。
- 完整 unit tests 與 architecture tests。

### 不做

- 不 import Pydantic/FastAPI/SQLAlchemy/httpx 到 core。
- 不做 query rewrite/history-aware RAG。
- 不實作 Conversation CRUD 或 SubmitTurn orchestration。

### 驗收

```bash
make lint
make typecheck
make test-unit
```

必測：空白/過長欄位、enum/state invariants、owner identity、Query Processor 次序與精確輸出。

## Phase 3：PostgreSQL、Alembic、Repository 與 UoW

### 目標

建立獨立 `chat` schema 、async persistence adapters 與可驗證的 transaction boundary。

### 必做

- SQLAlchemy ORM models，不得把 ORM model 當 Domain entity。
- Alembic initial migration，tables/constraints/indexes 必須符合 spec。
- Async engine/session factory。
- Repository 與 Unit of Work adapters。
- Owner-filtered CRUD、keyset pagination、conversation row lock/ordinal allocation。
- Idempotency unique conflict 與 replay 所需 query。
- Purge preview/digest/row-version/busy/cascade/audit persistence primitives。
- Purge execute 的 preview-first/conversation-ID-order row locks、AND selector 與 1,001 筆拒絕語意。
- Startup stale-turn recovery repository operation。
- Testcontainers PostgreSQL 16 integration tests。
- 擴充 Makefile，加入本 Phase 首次使用的 `test-integration` target。

### 關鍵限制

- MUST NOT 用 SQLite 代替 PostgreSQL integration test。
- MUST NOT 建立跨 `knowledge` schema FK。
- Route 層仍不存在或不接觸 ORM。

### 驗收

```bash
make lint
make typecheck
make test-unit
make test-integration
```

必測：Alembic upgrade、mapping、rollback、owner isolation、cursor/keyset pagination、concurrent
turn/message ordinal、unique race、row lock、purge execute 互斥/TOCTOU、cascade/content-free audit。

## Phase 4：Retrieval/Answer HTTP Adapters 與契約

### 目標

以嚴格本地 schema 實作兩個下游 adapter，完成 allowlist projection。

### 必做

- 重新讀取兩個下游當前 schemas/app/tests。
- Infrastructure-only Pydantic request/response/error schemas，`extra=forbid`。
- Lifespan-owned `httpx.AsyncClient`、fixed URL、redirect off、timeout/body-size/content-type checks。
- Retrieval development identity headers 只由 trusted Principal 建立。
- Retrieval 最多一次安全 transient retry。
- Answer MUST 零自動 retry。
- 實作錯誤分類，但對 Client mapping 留給 application/presentation。
- Contract tests 固定 Retrieval → Answer allowlist、exact query/language、evidence order/content，並覆蓋
  兩個下游的 cross-field invariants、未知 status、413/500、錯誤 content-type、malformed/oversized body。
- 必測合法 mixed ranks（allowed `[2]`/restricted `[1]`）與跨 answer parts 重用同一 citation ID，
  避免把合法下游 response 誤判為 contract error。
- 擴充 Makefile，加入本 Phase 首次使用的 `test-contract` 與 `test-security` targets。

### 安全驗收

- `restricted_evidence` 無法進入 Answer request type。
- Client-supplied identity/URL/key 無法進 adapter。
- Raw error body/secrets 不進 exception message/log。

### 驗收

```bash
make lint
make typecheck
make test-unit
make test-contract
make test-security
```

## Phase 5：Orchestration、Idempotency 與 Capacity

### 目標

完成 `SubmitTurn` 的狀態機、三路 Retrieval 分流、Answer 分流、冪等與 bounded
capacity，但尚不暴露 FastAPI route。

### 必做

- Create/list/get/update conversation use cases。
- Submit/get turn 與 list messages use cases。
- 取 capacity → Retrieval → optional Answer 的流程。
- 不持有 DB transaction 等 queue/HTTP。
- 完整 business/dependency mapping。
- 同 key replay、processing 202 outcome、different-payload 409。
- Idempotency UUID canonicalization；等價大小寫/無連字號表示必須命中同一 turn。
- `failed`/`failed_unknown` 與 no Answer auto-retry。
- Bounded active+waiting gate、429/Retry-After；被拒絕的已建立 turn 必須依規格落為 terminal
  `failed`，保存安全錯誤碼與 retryable flag，使同 key 能重播同一個 429。
- Fake ports 下的 exhaustive unit/security tests。

### 關鍵驗收

- Mixed evidence 只送 allowed。
- Pure restricted/insufficient 的 Answer fake call count 為 0。
- Answer outbound block 不 fallback。
- 同 key 重送的 Retrieval/Answer call count 不增加。
- Attestation 不存 DB。

### 驗收

```bash
make lint
make typecheck
make test-unit
make test-contract
make test-security
make test-integration
```

## Phase 6：FastAPI、Identity、Admin Purge 與 Observability

### 目標

完成全部 HTTP API、middleware、health/readiness、metrics 與 admin 兩階段 purge。

### 必做

- Development identity adapter 與 production fail closed。
- Spec 的所有 employee/admin endpoints 與 strict schemas。
- Unified safe error envelope 與 HTTP mapping。
- Pure-ASGI bounded streaming body middleware。
- Request ID/CORS/security headers 的合理設定。
- Live/ready/metrics；readiness 不呼叫 generation。
- Isolated Prometheus registry 與低 cardinality metrics。
- JSON logging/redaction。
- Purge preview/execute 所有限制。
- 在 lifespan/bootstrap 啟動完成前執行一次 stale-turn recovery；失敗時拒絕 ready/startup，並驗證
  internal processing states、DB time cutoff、固定 503 安全錯誤與 row-version 更新。
- API/security tests，包含 IDOR、body identity forgery、admin role、extra fields/body size。

### 驗收

```bash
make lint
make typecheck
make test-unit
make test-contract
make test-security
make test-integration
```

## Phase 7：Docker、Smoke/E2E、文件與最終驗收

### 目標

完成可在 Intel x86 Docker 本機執行，且不寫死平台的 Chat Service。

### 必做

- Pinned multi-arch Dockerfile，non-root、frozen uv layers。
- Compose `migrate` + `chat-api`，只 build 一次 image，read-only/cap-drop/no-new-privileges；
  `chat-api` 必須以 successful `migrate` completion 作啟動 barrier。
- External `helpdesk-rag` network，loopback port 8120。
- Makefile 全部 targets。
- 建立只供 host script 使用且不注入 Chat runtime 的 `.env.e2e.example`；實際 `.env.e2e` 必須
  ignore。`smoke-e2e` 需明確 opt-in 且 KIS key 非空，並先以該 key 通過 KIS
  `/api/v1/auth/check`；缺少/錯誤時在寫入前失敗且絕不洩漏 key。
- `make preflight` 依規格第 18 節只讀驗證 external network、PostgreSQL、KIS、Retrieval、Answer；
  README 明列先 KIS、再 Retrieval、再 Fake Answer、最後 Chat 的啟動順序。Ingestion 非前置。
- `docker_smoke_test.py`：health/CRUD/archive/admin purge，不生成答案；purge 只可刪除同一 script
  本次建立並記錄的 synthetic conversation IDs，不得使用廣泛 selector。
- `chat_e2e_test.py`：真實 Retrieval + KIS + Answer Fake Provider 與 idempotent replay。因既有 KIS
  smoke fixture 是 restricted，script 必須依規格第 18 節透過 KIS HTTP 建立/重用固定 source
  identity 的 synthetic `external_llm_allowed=true` fixture，等待索引完成，並在 `finally` 只 soft-delete
  KIS 回傳的精確 document ID。整個 fixture lifecycle 必須持有有 timeout 的 host-local exclusive
  advisory lock，避免平行 E2E 互刪。Submit `job_id=null` 時必須驗證已 indexed，不可 poll null；
  delete job 必須完成。Chat runtime 不得呼叫 KIS。
- 完整 `chat-service/README.md`。
- 更新根 `README.md` 的五服務架構、port、啟動順序與未完成範圍。
- 檢查 image 無 `.env`、secret、tests、company documents。

### 最終命令

```bash
make install
make lock-check
make lint
make typecheck
make test-unit
make test-contract
make test-security
make test-integration
make config
make build-native
make preflight
docker compose up -d --build
make smoke
make smoke-e2e
```

`make build-arm64` target 必須存在。若 Intel 上的 ARM 模擬執行過慢，progress 可標記未執行與
原因，但必須證明 Compose 與 Dockerfile 無寫死 amd64。

### 最終人工檢查

- Container UID 非 0，root filesystem 不可寫。
- 只對 host 暴露 `127.0.0.1:8120`。
- Logs 不含 query/answer/evidence/user/token/key/attestation。
- Restricted raw content/payload 無法進 Answer/DB/log/public API；DB 最多只有規格允許的 machine
  IDs + safe citation 投影，public API 只顯示 safe citation。
- Production 組態在未有 Entra adapter 時拒絕啟動。
- README 不宣稱 production-ready 或已完成 30 人壓測。

## Code review checkpoints

建議使用較強模型在以下時點只做 review，不擴張範圍：

1. Phase 3 後：migration、transaction、ownership、purge 資料安全。
2. Phase 5 後：attestation allowlist、restricted evidence、idempotency、timeout。
3. Phase 7 後：全契約、container security、E2E 與規格完整度。
