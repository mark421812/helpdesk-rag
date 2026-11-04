"""真實 tuning KIS 查詢；只輸出核對過的 synthetic ID/hash，不作品質評分。

limit=30 是入口診斷，不冒充 Retrieval candidates/selected context。
不改寫 query、不調參、不讀 holdout 執行結果、不呼叫 LLM。
"""

from __future__ import annotations

import hashlib
import math
import time
from collections import Counter

from build_revision_v2 import REVISION
from kis_persistence_probe import request, require
from runner import TYPES, read_json


def private_kis_url(url) -> bool:
    # HTTPX 字串可省略根路徑 slash；用解析欄位而非字串外觀核對。
    return (
        url.scheme == "http"
        and url.host == "kis-api"
        and url.port == 8080
        and url.path == "/"
        and not url.userinfo
        and not url.query
        and not url.fragment
    )


def percentile(values: list[float], fraction: float) -> float:
    require(bool(values) and 0 < fraction <= 1, "latency_percentile_input")
    require(all(math.isfinite(v) and v >= 0 for v in values), "latency_value")
    # nearest-rank；不是三次 runs 或 production p95。
    return round(sorted(values)[math.ceil(len(values) * fraction) - 1], 3)


def project_hit(hit: dict, mapping: dict, bodies: dict) -> dict:
    require(hit["chunk_id"] in mapping, "foreign_candidate_chunk")
    entry = mapping[hit["chunk_id"]]
    body = bodies[entry["fixture"]]
    chunk = body["chunks"][entry["chunk_index"]]
    require(
        hit["document_id"] == entry["document_id"]
        and hit["version_id"] == entry["version_id"]
        and hit["content"] == chunk["content"]
        and hit["page_number"] == chunk["page_number"]
        and hit["section_path"] == chunk["section_path"]
        and hit["token_count"] == chunk["token_count"]
        and hit["custom_metadata"] == body["custom_metadata"]
        and hit["external_llm_allowed"] is False
        and math.isfinite(hit["score"]),
        "candidate_canonical_or_governance_changed",
    )
    return {
        "fixture": entry["fixture"],
        "chunk_id": entry["chunk_id"],
        "document_id": entry["document_id"],
        "version_id": entry["version_id"],
        "chunk_index": entry["chunk_index"],
        "score": hit["score"],
        "content_sha256": entry["content_sha256"],
        "section_sha256": entry["section_sha256"],
        "page_number": entry["page_number"],
        "token_count": entry["token_count"],
        "external_llm_allowed": False,
    }


async def collect(client, bundle: dict, verified: dict) -> dict:
    from knowledge_index_service.presentation.schemas import SearchResponse

    require(private_kis_url(client.base_url), "private_kis_client_required")
    cases = read_json(REVISION / "datasets/tuning.json")
    require(
        len(cases) == len({c["id"] for c in cases}) == 40
        and all(c["split"] == "tuning" for c in cases)
        and Counter(c["type"] for c in cases) == Counter({t: 5 for t in TYPES}),
        "tuning_collection_incomplete",
    )
    mapping = {m["chunk_id"]: m for m in verified["mapping"]}
    bodies = {f["fixture"]: f["body"] for f in bundle["fixtures"]}
    records, durations = [], []
    for case in cases:
        start = time.monotonic()
        response = SearchResponse.model_validate(
            await request(
                client,
                "POST",
                "/api/v1/search",
                {
                    "query": case["question"],
                    "limit": 30,
                    "filters": {
                        "group_ids": ["rag-u0-fixtures"],
                        "statuses": ["published"],
                        "language": "zh-TW",
                    },
                },
            )
        )
        elapsed = (time.monotonic() - start) * 1000
        candidates = [
            {"rank": rank, **project_hit(hit.model_dump(mode="json"), mapping, bodies)}
            for rank, hit in enumerate(response.items, 1)
        ]
        require(
            len(candidates) <= 30
            and len({h["chunk_id"] for h in candidates}) == len(candidates),
            "candidate_count_or_duplicate",
        )
        durations.append(elapsed)
        records.append(
            {
                "case_id": case["id"],
                "type": case["type"],
                "split": "tuning",
                "query_source": "dataset_question_unmodified",
                "original_question_sha256": hashlib.sha256(
                    case["question"].encode()
                ).hexdigest(),
                "run": 1,
                "candidate_count": len(candidates),
                "kis_http_wall_ms": round(elapsed, 3),
                "candidates": candidates,
            }
        )
    return {
        "stage": "kis_v1_candidates_before_retrieval_or_answer",
        "status": "COLLECTED_NOT_QUALITY_SCORED",
        "cases": 40,
        "runs_per_case": 1,
        "case_types": dict(sorted(Counter(c["type"] for c in cases).items())),
        "query_limit": 30,
        "http_attempts": 40,
        "http_retry": False,
        "followup_history_used": False,
        "relevance_labels": "PENDING_HUMAN",
        "recall_ndcg": "NOT_SCORED",
        "retrieval_selection": "NOT_RUN",
        "answer_generation": "NOT_RUN",
        "holdout_queries": 0,
        "latency_measurement": "sequential_warm_kis_http_only_not_production_e2e",
        "kis_http_p50_ms": percentile(durations, 0.5),
        "kis_http_p95_ms": percentile(durations, 0.95),
        "records": records,
    }
