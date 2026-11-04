# Help Desk Demo Web Chat

這是一個只供本機展示的無登入 Web Chat。它只呼叫 Chat Service，讓既有流程繼續處理
conversation、query normalization、Retrieval、restricted evidence 分流、Answer Generation、
冪等與永久歷史；Web Chat 不直接連 PostgreSQL、Qdrant、KIS、Retrieval 或 Answer Generation。

## 目前範圍

- Python 3.11、FastAPI、HTTPX、HTML/CSS/原生 JavaScript。
- 瀏覽器只連 `http://127.0.0.1:8130`；server-side gateway 連 Docker network 內的 `chat-api:8120`。
- 沒有登入，所有請求使用共用的 development identity（預設 `web-chat-demo-user`、
  `all-employees`）。這不是權限隔離，也不能直接公開到網際網路。
- 只有目前畫面的單一對話；重新整理會重設畫面，但不會刪除 Chat Service 已保存的資料。
- 每個問題獨立檢索，不提供多輪上下文記憶。沒有歷史側欄、上傳、管理員 purge、SSE 或 Markdown。
- Answer Generation Service 若使用 Fake Provider，答案是固定的測試回答；Provider 設定在
  Answer，不是 Chat 或 Web Chat。已支援 Groq／NVIDIA adapter，但通過 Demo smoke 不代表
  已啟用真實 Provider，也不證明回答品質。
- 引用只顯示 citation ID、文件名稱、章節及頁碼，不顯示 path、URL、machine UUID 或原文 evidence。

完整服務分工與文件匯入／員工問答兩條流程，請看 [`系統總 README`](../README.md)。

## 啟動

先啟動既有 Help Desk stack，並確定 Chat Service 的 `chat-api` 已加入 external
`helpdesk-rag` network 且 readiness 成功：

```bash
cd /Users/markhuang/Documents/Codex/2026-07-23/new-chat/web-chat
cp -n .env.example .env
make network
docker compose up -d --build
docker compose ps -a
make smoke
```

瀏覽器開啟 <http://127.0.0.1:8130>。Web Chat compose 不會替你啟動或重啟其他服務。

## 設定

`.env` 只供 Web Chat container 使用，不能放 Chat/Retrieval/Answer 的 API key。可調整：

- `CHAT_SERVICE_URL`：Docker 預設 `http://chat-api:8120`。
- `DEMO_USER_ID`、`DEMO_GROUP_IDS`：server-side development headers；不要在瀏覽器傳入。
- `ALLOWED_HOSTS`：預設只允許 `localhost,127.0.0.1`。
- `WEB_CHAT_PORT`：預設 `8130`。Compose 永遠只 bind `127.0.0.1`。

服務 readiness 失敗時只會顯示安全的 `not_ready`，不會顯示內部 URL 或 secrets。常見原因是
Chat container 未啟動、migration 未完成，或 Chat 無法連 Retrieval/Answer；依既有服務 README
的順序先修正它們。沒有可讀文件時，正常結果會是 insufficient evidence，而不是系統故障。

## 開發與驗收

```bash
make install
make lock-check
make lint
make typecheck
make test
make test-ui
make config
make build-native
make build-amd64
make build-arm64
make smoke
```

`make smoke` 只做首頁、live、readiness 的 read-only 檢查，不建立對話、不寫 DB、不呼叫 LLM。
建置不在 Dockerfile 寫死 `platform`；Intel 開發機可建立 `linux/amd64` 與 `linux/arm64` image，
Oracle ARM64 部署時使用 ARM image。

## 錯誤與 idempotency

每一個新的問題由頁面建立一個 UUID `Idempotency-Key`。網路逾時或結果不明時，頁面會保留同一
conversation、key 和 request body，按「確認原請求結果」會重送相同 key，不會另造一個問題。
已知 terminal 結果後，重新輸入才會建立新 key。瀏覽器 timeout 不代表後端已取消。

Chat Service 回 `202 processing` 時，頁面每兩秒查詢固定 UUID path，最多等待六分鐘；不會盲信
response 內的 status URL，也不會把 polling timeout 假裝成 failed。answered、unable to answer、
external generation blocked、insufficient evidence、outbound policy blocked 都會以對應業務結果顯示。

## 安全限制

這是 localhost Demo，不是 production authentication。image 以 non-root、read-only root filesystem、
`cap_drop=ALL`、`no-new-privileges` 運行，沒有 Docker socket、公司文件或下游 secrets。頁面使用
`textContent` 顯示所有動態文字，沒有外部 CDN、任意連結或 Markdown HTML rendering。
