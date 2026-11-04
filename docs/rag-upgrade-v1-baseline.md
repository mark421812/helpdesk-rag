# Help Desk RAG U0 基線報告

日期：2026-10-06（v1/v2 歷史保留；Groq Free Plan 已確認；16/16 來源與預期答案已由使用者核對，實際模型回答／引用仍 0/16、真實生成尚未執行）。狀態：**現況盤點完成；回答品質 NOT_MEASURED；U0 品質補證 in_progress**。

使用者已確認「對Ｕ1」，目前可進入 [U1 契約／安全設計草案](./rag-upgrade-v1-contracts.md)，只做文件、不改服務。
這是 [升級計劃 v1.2](../升級計劃v1.md) 的 `u1-design-entry-v1` 例外，不是 U0 completed／review passed。
第一個 production 行為修改前須保全可重現舊 baseline；U3 比較前補相應檢索／人工證據，U6 release 前補真生成與 U0 review。
以下先前「不得進 U1」為歷史 gate，保留不刪除；不再拿它阻擋本次已核准的文件設計。

本次只建立 U0 評估材料／工具與隔離入庫，沒有修改五個服務或 Web Chat 的 production code、
API、migration、lock、Compose、公司文件 metadata 或 `.env`。只對全新 tmpfs 測試知識庫寫入
自製資料並清理本次資源；沒有碰公司 DB/索引、外部模型或任何付費操作。

## 目前結果與限制

已建立獨立 v2：80 題 Gold 草案、40 份自製来源文件、40 項安全／失敗案例清單。
155 個工具單元測試通過；v2 真實入庫單項通過：40 文件／91 chunks／91 points、最大 433 tokens。
v2 的 27 PDF 頁＋17 DOCX rendered 頁已完成實作端 rendering QA；16 題來源／預期抽查已核對，正式 80 Gold 表仍 pending，沒有品質分數。
40 題 tuning 的 KIS 候選已實際收集（每題 30 筆、共 1,200 筆）。另已實跑 installed Retrieval
use case＋real KIS HTTP：每題召回 24、選定 8，共 960 candidates／320 restricted evidence。
allowed context=0、Answer 尚未執行。這不是正式 Recall/nDCG 或完整答案品質。
單次 warm Retrieval usecase＋KIS HTTP p50/p95=117.903/150.5 ms，不含 Chat route／Answer；
前一 KIS-only p50/p95=125.431/195.892 ms 是不同 run，不拿兩者相減推算選取成本。
以下 v1 歷史結果保留：40 份文件的 Docker parser／chunking
單項通過，共 80 chunks、最大 171 tokens、必要 anchors 無遺失、token recount 一致。
另已驗證 KIS persistence：40 文件／40 版本／80 chunks／80 個 384 維向量，canonical mismatch=0、
40 replay／40 self-query／1 unauthorized-query 通過。**不代表 80 題答案／正式召回或 40 項安全 gate 通過。**
結構稽核已查明現況：60 列的線性欄位文字與 40 個流程步驟保留，但 typed table/row/cell/
跨頁續接不在現有 chunk 儲存與 API 契約中；這是 U2/U5 的缺口，不在 U0 修 production。

| 項目 | 狀態 | 已有證據 | 尚缺 |
|---|---|---|---|
| U0-01 題庫與人工抽查 | in_progress | v2 8 類各 10 題、2 領域、4 格式、40/40 split；44 頁 rendering QA；固定16題來源／預期答案已由使用者核對 OK | 同16題三次實際回答／引用抽查＋疑義異常確認、獨立 review；完整人工 Gold/freeze 延後 |
| U0-02 問題到答案的分段基線 | in_progress | v2 tuning 真 KIS 候選；installed Retrieval restricted trace；正式指標工具保留但不充當分數 | allowed context／簽章／Answer、80×3真實問答觀測、provisional診斷／人工抽查與分段歸因；不猜排除原因；全量 relevance 延後 |
| U0-03 資料可用性 | completed（現況盤點） | v1 installed schema/structure 盤點；v2 91 canonical chunks／272 線性列／362 anchors mapping、44 頁 rendering QA；typed table/row/cell/continuation 確認不存在 | 結構缺口留 U2/U5；來源 Gold 語意簽核仍屬 U0-01，這個 completed 不代表資料能力／問答品質通過 |
| U0-04 資源／相容性與候選方案 | in_progress | host 8 CPU／3.819 GiB；x86與ARM3.11 installed parser/chunker/真CPUembedding；120次restricted Retrieval/KIS資源基線；primary-source候選比較 | 完整問答／Provider tokens/network；其餘服務或新候選adapter尚未實跑，不宣告Oracle原生效能 |

## 題庫與資料治理

位置：[evals/rag-upgrade-v1](../evals/rag-upgrade-v1/README.md)。
來源設定：[scenarios.json](../evals/rag-upgrade-v1/scenarios.json)。
檔案指紋：[manifest.json](../evals/rag-upgrade-v1/manifest.json)。

- tuning：T01–T05；holdout：H01–H05。source group 不跨 split。
- 類型：fact、list、count、compare、procedure、followup、no_answer、ambiguity。
- 領域：規章與維運；不是針對招生簡章或 Resume 寫死。
- 四種格式各 10 份。PDF 操作流程兩頁，附錄有跨頁表格與重複表頭。
- Source anchors 與 row IDs 是 fixture lineage，不是 KIS chunk UUID；chunk UUID/hash mapping 已實測，結構列契約仍不存在。
- Gold 含 scope、必要條件、版本、章節／頁、anchors、計數單位與完整性要求。
- `human_review_status=pending` 共 80 題；**沒有冒稱人工已核對**。
- 所有 sidecar 外送許可為 false，ACL allow 只有 synthetic 測試群組。
- 不含公司資料；不曾匯入現有來源資料夾或知識庫。

初稿所有計數答案都是 2、只有三列目錄，不能用來證明真實大清單的計數完整性。
正式 freeze 前需不同總數、重複項、截斷／缺頁、跨欄及多版本等變化。
現有原始文件都具備來源編號；它們不能代替人類語意正確性標註。

## 已核對的現況

本次重新讀取五個後端與總 README，並唯讀核對 parser、chunker、KIS/Retrieval/Chat/Answer schemas：

- PDF parser (`ingestion-service/.../parsers/pdf.py`) 呼叫 pypdf extract_text，block_type 為 paragraph，保留頁碼，沒有 table/row lineage。
- DOCX parser (`.../parsers/docx.py`) 已按文件順序讀段落／表格，保留 Heading section，table 轉 Markdown-like 文字；不是完全忽略表格。
- Ingestion token-aware chunker 存在，使用固定 Hugging Face tokenizer。初次 bundled dependency/cache 不可用；
  本機 .venv 離線載入仍失敗，但使用者補登的 Docker cache probe 已成功。沒有以字數假裝 chunking 完成。
- KIS 現有 v1 search 做 query embedding、Qdrant 查詢與 PostgreSQL canonical 重查，沒有 doc/version 枚舉／結構列契約。
- Retrieval 現有 use case 做 score stable sort、內容去重、per-document cap、token packing。沒有語意 reranker。
- Chat 送相同 processed question 到 Retrieval／Answer，現有 API 沒有意圖／改寫／history planning 欄位。
- Answer 用 signed allowed evidence 組固定 Prompt，真實 Provider 仍須單獨 opt-in；不將 Fake 固定答案當品質。

### Parser-only 量測

執行在 Intel macOS 的既有 Ingestion `.venv`，Python 3.13.9；不是 Linux Python 3.11 或 Oracle ARM。
40 份小型自製文件在相同程序依序讀取，每份一次，含 cold import，併發 1。
全部必要文字 anchors 保留，但「文字存在」不等於「欄列關係與範圍完整」。
詳細檔案在 `evals/rag-upgrade-v1/runtime/u0-offline-checks.log`，只含 synthetic ID／計數。
這是初次 smoke-level parser measurement，不用作模型／服務效能結論。

| 格式 | 文件數 | 解析結果 |
|---|---:|---|
| MD | 10 | 有 section；文字標記保留 |
| DOCX | 10 | 每份 1 table block；有 section；列標記保留 |
| PDF | 10 | 每份 2 頁；table blocks=0；無 section；文字標記保留 |
| TXT | 10 | 文字標記保留；無 section |

已補齊 Docker CPU/RAM 與七個必要應用容器的瞬間快照；query embedding 時間、Retrieval latency、
Provider tokens、負載時資源與端到端 p50/p95 尚未量測，不能從小檔案 parser 速度或閒置快照
外推 100 GB 或 30 人併發。

### Host inventory 補充證據

使用者在 host 執行 `make inventory`，`inventory_exit=0`（非測試命令，passed/failed/skipped/errors=N/A）。
擷取時間：`2026-10-01T15:58:43.005469+00:00`；本次於 2026-10-02 核對並補登。
證據：[u0-host-inventory.log](../evals/rag-upgrade-v1/runtime/u0-host-inventory.log)。
Log SHA-256：`4b7b1370b76df370b2a2ecfda67d216ff6aa6edbc4274c030f558796e4962f26`。
來源為使用者附件 `/Users/markhuang/.codex/attachments/30941911-19cf-4412-85f3-8ac4f6236e53/Pasted text.txt`。

| 盤點項目 | 實際證據 |
|---|---|
| 主機／Docker 架構 | x86_64／x86_64；不是 Oracle 原生 ARM 證據 |
| Docker 資源 | 8 CPU、4,101,144,576 bytes = 3.819 GiB RAM |
| 必要應用容器 | 五個服務的七個 API／worker 容器均有唯一 running instance；沒有驗證 HTTP readiness |
| Embedding | intfloat/multilingual-e5-small；revision 614241f622f53c4eeff9890bdc4f31cfecc418b3；threads=2、concurrency=1 |
| KIS worker RAM | 928.7 MiB 的瞬間快照；不是 peak RAM 或 30 人負載結果 |
| Retrieval 設定 | default limit=8、candidate multiplier=3、per-document cap=30、context tokens=3000 |
| Answer 設定 | groq_hosted、openai/gpt-oss-20b、usage mode=trial、context tokens=3000 |
| 程式／設定指紋 | 185 個 source／lock／Docker／policy hash 全部與本次 workspace 核對一致 |
| 題庫 manifest | 40794e4027e12221c38710d7aa86da439b782314b07ffc6923b27f68751a1a9b；與目前檔案一致 |

沒有列出 API key、raw env、公司內容或 attestation。`trial` 只證明設定值，沒有驗證實際免費
quota／provider key，也沒有授權或呼叫 Groq。容器 image ID 已記錄，但未比對 image 內部程式
與 host 原始碼，因此不能僅凭兩種 fingerprint 宣稱它們來自同一次 build。
本快照未包含 PostgreSQL、Qdrant、scheduler、Web Chat 或 Docker VM 系統占用，不推估剩餘 RAM。

`MAX_CHUNKS_PER_DOCUMENT=30` 只放寬同文件上限；預設 Retrieval request 未指定 limit 時仍用 8，
allowed context 仍受 3000 tokens 限制。Chat 可傳入自己的 limit；此白名單快照未擷取 Chat 的
retrieval_limit，不以這份 log 宣稱實際每次請求一定是 8。沒有調參或修改任一 production 設定。

### Docker tokenizer probe 補充證據

2026-10-02 使用者提供 `ingestion-worker` 的離線 probe：`probe_exit=0`、
`tokenizer_probe=PASSED`、Python 3.11.15、model_max_length=512、synthetic_tokens=15。
固定 model/revision 與上方 inventory 一致；`HF_HUB_OFFLINE=1`、`TRANSFORMERS_OFFLINE=1`、
`local_files_only=True`；沒有下載、Embedding、DB 或 Provider 呼叫。
證據：[u0-tokenizer-probe.log](../evals/rag-upgrade-v1/runtime/u0-tokenizer-probe.log)，SHA-256：
`4eec42e758c2c8b992a0eabb0c42fcecd170593cc618ab7c2d7fae78742156d0`。

`PyTorch was not found` 是未安裝權重推論框架的訊息；此 probe 只執行 AutoTokenizer，已成功
計數，不是 embedding 失敗，也不需要在 Ingestion 補裝 Torch。模型向量推論仍是 KIS 的責任。
此簡易 log 未含 image ID／擷取時間／40 文件結果，僅補「cache 可用」前置證據，不能當作
完整 parser/chunk、KIS persistence 或 ARM runtime gate 通過。

2026-10-02 新增 `make parser-chunk-audit-docker`：從唯一 running worker 讀 immutable image ID
與 tokenizer volume，專用暫時容器 network=none、read-only、cache/eval 唯讀、不帶正式 env 或
公司文件／DB mount、不 pull/build；完成後只移除本次暫時容器。只執行 image 內已安裝的 parser
與 chunker；host workspace 只掛 eval，不掛 production source。
採類別預設 chunk 350／450／50、model 512，**不是驗證 live Settings 的自訂限制**。
資源上限為測試用 1 CPU／512 MiB，不當成 production p95 或原生 Oracle ARM 效能。
完整計數存在 `runtime/u0-parser-chunk-docker.json`，Terminal 只輸出彙總／fingerprint、不含全文。

工具會檢查 40 文件、所有必要 anchors、450／512 token 上限與精確 recount；缺標記、空 chunks
或超限 exit 1，環境阻擋 exit 2，不再把「程式結束」誤當單項驗收成功。
2026-10-02 的 Codex 執行因 Docker socket denied 而 exit 2；此歷史紀錄保留。
2026-10-04 已核對下方使用者的新 host 證據，解除「40 文件容器解析／切段未測」單項 blocker。

### Docker parser／chunking host 證據補登

使用者執行 `make parser-chunk-audit-docker` 並以 pipefail／tee 記錄 `chunk_exit=0`。
擷取時間：`2026-10-03T15:59:38.278331+00:00`（Asia/Taipei 2026-10-03 23:59:38）；
本次於 2026-10-04 核對，晚於 audit tool／tests 的最新修改時間。

- Log：[u0-parser-chunk-docker.log](../evals/rag-upgrade-v1/runtime/u0-parser-chunk-docker.log)。
  SHA-256：`3bf370874ce884ba5d79df6e20c2dcf20ab61ed68dd21ed6308ed6130129dcd9`。
- 完整逐文件計數：[u0-parser-chunk-docker.json](../evals/rag-upgrade-v1/runtime/u0-parser-chunk-docker.json)。
  SHA-256：`29256a57bb8cd46d40553747bce403a807c2bfecd1a179b7ad55111c14354d7c`。
- Image：`sha256:6fe0091088fcb2afe64c778d8f281cced217c171c447941acc12230d7e203ea8`；
  image user=app、network=none、installed parser、payloads_written=false。
- Dataset manifest／audit script／Docker tool 三個 hash 與目前 workspace 一致；
  40 個 fixture ID 完整且唯一，PDF／DOCX／TXT／MD 各 10 份；重新計算 failures=[]。
- Python 3.11.15、x86_64；固定 E5 tokenizer revision／model_max_length=512；
  類別預設 target/max/overlap/model=350/450/50/512，未讀 live Settings。
- 40 份都有 chunks，合計 80、最大 171 tokens；parser／chunk anchors 遺失=0、
  over_limit_count=0、recount_mismatch_count=0、status=PASSED；非業務／安全測試，數量記 N/A。

Peak process RSS=516,087,808 bytes（492.18 MiB），約為測試容器 512 MiB cap 的 96.13%。
此值來自單一程序的 `ru_maxrss`，不是整個 cgroup 峰值或正式 Worker 記憶體；不能據此估算
剩餘 RAM、30 人併發或大型文件容量。此批最大 chunk 僅 171 tokens，也不是接近 512 的長文壓力測試。
保留文字標記不等於還原跨頁表格欄列／完整性；沒有執行 embedding、KIS persistence、
Retrieval、Answer 或 ARM runtime，沒有呼叫 Provider。U0-03 仍為 in_progress。

### 文件視覺 QA

依 PDF／Word 技能使用 bundled LibreOffice 與 Poppler，而非使用者桌面 Office。
初稿 PDF CID font 有 Adobe-CNS1 mapping 缺失，已在尚未開始 live baseline 前修正為嵌入
TrueType font 並重產 hash；沒有改問題／Gold 語意或刪失敗題。
修正後 PDF T01 的兩頁確認中文字、步驟、表格可見。其餘 PDF 尚待逐頁核准。
DOCX canonical renderer 可執行（exit 0），但產出的中文有空白／缺字；切換字型及
SAL_FONTPATH 後仍未解決。**DOCX 視覺 gate BLOCKED，沒有宣稱渲染成功就是驗證通過。**
原始 DOCX 的 production parser 可以讀出正確 anchors，此差異需釐清 renderer 字型鏈路。
尚不將 DOCX 視覺輸出或 Gold 草案作為已核准的 final artifact。

## 工具驗收證據

cwd：`/Users/markhuang/Documents/Codex/2026-07-23/new-chat/evals/rag-upgrade-v1`。
工具使用已存在 `.venv`，沒有安裝或更新任一服務 dependency。

| 命令 | exit code | passed / failed / skipped / errors | 結論 |
|---|---:|---|---|
| `make validate` | 0 | N/A | 80 題／40 文件／40 安全清單與 hash/split 完整；人工 pending 80 |
| `make lint` | 0 | N/A | E/F/I/B/UP、Python 3.11 target，無降低既有服務規則 |
| `make format-check` | 0 | N/A | Ruff format check 通過 |
| `make test` | 0 | 18 / 0 / 0 / 0 | 評估工具測試；不是服務品質 |
| `make parser-audit` | 0 | N/A | 真 parser 讀取 40 fixture，missing anchor documents=0 |
| bundled Python `audit_parsers.py --chunks` | 2 | N/A | tokenizer dependency/cache 不可用；不下載、不假造 |
| bundled Python `host_inventory.py` | 2 | N/A | Docker socket denied；需 host evidence |
| `make inventory`（使用者 host） | 0 | N/A | 七個應用容器、185 檔指紋、dataset hash 完整；解除此次 inventory 證據 blocker |
| `make parser-chunk-audit`（本次 Codex、既有 Ingestion .venv） | 2 | N/A | OSError：固定 revision 本機 tokenizer 無法離線載入；沒有下載／改用假計數 |
| `make validate lint format-check test`（2026-10-02 回歸） | 0 | test：18 / 0 / 0 / 0；其餘 N/A | 四項均通過；Gold pending=80、安全 executed=0、品質仍 BLOCKED |
| Docker `ingestion-worker` 固定 tokenizer probe（使用者 host） | 0 | N/A | Python 3.11.15、512 上限、15 synthetic tokens；不是 embedding／40 文件驗收 |
| `make validate lint format-check test`（Docker audit 工具更新後） | 0 | test：27 / 0 / 0 / 0；其餘 N/A | 9 個新增工具測試；沒有 mock PostgreSQL 或服務品質宣稱 |
| `make parser-audit`（工具更新後） | 0 | N/A | parser-only 40 份、missing anchors=0；chunking 未執行 |
| `make parser-chunk-audit-docker`（Codex） | 2 | N/A | Docker socket denied；需更新後 host 容器證據，不 skip |
| `make parser-chunk-audit-docker`（使用者 host，新證據） | 0 | N/A | 40 文件／80 chunks／171 max tokens；anchors 遺失、超限與 recount mismatch 全為 0 |
| 證據／manifest／production 指紋唯讀核對（2026-10-04） | 0 | N/A | 完整唯一 40 fixture、三個 audit hash 一致；98 個實作 manifest 檔案與 185 個 production 指紋一致 |
| `PYTHONDONTWRITEBYTECODE=1 make validate`（2026-10-04） | 0 | N/A | hash/split 通過；Gold pending=80、安全 executed=0、品質仍 BLOCKED |
| `PYTHONDONTWRITEBYTECODE=1 make lint`（2026-10-04） | 0 | N/A | All checks passed，未改檢查規則 |
| `PYTHONDONTWRITEBYTECODE=1 make format-check`（2026-10-04） | 0 | N/A | 18 files already formatted，無格式化寫入 |
| `PYTHONDONTWRITEBYTECODE=1 make test`（2026-10-04） | 0 | 27 / 0 / 0 / 0 | 評估工具回歸；不是 RAG／安全品質通過數 |
| canonical DOCX renderer | 0 | N/A | 程序結束但中文視覺缺字；不能算 QA passed |
| initial CID PDF Poppler render | 0 | N/A | 有字型錯誤，未算 QA passed；已改嵌入 font |

初次 lint 發現格式／import 問題（exit 1），已修正後完整重跑。
初次系統 `python3 host_inventory.py` 是 Python 3.9，UTC import error（exit 1）；
host 命令已改使用現有 `.venv`，不以系統 Python 3.9 冒充 3.11 相容性。
所有後續修正只在 U0 新工具／fixture，沒有修 Groq 400 或其他 production 缺陷。
本次新增工具 lint 初次有兩行超長（make exit 2／ruff exit 1），已修正後完整回歸通過；
沒有放寬 E501。Apply patch 一次因 format 後 context 不符而拒絕，重新讀檔後正常套用。

`runner.py score` 目前只算 **anchor proxies** 和人工評分彙總；不是正式 Recall@30/nDCG@10。
沒有實際 observations 輸入，因此沒有發表基線分數。真實模型驗收要求全部 case 的三次 runs，
不允許 Fake、漏題、重複 run 或混用不同 prompt/policy/model；raw answer、原文與 token 欄位拒絕。
Holdout 輸出不含逐題問題／答案／失敗列表。實際 synthetic trace collector 尚未完成。

## 中文 keyword、CPU reranker 與 parser 候選

僅供 U1 決策，不新增 dependency、不下载模型，不承諾 ARM 效能／RAM。
以下依 2026-10-01 查核的 primary sources，非依記憶直接指定 production 型號。

| 能力 | 候選與理由 | U1 前須驗證 |
|---|---|---|
| 中文 keyword | 應用層中文斷詞＋PostgreSQL tsvector/GIN；另比較 pg_trgm 的局部名稱相似搜尋 | 中文詞庫、縮寫／日期／專名召回、索引大小、migration、ACL canonical；不能把 trigram 當語意 rerank |
| PG extension | zhparser 支援中文全文 parser，但有 extension build／部署成本 | PostgreSQL 16、Intel/ARM image、license、運維成本；未決定安裝 |
| CPU reranker 小模型候選 | cross-encoder/mmarco-mMiniLMv2-L12-H384-v1 的官方模型卡標為多語模型 | 繁中實測、固定 revision/license、CPU 精度、p95/RAM、Torch CPU wheel 或 ONNX 相容性 |
| CPU reranker 品質候選 | BAAI/bge-reranker-v2-m3 官方定位為 multilingual question/passage reranker | 實際記憶體與與現有 embedding 共存、候選 30 的 CPU 延遲；沒有下載／實跑 |
| PDF 表格 | pdfplumber 提供 table→row→cell 擷取，先比較文字型格線表格 | 跨頁續行、重複表頭、缺格、合併儲存格、source order；extract_tables 不代表所有 PDF 可完整還原 |
| DOCX 表格 | 優先延伸既有 python-docx parser，避免重複引入 Word 引擎 | row/cell lineage、合併格、章節／頁不確定性；Word page number 不可猜 |

Primary sources：PostgreSQL 16 的 [全文搜尋](https://www.postgresql.org/docs/16/textsearch-intro.html)、
[pg_trgm](https://www.postgresql.org/docs/16/pgtrgm.html)，
[jieba](https://github.com/fxsjy/jieba)、[zhparser](https://github.com/amutu/zhparser)，
[multilingual MiniLM cross encoder](https://huggingface.co/cross-encoder/mmarco-mMiniLMv2-L12-H384-v1)、
[BGE reranker](https://huggingface.co/BAAI/bge-reranker-v2-m3)、
[pdfplumber](https://github.com/jsvine/pdfplumber/blob/stable/README.md)。
以上只是選項比較；正式 revision、鎖版、效能 gate 與 fallback 必須在 U1 核准。

## Blockers 與下一個操作

### 2026-10-05 收尾範圍釐清

依使用者更新目標，U0 僅完成「升級前的品質基線與資料可用性盤點」，不把評估工具擴建成產品。
使用者已同意 `u0-preliminary-sampled-v1`：**80 題仍測，真人只看16題（每類2題）＋疑義異常**；
不要求真人逐一評完40題×91 chunks、不要求編輯 JSON。入口是 [16題核對表](../evals/rag-upgrade-v1/u0-human-sample-review.md)。
完整 Gold／全量 relevance／formal human-label freeze 延到後續正式評測，舊 templates 仍pending，validators 不改。
使用者已回覆 Groq 帳戶是 **Free Plan（免費）**，該前置阻擋解除；限額欄位見下表，不能據此推論剩餘 quota 或永久免費。
既有 80 題／40 文件、parser／KIS／restricted Retrieval／ARM 證據沿用，不重跑成功項目。
補最小必要的隔離 allowed → Answer 收集、80×3 全部真實觀測、AI provisional 診斷、16題人工答案／引用抽查、
tuning 失敗分類／Provider 延遲與 tokens，再交獨立 review；不新增核對介面或通用框架。
資料 hash、16題抽樣、模型／Prompt／policy／limits／硬體在生成前固定；全部 runs 保留，實際模型呼叫數另報，政策阻擋不算成功生成。
2026-10-06 使用者回覆「檢查完了 OK」，固定16題來源／預期答案核對16/16；實際模型回答／引用仍0/16、真實模型呼叫0。
原待核對表SHA-256=08445bf2f9ba8953757953e6ecac32182249387e79550de0f0f214620e4c6604，v2資料manifest未變；完整補登見抽查表。
不以來源核對代替生成答案核對，不冒稱正式 nDCG 或80題全量人工品質證明。

| 模型／方案（使用者確認） | RPM | RPD | TPM | TPD | ASH | ASD |
|---|---:|---:|---:|---|---|---|
| openai/gpt-oss-20b／Free Plan | 1K | 500K | 250K | - | - | - |

以上是使用者提供的帳戶資訊，非官方公開 Free 表的抄錄、API實測或保證；`-` 不自行解讀為无限額。
官方分別列出 [Free與Developer方案](https://console.groq.com/settings/billing/plans)，
並說明 [實際組織限額以帳戶Limits為準](https://console.groq.com/docs/rate-limits)。不切換付費，遇限額／方案變動即停止。

U0 不要求舊系統先達到 U6 的品質分數；低分或失敗是需要記錄的 baseline，而不是在此階段調參修功能。
40 項安全 catalog 保留，U6 新功能安全 gate 不提前做；runner 的外送、隔離與 exact-ID 清理仍需驗證。
synthetic-only 免費 Groq 授權已涵蓋新隔離 run 的必要外送設定，不再額外索取同一資料的 flag 授權。
不得改原始 sidecar／公司 metadata／ACL／policy／簽章；本次免費方案確認不代表已測過 API認證、模型可用性或剩餘額度。
本次是使用者核准的 U0 驗收修訂（plan_version1.1），不是替舊全量表單代簽。
U3 排序比較前補相應子集的固定真人 relevance／原始基線，U6 release 前補完整Gold/holdout/正式指標；第7.2節門檻不降低。
歷史段落保留，但不再要求其中全量人工前置阻擋精簡 U0；phase仍U0、真實觀測與獨立review尚缺。

1. Host inventory／tokenizer／40 文件 parser-chunk 與隔離 KIS persistence 已補齊；問答等 runtime 仍缺。
2. v2 多樣化與實作端 rendering QA 已補齊；16題來源／預期答案已核對。實際回答／引用抽查、疑義異常確認及獨立review仍未完成，正式全量Gold/freeze延後。
3. KIS mapping 與結構現況盤點已完成；typed lineage 未支援。40 題 tuning 的實際 Retrieval restricted selection 已收集，allowed context／簽章與 Answer 尚缺，全量relevance延後。
4. 隔離 synthetic 匯入／exact-ID cleanup 與 synthetic-only 免費 Groq eval 均已獲使用者同意；
   使用者已確認Free Plan與上述帳戶限額；遇限制／無法保持免費就停止，不得轉付費，不反覆索取同一確認。
5. 尚無80×3真實問答觀測、16題人工答案／引用結論與完整Provider分段基線。正式relevance labels仍pending但不阻擋精簡U0。

不必重跑沒有變更的 probes。下一步補最小 allowed 分支／Answer collector與16題人工抽查；
新增 collector 時使用自己的隔離 run/mapping，不沿用已清理的 UUID。
不直接複製 fixture 到公司來源資料夾；不寫共用 DB，不改公司 metadata/policy。
免費方案由使用者確認，API剩餘額度未實測；不取消 signed evidence/governance gate，本輪模型呼叫0。
仍留在 U0，補齊上述證據；不開始或宣稱已具備 U1 條件。

## 2026-10-04 隔離 KIS persistence 補充驗收

- 直接由 Codex 執行，Docker Engine 29.1.3 已連上；只授權本回合 socket／命令連線。
- 命令：eval cwd 的 `U0_ALLOW_ISOLATED_WRITES=1 PYTHONDONTWRITEBYTECODE=1 make kis-persistence-audit-docker`，exit 0。
- Artifact：`evals/rag-upgrade-v1/runtime/u0-kis-persistence-0c211fd0e82946a691b0e739a3445424.json`。
  SHA-256：`0ed33069a7224c252a18d53fa51960d943f7934c9d0514c7ef48acfc350e9cae`。
  started/captured=2026-10-04T02:35:57.717456+00:00；elapsed=139.128 s（不是端到端 latency 基線）。
- installed Ingestion parser/chunker/metadata/submission builder → KIS HTTP → 真實 CPU Worker →
  PostgreSQL 16／Qdrant；40 indexing jobs 全 completed。PG 診斷 query 為 READ ONLY，沒有 create_all/SQLite/mock。
- mapping 80 筆：fixture → document/version/chunk UUID、chunk index、頁碼、tokens、content/section SHA-256。
  實測欄位對照包含 metadata、ACL、parser、model/revision/dimension、chunk 全文與 hash、治理 payload。
- 40 次 replay 沒有增加文件／版本／jobs／chunks／points；40 self-query 的 canonical hydration 一致。
  1 個無關 group query 回空。這是功能探針，不是 80 題業務／40 安全案例的 passed 數量。
- 只有全新 internal network、無 ports、non-root/read-only/cap-drop、唯讀模型 cache/eval；
  API/Worker 分時。PG tmpfs 64 MiB／Qdrant 192 MiB；Qdrant WAL 4 MB／1 segment 是測試配置，非正式效能配置。
- 9 個測試容器與網路 exact-ID + ownership-label cleanup 全成功；tmpfs 自製資料已丟棄，可用工具重新產生。
  正式 KIS 四個容器仍 Running，StartedAt 保持 2026-10-01；185 個 production fingerprints 全一致。
- 保留失敗 artifact：b901669a（工具 mount 深度不符，exit 2）、56d8f56e（create-time NetworkID 尚空，exit 2）、
  71ddd599（index job failed，exit 2）、c02b264a（確認 tmpfs no-space，exit 2）。全數 cleanup 成功。
  修正只在新評估工具：改 mount 深度、create 驗證 NetworkMode/started 驗證 NetworkID、增大 test tmpfs 並縮 WAL。
  沒有降低規則、skip、修 production 或隱藏失敗；lint 初次 exit 2 已修正，patch context 拒絕後重讀重試。
- 工具驗收：`make lint format-check test validate` exit 0；test 34 passed／0 failed／0 skipped／0 errors。
  lint/format/validate N/A；runtime probe 是上述文件／job/check 計數，不冒充 pytest 數量。
- 免費 Provider 授權已取得，實際 quota 未確認；模型呼叫／模型下載均為 0；metadata external_llm_allowed 保持 false。
- 限制：未驗證 Ingestion DB/job/scheduler、Retrieval packing/ranking、Answer/Chat、Gold 品質、完整結構列或 ARM runtime。
  Artifact UUID 在清理後不可再呼叫；後續 collector 需重新建立隔離 run/mapping。U0-01～04 仍 in_progress/not_started，品質 BLOCKED。

### 最終 log 防護版本回歸

第一次功能通過的 artifact `0c211fd0...` 保留。之後發現 generator 的內部 payload stdout
會被預設 Docker log driver 保存（只有自製資料，且容器 cleanup 時已移除，沒有公司資料或 key）。
新工具所有 9 個功能容器改成 `--log-driver=none`，並由 inspect 驗證；失敗診斷只看安全 DB check code/OOM，
不讀 raw logs。沒有改正式服務的 logging。

以相同 runtime 命令重新完整測試，exit 0；最終 artifact：
`evals/rag-upgrade-v1/runtime/u0-kis-persistence-3a26cddc942b47b6bc4debaa4d84e833.json`，
SHA-256=`618d5862a6c2cad36deb2e3783917b9714ec19d4ccc2afc2671b7c33f028d518`。
captured=2026-10-04T02:57:07.959161+00:00、elapsed=125.512 s；同樣 40 文件／80 chunks/points、
40 replay／40 self-query／1 unauthorized-query，cleanup 全通過；全部 tools SHA 與目前版本一致。
`make lint format-check test validate` exit 0，34/0/0/0；runtime report 核對與無殘留檢查 exit 0。
U0 整體狀態／限制不變，沒有呼叫 Groq。

## 2026-10-04 結構可用性盤點

命令（eval cwd）：`PYTHONDONTWRITEBYTECODE=1 make structure-audit-docker`，exit 0，
passed/failed/skipped/errors=N/A（盤點，非問答測試）。Artifact：
`evals/rag-upgrade-v1/runtime/u0-structure-audit-802a3f7bab64462cb8c61a3b22d36470.json`；
SHA-256=`bf3668c750d19612a7a86033d31f9874baaab95b41a9fbcb10196032050572ab`。
captured=2026-10-04T04:33:43.321903+00:00；elapsed=15.472 s，非 end-to-end latency。

- 相同 immutable Ingestion/KIS images，installed Python 3.11/x86_64；兩個 non-root/read-only/
  network=none/log-driver=none 容器，唯讀 eval/cache，0 DB/HTTP/LLM/download，exact-ID cleanup 通過。
- 新 probe 重新產生全部 40 文件／80 chunks，content/section SHA、tokens、page 完全匹配
  已完成的 `3a26cddc...` 歷史入庫 artifact。舊 tmpfs 不存在；不是現在 DB 的重新查詢。
- 七個 installed module hash 與 workspace 對應 source hash 全一致，核對 exit 0；
  只證明這七個受檢模組，不宣稱整個 image 全部與 workspace 同 build。

| 範圍 | 實際結果 | 限制 |
|---|---|---|
| PDF 10 份／20 chunks | 30 列 anchor 與欄位文字順序、40 個步驟順序保留；20 個表頭／頁碼 | 0 table blocks、0 section；只是每頁線性文字，無 table ID／跨頁續接／cell 型別 |
| DOCX 10 份／20 chunks | 30 列欄位文字順序、10 parser table blocks、10 section-bearing chunks | Markdown-like 文字；block_type 不進 chunk；無可靠頁碼、row/cell identity |
| MD／TXT 20 份 | 40 chunks hash/page/token 全對上 KIS；MD 30 chunks 有 section | 不代表一般 Markdown table 或 TXT 清單完整性 |
| KIS installed schema | ChunkRequest、chunk ORM、search response 都沒有 typed table/row/cell/continuation 欄位 | JSONB custom_metadata 不是已實作、版本化、可枚舉的結構契約 |

`inventory_status=PASSED` 表示現況盤點完整，不表示資料結構能力 PASS。
60 列 linear text 共現與順序是本次小型 fixture 的觀察，不證明大型表、合併格、重複／缺頁／
部分 ACL 完整性，不自動升格為人工 Gold、視覺 QA 或正式 row lineage。
typed 結構缺口已具體確認，留給 U2/U5 的契約／實作；U0-03 仍 in_progress，人工可用性 QA 未完。

新增工具：structure_probe.py、structure_audit.py、tests/test_structure_audit.py；
沿用既有 isolated resource guards，但不改舊 audit tool，因此舊入庫證據仍可校驗。
`ruff format` 三個新增檔 exit 0；`PYTHONDONTWRITEBYTECODE=1 make lint format-check test validate`
exit 0；47 passed／0 failed／0 skipped／0 errors，其餘命令 N/A。
第一次 runtime 因本輪 Docker socket 尚未授權而 make exit 2，建立 0 容器，歷史 artifact
`u0-structure-audit-4a4a2363e1b144c59708bffc52496c6e.json` 保留；授權後成功實跑，不 skip。
`docker ps -a --filter label=helpdesk.u0.isolated-run` exit 0，無殘留。
結束回歸 `PYTHONDONTWRITEBYTECODE=1 make validate lint format-check test` exit 0，47/0/0/0；
105 項 implementation manifest／report/tool/dataset hash 核對 exit 0，mismatch=0。
185 項 production 指紋核對 exit 0，changed=0；KIS 四個正式容器仍 Running、StartedAt 2026-10-01。
這些是唯讀證據核對、passed/failed/skipped/errors=N/A，不是額外 RAG tests。

本次不是獨立 review；沒有修改 production、公司資料、key/policy 或 Gold。
規格偏離：無。品質 gate 仍 BLOCKED；U0-01/03/04=in_progress、U0-02=not_started、next=U0。
仍需題庫多樣化／人工 Gold／QA、safe collector／relevance、確認免費額度後的真生成、
人工評分、效能基線／ARM 相容性。不得開始 U1。

## 2026-10-04 v2 多樣化、rendering QA 與 tuning KIS 候選

以上 v1 歷史保留；本段是新版本，不能把 v1 的 80 chunks／171 tokens 結果套到 v2。
v2 manifest SHA-256=`09c9185ff12a773ab623be1f61ef03ddfc594f5ce28148f0ad5668a61e3b1ad2`，
父版 hash=`40794e4027e12221c38710d7aa86da439b782314b07ffc6923b27f68751a1a9b`。
位置 `evals/rag-upgrade-v1/revisions/u0-synthetic-v2/`，87 個資料檔，版本仍 draft，未 freeze。

- 保留 80 題／8 類／40 tuning+40 holdout、40 文件／4 格式、2 領域與來源 group 隔離。
  可回答計數為 0/1/2/3/5/33/36，同名同代碼不同單位以 `(unit_id,item_code)` 區別，重印列不重計。
  四個 list/count Gold 因缺頁／部分可見而拒答。部分可見仍是文字情境，不是已執行的 ACL 撤銷測試。
- 原始 sidecar 全部 external_llm_allowed=false、synthetic=true、ACL only rag-u0-fixtures。
  Human Gold pending=80、安全 catalog executed=0、真實 Provider runs=0；沒有正式品質分數。
- Host parser-only artifact `runtime/u0-v2-host-parser.json`，SHA-256=
  `f5f9bfe2c5b8f0a91814450dff874b3f794daf7dafdec2de903d548e149ee264`。
  272 列 linear cells／PDF 頁碼／必要 anchors 保留；host Python 3.13.9，不冒充 Docker 3.11。
- 實作端視覺 QA：27 PDF 頁+17 DOCX rendered 頁，共 44 頁逐頁查看。
  `runtime/u0-v2-visual-qa.json` SHA-256=
  `c318a1990236929136267708e3116febbfc81b9dc1dbabf56d11f8181518111e`，綁定 20 個來源及全部 PNG/PDF hash。
  DOCX 修正只在 workspace Fontconfig 與草案 Title 裝飾；來源 PDF 與原草案 bytes 相同。
  `PASSED_RENDERING_ONLY` 不代表人工 Gold、independent review、任意跨欄／合併格解析通過。

### 新版隔離入庫及候選：實際證據

命令 cwd=eval 目錄：
`U0_ALLOW_ISOLATED_WRITES=1 PYTHONDONTWRITEBYTECODE=1 make kis-persistence-v2-docker`，最終 exit 0。
artifact `runtime/u0-v2-kis-persistence-fdea2aa4a85f4a4ab80bfd655e3c5218.json`，SHA-256=
`b51c99aab846f1a33df10edd2de81ca76588e05550930bc67f8fec9203ab6670`。
captured=2026-10-04T06:13:15.540051+00:00、elapsed=137.816 s；這是整批功能測試時間，非單題 latency。

| 範圍 | 結果 | 不代表什麼 |
|---|---|---|
| installed Ingestion → KIS HTTP → CPU Worker → PG16/Qdrant | 40 文件／40 版本、40 jobs completed、0 failed；91 chunks／91 points／384 維，canonical mismatch=0 | 不是 Ingestion schema/job/scheduler E2E |
| Chunk/source binding | max 433 tokens（類別 350/450/50/512）；362 anchors 全對 canonical chunk；272 線性列同 chunk 欄位共現、頁碼無誤 | 不是 typed table/row/cell/continuation／人工 Gold |
| 冪等／搜尋探針 | 40 replay、40 self-query、1 unrelated-group 回空 | 不是 80 題召回品質／40 安全 cases 通過 |
| 實際 tuning KIS search | 40 題各一次，limit=30、每题 30 筆、共 1,200 筆；question hash 與資料集一致，全部 hits 對上該 run canonical mapping | 不是 Retrieval 實際 candidate limit 或 selected context，沒有 followup history |
| KIS HTTP timing | warm、sequential、nearest-rank p50=125.431 ms、p95=195.892 ms | 不是 Chat/Retrieval/Answer E2E／3 runs／30 人負載／Oracle ARM |
| 清理與隔離 | 9 本次容器 non-root/read-only/no ports/log-driver=none；internal network；exact-ID cleanup PASSED、無殘留 | 不移除既有容器、cache、volume 或 image |

14 個工具 hash／dataset／父版／91 個 mapping／40 個 unique case IDs／全部候選
UUID/hash/rank/finite score/治理及 p50/p95 已重新核對，exit 0。artifact 不含原文、query、answer、
path、URL、attestation 或 key；candidate question 僅 hash，可由自製題庫 case ID 取回。
9 個容器和 network 已移除，UUID 僅作歷史關聯；新 collector 需自己的 run，不呼叫舊 UUID。
沒有 model download、公司資料、外部 LLM、付費呼叫或 production 修改。

### 命令與失敗歷史

| 命令／核對 | exit | passed / failed / skipped / errors |
|---|---:|---|
| `PYTHONDONTWRITEBYTECODE=1 make validate validate-v2 lint format-check test`（最新回歸） | 0 | test 70 / 0 / 0 / 0；其餘 N/A |
| v2 runtime（初次 KIS-only） | 0 | N/A；artifact 25559758…，無 tuning collector，不套用新 tools hash |
| v2 runtime（加入 tuning collector 的首次） | 2 | N/A；artifact d9ce35b9…，check=private_kis_client_required，cleanup 全成功 |
| v2 runtime（修正後完整重跑） | 0 | N/A；artifact fdea2aa4…，上述 40 題 tuning collection 通過 |
| 未設 U0_ALLOW_ISOLATED_WRITES 的 controller | 2（預期拒絕） | N/A；0 資源，不當成 runtime PASS |
| report/tool/source/QA 綁定、185 production 指紋核對與 run-label 殘留檢查 | 0 | N/A；changed=0、44 QA 頁 hash 相符、無資源殘留 |

collector 的首次失敗是 HTTPX 的根 URL 字串省略 `/`，而 parsed path 為 `/`。
改用 parsed scheme/host/port/path 與無 credentials/query/fragment 檢查，補正／反例測試；
仍只准 http://kis-api:8080 的根 endpoint，不放寬網路或規則，不改正式服務。
失敗 artifact SHA-256=`b9a93c591f0f76848e63c55efb5e52d58e96adf020f8fb5a2af9ac615b20f10a`，保留不覆寫。
另外唯讀診斷有 path/cwd／不存在 bootstrap.py／inventory .json 假設錯誤（exit 1）；
已改讀实际檔案及 inventory log，不將初次錯誤命令當驗收通過，也不隱藏歷史。

本段非 independent review；規格偏離：無。U0-01/02/03/04 都 in_progress，
review=not_started、next_eligible_phase=U0、quality_gate=BLOCKED。
多樣化／rendering QA／KIS 候選缺證據已解除；仍需人工 Gold/freeze/relevance、
實際 Retrieval context、分段歸因、免費額度及測試治理確認後的真實生成／人工答案評分、
安全案例、端到端資源／ARM 相容性與獨立 review。本輪仍暫不呼叫 Groq，不進 U1。

## 2026-10-05：實際 Retrieval restricted selection 與資源基線

以下都是自己的隔離 run，不連正式 DB、不改正式 limits。原始 sidecars 仍外送 false。
使用已安裝的 RetrieveEvidence／KnowledgeIndexHttpClient，Observer 只委派／觀察，
不重寫 selector、不 monkeypatch、不 fake KIS；Retrieval runtime 沒有直接讀 PG/Qdrant。

| 證據 | 完整結果 | 邊界 |
|---|---|---|
| 41103591…單次trace | 40 tuning×1；candidate limit24，共960 candidates／320 selected；全部external_generation_blocked、allowed0；p50/p95=117.903/150.5ms | installed usecase＋real KIS HTTP，不是Chat/HTTP route/identity/Answer；不含history |
| a7140183…三client lanes | 40 tuning×3＝120 requests，共2,880候選／960選定；p50/p95=213.139/248.492ms；load wall32.138s | 所有runs保留，不重試lane；client lanes3不等於server embedding並行3；不是模型生成 |
| 同一私有cgroup window | 33.881981s；API/PG/QD平均CPU cores 0.923318/0.010857/0.015650；API throttled122 periods | 包含readiness與snapshot overhead；每容器1CPU的測試配置，不外推production吞吐 |
| cgroup lifetime peaks | API893.109MiB、PG80.336、QD88.543；client程序peak RSS51.449MiB | container lifetime含setup，不是純load峰值；各容器peak不可相加當同時RAM／Docker VM峰值 |
| 真實入庫與隔離 | 40文件/版本/jobs，91chunks/points384維，0failed/mismatch；PG16 migration exit0；10容器/net exact-ID cleanup passed | 自製tmpfs已丟棄可重建，無ports／lognone／non-root／readonly；不碰正式volume/image |

單次報告 `runtime/u0-v2-retrieval-restricted-411035912d1c430dafc4b7e321860564.json`，SHA=
`0e98cf7400fcdcdf42ee408a6210717456d772fd0cce3568028a5566b649a24b`。
該controller SHA屬歷史版本；後續加load模式，不拿舊report宣稱目前controller未變。
最終資源報告 `runtime/u0-v2-retrieval-restricted-load-a7140183cdf74f999c329ffc8f2f767d.json`，SHA=
`e09a15cc10997150bb1fa8f13205e8bc178df3282c95e6ee588659fcfd9724ec`，exit0，
captured=2026-10-04T16:54:38.994901+00:00（台北10-05），整批elapsed184.51s。
18工具SHA、9installed Retrieval模組、120唯一case/run、原題SHA與本run canonical mapping全核對。
候選及選定 rank、finite score、p50/p95與cgroupCPU單位皆重新計算；0raw敏感欄位。
固定 `attestation=NOT_ISSUED_RESTRICTED_ONLY` 是狀態，不是 token。

實際policy白名單：defaultlimit8、multiplier3、maxcandidate50、per-document30、allowedbudget3000；
未取得Chat retrieval_limit，不宣称Chat每次一定用8。HTTP retry setting記為1，但attempt實數
未instrument；排除原因 `NOT_OBSERVABLE_IN_UNMODIFIED_V1_USE_CASE`，不用猜測取代觀測。
沒有allowed context／簽章／Answer，不對restricted內容計生成品質或宣布Recall/nDCG。

### 命令與失敗紀錄（保留）

所有命令cwd=`/Users/markhuang/Documents/Codex/2026-07-23/new-chat/evals/rag-upgrade-v1`。

| 命令 | exit | passed / failed / skipped / errors |
|---|---:|---|
| `PYTHONDONTWRITEBYTECODE=1 make lint format-check test validate validate-v2`（人工核對工具完成後） | 0 | test88/0/0/0；其餘N/A |
| `PYTHONDONTWRITEBYTECODE=1 make lint format-check test validate-v2`（readiness guard更新後） | 0 | test90/0/0/0；其餘N/A |
| `U0_ALLOW_ISOLATED_WRITES=1 PYTHONDONTWRITEBYTECODE=1 make retrieval-restricted-v2-docker` | 2，後0 | N/A；e784… readiness不足，後4110…完整成功；各10容器/net cleanup成功 |
| `PYTHONDONTWRITEBYTECODE=1 make lint format-check test validate validate-v2 gold-review-v2`（初次load工具） | 0 | test100/0/0/0；其餘N/A |
| `U0_ALLOW_ISOLATED_WRITES=1 PYTHONDONTWRITEBYTECODE=1 make retrieval-load-v2-docker` | 2，後0 | N/A；5ca8…命令guard失敗，後a714…完整成功；不跳過／降低guard |
| `PYTHONDONTWRITEBYTECODE=1 make validate validate-v2 lint format-check test gold-review-v2`（最新） | 0 | test101/0/0/0；其餘N/A；pipefail/tee log含checks_exit=0 |
| Report/QA/185production hashes與run-label殘留核對 | 0 | N/A；44頁/source hash相符，production changed0，無隔離殘留 |

最新工具log `runtime/u0-v2-retrieval-load-checks.log`。初次trace有E501四行，修正後完整重跑。
e784…report SHA=`f4edae53745bbdff3bdbd98726143b062a7e5caaa9d0a12e41119cb33a9ed845`，
readiness polling工具限定60秒，不改HTTP search retry。5ca8…SHA=
`e839e7ba5eb419566194e597b7c2d417d9be0a6293fd574688f4831db532074e`，check=docker_command_failed，
沒有OOM診斷；三份完整trace的可重現序列化預估2,032,257bytes超過2MB出口guard。
compact projection保留所有run的ID/rank/score與共享來源映射，新輸出475,916bytes；2MB上限不變。
唯讀核對初次錯判NOT_ISSUED marker／漏corpus split路徑exit1，修正核對後exit0，artifact未更動。

### 完成稽核與尚缺證據

| 明確要求 | 現況證據 | 判定 |
|---|---|---|
| 80題／8類／40+40 source split、兩領域四格式、逐題Gold欄位 | v2 manifest/datasets validator exit0；44頁rendering QA | 結構完整，人工語意／freeze仍未完成 |
| 人類Gold | hash-bound核對表80pending；使用者表示自己核對，尚未提交approved紀錄 | 未完成，不代簽 |
| 原始問題→候選→選定→答案與逐段失敗分類 | 40tuning真KIS／restricted Retrieval，原題SHA/canonical映射；Answer0 | 只完成前三段的restricted觀察，allowed/生成與分類未完成 |
| 真模型80題×3、人工答案與citation評分 | synthetic-only免費授權已取得；帳戶方案名稱尚缺；actual calls0 | 未完成，不用Fake／工程測試替代 |
| PDF/DOCX表格／跨頁／章節是否存在KIS | installed schema、91 canonical chunks、272線性列/362anchors、44頁QA | U0-03現況盤點完成；typed結構缺口留U2/U5，不代表能力通過 |
| CPU/RAM/latency/模型套件相容與候選建議 | installed3.11/x86CPUembedding/parser、120次restricted資源基線、primary-source候選比較 | 部分完成；缺完整E2E/Provider/ARM／新候選adapter證據 |
| ≥40安全案例、獨立review | catalog40、executed0；未派reviewer、未簽review | 清單存在，執行／review未完成 |

使用者貼出Groq數值1K/500K/250K，未含欄位與方案名稱，不推論是Free Plan。
2026-10-05查閱[Groq官方限額](https://console.groq.com/docs/rate-limits)：實際organization limits
應在帳戶Limits確認；公開表不是個人帳戶免費證明。試驗仍暫不呼叫外部模型／不改付費方案。
隔離synthetic external_llm_allowed=true的核准尚未收到，所有sidecar/測試資料仍false。
U0-01/02/04=in_progress、U0-03=completed（inventory only）；review=not_started、next=U0。
不是獨立review，不宣告P0/P1/P2/P3=0；規格偏離無。完整U0品質gate仍BLOCKED，不進U1。

## 2026-10-05：人工相關性與正式 Recall/nDCG 評估工具

新增 `retrieval_metrics_v2.py`、`tuning-relevance-v2.template.json`、
`relevance-protocol-v1.md`、`tests/test_retrieval_metrics.py`，只在 eval 範圍。
不是新 API／query processor／reranker，也沒有改舊 runner 的 anchor proxy 來冒充正式指標。
核對方式是草案，真人逐題接受 protocol、核對完整 catalog 後才能 approved。

- 輸入是 a714… 已清理的真實隔離 report，文件 SHA 維持
  `e09a15cc10997150bb1fa8f13205e8bc178df3282c95e6ee588659fcfd9724ec`。
  離線投影完整 40 次 KIS limit30 與 120 次 actual Retrieval depth24；沒有重跑 Docker/HTTP。
- 91 個 canonical chunk identity 綁定 fixture/index/page/token/content/section hash，排除臨時 UUID。
  同 bytes 的新 run 可以對應；dataset／chunk 改變就拒沿用舊 labels，不能默默重配。
- 40 題 tuning 的 case hash、所有來源 catalog、protocol 都固定；grade 0–3 必須是真正整數，
  未標註為 null。default0 僅在真人明確核對全 catalog 後生效，不將未見 chunk 自動判無關。
- 必要入口可有等價 chunks，但不同入口不重複計數。KIS Recall@30、Retrieval Recall@24 分開。
  清單入口召回不等於全部 row/context 充分率；refuse/clarify 不進可回答題入口召回分母。
- nDCG@10 IDCG 使用全部人工判斷宇宙；未召回的高相關 chunk 仍影響理想排序。
  全零相關題為 N/A，報告分母與排除數，不给 1。完整 1／3 runs 保留，不挑最好。
- 表單不含已核准分數／真人；現在 relevance 40 pending、Gold 80 pending。
  CLI check 只驗結構與 trace，score 缺任何核准就 exit2/BLOCKED，不輸出部分分數。
  已核准也不認證真人真的閱讀、不自動 freeze，不把工具成功當 U0 PASS。
- 不外送、不連 DB/Docker、不讀 secret/.env、不保存新 raw query/evidence/answer；
  輸出 allowlist 不包含真人資訊、notes、path/URL 或 attestation。2MB input bound 保留，
  拒重複 JSON keys、NaN/Infinity、漏題/run、重複/外來 chunk 及治理／canonical 不一致。

完整定義見 [人工相關性核對方式](../evals/rag-upgrade-v1/relevance-protocol-v1.md)，
公式來源 [Stanford IR ranked evaluation](https://nlp.stanford.edu/IR-book/html/htmledition/evaluation-of-ranked-retrieval-results-1.html)。
這不是 holdout tuning 回饋；holdout queries=0、provider calls=0，沒有計算任何正式品質分數。

| 命令（eval cwd） | exit | passed / failed / skipped / errors |
|---|---:|---|
| Ruff format 新 Python 工具/tests | 0 | N/A，機械格式整理 |
| Ruff check 新工具初次 | 1 | N/A，import 排序；拆開 import 後修正，未降規則 |
| `PYTHONDONTWRITEBYTECODE=1 make lint format-check test relevance-review-v2` | 0 | test123/0/0/0，其餘N/A |
| `PYTHONDONTWRITEBYTECODE=1 make validate validate-v2 lint format-check test gold-review-v2 relevance-review-v2` | 0 | test123/0/0/0，其餘N/A；pipefail/tee、checks_exit=0 |
| `PYTHONDONTWRITEBYTECODE=1 ../../ingestion-service/.venv/bin/python retrieval_metrics_v2.py score` | 2（預期拒絕） | N/A，真人核准不足，沒有分數；不是品質 gate 通過 |

新 log `runtime/u0-v2-retrieval-metrics-checks.log`，SHA-256=
`bf99608952771d2a7fe52c8a58d3114aa813993283d810dc8016788532ca2a75`。
22 個新增 tests 使用 toy rankings/記憶體 approval records 驗數學和防護，不保存假真人紀錄，
不當作業務、安全 catalog 或實際生成的通過數。舊 101 tests／load log 保留。

Completion audit：正式指標**工具**已實作，不等於固定人工標註／正式分數完成。
U0-01/02/04 仍 in_progress、03 inventory completed；review not_started、next U0。
人工 Gold/freeze/relevance、免費方案／治理核准後的 allowed/Answer/240 次生成與人工評分、
安全 cases、完整效能／ARM 與 independent review 仍未完成。規格偏離無；不是獨立 review。

## 2026-10-05：ARM CPU 相容性工具與未通過的實跑

新增 `arm_cpu_audit.py`、`arm_cpu_probe.py`、`tests/test_arm_cpu_audit.py`；
eval Makefile 加入 `arm-cpu-audit-docker`。只使用本機既有 ARM image 與唯讀 tokenizer/model
cache，不 pull/build/download、不改 production、不連 DB 或外部模型。
預定依序驗 installed Ingestion 的完整 40 文件解析/chunk parity 與 KIS 的實際 CPU embedding。
目前在 Intel Docker 上進行 ARM 模擬，不是 Oracle 原生效能測試。

- 實跑命令（eval cwd）：`PYTHONDONTWRITEBYTECODE=1 ../../ingestion-service/.venv/bin/python arm_cpu_audit.py`。
- run `428240d7791343d699107ee072725f50`：exit 2、status BLOCKED，首個 Ingestion probe
  到 360 秒 deadline 未完成，整體 362.414 秒；`error_type=TimeoutExpired`。
  沒有完成 snapshot 或來源 hash/parity 驗證，KIS ARM embedding 尚未執行；不得宣稱 ARM 相容通過。
- image `sha256:b9c482c471920a425b06d8b8d3b0ebf9a0cec19c1ffb06ebc2e4f3b9bb5b1786`
  inventory 為 linux/arm64、user app。inventory 不等於 installed source/adapter 實跑通過。
- 本次 1 個專用容器：network none、non-root、rootfs read-only、cap-drop、no-new-privileges、
  log-driver none、1 CPU、768 MiB。診斷未記錄 OOM 類別，但不能推論逾時的確切原因。
  exact-ID/ownership-label cleanup 通過，正確 run label 的唯讀殘留檢查 exit 0／空結果；
  不移除使用者 image、cache、volume、network 或公司資料。
- Artifact `runtime/u0-arm-cpu-428240d7791343d699107ee072725f50.json` SHA-256=
  `27ea055e96e90ec5cd60b0b9323bfe05cb62d0422268cc90f9861a1d2b42bf9a`，失敗證據保留。
  沒有因逾時增加 deadline、跳過 parser、降低檢查或重試來宣稱成功。
- 完整離線命令 `PYTHONDONTWRITEBYTECODE=1 make validate validate-v2 lint format-check test
  gold-review-v2 relevance-review-v2` exit 0；130 passed、0 failed、0 skipped、0 errors。
  `runtime/u0-arm-cpu-checks.log` 記錄 checks_exit=0，SHA-256=
  `159878219d17936f8ba0dffc46f8d338bbf0cb014fa1a21f92f936a875958c15`。
  新增 7 個工具 guards，不當成 ARM runtime、40 safety cases 或業務答案通過數。

來源仍 external_llm_allowed=false、Gold 80 pending、relevance 40 pending；provider calls=0。
完整 U0 仍 in_progress／quality BLOCKED／review not_started／next U0；不進 U1。

## 2026-10-05：ARM 啟動問題定位與相容性單項通過

前段逾時紀錄保留。新增固定 data-free startup stages，把延遲定位到 tokenizer 初始化：
default diagnostic run d8c00d6c… 在90秒期限中止，工具/Counter匯入僅0.863秒、
8個 installed source hashes一致；不是沒有讀到40份文件或憑猜測認定parser壞掉。

Explicit `--single-thread` 只在專用容器設定 OPENBLAS/OMP/MKL/NUMEXPR 的 threads=1。
它是受控資源實驗，不改 production/模型/解析限制或檢索參數。
[OpenBLAS官方runtime變數](https://www.openmathlib.org/OpenBLAS/docs/runtime_variables/)
說明執行緒控制機制；未分別隔離四個變數的影響，不宣稱確定是哪個庫造成故障。
startup b487…與final295662dd…均完成（43.689/45.633秒、exit0/DIAGNOSTIC_ONLY），
診斷本身不算完整 compatibility PASS。

完整 runs 442e74cf…／f5cfda0f…／final ca1b4655…均 exit0/PASSED、exact-ID cleanup PASSED。
實作端 lint修正後產生 final report，不以舊工具 bytes代替current證據；全部4 tool hashes一致。
最後report：`runtime/u0-arm-cpu-ca1b4655dfed483fb8ec86ca825e9064.json` SHA-256=
`cb52932c5a1ed581818ea3e881ed0edc7234de912e09f77cad8eb0ad7fbebaeb`。

| 實跑範圍 | final結果 | process資源／時間 |
|---|---|---|
| installed Ingestion／Python3.11.15 aarch64 | 40文件／91chunks；8source SHA、全部content/section SHA、page、token parity一致；缺必要anchor0 | peak RSS604,610,560bytes；63.449秒 |
| installed KIS／同架構 | 4source SHA一致；真E5 pinned revision CPU embedding；query/document各1，384維、finite normalized、>512token拒絕 | peak RSS1,111,076,864bytes；207.436秒；新instance model-load149.629秒、2warm encode11.331秒 |

套件：pypdf6.15.0、python-docx1.2.0、transformers5.15.0、tokenizers0.22.2；
embedding另有torch2.11.0+cpu、sentence-transformers5.7.0，cuda_version=null。
E5 model revision仍 `614241f622f53c4eeff9890bdc4f31cfecc418b3`，沒有換模型。
只证明被實跑adapter／模組，不是所有ARM服務或新候選reranker已通過。
Intel ARM模擬不是Oracle原生效能；process RSS不是cgroup peak；新instance model-load不是OS
cold-cache。final尾段有startup診斷重疊，時間只是run記錄，不拿來作效能比較或容量外推。

final full 2容器與final startup1容器各自readonly/non-root/network none/log none/noports，
cleanup全通過，所有本次run labels查無殘留；舊timeout和兩個先前完整success artifacts仍保留。
沒有公司資料、DB/provider calls、model downloads；來源external_llm_allowed仍false。

完整驗收 `PYTHONDONTWRITEBYTECODE=1 make validate validate-v2 lint format-check test
gold-review-v2 relevance-review-v2` exit0，133 passed/0failed/0skipped/0errors，其他N/A。
log `runtime/u0-arm-reconciled-checks.log`含checks_exit=0，SHA-256=
`cf78116dec339fa40f59a9a3ac38d6a28c3525dc9a4fe557ae0e82fdf0c9c3ca`。
最初新增負數時間guard test為132/1/0/0、exit2，修正後通過；lazy-loader的B009/B018/E501
三個lint失敗紀錄保留，未降低規則、skip或刪test。新targets的`make -n`僅dry-run，不冒充實跑。

U0-04相容性子項的阻擋已解除，但完整問答/Provider資源基線仍缺；人工Gold80與relevance40
未核准、freeze未做、免費Groq方案與測試治理未確認、240次生成/人工答案citation評分與
independent review未完成。U0仍in_progress／quality BLOCKED／review not_started／next U0。

## 2026-10-05：資料／人工判斷凍結工具（正式 freeze 尚未執行）

新增 eval `freeze_dataset_v2.py`、`tests/test_freeze_dataset.py`，Makefile 新增
`freeze-check-v2`／`freeze-dataset-v2` 與明確 review 檔參數；使用方式見 eval README。
只做 U0 工具，沒有修改服務、來源／sidecar、真人記錄或加入 Provider 呼叫。

- 完整核對 v2 的 87 檔 manifest、80 Gold、40 tuning relevance、91 chunks catalog／真 trace；
  不以檔案存在、status 字串或自動 anchor/score 代替真人 checks。
- 綁定 dataset file SHA、逐題與 human records 的 canonical JSON SHA、a714… source report 的
  canonical SHA、protocol 文件／validator file SHA。file SHA 與 canonical SHA 明確分開。
- source report 保持原 raw SHA `e09a15cc10997150bb1fa8f13205e8bc178df3282c95e6ee588659fcfd9724ec`；
  canonical SHA `556b169f7c3bde6a2d002ff6eed00e3feaf1f3ecfd25447a3fccd527e9879234`，未換資料。
- 預設 pending 表單下，check/freeze 都 exit2，80/40 pending、approved0；目錄／快照未建立。
  enabled writer 只用 memory toy approvals／TemporaryDirectory 測試，不能當真人核准或正式 freeze。
- 原子 no-clobber／0600／dir_fd/NOFOLLOW、exact owned-temp cleanup；verify 重新核對完整 inputs。
  拒未知欄位、boolean/number 混淆、tamper、來源/標註/protocol/validator 改變、重複 JSON keys、
  NaN/Infinity、超過2MB、非法 ID、symlink、非 regular file/FIFO；兩個 concurrent writers 只有一個成功。
- 只保存 synthetic case IDs／hash/count/time，不轉抄 reviewer/notes/query/answer/evidence/
  實體 path/URL/key/attestation。不是模型設定凍結、語意認證或品質 PASS；生成仍 NOT_MEASURED。

命令 cwd=`/Users/markhuang/Documents/Codex/2026-07-23/new-chat/evals/rag-upgrade-v1`：

| 命令 | exit | passed / failed / skipped / errors |
|---|---:|---|
| 新檔 Ruff format | 0 | N/A |
| 初次 `PYTHONDONTWRITEBYTECODE=1 make lint format-check test` | 2 | N/A，UP017/I001，未進 tests |
| 新檔 Ruff check `--select I,UP017 --target-version py311 --fix` | 0 | N/A，機械修正；未降低規則 |
| 第二次相同 make checks | 2 | N/A，未用 timezone import；移除後修正 |
| 第三次相同 make checks | 2 | 150/1/0/0；新測試把 `no_answer` ID 誤作內容洩漏 |
| 完整 `make validate validate-v2 lint format-check test gold-review-v2 relevance-review-v2`（相同 env） | 0 | 最終155/0/0/0；其餘N/A |
| `PYTHONDONTWRITEBYTECODE=1 make freeze-check-v2` | 2 | N/A，預期 pending 拒絕；未 freeze |
| `PYTHONDONTWRITEBYTECODE=1 make freeze-dataset-v2` | 2 | N/A，預期 pending 拒絕；未寫入 |

失敗 test 改為遞迴檢查真正欄位與實際問題／reviewer 值，沒有放寬內容邊界或刪除測試。
新增22個工具 tests，最初18個，再補 parser/FIFO/concurrency 與 protocol/validator drift。
回歸先154/0/0/0，再補 fingerprint guard後155/0/0/0；71 Python files format-check 通過。
log `runtime/u0-dataset-freeze-reconciled-checks.log` 有 checks_exit=0，SHA=
`fe1ebe0989d772005e509c34c231b3c38cbb0fdbb511bc8d5334bce52a080e80`。
先前154 log保留，SHA=`dde32c256cc2185ceb9104c689d431a681986e51e645f70d6611bb658a4fb324`。
最終 pending guard log `runtime/u0-dataset-freeze-final-pending.log` SHA=
`ed589720b8c5155ecfb86a1f0aa5086f98f14c6939b23f79c05e78888aa8af7a`；
兩個 target 各exit2，外層檢查預期拒絕的 wrapper exit0，只表示拒絕路徑符合預期。

唯讀 production fingerprint 核對185檔 changed0；初次用錯相對 base 的診斷exit1，改workspace
base後exit0，沒有修改檔案／忽略 mismatch。`runtime/reviews/`、`runtime/frozen/` 均不存在。
沒有 Docker/DB/provider calls 或 model downloads；不重跑已通過的 ARM／KIS 資源 probes。
本次不是獨立 review；規格偏離無。U0-01/02/04=in_progress、03=completed_inventory_only。
真人 Gold/relevance、正式資料及 runtime freeze、allowed/Answer、Groq 免費方案確認後的真生成／
人工分數／逐段歸因／Provider資源與 independent review 尚未完成；不得進 U1。

## 2026-10-06：U1 文件設計進入例外與 U0 補證保留

使用者確認「對Ｕ1」，先進入契約／安全設計，不再擴建 U0 工具。
現況盤點完成；U0 的 U0-01/02/04 仍 in_progress、U0-03 completed（現況盤點）、review not_started。
回答品質 NOT_MEASURED、實際模型回答／引用0/16、U0真生成0；正式80 Gold／40 relevance仍 pending。
以上不因 U1 草案建立而自動 approved／PASS；沒有對未測項目補造數據。

[契約草案](./rag-upgrade-v1-contracts.md) 提出 v2 契約、CPU/keyword/parser 候選、有限 history／追問、coverage／signed facts、總預算及回退策略；尚待使用者決策與獨立 review。
舊 baseline 在第一個 production 行為修改前須保全可重現版本；U3比較前補相應檢索／人工證據，U6正式release前補真生成／分段基線及U0 review，精確期限以升級計劃 `u1-design-entry-v1` 為準。
本次只文件更新：沒有改服務、模板、題庫、sidecar、ACL、policy、.env、DB或Docker，沒有外部模型呼叫／下載／付費。
`PYTHONDONTWRITEBYTECODE=1 make validate-v2` exit0僅代表80題／40文件的結構與hash核對；正式Gold仍80pending、validator quality_gate=BLOCKED。
185個原production指紋未變；未重跑155個工具tests，不增算passed數或宣告U1completed。
