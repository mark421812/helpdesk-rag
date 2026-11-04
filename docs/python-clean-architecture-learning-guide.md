# 用三個 Help Desk 專案學 Python 與 Clean Architecture

這份指南不是 Python 語法大全，而是帶你沿著系統真正會走的路徑閱讀程式。每次先回答
「這一層負責什麼」，再看 Python 寫法；不要一開始就嘗試記住所有套件 API。

## 建議閱讀順序

### 第一輪：先讀最小的 Retrieval Service

1. `retrieval-service/src/retrieval_service/domain/entities.py`
   - 學 `dataclass`、型別標註、`@property`、驗證規則。
   - 理解 evidence 為什麼分成可送外部 LLM 與不可外送兩組。
2. `retrieval-service/src/retrieval_service/application/dtos.py`
   - 學 DTO、不可變資料與 policy settings。
3. `retrieval-service/src/retrieval_service/application/ports.py`
   - 學 `Protocol` 與 Dependency Inversion。
4. `retrieval-service/src/retrieval_service/application/use_cases.py`
   - 從 `RetrieveEvidence.execute()` 看完整查詢決策。
5. `retrieval-service/src/retrieval_service/infrastructure/knowledge_index/http_client.py`
   - 看 HTTP adapter 如何實作 port。
6. `retrieval-service/src/retrieval_service/presentation/app.py`
   - 看 FastAPI route 如何把 request 轉成 DTO，再呼叫 use case。
7. `retrieval-service/src/retrieval_service/bootstrap.py`
   - 最後看 composition root 如何把全部物件組裝起來。

### 第二輪：讀 Knowledge Index Service

1. `knowledge-index-service/src/knowledge_index_service/domain/entities.py`
2. `knowledge-index-service/src/knowledge_index_service/application/ports.py`
3. `knowledge-index-service/src/knowledge_index_service/application/use_cases.py`
4. `knowledge-index-service/src/knowledge_index_service/infrastructure/persistence/uow.py`
5. `knowledge-index-service/src/knowledge_index_service/infrastructure/vector_store/qdrant.py`
6. `knowledge-index-service/src/knowledge_index_service/infrastructure/embedding/sentence_transformer.py`
7. `knowledge-index-service/src/knowledge_index_service/presentation/routes.py`
8. `knowledge-index-service/src/knowledge_index_service/bootstrap.py`

這一輪要理解 PostgreSQL 為何是真實來源、Qdrant 為何只是可重建索引，以及 outbox 如何讓
「DB 已提交、Qdrant 尚未更新」的中間狀態可以恢復。

### 第三輪：讀流程最完整的 Ingestion Service

1. `ingestion-service/src/ingestion_service/domain/entities.py`
2. `ingestion-service/src/ingestion_service/application/ports.py`
3. `ingestion-service/src/ingestion_service/application/use_cases.py`
4. `ingestion-service/src/ingestion_service/infrastructure/sources/local_folder.py`
5. `ingestion-service/src/ingestion_service/infrastructure/metadata/yaml_resolver.py`
6. `ingestion-service/src/ingestion_service/infrastructure/parsers/registry.py`
7. `ingestion-service/src/ingestion_service/infrastructure/chunking/structure_aware.py`
8. `ingestion-service/src/ingestion_service/infrastructure/knowledge_index/http_client.py`
9. `ingestion-service/src/ingestion_service/infrastructure/persistence/repositories.py`
10. `ingestion-service/src/ingestion_service/worker.py`
11. `ingestion-service/src/ingestion_service/bootstrap.py`

這一輪要把 scan、change detection、job queue、parse、metadata、chunk、KIS submission 串成
一條完整資料流。

## 三個專案共同使用的 Python 語法

### Module、package 與 import

每個 `.py` 是 module；包含 `__init__.py` 的資料夾是 package。例：

```python
from ingestion_service.domain.entities import SourceItem
```

意思是從 `ingestion_service/domain/entities.py` 匯入 `SourceItem` 名稱。最上方三引號文字是
module docstring，可用 `help(module)` 或 IDE 查看。

```python
from __future__ import annotations
```

讓型別標註延後求值，因此 class 可以引用稍後才宣告的型別，也能減少 import 循環問題。

### 型別標註

```python
name: str
page: int | None
chunks: tuple[Chunk, ...]
metadata: dict[str, Any]
```

- `str`、`int` 是基本型別。
- `A | None` 表示值可以是 A，也可以沒有值。
- `tuple[Chunk, ...]` 表示任意長度、內容都是 Chunk 的 tuple。
- `dict[str, Any]` 表示 key 是字串，value 可以是 JSON-compatible 的多種型別。
- 型別標註主要給 IDE、mypy 與人閱讀；Python 執行時仍需自己做重要資料驗證。

### dataclass 與一般 class

```python
@dataclass(frozen=True, slots=True)
class Chunk:
    content: str
    token_count: int
```

`@dataclass` 自動產生 `__init__`、比較與顯示方法。`frozen=True` 適合 DTO/value object；具有
生命週期的 entity 則通常可變，例如 `SourceItem` 與 `IngestionJob`。

`self` 代表目前 instance。`__post_init__()` 在 dataclass 自動初始化後執行，專案用它維護
「物件只要存在就一定有效」的 invariant。

```python
@property
def is_terminal(self) -> bool:
    return self.status in terminal_statuses
```

`@property` 讓方法以 `job.is_terminal` 方式使用。`@classmethod` 則把 class 本身以 `cls`
傳入，常用於有名稱的建構方式。

### Collection 與 comprehension

```python
ids = {item.id for item in items}
views = tuple(to_view(item) for item in items)
by_id = {item.id: item for item in items}
```

- `{value for ...}` 是 set comprehension，適合去重與集合運算。
- `tuple(expression for ...)` 包住 generator expression，建立不可變序列。
- `{key: value for ...}` 是 dict comprehension，適合建立快速 lookup。
- `enumerate(items, start=1)` 同時取得編號與值。
- `zip(a, b, strict=True)` 成對走訪，長度不同時立即報錯。

### 例外處理

```python
try:
    result = await dependency.call()
except TemporaryError as exc:
    raise ExternalServiceError("dependency failed") from exc
finally:
    await resource.close()
```

- `raise` 中止目前流程並往呼叫端傳遞錯誤。
- `from exc` 保留原始原因，log/debug 時能看到完整 exception chain。
- `finally` 無論成功或失敗都執行，適合釋放 client、DB connection。
- 專案用不同 Domain exception 表達 retry、permanent failure、not found 與 forbidden；不要靠
  錯誤訊息字串判斷政策。

### async、await 與背景程序

```python
result = await client.search(...)
parsed = await asyncio.to_thread(parser.parse, path)
```

`async def` 回傳 coroutine；`await` 暫停目前 coroutine，讓 event loop 執行其他工作。
HTTP 與 async PostgreSQL client 原生使用 await。同步的 parser/檔案 I/O 則以 `to_thread`
移到 thread，避免卡住 event loop。

`asyncio.create_task()` 讓 heartbeat 等 coroutine 與主 loop 並行；`asyncio.Event` 用來通知
worker/scheduler 優雅停止。

### Context manager 與 transaction

```python
async with uow_factory() as uow:
    entity = await uow.items.get(item_id)
    entity.change()
    await uow.items.update(entity)
    await uow.commit()
```

`async with` 保證結束時呼叫 `__aexit__`，即使發生 exception 也能 rollback/close。
Unit of Work 將多個 repository 操作放在同一 transaction，是應用層與資料庫層之間的邊界。

### Protocol 與 Dependency Injection

```python
class KnowledgeSearchPort(Protocol):
    async def search(self, query: str) -> SearchResult: ...
```

`Protocol` 採 structural typing：物件只要提供相同方法即可，不必繼承該 class。Application
依賴 port，Infrastructure 提供 httpx/Qdrant/SQLAlchemy 實作，測試則可注入 fake。

`bootstrap.py` 是 composition root。只有最外層需要知道「哪個 interface 配哪個實作」，
核心 use case 不應自行 `new` 一個 HTTP client 或 DB repository。

### Decorator、FastAPI 與 Pydantic

```python
@app.post("/api/v1/retrievals")
async def retrieve(request: RetrievalRequest) -> RetrievalResponse:
    ...
```

decorator 在函式宣告後將它註冊為 route。FastAPI 用 Pydantic schema 驗證 HTTP input/output；
Application DTO 與 Domain entity 不應直接等同於 HTTP schema，否則外部 contract 會滲入核心。

## Clean Architecture 四層怎麼分

```text
Presentation  ->  Application  ->  Domain
                       ^
                       |
                Infrastructure
```

| 層 | 可以知道 | 不應負責 |
|---|---|---|
| Domain | entity、value object、enum、商業 invariant | HTTP、SQL、Qdrant、檔案格式 |
| Application | use case、DTO、port、流程與政策 | 某個框架的 request/ORM object |
| Infrastructure | SQLAlchemy、httpx、Qdrant、parser、filesystem | 決定核心商業政策 |
| Presentation | FastAPI、Pydantic、CLI、錯誤到 HTTP 的 mapping | 重複 use-case 邏輯 |

依賴方向不是「執行時誰呼叫誰」，而是「source code import 誰」。Infrastructure 在執行時被
Application 呼叫，但 source code 是 Infrastructure import Application port 並實作它。

## 常見名稱不要混在一起

| 名稱 | 用途 | 例子 |
|---|---|---|
| Entity | 有 identity、狀態與商業行為 | `SourceItem`、`Document`、`IngestionJob` |
| Value object | 只在乎值，通常不可變 | `Chunk`、`AccessControlEntry` |
| DTO | use case 邊界傳遞資料 | `SearchRequest`、`ProcessJobResult` |
| Pydantic schema | HTTP JSON contract 與驗證 | `RetrievalRequest`、`SourceResponse` |
| ORM model | Python object 與 DB table 的 mapping | `SourceItemModel`、`DocumentModel` |
| Port | 核心需要的抽象能力 | `KnowledgeIndexClient`、`UnitOfWork` |
| Adapter | 某種技術對 port 的實作 | `KnowledgeIndexHttpClient`、Qdrant adapter |

DTO、schema、entity、ORM model 看似有相似欄位，但分開能防止 DB schema 或公開 API 的改動
直接破壞商業核心。

## 跟著兩條真實流程讀

### 文件匯入流程

```text
CLI/API/scheduler
  -> ScanSource
  -> LocalFolderScanner
  -> PostgreSQL SourceItem + IngestionJob
  -> worker claim lease
  -> ProcessIngestionJob
  -> ParserRegistry -> parser
  -> YamlMetadataResolver
  -> StructureAwareChunker
  -> KnowledgeIndexHttpClient
  -> KIS SubmitDocument
  -> PostgreSQL outbox
  -> KIS worker -> embedding -> Qdrant
```

閱讀時特別觀察：慢速 I/O 前後 transaction 為何會關閉、hash 如何避免重工、lease 如何避免
兩個 worker 重複工作、遠端 pending job 如何轉成 poll job。

### 查詢證據流程

```text
Client（未來經 Chat Service）
  -> Retrieval FastAPI
  -> trusted IdentityProvider
  -> RetrieveEvidence
  -> KnowledgeIndexHttpClient
  -> KIS SearchKnowledge
  -> query embedding -> Qdrant candidates
  -> PostgreSQL canonical ACL/status recheck
  -> Retrieval policy
  -> llm_context / restricted_evidence / citations
```

閱讀時特別觀察：身分不從 request body 接受、ACL 在 KIS canonical DB 再確認、
`external_llm_allowed=false` 為何永遠不能進入 `llm_context`、citation 為何不顯示實體路徑。

## 建議練習

1. 在 Retrieval 的單元測試新增一個 `external_llm_allowed=false` 案例，預測結果後再執行。
2. 在 Ingestion 的暫存 documents 資料夾放一份 TXT，逐步追 `scan -> item -> job -> KIS`。
3. 只改 YAML 的 `document_owner`，觀察它走 metadata PATCH 而不是重新 embedding。
4. 複製一個 fake port adapter，體會為何測試不需真的啟動 Qdrant。
5. 替現有 enum 加一個不合法輸入測試，觀察 Pydantic、Domain validation 分別在哪一層擋下。

讀程式時可以先摺疊 private helper（名稱以 `_` 開頭），先掌握 public class 與 `execute()`；
第二輪再展開 helper 看安全性、映射與邊界案例，會比從第一行一路硬讀更容易建立全貌。
