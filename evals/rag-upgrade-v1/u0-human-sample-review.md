# U0 人工抽查：只核對 16 題

版本：`u0-preliminary-sampled-v1`；建立日期：2026-10-05。
狀態：**來源／預期答案已由使用者核對 16/16；模型回答／引用尚待核對 0/16**。目前尚無真實生成答案，不是已通過的評測報告。

## 你要做什麼

每題打開「來源」，只確認右邊的預期答案／拒答是否有依據、條件與引用位置是否正確。
PDF 清單／計數要看表格所在的第 1、2 頁；DOCX 不猜頁碼。其他文件找表中列出的章節／標記即可。
生成測試完成後，同一題會提供三次實際回答與引用供你比較，不需要另外填三份表單。

直接在對話回覆，例如：`T01-fact 正確；T03-count 有問題，應該是……`。
你也可以一次回覆多題，或說「這一批除某題外都正確」。只能核准你實際看過的題目；不必編輯 JSON。
核對者與回覆日期會隨你的回覆記錄；未提供的實際核對時間不猜測，AI 不代簽。

「有問題」不代表要你修程式：指出問題即可，由工程師判定是 Gold 草案錯誤還是系統失敗。
16 題之外，只需再確認來源／Gold 衝突、數字或引用錯誤、三次結果矛盾等有疑義項目；不是逐一閱讀所有低分題的全部 chunk。

## 固定抽樣，不挑成功題

使用 v2 資料集，manifest SHA-256：
`09c9185ff12a773ab623be1f61ef03ddfc594f5ce28148f0ad5668a61e3b1ad2`。

依 `fact/list/count/compare/procedure/followup/no_answer/ambiguity` 順序，
來源群組依序取 `01/02/03/04/05/01/02/03`；tuning 與 holdout 各取一次。
共 16 個固定 case ID、8 類各 2 題，涵蓋兩領域、四種格式與全部 10 個來源群組。
抽樣在真實生成前固定；不得依回答好壞換題。這是代表性診斷抽查，不是隨機樣本或統計品質保證。

下面「預期」逐字／語意摘自 **AI 建立的 Gold 草案**；使用者於 2026-10-06 核對這 16 題的來源與預期並回覆 OK。這不是模型實際回覆，也不是全量 80 題正式 Gold 核准。
清單與計數只計 `open` 項目，以 `(unit_id, item_code)` 去重：同碼不同單位分開算，重印列不重算；必須確認範圍與結束標記。

## Tuning：8 題

| ID／題型 | 問題 | 預期答案／行為（草案） | 核對來源／位置 | 真人來源／預期結論 |
|---|---|---|---|---|
| T01-fact／單點 | 教育訓練補助 v2 的申請截止日期？ | 2027-02-18 | [rules.md](/Users/markhuang/Documents/Codex/2026-07-23/new-chat/evals/rag-upgrade-v1/revisions/u0-synthetic-v2/corpus/tuning/T01/rules.md)：日期，`E-T01-DATE` | 來源／預期 OK |
| T02-list／清單 | 根據測試環境備份 v2 操作流程 PDF，列出全部開放項目，附單位及代碼並去重。 | 甲單位 K001 資料庫快照；不得把重印列或未開放列當新增項目 | [procedure.pdf](/Users/markhuang/Documents/Codex/2026-07-23/new-chat/evals/rag-upgrade-v1/revisions/u0-synthetic-v2/corpus/tuning/T02/procedure.pdf)：第 1、2 頁，適用項目；`E-T02-CATALOG`、`E-T02-END`、R-T02-001～006 | 來源／預期 OK |
| T03-count／計數 | 根據公務設備借用 v2 操作流程 PDF，以單位與項目代碼的組合去重，共有幾個開放項目？ | 3 種設備；不是表格實體列數 | [procedure.pdf](/Users/markhuang/Documents/Codex/2026-07-23/new-chat/evals/rag-upgrade-v1/revisions/u0-synthetic-v2/corpus/tuning/T03/procedure.pdf)：第 1、2 頁，適用項目；`E-T03-CATALOG`、`E-T03-END`、R-T03-001～008 | 來源／預期 OK |
| T04-compare／比較 | 比較測試憑證輪替 v2 目錄中甲單位 K001 與乙單位 K001 的資格及費用。 | 甲：入口維運人員、30 元；乙：代理維運人員、20 元。來源不完整不妨礙比較這兩筆已記載資料，但不能聲稱全目錄完整 | [catalog.docx](/Users/markhuang/Documents/Codex/2026-07-23/new-chat/evals/rag-upgrade-v1/revisions/u0-synthetic-v2/corpus/tuning/T04/catalog.docx)：適用項目，R-T04-001／002 | 來源／預期 OK |
| T05-procedure／流程 | 會議室預約 v2 操作流程的四個步驟，請依序說明。 | 查詢可用時段 → 填寫活動用途 → 送出預約申請 → 使用後完成設備復原 | [procedure.pdf](/Users/markhuang/Documents/Codex/2026-07-23/new-chat/evals/rag-upgrade-v1/revisions/u0-synthetic-v2/corpus/tuning/T05/procedure.pdf)：第 1、2 頁，操作步驟，E-T05-STEP1～4 | 來源／預期 OK |
| T01-followup／追問 | 那第二個的資格和費用呢？ | 依下方既定 history，第二個是乙單位 K001：任職滿六個月、1200 元 | [catalog.docx](/Users/markhuang/Documents/Codex/2026-07-23/new-chat/evals/rag-upgrade-v1/revisions/u0-synthetic-v2/corpus/tuning/T01/catalog.docx)：適用項目，R-T01-002 | 來源／預期 OK |
| T02-no_answer／無答案 | 測試環境備份文件中的正式環境的管理員密碼是多少？ | 拒答：文件未記載，不推測／編造密碼 | [notes.txt](/Users/markhuang/Documents/Codex/2026-07-23/new-chat/evals/rag-upgrade-v1/revisions/u0-synthetic-v2/corpus/tuning/T02/notes.txt)：`E-T02-MISSING` | 來源／預期 OK |
| T03-ambiguity／歧義 | 公務設備借用這次的截止日是哪一天？ | 追問版本：未指定 edition，不能自行當成 v2 | [notes.txt](/Users/markhuang/Documents/Codex/2026-07-23/new-chat/evals/rag-upgrade-v1/revisions/u0-synthetic-v2/corpus/tuning/T03/notes.txt)：`E-T03-EDITION` | 來源／預期 OK |

T01-followup 的固定 history：

- 使用者：「請在教育訓練補助 v2 目錄比較甲單位 K001 與乙單位 K001。」
- 助理：「第一個是甲單位 K001，第二個是乙單位 K001。」

現有 v1 系統沒有語意 history 功能。此題仍保留以量出能力缺口；不能在 U0 偷加 rewrite/history 後再稱為舊基線。

## Holdout：8 題，真人／獨立 reviewer 核對

來源與 Gold 草案可查看；之後的逐題生成失敗內容不回饋實作 agent 調參，只回報彙總。

| ID／題型 | 問題 | 預期答案／行為（草案） | 核對來源／位置 | 真人來源／預期結論 |
|---|---|---|---|---|
| H01-fact／單點 | 版本发布演練 v2 的演練結案截止日期？ | 2028-07-11 | [rules.md](/Users/markhuang/Documents/Codex/2026-07-23/new-chat/evals/rag-upgrade-v1/revisions/u0-synthetic-v2/corpus/holdout/H01/rules.md)：日期，`E-H01-DATE` | 來源／預期 OK |
| H02-list／清單 | 根據交通接駁登記 v2 操作流程 PDF，列出全部開放項目，附單位及代碼並去重。 | 甲單位 K001 河岸線；乙單位 K001 河岸線。名稱／代碼相同但單位不同，不合成一項 | [procedure.pdf](/Users/markhuang/Documents/Codex/2026-07-23/new-chat/evals/rag-upgrade-v1/revisions/u0-synthetic-v2/corpus/holdout/H02/procedure.pdf)：第 1、2 頁，適用項目；`E-H02-CATALOG`、`E-H02-END`、R-H02-001～007 | 來源／預期 OK |
| H03-count／計數 | 根據事件告警演練 v2 操作流程 PDF，以單位與項目代碼的組合去重，共有幾個開放項目？ | 5 個告警規則；不是表格實體列數 | [procedure.pdf](/Users/markhuang/Documents/Codex/2026-07-23/new-chat/evals/rag-upgrade-v1/revisions/u0-synthetic-v2/corpus/holdout/H03/procedure.pdf)：第 1、2 頁，適用項目；`E-H03-CATALOG`、`E-H03-END`、R-H03-001～010 | 來源／預期 OK |
| H04-compare／比較 | 比較展覽攤位申請 v2 目錄中甲單位 K001 與乙單位 K001 的資格及費用。 | 甲：有核准展示計畫、1500 元；乙：有互動展示設備、2100 元。不宣稱不完整目錄涵蓋全部項目 | [catalog.docx](/Users/markhuang/Documents/Codex/2026-07-23/new-chat/evals/rag-upgrade-v1/revisions/u0-synthetic-v2/corpus/holdout/H04/catalog.docx)：適用項目，R-H04-001／002 | 來源／預期 OK |
| H05-procedure／流程 | 儲存容量檢查 v2 操作流程的四個步驟，請依序說明。 | 確認隔離測試資源 → 收集容量指標 → 比對核准警戒值 → 提交容量盤點結果 | [procedure.pdf](/Users/markhuang/Documents/Codex/2026-07-23/new-chat/evals/rag-upgrade-v1/revisions/u0-synthetic-v2/corpus/holdout/H05/procedure.pdf)：第 1、2 頁，操作步驟，E-H05-STEP1～4 | 來源／預期 OK |
| H01-followup／追問 | 那第二個的資格和費用呢？ | 依下方既定 history，第二個是乙單位 K001：平台管理員、70 元 | [catalog.docx](/Users/markhuang/Documents/Codex/2026-07-23/new-chat/evals/rag-upgrade-v1/revisions/u0-synthetic-v2/corpus/holdout/H01/catalog.docx)：適用項目，R-H01-002 | 來源／預期 OK |
| H02-no_answer／無答案 | 交通接駁登記文件中的眷屬可否免費搭乘是多少？ | 拒答：文件未記載眷屬免費搭乘規則。不依常識回答；题目措辭若有問題也請指出 | [notes.txt](/Users/markhuang/Documents/Codex/2026-07-23/new-chat/evals/rag-upgrade-v1/revisions/u0-synthetic-v2/corpus/holdout/H02/notes.txt)：`E-H02-MISSING` | 來源／預期 OK |
| H03-ambiguity／歧義 | 事件告警演練這次的截止日是哪一天？ | 追問版本：未指定 edition，不能自行當成 v2 | [notes.txt](/Users/markhuang/Documents/Codex/2026-07-23/new-chat/evals/rag-upgrade-v1/revisions/u0-synthetic-v2/corpus/holdout/H03/notes.txt)：`E-H03-EDITION` | 來源／預期 OK |

H01-followup 的固定 history：

- 使用者：「請在版本发布演練 v2 目錄比較甲單位 K001 與乙單位 K001。」
- 助理：「第一個是甲單位 K001，第二個是乙單位 K001。」

## 狀態與限制

- 來源／預期答案核對：16/16 OK；核對者為本對話使用者（human），回覆日期 2026-10-06。
- 實際模型回答／引用核對：0/16，pending；尚未生成，不沿用來源核對結果代替。
- 異常確認：真實生成尚未執行，沒有捏造異常清單或模型答案。
- 完整 80 題 Gold 與 40 題全 catalog relevance templates 仍 pending；本次抽查確認不改寫正式表單，不執行 formal freeze。
- 本表只有自製測試問題／預期草案，不含公司資料、API key、attestation 或真正憑證。
- 抽查完成只支持 U0 初步基線，不是正式 nDCG、完整品質正確率、U6 release PASS 或 U1 自動授權。

## 2026-10-06 人工核對補登

- 直接證據：本對話使用者回覆「檢查完了 OK」，對應上一輪提供的這 16 題來源／預期答案核對表；沒有提供異議或修正。
- actor_kind：human（本對話使用者）；紀錄時間：2026-10-06T00:16:43+08:00。實際逐題核對完成時間未提供，不以紀錄時間冒充 reviewed_at。
- 核對範圍：表內固定的 8 題 tuning＋8 題 holdout，16/16 來源／預期 OK；不是獨立 Code Review 或模型答案核准。
- 原待核對表 SHA-256：`08445bf2f9ba8953757953e6ecac32182249387e79550de0f0f214620e4c6604`。
- v2 dataset manifest SHA-256：`09c9185ff12a773ab623be1f61ef03ddfc594f5ce28148f0ad5668a61e3b1ad2`；本次未改題目、預期、抽樣 ID、來源或 sidecar。
- 歷史：2026-10-05 建立時來源核對 0/16；本次只補登使用者的真人確認，不由實作 AI 重新核准來源。
- 未完成：80×3 真實問答觀測、同 16 題的實際回答／引用抽查、疑義異常確認、Provider 分段基線與獨立 review。正式 80 Gold／40 relevance 保持 pending。
- U0 仍 in_progress、review_status=not_started、next_eligible_phase=U0；不發布品質 PASS、不開始 U1。
