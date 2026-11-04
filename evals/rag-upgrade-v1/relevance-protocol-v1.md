# U0 tuning 相關性核對方式（草案，需真人接受）

這份文件只定義離線評估的核對方式，不是新版服務 API，也不核准任何答案。
目前 40 題 tuning 相關性皆 pending、80 題 Gold 皆 pending；沒有正式分數。
本表與 Gold 核對是不同工作：Gold 確認「正確答案應是什麼」，相關性表確認「哪些 chunk 能幫助回答」。

## 先看哪些檔案

- `revisions/u0-synthetic-v2/datasets/tuning.json`：原始問題、條件與 Gold 草案。
- 該版本的 `documents.json` 與 `corpus/tuning/`、`corpus/holdout/`：自製來源文件。
- `tuning-relevance-v2.template.json` 的 catalog：實際入庫的 91 個 chunk，含來源、ordinal、頁碼、token 與 hash。
- `runtime/u0-v2-retrieval-restricted-load-a7140183cdf74f999c329ffc8f2f767d.json`：同一 run 的
  mapping／anchor_mapping／chunk_observations，方便定位每個 chunk 的來源標記。

可以閱讀原始 synthetic holdout 文件，但這輪不讀 holdout 問題的檢索失敗來調參，也不執行 holdout queries。
不要用已清理的 UUID 呼叫現在的 KIS；它們只是歷史入庫關聯。
如果來源與 mapping 不足以判斷某個實際 chunk 的內容，保持 pending，不能憑 hash 或 score 猜分數。
來源本身的完整性、拒答或版本問題，先記入 Gold review finding，不靠相關性分數掩蓋。

## 等級提案

| grade | 核對意義 |
|---:|---|
| 0 | 不支持本題；僅名稱相似、錯 scope／版本也算 0 |
| 1 | 同 scope 的背景資訊，不能直接支持必要答案要點 |
| 2 | 直接支持部分必要要點或正確拒答依據，但本身不足 |
| 3 | 本題直接依據／所需完整來源範圍的入口，適用條件正確 |

分數不是模型信心值，也不是 external_llm_allowed 的許可；相關不等於可外送。
等級越高不代表單一 chunk 已足夠產生完整清單或確切數字。
這是提案，真人接受才填 `protocol_accepted=true`；若不適合，填 changes_requested 與 notes，
不要先核准再偷偷改規則。規則改版後應留新版本、重新核對與 freeze，而非混用標註。

## 如何填一題

先複製 template 為自己的 review 副本，放到 `runtime/reviews/`，不要改原始 template 或題庫 manifest。
每題核對**整個 91-chunk catalog**，不能只核對目前排名前 30 的 chunk。

- `default_grade` 原本為 null，不代表其他 chunk 都不相關。核對完整 catalog 後才明確填整數 0。
- `grade_overrides` 逐筆填需要不同分數的 chunk_key 及整數 grade 0–3。
  沒有列出的 chunk 才使用真人確認的 default 0；工具不會自己推定未標註內容不相關。
- `entry_groups`：可回答題的每個必要來源入口各建一群；群內列真人確認等價的 chunk_key。
  每群至少一個、不同群不能重複同一 chunk；群內每個 chunk 必須有正相關等級。
  例如比較 A/B 若需要兩個不同入口，就有兩群，不是命中 A 即算全部召回。
- 清單／統計只標**通往完整範圍的必要入口**，不要求所有表格列擠進前 30 名。
  這個 Recall 不取代後續的完整列、去重、版本與最終 context 充分率驗收。
- Gold 預期 refuse／clarify 的题填空 entry_groups；它們不加入可回答題的入口召回分母。
  可以給拒答依據正相關 grade；拒答是否正確另由答案人工評分，不由檢索指標判定。
- 完成核對才填 `status=approved`、`actor_kind=human`、真人 reviewer、含時區的 reviewed_at、
  `full_catalog_reviewed=true` 與 `protocol_accepted=true`。工具無法證明真人真的核對，不代簽。
- 若找不到合適入口、Gold 有誤或 chunk 難以辨認，保持 pending 或 changes_requested，並說明原因。

這裡不預填真人名字、時間、grade 或入口，也不提供一份全部 approved 的範例。

## 指標與不同階段

- KIS search limit30：入口群組中至少一個等價 chunk 出現在前 30，該群才算命中。
  每題召回率＝命中必要入口群數／全部必要入口群數，按全部可回答題與全部 runs 平均。
- 實際 Retrieval candidate depth24：同樣計算入口召回，但名稱是 Recall@24，**不能冒充 Recall@30**。
- nDCG@10：DCG 使用 `(2^grade - 1) / log2(rank + 1)`，rank 從 1 起。
  IDCG 是**全部 91 個固定人工等級**的理想前 10 名，不只目前搜到的候選。
  未搜到的高相關 chunk 仍留在理想排序中，避免漏召回卻得到完美排序分數。
- 全部等級 0 的題 nDCG 為 N/A，不給 1；回傳 nDCG 分母與 N/A 數量。
  入口召回排除 refuse／clarify，nDCG 可以包含有正相關拒答依據的題；兩者分母不同，不能混讀。
- KIS 每題 1 run、Retrieval 每題 3 runs，全部 40／120 observations 保留，不挑最好的一次。
  這是 tuning、restricted-only baseline，不是最終 holdout gate，也不包含 Answer 或真正模型生成。

分級 nDCG 的算法參考 [Stanford IR：ranked retrieval evaluation](https://nlp.stanford.edu/IR-book/html/htmledition/evaluation-of-ranked-retrieval-results-1.html)。
完整 catalog 核對是本小型資料集的評估方法，不聲稱所有大型公司資料都可窮盡人工標註。
人工相關性與 pooling 的背景參考 [Stanford IR：assessing relevance](https://nlp.stanford.edu/IR-book/html/htmledition/assessing-relevance-1.html)。

## 唯讀驗證與計算

在 `evals/rag-upgrade-v1/`：

```bash
# 結構正確仍 pending；exit0 不代表標註／品質完成。
make relevance-review-v2

# 只有兩份真人 review 完成後才計算；輸入保持只讀。
../../ingestion-service/.venv/bin/python retrieval_metrics_v2.py score \
  --labels runtime/reviews/v2-tuning-relevance.json \
  --gold-review runtime/reviews/v2-gold.json
```

`score` 要求 40 relevance approved 與 80 Gold approved，缺一則 exit2/BLOCKED，不計部分分數。
`check` 不需要 Gold approved，只驗表單與完整真實 trace；即使 labels 全 approved，仍不認證语意。
資料／chunking 改變使 catalog 或逐題 hash 改變，工具拒絕沿用舊標註；只有 run UUID 改變可保持
同一 canonical identity。輸出只含 ID/hash/分數/分母，不印真人資料、notes、原文或秘密。
輸入最多 2 MB，拒重複 JSON keys、NaN／Infinity、漏題／漏 run、重複／外來 chunk 及未知標註欄位。
目前預設 report 是已驗證、已清理的 a714… 歷史證據；不是目前 live DB。
read-only 工具不認證捏造的 report／人類紀錄；獨立 reviewer 仍需驗來源、fingerprints 與真人簽核。

計算成功也只代表對固定人工標註的 tuning 指標；`quality_gate=BLOCKED`，不自动 freeze、
不宣稱生成品質／安全／ARM／U0 完成，不開始 U1。
