# U0 問答品質基線工具

這裡建立可重複的測試材料、只讀盤點與 opt-in 隔離入庫工具，不升級既有服務。
目前是 **U0 品質補證 in_progress／回答品質 NOT_MEASURED**，不是 RAG 品質已驗收通過。

2026-10-06 使用者核准先進入 [U1 文件設計](../../docs/rag-upgrade-v1-contracts.md)，不繼續擴大 U0 工具阻擋設計。
本目錄的原始資料、人工 pending、歷史 logs 與嚴格 validators 均保留；本次不執行生成或 freeze。
舊 baseline 在第一個 production 行為修改前保全；U3/U6 正式比較前補相應人工與真實品質證據。
精確進入例外與補證期限見 [升級計劃 v1.2](../../升級計劃v1.md) 的 `u1-design-entry-v1`。

## U0 收尾原則（2026-10-05，原品質流程保留；目前先做 U1 文件）

使用者已核准精簡 U0（`u0-preliminary-sampled-v1`）：沿用現有 80 題 v2，真實問答仍每題 3 次；
真人只抽查 **16 題（8 類各 2，tuning/holdout 各 1）及確認有疑義的異常**，不必評完 3,640 個 chunk。
直接看 [16 題核對表](./u0-human-sample-review.md)，回覆「正確／有問題」即可，不需要編輯 JSON。
2026-10-06 使用者回覆「檢查完了 OK」，16/16 來源與預期答案已核對；實際模型回答／引用仍 0/16、尚未生成。
生成後在同一題列出三次回答／引用，不必重做來源核對；本次不改正式 80/40 人工表單或執行 freeze。
品質补證仍須最小生成收集、80×3 全部真實觀測、AI provisional 診斷／人工抽查與分段 baseline，最後獨立 review；不再是 U1 文件設計的前置阻擋。
完整人工 Gold、全量 relevance、正式 Recall/nDCG 與 human-label freeze 延至相應 U3/U6 正式驗收，不阻擋精簡 U0；不修改舊 validators 或捏造 approved。
資料／抽樣 ID／模型／Prompt／policy／limits／硬體仍须在生成前固定，不按結果換題。
已完成且未變更的 probes 不重跑；U6 release 分數與新功能安全 gate 不提前當成 U0 的實作任務。
使用者已確認 Groq **Free Plan**，提供 openai/gpt-oss-20b 的 RPM=1K／RPD=500K／TPM=250K，其餘欄 `-`；實際 API／剩餘 quota 尚未測。
synthetic-only 免費生成已獲授權，無須再索取同一資料的隔離 flag／免費方案確認；不改來源 sidecar 或公司設定，遇限額或不能保持免費就停止，不轉付費。
只將實際模型呼叫算作生成；政策阻擋／Provider 錯誤另列。此階段產物稱「初步基線」，不稱正式品質 PASS。
詳見升級計劃的「U0 執行收斂」與 baseline「收尾範圍釐清」；以下歷史證據保留，不代表每一項都要重做。

## 已建立什麼

**版本先分清楚**：本目錄根層的 corpus／datasets／manifest 是保留的 v1 初稿。
`revisions/u0-synthetic-v2/` 是新的多樣化草案；目前尚未人工核准或正式 freeze。
v1 的 80 chunks 入庫證據不能用來證明 v2；每份 runtime report 都綁定自己的 manifest hash。

- 80 題業務題：8 類各 10 題，40 tuning／40 holdout。
- 10 個不重疊的來源群組，分布在規章與維運兩個領域。
- 40 份來源文件：PDF、DOCX、TXT、MD 各 10 份；PDF 有兩頁、重複表頭與跨頁清單。
- 40 項安全／失敗注入案例**清單**。這不是 40 個已通過的安全測試。
- Gold 草案包括來源版本、頁／章節、文字／row anchors、適用範圍、計數單位與完整性要求。
- `manifest.json` 固定每個資料檔 SHA-256，檔案改動後 validate 會拒絕；builder 不覆寫現有資料。

完整 80 題正式人工核對尚未完成；16 題來源／預期抽查 OK 不等於全部核准，不得把正式 `pending` 改成 `approved` 來繞過驗收。
實作 agent 不讀真實 holdout 逐題失敗結果來調參；baseline／U6 的 reviewer 只能提供彙總。
Fixture、gold、parser lineage 本身可查看；它們不是正式公司文件或服務 log。

### v2 多樣化、解析與視覺 QA

v2 仍是 80 題、40 文件、40/40 split、8 類與兩個領域。可回答計數值為
0、1、2、3、5、33、36；包含同名同代碼但不同單位、同列重印、大型跨頁清單。
四個清單／計數案例因缺頁／部分可見而拒答，沒有 exact-total Gold。
「部分可見」目前是文件描述的情境，**不是已執行 canonical ACL 安全案例**。

```bash
make validate-v2
make parser-audit-v2
# 僅在資料／工具变更，需要新的隔離證據時執行：
U0_ALLOW_ISOLATED_WRITES=1 make kis-persistence-v2-docker
```

`make validate` 預設仍驗 v1；`validate-v2` 只驗資料 hash/split/草案一致性，
不讀 runtime QA 或 KIS report，因此其 `NOT_CHECKED_BY_DATASET_VALIDATOR` 不表示未實跑。
`parser-audit-v2` 是 host production parser-only，不是 installed Docker chunk／KIS 證據。

v2 manifest SHA-256：`09c9185ff12a773ab623be1f61ef03ddfc594f5ce28148f0ad5668a61e3b1ad2`。
Host parser 的 272 列線性欄位順序／PDF 頁碼／必要文字標記全通過。
PDF 27 頁、DOCX rendered 17 頁已逐頁由實作 agent 查看：中文可見、表頭／欄位清晰，無裁切重疊。
DOCX 中文缺字是 bundled renderer Fontconfig 鏈路問題，以 workspace-only `qa-fonts.conf`
修正，不修改系統字型、不下載字型或操作使用者桌面 Word；移除非必要 Title 裝飾邊線後重渲染。
證據 `runtime/u0-v2-visual-qa.json` 綁定全部 44 PNG/source hash；PDF 與修正前草案 bytes 相同。
這是 rendering QA，**不是獨立 review 或人工 Gold 語意核准**；80 題人工狀態仍 pending。

v2 入庫初次通過 artifact：`runtime/u0-v2-kis-persistence-2555975826104edc9fce49cb8216d66a.json`。
40 文件／40 版本／91 chunks／91 points、最大 433 tokens；272 列在同 chunk 中可觀察到欄位文字，
362 個必要 anchors 全綁定 canonical chunk，頁碼錯誤與 canonical mismatch 均為 0。
與 v1 一樣是真 PG16/Alembic/CPU Worker/Qdrant，9 個本次容器＋network cleanup 全通過。
artifact UUID 是已清理 run 的歷史證據；不直接拿它向現在的 KIS 呼叫。
v1 tools／corpus/hash 保留不改，v2 沿用隔離 lifecycle 並明確投影 producer/probe invocation。
已另產生含 collector hash 的新 run：`runtime/u0-v2-kis-persistence-fdea2aa4a85f4a4ab80bfd655e3c5218.json`，
SHA-256=`b51c99aab846f1a33df10edd2de81ca76588e05550930bc67f8fec9203ab6670`。
相同 40 文件／91 chunks，40 題 tuning 各 30 個實際 KIS 候選，共 1,200 筆。
候選全核對本 run 的 canonical document/version/chunk／內容與治理，不含原文、path、URL 或 key。
只保存問題 SHA（可由 synthetic case ID 查回題庫）、rank/score/latency/來源 ID 與 hash；holdout queries=0。
這是 KIS limit=30 的前置診斷，**不是實際 Retrieval 的 candidate limit／selected context**。
不改寫、不用 followup history、不調參、不呼叫 Answer/Groq；relevance labels 與 Recall/nDCG 尚未評分。
40 次單次 warm KIS HTTP：p50=125.431 ms、p95=195.892 ms，非三次 runs／正式 E2E／Oracle 效能。
工具回歸 70 passed、0 failed/skipped/errors；9 個本次容器＋network exact-ID cleanup 通過，無殘留。

### 實際 Retrieval 與資源盤點（2026-10-05）

```bash
# 只在需要新隔離證據時重跑；每次建立自己的 mapping 並 exact-ID cleanup。
U0_ALLOW_ISOLATED_WRITES=1 make retrieval-restricted-v2-docker
U0_ALLOW_ISOLATED_WRITES=1 make retrieval-load-v2-docker
```

讀取唯一running Retrieval的15個白名單設定（不讀key/URL）；installed usecase與real KIS HTTP。
私有internalnet/noports、tmpfs PG/QD、唯讀model/eval、non-root/readonly/lognone；不改正式服務。
單次artifact41103591…：40tuning、candidate24/selected8，960/320、allowed0；
p50/p95=117.903/150.5ms。不是KIS limit30 collector或Chat route的trace，不含history/Answer。

load最終artifact `runtime/u0-v2-retrieval-restricted-load-a7140183cdf74f999c329ffc8f2f767d.json`，
SHA=`e09a15cc10997150bb1fa8f13205e8bc178df3282c95e6ee588659fcfd9724ec`，exit0。
40tuning×3、三client lanes、全部120requests與結果保留；p50/p95=213.139/248.492ms。
API/PG/QD cgroup CPU與RAM已量；API lifetime peak893.109MiB，包含setup/readiness，
不當作純load／全DockerVM峰值；client peakRSS51.449MiB。每容器1CPU限制，不推估30人吞吐。
client lanes3不表示embedding server並行3，這不是240次LLM生成或OracleARM效能。

全部候選與選定都經該run canonical mapping核對；報告保存synthetic ID/rank/score/hash，
不含原文/query/answer/path/URL/key或token。load映射共享一次避免突破2MB出口，guard不變。
舊失敗e784…（readiness不足）、5ca8…（command guard）保留且各10容器/net清理通過；
修正後a714…也全部清理。runtime UUID只是歷史關聯，不向現在DB呼叫。
18工具／9受測installed模組指紋全核對，不宣稱整個image同build；185production hashes未變。
HTTP attempts未instrument、排除原因不可觀測，明列而不猜。正式relevance/Recall/nDCG未評分。

前次 `make validate validate-v2 lint format-check test gold-review-v2`（PYTHONDONTWRITEBYTECODE=1）
exit0，101tests/0failed/0skipped/0errors，log=`runtime/u0-v2-retrieval-load-checks.log`。
80humanGold仍pending。使用者會自己核對；表單validator不等於語意簽核／freeze。
U0-03只標「現況盤點completed」，typed結構能力仍留U2/U5，其餘U0項目與整體品質未完成。
仍待 allowed/Answer collector、80×3 真實問答觀測、16題人工抽查／異常確認與獨立review。
免費帳戶方案已由使用者確認；isolated synthetic 外送設定由既有 opt-in 涵蓋，正式 sidecars／公司設定不變。

### 正式檢索指標工具與人工相關性表（2026-10-05）

本節是**後續正式評測**，不是精簡 U0 的前置工作；舊工具的嚴格要求不因抽查版而降低。

已新增 `retrieval_metrics_v2.py`、40 題全 pending 的 `tuning-relevance-v2.template.json`，
以及 [相關性核對方式](./relevance-protocol-v1.md)。它們只離線讀取已驗證的 a714… report，
不連 DB／Docker／Provider、不自動標註、不修改來源或原 sidecar。

```bash
make relevance-review-v2
```

此命令 exit0 只代表表單／catalog／完整 trace 結構正確：40 relevance pending、0 approved，
品質仍 BLOCKED。正式 `score` 要求 40 relevance approved 與 80 Gold approved；
目前執行會 exit2/BLOCKED，沒有發表任何正式分數。
人工表核對全部 91 chunks，不只 retrieved pool；未填 default grade 不會被當成 0。
KIS 的入口 Recall@30 與 actual Retrieval 的 Recall@24 分開；nDCG@10 以完整人工判斷宇宙
形成理想排序，沒有正相關時為 N/A，不給完美分數。全部 40／120 observations 保留。
入口命中不是完整表格列或 selected context 充分率，這個工具也不評估生成答案／holdout。

最新 `PYTHONDONTWRITEBYTECODE=1 make validate validate-v2 lint format-check test gold-review-v2
relevance-review-v2` 全通過，123 工具 tests／0 failed／0 skipped／0 errors；
log=`runtime/u0-v2-retrieval-metrics-checks.log`、checks_exit=0。
新增 22 個數學／validator tests 使用明確 toy rankings，只證明工具正確，不是 RAG 業務分數。
即使未來計算成功也不自動 freeze、品質 PASS 或進 U1；人工語意核對與獨立 review 仍必須完成。

## 第一個要做的事

2026-10-04：inventory、tokenizer probe、40 文件 Docker parser/chunk audit 均已補登 exit 0。
本次 40 文件產生 80 chunks、最大 171 tokens、anchors／token bound／recount 全通過。
另已直接執行隔離 KIS persistence audit，exit 0：40 文件／40 版本／80 chunks／80 向量，
canonical mismatch=0、40 次 idempotent replay、40 次自文件文字搜尋與 1 次未授權搜尋通過。
這些是 U0 的部分證據，不是 80 題問答品質通過；不必重跑下方已成功的命令。
v2 多樣化／rendering QA、KIS candidates 與實際 Retrieval restricted selection 已另補；
U0 品質待辦為 allowed context／Answer、真實觀測與16題實際回答抽查；目前先做 U1 文件設計，完整 Gold/relevance 為後續正式驗收。
以下 80 chunks 是 v1 歷史，不是 v2 最新結果。
結構盤點也已完成：60 列的 linear text、40 個跨頁步驟仍在相同 canonical chunks；
但 table/row/cell/continuation 沒有 typed 儲存／API 契約，DOCX block_type 不進 chunk。
這是已確認的能力缺口，留給 U2/U5；不是要求先實作 U5 才能完成 U0。
以下 inventory／parser-chunk 指令保留供未來資料／工具變更後重新擷取證據。

Docker 開著，KIS、Ingestion、Retrieval、Answer、Chat 五個服務維持你原本的設定。
不用 rebuild、不用改 key、不用切 Fake、不用先把 corpus 放進文件資料夾。

在 Terminal 貼：

```bash
cd /Users/markhuang/Documents/Codex/2026-07-23/new-chat/evals/rag-upgrade-v1
mkdir -p runtime
set -o pipefail
make inventory 2>&1 | tee runtime/u0-host-inventory.log
inventory_exit=$?
printf 'inventory_exit=%s\n' "$inventory_exit" | tee -a runtime/u0-host-inventory.log
```

把輸出貼給 Codex。只讀取得 Docker CPU、RAM、image ID、白名單設定與程式／lock／policy
hash；不列出 key、不 dump env、不呼叫 LLM、不寫資料庫、不刪文件。若有服務缺少或
Docker 連線失敗，exit 2；不能当通過。

工具預設使用已存在的 `ingestion-service/.venv` Python，避免 macOS 系統 Python 3.9。
不會自行 install；缺少 `.venv` 時請提供錯誤，不要直接改成系統 python3。

## 離線工具驗收

```bash
make validate
make lint
make format-check
make test
make parser-audit
```

`make test` 只驗證評估工具：hash、split、漏題、重複 run、Fake 分數濫用、
不合法數值、非 fixture anchor、敏感欄位拒絕、holdout 彙總。不是服務 integration。
`make parser-audit` 唯讀 import 現有 Ingestion parser；不是 HTTP 或 DB 測試。

若本機已有**固定 revision** 的 tokenizer cache，可再跑：

```bash
make parser-chunk-audit
```

這個命令強制離線，不下載模型。沒有 cache/dependency 時回 BLOCKED，不能用字數計數器
冒充 tokenizer。成功時 synthetic chunk payload 存在 `runtime/parser-payloads/`，沒有送 KIS。

### Docker 中的 40 份文件解析／chunking

最新有效證據：`runtime/u0-parser-chunk-docker.log`／`.json`，擷取於
2026-10-03T15:59:38.278331+00:00；已核對完整 fixture 與 code/dataset hash。
結果 PASSED／chunk_exit=0；單程序峰值 RSS 約 492.18 MiB，僅代表本批小型文件測試。
不必重跑；下列是可重現命令，不是目前待辦。

Host 沒有 tokenizer cache、但 Docker probe 成功時，執行：

```bash
cd /Users/markhuang/Documents/Codex/2026-07-23/new-chat/evals/rag-upgrade-v1
set -o pipefail
make parser-chunk-audit-docker 2>&1 | tee runtime/u0-parser-chunk-docker.log
chunk_exit=$?
printf 'chunk_exit=%s\n' "$chunk_exit" | tee -a runtime/u0-parser-chunk-docker.log
```

將輸出貼回。完整每文件計數另存在 `runtime/u0-parser-chunk-docker.json`，不列 chunk 全文。
新增 target 先執行 manifest validate，再從唯一 running ingestion-worker 讀 immutable image ID
與 tokenizer named volume。它不讀 Config.Env、不帶入正式服務 secret，不掛公司文件或 DB volume。
以該 image 啟動專用暫時容器：non-root image user、network=none、read-only root/cache/eval mount、
cap-drop、no-new-privileges、無 healthcheck、不 pull/build；完成後只移除本次暫時容器。
唯一暫存寫入處為 container /tmp；host 只保存上述合成計數 artifact／log。

使用 image 內已安裝的 parser/chunker，而不是掛 host production source。
固定 tokenizer revision，`local_files_only=True`，**採 ChunkingConfig 類別預設 350/450/50/512**，
不讀／猜測 live Settings 的自訂 chunk 限制，也不呼叫 Worker、CLI、API、DB 或外部模型。
測試容器限制 1 CPU／512 MiB，不把此量測稱為 production latency 或 Oracle 原生 ARM 效能。

40 份均有 chunks、parser/chunk anchors 無遺失、每塊不超過 450／512、token recount 一致才 exit 0；
資料檢查失敗 exit 1，Docker/cache 等環境阻擋 exit 2。此處 PASSED 僅為 parser/chunk gate，
**不是 KIS 已保存，也不是 80 題問答／人工 Gold／整個 U0 完成**。

## 後續正式評測：如何看完整 Gold（非精簡 U0 前置）

先核對 `datasets/tuning.json` 與對應的 `corpus/tuning/` 文件。每题需確認：

1. 問題條件與文件適用範圍一致。
2. 答案、拒答或追問理由可從來源直接確認。
3. 清單與計數需要全表、開放狀態與 item code 去重，不是 top-k 的列數。
4. 流程包含兩頁的四步，順序不可錯。
5. Followup 需要同一題提供的 history，不把其他題的 history 混進來。

Gold 的人工核對用另行簽名的 review 記錄，不直接偷改 frozen manifest。
Holdout 的人工核對由獨立 reviewer 完成，避免把逐題失敗內容回饋實作者。
初稿目前清單只有三列、全部計數 gold 為 2；這是小型解析盤點材料，
**不足以驗證不同總數、重複列、大清單、缺頁與部分 ACL 完整性**。
正式 freeze 前須補充這些變化並留下 dataset version／review 證據，不降品質 gate。

上述三列／答案 2 限制是 **v1 歷史**。v2 已增加多樣化與全部 44 頁實作端 rendering QA，
未涵蓋任意合併格、跨欄或多版本競態，不將它當作 U5 完成。
人工 review 請只讀 v2 的 `datasets/tuning.json`、對應文件與 `lineage.json`，
逐題確認條件、答案／拒答、來源版本、頁碼／章節、必要 anchors、完整性和計數單位。
去重鍵是 `(unit_id, item_code)`；同名同代碼但不同單位要分開，重印列不要增加數量。
0 也須確認全表及結束標記。缺頁／部分可見不得核准精確總數。
Holdout 同樣由獨立 reviewer 核對來源 Gold，不向實作者提供逐題執行失敗作調參。
Review 記錄要有 reviewer／時間、v2 manifest hash、80 個唯一 case ID 與每題結論／備註；
沒有真的核對該題，不能只寫總結「全部 approved」。實作 agent 不替代這份人工簽核。

已提供 `gold-review-v2.template.json`：80 題皆 pending、沒有 reviewer 或核准時間。
`gold_review_v2.py` 綁定 manifest／逐題 canonical JSON hash，拒絕漏題、重複、改 split/hash、
未知欄位、以數字代替 boolean、未填 checks 卻 approved 或 holdout 非獨立 reviewer。
人工請先核對來源與 Gold，再在表單副本逐題填 reviewer、含時區 reviewed_at、actor_kind=human、
真實 independence 與 checks。發現錯誤填 changes_requested 及 notes，不改題庫來掩蓋錯誤。
不要直接修改 template；可在 `runtime/reviews/` 保存人工填寫副本（不提交私人 reviewer 資料）。

```bash
# 預設驗空白 template，只代表 JSON/hash 結構有效；結果仍 pending=80、quality BLOCKED。
make gold-review-v2
# 已由真人完成表單後，唯讀驗其結構：
../../ingestion-service/.venv/bin/python gold_review_v2.py \
  --review runtime/reviews/v2-gold.json
```

驗證器不能證明填表者真的讀過文件，即使 80 題 approved 也不自動 freeze、品質 PASS 或進 U1。
真人簽核、真實品質測試與 independent review 是分開的證據。

### 核准完成後的資料凍結

`freeze_dataset_v2.py` 已實作，但目前沒有真人 review 副本，**正式 freeze 尚未執行**。
預設 `make freeze-check-v2` 和 `make freeze-dataset-v2` 都讀 pending templates，exit2／BLOCKED；
不建 `runtime/frozen/`，也不修改 templates、來源、sidecars 或進度。這是預期拒絕，不是品質 PASS。

只有真人實際完成 80 題 Gold 與 40 題 tuning relevance、接受相關性 protocol，才執行：

```bash
make freeze-check-v2 \
  GOLD_REVIEW=runtime/reviews/v2-gold.json \
  RELEVANCE_REVIEW=runtime/reviews/v2-tuning-relevance.json
make freeze-dataset-v2 \
  GOLD_REVIEW=runtime/reviews/v2-gold.json \
  RELEVANCE_REVIEW=runtime/reviews/v2-tuning-relevance.json
```

這兩個副本目前不存在；不要自行造 reviewer／核准紀錄，只為讓命令通過。
工具重新核對 87 個 dataset 檔、全部 80 題／40 標註／91 個完整 catalog、既有真實 trace，
以及 independent holdout human、必要 checks、timezone、protocol 與整數等級。
來源固定為 a714… 的 canonical JSON SHA，不能偷換其他 report 或造 observations。
快照包含 dataset manifest 的 file SHA、reviews/report/catalog 的 canonical JSON SHA、
標註 protocol 文件與 validators 的 file SHA、80 個 case ID/split/hash；不轉抄真人、notes、
問題、Gold 答案、文件原文、實體 path/URL、key 或 attestation。

結果保存在 `runtime/frozen/`，檔名是輸出 `snapshot_id` 的 64 碼加 `.json`，file mode 0600。
原子 no-clobber 發布、拒絕 symlink／FIFO／越界、失敗只清自己建立的 temp；不覆寫既有快照。
不自動挑「最新」快照。後續執行者必須明確使用該次 ID，並唯讀重新驗證當前輸入：

```bash
# 先將 freeze 輸出的 snapshot_id 設到 U0_SNAPSHOT_ID；不是檔案路徑或 API key。
../../ingestion-service/.venv/bin/python freeze_dataset_v2.py verify \
  --snapshot-id "$U0_SNAPSHOT_ID" \
  --gold-review runtime/reviews/v2-gold.json \
  --labels runtime/reviews/v2-tuning-relevance.json
```

這是「hash-bound 資料與人工判斷紀錄凍結」，不是檔案系統永久唯讀或真人語意認證。
資料／判斷／protocol／validator 改變後 verify 拒絕沿用；需要重新核對並另建版本證據。
**不包含 runtime/model/prompt/policy freeze、allowed context、真生成、正式分數或 U0 PASS**；
原 sidecar 外送仍 false。工程工具驗收最新為 155 passed、0 failed/skipped/errors，
`runtime/u0-dataset-freeze-reconciled-checks.log` 記錄 checks_exit=0。
enabled writer 只在單元測試的 temporary directory 執行，toy approvals 不是真人紀錄。

## 真實問答評估尚未執行

### 隔離 KIS 入庫：已通過

使用者已同意只匯入自製資料到全新測試知識庫，並在完成後 exact-ID cleanup。
執行方式（只有資料／工具變更需重跑，不要對正式 Compose 執行此流程）：

```bash
cd /Users/markhuang/Documents/Codex/2026-07-23/new-chat/evals/rag-upgrade-v1
U0_ALLOW_ISOLATED_WRITES=1 make kis-persistence-audit-docker
```

未設定 opt-in 會 exit 2，不建立資源。工具需要 Docker Desktop 已啟動，且 KIS、Ingestion 的
原有容器可供唯讀取得 local image/cache；不會繼承其 env/key、不 pull/build/download。
全新 internal network 不接 helpdesk-rag，沒有 host ports，PG/Qdrant 只寫測試 tmpfs。
模型 cache 與 eval 只讀；API/Worker 分時運行；PG 16 經真正 Alembic upgrade 與真實 Worker。
最終 artifact：`runtime/u0-kis-persistence-3a26cddc942b47b6bc4debaa4d84e833.json`。
映射只有 fixture、document/version/chunk UUID、頁碼、token count 與內容／章節 SHA-256，
沒有全文、source path/URL 或 key；9 個容器 log driver 均為 none，內部 payload stdout 不保存為 Docker log。
9 個測試容器與網路皆已移除，artifact 的 UUID 是歷史證據，
不是現在可呼叫的文件 ID；後續 collector 必須在自己的隔離 run 重新綁定。

測試 Qdrant 使用 192 MiB storage tmpfs／4 MB WAL／1 個預設 segment；
這是小型功能測試配置，不是正式效能／資源基線。測試程式 34 passed、0 failed/skipped/errors。
直接 DB 查詢只在本次全新測試 PG，並使用 READ ONLY；Retrieval runtime 仍只能透過 KIS。
**自文件文字搜尋不是 80 題 Recall/nDCG；內容 hash 一致也不證明表格有 typed row lineage。**

### 結構資訊稽核：已完成現況盤點

```bash
make structure-audit-docker
```

這個 target 不重建 DB，也不重新執行上述入庫流程。它用相同 immutable Ingestion/KIS
images 建立兩個 network=none／non-root／read-only／log-driver=none 暫時容器，讀 installed
parser/chunker/schema。重新產生的 80 個 content/section hash、頁碼、tokens 全部核對先前
成功 artifact；任何來源、tool、image 或 mapping 不一致都拒絕，不能沿用舊 pass。
容器只掛 eval 與唯讀 cache，不讀正式 env/key，不連 DB、不下載，exact-ID cleanup。

Artifact：`runtime/u0-structure-audit-802a3f7bab64462cb8c61a3b22d36470.json`。
`inventory_status=PASSED` 只代表盤點執行完整；`quality_gate=BLOCKED` 保持不變。
已測得 PDF 30 列、DOCX 30 列的 anchor／cell 文字順序保留；PDF 40 個步驟順序保留。
PDF 20 chunks 有頁碼、沒有 section/table block；DOCX 有 10 table blocks，但 block_type 不進
chunk，沒有頁碼。現有 chunk/request/DB/search 都沒有 typed row/cell/table/continuation。
Generic custom_metadata JSONB 不等於已有 typed lineage 契約；anchor 是自製文字，不是 DB row ID。

這是同 corpus/image/hash 的**歷史 persistence 關聯**，不是重新連目前 DB 驗證；舊 tmpfs 已清理。
不證明大表、合併格、缺頁／部分 ACL 完整性，也不取代視覺 QA／人工 Gold／問答品質。
工具測試現在 47 passed（新增 13 個 guards），0 failed/skipped/errors，不冒充業務測試通過數。

現有 Chat E2E 是 Fake-only。不要用它當真實 Groq 回答品質，也不要把目前公司文件的
external_llm_allowed 改成 true 來做基線。

下列為歷史完整驗收待辦。**目前先做 U1 文件設計，不啟動 U0 新 runtime**；U0 的精簡 protocol 與後續正式證據要求仍保留，免費方案與必要 synthetic 治理授權亦已確認。

原完整驗收待辦（保留供後續正式評測）：

- 隔離 synthetic 匯入／exact-ID cleanup 授權及 KIS chunk mapping 單項已完成；共用 DB 仍未授權。
- 補齊人工 Gold／freeze／獨立 review；typed table/row 可用性已判定為未支援，不在 U0 修 parser。
- v2 已補多樣化與實作端 rendering QA；剩下的是人工語意 Gold／freeze／獨立 review，
  不是繼續沿用 v1 固定答案或將 rendering pass 當 Gold approved。
- 在真實 runtime 凍結模型、prompt、policy、limits、硬體及 corpus fingerprint。
- 實作並 review 安全的 HTTP baseline collector：只能使用有 namespace／ID 驗證的
  synthetic evidence，不能因共享檢索混進公司文件就送往外部模型。
- tuning 原始問題 SHA、KIS limit=30 與 installed Retrieval restricted selection 已收集；
  下一段需 allowed 最終 context／簽章／Answer，
  包裝／排序的排除原因不能憑猜測標註。
- 固定相關性標註後計算真正 Recall@30 入口召回與 nDCG@10；目前 anchor proxy 不能取代它們。
  正式指標工具已建立，核對方式見 relevance-protocol-v1.md；40 個 tuning 人工 relevance 尚未填寫。
- 使用者已同意 synthetic-only 免費 Groq eval：每題 3 次，共至少 240 次生成；
  但實際免費帳戶／額度尚未確認。無法確認免費或遇到額度限制就停止，不轉付費方案。
  本次 external_llm_allowed 維持 false，Groq 呼叫為 0；後續需單獨凍結核准的測試治理設定。
  Provider 改變／400／401／429／timeout 全部記錄，不挑最好 run、不以重送掩蓋失敗。
- 人工核對生成答案與 citations；安全案例、CPU/RAM／p50/p95 與 ARM 相容性分開報告。

`runner.py score` 是離線彙總器，不是 HTTP collector；不用偽造 observations 來宣稱 baseline。
觀察檔只允許 case/run、synthetic anchors、人工等級、計數與 fingerprint，拒絕 answer、
content、source_path、attestation、api_key 欄位。不呼叫任何 Provider。

```bash
# 只有取得真實且人工評分的 observations 後才能用；目前沒有這份輸入檔。
../../ingestion-service/.venv/bin/python runner.py score \
  --split tuning --real-generation --observations runtime/tuning-observations.json
```

同題 3 runs、全部 40 題、同 provider/model/prompt/policy 必須完整，不接受漏題／重複／混設定。
輸出 anchor recall、anchor nDCG **proxy**、selected coverage 與人工評分彙總；
proxy 只作工具測試，不是第 7.2 節 release metrics。即使輸入人工分數全 true，
沒有人工 Gold／獨立驗收也不會輸出 quality PASS。

## ARM CPU 相容性工具（單執行緒單項已通過）

```bash
# 本機既有 ARM images／唯讀 cache；不下載、不重建、不改正式env。
make arm-cpu-single-thread-audit-docker
# 只定位啟動階段，不算完整 compatibility PASS：
make arm-startup-diagnostic-docker
```

`arm_cpu_audit.py` 只建立本次專用 network=none／non-root／read-only／log-driver=none 容器，
不繼承正式 env/key、公司文件 volume 或 DB。依序驗 installed parser/chunker 與 CPU embedding；
來源模組 hash 必須等於 workspace，解析/chunk bytes、頁碼與 tokens 必須等於真實 v2 baseline。
embedding 必須固定 revision、384 維、finite/normalized、CPU-only，且拒绝 over-limit 輸入。
每個 probe 有 360 秒 deadline，結束時只按 exact-ID 與 ownership label 清理自己建立的容器。

2026-10-05 首次default實跑為 **BLOCKED／exit 2**：Intel 上 ARM 模擬的 Ingestion probe 逾時；
沒有完整 snapshot、source parity 或 ARM embedding 結果，不能當相容性通過。
Artifact `runtime/u0-arm-cpu-428240d7791343d699107ee072725f50.json` 保留，1 個容器 cleanup 通過，
正確 run label 的唯讀殘留檢查為空。沒有提高 deadline／skip／更動 production 來掩蓋失敗。
後續startup診斷定位到tokenizer初始化，source與工具/Counter匯入沒有卡住。Explicit
`--single-thread`僅在本次容器把OPENBLAS/OMP/MKL/NUMEXPR threads設1，舊default target保留，
不默默更改預设、不提高360秒deadline或降低parser完整比對門檻。
使用這個明確配置的3次full runs均PASSED；final
`runtime/u0-arm-cpu-ca1b4655dfed483fb8ec86ca825e9064.json`綁定目前4個tool hashes及source hashes。
40文件/91chunks content/page/token parity全同真x86 baseline；真CPU E5 query/document各1向量，
384維/finite/normalized/over-limit拒絕通過。2個本次容器cleanup通過，沒有外部模型或DB呼叫。
final startup run `runtime/u0-arm-startup-295662dd5a0b4ef9a038e3faed4fa1eb.json`只有DIAGNOSTIC_ONLY，
不替代full；1容器cleanup通過，純allowlist stages/hash/elapsed與synthetic token計數，不記原文。
完整CLI實跑與Make target dry-run的證據區分記在baseline；不把`make -n`當測試通過。
這不是 Oracle 原生效能、migration/E2E 或候選 reranker/parser adapter 證据；不外推容量。
離線工具回歸目前133 passed、0 failed/skipped/errors；Gold/relevance仍全部pending，U0品質未通過。

## 安全與工作範圍

- 所有 sidecar 都是 `external_llm_allowed=false`；只允許 `rag-u0-fixtures` 測試群組讀取。
- 不讀 `.env`，不幫你切 Provider 或修改 policy。預設離線工具不執行 HTTP／SQL；
  opt-in KIS audit 只對本次全新隔離 DB/API 執行測試，不連正式 DB。結構 audit 無 DB 連線。
- Docker parser/chunk target 只建／移除專用無網路暫時容器，不改既有服務、volume 或 cache。
- `runtime/` 與 `qa/` 不提交；正式 log 與 synthetic 評估 artifact 分開。
- 不重建公司索引，不匯入 Resume／招生簡章，不改五個後端與 Web Chat 的 production code。
- 可依 `u1-design-entry-v1` 開始 U1 文件設計；不得把未量測品質改成 PASS，不開始 U2 或 production 修改。

目前結果與精確 blockers 見 [U0 基線報告](../../docs/rag-upgrade-v1-baseline.md)。
