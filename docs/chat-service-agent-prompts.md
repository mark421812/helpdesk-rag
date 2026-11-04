# Chat Service 低階 Agent Prompts

本檔案只放精簡執行提示，不在 prompt 重複整份規格。文件優先順序固定為：規格（normative）→
施工計畫（Phase scope）→ AGENTS（執行/安全規則）→ progress（狀態與證據）：

- `/Users/markhuang/Documents/Codex/2026-07-23/new-chat/docs/chat-service-spec.md`
- `/Users/markhuang/Documents/Codex/2026-07-23/new-chat/docs/chat-service-implementation-plan.md`
- `/Users/markhuang/Documents/Codex/2026-07-23/new-chat/docs/chat-service-progress.md`
- `/Users/markhuang/Documents/Codex/2026-07-23/new-chat/chat-service/AGENTS.md`

## 首次啟動：Phase 1

```text
請在下列 workspace 開發 Chat Service：

/Users/markhuang/Documents/Codex/2026-07-23/new-chat

本次只執行 Phase 1：Scaffold 與工程基線。不得開始 Phase 2。

開始任何修改前，必須完整讀取：

1. /Users/markhuang/Documents/Codex/2026-07-23/new-chat/docs/chat-service-spec.md
2. /Users/markhuang/Documents/Codex/2026-07-23/new-chat/docs/chat-service-implementation-plan.md
3. /Users/markhuang/Documents/Codex/2026-07-23/new-chat/docs/chat-service-progress.md
4. /Users/markhuang/Documents/Codex/2026-07-23/new-chat/chat-service/AGENTS.md

先確認 progress 的 next_eligible_phase 是 1，再依施工計畫的 Phase 1「必做」、「不做」與
「驗收」實作。若文件內容衝突，以規格為準，但停止衝突部分並回報，不可自行補需求。

授權範圍：可自主讀取 workspace、在 chat-service 內建立/修改檔案、只為本 Phase handoff
更新 docs/chat-service-progress.md，並執行本 Phase 要求的無破壞性 lint、typecheck 與 tests。
不得修改其他四個服務，不得做外部寫入、付費呼叫或刪除使用者資料。

如規格與現有契約衝突，停止實作衝突部分，回報精確檔案、行號、衝突及建議，不要猜。
不可以 TODO、pass、假實作、刪測試或降低檢查規則來宣稱完成。

完成後：

1. 執行 Phase 1 全部驗收命令。
2. 更新 docs/chat-service-progress.md，依 handoff 模板寫入逐檔 manifest、命令與 exit code、
   pass/fail/skip 數量、偏離與阻擋，並正確設定 next_eligible_phase。
3. 回覆完成證據與是否具備進入 Phase 2 的條件。
4. 停止，不要自行開始 Phase 2。
```

## 後續 Phase 通用模板

將 `<N>` 與 `<PHASE_NAME>` 替換為目標 Phase。

```text
請繼續下列 Chat Service：

/Users/markhuang/Documents/Codex/2026-07-23/new-chat/chat-service

本次只執行 Phase <N>：<PHASE_NAME>。不得開始下一 Phase。

開始前必須完整讀取：

1. /Users/markhuang/Documents/Codex/2026-07-23/new-chat/docs/chat-service-spec.md
2. /Users/markhuang/Documents/Codex/2026-07-23/new-chat/docs/chat-service-implementation-plan.md
3. /Users/markhuang/Documents/Codex/2026-07-23/new-chat/docs/chat-service-progress.md
4. /Users/markhuang/Documents/Codex/2026-07-23/new-chat/chat-service/AGENTS.md

先確認 progress 的 next_eligible_phase 等於 <N>；Phase 3、5、7 還必須符合 review gate。
重新讀取本 Phase 相關的現有 Retrieval/Answer 實際契約。只實作施工計畫中 Phase <N> 的必做範圍。

授權範圍：可自主讀檔、修改 chat-service 與為記錄實作結果而更新 progress，並執行
本 Phase 要求的無破壞性測試。Phase 7 另可更新 workspace 根 README。不得修改其他四個服務的
production code/API。

唯一測試資料例外：Phase 7 的 smoke 可 hard purge 同一 script 本次建立並記錄的 synthetic
conversation IDs；E2E 可依規格第 18 節透過 KIS HTTP 建立/重用固定 synthetic allowed fixture，
並在 finally 只 soft-delete KIS 回傳的精確 document ID。必須遵守 `.env.e2e` opt-in、key 隔離及
exclusive lock。不得用廣泛 selector 或碰既有公司資料。

如規格衝突、前階段未完成或需要擴大範圍，停止並列出精確證據，不要猜。不得以
TODO、pass、假實作、刪測試或降低檢查規則來宣稱完成。

完成後執行 Phase <N> 的所有驗收命令，依 handoff 模板更新 docs/chat-service-progress.md，回報
完成範圍、逐檔 manifest、實際測試結果、偏離、阻擋與下一 Phase gate，然後停止。Phase 3、5、7
只能把 review_status 設為 pending，不得自行宣告 review passed 或開放下一 Phase。
```

## Review Prompt

```text
請只做 Chat Service Phase <N> code review，不要開始下一 Phase，也不要擴張範圍。

完整讀取：
- docs/chat-service-spec.md
- docs/chat-service-implementation-plan.md
- docs/chat-service-progress.md
- chat-service/AGENTS.md

先確認 Phase <N> 是 review checkpoint 且 review_status 為 pending。依 progress 的逐檔 manifest
與 workspace 現況檢查程式碼、migration 與測試證據；workspace 根目錄不是 Git repository，
不得把不存在的 root git diff 當成唯一依據。重新執行所有可執行的 Phase 驗收命令並記錄 exit code。

判斷：
1. 是否完整符合該 Phase 規格。
2. 是否有安全、所有權、restricted evidence、transaction、idempotency 或契約風險。
3. 是否有冗餘、dead code、無效測試或用 mock 隱藏的問題。
4. progress 中的完成宣稱是否有證據。

先以優先級列出 findings，每項附檔案與行號。如無 findings，明確說明檢查過的範圍與剩餘風險。
本次不修改 production code；只可更新 docs/chat-service-progress.md 的 review findings、驗收證據、
review_status 與 next_eligible_phase。若有實質 finding 設為 changes_requested；全部通過才設為 passed。
```
