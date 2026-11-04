"""無網路容器中的 U0 結構盤點；只輸出 synthetic anchor／hash／欄位名稱。

使用已安裝 production parser/chunker，不修復資料、不建立新的 parser。
欄位文字同列且有順序只證明 linear text 可用，不證明 typed row/cell 或完整性。
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import importlib
import json
import platform
import re
from collections import Counter
from pathlib import Path

from index_fixtures import build

ROOT = Path(__file__).resolve().parent
HEADERS = ("項目代碼", "名稱", "資格", "費用", "狀態")


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def compact(value: str) -> str:
    # 忽略 PDF 換行／cell 空白；這不是通用表格還原器。
    return "".join(value.split())


def ordered_cells(span: str, expected: list[str]) -> bool:
    """只能在同一個 row-anchor 的文字 span 檢查，不能借用其他列的 cell。"""
    remaining = compact(span)
    for value in expected:
        offset = remaining.find(compact(value))
        if offset < 0:
            return False
        remaining = remaining[offset + len(compact(value)) :]
    return True


def rows_in_text(text: str, group: str, expected: dict[str, list[str]]) -> list[dict]:
    markers = list(re.finditer(rf"R-{re.escape(group)}-\d+\b", text))
    found = []
    for index, marker in enumerate(markers):
        anchor = marker.group()
        end = markers[index + 1].start() if index + 1 < len(markers) else len(text)
        cells = expected.get(anchor)
        found.append(
            {
                "anchor": anchor,
                "known_row": cells is not None,
                "linear_text_cell_order": cells is not None
                and ordered_cells(text[marker.start() : end], cells),
            }
        )
    return found


def module_hashes(names: tuple[str, ...]) -> dict[str, str]:
    return {
        name: hashlib.sha256(
            Path(importlib.import_module(name).__file__).read_bytes()
        ).hexdigest()
        for name in names
    }


def ingestion() -> dict:
    from ingestion_service.domain.entities import Chunk, ParsedBlock
    from ingestion_service.infrastructure.parsers.registry import (
        create_default_parser_registry,
    )

    # build() 驗 source hash、synthetic metadata、external=false、token 上限。
    # 全文只在本程序記憶體使用；不將 body 輸出、寫檔或送 HTTP。
    bundle = build()
    registry = create_default_parser_registry()
    docs = {d["id"]: d for d in json.loads((ROOT / "documents.json").read_text())}
    scenarios = {s["id"]: s for s in json.loads((ROOT / "scenarios.json").read_text())}
    results = []
    for fixture in bundle["fixtures"]:
        identifier, body = fixture["fixture"], fixture["body"]
        group, name = identifier.split("/")
        scenario = scenarios[group]
        parsed = registry.parse(ROOT / docs[identifier]["path"])
        expected = {
            f"R-{group}-{index + 1}": [
                f"R-{group}-{index + 1}",
                label,
                scenario["qualifications"][index],
                f"{scenario['fees'][index]} 元",
                "開放" if index < 2 else "暫停",
            ]
            for index, label in enumerate(scenario["names"])
        }
        has_rows = name in {"catalog.docx", "procedure.pdf"}
        blocks = []
        chunks = []
        for index, block in enumerate(parsed.blocks):
            blocks.append(
                {
                    "index": index,
                    "kind": block.block_type,
                    "page": block.page_number,
                    "section_sha256": digest(block.section_path or ""),
                    "has_section": block.section_path is not None,
                    "header_occurrences": block.content.count(HEADERS[0]),
                    "header_cell_order": ordered_cells(block.content, list(HEADERS)),
                    "rows": rows_in_text(block.content, group, expected)
                    if has_rows
                    else [],
                }
            )
        for index, chunk in enumerate(body["chunks"]):
            content = chunk["content"]
            steps = [
                {"anchor": match.group(), "offset": match.start()}
                for match in re.finditer(rf"E-{group}-STEP[1-4]\b", content)
            ]
            chunks.append(
                {
                    "index": index,
                    "page": chunk["page_number"],
                    "tokens": chunk["token_count"],
                    "content_sha256": digest(content),
                    "section_sha256": digest(chunk["section_path"] or ""),
                    "has_section": chunk["section_path"] is not None,
                    "header_occurrences": content.count(HEADERS[0]),
                    "header_cell_order": ordered_cells(content, list(HEADERS)),
                    "rows": rows_in_text(content, group, expected) if has_rows else [],
                    "steps": steps,
                }
            )
        results.append(
            {
                "fixture": identifier,
                "format": Path(name).suffix,
                "parser": parsed.parser_name,
                "parser_version": parsed.parser_version,
                "block_types": dict(Counter(b.block_type for b in parsed.blocks)),
                "expected_rows": list(expected) if has_rows else [],
                "expected_steps": [f"E-{group}-STEP{i}" for i in range(1, 5)]
                if name == "procedure.pdf"
                else [],
                "metadata_keys": sorted(body["custom_metadata"]),
                "blocks": blocks,
                "chunks": chunks,
            }
        )
    return {
        "status": "PASSED",
        "mode": "ingestion",
        "python": platform.python_version(),
        "machine": platform.machine(),
        "parsed_block_fields": [f.name for f in dataclasses.fields(ParsedBlock)],
        "chunk_fields": [f.name for f in dataclasses.fields(Chunk)],
        "chunking_config": bundle["chunking_config"],
        "config_source": bundle["config_source"],
        "module_sha256": module_hashes(
            (
                "ingestion_service.domain.entities",
                "ingestion_service.infrastructure.parsers.pdf",
                "ingestion_service.infrastructure.parsers.docx",
                "ingestion_service.infrastructure.chunking.structure_aware",
                "ingestion_service.application.use_cases",
            )
        ),
        "documents": results,
    }


def kis() -> dict:
    from knowledge_index_service.infrastructure.persistence.models import (
        Base,
        ChunkModel,
    )
    from knowledge_index_service.presentation.schemas import (
        ChunkRequest,
        SearchResultItemResponse,
    )

    # 只 introspect 已安裝 class；不 create engine、connect DB 或 create_all。
    return {
        "status": "PASSED",
        "mode": "kis",
        "python": platform.python_version(),
        "machine": platform.machine(),
        "chunk_request_fields": sorted(ChunkRequest.model_fields),
        "search_result_fields": sorted(SearchResultItemResponse.model_fields),
        "chunk_columns": sorted(ChunkModel.__table__.columns.keys()),
        "tables": {
            name: sorted(table.columns.keys())
            for name, table in sorted(Base.metadata.tables.items())
        },
        "module_sha256": module_hashes(
            (
                "knowledge_index_service.infrastructure.persistence.models",
                "knowledge_index_service.presentation.schemas",
            )
        ),
        "database_connections": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("ingestion", "kis"))
    args = parser.parse_args()
    try:
        result = ingestion() if args.mode == "ingestion" else kis()
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "BLOCKED", "error_type": type(exc).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
