"""三個同時啟動的 client lanes；沿用 installed Retrieval/real KIS collector。

每 lane 完整跑 40 tuning 題一次，不重試整個 lane、不選最好結果。
這是 restricted-only 負載盤點，不是三次模型生成或 Chat E2E。
"""

from __future__ import annotations

import asyncio
import json
import logging
import resource
import sys
import time

from retrieval_trace_v2 import TraceFailure, collect, percentile, require


def compact_record(row: dict, run: int) -> dict:
    """source UUID/page/token/hash 在外層 mapping 只存一次，避免突破 2 MB pipe。

    collect 已逐次核對 canonical；此處仍保留全部 run 的順序/分數/選取，
    不拿 first/best run 取代其他結果，也不提高 response bound。
    """
    fields = (
        "case_id",
        "type",
        "split",
        "original_question_sha256",
        "candidate_limit",
        "status",
        "allowed_context_count",
        "restricted_count",
        "unselected_chunk_ids",
        "exclusion_reasons",
        "usecase_http_wall_ms",
    )
    return {
        **{key: row[key] for key in fields},
        "run": run,
        "candidates": [
            {key: candidate[key] for key in ("chunk_id", "rank", "score")}
            for candidate in row["candidates"]
        ],
        "selected_evidence": [
            {key: selected[key] for key in ("chunk_id", "rank", "channel")}
            for selected in row["selected_evidence"]
        ],
    }


def merge_runs(results: list[dict]) -> dict:
    require(len(results) == 3, "load_three_lanes_required")
    first = results[0]
    identity = (
        "stage",
        "status",
        "python",
        "machine",
        "settings",
        "settings_sha256",
        "installed_modules_sha256",
        "cases",
        "runs_per_case",
        "allowed_context_count",
        "answer_generation",
        "holdout_queries",
    )
    require(
        first["cases"] == 40
        and first["runs_per_case"] == 1
        and first["allowed_context_count"] == 0
        and first["answer_generation"] == "NOT_RUN"
        and first["holdout_queries"] == 0
        and all(
            all(result[key] == first[key] for key in identity) for result in results
        ),
        "load_snapshot_changed",
    )
    expected = {record["case_id"] for record in first["records"]}
    require(len(expected) == 40, "load_unique_cases_required")
    records = []
    for run, result in enumerate(results, 1):
        rows = result["records"]
        require(
            len(rows) == 40
            and {row["case_id"] for row in rows} == expected
            and all(row["run"] == 1 and row["split"] == "tuning" for row in rows),
            "load_missing_or_duplicate_case",
        )
        records.extend(compact_record(row, run) for row in rows)
    durations = [row["usecase_http_wall_ms"] for row in records]
    return {
        **{key: value for key, value in first.items() if key != "records"},
        "runs_per_case": 3,
        "total_requests": len(records),
        "source_mapping": "outer_report_mapping_verified_for_each_run_by_collect",
        "concurrent_client_lanes": 3,
        "server_embedding_concurrency": "NOT_INFERRED_FROM_CLIENT_LANES",
        "latency_measurement": (
            "three_concurrent_client_lanes_warm_usecase_http_not_chat_or_answer"
        ),
        "usecase_p50_ms": percentile(durations, 0.5),
        "usecase_p95_ms": percentile(durations, 0.95),
        "records": records,
    }


async def load(incoming: dict) -> dict:
    started = time.monotonic()
    # gather 等全部 lane 結束，即使其中失敗也不提早留下尚未關閉的 adapter。
    results = await asyncio.gather(
        *(collect(incoming) for _ in range(3)), return_exceptions=True
    )
    require(
        not any(isinstance(result, BaseException) for result in results),
        "load_lane_failed_no_retry",
    )
    result = merge_runs(results)
    result["load_wall_seconds"] = round(time.monotonic() - started, 3)
    # Linux 的 ru_maxrss 單位為 KiB；只量本 client 程序，不當成 server RAM。
    result["client_process_peak_rss_bytes"] = (
        resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    )
    return result


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    try:
        raw = sys.stdin.buffer.read(2_000_001)
        require(len(raw) <= 2_000_000, "load_input_bound")
        print(json.dumps(asyncio.run(load(json.loads(raw))), ensure_ascii=False))
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
