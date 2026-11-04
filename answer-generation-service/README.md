# Answer Generation Service

Help Desk RAG 的受限回答服務。它只接受 Retrieval Service 已核准且簽章的 `llm_context`，
通過本機敏感資料政策後，使用固定 Prompt 呼叫設定好的 LLM Provider，最後驗證結構化答案
與 citation。

## 服務邊界

它不搜尋文件、不連接 PostgreSQL/Qdrant、不保存對話，也不接受任意 Prompt、Provider URL、
Model 或 tools。Ingestion 與 Knowledge Index 不是它的 runtime dependency。

```text
Web Chat -> Chat -> Retrieval -> llm_context + Ed25519 attestation -> Chat
Chat -> Answer Generation -> Fake / approved Groq / approved NVIDIA Provider
```

Answer API 絕不能接收 `restricted_evidence`。Citation 只公開文件名稱、章節與頁碼；機器識別
保留 chunk/document/version UUID，但不公開實體路徑或 source URL。

## 一次回答怎麼產生

1. Chat 將本次問題、語言、allowed evidence 與 opaque attestation 傳入。
2. 驗證簽章、文件外送許可與 Provider 核准，依 `policy.yaml` 啟用狀態掃描實際內容。
3. 組成固定版本的 system prompt，以及包含問題和 evidence 的 user JSON；不讀歷史對話。
4. Provider 回傳 `status`、`answer_parts` 與 `citation_ids`，服務驗證結構與引用對應。
5. 以可信 evidence 重建引用資料，將結果交回 Chat 保存，再由 Web Chat 顯示。

模型只看到選中的段落，不會自動閱讀整份 PDF。Prompt 不新增 source path／source URL、
machine UUID、score 或 attestation 欄位；但 evidence 內文本身仍可能有 URL／敏感字串，
不代表內文已被自動遮罩或去識別。

`answered` 代表輸出格式與引用 ID 有效，不代表程式已證明內容正確。
`unable_to_answer` 是允許的業務結果：即使檢索到相似段落，仍可能缺乏完整清單或明確日期。
Fake Provider 的固定回答僅測試串接，真實 Provider 才依 Prompt 生成回答。

## Local Docker

```bash
cp -n .env.example .env
make keys-dev
make network
docker compose up -d --build
make smoke
```

Swagger UI：`http://127.0.0.1:8110/docs`

`make smoke` 使用 Fake Provider，不連外、不消耗 API 額度。NVIDIA live 測試必須明確 opt-in，
並且只能使用 synthetic/public/non-confidential fixture。

開發階段若要切換 NVIDIA Trial，先在 `.env` 設定：

```env
LLM_PROVIDER=nvidia_hosted
LLM_PROVIDER_USAGE_MODE=trial
EXTERNAL_LLM_APPROVED=true
LLM_MODEL=<目前帳號實際可用的 model ID>
NVIDIA_API_KEY=<development trial key>
```

重新建立 container 後，以 `RUN_LIVE_NVIDIA_SMOKE=1 make smoke-live` 執行。Smoke script 會拒絕
Provider mode 與命令不一致的情況，避免一般 `make smoke` 意外消耗外部額度。

若要使用 Groq，先在 Groq Console 建立 API key，再於本機 `.env` 設定（不要把 key commit
進 Git）：

```env
LLM_PROVIDER=groq_hosted
LLM_PROVIDER_USAGE_MODE=trial
EXTERNAL_LLM_APPROVED=true
LLM_MODEL=openai/gpt-oss-20b
GROQ_BASE_URL=https://api.groq.com/openai/v1
GROQ_API_KEY=<你的 Groq API key>
GROQ_ALLOWED_HOSTS=["api.groq.com"]
```

`LLM_MODEL` 必須是你的 Groq 帳號目前可用的 model ID；上例只是可替換的開發預設。重新建立
container 後，Web Chat → Chat Service → Answer Generation 的請求就會改由 Groq 生成回答：

```bash
docker compose up -d --build --force-recreate answer-generation-api
curl -fsS http://127.0.0.1:8110/health/ready
```

若要明確執行會消耗 Groq quota 的 synthetic live smoke，使用：

```bash
RUN_LIVE_GROQ_SMOKE=1 make smoke-groq-live
```

若 `policy.yaml` 的 `enabled: false`，live smoke 必須明確跳過敏感內容探針：

```bash
EXPECT_OUTBOUND_POLICY_BLOCK=0 RUN_LIVE_GROQ_SMOKE=1 make smoke-groq-live
```

這只會跳過 smoke 的 synthetic password 測試，不會改動服務政策；也能避免 scanner 關閉時把
測試字串送到外部 Provider。

一般 `make smoke` 仍只允許 Fake Provider，因此不會意外呼叫 Groq。

## API

```text
POST /api/v1/answers
GET  /health/live
GET  /health/ready
GET  /metrics
```

`POST /api/v1/answers` 需要專屬 `x-internal-api-key`，request 必須包含原始問題、Retrieval 的
allowed evidence，以及與兩者完全相符的短效 context attestation。

平常由已實作的 Chat Service 建立新的 allowlist request，不應手動組合：

- `question` 與 `language` 必須使用當初實際送進 Retrieval 的精確值，不能在檢索後 trim、
  rewrite 或翻譯，否則 attestation 驗證會失敗。
- `evidence` 只能取 Retrieval response 的 `llm_context`；`restricted_evidence` 絕不能轉送。
- `context_attestation` 使用 Retrieval 回傳的同名欄位。
- 不得把整份 Retrieval response 當成 Answer request。

單筆 `llm_context` evidence 可以保留 Retrieval 回傳的完整欄位，包括有限數值 `score`。
`score` 只用來維持跨服務 schema 相容與診斷；Answer 會在 Presentation → Domain 投影時捨棄它，
不會把它加入 attestation canonical digest、Prompt、回答決策或 response。

```json
{
  "question": "員工每週可以遠端工作幾天？",
  "language": "zh-TW",
  "evidence": [
    {
      "rank": 1,
      "chunk_id": "00000000-0000-0000-0000-000000000001",
      "document_id": "00000000-0000-0000-0000-000000000002",
      "version_id": "00000000-0000-0000-0000-000000000003",
      "score": 0.91,
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
        "external_llm_allowed": true
      }
    }
  ],
  "context_attestation": "<Retrieval 回傳的 compact JWS>"
}
```

正常業務結果：

- `answered`
- `unable_to_answer`

錯誤語意：401 service key、403 attestation/policy、413 body、422 schema/domain、429 quota、
502 Provider contract、503 Provider unavailable、504 timeout。

## Provider adapters

Application 只依賴 `LLMProviderPort`。目前 adapters：

- `FakeLLMProvider`：本機/CI。
- `NvidiaHostedNimProvider`：非串流、202 polling、有界 retry、總 deadline、response-size limit。
- `GroqHostedProvider`：Groq Chat Completions、JSON Object Mode、非串流、有界 retry、總
  deadline、response-size limit。

未來增加其他公司時新增 Infrastructure Adapter，不修改 Domain/Use Case。第一版禁止跨公司
自動 fallback，因為每一家 Provider 都必須分別通過公司法務、資安及資料外送核准。

Provider adapter 的有界 HTTP retry 與 Chat → Answer 的契約不同：Chat 不會自動 retry
Answer POST。`/health/ready` 僅驗證本機設定與必要元件，不向 Provider 發出付費／消耗 quota
請求，因此 ready 不保證外部金鑰、模型或剩餘額度有效。

NVIDIA Developer Program hosted endpoint 只適合原型與測試；Trial 資料條款也禁止機密、
敏感或個人資料。正式 Help Desk 不得用 Trial 設定服務真實員工。

## Outbound Policy

第一版使用本機規則掃描 question、content、title、section。命中憑證、個資、金融、HR、
系統安全或公司規則時整題阻擋，不自動遮罩。Scanner/YAML 失敗也 fail closed。

所有內容規則都集中在
`src/answer_generation_service/infrastructure/outbound_policy/policy.yaml`。`rules` 支援
`regex`、`all_regex`、`contains`、`all_contains`；台灣身分證、Luhn 卡號與 private IP 則由規則
明確指定通用 validator。新增或停用內容規則不需要修改 Python。

第二層內容掃描的全域開關是 YAML root 的：

```yaml
enabled: true
```

改成 `false` 會停用本檔全部內容規則。Compose 將 host 上的 policy 以唯讀方式掛入 container，
修改後執行下列命令即可重新載入，不需要 rebuild：

```bash
docker compose restart answer-generation-api
```

這個開關不會停用 Retrieval 的 `external_llm_allowed`、ACL、context attestation 或外部 Provider
核准。停用內容掃描會讓原本被 Email、憑證、個資等規則攔截的 allowed evidence 進入 Provider；
只應用於隔離的本機 Demo，正式環境應維持 `enabled: true` 並由資安負責人管控 policy 檔。

這是基礎規則引擎，不是完整企業 DLP，不能宣稱能辨識所有敏感資訊。正式上線仍需公司
資料分類、法務、資安政策及核准 Provider。

## Development checks

```bash
make lint
make typecheck
make test-unit
make test-contract
make test-security
make test
make config
make smoke
```

`make test-contract` 包含不連接 KIS、不呼叫外部 LLM且不寫資料庫的 synthetic
Retrieval → Answer in-process HTTP 測試。它使用暫存 Ed25519 key pair，同時驗證 allowed evidence
能完成回答、restricted evidence 不進入 Answer request/Prompt，以及 Retrieval `score` contract。

若兩個服務已啟動，且知識庫已存在一份專供測試的 public/allowed fixture，可額外執行真正的
HTTP E2E。這個腳本只查詢既有資料，不會新增、修改或刪除文件，但會呼叫 Answer；若設定
真實 Provider，會外送通過政策的內容並消耗 API quota，不等於「不呼叫外部模型」：

```bash
E2E_ALLOWED_QUERY='synthetic-allowed-fixture-code' \
  python3 scripts/retrieval_answer_e2e.py
```

KIS 既有的 `smoke-rag-2026` fixture 是 restricted，刻意不能用於這項成功路徑測試。不要為了
執行腳本而把真實公司文件改成允許外送。
