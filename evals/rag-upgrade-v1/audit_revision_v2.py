"""使用既有 production parser 唯讀稽核 v2；不切段、不寫 KIS、不外送。"""

from __future__ import annotations

import hashlib
import json
import platform
import re
import sys
from datetime import UTC, datetime

from build_revision_v2 import REVISION, ROOT, values
from runner import read_json
from validate_revision_v2 import validate_at

ROW = re.compile(r"R-[TH]0[1-5]-[0-9]{3}(?![0-9])")


def row_cells_present(text: str, row: dict) -> bool:
    """只驗每個來源列內的線性欄位順序，不冒充 typed table／cell。"""
    matches = list(ROW.finditer(text))
    for index, match in enumerate(matches):
        if match.group() != row["anchor"]:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = re.sub(r"\s+", "", text[match.end() : end])
        offset = 0
        for cell in values(row)[1:]:
            position = body.find(re.sub(r"\s+", "", cell), offset)
            if position < 0:
                return False
            offset = position + len(re.sub(r"\s+", "", cell))
        return True
    return False


def audit() -> dict:
    validate_at(REVISION)
    # 僅測試工具讀 source；服務之間沒有新增 sibling Python import。
    sys.path.insert(0, str(ROOT.parents[1] / "ingestion-service" / "src"))
    from ingestion_service.infrastructure.parsers.registry import (
        create_default_parser_registry,
    )

    registry = create_default_parser_registry()
    documents = read_json(REVISION / "documents.json")
    lineage = {
        item["group"]: item["rows"] for item in read_json(REVISION / "lineage.json")
    }
    cases = [
        case
        for split in ("tuning", "holdout")
        for case in read_json(REVISION / "datasets" / f"{split}.json")
    ]
    results, failures = [], []
    for doc in documents:
        parsed = registry.parse(REVISION / doc["path"])
        combined = "\n".join(block.content for block in parsed.blocks)
        required = {
            anchor
            for c in cases
            for e in c["gold"]["evidence"]
            if e["document"] == doc["id"]
            for anchor in e["anchors"]
        }
        # PDF 和 Word 都有同一目錄；不僅測 Gold 引用的前兩列。
        rows = lineage[doc["group"]] if doc["path"].endswith((".pdf", ".docx")) else []
        required.update(row["anchor"] for row in rows)
        missing = sorted(anchor for anchor in required if anchor not in combined)
        cells_missing = [
            r["anchor"] for r in rows if not row_cells_present(combined, r)
        ]
        wrong_pages = []
        for row in rows if doc["path"].endswith(".pdf") else []:
            observed = {
                block.page_number
                for block in parsed.blocks
                if row["anchor"] in block.content
            }
            if observed != {row["page"]}:
                wrong_pages.append(row["anchor"])
        pages = sorted({b.page_number for b in parsed.blocks if b.page_number})
        if doc["expected_pages"] is not None and pages != list(
            range(1, doc["expected_pages"] + 1)
        ):
            failures.append({"fixture": doc["id"], "reason": "page_count_mismatch"})
        if missing or cells_missing or wrong_pages:
            failures.append(
                {"fixture": doc["id"], "reason": "source_text_or_page_mismatch"}
            )
        results.append(
            {
                "fixture": doc["id"],
                "source_sha256": doc["sha256"],
                "parser": parsed.parser_name,
                "parser_version": parsed.parser_version,
                "blocks": len(parsed.blocks),
                "pages": pages,
                "required_anchors": len(required),
                "missing_anchors": missing,
                "rows_checked": len(rows),
                "row_cells_missing": cells_missing,
                "wrong_row_pages": wrong_pages,
            }
        )
    return {
        "stage": "v2_host_parser_only_not_chunking_or_persistence",
        "captured_at": datetime.now(UTC).isoformat(),
        "python": platform.python_version(),
        "machine": platform.machine(),
        "dataset_manifest_sha256": hashlib.sha256(
            (REVISION / "manifest.json").read_bytes()
        ).hexdigest(),
        "documents": len(results),
        "fixtures": results,
        "failures": failures,
        "status": "PASSED" if len(results) == 40 and not failures else "FAILED",
        "human_gold": "PENDING",
        "quality_gate": "BLOCKED",
        "database_calls": 0,
        "provider_calls": 0,
    }


if __name__ == "__main__":
    report = audit()
    destination = ROOT / "runtime" / "u0-v2-host-parser.json"
    destination.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {key: value for key, value in report.items() if key != "fixtures"}, indent=2
        )
    )
    raise SystemExit(0 if report["status"] == "PASSED" else 1)
