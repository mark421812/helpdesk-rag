from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, date, datetime
from unittest.mock import patch
from uuid import uuid4

from knowledge_index_service.application.dtos import (
    ChunkSubmission,
    DocumentSubmission,
    MetadataPatch,
    SearchRequest,
)
from knowledge_index_service.application.use_cases import (
    DeleteDocument,
    ProcessNextOutboxEvent,
    SearchKnowledge,
    SubmitDocument,
    UpdateDocumentMetadata,
)
from knowledge_index_service.domain.entities import (
    AccessControlEntry,
    SearchFilters,
    VectorSearchHit,
)
from knowledge_index_service.domain.enums import (
    Confidentiality,
    DocumentStatus,
    IndexingStatus,
    JobStatus,
    OutboxStatus,
    PermissionEffect,
    PrincipalType,
)
from knowledge_index_service.domain.exceptions import (
    CapacityExceededError,
    ConflictError,
    ValidationError,
)
from tests.unit.fakes import (
    FakeEmbeddingModel,
    FakeUnitOfWorkFactory,
    FakeVectorStore,
    MemoryStore,
)


class RestoreDuringDeleteVectorStore(FakeVectorStore):
    def __init__(self, store: MemoryStore) -> None:
        super().__init__()
        self._store = store

    async def delete_document(self, document_id) -> None:
        await super().delete_document(document_id)
        document = self._store.documents[document_id]
        document.deleted_at = None
        document.status = DocumentStatus.PUBLISHED


def submission() -> DocumentSubmission:
    return DocumentSubmission(
        source_type="local_folder",
        source_item_id="company/policy.md",
        source_version="mtime:1",
        title="Remote Work Policy",
        document_type="policy",
        owner_department="HR",
        document_owner="hr@example.com",
        confidentiality=Confidentiality.INTERNAL,
        status=DocumentStatus.PUBLISHED,
        language="zh-TW",
        external_llm_allowed=False,
        version_number="1.0",
        content_hash="a" * 64,
        mime_type="text/markdown",
        file_size=100,
        parser_name="markdown",
        parser_version="1",
        chunks=(
            ChunkSubmission("員工每週可以遠端工作兩天。", section_path="遠端工作"),
            ChunkSubmission("申請需由主管核准。", section_path="申請流程"),
        ),
        source_path="/data/company/policy.md",
    )


class IndexingFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.store = MemoryStore()
        self.uow_factory = FakeUnitOfWorkFactory(self.store)
        self.embedding = FakeEmbeddingModel()
        self.vector_store = FakeVectorStore()

    async def test_submit_is_idempotent_and_worker_indexes_once(self) -> None:
        submit = SubmitDocument(self.uow_factory, self.embedding)
        first = await submit.execute(submission())
        duplicate = await submit.execute(submission())

        self.assertTrue(first.created)
        self.assertFalse(duplicate.created)
        self.assertEqual(first.version_id, duplicate.version_id)
        self.assertEqual(len(self.store.versions), 1)
        self.assertEqual(len(self.store.chunks), 2)

        worker = ProcessNextOutboxEvent(
            self.uow_factory,
            self.embedding,
            self.vector_store,
            lease_seconds=60,
            retry_base_seconds=1,
        )
        self.assertTrue(await worker.execute())

        version = self.store.versions[first.version_id]
        job = self.store.jobs[first.job_id]
        event = next(iter(self.store.events.values()))
        self.assertEqual(version.indexing_status, IndexingStatus.INDEXED)
        self.assertEqual(job.status, JobStatus.COMPLETED)
        self.assertEqual(event.status, OutboxStatus.PROCESSED)
        self.assertEqual(len(self.vector_store.points), 2)
        self.assertEqual(self.vector_store.dimension, 3)
        self.assertEqual(
            self.store.documents[first.document_id].current_version_id,
            first.version_id,
        )

    async def test_submit_rejects_invalid_content_hash(self) -> None:
        with self.assertRaises(ValidationError):
            await SubmitDocument(self.uow_factory, self.embedding).execute(
                replace(submission(), content_hash="not-a-sha256")
            )

    async def test_submit_enforces_document_size_limit(self) -> None:
        with self.assertRaises(CapacityExceededError):
            await SubmitDocument(
                self.uow_factory,
                self.embedding,
                max_characters=5,
            ).execute(submission())

    async def test_resubmitting_failed_version_creates_new_job(self) -> None:
        submit = SubmitDocument(self.uow_factory, self.embedding)
        first = await submit.execute(submission())
        self.store.versions[first.version_id].indexing_status = IndexingStatus.FAILED
        self.store.jobs[first.job_id].status = JobStatus.FAILED
        next(iter(self.store.events.values())).status = OutboxStatus.FAILED

        retried = await submit.execute(submission())

        self.assertFalse(retried.created)
        self.assertEqual(retried.indexing_status, IndexingStatus.PENDING)
        self.assertNotEqual(retried.job_id, first.job_id)
        self.assertEqual(len(self.store.jobs), 2)

    async def test_restore_reuses_version_reapplies_metadata_and_reindexes(self) -> None:
        submit = SubmitDocument(self.uow_factory, self.embedding)
        created = await submit.execute(submission())
        worker = ProcessNextOutboxEvent(
            self.uow_factory,
            self.embedding,
            self.vector_store,
            lease_seconds=60,
            retry_base_seconds=1,
        )
        await worker.execute()
        await DeleteDocument(self.uow_factory).execute(created.document_id)
        await worker.execute()
        self.assertEqual(self.vector_store.points, {})

        restored_submission = replace(
            submission(),
            title="Restored Policy",
            source_version="mtime:2",
            version_number="1.1",
            effective_at=date(2026, 2, 1),
            expires_at=date(2027, 1, 31),
            review_at=date(2026, 8, 1),
        )
        restored = await submit.execute(restored_submission)
        duplicate_restore = await submit.execute(restored_submission)

        document = self.store.documents[created.document_id]
        version = self.store.versions[created.version_id]
        self.assertFalse(restored.created)
        self.assertEqual(restored.version_id, created.version_id)
        self.assertIsNotNone(restored.job_id)
        self.assertEqual(duplicate_restore.job_id, restored.job_id)
        self.assertEqual(len(self.store.versions), 1)
        self.assertEqual(len(self.store.jobs), 3)
        self.assertIsNone(document.deleted_at)
        self.assertEqual(document.status, DocumentStatus.PUBLISHED)
        self.assertEqual(document.title, "Restored Policy")
        self.assertEqual(document.current_version_id, created.version_id)
        self.assertEqual(version.source_version, "mtime:2")
        self.assertEqual(version.version_number, "1.1")
        self.assertEqual(version.effective_at, date(2026, 2, 1))
        self.assertEqual(version.expires_at, date(2027, 1, 31))
        self.assertEqual(version.review_at, date(2026, 8, 1))

        await worker.execute()
        self.assertEqual(len(self.vector_store.points), 2)

    async def test_stale_delete_event_is_skipped_after_restore(self) -> None:
        submit = SubmitDocument(self.uow_factory, self.embedding)
        created = await submit.execute(submission())
        worker = ProcessNextOutboxEvent(
            self.uow_factory,
            self.embedding,
            self.vector_store,
            lease_seconds=60,
            retry_base_seconds=1,
        )
        await worker.execute()
        await DeleteDocument(self.uow_factory).execute(created.document_id)
        restored = await submit.execute(submission())

        await worker.execute()

        self.assertIsNone(self.store.documents[created.document_id].deleted_at)
        self.assertEqual(self.vector_store.deleted_documents, [])
        self.assertEqual(self.store.jobs[restored.job_id].status, JobStatus.PENDING)
        await worker.execute()
        self.assertEqual(self.store.jobs[restored.job_id].status, JobStatus.COMPLETED)

    async def test_repeated_restore_returns_latest_restore_job(self) -> None:
        submit = SubmitDocument(self.uow_factory, self.embedding)
        created = await submit.execute(submission())
        await DeleteDocument(self.uow_factory).execute(created.document_id)

        restored = await submit.execute(submission())
        duplicate = await submit.execute(submission())

        self.assertNotEqual(restored.job_id, created.job_id)
        self.assertEqual(duplicate.job_id, restored.job_id)

    async def test_repeated_delete_returns_same_latest_job_before_and_after_completion(
        self,
    ) -> None:
        created = await SubmitDocument(self.uow_factory, self.embedding).execute(submission())
        worker = ProcessNextOutboxEvent(
            self.uow_factory,
            self.embedding,
            self.vector_store,
            lease_seconds=60,
            retry_base_seconds=1,
        )
        await worker.execute()
        delete = DeleteDocument(self.uow_factory)

        first = await delete.execute(created.document_id)
        duplicate = await delete.execute(created.document_id)

        self.assertEqual(duplicate.job_id, first.job_id)
        self.assertEqual(duplicate.status, JobStatus.PENDING)
        await worker.execute()

        completed_duplicate = await delete.execute(created.document_id)
        self.assertEqual(completed_duplicate.job_id, first.job_id)
        self.assertEqual(completed_duplicate.status, JobStatus.COMPLETED)
        self.assertEqual(len(self.store.jobs), 2)

    async def test_resubmitting_old_hash_reactivates_version_and_submission_metadata(self) -> None:
        submit = SubmitDocument(self.uow_factory, self.embedding)
        first = await submit.execute(submission())
        worker = ProcessNextOutboxEvent(
            self.uow_factory,
            self.embedding,
            self.vector_store,
            lease_seconds=60,
            retry_base_seconds=1,
        )
        await worker.execute()
        second = await submit.execute(
            replace(
                submission(),
                content_hash="b" * 64,
                source_version="mtime:2",
                version_number="2.0",
                chunks=(ChunkSubmission("第二版規章。"),),
            )
        )
        await worker.execute()
        self.assertEqual(
            self.store.documents[first.document_id].current_version_id,
            second.version_id,
        )

        reactivated_submission = replace(
            submission(),
            title="Restored First Edition",
            source_version="mtime:3",
            version_number="3.0",
            source_url="https://intranet.example/policy",
            effective_at=date(2026, 3, 1),
            expires_at=date(2027, 3, 1),
            review_at=date(2026, 9, 1),
            acl=(AccessControlEntry(PrincipalType.GROUP, "employees"),),
        )
        reactivated = await submit.execute(reactivated_submission)

        document = self.store.documents[first.document_id]
        version = self.store.versions[first.version_id]
        self.assertFalse(reactivated.created)
        self.assertEqual(reactivated.version_id, first.version_id)
        self.assertNotEqual(reactivated.job_id, first.job_id)
        self.assertEqual(reactivated.indexing_status, IndexingStatus.PENDING)
        self.assertEqual(document.current_version_id, first.version_id)
        self.assertEqual(document.title, "Restored First Edition")
        self.assertEqual(document.source_url, "https://intranet.example/policy")
        self.assertEqual(document.acl, list(reactivated_submission.acl))
        self.assertEqual(version.source_version, "mtime:3")
        self.assertEqual(version.version_number, "3.0")
        self.assertEqual(version.effective_at, date(2026, 3, 1))
        self.assertEqual(version.expires_at, date(2027, 3, 1))
        self.assertEqual(version.review_at, date(2026, 9, 1))

        await worker.execute()
        self.assertEqual(self.store.jobs[reactivated.job_id].status, JobStatus.COMPLETED)
        self.assertEqual(document.current_version_id, first.version_id)

    async def test_old_hash_reactivation_supersedes_other_pending_version(self) -> None:
        submit = SubmitDocument(self.uow_factory, self.embedding)
        first = await submit.execute(submission())
        worker = ProcessNextOutboxEvent(
            self.uow_factory,
            self.embedding,
            self.vector_store,
            lease_seconds=60,
            retry_base_seconds=1,
        )
        await worker.execute()
        second = await submit.execute(
            replace(
                submission(),
                content_hash="b" * 64,
                source_version="mtime:2",
                version_number="2.0",
                chunks=(ChunkSubmission("尚未索引的第二版。"),),
            )
        )

        reactivated = await submit.execute(
            replace(submission(), source_version="mtime:3", version_number="3.0")
        )

        self.assertEqual(
            self.store.documents[first.document_id].current_version_id,
            first.version_id,
        )
        self.assertNotEqual(reactivated.job_id, first.job_id)
        await worker.execute()
        self.assertEqual(self.store.jobs[second.job_id].status, JobStatus.FAILED)
        await worker.execute()
        self.assertEqual(self.store.jobs[reactivated.job_id].status, JobStatus.COMPLETED)

    async def test_restore_with_new_content_creates_and_indexes_new_current_version(self) -> None:
        submit = SubmitDocument(self.uow_factory, self.embedding)
        created = await submit.execute(submission())
        worker = ProcessNextOutboxEvent(
            self.uow_factory,
            self.embedding,
            self.vector_store,
            lease_seconds=60,
            retry_base_seconds=1,
        )
        await worker.execute()
        await DeleteDocument(self.uow_factory).execute(created.document_id)
        await worker.execute()

        restored = await submit.execute(
            replace(
                submission(),
                content_hash="b" * 64,
                source_version="mtime:2",
                version_number="2.0",
                chunks=(ChunkSubmission("更新後的遠端工作規章。"),),
            )
        )

        self.assertTrue(restored.created)
        self.assertNotEqual(restored.version_id, created.version_id)
        document = self.store.documents[created.document_id]
        self.assertIsNone(document.deleted_at)
        self.assertEqual(document.current_version_id, restored.version_id)
        await worker.execute()
        self.assertEqual(self.store.jobs[restored.job_id].status, JobStatus.COMPLETED)
        self.assertEqual(len(self.vector_store.points), 1)

    async def test_restore_target_is_not_replaced_by_older_pending_index_job(self) -> None:
        submit = SubmitDocument(self.uow_factory, self.embedding)
        original = await submit.execute(submission())
        await DeleteDocument(self.uow_factory).execute(original.document_id)
        restored = await submit.execute(
            replace(
                submission(),
                content_hash="b" * 64,
                source_version="mtime:2",
                version_number="2.0",
                chunks=(ChunkSubmission("更新後的規章。"),),
            )
        )
        worker = ProcessNextOutboxEvent(
            self.uow_factory,
            self.embedding,
            self.vector_store,
            lease_seconds=60,
            retry_base_seconds=1,
        )

        await worker.execute()

        self.assertEqual(self.store.jobs[original.job_id].status, JobStatus.FAILED)
        self.assertEqual(
            self.store.documents[original.document_id].current_version_id,
            restored.version_id,
        )

    async def test_delete_race_queues_recovery_index_after_restore(self) -> None:
        submit = SubmitDocument(self.uow_factory, self.embedding)
        created = await submit.execute(submission())
        initial_worker = ProcessNextOutboxEvent(
            self.uow_factory,
            self.embedding,
            self.vector_store,
            lease_seconds=60,
            retry_base_seconds=1,
        )
        await initial_worker.execute()
        await DeleteDocument(self.uow_factory).execute(created.document_id)

        racing_vector_store = RestoreDuringDeleteVectorStore(self.store)
        racing_vector_store.points = dict(self.vector_store.points)
        worker = ProcessNextOutboxEvent(
            self.uow_factory,
            self.embedding,
            racing_vector_store,
            lease_seconds=60,
            retry_base_seconds=1,
        )
        await worker.execute()

        recovery_jobs = [
            job
            for job in self.store.jobs.values()
            if job.job_type.value == "index_version" and job.status == JobStatus.PENDING
        ]
        self.assertEqual(len(recovery_jobs), 1)
        self.assertIsNone(self.store.documents[created.document_id].deleted_at)
        await worker.execute()
        self.assertEqual(len(racing_vector_store.points), 2)

    async def test_new_embedding_revision_creates_new_vector_version(self) -> None:
        submit = SubmitDocument(self.uow_factory, self.embedding)
        first = await submit.execute(submission())
        self.embedding.revision = "test-2"

        second = await submit.execute(submission())

        self.assertTrue(second.created)
        self.assertNotEqual(second.version_id, first.version_id)
        self.assertEqual(len(self.store.versions), 2)

    async def test_non_retryable_model_mismatch_fails_immediately(self) -> None:
        created = await SubmitDocument(self.uow_factory, self.embedding).execute(submission())
        self.embedding.revision = "unexpected-revision"

        await ProcessNextOutboxEvent(
            self.uow_factory,
            self.embedding,
            self.vector_store,
            lease_seconds=60,
            retry_base_seconds=1,
        ).execute()

        job = self.store.jobs[created.job_id]
        event = next(iter(self.store.events.values()))
        self.assertEqual(job.attempt_count, 1)
        self.assertEqual(job.status, JobStatus.FAILED)
        self.assertEqual(event.status, OutboxStatus.FAILED)
        self.assertEqual(
            self.store.versions[created.version_id].indexing_status,
            IndexingStatus.FAILED,
        )

    async def test_metadata_update_syncs_payload_without_embedding(self) -> None:
        created = await SubmitDocument(self.uow_factory, self.embedding).execute(submission())
        worker = ProcessNextOutboxEvent(
            self.uow_factory,
            self.embedding,
            self.vector_store,
            lease_seconds=60,
            retry_base_seconds=1,
        )
        await worker.execute()
        embedding_calls_after_index = len(self.embedding.calls)

        update = UpdateDocumentMetadata(self.uow_factory)
        await update.execute(
            created.document_id,
            MetadataPatch(owner_department="People Operations"),
        )
        await worker.execute()

        self.assertEqual(len(self.embedding.calls), embedding_calls_after_index)
        self.assertEqual(self.vector_store.synced_documents, [created.document_id])
        self.assertEqual(
            self.store.documents[created.document_id].owner_department,
            "People Operations",
        )

    async def test_current_version_metadata_update_does_not_reembed(self) -> None:
        original = replace(
            submission(),
            effective_at=date(2026, 1, 1),
            expires_at=date(2026, 12, 31),
            review_at=date(2026, 6, 1),
        )
        created = await SubmitDocument(self.uow_factory, self.embedding).execute(original)
        worker = ProcessNextOutboxEvent(
            self.uow_factory,
            self.embedding,
            self.vector_store,
            lease_seconds=60,
            retry_base_seconds=1,
        )
        await worker.execute()
        embedding_calls_after_index = len(self.embedding.calls)

        await UpdateDocumentMetadata(self.uow_factory).execute(
            created.document_id,
            MetadataPatch(
                version_number="1.1",
                source_version="mtime:2",
                effective_at=date(2026, 2, 1),
                expires_at=date(2027, 1, 31),
                review_at=date(2026, 8, 1),
            ),
        )
        await worker.execute()

        version = self.store.versions[created.version_id]
        self.assertEqual(version.version_number, "1.1")
        self.assertEqual(version.source_version, "mtime:2")
        self.assertEqual(version.effective_at, date(2026, 2, 1))
        self.assertEqual(version.expires_at, date(2027, 1, 31))
        self.assertEqual(version.review_at, date(2026, 8, 1))
        self.assertEqual(len(self.embedding.calls), embedding_calls_after_index)

    async def test_current_version_nullable_dates_can_be_cleared(self) -> None:
        original = replace(
            submission(),
            effective_at=date(2026, 1, 1),
            expires_at=date(2026, 12, 31),
            review_at=date(2026, 6, 1),
        )
        created = await SubmitDocument(self.uow_factory, self.embedding).execute(original)
        await ProcessNextOutboxEvent(
            self.uow_factory,
            self.embedding,
            self.vector_store,
            lease_seconds=60,
            retry_base_seconds=1,
        ).execute()

        await UpdateDocumentMetadata(self.uow_factory).execute(
            created.document_id,
            MetadataPatch(effective_at=None, expires_at=None, review_at=None),
        )

        version = self.store.versions[created.version_id]
        self.assertIsNone(version.effective_at)
        self.assertIsNone(version.expires_at)
        self.assertIsNone(version.review_at)

    async def test_source_url_patch_distinguishes_omitted_from_explicit_null(self) -> None:
        created = await SubmitDocument(self.uow_factory, self.embedding).execute(
            replace(submission(), source_url="https://intranet.example/policy")
        )
        await ProcessNextOutboxEvent(
            self.uow_factory,
            self.embedding,
            self.vector_store,
            lease_seconds=60,
            retry_base_seconds=1,
        ).execute()
        update = UpdateDocumentMetadata(self.uow_factory)

        await update.execute(
            created.document_id,
            MetadataPatch(owner_department="People Operations"),
        )
        self.assertEqual(
            self.store.documents[created.document_id].source_url,
            "https://intranet.example/policy",
        )

        await update.execute(created.document_id, MetadataPatch(source_url=None))
        self.assertIsNone(self.store.documents[created.document_id].source_url)

        await update.execute(
            created.document_id,
            MetadataPatch(source_url="https://intranet.example/new-policy"),
        )
        self.assertEqual(
            self.store.documents[created.document_id].source_url,
            "https://intranet.example/new-policy",
        )

    async def test_current_version_metadata_rejects_invalid_date_range(self) -> None:
        original = replace(
            submission(),
            effective_at=date(2026, 1, 1),
            expires_at=date(2026, 12, 31),
        )
        created = await SubmitDocument(self.uow_factory, self.embedding).execute(original)
        await ProcessNextOutboxEvent(
            self.uow_factory,
            self.embedding,
            self.vector_store,
            lease_seconds=60,
            retry_base_seconds=1,
        ).execute()

        with self.assertRaises(ValidationError):
            await UpdateDocumentMetadata(self.uow_factory).execute(
                created.document_id,
                MetadataPatch(effective_at=date(2027, 1, 1)),
            )

        version = self.store.versions[created.version_id]
        self.assertEqual(version.effective_at, date(2026, 1, 1))
        self.assertEqual(version.expires_at, date(2026, 12, 31))

    async def test_same_content_post_does_not_replace_metadata(self) -> None:
        submit = SubmitDocument(self.uow_factory, self.embedding)
        created = await submit.execute(submission())
        await ProcessNextOutboxEvent(
            self.uow_factory,
            self.embedding,
            self.vector_store,
            lease_seconds=60,
            retry_base_seconds=1,
        ).execute()

        duplicate = await submit.execute(
            replace(
                submission(),
                title="Changed title",
                version_number="2.0",
                source_version="mtime:2",
            )
        )

        self.assertFalse(duplicate.created)
        self.assertEqual(self.store.documents[created.document_id].title, "Remote Work Policy")
        self.assertEqual(self.store.versions[created.version_id].version_number, "1.0")
        self.assertEqual(self.store.versions[created.version_id].source_version, "mtime:1")

    async def test_version_metadata_patch_requires_an_indexed_current_version(self) -> None:
        created = await SubmitDocument(self.uow_factory, self.embedding).execute(submission())

        with self.assertRaises(ConflictError):
            await UpdateDocumentMetadata(self.uow_factory).execute(
                created.document_id,
                MetadataPatch(source_version="mtime:2"),
            )

        self.assertEqual(self.store.versions[created.version_id].source_version, "mtime:1")

    async def test_search_rechecks_canonical_acl_before_returning_content(self) -> None:
        created = await SubmitDocument(self.uow_factory, self.embedding).execute(submission())
        worker = ProcessNextOutboxEvent(
            self.uow_factory,
            self.embedding,
            self.vector_store,
            lease_seconds=60,
            retry_base_seconds=1,
        )
        await worker.execute()
        await UpdateDocumentMetadata(self.uow_factory).execute(
            created.document_id,
            MetadataPatch(
                acl=(
                    AccessControlEntry(
                        PrincipalType.USER,
                        "blocked-user",
                        PermissionEffect.DENY,
                    ),
                )
            ),
        )
        chunk = next(iter(self.store.chunks.values()))
        self.vector_store.hits = [
            VectorSearchHit(
                point_id=str(chunk.id),
                score=0.99,
                payload={"chunk_id": str(chunk.id)},
            )
        ]

        result = await SearchKnowledge(
            self.uow_factory,
            self.embedding,
            self.vector_store,
            max_limit=20,
        ).execute(
            SearchRequest(
                query="遠端工作",
                filters=SearchFilters(user_id="blocked-user"),
            )
        )

        self.assertEqual(result.items, ())

    async def test_search_excludes_expired_document_version(self) -> None:
        expired = replace(submission(), expires_at=date(2000, 1, 1))
        created = await SubmitDocument(self.uow_factory, self.embedding).execute(expired)
        worker = ProcessNextOutboxEvent(
            self.uow_factory,
            self.embedding,
            self.vector_store,
            lease_seconds=60,
            retry_base_seconds=1,
        )
        await worker.execute()
        chunk = next(iter(self.store.chunks.values()))
        self.vector_store.hits = [
            VectorSearchHit(
                point_id=str(chunk.id),
                score=0.99,
                payload={"chunk_id": str(chunk.id)},
            )
        ]

        result = await SearchKnowledge(
            self.uow_factory,
            self.embedding,
            self.vector_store,
            max_limit=20,
        ).execute(SearchRequest(query="遠端工作"))

        self.assertEqual(created.version_id, chunk.version_id)
        self.assertEqual(result.items, ())

    async def test_search_progressively_fetches_and_returns_canonical_response_fields(
        self,
    ) -> None:
        created = await SubmitDocument(self.uow_factory, self.embedding).execute(
            replace(
                submission(),
                chunks=(
                    ChunkSubmission(
                        "員工每週可以遠端工作兩天。",
                        section_path="遠端工作",
                        token_count=17,
                    ),
                ),
            )
        )
        await ProcessNextOutboxEvent(
            self.uow_factory,
            self.embedding,
            self.vector_store,
            lease_seconds=60,
            retry_base_seconds=1,
        ).execute()
        chunk = next(iter(self.store.chunks.values()))
        stale_hits = [
            VectorSearchHit(
                point_id=str(missing_id),
                score=0.99 - index / 100,
                payload={"chunk_id": str(missing_id)},
            )
            for index, missing_id in enumerate((uuid4(), uuid4(), uuid4()))
        ]
        self.vector_store.hits = [
            *stale_hits,
            VectorSearchHit(
                point_id=str(chunk.id),
                score=0.87,
                payload={
                    "chunk_id": str(chunk.id),
                    "external_llm_allowed": True,
                    "confidentiality": Confidentiality.PUBLIC.value,
                    "language": "en-US",
                    "status": DocumentStatus.DRAFT.value,
                },
            ),
        ]
        single_document_reads = self.store.document_get_calls
        single_version_reads = self.store.version_get_calls
        batch_document_reads = self.store.document_get_many_calls
        batch_version_reads = self.store.version_get_many_calls

        result = await SearchKnowledge(
            self.uow_factory,
            self.embedding,
            self.vector_store,
            max_limit=20,
            max_candidates=20,
        ).execute(SearchRequest(query="遠端工作", limit=1))

        self.assertEqual(
            [(call[0], call[1]) for call in self.vector_store.search_calls], [(3, 0), (6, 3)]
        )
        self.assertEqual(len(result.items), 1)
        item = result.items[0]
        document = self.store.documents[created.document_id]
        self.assertEqual(item.chunk_id, chunk.id)
        self.assertEqual(item.score, 0.87)
        self.assertEqual(item.token_count, 17)
        self.assertEqual(item.chunk_index, 0)
        self.assertEqual(item.external_llm_allowed, document.external_llm_allowed)
        self.assertEqual(item.confidentiality, document.confidentiality)
        self.assertEqual(item.language, document.language)
        self.assertEqual(item.status, document.status)
        self.assertEqual(self.store.document_get_calls, single_document_reads)
        self.assertEqual(self.store.version_get_calls, single_version_reads)
        self.assertEqual(self.store.document_get_many_calls, batch_document_reads + 1)
        self.assertEqual(self.store.version_get_many_calls, batch_version_reads + 1)

    async def test_search_empty_statuses_fail_closed_to_published(self) -> None:
        await SubmitDocument(self.uow_factory, self.embedding).execute(
            replace(
                submission(),
                status=DocumentStatus.DRAFT,
                chunks=(ChunkSubmission("尚未發布的規章。"),),
            )
        )
        await ProcessNextOutboxEvent(
            self.uow_factory,
            self.embedding,
            self.vector_store,
            lease_seconds=60,
            retry_base_seconds=1,
        ).execute()
        chunk = next(iter(self.store.chunks.values()))
        self.vector_store.hits = [
            VectorSearchHit(
                point_id=str(chunk.id),
                score=0.99,
                payload={"chunk_id": str(chunk.id)},
            )
        ]

        result = await SearchKnowledge(
            self.uow_factory,
            self.embedding,
            self.vector_store,
            max_limit=20,
        ).execute(
            SearchRequest(
                query="規章",
                filters=SearchFilters(statuses=()),
            )
        )

        self.assertEqual(result.items, ())
        self.assertEqual(
            self.vector_store.search_calls[0][2].statuses,
            (DocumentStatus.PUBLISHED,),
        )

    async def test_search_batch_hydrates_multiple_documents_and_preserves_score_order(
        self,
    ) -> None:
        submit = SubmitDocument(self.uow_factory, self.embedding)
        first = await submit.execute(
            replace(submission(), chunks=(ChunkSubmission("第一份規章。"),))
        )
        second = await submit.execute(
            replace(
                submission(),
                source_item_id="company/second-policy.md",
                content_hash="b" * 64,
                title="Second Policy",
                chunks=(ChunkSubmission("第二份規章。"),),
            )
        )
        worker = ProcessNextOutboxEvent(
            self.uow_factory,
            self.embedding,
            self.vector_store,
            lease_seconds=60,
            retry_base_seconds=1,
        )
        await worker.execute()
        await worker.execute()
        chunks_by_document = {chunk.document_id: chunk for chunk in self.store.chunks.values()}
        first_chunk = chunks_by_document[first.document_id]
        second_chunk = chunks_by_document[second.document_id]
        self.vector_store.hits = [
            VectorSearchHit(
                point_id=str(second_chunk.id),
                score=0.95,
                payload={"chunk_id": str(second_chunk.id)},
            ),
            VectorSearchHit(
                point_id=str(first_chunk.id),
                score=0.90,
                payload={"chunk_id": str(first_chunk.id)},
            ),
        ]
        single_document_reads = self.store.document_get_calls
        single_version_reads = self.store.version_get_calls
        batch_document_reads = self.store.document_get_many_calls
        batch_version_reads = self.store.version_get_many_calls

        result = await SearchKnowledge(
            self.uow_factory,
            self.embedding,
            self.vector_store,
            max_limit=20,
        ).execute(SearchRequest(query="規章", limit=2))

        self.assertEqual(
            [item.document_id for item in result.items],
            [second.document_id, first.document_id],
        )
        self.assertEqual(self.store.document_get_calls, single_document_reads)
        self.assertEqual(self.store.version_get_calls, single_version_reads)
        self.assertEqual(self.store.document_get_many_calls, batch_document_reads + 1)
        self.assertEqual(self.store.version_get_many_calls, batch_version_reads + 1)

    async def test_search_progressive_fetch_stops_at_candidate_bound(self) -> None:
        await SubmitDocument(self.uow_factory, self.embedding).execute(
            replace(submission(), chunks=(ChunkSubmission("有效規章。"),))
        )
        await ProcessNextOutboxEvent(
            self.uow_factory,
            self.embedding,
            self.vector_store,
            lease_seconds=60,
            retry_base_seconds=1,
        ).execute()
        chunk = next(iter(self.store.chunks.values()))
        missing_ids = [uuid4() for _ in range(4)]
        self.vector_store.hits = [
            *[
                VectorSearchHit(
                    point_id=str(missing_id),
                    score=0.99 - index / 100,
                    payload={"chunk_id": str(missing_id)},
                )
                for index, missing_id in enumerate(missing_ids)
            ],
            VectorSearchHit(
                point_id=str(chunk.id),
                score=0.80,
                payload={"chunk_id": str(chunk.id)},
            ),
        ]

        result = await SearchKnowledge(
            self.uow_factory,
            self.embedding,
            self.vector_store,
            max_limit=1,
            max_candidates=4,
        ).execute(SearchRequest(query="規章", limit=1))

        self.assertEqual(result.items, ())
        self.assertEqual(
            [(call[0], call[1]) for call in self.vector_store.search_calls], [(3, 0), (1, 3)]
        )

    async def test_search_lifecycle_uses_configured_business_timezone(self) -> None:
        await SubmitDocument(self.uow_factory, self.embedding).execute(
            replace(
                submission(),
                effective_at=date(2026, 8, 4),
                chunks=(ChunkSubmission("八月四日生效的規章。"),),
            )
        )
        await ProcessNextOutboxEvent(
            self.uow_factory,
            self.embedding,
            self.vector_store,
            lease_seconds=60,
            retry_base_seconds=1,
        ).execute()
        chunk = next(iter(self.store.chunks.values()))
        self.vector_store.hits = [
            VectorSearchHit(
                point_id=str(chunk.id),
                score=0.99,
                payload={"chunk_id": str(chunk.id)},
            )
        ]
        fixed_utc = datetime(2026, 8, 3, 16, 30, tzinfo=UTC)

        with patch(
            "knowledge_index_service.application.use_cases.utc_now",
            return_value=fixed_utc,
        ):
            taipei_result = await SearchKnowledge(
                self.uow_factory,
                self.embedding,
                self.vector_store,
                max_limit=20,
                business_timezone="Asia/Taipei",
            ).execute(SearchRequest(query="規章"))
            utc_result = await SearchKnowledge(
                self.uow_factory,
                self.embedding,
                self.vector_store,
                max_limit=20,
                business_timezone="UTC",
            ).execute(SearchRequest(query="規章"))

        self.assertEqual(len(taipei_result.items), 1)
        self.assertEqual(
            taipei_result.items[0].token_count,
            len("八月四日生效的規章。".encode()),
        )
        self.assertEqual(utc_result.items, ())


if __name__ == "__main__":
    unittest.main()
