"""Installed Retrieval use case + 真 KIS HTTP；只有自製 restricted evidence。

Observer 只記錄實際 port 回傳，不 fake KIS、不重寫 selector、不猜排除原因。
不啟動 Chat/Answer，不保存原文/attestation；所有內容僅在 stdin/記憶體。
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import logging
import math
import os
import platform
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REVISION = ROOT / "revisions/u0-synthetic-v2"
POLICY_FIELDS = (
    "default_retrieval_limit",
    "max_retrieval_limit",
    "max_candidate_limit",
    "candidate_multiplier",
    "max_context_tokens",
    "max_chunks_per_document",
    "score_threshold",
    "knowledge_index_connect_timeout_seconds",
    "knowledge_index_read_timeout_seconds",
    "knowledge_index_total_timeout_seconds",
    "knowledge_index_readiness_timeout_seconds",
    "knowledge_index_max_response_body_bytes",
    "knowledge_index_retry_attempts",
    "knowledge_index_retry_backoff_seconds",
    "knowledge_index_retry_max_backoff_seconds",
)
MODULES = (
    "application.dtos",
    "application.ports",
    "application.use_cases",
    "domain.entities",
    "domain.enums",
    "domain.exceptions",
    "infrastructure.config",
    "infrastructure.knowledge_index.http_client",
    "infrastructure.request_context",
)


class TraceFailure(RuntimeError):
    """只攜帶程式固定 code；不顯示下游 exception/body。"""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def require(condition: bool, code: str) -> None:
    if not condition:
        raise TraceFailure(code)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def percentile(values: list[float], fraction: float) -> float:
    require(
        bool(values)
        and 0 < fraction <= 1
        and all(math.isfinite(value) and value >= 0 for value in values),
        "trace_latency_invalid",
    )
    return round(sorted(values)[math.ceil(len(values) * fraction) - 1], 3)


def project(candidate, mapping: dict, bodies: dict, *, rank: int) -> dict:
    require(type(rank) is int and rank > 0, "trace_rank_invalid")
    chunk_id = str(candidate.chunk_id)
    require(chunk_id in mapping, "trace_foreign_chunk")
    entry = mapping[chunk_id]
    body = bodies[entry["fixture"]]
    chunk = body["chunks"][entry["chunk_index"]]
    require(
        str(candidate.document_id) == entry["document_id"]
        and str(candidate.version_id) == entry["version_id"]
        and candidate.content == chunk["content"]
        and digest(candidate.content) == entry["content_sha256"]
        and candidate.title == body["title"]
        and candidate.document_type == body["document_type"]
        and candidate.section_path == chunk["section_path"]
        and candidate.page_number == chunk["page_number"]
        and candidate.token_count == entry["token_count"]
        and candidate.chunk_index == entry["chunk_index"]
        and candidate.language == body["language"]
        and candidate.confidentiality.value == body["confidentiality"]
        and candidate.external_llm_allowed is False
        and math.isfinite(candidate.score),
        "trace_canonical_or_governance_changed",
    )
    return {
        "rank": rank,
        **entry,
        "score": candidate.score,
        "external_llm_allowed": False,
    }


def selected_projection(evidence, candidates: dict) -> dict:
    chunk_id = str(evidence.chunk_id)
    require(chunk_id in candidates, "trace_selected_not_candidate")
    candidate, projected = candidates[chunk_id]
    require(
        evidence.document_id == candidate.document_id
        and evidence.version_id == candidate.version_id
        and evidence.content == candidate.content
        and evidence.document_type == candidate.document_type
        and evidence.token_count == candidate.token_count
        and evidence.score == candidate.score
        and evidence.citation.title == candidate.title
        and evidence.citation.section_path == candidate.section_path
        and evidence.citation.page_number == candidate.page_number
        and evidence.governance.external_llm_allowed is False
        and evidence.governance.confidentiality == candidate.confidentiality
        and evidence.governance.language == candidate.language,
        "trace_selected_canonical_changed",
    )
    return {**projected, "rank": evidence.rank, "channel": "restricted_evidence"}


async def wait_ready(observer, *, timeout_seconds: float = 60) -> None:
    """restart 返回不等於模型載入完成；bounded readiness polling，不重送 search。"""
    try:
        async with asyncio.timeout(timeout_seconds):
            while not await observer.is_ready():
                await asyncio.sleep(1)
    except TimeoutError:
        raise TraceFailure("trace_private_kis_not_ready") from None


async def collect(incoming: dict) -> dict:
    from retrieval_service.application.dtos import RetrievalPolicy
    from retrieval_service.application.use_cases import RetrieveEvidence
    from retrieval_service.domain.entities import PrincipalContext, RetrievalQuery
    from retrieval_service.infrastructure.config import Settings
    from retrieval_service.infrastructure.knowledge_index.http_client import (
        KnowledgeIndexHttpClient,
    )

    require(os.environ.get("U0_RUN_ID") == incoming["run_id"], "trace_run_binding")
    bundle, verified = incoming["bundle"], incoming["verified"]
    require(verified.get("status") == "PASSED", "trace_verified_persistence_required")
    require(
        bundle["dataset_manifest_sha256"]
        == verified["dataset_manifest_sha256"]
        == hashlib.sha256((REVISION / "manifest.json").read_bytes()).hexdigest(),
        "trace_dataset_binding",
    )
    values = incoming["settings"]
    require(set(values) == set(POLICY_FIELDS), "trace_policy_allowlist")
    settings = Settings(_env_file=None, **values)
    actual = {key: getattr(settings, key) for key in POLICY_FIELDS}
    require(actual == values, "trace_policy_coercion")
    policy = RetrievalPolicy(
        candidate_multiplier=settings.candidate_multiplier,
        max_candidate_limit=settings.max_candidate_limit,
        max_context_tokens=settings.max_context_tokens,
        max_chunks_per_document=settings.max_chunks_per_document,
        score_threshold=settings.score_threshold,
    )
    adapter = KnowledgeIndexHttpClient(
        "http://kis-api:8080",
        api_key=os.environ["API_INTERNAL_KEY"],
        **{
            key.removeprefix("knowledge_index_"): value
            for key, value in values.items()
            if key.startswith("knowledge_index_")
        },
    )

    class Observer:
        """純委派；實際 candidates 由 installed HTTP adapter 取得。"""

        def __init__(self):
            self.result = None
            self.criteria = None

        async def search(self, criteria):
            self.criteria = criteria
            self.result = await adapter.search(criteria)
            return self.result

        async def is_ready(self):
            return await adapter.is_ready()

    observer = Observer()
    use_case = RetrieveEvidence(observer, policy)
    mapping = {entry["chunk_id"]: entry for entry in verified["mapping"]}
    bodies = {entry["fixture"]: entry["body"] for entry in bundle["fixtures"]}
    require(len(mapping) == len(verified["mapping"]) == 91, "trace_mapping_count")
    require(len(bodies) == 40, "trace_fixture_count")
    require(
        all(
            body["external_llm_allowed"] is False
            and body["custom_metadata"].get("synthetic") is True
            for body in bodies.values()
        ),
        "trace_synthetic_restricted_required",
    )
    cases = json.loads((REVISION / "datasets/tuning.json").read_text())
    require(
        len(cases) == len({case["id"] for case in cases}) == 40
        and all(case["split"] == "tuning" for case in cases),
        "trace_tuning_count",
    )
    records, durations = [], []
    try:
        await wait_ready(observer)
        for case in cases:
            start = time.monotonic()
            result = await use_case.execute(
                RetrievalQuery(
                    case["question"], settings.default_retrieval_limit, "zh-TW"
                ),
                PrincipalContext("rag-u0-trace", ("rag-u0-fixtures",)),
            )
            elapsed = (time.monotonic() - start) * 1000
            candidates = observer.result.candidates
            require(
                result.candidate_count == len(candidates)
                and len(candidates) <= observer.criteria.limit
                and len({c.chunk_id for c in candidates}) == len(candidates),
                "trace_candidate_count",
            )
            projected = [
                project(c, mapping, bodies, rank=rank)
                for rank, c in enumerate(candidates, 1)
            ]
            by_id = {
                str(c.chunk_id): (c, item)
                for c, item in zip(candidates, projected, strict=True)
            }
            selected = [selected_projection(e, by_id) for e in result.all_evidence]
            require(
                not result.llm_context
                and result.status.value
                in {"external_generation_blocked", "insufficient_evidence"}
                and len(selected) <= settings.default_retrieval_limit,
                "trace_restricted_status",
            )
            durations.append(elapsed)
            records.append(
                {
                    "case_id": case["id"],
                    "type": case["type"],
                    "split": "tuning",
                    "run": 1,
                    "original_question_sha256": digest(case["question"]),
                    "candidate_limit": observer.criteria.limit,
                    "candidates": projected,
                    "selected_evidence": selected,
                    "status": result.status.value,
                    "allowed_context_count": 0,
                    "restricted_count": len(selected),
                    "unselected_chunk_ids": [
                        c["chunk_id"]
                        for c in projected
                        if c["chunk_id"] not in {e["chunk_id"] for e in selected}
                    ],
                    "exclusion_reasons": "NOT_OBSERVABLE_IN_UNMODIFIED_V1_USE_CASE",
                    "usecase_http_wall_ms": round(elapsed, 3),
                }
            )
    finally:
        await adapter.close()
    hashes = {}
    for name in MODULES:
        module = importlib.import_module(f"retrieval_service.{name}")
        hashes[name] = hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
    return {
        "stage": (
            "installed_retrieval_usecase_and_real_kis_http_not_chat_route_or_answer"
        ),
        "status": "COLLECTED_NOT_QUALITY_SCORED",
        "python": platform.python_version(),
        "machine": platform.machine(),
        "settings_source": "live_retrieval_whitelist_snapshot",
        "settings": actual,
        "settings_sha256": digest(
            json.dumps(actual, sort_keys=True, separators=(",", ":"))
        ),
        "installed_modules_sha256": hashes,
        "cases": 40,
        "runs_per_case": 1,
        "followup_history_used": False,
        "allowed_context_count": 0,
        "answer_generation": "NOT_RUN",
        "attestation": "NOT_ISSUED_RESTRICTED_ONLY",
        "holdout_queries": 0,
        "http_attempt_count": (
            "NOT_INSTRUMENTED_INSTALLED_ADAPTER_RETRY_SETTING_RECORDED"
        ),
        "relevance_labels": "PENDING_HUMAN",
        "recall_ndcg": "NOT_SCORED",
        "latency_measurement": (
            "sequential_warm_usecase_plus_kis_http_not_route_or_production_e2e"
        ),
        "usecase_p50_ms": percentile(durations, 0.5),
        "usecase_p95_ms": percentile(durations, 0.95),
        "records": records,
    }


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    try:
        raw = sys.stdin.buffer.read(2_000_001)
        require(len(raw) <= 2_000_000, "trace_input_bound")
        print(json.dumps(asyncio.run(collect(json.loads(raw))), ensure_ascii=False))
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "error_type": type(exc).__name__,
                    "check": exc.code if isinstance(exc, TraceFailure) else None,
                }
            )
        )
        raise SystemExit(2) from None
