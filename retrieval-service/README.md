# Retrieval Service

> 學習程式碼時，建議搭配
> [`Python／Clean Architecture 學習指南`](../docs/python-clean-architecture-learning-guide.md)。

Stateless, policy-aware evidence retrieval for the Help Desk RAG system. The service accepts
a question and a trusted employee principal, calls Knowledge Index Service, applies a bounded
evidence-selection policy, and returns citations that are safe for the next RAG stage.

It does **not** embed queries, connect directly to Qdrant or PostgreSQL, parse documents, store
conversations, or call an LLM.

## Architecture

```text
presentation (FastAPI) -> application use case -> domain
                                 |
                                 v
                    KnowledgeSearchPort / IdentityProviderPort
                                 ^
                                 |
                     infrastructure HTTP / identity adapters
```

Runtime data flow:

```text
Chat / Query Processor
  -> Retrieval Service
  -> Knowledge Index Service /api/v1/search
  -> PostgreSQL canonical metadata + Qdrant vector index
  -> Retrieval Service evidence policy
  -> llm_context + restricted_evidence + citations + context attestation
```

Ingestion Service is a separate write path and is not a runtime dependency of this service.

## 目前功能，用白話說

Retrieval 負責「為這個使用者挑出可用的證據」，不負責產生答案。Chat 傳入經基本正規化的
問題，本服務請 KIS 進行向量搜尋，再依相似度排序、去重、每份文件上限、token budget 與
外送政策挑選段落。選好的結果回到 Chat，由 Chat 決定是否呼叫 Answer Generation。

- 基本 Query Processor 已在 [`Chat`](../chat-service/src/chat_service/application/query_processor.py)
  實作；只做型別／長度檢查、換行／Unicode／頭尾空白正規化，沒有意圖分類或查詢改寫。
- 本服務尚未實作 keyword/vector hybrid search、語意 reranker、Agent、多次檢索規劃或全文件統計。
- 預設 `limit=8`、`CANDIDATE_MULTIPLIER=3`、`MAX_CHUNKS_PER_DOCUMENT=3`、
  `MAX_CONTEXT_TOKENS=3000`，依 request／部署設定可調整。只把每份文件上限提高到 30，
  不會自動提高其他限制，也不保證能取得完整系所清單等統計依據。
- `evidence_found` 表示存在 allowed evidence，不表示內容一定足夠回答問題。

## Security decisions

- End-user identity is never accepted from the JSON body.
- `x-dev-user-id` and `x-dev-group-ids` are accepted only by the development identity adapter.
- Production refuses to start with the development identity adapter.
- Employee retrieval always requests `published` documents from Knowledge Index Service.
- A chunk with `external_llm_allowed=false` is placed only in `restricted_evidence`; it can
  never enter `llm_context`.
- A result containing only restricted evidence has status `external_generation_blocked`.
- Filesystem `source_path` and unverified `source_url` are deliberately absent from the public
  domain and API response. Citations expose only title, section and page number.
- Query text, chunk content, user ID and physical source path are not written to logs.
- Allowed `llm_context` is sealed with a short-lived Ed25519 attestation. Answer Generation
  Service detects a modified question, evidence, citation or governance field before any
  external LLM call. Restricted evidence is never included in that signature.
- `score` is a finite, model-specific diagnostic field. Answer Generation accepts it for schema
  compatibility but deliberately excludes it from the attestation digest and LLM prompt; it must
  never be interpreted as answer confidence.

The Entra JWT adapter is reserved but intentionally not implemented yet. Until a verified
production identity adapter exists, this service is development-only and fails closed in
production mode.

## API

### `POST /api/v1/retrieve`

Development request:

```bash
curl --request POST http://127.0.0.1:8100/api/v1/retrieve \
  --header 'content-type: application/json' \
  --header 'x-internal-api-key: local-development-only' \
  --header 'x-dev-user-id: employee-001' \
  --header 'x-dev-group-ids: all-employees,helpdesk' \
  --data '{
    "query": "員工每週可以遠端工作幾天？",
    "limit": 8,
    "language": "zh-TW"
  }'
```

The example key is the local default. If `API_INTERNAL_KEY` is changed, send the configured
Retrieval Service key instead:

```text
x-internal-api-key: <retrieval-service-key>
```

Example policy response:

```json
{
  "status": "external_generation_blocked",
  "llm_context": [],
  "restricted_evidence": [
    {
      "rank": 1,
      "chunk_id": "00000000-0000-0000-0000-000000000001",
      "document_id": "00000000-0000-0000-0000-000000000002",
      "version_id": "00000000-0000-0000-0000-000000000003",
      "score": 0.89,
      "content": "員工每週可以遠端工作兩天。",
      "document_type": "policy",
      "token_count": 15,
      "citation": {
        "title": "遠端工作規章",
        "section_path": "遠端工作",
        "page_number": 3
      },
      "governance": {
        "confidentiality": "internal",
        "language": "zh-TW",
        "external_llm_allowed": false
      }
    }
  ],
  "external_generation_blocked": true,
  "context_attestation": null,
  "retrieval": {
    "candidate_count": 1,
    "selected_count": 1,
    "model_id": "intfloat/multilingual-e5-small",
    "model_revision": "..."
  }
}
```

Result status meanings:

- `evidence_found`: at least one selected chunk is permitted for an external LLM.
- `external_generation_blocked`: evidence exists, but every selected chunk is forbidden from
  external LLM processing. The internal Retrieval response contains restricted evidence; the
  current Chat/Web Chat only expose safe citations, never the restricted excerpt, and do not call Answer.
- `insufficient_evidence`: no candidate passed the configured evidence policy.

An empty or insufficient result is HTTP 200. Dependency failures use 502/503/504 so an upper
layer cannot mistake an unavailable search engine for "the company has no rule".

Downstream Chat orchestration must not forward this complete response to Answer Generation. It
must preserve the exact request `query`/`language`, map them to Answer `question`/`language`, use
only `llm_context` as Answer `evidence`, and forward `context_attestation`. It must never copy
`restricted_evidence`. Each `llm_context` item, including its diagnostic `score`, is directly
compatible with Answer's evidence schema.

Other endpoints:

```text
GET /health/live
GET /health/ready
GET /metrics
GET /docs
```

## Local Docker

Knowledge Index Service must already be running on the external `helpdesk-rag` network and its
API service must have the `knowledge-index-api` network alias.

```bash
cp -n .env.example .env
make keys-dev
make network
docker compose up -d --build
make smoke
```

After `knowledge-index-service make smoke` has seeded the known ACL-protected fixture, run the
stronger end-to-end assertion:

```bash
make smoke-e2e
```

The read-only smoke test checks:

- liveness and Knowledge Index readiness;
- Knowledge Index service-key authentication through `/api/v1/auth/check`;
- missing end-user identity is rejected;
- retrieval can call the real Knowledge Index Service;
- `source_path` is absent;
- restricted evidence never enters `llm_context`;
- allowed context is never returned without an attestation;
- Prometheus metrics are available.

## Configuration

Important settings are documented in `.env.example`. The most relevant are:

```text
IDENTITY_MODE=development_headers
KNOWLEDGE_INDEX_URL=http://knowledge-index-api:8080
KNOWLEDGE_INDEX_API_KEY=<must match Knowledge Index API_INTERNAL_KEY>
DEFAULT_RETRIEVAL_LIMIT=8
MAX_RETRIEVAL_LIMIT=20
MAX_CANDIDATE_LIMIT=50
CANDIDATE_MULTIPLIER=3
MAX_CONTEXT_TOKENS=3000
MAX_CHUNKS_PER_DOCUMENT=3
```

`SCORE_THRESHOLD` is intentionally unset. A cosine score is model-specific and is not a
confidence percentage. Set it only after evaluating representative company questions against
the pinned embedding model revision.

The published port binds to `127.0.0.1` by default so the development identity headers are not
exposed to the local network. A production reverse proxy may override `RETRIEVAL_BIND_ADDRESS`
only after a verified identity adapter and real service keys are configured.

## Development checks

```bash
make lint
make typecheck
make test-unit
make test-contract
make test
make config
```

The service uses Python 3.11, FastAPI, Pydantic Settings, httpx, structlog, Prometheus, Ruff,
strict mypy and pytest, following the shared backend engineering conventions.

## Production follow-up

Before Oracle Cloud production deployment:

1. Implement and test a verified Entra ID/M365 or approved company identity adapter.
2. Configure independent Retrieval and Knowledge Index service keys.
3. Put the API behind TLS and keep it off the public internet.
4. Calibrate the evidence threshold with 20-50 representative questions.
5. Load-test 30 concurrent users and measure p50/p95 latency on ARM64 CPU-only hardware.
6. Keep PostgreSQL and Qdrant private and backed up; Retrieval Service itself has no persistent
   volume.
