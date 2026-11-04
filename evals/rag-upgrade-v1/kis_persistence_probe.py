"""僅在 U0 私有網路執行的真實 KIS/PG/Qdrant probe，輸出 content-free mapping。

stdin 的自製 payload 只在記憶體使用；DB 直接查詢一律 READ ONLY。
HTTP submission/replay 寫入的只有本次全新、tmpfs-backed 測試知識庫。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import sys
import time

import httpx
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from audit_parsers import MODEL_ID, MODEL_REVISION


class ProbeFailure(RuntimeError):
    """只攜帶程式內固定 check code，不包含資料或 provider exception message。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def require(condition: bool, code: str) -> None:
    if not condition:
        raise ProbeFailure(code)


def guards(bundle: dict) -> None:
    require(
        bool(re.fullmatch(r"[a-f0-9]{32}", os.environ.get("U0_RUN_ID", ""))),
        "isolated_run_required",
    )
    url = make_url(os.environ["DATABASE_URL"])
    require(
        url.host == "postgres"
        and url.database == "rag_u0"
        and url.username == "u0_only",
        "isolated_database_required",
    )
    fixtures = bundle["fixtures"]
    require(
        len(fixtures) == 40 and len({f["fixture"] for f in fixtures}) == 40,
        "fixture_count",
    )
    for fixture in fixtures:
        require(
            bool(
                re.fullmatch(
                    r"[TH]0[1-5]/(?:rules\.md|catalog\.docx|procedure\.pdf|notes\.txt)",
                    fixture["fixture"],
                )
            ),
            "synthetic_fixture_required",
        )
        body = fixture["body"]
        require(
            body["custom_metadata"].get("synthetic") is True
            and body["external_llm_allowed"] is False
            and body["source_url"] is None
            and body["acl"]
            == [
                {
                    "principal_type": "group",
                    "principal_id": "rag-u0-fixtures",
                    "effect": "allow",
                }
            ],
            "synthetic_governance_required",
        )


async def request(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    body: dict | None = None,
    *,
    status: int = 200,
) -> dict:
    async with client.stream(method, path, json=body) as response:
        require(response.status_code == status, "unexpected_http_status")
        require(
            response.headers.get("content-type", "").split(";", 1)[0]
            == "application/json",
            "unexpected_content_type",
        )
        data = bytearray()
        async for part in response.aiter_bytes():
            data.extend(part)
            require(len(data) <= 2_000_000, "response_too_large")
        return json.loads(data)


async def readiness(client: httpx.AsyncClient) -> None:
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        try:
            value = await request(client, "GET", "/health/ready")
            if value.get("status") == "ok" and all(value.get("checks", {}).values()):
                return
        except (httpx.HTTPError, ValueError, ProbeFailure):
            await asyncio.sleep(2)
    raise TimeoutError("isolated_api_readiness")


async def rows(engine, statement: str) -> list[dict]:
    async with engine.begin() as connection:
        await connection.execute(text("SET TRANSACTION READ ONLY"))
        return [
            dict(row) for row in (await connection.execute(text(statement))).mappings()
        ]


async def wait_jobs(engine) -> dict:
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        jobs = await rows(
            engine, "SELECT status, error_code, error_message FROM indexing_jobs"
        )
        failed = [j for j in jobs if j["status"] == "failed"]
        if failed:
            code = failed[0]["error_code"]
            allowed = {
                "ServiceNotReadyError",
                "UnexpectedResponse",
                "ValidationError",
                "ConflictError",
                "RuntimeError",
                "OSError",
                "PermissionError",
            }
            category = code.lower() if code in allowed else "other"
            if "No space left on device" in (failed[0]["error_message"] or ""):
                category = "no_space_left"
            raise ProbeFailure("index_job_failed_" + category)
        if len(jobs) == 40 and all(j["status"] == "completed" for j in jobs):
            return {"jobs_completed": 40, "jobs_failed": 0}
        await asyncio.sleep(2)
    raise TimeoutError("index_jobs_deadline")


async def submit(client, bundle: dict) -> dict:
    from knowledge_index_service.presentation.schemas import (
        DocumentSubmissionRequest,
        DocumentSubmissionResponse,
    )

    await readiness(client)
    receipts = []
    for fixture in bundle["fixtures"]:
        body = DocumentSubmissionRequest.model_validate(fixture["body"])
        result = DocumentSubmissionResponse.model_validate(
            await request(
                client,
                "POST",
                "/api/v1/documents",
                body.model_dump(mode="json"),
                status=202,
            )
        )
        require(result.created and result.job_id is not None, "initial_submission")
        receipts.append(
            {"fixture": fixture["fixture"], **result.model_dump(mode="json")}
        )
    return {"receipts": receipts, "submitted": len(receipts)}


def chunk_check(row: dict, expected: dict) -> None:
    for field in ("content", "section_path", "page_number", "token_count"):
        require(row[field] == expected[field], "canonical_chunk_mismatch")
    require(
        row["content_hash"] == hashlib.sha256(row["content"].encode()).hexdigest(),
        "canonical_chunk_hash",
    )
    require(
        row["indexing_status"] == "indexed"
        and row["qdrant_point_id"] == str(row["id"]),
        "canonical_chunk_index_state",
    )


async def verify(client, engine, bundle: dict) -> dict:
    from knowledge_index_service.presentation.schemas import (
        DocumentSubmissionResponse,
        JobResponse,
        SearchResponse,
    )

    await readiness(client)
    version = (await rows(engine, "SHOW server_version_num"))[0]["server_version_num"]
    require(int(version) // 10000 == 16, "postgres_version")
    docs = await rows(engine, "SELECT * FROM documents")
    versions = await rows(engine, "SELECT * FROM document_versions")
    chunks = await rows(engine, "SELECT * FROM chunks ORDER BY version_id, chunk_index")
    acl = await rows(engine, "SELECT * FROM document_acl")
    require(len(docs) == len(versions) == len(acl) == 40, "canonical_counts")
    require(
        len(chunks) == sum(len(f["body"]["chunks"]) for f in bundle["fixtures"]),
        "canonical_chunk_count",
    )
    require(
        (
            await rows(
                engine,
                "SELECT count(*) AS n FROM outbox_events WHERE status <> 'processed'",
            )
        )[0]["n"]
        == 0,
        "outbox_not_completed",
    )
    by_source = {d["source_item_id"]: d for d in docs}
    by_version = {str(v["id"]): v for v in versions}
    receipts = {r["fixture"]: r for r in bundle["receipts"]}
    canonical = {str(c["id"]): c for c in chunks}
    mapping = []
    for fixture in bundle["fixtures"]:
        body = fixture["body"]
        receipt = receipts[fixture["fixture"]]
        doc = by_source[body["source_item_id"]]
        require(
            str(doc["id"]) == receipt["document_id"]
            and str(doc["current_version_id"]) == receipt["version_id"],
            "document_binding",
        )
        for field in (
            "title",
            "document_type",
            "owner_department",
            "document_owner",
            "confidentiality",
            "status",
            "language",
            "external_llm_allowed",
            "source_path",
            "source_url",
            "custom_metadata",
        ):
            require(doc[field] == body[field], "canonical_metadata_mismatch")
        entries = [a for a in acl if a["document_id"] == doc["id"]]
        require(
            [
                {k: a[k] for k in ("principal_type", "principal_id", "effect")}
                for a in entries
            ]
            == body["acl"],
            "canonical_acl",
        )
        stored = by_version[receipt["version_id"]]
        for field in (
            "version_number",
            "source_version",
            "content_hash",
            "mime_type",
            "file_size",
            "parser_name",
            "parser_version",
        ):
            require(stored[field] == body[field], "canonical_version_mismatch")
        require(
            stored["embedding_model"] == MODEL_ID
            and stored["embedding_model_revision"] == MODEL_REVISION
            and stored["embedding_dimension"] == 384
            and stored["indexing_status"] == "indexed",
            "embedding_contract",
        )
        own = [c for c in chunks if str(c["version_id"]) == receipt["version_id"]]
        require(
            len(own) == stored["chunk_count"] == len(body["chunks"]),
            "version_chunk_count",
        )
        for index, (chunk, expected) in enumerate(
            zip(own, body["chunks"], strict=True)
        ):
            require(
                chunk["chunk_index"] == index and chunk["document_id"] == doc["id"],
                "chunk_binding",
            )
            chunk_check(chunk, expected)
            # 記錄 hash/頁碼/ID，不記錄 content、路徑或章節文字。
            mapping.append(
                {
                    "fixture": fixture["fixture"],
                    "document_id": str(doc["id"]),
                    "version_id": receipt["version_id"],
                    "chunk_id": str(chunk["id"]),
                    "chunk_index": index,
                    "page_number": chunk["page_number"],
                    "token_count": chunk["token_count"],
                    "content_sha256": chunk["content_hash"],
                    "section_sha256": hashlib.sha256(
                        (chunk["section_path"] or "").encode()
                    ).hexdigest(),
                }
            )
        job = JobResponse.model_validate(
            await request(client, "GET", f"/api/v1/jobs/{receipt['job_id']}")
        )
        require(
            job.status.value == "completed"
            and str(job.document_id) == receipt["document_id"],
            "job_http_contract",
        )
        replay = DocumentSubmissionResponse.model_validate(
            await request(
                client,
                "POST",
                "/api/v1/documents",
                body,
                status=202,
            )
        )
        require(
            not replay.created
            and str(replay.document_id) == receipt["document_id"]
            and str(replay.version_id) == receipt["version_id"]
            and replay.indexing_status.value == "indexed",
            "idempotent_replay",
        )
        search = SearchResponse.model_validate(
            await request(
                client,
                "POST",
                "/api/v1/search",
                {
                    "query": body["chunks"][0]["content"],
                    "limit": 50,
                    "filters": {"group_ids": ["rag-u0-fixtures"]},
                },
            )
        )
        require(
            search.model_id == MODEL_ID
            and search.model_revision == MODEL_REVISION
            and any(str(i.document_id) == receipt["document_id"] for i in search.items),
            "self_query_not_found",
        )
        for hit in search.items:
            chunk = canonical[str(hit.chunk_id)]
            for field in (
                "content",
                "section_path",
                "page_number",
                "token_count",
                "chunk_index",
            ):
                require(
                    getattr(hit, field) == chunk[field], "search_hydration_mismatch"
                )
            require(hit.external_llm_allowed is False, "search_governance")
    denied = await request(
        client,
        "POST",
        "/api/v1/search",
        {
            "query": "U0 synthetic",
            "limit": 50,
            "filters": {"group_ids": ["unrelated-u0-group"]},
        },
    )
    require(denied["items"] == [], "acl_fail_closed")
    collection = (
        "rag_u0_fixture_"
        + hashlib.sha256(f"{MODEL_ID}@{MODEL_REVISION}".encode()).hexdigest()[:12]
    )
    async with httpx.AsyncClient(
        base_url="http://qdrant:6333",
        trust_env=False,
        follow_redirects=False,
        timeout=30,
        headers={"api-key": os.environ["QDRANT_API_KEY"]},
    ) as qdrant:
        collections = await request(qdrant, "GET", "/collections")
        require(
            [c["name"] for c in collections["result"]["collections"]] == [collection],
            "isolated_collection_count",
        )
        points = (
            await request(
                qdrant,
                "POST",
                f"/collections/{collection}/points",
                {
                    "ids": list(canonical),
                    "with_payload": True,
                    "with_vector": True,
                },
            )
        )["result"]
        require(len(points) == len(chunks), "vector_count")
        for point in points:
            chunk = canonical[str(point["id"])]
            doc = next(d for d in docs if d["id"] == chunk["document_id"])
            expected_payload = {
                "chunk_id": str(chunk["id"]),
                "document_id": str(doc["id"]),
                "version_id": str(chunk["version_id"]),
                "is_current": True,
                "status": doc["status"],
                "confidentiality": doc["confidentiality"],
                "language": doc["language"],
                "external_llm_allowed": False,
                "acl_public": False,
                "allowed_user_ids": [],
                "allowed_group_ids": ["rag-u0-fixtures"],
                "denied_user_ids": [],
                "denied_group_ids": [],
            }
            require(point["payload"] == expected_payload, "vector_payload_mismatch")
            vector = point["vector"]
            require(
                len(vector) == 384
                and all(math.isfinite(x) for x in vector)
                and abs(sum(x * x for x in vector) - 1) < 0.01,
                "vector_dimension_norm",
            )
        total = (
            await request(
                qdrant,
                "POST",
                f"/collections/{collection}/points/count",
                {"exact": True},
            )
        )["result"]["count"]
        require(total == len(chunks), "vector_duplicate_points")
    for table, expected in (
        ("documents", 40),
        ("document_versions", 40),
        ("indexing_jobs", 40),
        ("chunks", len(chunks)),
    ):
        require(
            (await rows(engine, f"SELECT count(*) AS n FROM {table}"))[0]["n"]
            == expected,
            "replay_duplicate_rows",
        )
    return {
        "status": "PASSED",
        "documents": 40,
        "versions": 40,
        "chunks": len(chunks),
        "jobs_completed": 40,
        "jobs_failed": 0,
        "vector_points": len(points),
        "vector_dimension": 384,
        "canonical_mismatches": 0,
        "replays_verified": 40,
        "self_queries_verified": 40,
        "unauthorized_queries_verified": 1,
        "postgres_major": 16,
        "mapping": mapping,
    }


async def run(mode: str, bundle: dict) -> dict:
    guards(bundle)
    engine = create_async_engine(
        os.environ["DATABASE_URL"], pool_size=1, max_overflow=0
    )
    try:
        async with httpx.AsyncClient(
            base_url="http://kis-api:8080",
            trust_env=False,
            follow_redirects=False,
            timeout=60,
            headers={"x-internal-api-key": os.environ["API_INTERNAL_KEY"]},
        ) as client:
            if mode == "submit":
                return await submit(client, bundle)
            if mode == "wait":
                return await wait_jobs(engine)
            return await verify(client, engine, bundle)
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("submit", "wait", "verify"))
    mode = parser.parse_args().mode
    try:
        incoming = sys.stdin.buffer.read(2_000_001)
        require(len(incoming) <= 2_000_000, "input_too_large")
        result = asyncio.run(run(mode, json.loads(incoming)))
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        # ValueError message 也不信任（SQLAlchemy/Pydantic 可能含 input/URL）。
        print(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "error_type": type(exc).__name__,
                    "check": exc.code if isinstance(exc, ProbeFailure) else None,
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
