from __future__ import annotations

import asyncio
import time
import urllib.error
import urllib.request
from dataclasses import replace
from datetime import date
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from testcontainers.core.container import DockerContainer
from testcontainers.postgres import PostgresContainer

from knowledge_index_service.domain.entities import (
    AccessControlEntry,
    Chunk,
    Document,
    DocumentVersion,
    IndexingJob,
    OutboxEvent,
    SearchFilters,
    VectorPoint,
)
from knowledge_index_service.domain.enums import (
    Confidentiality,
    DocumentStatus,
    JobType,
    PrincipalType,
)
from knowledge_index_service.infrastructure.config import Settings
from knowledge_index_service.infrastructure.persistence.database import (
    create_engine,
    create_session_factory,
)
from knowledge_index_service.infrastructure.persistence.uow import (
    SqlAlchemyUnitOfWorkFactory,
)
from knowledge_index_service.infrastructure.vector_store.qdrant import QdrantVectorStore

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_real_postgres_and_qdrant_adapters() -> None:
    with (
        PostgresContainer("postgres:16-alpine", driver="asyncpg") as postgres,
        DockerContainer("qdrant/qdrant:v1.18.3-unprivileged").with_exposed_ports(6333) as qdrant,
    ):
        qdrant_url = f"http://{qdrant.get_container_host_ip()}:{qdrant.get_exposed_port(6333)}"
        _wait_until_ready(f"{qdrant_url}/healthz")
        settings = Settings(
            database_url=postgres.get_connection_url(),
            qdrant_url=qdrant_url,
            qdrant_collection=f"integration_{uuid4().hex}",
        )
        await asyncio.to_thread(_upgrade_database, settings.database_url)
        engine = create_engine(settings)
        uow_factory = SqlAlchemyUnitOfWorkFactory(create_session_factory(engine))

        document = Document(
            id=uuid4(),
            source_type="test",
            source_item_id="policy.md",
            title="Policy",
            document_type="policy",
            owner_department="IT",
            document_owner="owner@example.com",
            confidentiality=Confidentiality.INTERNAL,
            status=DocumentStatus.PUBLISHED,
            language="zh-TW",
            external_llm_allowed=False,
            acl=[AccessControlEntry(PrincipalType.GROUP, "all-employees")],
        )
        async with uow_factory() as uow:
            await uow.documents.add(document)
            await uow.commit()

        version = DocumentVersion(
            id=uuid4(),
            document_id=document.id,
            version_number="1",
            source_version="1",
            content_hash="a" * 64,
            mime_type="text/plain",
            file_size=12,
            effective_at=None,
            expires_at=None,
            review_at=None,
            parser_name="integration-test",
            parser_version="1",
            embedding_model="integration-model",
            embedding_model_revision="revision-1",
            embedding_dimension=3,
            chunk_count=1,
        )
        chunk = Chunk(
            id=uuid4(),
            document_id=document.id,
            version_id=version.id,
            chunk_index=0,
            content="Policy text",
            content_hash="b" * 64,
            section_path=None,
            page_number=None,
            token_count=2,
        )
        job = IndexingJob(
            id=uuid4(),
            job_type=JobType.INDEX_VERSION,
            document_id=document.id,
            version_id=version.id,
        )
        event = OutboxEvent(
            id=uuid4(),
            event_type=JobType.INDEX_VERSION,
            aggregate_id=document.id,
            job_id=job.id,
            payload={
                "document_id": str(document.id),
                "version_id": str(version.id),
            },
        )
        async with uow_factory() as uow:
            await uow.documents.add_version(version)
            await uow.documents.add_chunks([chunk])
            await uow.jobs.add(job)
            await uow.outbox.add(event)
            await uow.commit()

        async with uow_factory() as uow:
            loaded_documents = await uow.documents.get_many([document.id])
            loaded_versions = await uow.documents.get_versions([version.id])
            loaded_chunks = await uow.documents.list_chunks(version.id)
        loaded = loaded_documents[0]
        assert loaded is not None
        assert loaded.title == "Policy"
        assert loaded.allowed_group_ids == ["all-employees"]
        assert [item.id for item in loaded_versions] == [version.id]
        assert [item.id for item in loaded_chunks] == [chunk.id]

        updated_version = replace(
            version,
            version_number="1.1",
            source_version="2",
            effective_at=date(2026, 1, 1),
            expires_at=date(2026, 12, 31),
            review_at=date(2026, 6, 1),
        )
        async with uow_factory() as uow:
            await uow.documents.update_version_metadata(updated_version)
            await uow.commit()
        async with uow_factory() as uow:
            loaded_version = await uow.documents.get_version(version.id)
        assert loaded_version is not None
        assert loaded_version.version_number == "1.1"
        assert loaded_version.source_version == "2"
        assert loaded_version.effective_at == date(2026, 1, 1)
        assert loaded_version.expires_at == date(2026, 12, 31)
        assert loaded_version.review_at == date(2026, 6, 1)

        vector_store = QdrantVectorStore(settings)
        await vector_store.ensure_collection(3)
        await vector_store.upsert(
            [
                VectorPoint(
                    point_id=str(uuid4()),
                    vector=[1.0, 0.0, 0.0],
                    payload={
                        "chunk_id": str(uuid4()),
                        "document_id": str(document.id),
                        "version_id": str(uuid4()),
                        "is_current": True,
                        "status": DocumentStatus.PUBLISHED.value,
                        "confidentiality": Confidentiality.INTERNAL.value,
                        "language": "zh-TW",
                        "acl_public": True,
                        "allowed_user_ids": [],
                        "allowed_group_ids": [],
                    },
                )
            ]
        )
        hits = await vector_store.search(
            vector=[1.0, 0.0, 0.0],
            limit=5,
            filters=SearchFilters(),
            score_threshold=0.5,
        )
        assert len(hits) == 1

        await vector_store.close()
        await engine.dispose()


def _wait_until_ready(url: str, timeout_seconds: float = 30) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError):
            time.sleep(0.25)
    raise TimeoutError(f"Service did not become ready: {url}")


def _upgrade_database(database_url: str) -> None:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")
