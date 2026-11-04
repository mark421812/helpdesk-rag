"""以既有 Ingestion parser 讀取隔離的 synthetic corpus；不掃來源、不寫 DB。

輸出只有 fixture ID、計數、gold anchor 遺失與耗時，不輸出全文。
--chunks 使用原 production chunker 和 cached pinned tokenizer；禁止網路下載。
Parser-only 結果不能冒充 KIS/PostgreSQL/Qdrant 已保存的結果。
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import platform
import resource
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parents[1]
MODEL_ID = "intfloat/multilingual-e5-small"
MODEL_REVISION = "614241f622f53c4eeff9890bdc4f31cfecc418b3"


def audit(
    chunks: bool, *, write_payloads: bool = True, installed_only: bool = False
) -> dict:
    # 這是獨立評估工具的 read-only parser probe，不是 sibling runtime 的依賴。
    if not installed_only:
        sys.path.insert(0, str(WORKSPACE / "ingestion-service" / "src"))
    from ingestion_service.infrastructure.parsers.registry import (
        create_default_parser_registry,
    )

    registry = create_default_parser_registry()
    chunker = None
    chunk_config = None
    model_max_length = None
    if chunks:
        from ingestion_service.infrastructure.chunking.structure_aware import (
            ChunkingConfig,
            StructureAwareChunker,
        )
        from ingestion_service.infrastructure.chunking.token_counter import (
            HuggingFaceTokenCounter,
        )

        counter = HuggingFaceTokenCounter(
            MODEL_ID,
            MODEL_REVISION,
            local_files_only=True,
        )
        model_max_length = counter.model_max_length
        if model_max_length != 512:
            raise ValueError("Pinned tokenizer contract mismatch")
        chunk_config = ChunkingConfig()
        chunker = StructureAwareChunker(counter, chunk_config)
    docs = json.loads((ROOT / "documents.json").read_text(encoding="utf-8"))
    results = []
    for doc in docs:
        source = ROOT / doc["path"]
        if hashlib.sha256(source.read_bytes()).hexdigest() != doc["sha256"]:
            raise ValueError("Source hash mismatch")
        started = time.perf_counter()
        parsed = registry.parse(source)
        combined = "\n".join(b.content for b in parsed.blocks)
        # 預期來源標記由人工可讀 fixture 契約推導，不從 parser 輸出反推 gold。
        group, name = doc["id"].split("/", 1)
        anchors = (
            [f"R-{group}-{i}" for i in range(1, 4)]
            if name == "catalog.docx"
            else [f"E-{group}-STEP{i}" for i in range(1, 5)]
            if name == "procedure.pdf"
            else [f"E-{group}-DATE"]
            if name == "rules.md"
            else [f"E-{group}-EDITION", f"E-{group}-MISSING"]
        )
        missing = [a for a in anchors if a not in combined]
        item = {
            "fixture": doc["id"],
            "format": source.suffix,
            "parser": parsed.parser_name,
            "parser_version": parsed.parser_version,
            "blocks": len(parsed.blocks),
            "block_types": dict(Counter(b.block_type for b in parsed.blocks)),
            "pages": sorted({b.page_number for b in parsed.blocks if b.page_number}),
            "has_section": any(b.section_path for b in parsed.blocks),
            "missing_gold_anchors": missing,
            "characters": len(combined),
            "latency_ms": round((time.perf_counter() - started) * 1000, 3),
            "chunks": None,
        }
        if chunker:
            produced = chunker.chunk(parsed)
            item["chunks"] = {
                "count": len(produced),
                "max_tokens": max((c.token_count for c in produced), default=0),
                "over_limit_count": sum(
                    c.token_count > chunk_config.max_tokens
                    or c.token_count > model_max_length
                    for c in produced
                ),
                "recount_mismatch_count": sum(
                    counter.count(c.content) != c.token_count for c in produced
                ),
                "missing_gold_anchors": [
                    a for a in anchors if not any(a in c.content for c in produced)
                ],
            }
            if write_payloads:
                # 自製 payload 是評估 artifact，不是 log，也從未提交 KIS。
                destination = (
                    ROOT
                    / "runtime"
                    / "parser-payloads"
                    / (doc["id"].replace("/", "-") + ".json")
                )
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(
                    json.dumps(
                        [dataclasses.asdict(c) for c in produced],
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
        results.append(item)
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return {
        "stage": (
            "production_parser_chunking_only_not_kis_persistence"
            if chunks
            else "production_parser_only_not_kis_persistence"
        ),
        "documents": len(results),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "peak_process_rss_bytes": peak if sys.platform == "darwin" else peak * 1024,
        "missing_anchor_documents": sum(
            bool(r["missing_gold_anchors"]) for r in results
        ),
        "chunking_executed": chunks,
        "payloads_written": bool(chunks and write_payloads),
        "parser_import_mode": "installed" if installed_only else "workspace",
        "model_id": MODEL_ID if chunks else None,
        "model_revision": MODEL_REVISION if chunks else None,
        "model_max_length": model_max_length,
        "chunking_config": dataclasses.asdict(chunk_config) if chunk_config else None,
        "chunking_config_source": "class_defaults_not_live_settings",
        "results": results,
    }


def failures(result: dict) -> list[str]:
    """程序完成不等於驗收通過；標記遺失、空輸出與 token 錯誤必須 exit 1。"""
    problems = set()
    if result["documents"] != 40 or len(result["results"]) != 40:
        problems.add("document_count_mismatch")
    for item in result["results"]:
        if item["missing_gold_anchors"]:
            problems.add("parser_anchor_missing")
        if result["chunking_executed"]:
            produced = item["chunks"]
            if produced is None or produced["count"] <= 0:
                problems.add("empty_chunks")
                continue
            if produced["missing_gold_anchors"]:
                problems.add("chunk_anchor_missing")
            if (
                produced["over_limit_count"]
                or produced["max_tokens"] > result["chunking_config"]["max_tokens"]
                or produced["max_tokens"] > result["model_max_length"]
                or produced["max_tokens"] <= 0
            ):
                problems.add("chunk_token_limit")
            if produced["recount_mismatch_count"]:
                problems.add("token_recount_mismatch")
    return sorted(problems)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chunks", action="store_true")
    parser.add_argument("--no-payloads", action="store_true")
    parser.add_argument("--installed-parser-only", action="store_true")
    args = parser.parse_args()
    try:
        result = audit(
            args.chunks,
            write_payloads=not args.no_payloads,
            installed_only=args.installed_parser_only,
        )
        result["failures"] = failures(result)
        result["status"] = "FAILED" if result["failures"] else "PASSED"
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "error_type": type(exc).__name__,
                    "details": (
                        "Parser dependency or pinned local tokenizer unavailable; "
                        "no network attempted"
                    ),
                }
            )
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
