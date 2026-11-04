# Chat Service v1 開發規格

> 狀態：Approved for implementation  
> 規格版本：1.0  
> 日期：2026-08-15  
> 專案路徑：`/Users/markhuang/Documents/Codex/2026-07-23/new-chat/chat-service`

本檔案是 Chat Service v1 的唯一規範性來源（source of truth）。`MUST`、`MUST NOT`
是必須驗收的要求；`SHOULD` 是有強烈理由才可偏離的要求。實作進度與每階段驗收請分別更新
[`chat-service-progress.md`](./chat-service-progress.md) 與
[`chat-service-implementation-plan.md`](./chat-service-implementation-plan.md)。

## 1. 目標與範圍

Chat Service 是 Help Desk RAG 的使用者入口與 orchestration owner：

- 接收同仁問題，建立與讀取對話。
- 執行 deterministic Query Processor v1。
- 依序呼叫 Retrieval Service 與 Answer Generation Service。
- 嚴格分流 allowed 與 restricted evidence。
- 永久保存對話，直到管理員明確 hard purge。
- 實作對話所有權、封存、安全引用、冪等及有界容量。

v1 **不包含**：

- Web/React 前端。
- Entra ID/M365 JWT 實作（只保留 port 並 fail closed）。
- SharePoint connector、OCR。
- 多輪語意、對話摘要、LLM query rewrite。
- Streaming/SSE、Redis、Kafka、RabbitMQ、Celery。
- 使用者 feedback/rating API（放到 Chat v1.1，v1 不得自行加入）。
- 直接連線 Qdrant、`knowledge` schema 或 `ingestion` schema。
- 直接呼叫 NVIDIA 或任何 LLM Provider。

v1 是「有永久歷史的單輪 RAG」：Conversation 可容納多個 turn，但每個 turn 獨立
檢索，不把歷史訊息加入下一次 Retrieval 或 Answer request。

## 2. 現有系統與唯一允許的依賴

| 服務 | Host port | Docker DNS | Chat 的用途 |
|---|---:|---|---|
| Knowledge Index | `8080` | `knowledge-index-api:8080` | Chat 不得直接呼叫 |
| Ingestion | `8090` | `ingestion-api:8090` | 非 runtime dependency |
| Retrieval | `8100` | `retrieval-api:8100` | 身分與安全 evidence 檢索 |
| Answer Generation | `8110` | `answer-generation-api:8110` | 只處理已簽章 allowed context |
| Chat | `8120` | `chat-api:8120` | 本專案 |

實作前 MUST 讀取現有契約，不得複製或 import sibling package：

- `retrieval-service/src/retrieval_service/presentation/schemas.py`
- `retrieval-service/src/retrieval_service/presentation/app.py`
- `retrieval-service/src/retrieval_service/infrastructure/identity/development.py`
- `retrieval-service/README.md`
- `answer-generation-service/src/answer_generation_service/presentation/schemas.py`
- `answer-generation-service/src/answer_generation_service/presentation/app.py`
- `answer-generation-service/README.md`

若本規格與實際契約衝突，MUST 停止該階段，在 progress 記錄檔案、行號、衝突及建議；
MUST NOT 自行修改其他四個服務。

## 3. 技術與工程基線

- Python `3.11`；`requires-python = ">=3.11,<3.14"`。
- FastAPI、Pydantic v2/pydantic-settings。
- SQLAlchemy 2 async、asyncpg、Alembic。
- `httpx.AsyncClient`、structlog JSON、prometheus-client。
- `src/chat_service` package layout，含 `py.typed`。
- `hatchling==1.27.0`。
- MUST 提交 `uv.lock`；除唯一會更新 lockfile 的 `make lock` 外，所有 `uv sync`/`uv run` 都必須
  使用 `--frozen`，包含 install、lint、format、typecheck、tests 與 Docker build。
- MUST NOT 使用 `pip install .`，不得引入 torch、transformers、CUDA/NVIDIA runtime。
- Ruff：`py311`、line length 100、rules `E,F,I,B,UP,ASYNC,RUF`。
- mypy `strict = true`。
- pytest：`asyncio_mode = auto`、`--strict-markers`、branch coverage。

Production modules MUST 以繁體中文 docstring 說明 Clean Architecture、async、transaction、
port/adapter 與不直觀的 Python 語法；MUST NOT 加入逐行重複型註解。

## 4. Clean Architecture

```text
presentation/FastAPI
        -> application/use cases + DTOs + ports
                -> domain
infrastructure adapters -> application ports
bootstrap.py -> composition root
```

目錄 MUST 至少包含：

```text
src/chat_service/
  domain/{entities,enums,exceptions}.py
  application/{dtos,ports,use_cases,query_processor}.py
  infrastructure/
    config.py logging.py request_context.py capacity.py
    identity/development.py
    persistence/{database,models,repositories,uow}.py
    retrieval/{http_client,schemas}.py
    answer_generation/{http_client,schemas}.py
  presentation/{app,middleware,schemas}.py
  bootstrap.py
```

強制依賴規則：

- Domain 只能 import Python 標準庫。
- Application 只能依賴 Domain 與自身 DTO/port，MUST NOT import FastAPI、Pydantic、
  SQLAlchemy、httpx、Prometheus 或 structlog。
- Infrastructure 實作 ports。
- Presentation 只做 HTTP/schema/identity-input/error mapping，route MUST NOT 操作 SQLAlchemy。
- `bootstrap.py` 是唯一 composition root。
- MUST 以 AST architecture test 防止依賴逆轉。

Application ports 至少包含：

- `IdentityProviderPort`
- `ConversationRepositoryPort`
- `TurnRepositoryPort`
- `PurgeRepositoryPort`
- `UnitOfWorkPort`
- `RetrievalGatewayPort`
- `AnswerGenerationGatewayPort`
- `CapacityGatePort`
- `ClockPort`

Use cases 至少包含：

- `CreateConversation`
- `ListConversations`
- `GetConversation`
- `UpdateConversation`
- `SubmitTurn`
- `GetTurn`
- `ListMessages`
- `PreviewConversationPurge`
- `ExecuteConversationPurge`

## 5. 身分、所有權與開發模式

v1 只實作 `development_headers` adapter：

- `x-dev-user-id`：必填，trim 後不可空白，最長 512。
- `x-dev-group-ids`：逗號分隔，最多 100 個，trim 後不可空白或重複。
- `x-dev-roles`：逗號分隔，最多 20 個，trim 後不可空白或重複。
- Development adapter MUST 拒絕 Bearer token。

Development principal：

```text
issuer     = urn:helpdesk:development
subject    = x-dev-user-id
tenant_id  = null
group_ids  = x-dev-group-ids
roles      = x-dev-roles
```

管理員角色固定為 `helpdesk-admin`。Owner MUST 來自已驗證 principal，API body MUST NOT 含
`user_id`、`group_ids`、`roles`、`is_admin`。所有 owner query MUST 同時以
`(owner_issuer, owner_subject, resource_id)` 過濾；非 owner 與不存在都回 404。

`APP_ENV=production` MUST 禁止 `development_headers`。`entra_jwt` 可作為設定值保留，但本版
MUST 拒絕啟動，不得用假 JWT parser 讓 production 啟動。

## 6. Query Processor v1

Query Processor MUST deterministic 且不呼叫 LLM：

1. `original_question` 保存 request 原文。
2. `processed_query` 依序執行 CRLF → LF、Unicode NFC、移除頭尾空白。
3. 不壓縮內部空白，不翻譯、拼字修正、rewrite 或加入 history。
4. `language` 可為 null；有值時 trim，不推測預設語言。
5. Public request 的原始 `question` 必須 1–20,000 字元且不可全空白；處理後 query
   也必須 1–20,000。
6. Public request 的 `language` 可 null；有值時原始長度 1–20，trim 後仍必須
   1–20。不合法輸入由 Chat 直接回 422，不可等 Retrieval 回 422 才轉成 502。

關鍵 invariant：

```text
Retrieval request.query    == processed_query
Answer request.question    == processed_query
Retrieval request.language == Answer request.language
```

Retrieval 回覆後 MUST NOT 再 trim、翻譯或改寫 question/evidence。

## 7. Chat → Retrieval 契約

```http
POST http://retrieval-api:8100/api/v1/retrieve
content-type: application/json
x-internal-api-key: <RETRIEVAL_API_KEY>
x-request-id: <Chat request ID>
x-dev-user-id: <principal.subject>             # development only
x-dev-group-ids: <comma-separated group IDs>  # development only
```

```json
{
  "query": "<processed_query>",
  "limit": 8,
  "language": "zh-TW"
}
```

- `limit` MUST 來自 `CHAT_RETRIEVAL_LIMIT`，預設 8，允許 1–20；Client 不得指定。
- Chat MUST 使用自己 lifespan 共用的 async client、固定 base URL、timeout、response-size
  limit、關閉 redirect 與嚴格 response schema。
- Retrieval 為無副作用查詢，Chat 可最多做一次有界 transient retry。
- MUST NOT 將 downstream raw error body 直接回給 Client。
- HTTP 200 的 Pydantic shape 驗證後，MUST 再驗證與 Retrieval Domain 一致的跨欄位
  invariants：
  - `evidence_found`：`llm_context` 非空、attestation 非空、blocked flag 為 false。
  - `external_generation_blocked`：allowed context 空、restricted 非空、attestation null、
    blocked flag 為 true。
  - `insufficient_evidence`：兩個 evidence list 都空、attestation null、blocked flag 為 false。
  - `llm_context` 每筆 `external_llm_allowed=true`；restricted 每筆為 false。
  - `llm_context` 與 `restricted_evidence` 各自依 rank 升冪；兩個 list 的 union 依 rank 排序後，
    ranks 必須全域唯一且恰為 `1..selected_count`。合法 mixed response 可以是 allowed ranks `[2]`
    與 restricted ranks `[1]`，不可直接串接兩個 list 後要求升冪。
  - `retrieval.selected_count == len(llm_context) + len(restricted_evidence)`，且
    `retrieval.candidate_count >= retrieval.selected_count`。
  任一 invariant 失敗都是 502 `retrieval_contract_error`，MUST NOT 呼叫 Answer。

Retrieval 的三種 HTTP 200 業務狀態：

| `status` | Chat 行為 |
|---|---|
| `evidence_found` | 驗證 `llm_context` 與 attestation 非空，再呼叫 Answer |
| `external_generation_blocked` | MUST NOT 呼叫 Answer；只保存安全 citation |
| `insufficient_evidence` | MUST NOT 呼叫 Answer；citations 為空 |

`evidence_found` 可能同時含 `restricted_evidence`。Chat MUST 完全忽略它，不得送給
Answer、寫入 DB、log 或 public response。`external_generation_blocked` 只可取每筆
restricted evidence 的 machine IDs 與 citation，MUST NOT 保存或回傳 raw `content`。

## 8. Retrieval → Answer 唯一合法投影

```http
POST http://answer-generation-api:8110/api/v1/answers
content-type: application/json
x-internal-api-key: <ANSWER_GENERATION_API_KEY>
x-request-id: <same Chat request ID>
```

Body MUST 以 allowlist 新建：

```json
{
  "question": "<processed_query>",
  "language": "<same language or null>",
  "evidence": "<Retrieval response.llm_context, unchanged and ordered>",
  "context_attestation": "<Retrieval context_attestation>"
}
```

MUST NOT 傳送完整 Retrieval response、`restricted_evidence`、`status`、`retrieval` diagnostics。MUST NOT
修改、刪減、截短、重排 evidence，不得重新編號 rank。Attestation 為 opaque token，Chat
MUST NOT 解析、修改、重簽或保存。

Chat 取得 `evidence_found` 後要立即呼叫 Answer；MUST NOT 對 Answer POST 自動 retry，因現有
Answer 無 idempotency contract。

Answer HTTP 200 也 MUST 實作跨欄位 invariant 驗證：

- `answered`：`answer_parts` 非空，每段 text 非空且至少有一個 citation ID；同一 part 內不可重複。
- Response citation objects 的 `citation_id` 必須全域唯一。每個 part reference 必須存在於 response
  citations；不同 parts 可以合法重用同一 citation ID，但不可有未被任何 part 使用的額外 citation object。
- `unable_to_answer`：`answer_parts` 與 citations 都必須為空。
- status 只能是契約列出的值。

任一 invariant 失敗都是 502 `answer_contract_error`。

## 9. Chat 業務結果

Public `outcome` 固定為：

- `answered`
- `unable_to_answer`
- `external_generation_blocked`
- `insufficient_evidence`
- `outbound_policy_blocked`

`answered`：保存 Answer `answer_parts` 與 citations；`content` 以 parts 原順序用兩個換行組合。
`external_generation_blocked` 的安全 citations 依 restricted evidence 全域 rank 升冪排序，由 Chat
重新指定 public `citation_id` 為 `C1`、`C2` …；不沿用或暴露 machine ID。

其他結果的固定訊息：

| outcome | 訊息 |
|---|---|
| `unable_to_answer` | 目前無法根據已取得的公司文件可靠回答此問題。 |
| `external_generation_blocked` | 已找到相關文件，但依資料外送政策，這些內容不能送至外部 AI 產生答案。請參考文件引用或聯絡文件負責單位。 |
| `insufficient_evidence` | 目前找不到足夠的公司文件依據，無法可靠回答。請換個方式提問或聯絡 Help Desk。 |
| `outbound_policy_blocked` | 此問題或相關內容依資料外送政策無法送至目前的 AI 服務，因此未產生答案。 |

`unable_to_answer`、Retrieval 兩種無生成結果與 Answer `outbound_policy_blocked` 都是 HTTP 200，
不可偽裝成 infrastructure error。

Public citation 只可回：

```json
{
  "citation_id": "C1",
  "title": "遠端工作規章",
  "section_path": "第三條",
  "page_number": 3
}
```

`chunk_id`、`document_id`、`version_id` MUST 保存於 DB 供稽核，MUST NOT 顯示於 employee API。
`source_path`、`source_url` 不得出現在 Chat domain、DB、response 或 log。

## 10. Downstream 錯誤轉換

Retrieval：

| Downstream | Chat HTTP / code |
|---|---|
| 401 `unauthorized` | 502 `retrieval_auth_misconfigured` |
| 401 `unauthorized_principal` | 502 `retrieval_identity_rejected`（Chat 已驗證 principal） |
| 400/413/422 | 502 `retrieval_contract_error` |
| 200 但 schema/invariant/JSON/content-type 錯誤 | 502 `retrieval_contract_error` |
| 500 或未列出的 status | 502 `retrieval_service_error` |
| 502 | 502 `retrieval_contract_error` |
| 503 | 503 `retrieval_unavailable` |
| 504 | 504 `retrieval_timeout` |
| DNS/connect failure | 503 `retrieval_unavailable` |

Answer：

| Downstream | Chat HTTP / code |
|---|---|
| 401 | 502 `answer_auth_misconfigured` |
| 403 `context_attestation_invalid` | 502 `answer_context_rejected` |
| 403 `outbound_policy_blocked` | 200 business outcome |
| 413/422 | 502 `answer_contract_error` |
| 200 但 schema/invariant/JSON/content-type 錯誤 | 502 `answer_contract_error` |
| 429 | 503 `generation_busy`，保留已驗證 `Retry-After` |
| 502 | 502 `provider_contract_error` |
| 503 | 503 `provider_unavailable` |
| 504 | 504 `provider_timeout` |
| 500 或未列出的 status | 502 `answer_service_error` |
| DNS/connect failure | 503 `provider_unavailable` |

Dependency error MUST NOT 轉成 `insufficient_evidence`。錯誤 envelope 固定：

```json
{
  "error": "machine_readable_code",
  "message": "safe message",
  "request_id": "...",
  "details": null
}
```

MUST NOT 回傳 traceback、downstream URL/key/raw body、token、attestation、query 或 evidence。

## 11. Employee API

所有 Pydantic API models MUST 使用 `ConfigDict(extra="forbid")`。

### 11.1 `POST /api/v1/conversations`

Request：`{"title": null}`。`title` 可 null；有值時 1–200 字元且不可全空白。MUST NOT
自動用 LLM 產生標題。Response HTTP 201：

```json
{
  "conversation_id": "uuid",
  "title": null,
  "status": "active",
  "created_at": "timestamptz",
  "updated_at": "timestamptz",
  "last_activity_at": "timestamptz"
}
```

### 11.2 `GET /api/v1/conversations`

Query：`limit=20&cursor=<opaque>`；limit 1–100。只查 principal 自己的資料，依
`last_activity_at DESC, id DESC` 的 keyset/cursor pagination，MUST NOT 使用無界 offset，response
不含 owner ID。Response HTTP 200：

```json
{
  "items": [
    {
      "conversation_id": "uuid",
      "title": null,
      "status": "active",
      "created_at": "timestamptz",
      "updated_at": "timestamptz",
      "last_activity_at": "timestamptz"
    }
  ],
  "next_cursor": null
}
```

Cursor MUST 是 URL-safe opaque token，內含 version、last activity timestamp 與 UUID；必須完整驗證長度/
decode/schema。畸形 cursor 回 422 `invalid_cursor`。Cursor 不得取代 owner filter。

### 11.3 `GET /api/v1/conversations/{conversation_id}`

只有 owner 可讀；非 owner 與不存在均回 404。HTTP 200 body 與 11.1 的 conversation
response 相同。

### 11.4 `PATCH /api/v1/conversations/{conversation_id}`

Body 可更新 `title` 與/ 或 `status`，但至少要有一欄。`status` 只能 `active|archived`。
Archived conversation 依然可讀，但新 turn 回 409 `conversation_archived`。普通使用者不可
hard delete。HTTP 200 body 為更新後的 conversation response。

### 11.5 `POST /api/v1/conversations/{conversation_id}/turns`

Header `Idempotency-Key` raw value 最多 64 字元，trim 後 MUST 可解析為 UUID；Chat 必須立即以
Python `str(UUID(value))` 轉成 lowercase hyphenated 36 字元 canonical form，後續 lookup、insert、
unique conflict 與 replay 全部只使用 canonical value。大小寫、連字號等不同但等價的 UUID 表示
必須命中同一 turn；空白/無效值回 422 `invalid_idempotency_key`。Body：

```json
{
  "question": "員工每週可以遠端工作幾天？",
  "language": "zh-TW"
}
```

Body MUST NOT 含 identity、model、prompt、provider、URL 或 retrieval limit。Terminal HTTP 200 response：

```json
{
  "conversation_id": "uuid",
  "turn_id": "uuid",
  "state": "completed",
  "outcome": "answered",
  "user_message": {
    "message_id": "uuid",
    "role": "user",
    "content": "原始問題",
    "created_at": "timestamptz"
  },
  "assistant_message": {
    "message_id": "uuid",
    "role": "assistant",
    "response_kind": "answered",
    "content": "回答",
    "answer_parts": [{"text": "...", "citation_ids": ["C1"]}],
    "citations": [
      {"citation_id": "C1", "title": "...", "section_path": null, "page_number": 1}
    ],
    "created_at": "timestamptz"
  },
  "request_id": "..."
}
```

若同 key 仍在處理，HTTP 202 回：

```json
{
  "conversation_id": "uuid",
  "turn_id": "uuid",
  "state": "processing",
  "outcome": null,
  "status_url": "/api/v1/conversations/<conversation_id>/turns/<turn_id>",
  "request_id": "..."
}
```

### 11.6 `GET /api/v1/conversations/{conversation_id}/turns/{turn_id}`

只可查自己 conversation。HTTP 200 response：

```json
{
  "conversation_id": "uuid",
  "turn_id": "uuid",
  "state": "processing|completed|failed|failed_unknown",
  "outcome": null,
  "user_message": {
    "message_id": "uuid",
    "role": "user",
    "content": "原始問題",
    "created_at": "timestamptz"
  },
  "assistant_message": null,
  "error": null,
  "request_id": "...",
  "created_at": "timestamptz",
  "updated_at": "timestamptz"
}
```

Completed 時帶與 11.5 相同的安全 messages/outcome。Failed 時 `assistant_message=null`，`error`
只含 `code`、safe `message`、`retryable`與可選 `retry_after_seconds`，不回 downstream details。

### 11.7 `GET /api/v1/conversations/{conversation_id}/messages`

Query：`limit=50&cursor=<opaque>`；limit 1–100；依 ordinal ASC 的 keyset pagination。MUST NOT 回傳
diagnostics、machine UUID、evidence 或 attestation。Response HTTP 200：

```json
{
  "items": [
    {
      "message_id": "uuid",
      "role": "user|assistant",
      "content": "...",
      "response_kind": null,
      "answer_parts": null,
      "citations": [],
      "created_at": "timestamptz"
    }
  ],
  "next_cursor": null
}
```

User message 的 `answer_parts=null`、citations 為空。Assistant 依 business outcome 回安全 parts/citations。

Conversation 與 message cursor 共用 strict codec：最多 512 字元、URL-safe base64、不得有 padding，
decode 後是 UTF-8 canonical JSON 且拒絕 extra/missing fields。Canonical JSON 使用
`json.dumps(..., ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)`。
Conversation cursor payload 固定為
`{"v":1,"last_activity_at":"<UTC RFC3339>","id":"<UUID>"}`；message cursor 固定為
`{"v":1,"ordinal":<positive integer>}`。任何長度、base64、UTF-8、JSON、version、timestamp、UUID
或 ordinal 錯誤都回 422 `invalid_cursor`。Cursor 只是 keyset 位置，不得削弱 owner filter。

Employee API 共同錯誤：identity 缺失/不合法 401、request/schema 422、owner mismatch/not found
404、archived/idempotency conflict 409、capacity 429，再加上第 10 節 dependency errors。

## 12. Idempotency、狀態與 transaction

`request_fingerprint` 固定使用 Query Processor 輸出，不使用 raw `original_question`。以 Python
`json.dumps({"language": normalized_language, "question": processed_query}, ensure_ascii=False,
sort_keys=True, separators=(",", ":"), allow_nan=False)` 的 UTF-8 bytes 計算 SHA-256。因此只差
CRLF/NFC/頭尾空白的重試視為同一 payload，並重播第一次保存的 original question。DB MUST 以
`(conversation_id, idempotency_key)` unique constraint 解決競態：

- 同 key/同 fingerprint/已完成：重播原結果，不再呼叫下游。
- 同 key/同 fingerprint/處理中：HTTP 202，回 `turn_id` 與 status URL。
- 同 key/不同 fingerprint：409 `idempotency_conflict`。
- 同 key/已失敗：重播原安全錯誤，不自動 retry。

內部持久化 `state` 固定為：

```text
accepted | waiting_for_capacity | retrieving | generating |
completed | failed | failed_unknown
```

Public API 不暴露內部 phase；投影固定為：`accepted|waiting_for_capacity|retrieving|generating` →
`processing`，其餘三個 state 原值回傳。本文所有「processing turn」都專指這四個內部非 terminal
states。POST 同 key 在任一 processing state 都回 202。

Timestamp 規則固定：建立為 `accepted` 時 `created_at/updated_at` 使用 database time、
`started_at/completed_at` 為 null；成功取得 capacity 並首次進入 `retrieving` 時設定一次
`started_at`；每次 transition 更新 `updated_at`；三種 terminal state 設定一次 `completed_at`。

流程狀態：

```text
accepted -> waiting_for_capacity -> retrieving -> generating -> completed
             |                        |              |
             +-> failed(429)          +-> failed     +-> failed / failed_unknown
```

- 建立 turn 與 user message 後 MUST 先 commit。
- 等 capacity 與呼叫 HTTP 時 MUST NOT 持有 DB transaction。
- 寫 assistant message、citations、terminal turn 與 conversation activity MUST 同一 transaction。
- MUST 使用 row lock/CAS 防止重複生成。
- 每次內部 state transition 都更新 `updated_at`。啟動時只將上述四種 processing state 且
  `updated_at < database_now - PROCESSING_STALE_SECONDS` 的 turn 標為 `failed_unknown`；同一
  recovery transaction 固定寫入 `response_http_status=503`、
  `safe_error_code=chat_processing_interrupted`、固定訊息
  `先前的處理程序已中斷，無法確認是否完成；請使用新的請求重新送出。`、`error_retryable=false`、
  `completed_at=database_now`，並增加 conversation row version。之後同 key 重播同一 503，
  不可自動重送可能已由 Provider 處理的請求。
- Capacity 已滿或 queue wait timeout 時，MUST 在新 transaction 將 turn 標為 terminal
  `failed`、HTTP 429、`safe_error_code=chat_capacity_exceeded`、`error_retryable=true`並寫 completed time。
  同 key 後續重播同一 429，不得永久留在 processing/202。
- Answer timeout、程序中斷等無法確認 Provider 結果的情況 MUST 為 `failed_unknown`，不自動重送。
- MUST NOT 保存 Bearer token 或建立需要 token 的背景 job。

## 13. PostgreSQL `chat` schema

本機共用現有 PostgreSQL instance/database，但所有 table MUST 在獨立 `chat` schema。本機可沿用
`knowledge` DB user；production 獨立 least-privilege role 不在 v1。Alembic migration MUST 建立：

### 13.1 `chat.conversations`

| 欄位 | 型別/規則 |
|---|---|
| `id` | UUID PK |
| `owner_issuer` | varchar(255) not null |
| `owner_subject` | varchar(512) not null |
| `tenant_id` | varchar(255) null |
| `title` | varchar(200) null |
| `status` | varchar(20), check `active|archived` |
| `next_turn_ordinal` | integer default 1, > 0 |
| `next_message_ordinal` | integer default 1, > 0 |
| `row_version` | bigint default 1, > 0 |
| `created_at/updated_at/last_activity_at` | timestamptz not null |

Index：`(owner_issuer, owner_subject, last_activity_at DESC, id DESC)`。

`row_version` MUST 在所有會改變 conversation 內容/子資源的 transaction 原子加 1：
更新 title/status、分配新 turn ordinal/建立 user message、寫 terminal/failure state、assistant message
或 citations。建立 user/assistant message 時都必須先鎖 conversation row，取目前
`next_message_ordinal` 作該 message ordinal，再原子加 1；不得用 `max(ordinal)+1`。這允許並行 turns
仍維持實際寫入順序與唯一 keyset。`last_activity_at` 在新 turn 與 terminal 結果時更新；光讀取或建立 purge preview
不增加版本。這保證 preview 後任何 child mutation 都會使 purge digest 失效。

### 13.2 `chat.turns`

| 欄位 | 型別/規則 |
|---|---|
| `id` | UUID PK |
| `conversation_id` | UUID FK cascade |
| `ordinal` | integer > 0 |
| `idempotency_key` | varchar(36) |
| `request_fingerprint` | char(64) |
| `original_question/processed_query` | text, length 1–20000 |
| `language` | varchar(20) null |
| `state` | varchar(40), check 七種 internal states |
| `retrieval_status/answer_status/outcome` | varchar, nullable |
| `response_http_status` | smallint nullable |
| `safe_error_code` | varchar(100) null |
| `safe_error_message` | text null |
| `error_retryable` | boolean not null default false |
| `retry_after_seconds` | integer null, >= 0 |
| `request_id` | varchar(128) not null |
| `retrieval_diagnostics/generation_diagnostics` | JSONB null |
| timestamps | created/updated/started/completed timestamptz |

Unique：`(conversation_id, ordinal)` 與 `(conversation_id, idempotency_key)`。Diagnostics 只能含計數、
model revision、provider/model/prompt version、finish reason 與 token counts，MUST NOT 含內文或 secret。

### 13.3 `chat.messages`

`id` UUID PK、conversation/turn FK cascade、`ordinal` integer、`role` check `user|assistant`、
`response_kind` nullable、`content` text、`answer_parts` JSONB nullable、`payload_schema_version`、
`created_at`。Unique：`(turn_id, role)` 與 `(conversation_id, ordinal)`。

### 13.4 `chat.citations`

`id` UUID PK、`assistant_message_id` FK cascade、`citation_id`、`ordinal`、chunk/document/version UUID、
`title` varchar(1000)、`section_path` varchar(2000) null、`page_number` null 或 > 0。Unique：
`(assistant_message_id, citation_id)`。MUST NOT 建立跨 schema FK。

### 13.5 `chat.purge_previews`

UUID PK、selector JSONB、selected conversations（只存 ID 與 row_version）JSONB、SHA-256 digest、
conversation/message counts、creator issuer/subject、expires/consumed/created timestamps。

### 13.6 `chat.purge_audit`

UUID PK、actor issuer/subject、selector digest、conversation/message counts、reason varchar(500)、request ID、
purged time。MUST NOT 含被刪問題、回答、evidence 或文件名。

Conversation 預設永久保留：MUST NOT 有 TTL 或定時刪除。

## 14. 管理員兩階段 hard purge

### `POST /api/v1/admin/conversation-purge-previews`

Body 可以 `conversation_ids`(最多 100)、`owner_issuer+owner_subject`、`created_before` 組合選取，
但 MUST 至少有一種 selector，MUST NOT 允許無條件全刪。多種 selector 的語意固定為 AND；
`owner_issuer` 與 `owner_subject` 必須同時出現。實作 MUST 查到第 1,001 筆以辨識超量，超過 1,000
回 422 `purge_selection_too_large`，不得截斷後建立 preview。Preview
預設 600 秒過期，response HTTP 201 只回 preview ID、conversation/message/busy counts 與過期時間，
不回內容。

### `POST /api/v1/admin/conversation-purges`

Body：`preview_id` 與必填 `reason`(1–500)。執行前 MUST 驗證：

- Principal 含 `helpdesk-admin`。
- Principal issuer/subject 與 preview creator 完全相同；不相同視為 404 `purge_preview_not_found`。
- Preview 未過期、未使用。
- IDs/row versions digest 未改變，否則 409 `purge_preview_stale`。
- 無 processing turn，否則 409 `conversation_busy`。

兩個 admin endpoints 都必須先驗證 `helpdesk-admin`。Execute MUST 使用單一 transaction，依序：
`SELECT preview FOR UPDATE`；驗證 creator identity、未過期、
未使用；依 UUID 升冪 `SELECT selected conversations FOR UPDATE`；重新驗證 ID 集合、row versions、
digest 與無 processing turn；寫 content-free audit；cascade hard delete；標記 preview consumed；commit。
所有會新增/修改 conversation child 的流程也 MUST 先鎖同一 conversation row，因此 preview 後的
SubmitTurn 無法穿過 execute。兩個 concurrent execute 必須由 preview row lock 串行化，第二個得到
409 `purge_preview_consumed`。任一 selected row 遺失/版本不同回 409 `purge_preview_stale`；鎖定順序
固定以避免 deadlock。不得部分刪除。Response HTTP 200 回 purge ID、counts 與時間。

## 15. Capacity、timeout 與可觀測性

單一 Chat process 的 bounded capacity 預設：

```text
MAX_ACTIVE_TURNS=10
MAX_TURN_QUEUE_SIZE=20
TURN_QUEUE_WAIT_TIMEOUT_SECONDS=120
```

超出時回 429 `chat_capacity_exceeded` + `Retry-After`。這只代表 queue 有界，不代表已證明
30 人 production 容量。MUST 在取得 capacity 後才開始 Retrieval，避免 attestation 在 queue
過期。

v1 只支援單一 Chat process/replica。`PROCESSING_STALE_SECONDS` MUST 大於
`TURN_QUEUE_WAIT_TIMEOUT_SECONDS + RETRIEVAL_TOTAL_TIMEOUT_SECONDS + ANSWER_TOTAL_TIMEOUT_SECONDS + 30`，
預設 360 秒。Settings 必須在啟動時驗證此關係，避免合法處理中的 turn 被 stale
recovery 錯殺。多 replica lease/heartbeat 為 production 後續工作。

HTTP infrastructure MUST 有 connect/read/total timeout、response byte limit、關閉 redirects、固定 base URL。
`RETRIEVAL_TOTAL_TIMEOUT_SECONDS` 是涵蓋首次 attempt、bounded backoff 與唯一一次 retry 的單一
end-to-end deadline，不是每個 attempt 各自取得一份 timeout。只有 connect/DNS failure、connect/read
timeout、HTTP 503/504 可在 deadline 尚有餘額時 retry；4xx、500、502、oversized body、錯誤
content-type、JSON/schema/invariant 錯誤一律不 retry。Backoff 不得延長 total deadline。

Endpoints：

- `GET /health/live`：只檢查 process。
- `GET /health/ready`：短 timeout 檢查 PostgreSQL `SELECT 1`、Retrieval readiness、Answer readiness；
  MUST NOT 呼叫真實 generation。
- `GET /metrics`：不加入 OpenAPI。

MUST 實作純 ASGI streaming body-size middleware，不可只信 `Content-Length`。`x-request-id`
最長 128 且必須 printable，否則產生 UUID，並將同值傳給兩個下游。Prometheus
MUST 用 isolated `CollectorRegistry`。

Metrics 至少：HTTP count/latency、orchestration outcome、Retrieval/Answer latency/error、active turns、
capacity rejection、persistence failure。Labels 只可用 method、route template、HTTP status、outcome、
dependency、error category，MUST NOT 用 user/conversation/query/document ID。

Structlog MUST 產生 stdout JSON + UTC timestamp。Log 可含 request ID、route、status、latency、outcome、
dependency，MUST NOT 含問題/回答/evidence、user subject/groups、conversation/message ID、Authorization、
API key、attestation 或 Provider raw response。

## 16. 設定與 production fail closed

`.env.example` 至少包含：

```dotenv
APP_NAME=chat-service
APP_ENV=development
LOG_LEVEL=INFO
CHAT_BIND_ADDRESS=127.0.0.1
CHAT_API_PORT=8120
HELPDESK_NETWORK=helpdesk-rag
CORS_ORIGINS=[]
IDENTITY_MODE=development_headers
ADMIN_ROLE=helpdesk-admin
DATABASE_URL=postgresql+asyncpg://knowledge:knowledge@postgres:5432/knowledge
DATABASE_POOL_SIZE=10
DATABASE_MAX_OVERFLOW=10
RETRIEVAL_URL=http://retrieval-api:8100
RETRIEVAL_API_KEY=local-development-only
CHAT_RETRIEVAL_LIMIT=8
RETRIEVAL_CONNECT_TIMEOUT_SECONDS=3
RETRIEVAL_READ_TIMEOUT_SECONDS=55
RETRIEVAL_TOTAL_TIMEOUT_SECONDS=60
RETRIEVAL_READINESS_TIMEOUT_SECONDS=5
RETRIEVAL_MAX_RESPONSE_BODY_BYTES=5000000
ANSWER_GENERATION_URL=http://answer-generation-api:8110
ANSWER_GENERATION_API_KEY=local-development-only
ANSWER_CONNECT_TIMEOUT_SECONDS=3
ANSWER_READ_TIMEOUT_SECONDS=85
ANSWER_TOTAL_TIMEOUT_SECONDS=90
ANSWER_READINESS_TIMEOUT_SECONDS=5
ANSWER_MAX_RESPONSE_BODY_BYTES=524288
MAX_ACTIVE_TURNS=10
MAX_TURN_QUEUE_SIZE=20
TURN_QUEUE_WAIT_TIMEOUT_SECONDS=120
PROCESSING_STALE_SECONDS=360
PURGE_PREVIEW_TTL_SECONDS=600
API_MAX_REQUEST_BODY_BYTES=65536
```

所有 timeout/size 必須是正數，且 connect/read 不得大於同 dependency 的 total timeout。
Production MUST 拒絕 development identity、空白或 `local-development-only` keys、wildcard CORS 與
未實作 Entra adapter。

Phase 7 另建 `.env.e2e.example`，只供 host 上的 E2E script 使用：

```dotenv
CHAT_E2E_ALLOW_KIS_WRITES=0
CHAT_E2E_KIS_BASE_URL=http://127.0.0.1:8080
CHAT_E2E_KIS_API_KEY=
CHAT_E2E_LOCK_TIMEOUT_SECONDS=60
```

實際 `.env.e2e` MUST 被 Git/Docker ignore，MUST NOT 由 Compose 注入 `chat-api`、`migrate` 或任何
Chat runtime process。`make smoke-e2e` 只有在 `CHAT_E2E_ALLOW_KIS_WRITES=1` 且 key 非空時才可執行
KIS fixture 寫入；否則必須在任何寫入前安全失敗。Key 必須等於本機 KIS 的 `API_INTERNAL_KEY`，
不得輸出到 command line、log、progress 或錯誤訊息。在任何 fixture 寫入前，preflight/E2E 必須用
此 host-only key 呼叫 KIS `GET /api/v1/auth/check`；非 200 時安全失敗，不得只因 readiness 成功就
假設 key 正確。

## 17. Docker 與跨平台

- 以 `answer-generation-service/Dockerfile` 為基準，沿用相同 pinned multi-arch uv/Python digest。
- Non-root `app` user、`EXPOSE 8120`、Docker healthcheck 只打 `/health/live`。
- Dependency layer 先 copy `pyproject.toml`/`uv.lock` 並 `uv sync --frozen --no-dev --no-install-project`；
  copy source 後 `uv sync --frozen --no-dev --no-editable`。
- Compose 只有 `migrate` 擁有 `build:`，`chat-api` 重用 `helpdesk/chat-service:local`，避免相同
  image tag 的 concurrent export 競態。
- `chat-api` MUST 有 `depends_on: migrate: condition: service_completed_successfully`（或等價的 migration
  barrier），不得在 `chat` schema 成功建立前開始服務。
- Compose 不建 PostgreSQL/Qdrant，加入 external `helpdesk-rag` network，alias `chat-api`。
- Port 預設只 bind `127.0.0.1:8120`。
- `init: true`、`restart: unless-stopped`、`read_only: true`、`tmpfs /tmp`、`cap_drop: ALL`、
  `no-new-privileges:true`。Migrate 可依職責使用 `restart: on-failure`。
- MUST NOT 寫死 `platform`；相同 image 需支援 Intel `linux/amd64` 與 Oracle `linux/arm64`。
- `.dockerignore` MUST 排除 `.env`、secrets、tests、scripts、cache、logs、company documents。

## 18. 測試與驗收不變條件

必備測試層：`tests/unit`、`tests/contract`、`tests/security`、`tests/integration`。Integration
MUST 使用 Testcontainers PostgreSQL 16 驗證 migration/repository/transaction，不可以 in-memory
SQLite 假裝 PostgreSQL。

不變的核心測試：

- Query Processor 與 Retrieval/Answer 的 query/language 精確相等。
- `restricted_evidence` 任何情況都不進 Answer；raw payload/content 不進 DB/log/public response。
  純 blocked 結果只允許 machine IDs + citation 的 DB 投影，public API 只顯示 citation。
- Mixed result 只轉送 `llm_context`。
- `insufficient_evidence` 與 `external_generation_blocked` 不呼叫 Answer。
- Body 無法偽造 identity/admin；非 owner 一律 404。
- Public citation 無 machine UUID/path/URL。
- 同 idempotency key 不重複新增 turn/message 或呼叫 Answer。
- DB transaction 不跨越下游 HTTP call。
- Purge preview/digest/busy/cascade/audit 正確。
- Production identity/key/CORS 錯誤時 fail closed。
- Request body limit、strict extra fields、log redaction。
- Architecture import boundaries。

`make smoke` 只做 Docker status、live/ready、conversation CRUD/archive 與 admin purge，不消耗 LLM。
`make smoke-e2e` 要求現有四服務，驗證 Client → Chat → Retrieval → KIS →
Answer/Fake Provider、safe citation、永久歷史、idempotent replay，並清理 fixture。現有 KIS
`smoke-rag-2026` fixture 的 `external_llm_allowed=false`，只能用來測 restricted/no-Answer 路徑，
MUST NOT 當成生成成功 fixture。

Chat E2E script 可作為**測試夾具特例**，透過 KIS 現有 HTTP API 建立固定 synthetic allowed
fixture：`source_type=chat_e2e`、`source_item_id=chat-service/allowed-grounded-v1`、
`external_llm_allowed=true`、ACL 只允許 `all-employees`，內容必須是專用驗證碼且不可含公司
資料。Script MUST 使用 KIS service key，等待 index job，並在 `finally` 透過 KIS DELETE API
soft-delete **同一 source identity 回傳的 document ID** 及等待 cleanup job。固定 source identity 可讓
重複測試復原同一筆，不無界建新文件。Script MUST NOT 刪除或修改其他 document，
Chat runtime 仍 MUST NOT 直接呼叫 KIS。Chat smoke 只可 purge 本次測試回傳 ID 的 synthetic
conversation，不可使用 broad selector。

因 source identity 固定，E2E script MUST 在任何 KIS 寫入前取得 host-local exclusive advisory
file lock，並持有到 `finally` cleanup 完成；等待超過 `CHAT_E2E_LOCK_TIMEOUT_SECONDS` 就安全失敗，
不可與另一個 E2E run 共用 fixture。此鎖只解決本機開發的並行執行，符合 v1 單機範圍。

KIS submit HTTP 202 若 `job_id` 非 null 才 poll 到 `completed`；若為 null，必須確認
`indexing_status=indexed` 才可繼續，其他狀態安全失敗。KIS DELETE HTTP 202 回傳非 null job ID，
必須 poll 到 `completed`；任一 job `failed` 都使 E2E 失敗。不得對 null job ID 發出 GET。

Chat Docker 驗收的外部前置條件 MUST 明列於 README 並由 `make preflight` 驗證，不得弱化
readiness 來掩蓋缺少的依賴。啟動順序固定為：

1. Knowledge Index 的 PostgreSQL/Qdrant/migrate/API/worker 全部 healthy/ready。
2. Retrieval 已使用同一 KIS internal key，且 `/health/ready` 成功。
3. Answer Generation 使用 Fake Provider，載入與 Retrieval attestation private key 配對的 public key，
   且 `/health/ready` 成功；一般 smoke 不得啟用 NVIDIA live provider。
4. 再啟動 Chat `migrate`/`chat-api`。

Ingestion 不是 Chat runtime/smoke 前置。`make preflight` MUST 做 read-only 檢查：external network、
PostgreSQL container health、KIS/Retrieval/Answer readiness 與必要 E2E opt-in 組態；不得建立或刪除資料，
不得顯示 keys。

Make targets MUST 包含：

```text
lock lock-check install lint format typecheck test test-unit test-contract
test-security test-integration config build-native build-amd64 build-arm64
network preflight migrate smoke smoke-e2e up down logs ps
```

階段驗收依 [`chat-service-implementation-plan.md`](./chat-service-implementation-plan.md)。最終至少 MUST
成功：

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

`build-arm64` target MUST 存在。Intel 開發機的跨架構 build 若因模擬過慢，可不在每階段執行，
但不得寫死 amd64，並需在 Oracle ARM 部署時實測。

## 19. 完成定義與交付報告

必備檔案：`.env.example`、`.dockerignore`、`pyproject.toml`、`uv.lock`、Dockerfile、Compose、
Alembic、Makefile、README、smoke/e2e scripts、所有測試。根 `README.md` MUST 從四服務更新為五服務，
加入 port 8120/chat schema，但仍明列 Web Client、Entra、SharePoint、OCR 與 production hardening
未完成。

MUST NOT 留下代替必備功能的 `pass`、`TODO`、fake Entra、無用 code、hard-coded production
secret、Client-supplied URL/provider/prompt 或 Answer auto-retry。

每個 Phase 完成時，Agent MUST 更新 progress 檔，回報：

1. 完成的範圍。
2. 新增/修改檔案。
3. 執行的命令、測試數量與結果。
4. 規格偏離與理由。
5. 阻擋事項。
6. 下一階段是否已具備進入條件。

Agent MUST 在指定 Phase 完成後停止，不可自行跨入下一 Phase。
