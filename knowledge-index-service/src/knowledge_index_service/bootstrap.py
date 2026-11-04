"""KIS Composition Root：建立正式 adapters，並注入各個 use case。"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine

from knowledge_index_service.application.use_cases import (
    DeleteDocument,
    EmbedTexts,
    GetJob,
    ProcessNextOutboxEvent,
    ReindexDocument,
    SearchKnowledge,
    SubmitDocument,
    UpdateDocumentMetadata,
)
from knowledge_index_service.infrastructure.config import Settings
from knowledge_index_service.infrastructure.embedding.sentence_transformer import (
    SentenceTransformerEmbeddingModel,
)
from knowledge_index_service.infrastructure.persistence.database import (
    create_engine,
    create_session_factory,
)
from knowledge_index_service.infrastructure.persistence.uow import (
    SqlAlchemyUnitOfWorkFactory,
)
from knowledge_index_service.infrastructure.vector_store.qdrant import QdrantVectorStore


@dataclass(slots=True)
class Container:
    """KIS process 共用的 object graph；欄位型別清楚列出所有依賴。"""

    engine: AsyncEngine
    embedding_model: SentenceTransformerEmbeddingModel
    vector_store: QdrantVectorStore
    submit_document: SubmitDocument
    embed_texts: EmbedTexts
    search_knowledge: SearchKnowledge
    update_metadata: UpdateDocumentMetadata
    reindex_document: ReindexDocument
    delete_document: DeleteDocument
    get_job: GetJob
    process_next_event: ProcessNextOutboxEvent

    async def close(self) -> None:
        """依序關閉 Qdrant client 與 SQLAlchemy engine pool。"""

        await self.vector_store.close()
        await self.engine.dispose()


def create_container(settings: Settings) -> Container:
    """手動 Dependency Injection：先建 adapters，再把 ports 注入 use cases。"""

    # 同一個 session_factory 由每次 UnitOfWork 建立獨立 session/transaction。
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    uow_factory = SqlAlchemyUnitOfWorkFactory(session_factory)
    embedding_model = SentenceTransformerEmbeddingModel(settings)
    vector_store = QdrantVectorStore(settings)
    return Container(
        engine=engine,
        embedding_model=embedding_model,
        vector_store=vector_store,
        submit_document=SubmitDocument(
            uow_factory,
            embedding_model,
            settings.max_document_chunks,
            settings.max_document_characters,
        ),
        embed_texts=EmbedTexts(
            embedding_model,
            settings.max_embedding_texts,
            settings.max_characters_per_text,
        ),
        search_knowledge=SearchKnowledge(
            uow_factory,
            embedding_model,
            vector_store,
            settings.max_search_limit,
            settings.max_search_candidates,
            settings.business_timezone,
        ),
        update_metadata=UpdateDocumentMetadata(uow_factory),
        reindex_document=ReindexDocument(uow_factory),
        delete_document=DeleteDocument(uow_factory),
        get_job=GetJob(uow_factory),
        process_next_event=ProcessNextOutboxEvent(
            uow_factory,
            embedding_model,
            vector_store,
            settings.worker_lease_seconds,
            settings.worker_retry_base_seconds,
        ),
    )
