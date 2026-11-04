# Help Desk RAG U1：跨服務契約與安全設計草案

日期：2026-10-06。契約草案版本：`rag-upgrade-contracts-v0.2.1`。
狀態：`implementation_status=in_progress`、`review_status=not_started`、`approval=pending`。
進度唯一來源：[升級計劃 v1](../升級計劃v1.md)，目前 `next_eligible_phase=U1`。

**這是設計提案，不是已實作 API，也不是 production 修改授權。**
下列新增端點、欄位、數值與候選選型，全部須經 D02–D07 核准及 U1 獨立 review 才能凍結。
尚未選定的部分明列在第 13 節；AI 不得自行補值、安裝、下載或宣布 U1 完成。
第 15–19 節將提案寫成精確欄位與可重現資料；它們仍待核准，不是已部署的 v2 功能。
既有安全限制已生效，不因本草案而暫停；只允許本輪更新工作區文件。

## 1. 給使用者看的摘要

現在的問題是「找到相似段落」不等於「取得能回答整個問題的資料」。本次設計分成三件事：

1. Chat 先判斷問題是在問日期、清單、總數、比較或流程；年度／對象不清楚就追問。
2. Retrieval 依問題需要使用向量與關鍵字搜尋，重新排序，必要時讀完整章節／表格，而非只拿前三段。
3. 只有完整、適用版本一致、具備使用與外送許可的資料才能產生答案；總數由程式算，不交給模型猜。

保留現有五個後端，不合併 Chat／Retrieval，不新增 Agent 或 MCP 服務。
保留 v1 與現在的 Web Demo；新版先用 v2 API 驗收，不把新追問結果塞進舊畫面契約。
U0 現況盤點完成但回答品質未量測；這個事實不會因進入設計而變成 PASS。

## 2. 現有契約與不能直接更動的原因

已唯讀核對以下 production 檔案；下列欄位是現況，不是新增功能：

| 服務／檔案 | 現況 | 升級限制 |
|---|---|---|
| [KIS schemas](../knowledge-index-service/src/knowledge_index_service/presentation/schemas.py) | v1 search：query、limit、score_threshold、filters；chunk 無 typed table/row/cell | 不能用 custom_metadata 假裝已支援完整枚舉／結構 lineage |
| [Retrieval schemas](../retrieval-service/src/retrieval_service/presentation/schemas.py) | v1 retrieve：query、limit、language；status、llm_context、restricted_evidence、attestation、diagnostics | extra=forbid；v1 restricted evidence 有原文，只允許目前安全投影方式 |
| [Answer schemas](../answer-generation-service/src/answer_generation_service/presentation/schemas.py) | v1 answers：question、language、evidence（1–20）、context_attestation | 外送旗標、簽章及 citation 驗證不能因新增 planner 而繞過 |
| [Chat schemas](../chat-service/src/chat_service/presentation/schemas.py) | v1 turn：question、language；五種 outcome，沒有 clarification | 舊 client 不會自動理解新狀態；不擴充原 v1 enum 假裝相容 |
| [Retrieval canonical](../retrieval-service/src/retrieval_service/infrastructure/attestation/canonical.py)／[Answer canonical](../answer-generation-service/src/answer_generation_service/infrastructure/attestation/canonical.py) | RFC 8785 canonical JSON、SHA-256、Ed25519；綁定 question/language 與有序 evidence | 改問題、內容、順序、版本或數量後，必須重新由 Retrieval 組 context 並簽章 |
| [Chat v1 spec](./chat-service-spec.md)／[AGENTS](../chat-service/AGENTS.md) | 單輪、基本正規化；不允許 multi-turn rewrite、Chat 直連 Provider/KIS | 必須先核准第 11 節差異；不能把本草案當成解除全部規則 |

KIS v1 error envelope 為 `code/message/request_id`；其他服務有 `error/message/request_id/details`。
adapter 需按實際版本解碼，不能假設目前所有服務同一形狀。
舊 v1 的限制、錯誤映射、簽章與 tests 全部保留。

## 3. 責任與資料所有權

| Owner | 接收 | 產出／責任 | 不做 |
|---|---|---|---|
| Chat | 同 owner 的問題／有限 user history | task plan、追問、預算、冪等、對話 transaction、public 結果 | 直接 Provider／KIS／Knowledge DB／Qdrant；自行簽章或計算文件總數 |
| Answer planning adapter | 已通過外送掃描的問題與 user history | 不可信的 typed plan proposal | 讀文件、指定 identity/ACL、修改 policy、產生公司答案、任意工具呼叫 |
| Retrieval | 驗證過的 task plan＋可信 Principal | KIS 查詢、融合／CPU rerank、scope 解析、coverage、typed facts、最終簽章 | 直接 DB／Qdrant；相信模型說「已完整」或「有權限」 |
| KIS | 可信內部 client、搜尋／scope／分頁要求 | 每次 canonical ACL/lifecycle/version 檢查、搜尋、精確取文、typed 結構 | 從 client 的宣稱推論授權；簽發可外送 context |
| Ingestion | 來源＋metadata | parser 結構／品質標记、chunk、版本化 lineage；以 HTTP 提交 KIS | embedding／Qdrant；猜缺失 metadata／缺页／欄列關係 |
| Answer generation | signed allowed evidence＋signed facts | Prompt、Provider、grounded answer、引用／數值驗證 | 重寫已簽問題、補搜、計算未證明完整的總數 |

維持 Clean Architecture：Domain 標準庫；Application 依 DTO／port，不依 FastAPI、Pydantic、SQLAlchemy、HTTPX、模型套件。
HTTP schemas、ORM、Domain entity 分離；不 import sibling package。模型只在 Infrastructure 載入。

## 4. 版本、身份與共同 wire 規則

### 4.1 平行 v2，不改 v1

以下端點名稱為 **D05 提案**；目前不得呼叫它們來宣稱驗收：

| 服務 | v2 提案端點 | phase |
|---|---|---|
| Chat | POST `/api/v2/conversations`；POST `/api/v2/conversations/{id}/turns` | U4 |
| Chat | GET conversation／turn／messages 的同名 v2 resource paths | U4 |
| Answer | POST `/api/v2/plans`（只有 planning 用途） | U4 |
| KIS | POST `/api/v2/search`；POST `/api/v2/scope-pages` | U2 |
| KIS | POST `/api/v2/documents`（提交結構）；POST `/api/v2/chunks/revalidate` | U2，producer U5 |
| Retrieval | POST `/api/v2/retrieve`（一輪計畫，可含多查詢／scope 擴展） | U3 |
| Answer | POST `/api/v2/answers` | U5 |

v2 conversation 具有固定 contract version；v1 routes 不讀／修改 v2 conversation，反之亦然，owner 不符或版本不符皆 404。
避免 v2 clarification／message 型別意外出現在舊 Web Demo 的 v1 list/history。
v2 需要的 conversation/version/schema migration 留給對應 phase，本次不建 migration。
現有 v1 API key/header 命名仍照實際服務，沒有新全域共用 key，也不使用 Groq key 當內部 key。

### 4.2 身份與 transport

- Browser／public body 不接受 issuer、subject、group、ACL、外送許可、Provider 或任意 URL。
- Principal 由 Chat identity adapter 取得；Retrieval 只接受可信 gateway headers；KIS filters 只能由可信 Retrieval adapter 建構，不轉送使用者自由 JSON。
- 每一輪、每一次取文與每頁重新檢查 Principal＋published/current indexed version＋有效期限＋ACL；Qdrant payload 不是真實來源。
- 外部 JSON 全部 `extra=forbid`、finite numbers、strict integer/boolean；不接受 `1` 當 true，拒絕 NaN／infinity。
- UUID 用標準小寫格式；欄位省略／null 語意明定，不讓不同 client 推論。固定 HTTP/HTTPS base URL，禁止 redirects、credentials、query、fragment、控制字元與非法 port。
- lifespan 注入共用 async client；先驗 Content-Type、Content-Length 及實際 bytes，再解碼；JSON/charset 或超限失敗不 retry。
- Internal `request_id` 沿用既有可列印128字元限制；不加入 query/evidence 的 URL query string。
- v2 error 提案統一 `error/message/request_id/details`；details 只用 safe code／有界數值，不含 raw upstream body、path、URL 或來源內容。

## 5. Chat：問題、歷史、追問與冪等

### 5.1 三種問題不得混用

| 名稱 | 用途 | 存放／限制 |
|---|---|---|
| original_question | 使用者本次原文 | 僅 Chat user message，照現有對話治理；不可 log |
| answer_question | 正規化後、只含使用者明確條件的完整回答問題 | 送 Retrieval／Answer 並綁簽章；取得 final context 後不更改 |
| search_queries | 搜尋的有限改寫／子問題 | 最多3條／輪；僅 request 記憶體，不作冪等 fingerprint，不當答案或已確認事實 |

v2 SubmitTurnRequest 提案：`question: StrictStr[1..20000]`、`language: Str[1..20] | null`、
`reply_to_turn_id: UUID | null`。其他全部拒絕。HTTP Idempotency-Key 繼續為 UUID。
reply_to 只允許同 conversation、同 owner、已完成 clarification 的 turn；不存在／跨 owner 404，過期承接409。

### 5.2 有限 history 提案（D05）

最多最近4個已完成 turn 的 **user questions**，按 ordinal 升序；不取其他 owner／處理中／失敗 turns。
第一版不把 assistant answer、restricted 引用標題或 evidence 當外部 planner history，避免資料權限撤銷後沿歷史外送。
使用者已明確提供的年度／單位／對象可承接；「第二個」但只在上一個 assistant 清單出現則追問，不猜指代。
history 總量按第18節的保守UTF-8 byte上界<=2000（另有16000bytes transport護欄，不是可用配額）；不宣稱使用已取得的Groq tokenizer。超限只移除最舊的完整turn，不截斷一個問題改變語意。
模型推測欄位不標成 user_confirmed。遇缺條件，public clarification 只問缺哪個條件，不帶 restricted／未授權文件資訊。

### 5.3 Snapshot 與 fingerprint

- accepted transaction 鎖 conversation、驗 owner／reply_to、取得 `history_as_of_ordinal`、配置 ordinal；保存 original user message、v2 contract version、snapshot source IDs/hash及明確承接關係後 commit。
- 不在 transaction 內等 capacity、呼叫 planner／Retrieval／Answer。後續 history 依已凍結 ordinal 與同 owner讀取，其他新 turn 不改 snapshot。
- 提案同一 v2 conversation 僅允許一個 processing turn；不同 conversation 可並行。若已有 processing turn，409 `conversation_busy`，不偷偷附到對方 turn。
- fingerprint 用 normalized **本次輸入** question、language、reply_to_turn_id、contract version 的 canonical JSON SHA-256；不納入模型生成的 plan／rewrite／動態 history。snapshot 是首次 accepted 保存的結果，不在 replay 時重取最新。
  v2精確物件為`{contract_version:"chat-v2",question,language,reply_to_turn_id}`，可null欄位保留；normalize只將CRLF/CR轉LF及trim外圍空白，不casefold／Unicode正規化。hash為SHA256(`helpdesk-rag/chat-idempotency/v2\0`＋JCS)的hex，v1舊fingerprint不改。
- 同 key同 payload處理中202；terminal replay 原結果，不再呼叫 planner、Retrieval或Answer；不同payload409。
- clarification 是 completed 業務結果；下一個回答用新 Idempotency-Key 與 reply_to，不能改寫上一個 turn。
- 保存結果使用新 transaction、DB time、row_version／last_activity 更新；capacity lease 所有路徑釋放；429 rejection／failure 可重播。stale recovery 保持 fail-unknown、不重新產生答案。
- public v2 既有五種 outcome＋`clarification_required`；clarification 欄位為有界問題（<=1000字）與缺條件代碼（<=4），不是偽裝成 answered。無模型答案或文件原文。
- Chat 可存有界任務／safe reason code／user-confirmed條件／計數診斷，但不存整份下游 request、search query、evidence／facts原文、path/URL、key、attestation。新增 persistence 與 purge cascade 規則要在 U4 明確 migration/tests，不能暗加 generic JSON dump。

## 6. Planning 契約與外送防護

PlanRequest 提案只包含 `answer_question`、`language`、最多4條 user history、`planning_schema_version`、`budget`。
不接受 evidence、文件 citation、ACL、user ID、attestation、URL、任意 tools 或 system prompt。
planning_schema_version 固定 `query-plan-v1`，錯誤版本422；Prompt 為服務內固定版本。

PlanProposal 提案：

- `task_type`: fact/list/count/compare/procedure/followup。
- followup 必須另有 `resolved_task_type`，且為前五種；非 followup 必須 null。
- `answer_question: Str[1..20000]`、`search_queries: list[Str[1..2000]][0..3]`；長度0只允許needs_clarification=true，不同時要求1..3又要求空array。
- `required_conditions`: 最多4個第15節Requirement；`missing_conditions`: 最多4個同registry的field enum。值不可由模型猜成已確認，不接受模型自由創建SQL filter。registry提案仍待D07核准。
- `scope_hints`: 最多3個自然語言標籤（<=256），不是文件 UUID／URL，也不能被當作 canonical scope。
- `needs_clarification: StrictBool`；true 時 search_queries 為空、缺條件非空；false 時1–3 queries、必要條件齊備。
- 不回傳答案／confidence／“完整=true”／授權或外送許可。Chat 重新驗 typed schema、條件來源及規則，才形成 trusted TaskPlan。

外部 planning 前，Answer 掃描問題＋每條 history，不只掃最終 evidence。
`external_llm_approved`、usage mode、provider config與內容 policy 必須全部允許；v2外部planning啟用時不允許停用內容 scanner。
任一阻擋不呼叫 Provider，回可投影的 safe policy code；公司 history 外送不從 U0 synthetic opt-in 推論授權。
Prompt injection／模型非法欄位按 contract failure 處理；不能讓模型指定多花預算或繞過 ACL。
規劃模型建議沿用 Groq `openai/gpt-oss-20b`，可經 port 換 Provider，但不自動切付費或改模型。
模型 JSON format 只幫助格式，不證明問題理解正確；一定本地 strict validation。

## 7. KIS 搜尋／取文／結構提案

### 7.1 SearchRequestV2

`schema_version=knowledge-search-v2`、`mode=vector|keyword`、`query[1..2000]`、`limit[1..30]`、
可信 adapter 建立的 `filters`（identity／published／language／最多3個 document+version scope）、`budget`。
vector query 含 E5 prefix／special tokens 必須<=512；超限422，不偷偷截斷或送入模型造成 indexing error。
keyword 模式使用相同版本的索引／查詢 tokenizer；不可將向量 threshold 套在 keyword score。
Response：有界items（canonical UUID／version／ordinal、content、title/type、section/page、token count、governance、結構引用），
`score_kind=cosine|ts_rank_cd`、finite `score`、model或tokenizer revision與`budget_used`。
v2 search 對 Retrieval 不需回傳 source path/URL；它們保留在 KIS metadata，不进入生成資料流。

### 7.2 ScopePagesRequestV2／ChunksRevalidateRequestV2

精確取文只接受固定 document_id＋version_id 與 typed selector：section/block/table/ordinal-window；無任意 SQL／path。
先從已授權 search candidate 選 scope，再取文；LLM scope hint不能直接變文件ID。
每頁<=100 blocks或rows、每次response<=1MiB；最多20頁／輪，總2000 rows／輪與2MiB取文原文。
cursor 是 opaque、具 integrity protection，綁 Principal fingerprint、document/version、selector、ordinal、schema/limit、expiry；不是可手改的 offset。
KIS query 用穩定唯一排序（ordinal＋ID），cursor與scope不符／版本變動409，未授權或不存在404，不回其他文件資訊。
每頁重驗當前ACL／published／current version／有效期限；cursor不沿用前頁許可，不混新旧版本。
Response 包含 `next_cursor|null`、authorized scope manifest、`complete/truncated`、有界gap codes、typed lineage；complete 不表示外送許可。
被拒絕資料不回其標題、row count或“被隱藏X筆”；不能把僅可見部分標成全體完整。
最終選定前 revalidate 最多30個 evidence chunk IDs，另以最多3個完整 scope proof 重驗表格／章節；不能把30個ID上限套在完整2000列的枚舉上。
scope proof 綁定固定 selector、version、structure/content manifest與governance revision；KIS重新從 canonical 來源計算並驗授權，不能只驗 cursor 或相信 Retrieval 自報完整。
任何變動停止該context或重新取得（仍扣預算），不默默保留旧signed內容。

### 7.3 Typed structure／lineage

DocumentSubmissionV2 在原嚴格metadata/ACL規則外，新增獨立typed structure，不用自由custom_metadata塞列。
提案階層：document version → section/block → table → logical row → cells → source anchors。
block類型 paragraph/heading/list_item/table；row/column ordinal、header-role、column key、cell text、
page/section、source block/row IDs、parser revision、parse_quality、continuation relation全部有明確欄位。
source ID 在同版本／parser revision 下穩定；同列重印與跨頁續接保留來源，logical row只算一次。
行列或合併格不確定就 `incomplete/ambiguous`，不自動修成完整；非文字PDF依既有規則拒絕／OCR留下一版。
上限提案：每文件10000 chunks/blocks、100 tables、每table2000 logical rows、每row100 cells、cell<=4000字；超限停該文件，不丟剩餘資料卻標完整。
结构、parser revision及内容都進新版submission fingerprint；不能因檔案bytes相同就跳過结构重建。
U2 可儲存並驗證synthetic typed payload；U5才接真parser與重新匯入。旧文結構標為unknown，不自动批次重建100GB或編造table rows。
精確wire／儲存約束提案見第16節；D05/D07核准之前不能自行migration。

## 8. Retrieval：融合、coverage、facts與簽章

RetrieveRequestV2 提案：`schema_version=retrieval-plan-v2`、`answer_question`、`language`、trusted TaskPlan、
`round_index[1..2]`、`prior_scope_state|null`、`budget`。不接受rawhistory／answer／Provider／ACL override。
prior scope僅metadata/version IDs，不保存或信任舊內容；每輪經KIS canonical重驗。

每輪流程固定：vector/keyword各查 → 依canonical chunk/version ID去重 → RRF融合 → 本地CPU rerank → 必要scope擴展 → coverage/facts → 最終allowed packing → canonical重驗 → 簽章。
提案RRF `k=60`、兩路等權重、finite值、穩定ID tie-break；各路score只在自身排序，不當同一概率或confidence。
rerank global cap30，input pair<=512 tokens；rerank文字視窗規則尚待D03凍結，禁止靜默丟關鍵條件當完整證據。
per-document cap是packing的多樣性規則，不能限制完整表格枚舉；超context預算即coverage不足，不靠調成30宣稱完整。

RetrieveResponseV2 提案：status沿用三種業務結果；`allowed_evidence[0..20]`、
`restricted_references`（只含可讀文件的machine IDs＋安全title/section/page，**無原文**）、
`coverage`、`verified_facts`、`context_attestation|null`、content-free diagnostics、`budget_used`。
allowed內rank為1..N；restricted refs獨立排序，不參與可外送signed evidence；不向Answer轉送restricted refs。
混合結果只送allowed；pure restricted不呼叫Answer。全部checked枚舉資料只要有restricted且影響清單總體，就不產生精確總数的外部答案。

Coverage不是LLM評分，提案欄位：`state=sufficient|partial|insufficient|ambiguous|blocked`、
`required_fields`／`satisfied_fields`、`missing_codes`、`pinned_scopes`、`complete_scope: StrictBool`、`truncated: StrictBool`。
只有同version、完整來源、沒有未知結構／缺頁／未解ACL範圍、必要條件都有anchors且packing未失去必要資料才能sufficient。
fact需直接anchor；compare每側每欄需anchor；procedure順序/前置/警告需保留；清單/count需完整枚舉而非top-k。
allowed資料不足不送Answer，回insufficient；語義scope冲突／歧義由Chat用safe code產生clarification，不能透露未授權scope。

VerifiedFacts提案只支援typed count/list/compare/procedure：schema/version、task、unit、predicate、dedup key、
count/items/steps值與每項canonical lineage、scope completeness。最多2000 source rows；要外送的facts仍受總context/token/body上限。
計數先限定單一明確logical scope與固定version；去重鍵由核准欄位registry＋使用者意圖定義，不使用同名猜身份。
跨文件／多版本聯集、未明確定義單位、無法證明0筆的完整範圍，追問／拒答；LLM不能自行選最新或用找到的列数當總数。
facts、lineage／label都要重驗外送許可與內容policy；純数值也不能當成可繞過restricted的資料。

## 9. Answer v2與Ed25519 v2

AnswerRequestV2 提案只允許 `schema_version=answer-context-v2`、answer_question、language、task、
requirements、pinned_scopes、coverage、allowed_evidence、verified_facts、context_attestation、budget。
這些欄位必須與Retrieval最終context相同；不接受original/history/search_queries/restricted refs/path/URL。
只有coverage=sufficient、external_llm_allowed是真boolean true的evidence/facts可用。
輸出保留parts＋引用對應模式；新增對count/list的typed结果校验：数值/項目必須匹配verified facts，不接受模型另算總数。
模型漏引用、加新數值、捏造來源、格式錯誤回safe contract error，不重送Answer POST或用未驗證答案頂替。
runtime不保存Prompt/body/token；只log provider/model/prompt_version、token數、status、safe codes與request_id。

沿用Ed25519／RFC8785／SHA256，不改成另一種共享secret簽章。
v2 separator提案為 `helpdesk-rag/question/v2\0` 與 `helpdesk-rag/context/v2\0`。
question canonical精確物件為 `{"question": answer_question, "language": language}`（key仍叫question，與HTTP欄位名不同）；context精確物件見第17節，包含 **schema_version、task、requirements、pinned_scopes、coverage、ordered allowed_evidence、verified_facts與lineage**；不含score/diagnostics/budget。
digest採UTF-8 JCS後domain-separated SHA256、base64url無padding；v1的hex/JSON預設序列化不能替代。
JWT alg/kid/iss/aud/iat/nbf/exp/jti規則沿用；新增`context_schema_version=retrieval-context-v2`、v2 policy_version、question/context digest。
TTL建議120秒；Answer只接受對應v2schema/policy/key/用途、expiry与signature，禁止v2失敗後downgrade成v1／unsigned。
Retrieval唯一持private key；Answer只有公钥；Chat opaque即時轉送、不解析不保存。不提供任意可簽外部payload的endpoint。
最終canonical重驗後才簽，立即送Answer。簽章不是即時撤權查詢：重驗後到Answer之間仍有TTL內快照窗口，不能宣稱跨服務零TOCTOU；有更嚴格撤權需求須另定protocol。

**U1 golden vectors**：合成fixture與精確驗證邊界見第17節及其連結。正常中文/null、Unicode及簽章/digest負向案例可在不呼叫服務下交叉計算；
expiry/key/policy/strict boolean/coverage等語意拒絕規則仍需在後續兩個服務各自的contract/security tests執行，不把離線資料驗證冒充runtime驗收。

## 10. 有界預算、錯誤與feature flags（D06提案）

所有數值都是 **待核准的上限／效能目標**，不是現有.env指令或已測結果。

| 資源 | 提案上限／目標 | Owner／檢查 |
|---|---|---|
| accepted後整個turn | 120秒，含capacity wait、planning、兩輪搜尋、backoff、生成、保存 | Chat monotonic deadline；開始每段先檢剩餘，不每輪重置 |
| 搜尋輪／改寫 | 最多2輪，每輪最多3query；第二輪只補既有safe gap | Chat；不任意agent loop、不換目標 |
| KIS搜尋 | 最多12logical calls／turn（2輪×3query×2mode）；含retry最多24HTTP attempts | Retrieval統計，回報spent；scope reads另計但有總頁上限40 |
| 模型呼叫 | 最多3logical calls／turn，實際Provider HTTP attempts總計<=5 | Answer回spent；v2 generation只有1 logical POST，不由Chatretry |
| 第一版模型路徑 | planning一次＋answer一次；不自動plan repair；只受控 transient Provider retry | Answer POST重放未知結果禁止；即使壞plan也不消耗第3次“補救”猜意思 |
| 各段total ceiling | planner20秒、每輪Retrieval20秒、Answer75秒 | 全部還需clamp到剩餘turn deadline；不能把ceiling相加當獲准超時 |
| rerank | 每輪<=30candidate、CPU threads<=2、inference concurrency1／queue2 | Retrieval單一模型實例；禁止多uvicorn worker複製weights |
| user history／planner I/O | 2000history tokens；prompt<=6000 tokens、output<=1000 | 使用選定planning tokenizer；無法計數就fail closed，不用字數冒充 |
| final evidence／generation I/O | evidence<=20；context<=3000 tokens；prompt<=12000、output<=1024 | signed facts及citation文字也算context；真Provider tokenizer待凍結 |
| bytes | Chat submit<=64KiB；plan/retrieve request<=128KiB；Retrieve response<=2MiB；Answer request/response<=512KiB | sender＋receiver雙重限制，實際bytes檢查 |
| scope | 每輪<=20頁／2000rows／2MiB；單頁<=100items／1MiB | 不先fetch無限全文再事後截斷；truncated不精確計數 |
| 三併發 warm驗收 | local Retrieval p95<=15秒；完整turn p50<=45秒／p95<=100秒；120秒硬截止 | 固定synthetic tuning／CPU限制／模型settings；未實測，quotafailure另列不挑最好run |
| 記憶體 | 新Retrieval含模型peak<=1.5GiB；整套Docker workload peak<=3.5GiB、無OOM/swap依賴 | 目前VM3.819GiB；必須量測，不按模型檔大小推估RAM |

Budget wire的精確欄位、clock與attempt accounting提案見第18節；全是trusted內部欄位。
每層限制為min(自己的config,上游remaining)，每次attempt前扣額；平行call先分配不重複額度，下游回spent，未知消耗保守當已使用。
不由public client提供budget，也不讓模型調大；receiver只能缩小本次配額，不能將整turn120秒重新起算。
v2 retrieve/plans/answers POST不由Chat自動retry；KIS read及Provider retries只在許可transient分類與剩餘aggregatebudget內。
401/403/413/422、malformed、content-type、oversize、provider400/contract failure不retry。429尊重有上限Retry-After且免費quota限制停止，不改key／模型／付費方案繼續跑。

| 類別 | safe HTTP／code提案 | 語意 |
|---|---|---|
| public schema／版本 | 422 invalid_request/unsupported_schema | 未accepted則不呼叫下游 |
| owner／scope不可見 | 404 resource_not_found | 不洩露存在性 |
| payload／cursor／version冲突 | 409 idempotency_conflict/scope_changed/clarification_stale/conversation_busy | 不混版本或重放不同問題 |
| 自己容量拒絕 | terminal429 capacity_exceeded＋bounded Retry-After | accepted後的新transaction保存與replay |
| 下游auth／configuration | 503 dependency_unavailable/provider_unavailable | 不回key、raw body、401詳情 |
| 下游contract錯誤 | 502 dependency_contract_error/provider_contract_error | 不把失敗當unable_to_answer業務答案 |
| deadline／結果未知 | 504 deadline_exceeded；Provider結果未知failed_unknown | 保存terminal，禁止用同key再生答案 |
| 無答案／restricted／追問 | completed的typed outcome，HTTP200 | 與transporterror分開，不謊稱answered |

v2 feature flag提案：CHAT_RAG_V2_ENABLED、PLANNING_ENABLED、HYBRID_ENABLED、RERANK_ENABLED、STRUCTURED_SCOPE_ENABLED、GAP_RETRY_ENABLED；預設false，整套能力要明確啟用／驗收，不以永遠false宣稱完成。
v2 endpoint停用回明確503 feature_disabled，不默默將v2轉成v1。reranker enabled但不可用，v2 readiness fail closed；不偷偷跳rerank或外送fallback。
保持v1 readiness／可用性獨立；細部health endpoint版本／metrics label allowlist需U1凍結。
只有content-free counters／低基數status/task/reason/timing/modelrevision；不log原問題、answer、history、evidence、scopeID、cursor、token、key、path、URL或attestation。

## 11. 舊規格差異、遷移與回退

下表是 **需要核准的替換矩陣**，本次不改舊spec、progress或AGENTS。

| 精確來源 | v1保留 | v2待核准增補／替換 |
|---|---|---|
| Chat spec §6，165–187行 | 正規化規則、長度、型別 | 增加typed planner與有限user-history，不讓LLM直接產生事實 |
| Chat spec §7–8，188／238行起 | old request／簽章allowlist／restricted禁外送 | v2 plan request、signed task/coverage/facts；final context後仍不可改問題/順序 |
| Chat spec §9，275行起；§11，360行起 | v1五outcome/CRUD原樣 | v2獨立conversation與clarification_required；舊Web不改 |
| Chat spec §12，534行起 | commit-before-HTTP、owner lock、replay、不重複Provider | 加snapshot／reply_to／v2fingerprint及同conversation單processing規則 |
| Chat spec §13–14，587／665行起 | 六表、跨schema零FK、row_version、purge preview/lock/audit | U4增欄位/型別需explicit migration；snapshot/meta隨conversation cascade且不能進audit内容 |
| Chat spec §15，695行起 | v1timeouts/stale關係、size/redirect/安全logs | v2整turn deadline與aggregatebudget；stale>max(v1合法處理窗,v2deadline)+margin |
| Chat AGENTS 14–16、53–55行 | 舊Phase1–7completed歷史不重設 | 新U階段以升級計劃gate及handoff進行；需明確AGENTS增補後才能實作Chat |
| Chat AGENTS 30–39行 | HTTP sibling、noKIS/DB、owner、restricted／log禁令、opaque簽章、Answer POST不retry | 無放寬；v2只轉送專屬allowlist |
| Chat AGENTS 40行 | 禁Redis/Kafka/Celery、torch/transformers進Chat、前端/SharePoint/OCR | 只為v2解除multi-turn rewrite禁令；CPU模型在Retrieval，Chat不加torch |

遷移順序：U1核准 → 保全舊baseline → U2 additive KIS API/schema／synthetic backfill → U3 Retrieval v2 → U4 planner/Chat v2 → U5 parser/Answer typed facts → U6安全/品質/CPU验收 → 明確啟用v2。
未取得額外授權不對公司100GB文件reindex、backfill、改外送metadata；先只新隔離synthetic庫。
Schema upgrade必須Alembic/PG16測試；migration barrier；不得create_all/SQLite/mockPG/skip替代。
rollback先關v2入口及flags，保留v1 API／image／舊列，不直接drop新欄位／公司索引或改parserversion偽裝回退。
恢復舊binary需驗證additive schema相容；若不相容就停止並按核准回退runbook處理，不對共用DB破壞downgrade。
新依賴只進owner service的lock與pinnedCPU來源；Python3.11／non-root／read-only／capdrop／no-new-privileges／loopback ports／secret不上image的舊規則保留。

## 12. CPU／keyword／parser選型建議（D02/D03，未批准）

| 項目 | 首選提案與已查證 | 尚缺，不能宣稱已可部署 |
|---|---|---|
| 中文keyword | jieba 0.42.1（MIT）＋固定繁中字典，query與document同分詞；PG16 simple ts_vector＋GIN、ts_rank_cd | 字典commit/hash、normalization/符號/HMM與query組合規則、indexmigration／大小、繁中tuning召回、Python3.11双架構實測 |
| CPU reranker | cross-encoder/mmarco-mMiniLMv2-L12-H384-v1；Apache-2.0；候選revision 8008f0154ca2387468013ed439df09d70c122412；官方safetensors權重SHA見下文 | tokenizer SHA／本機下載hash、實際繁中品質、512pair視窗規則、CPU dependency exact lock/wheels、ARM原生/模擬區分、RSS/latency；不承諾模型一定fit現有RAM |
| rerank runtime | 優先評估小型CPU-only PyTorch adapter，單模型實例；可經port評估ONNX但不雙路一起引入 | 精確torch/transformers或ONNX版本、license/CPUwheel與安全載入；cache預先準備、runtimeoffline、不trust_remote_code、不pickle任意外部檔 |
| PDF | 保留既有pypdf；評估pdfplumber作文字型PDF table extractor（MIT） | exact版本／lock、跨頁/合併格fixture、RSS、parserconfidence/gap規則；不能當OCR或保證任意PDF表格可辨識 |
| DOCX/TXT/MD | 保留現有locked解析套件，增typedblock/row lineage | 不變更套件仍需U5fixture；DOCX目前表格已有文字但非typed rows，不能將舊內容標成完整 |
| Planning | 沿用Groq openai/gpt-oss-20b，透過Answer用途port | U1尚未呼叫；免費額度只由使用者確認，remaining未測；無公司內容/history外送授权 |

來源（2026-10-06唯讀查證；不是效能證據）：
[jieba維護者文件](https://github.com/fxsjy/jieba)、[jieba0.42.1/PyPI](https://pypi.org/project/jieba/0.42.1/)、
[PG16全文索引](https://www.postgresql.org/docs/16/textsearch-tables.html)、
[reranker官方模型卡](https://huggingface.co/cross-encoder/mmarco-mMiniLMv2-L12-H384-v1)、
[候選固定revision config](https://huggingface.co/cross-encoder/mmarco-mMiniLMv2-L12-H384-v1/blob/8008f0154ca2387468013ed439df09d70c122412/config.json)、
[pdfplumber維護者文件](https://github.com/jsvine/pdfplumber/blob/stable/README.md)、[MIT license](https://github.com/jsvine/pdfplumber/blob/stable/LICENSE.txt)。
模型卡支援多語及許可不代表本公司繁中品質達標；config的hidden size384／12layers不等於實際RAM。

候選下載清單只允許上述固定revision的`model.safetensors/config.json/tokenizer.json/tokenizer_config.json/special_tokens_map.json`；fast tokenizer實測如需其他檔案，先回報，不拉整個repo或執行train_script.py。
[`model.safetensors`官方LFS metadata](https://huggingface.co/cross-encoder/mmarco-mMiniLMv2-L12-H384-v1/blob/8008f0154ca2387468013ed439df09d70c122412/model.safetensors)列SHA256 `5daeca2481a76b5976a2bdc32f0a78532b6716da4f8cd3ff59460ef8d2f359b4`、約471MB。
這是**遠端預期hash**，不是已下載/載入通過；不能把Xet hash或Git pointer hash當weights SHA。使用`use_safetensors=True/trust_remote_code=False/local_files_only=True`，不下載/載入pytorch_model.bin或ONNX另一套weights來隱藏失敗。
繁中字典候選正確來源是[`v0.42.1/extra_dict/dict.txt.big`](https://github.com/fxsjy/jieba/blob/v0.42.1/extra_dict/dict.txt.big)，不是`jieba/dict.txt.big`；tag commit與實際bytes SHA尚缺，不以浮動master路徑凍結。

CPU依賴候選沿用已存在KIS `uv.lock`的精確版本，reference lock SHA256=`b6f3b572168c898c89f9e27bf759aeefd6f390a7442b6c997069fa850435677c`。這是**參考資料**，不是Retrieval已更新或已安裝成功；兩服務不import彼此package。U3需在自己的lock完整解析/凍結transitive dependencies，不能只抄4個top-level版本後稱可重建。

| 候選package | Python3.11 Linux x86_64 wheel SHA256 | Python3.11 Linux aarch64 wheel SHA256 |
|---|---|---|
| torch 2.11.0+cpu（download.pytorch.org/whl/cpu，manylinux2_28） | 8a56a8c95531ef0e454510ba8bbd9d11dc7a9000337265210b10f6bfeacdd485 | 46fbb0aa257bb781efbfad648f5b045c0e232573b661f1461593db61342e9096 |
| tokenizers 0.22.2（cp39-abi3 manylinux2_17） | 369cc9fc8cc10cb24143873a0d95438bb8ee257bb80c71989e3ee290e8d72c67 | 2249487018adec45d6e3554c71d46eb39fa8ea67156c640f7513eb26f318cec7 |
| safetensors 0.8.0（cp310-abi3 manylinux2_17） | fd6f3f93c9a0a7cc2788ee63fb763353d4bd2e89b0751bc78fcf7dda00bea774 | 7a46e5ff292c356d6991e60942ba7f79817682d3a2cef0702136448cb9c4d235 |
| transformers 5.15.0（兩側相同py3-none-any） | d7f007736f67749ae9490c4f8cb5d30b452ae2d68c8675e50ba8d63ea7feb107 | 同左 |

這些是local lock列的expected wheel hash，非本輪下載驗證或新reranker可運行證據。CPU source／ABI與Python3.11/Linux target須匹配；不得採Mac wheel、PyPI CUDA torch或建置時最新版本。候選授權/完整deps/模型相容性與RAM/latency仍需核准後有界probe，現有KIS ARM可運行不等於新Retrieval模型通過。

## 13. U1完成gate與待決策

| 項目 | 現在狀態 | 完成前必須補 |
|---|---|---|
| U1-01 | in_progress | 第15–18節已補wire/null/version、cursor／typedstructure/facts／簽章提案與golden vectors；離線Python/Node各12case通過，尚待契約核准與獨立review，不是v2 runtime通過 |
| U1-02 | in_progress | 核准v2conversation隔離、4個user-onlyhistory、snapshot/idempotency、clarification承接、processing互斥提案 |
| U1-03 | in_progress | 確認候選，freeze依賴／字典／權重hash、下載來源、license、CPU版本和有界相容/資源probe；本輪不install/download/probe |
| U1-04 | in_progress | 第18節已有exact deadline/spent/clock/retry、保守byte-budget與baseline/rollback提案；核准targets/flags及其限制，CPU資源未量測 |
| U1-05 | in_progress | 第11/19節已有精確差異與最小AGENTS增補提案；尚未核准，不修改實際AGENTS/舊Phase歷史；獨立review不能由本實作agent自評passed |

先請使用者決定三組方向，避免低階模型隨意猜：

- A（D05/D07）：保留v1，獨立v2 API先驗收；有限user-onlyhistory、不明追問；count先單一完整scope/version，跨文件不猜。
- B（D02/D03）：同意採上述免費CPU候選做後續有界技術驗證；實測不fit／繁中不佳時回報換選型，不直接升大型模型或外部rerank。
- C（D04/D06）：同意Groq受控planning方向、2輪/每輪3查詢/120秒及表列資源上限作凍結提案；不授權本輪模型呼叫、公司資料外送或付費fallback。

方向同意不等於未知版本／hash自動凍結；U1-01/03未完成就留in_progress，不開始U2。
驗收需包含跨服務schema對照、正常與負向golden vectors、budget算式／transaction／privacy审查、amd64/arm64证据，且使用者批准、獨立review无P0/P1/P2。
整體品質門檻照升級計劃§7：未量測就是NOT_MEASURED，不能拿文件完整或155個eval工具tests代替新功能/业务品質。

## 14. 首輪handoff邊界（歷史，不是v0.2最新結果）

本輪只有契約草案與現況文件更新，production changes=0；models/provider calls=0；DB/Docker操作=0。
尚未freeze契約、沒有獨立review、沒有golden vector執行或效能測量，沒有開始U2。
精確逐檔manifest、指紋核對與命令結果記於升級計劃本次handoff；本文件不是第二個phase進度來源。

## 15. 精確 wire 欄位提案（D05/D07）

這一節是低階模型的欄位清單，不是要求自行設計 generic agent framework。
`S(a,b)` 是strict string，長度以Unicode code points計；a>=1時另拒絕全空白，a=0時允許來源空字串/空白（例如空cell），不能同時宣稱非空白；`I(a,b)` 是strict integer（bool不算int）；
`B` 是真正 JSON boolean；`U` 是小寫標準 UUID；`H` 是64個小寫hex；`?` 表示必填且可null。
`A(T,a,b)` 是有界array；所有object拒絕未知key。所有response欄位都必填，無資料使用null/[]，不省略。
只有下表明列default的request欄位可省略；adapter送出前必須填入default。不得把整段原始下游JSON直接轉送。

### 15.1 共用 records

| Record | 精確 keys / 型別 | Invariant |
|---|---|---|
| Citation | title:S(1,1000), section_path:S(1,2000)?, page_number:I(1,1000000)? | 只由已授權來源取得；無path/URL |
| Governance | confidentiality:public/internal/confidential/restricted, language:S(1,20), external_llm_allowed:B | Answer只接受`is True`；policy仍可拒絕 |
| Requirement | field:scope/year/audience/unit/comparison_field, value:S(1,256) | 不接受SQL／自由filter；來源由Chat另行核對，不把owner或turn ID送Answer |
| InputSourceRef | block_id:S(1,128), row_id:S(1,128)?, cell_keys:A(S(1,64),0,100) | 僅Ingestion→KIS提交；引用本次Structure內local ID，不帶尚未分配的document/version UUID |
| SourceRef | document_id:U, version_id:U, block_id:S(1,128), row_id:S(1,128)?, cell_keys:A(S(1,64),0,100) | 引用固定version；cell keys唯一且按column ordinal排序 |
| Evidence | rank:I(1,20), chunk_id:U, document_id:U, version_id:U, content:S(1,100000), document_type:S(1,100), token_count:I(1,512), citation:Citation, governance:Governance, source_refs:A(SourceRef,0,100) | rank恰為1..N；chunk/version不重複；所有refs同evidence文件/version；不含score |
| ScopeSelector | kind:section/block/table/ordinal_window, source_id:S(1,128)?, start_ordinal:I(0,9999)?, end_ordinal:I(0,9999)? | section/block/table只有source_id；window只有start/end且start<=end；所有unused欄位明確null |
| PinnedScope | document_id:U, version_id:U, selector:ScopeSelector, structure_sha256:H, scope_sha256:H, governance_revision:I(1,2147483647) | scope hash涵蓋穩定有序members/content/anchors/quality，不是只hash列數 |
| Coverage | state:sufficient/partial/insufficient/ambiguous/blocked, required_fields:A(S(1,64),0,8), satisfied_fields:A(S(1,64),0,8), missing_codes:A(Gap,0,8), complete_scope:B, truncated:B | arrays唯一；satisfied子集required；sufficient須無gap、不truncated且required=satisfied；list/count另需complete_scope=true |
| FactItem | key:S(1,256), label:S(1,1000), values:A(FieldValue,0,8), source_refs:A(SourceRef,1,20) | key是核准source欄位的exact值或logical row ID，不用名稱相似度去重 |
| FieldValue | field:S(1,64), value:S(1,4000) | 欄位必須存在於該scope的column registry；不猜日期／單位換算 |
| VerifiedFacts | schema_version:"verified-facts-v1", task:list/count/compare/procedure, unit:logical_row/distinct_value, key_field:S(1,64)?, count:I(0,2000), items:A(FactItem,0,2000) | count等於唯一items數；logical_row時key_field=null；distinct_value時需固定欄位；compare/procedure依items順序及values表示，不另設自由facts map |

Gap封閉enum：`scope_required/year_required/audience_required/unit_required/comparison_field_required/scope_ambiguous/version_conflict/structure_unknown/parse_incomplete/coverage_gap/budget_exhausted/context_overflow/outbound_blocked/unsupported_task`。
`required_fields`只允許`scope/year/audience/unit/comparison_field/key/label/source_anchor`；非registry字串拒絕。
scope欄位registry由typed parser/YAML mapping提供，只有`key/label/year/audience`與最多8個來源原欄位；不把模型猜出的欄名當column key。
count/list只支援**單一完整表格或章節/version**。來源沒有核准去重鍵或「系所」與「班別」意義不明，必須追問；預設logical_row不是預設「系所總數」。
compare先同一文件/version，兩側各有來源anchor；跨文件、跨年度版本聯集不在v1升級的精確計數能力內。
fact沒有VerifiedFacts（null）；其他task成功產生時必須有matching facts。完整0筆須有完整scope proof，不能從search空陣列推論0。

### 15.2 Request / response allowlists

| 入口 | Request keys | Response keys |
|---|---|---|
| Chat create v2 | title:S(1,200)? default null | v1 ConversationResponse全部keys＋contract_version:"chat-v2"；201 |
| Chat submit v2 | question:S(1,20000), language:S(1,20)? default null, reply_to_turn_id:U? default null | v1 TurnResponse全部keys＋contract_version:"chat-v2", clarification:Clarification?；200/202或原terminal錯誤status |
| Chat GET v2 conversation/turn/messages | path IDs；messages limit:I(1,100) default50,cursor:S(1,4096)? defaultnull | 對應v1安全response全部keys＋contract_version；Message新增clarification?，response_kind可為clarification_required |
| Answer plans | planning_schema_version:"query-plan-v1", answer_question:S(1,20000), language:S(1,20)?, history:A(History,0,4), budget:Budget | planning_schema_version, proposal:PlanProposal, budget_used:Spent |
| KIS search | schema_version:"knowledge-search-v2", mode:vector/keyword, query:S(1,2000), limit:I(1,30) default30, filters:Filters, budget:Budget | schema_version, items:A(SearchItem,0,30), score_kind:cosine/ts_rank_cd, engine_revision:S(1,128), budget_used:Spent |
| KIS scope-pages | schema_version:"knowledge-scope-v2", scope:ScopeRequest, cursor:S(1,4096)? defaultnull, limit:I(1,100) default100, filters:Filters, budget:Budget | schema_version, scope:PinnedScope, items:A(BlockOrRow,0,100), next_cursor:S(1,4096)?, complete:B, truncated:B, missing_codes:A(Gap,0,8), budget_used:Spent |
| KIS chunks/revalidate | schema_version:"knowledge-revalidate-v2", chunk_ids:A(U,0,30), scope_proofs:A(PinnedScope,0,3), filters:Filters, budget:Budget | schema_version, chunks:A(SearchItem,0,30), scopes:A(PinnedScope,0,3), checked_at:UTC datetime, budget_used:Spent |
| Retrieval retrieve | schema_version:"retrieval-plan-v2", answer_question:S(1,20000), language:S(1,20)?, task_plan:TaskPlan, round_index:I(1,2), prior_scope_state:A(PinnedScope,0,3), budget:Budget | schema_version:"retrieval-result-v2", status, allowed_evidence:A(Evidence,0,20), restricted_references:A(RestrictedRef,0,20), task, requirements, pinned_scopes:A(PinnedScope,0,3), coverage, verified_facts?, context_attestation:S(1,16384)?, diagnostics:Diagnostics, budget_used:Spent |
| Answer answers | schema_version:"answer-context-v2", answer_question:S(1,20000), language:S(1,20)?, task, requirements, pinned_scopes, coverage, allowed_evidence:A(Evidence,1,20), verified_facts?, context_attestation:S(1,16384), budget:Budget | schema_version:"answer-result-v2", status:answered/unable_to_answer, answer_parts/citations/generation（原Answer response shapes）, verified_result:VerifiedFacts?, budget_used:Spent |

`task`為fact/list/count/compare/procedure；`requirements=A(Requirement,0,4)`、field唯一。
`Clarification={question:S(1,1000), missing_conditions:A(Gap,1,4)}`；只允許前六個條件缺失/歧義codes，不帶文件候選內容。
`History={question:S(1,20000)}`；不送user ID／assistant答案。ordinal/order在Chat內部固定。
`PlanProposal={task_type, resolved_task_type?, answer_question, search_queries, required_conditions, missing_conditions, scope_hints, needs_clarification}`完全按§6 bounds；required_conditions使用Requirement，missing_conditions為最多4個Requirement.field enum（無value），不新增自由map。
TaskPlan由Chat形成：`{task, search_queries:A(S(1,2000),1,3), requirements, scope_hints:A(S(1,256),0,3)}`。第二輪queries只補第1輪已回safe gap，不改answer_question/task/requirements。
`Filters={issuer:S(1,255),user_id:S(1,512), group_ids:A(S(1,512),0,100), language:S(1,20)?, scopes:A({document_id:U,version_id:U},0,3)}`；published/current在KIS強制，不讓caller選draft。user_id必須非空，groups唯一。
`ScopeRequest={document_id:U,version_id:U,selector:ScopeSelector}`；無完整性hash初值，KIS首次取得時產生manifest。
SearchItem為Evidence移除rank、增加`score:finite number,chunk_index:I(0,9999),structure_state:known/unknown,scope_hints:A(ScopeSelector,0,3),governance_revision:I(1,2147483647)`；token_count正整數<=512。
RestrictedRef只有`document_id/version_id/chunk_id:U,citation:Citation`，不得出現content/source_refs/facts。
Diagnostics只有`candidate_count:I(0,180),selected_count:I(0,20),rerank_applied:B,gap_codes:A(Gap,0,8),retrieval_elapsed_ms:I(0,20000)`；selected_count等於allowed+restricted引用數，總和<=20。
status=evidence_found需coverage=sufficient、allowed非空且attestation非null；external_llm_blocked只可coverage=blocked且allowed空、token=null；其他不足/歧義為insufficient_evidence且token=null，不呼叫Answer。
Answer coverage不足／task/facts不相符回422，不呼叫Provider；answered每part必有有效C1..CN引用，无孤兒citation；unable兩陣列空且verified_result=null。
對count/list，**建議Answer以已核驗facts確定性排版**（不讓模型改寫項目/數字）；Provider calls可為0。這是D07待核准提案，不能把模型自然語言中的數字僅靠regex當成完整驗證。
fact/compare/procedure才用grounded Provider文本；比較欄位/數字必來自typed values，無支持主張按原contract error處理，不把LLM judge當安全驗證。
KIS v2 document submission沿用v1 DocumentSubmissionRequest的metadata/default/ACL/lifecycle欄位；數值/boolean改strict，新增`schema_version:"knowledge-document-v2",structure:Structure`，chunks各新增`source_refs`（預設[]）。Response沿用v1 submission全部keys＋schema_version；job status仍pending/processing/completed/failed。
提交chunk.source_refs精確為`A(InputSourceRef,0,100)`；KIS先完整驗local ID，再分配/取得canonical document/version並轉成SourceRef供讀取。禁止要求Ingestion猜UUID，也禁止直接信提交者的document/version欄位。
KIS submission request body上限32MiB（不是原檔案100MB上限）；超限413，全文件停止，不部分匯入後宣稱完整。自製fixture先走v2，舊v1文件不自動重建。

### 15.3 Headers / errors

內部POST都是`Content-Type:application/json`、`Accept:application/json`、`X-Request-ID`與目的服務專屬`x-internal-api-key`；沒有Provider key混用。
Chat→Retrieval在development使用現有`x-dev-user-id`、`x-dev-group-ids`；production仍fail closed，不新造Entra或可信headers方案。
KIS identity只從已驗的Retrieval Principal形成Filters。新增v2不授權正式SSO/SharePoint。
v2 Error精確為`{error:S(1,64),message:S(1,500),request_id:S(1,128)?,details:{reason_code:Gap?,retry_after_seconds:I(1,86400)?}?}`；外部raw schema/value不回顯。
v1 Error解碼保持原样。内部401/403不原樣當public employee auth失敗，依§10映射503；policy拒絕為安全業務outcome。
Retry-After只採整數秒或有效HTTP日期，ceil/clamp 1..86400；無效值null。429免費quota停止該turn，不等待長backoff穿越deadline。

## 16. Structure / cursor / persistence 約束提案

`Structure={schema_version:"document-structure-v1",parser_revision:S(1,128),quality:complete/incomplete/ambiguous,blocks:A(Block,1,10000),tables:A(Table,0,100)}`。
Block欄位：`block_id:S(1,128),ordinal:I(0,9999),kind:paragraph/heading/list_item/table,section_path:S(1,2000)?,page_number:I(1,1000000)?,text:S(0,100000),table_id:S(1,128)?,quality:complete/incomplete/ambiguous`。
Table欄位：`table_id:S(1,128),block_id:S(1,128),unit_label:S(1,64)?,key_column:S(1,64)?,columns:A(Column,1,100),rows:A(Row,0,2000),quality:complete/incomplete/ambiguous`。
Column：`key:S(1,64),ordinal:I(0,99),label:S(1,256),role:key/label/value`；key/ordinal唯一；最多一個key及一個label role，不確定則key_column=null。
Row：`row_id:S(1,128),ordinal:I(0,1999),cells:A(Cell,1,100),anchors:A(Anchor,1,20),continuation_of:S(1,128)?,quality:complete/incomplete/ambiguous`。
Cell：`column_key:S(1,64),text:S(0,4000)`；恰好對應每個column一次，缺格以空字串表示但quality不能假定完整。
Anchor：`block_id:S(1,128),page_number:I(1,1000000)?,section_path:S(1,2000)?`；文件/version從父層繼承，不能reference別份文件。
IDs是來源內local IDs，parser相同revision/輸出須穩定；block_id/table_id/row_id各自在整個Structure內唯一（row_id不可在別表重用），ordinal在各自block stream/table/column內唯一。KIS以version FK界定唯一性，不要求Ingestion猜尚未分配的version UUID。
table block與Table需雙向對應；所有row/anchor/source_refs必須解析得到，循環continuation拒絕。continuation關係只保留來源，不額外算一筆；parser不確定去重就incomplete，不能count。
complete scope包含heading/header、所有logical rows/必要warnings，穩定ordinal排序；scope hash用RFC8785＋`helpdesk-rag/scope/v1\0`的SHA256 **hex**，與JWT digest的base64url不要混用。
manifest object精確為`{document_id,version_id,selector,parser_revision,quality,members}`；members為該scope的完整有序Block/Row，含anchors/cells；doc治理另以governance_revision重驗。structure hash則對整個Structure JCS的SHA256 hex。

儲存選擇採最小增量：KIS `document_versions.structure JSONB NULL`及`structure_sha256 VARCHAR(64) NULL`；舊NULL就是unknown，不變成complete；documents增加positive INTEGER governance_revision，任何metadata/ACL變動同transaction+1，溢位fail closed。
typed payload先完整schema/invariant檢查，再保存；JSONB不是任意metadata袋。不新增一套多表文件引擎；version原FK/cascade/lifecycle照現有模型。
chunks新增`source_refs JSONB NOT NULL DEFAULT '[]'`與keyword `TSVECTOR`，來源refs在同transaction驗version內local ID；GIN索引、所有FK留在knowledge schema；ACL仍canonical原表。
DB CHECK需保護structure/hash同時NULL或非NULL、hash格式、gov revision>0、JSONB頂層object/array型別；typed內部約束由strict parser與application驗證，不宣稱PG CHECK可驗全部cell關係。
KIS submission fingerprint固定含schema/parser_revision/structure及chunks/source_refs；mapping或quality改變也新version。不偷用原檔hash或原v1fingerprint。
Chat U4只增既有tables必要欄位：conversation contract_version（舊列chat-v1）、turn reply_to_turn_id/history_as_of_ordinal/有界snapshot_meta與clarification。FK只在chat schema，所有結果purge cascade；snapshot只含同owner turn IDs、ordinal、hash及user-confirmed條件，不含原文、attestation或facts。
reply_to若前一clarification已被承接或不再是最新可承接turn，409；accepted鎖conversation內檢查，避免兩個承接都成功。

Cursor最小方案用KIS獨立secret的HMAC-SHA256，不復用API/Groq/Ed25519 key；secret只唯讀mount，不能寫image/doc/sample.env。
payload精確`{schema:"scope-cursor-v1",principal_sha256:H,scope:PinnedScope,limit:I(1,100),next_ordinal:I(0,12000),iat:I(0,9007199254740991),exp:I(0,9007199254740991)}`；base64url(JCS(payload))＋`.`＋base64url(HMAC(payload-bytes))，總4096bytes，0<exp-iat<=120秒。
next_ordinal是manifest的**展平item stream位置**，不是可能重複的Block.ordinal或Row.ordinal；header→該table rows→後續block按來源順序，每項有唯一stream position。超過20頁/2000rows的scope必回partial，不承諾2000rows另加header仍一定能在20頁讀完。
Principal hash為SHA256(`helpdesk-rag/principal/v1\0`＋JCS `{issuer,subject,group_ids}`)的hex；groups依Unicode code point排序去重、不改identity大小寫。KIS可信caller給issuer/subject，舊development user_id adapter綁固定issuer，禁止client自行串接身份。
Filters的subject即user_id；單独KIS key不是任意issuer的授權來源，只用固定可信adapter設定的issuer allowlist。
cursor全欄strict、常數時間驗MAC、expiry/version/limit/selector/principal hash必匹配，之後仍重新驗ACL/current version；不以簽過cursor代替授權。
scope若換version/content/gov revision或parser quality，409 scope_changed；denied/nonexistent一律404，不能回漏掉的列數。revalidate scope需重新計算完整manifest，最多3scope/各2000rows且仍扣bytes/time預算；不能只echo caller hash。

## 17. 精確簽章與合成 golden vectors

v2 canonical question只含`question/language`，language null保留；不在簽章階段Unicode normalization或重新strip。
v2 canonical context **只有**以下7個keys：`schema_version:"retrieval-context-v2",task,requirements,pinned_scopes,coverage,allowed_evidence,verified_facts`。
requirements按field Unicode code point升序；scopes按document_id/version_id/selector JCS升序；其餘arrays保留規定source/rank順序，不任意sort evidence/items。
Evidence包含§15.1所有keys（含source_refs），可null值必保留；VerifiedFacts=null也必保留。已簽值原樣allowlist轉送Answer，未知key拒絕而不是悄悄刪掉再驗hash。
Question/context digest為SHA256(domain separator含NUL＋RFC8785 UTF-8 bytes)，base64url無padding。
header恰為`alg:EdDSA,kid,typ:JWT`；不接受jku/x5u/crit/jwk或從token網址讀key。
claims恰為`iss:"retrieval-service",aud:"answer-generation-service",iat,nbf,exp,jti,context_schema_version:"retrieval-context-v2",policy_version:"retrieval-policy-v2",question_sha256,context_sha256`。
iat/nbf/exp是strict Unix整數秒，nbf=iat、0<exp-iat<=120；接收clock skew上限5秒，`now>=exp+5`過期、iat/nbf>now+5拒絕；deadline可更早終止，不被JWT時窗延長。
v2 policy只可綁`grounded-answer-v2`（fact/compare/procedure）與`verified-facts-render-v1`（count/list確定性排版）；路徑由已簽task決定，不接受caller指定prompt。planning用`query-plan-v1`且不接受context token，不把不同purpose共用成任意LLM proxy。正式key ID由secret設定，不硬編碼test key。
count/list仍驗signature/coverage/governance/source refs及內容policy，renderer不繞過安全檢查；generation的既有keys不變，固定provider=`deterministic`、model=`verified-facts-v1`、prompt_version=`verified-facts-render-v1`、finish_reason=null、prompt_tokens/completion_tokens=null、Spent provider兩欄0。不能把排版回應記成Groq真生成。

fixture：[rag-upgrade-v1-golden-vectors.json](./rag-upgrade-v1-golden-vectors.json)。使用RFC8032公開test seed產生**合成**Ed25519 key，絕不是可部署私鑰；不得放入任何service .env/secret。
重現命令與驗證限制見[離線核對方法](./rag-upgrade-v1-vector-verification.md)。
包含中文/null、Unicode/跳脫與完整2列count兩個正常case；固定時刻、JCS字串/digests/JWS/signature及負向mutation清單，可由Python rfc8785/cryptography與Node crypto各自重算。
負向mutation明列應拒絕階段：digest/signature篡改可離線證明；expiry/kid/policy/schema/strict boolean/coverage等需要兩側runtime tests，沒有v2服務前記`not_executed_runtime`。
這份fixture只證明**encoding/cryptography互通**，不證明Retriever真的取到完整表格／實際ACL／答案品質，也不算U0生成觀測。

## 18. 預算 / readiness / 回退精確提案

Budget恰為`{deadline_utc_ms:I(1,9007199254740991),max_duration_ms:I(1,120000),provider_logical_remaining:I(0,3),provider_attempts_remaining:I(0,5),search_logical_remaining:I(0,12),search_attempts_remaining:I(0,24),scope_pages_remaining:I(0,40)}`。
Spent恰為`{provider_logical:I(0,3),provider_attempts:I(0,5),search_logical:I(0,12),search_attempts:I(0,24),scope_pages:I(0,40),elapsed_ms:I(0,120000)}`；未用resource填0，超分配配額就是502契約錯誤。
Chat開始時建立UTC deadline與本機monotonic deadline；receiver可用時間=min(stage ceiling,max_duration_ms,deadline_utc_ms-wallclock_now-5000ms)。5秒為clock-skew保守reserve，不能加到deadline上；剩餘<=0立即504。主機時鐘不同步超過5秒，不宣稱有一致跨服務截止保證，v2 fail closed。
Chat預留5秒作terminal DB保存，不拿最後剩餘時間再發生成；persistence在有界timeout內失敗按既有stale recovery處理，不回成功。planning/搜尋queue wait/backoff全算stage時間。
provider v2每logical最多2HTTP attempts，僅對已確認可安全重試的connect-before-send/合格503/504；read timeout或送出後結果未知不重發生成。最多3logical/5attempts是總護欄，第一版不增加第3次自動修plan。
KIS read每logical最多2attempts；scope pages每HTTP attempt都扣1（失敗／重試仍耗頁額），不足直接truncated+budget_exhausted，不標complete。revalidation也扣scope配額/時間，不另開無上限通道。
Chat對v2 plans/retrieve/answers POST不retry；任何回應遺失將本次保守記為分配配額全部使用且failed_unknown，不換Idempotency-Key自動續跑。
v2只一個processing turn/conversation；Chat v2 max-active=3、queue=6（與v1 capacity budget共用總池，不另外超賣）；全部走deadline/lease finally release。v1舊限額不直接調整。
scope paged rows只在本次Retrieval記憶體聚合，20pages/2000rows/2MiB含manifest與anchors；達界即partial。hash revalidation不容許skip已被截斷的members。
Token budgeting先採**保守UTF-8 byte上界**（每byte計1），所有system/chat-template/JSON/citation/facts一起算，不宣稱此數等於Groq billing tokens；僅限制送出資料，真Provider usage仍記實際值。Provider tokenizer未可離線可靠取得前，不使用錯模型tokenizer估算成精確數。
§10的2000/6000/3000/12000門檻套此保守上界，常較真正tokens嚴格；不容納時拒絕／partial，不增加默認門檻。E5及reranker512token則用各自固定真tokenizer，不能用byte規則代替模型shape檢查。
rerank pair以query優先、完整chunk<=512pair：query最多128tokens，chunk若連special超512則截取有界window作**排序訊號**；原始完整chunk仍保留檢查/packing，不能把window當完整scope。不得依window新增不在原文的事實。
health採原`/health/live`與v1`/health/ready`不變；新增`/health/ready/v2`，false flag或必要v2模型/keyword/schema/planningpolicy/token能力缺失回503。Chat v2探測對應v2readiness，不拿v1ready當新能力ready。
v2 readiness不付費呼叫Provider、不展示secret或高基數值；checks固定`feature_enabled/schema/keyword/reranker/planning_policy/context_policy`（各服務只自己持有那一組）；status只有ok/not_ready。
metrics labels限`service/task/status/reason`封閉enum；request_id僅safe log correlation非metrics label。不加scopeID/文件名/query當label。
回退順序：停Chat v2入口→等在途turn terminal或stale→關v2flags→切舊已保全image；PG additive欄位留存，不downgrade共享庫；驗v1 contract/readiness＋既有回歸集。回退不能重放failed_unknown生成。
舊baseline保全清單：185 source sha、五份uv.lock／Dockerfile／image digest、embedding revision、Prompt/policy內容hash、**不含key的allowlist limits**、資料manifest與ACL治理指紋。未保存不可變舊image/源码前U2不能改production行為。

## 19. 既有 AGENTS 的最小增補提案（尚未寫入）

只在使用者核准U1契約後，向Chat AGENTS追加獨立「RAG v1升級」節，不覆寫Phase1–7歷史：

1. U階段只按升級計劃next_eligible_phase執行，handoff寫升級計劃；舊Chatprogress只記原Phase1–7歷史，不能重設。
2. 僅v2、且U4已授權時允許有限user-history/query planning，取代舊第40行multi-turn rewrite禁令；其餘Redis/torch/前端/OCR禁令不變。
3. planner只透過Answer `/api/v2/plans`，不得直Provider/KIS/DB；模型輸出不得決定identity、policy、預算或任意tool。
4. 原Answer allowlist規則在v2精確對應§15/17；restricted raw/facts禁轉送，signed context不可改、token opaque/no-store、POST不retry不变。
5. 新typedoutcomes/snapshot/flags只在v2，所有owner/transaction/idempotency/purge和舊v1 tests不降級。
6. 本增補不授權模型/公司資料外送、公司索引重建或U2–U6自動執行；phase末仍等獨立review。

AGENTS文件優先順序只對這些明列v2差異採已核准契約，不寫「一律新計劃優先」；其他衝突停止回報。
