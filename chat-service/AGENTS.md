# Chat Service Agent Rules

本目錄是 Help Desk RAG 的 Chat Service。實作前必須完整讀取：

1. `../docs/chat-service-spec.md`
2. `../docs/chat-service-implementation-plan.md`
3. `../docs/chat-service-progress.md`

文件優先順序為：spec（規範性需求）→ implementation plan（Phase scope）→ 本檔（執行與安全）→
progress（狀態與證據）。內容衝突時停止衝突部分並回報，不得自行挑選較方便的版本。

## 工作方式

- 每次只執行 prompt 指定且等於 progress `next_eligible_phase` 的一個 Phase，通過該 Phase 驗收後停止。
- 開始時將該 Phase 在 progress 標為 `in_progress`；只有完整測試證據才可標為
  `completed`。
- 實作前重新讀取本 Phase 相關的 Retrieval/Answer 實際契約，不得只靠規格摘要。
- 規格與實際契約衝突時，停止並記錄檔案、行號、衝突與建議，不要猜。
- 不得開始下一 Phase，也不得藉機重構其他專案。
- 可自主進行任務範圍內的讀檔、編輯、無破壞性測試與 Docker 驗證。外部寫入、刪除使用者
  資料、付費呼叫、擴張範圍或修改現有服務契約前必須停止請求決策。
- 唯一測試資料例外只適用 Phase 7：smoke 可 hard purge 同一 script 本次建立並記錄的 synthetic
  conversation IDs；E2E 可依 spec 第 18 節透過 KIS HTTP 建立/重用固定 synthetic allowed fixture，
  並在 `finally` 只 soft-delete KIS 回傳的精確 document ID。不得使用廣泛 selector、不得觸碰既有
  文件/對話，且必須使用 host-only `.env.e2e` opt-in、非空 KIS key 與 exclusive lock；這些設定
  不得注入 Chat runtime，Chat runtime 也不得呼叫 KIS。

## 絕對邊界

- 不得 import sibling service 的 production Python package；只用 HTTP contract。
- 不得直接連 Qdrant、Knowledge DB table 或 Ingestion table。
- `restricted_evidence` 絕不可進 Answer。其 raw payload/content 絕不可進 DB、log 或 public response；
  純 restricted 結果只可將 machine IDs + citation 投影存入 DB，public API 只顯示 citation。
- Answer request 只能從 Retrieval `llm_context` 作 allowlist projection。
- Retrieval 後不得改動 question/language/evidence/order；attestation 當 opaque token 即時轉送且不存檔。
- 不得對 Answer POST 自動 retry。
- Identity 不得來自 body。Development headers 在 production 必須 fail closed；不得建假 Entra adapter。
- 非 owner 存取對話一律回 404。普通使用者不得 hard delete。
- 不得 log 問題、回答、evidence、user/group/conversation ID、token、key 或 attestation。
- 不得引入 Redis/Kafka/Celery、torch/transformers、前端、SharePoint、OCR 或 multi-turn rewrite。

## 架構與品質

- Python 3.11、FastAPI、Clean Architecture、async PostgreSQL，只使用 `chat` schema。
- Domain 只使用標準庫；Application 不得 import framework/infrastructure；route 不得直接操作 ORM。
- Production modules 以繁體中文 docstring 說明架構與不直觀語法，不寫冗餘逐行註解。
- 必須提交 `uv.lock`；所有 install/test/build 使用 `--frozen`；不用 `pip install .`。
- 測試不可只測 happy path；不可用 SQLite 代替 PostgreSQL integration test。
- 不可用 `pass`、TODO、dead code 或降低既有測試要求來宣稱完成。

## 完成回報

階段結束前必須依 handoff 模板更新 `../docs/chat-service-progress.md`，列出完成範圍、逐檔 manifest、
實際執行的命令與 exit code、pass/fail/skip 數量、偏離、阻擋與 gate。Phase 3、5、7 完成後只能
將 review 設為 `pending`，必須由另一個 review task 通過後才可前進。
