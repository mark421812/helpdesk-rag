"""驗證 v2 草案的 hash、split、Gold 與來源 lineage；不核准人工品質。"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

from build_revision_v2 import REVISION, VERSION, build_cases, physical_rows, scenarios
from runner import TYPES, read_json


def validate_at(root: Path) -> dict:
    root = root.resolve()
    manifest = read_json(root / "manifest.json")
    module_root = Path(__file__).resolve().parent
    if (
        manifest["parent_manifest_sha256"]
        != hashlib.sha256((module_root / "manifest.json").read_bytes()).hexdigest()
    ):
        raise ValueError("Parent v1 changed")
    if (
        manifest["builder_sha256"]
        != hashlib.sha256(
            (module_root / "build_revision_v2.py").read_bytes()
        ).hexdigest()
    ):
        raise ValueError("Revision builder changed; require new draft evidence")
    if (
        manifest["version"] != VERSION
        or manifest["gold_status"] != "pending_human_review"
    ):
        raise ValueError("Wrong revision or unreviewed approval claim")
    actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
    if actual != {*manifest["files"], "manifest.json"}:
        raise ValueError("Missing or unmanifested revision file")
    for relative, digest in manifest["files"].items():
        path = root / relative
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError("Unsafe dataset path")
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("Revision hash mismatch")
    groups = read_json(root / "scenarios.json")
    if groups != scenarios():
        raise ValueError("Scenario definition drift")
    expected_cases, expected_lineage = [], []
    for group in groups:
        rows = physical_rows(group)
        expected_cases.extend(build_cases(group, rows))
        expected_lineage.append(
            {"group": group["id"], "completeness": group["completeness"], "rows": rows}
        )
    if read_json(root / "lineage.json") != expected_lineage:
        raise ValueError("Source row lineage mismatch")
    cases, domains, split_groups = [], set(), {}
    for split in ("tuning", "holdout"):
        current = read_json(root / "datasets" / f"{split}.json")
        if current != [c for c in expected_cases if c["split"] == split]:
            raise ValueError("Gold does not match draft source definition")
        if Counter(c["type"] for c in current) != Counter({t: 5 for t in TYPES}):
            raise ValueError("Five cases per category and split required")
        split_groups[split] = {c["source_group"] for c in current}
        domains.update(c["domain"] for c in current)
        cases.extend(current)
    if split_groups["tuning"] & split_groups["holdout"] or len(domains) < 2:
        raise ValueError("Split leakage or missing domains")
    if len({c["id"] for c in cases}) != 80:
        raise ValueError("Eighty unique business cases required")
    docs = read_json(root / "documents.json")
    if len(docs) != 40 or len({d["id"] for d in docs}) != 40:
        raise ValueError("Forty unique source documents required")
    if Counter(Path(d["path"]).suffix for d in docs) != Counter(
        {e: 10 for e in (".md", ".pdf", ".docx", ".txt")}
    ):
        raise ValueError("Four formats with ten files each required")
    doc_ids = {d["id"] for d in docs}
    for doc in docs:
        if doc["sha256"] != manifest["files"].get(doc["path"]):
            raise ValueError("Document hash differs from manifest")
        path = root / doc["path"]
        metadata = read_json(path.with_name(path.name + ".metadata.yaml"))
        if (
            metadata["external_llm_allowed"] is not False
            or metadata["custom_metadata"]["synthetic"] is not True
        ):
            raise ValueError("Draft must be synthetic and non-outbound")
        if metadata["acl"] != [
            {
                "principal_type": "group",
                "principal_id": "rag-u0-fixtures",
                "effect": "allow",
            }
        ]:
            raise ValueError("Synthetic ACL boundary changed")
    for case in cases:
        for evidence in case["gold"]["evidence"]:
            if evidence["document"] not in doc_ids or not evidence["anchors"]:
                raise ValueError("Missing source evidence")
    safety = read_json(root / "safety-cases.json")
    if len(safety) != 40 or len({c["id"] for c in safety}) != 40:
        raise ValueError("Forty distinct safety catalog cases required")
    return {
        "revision": VERSION,
        "business_cases": 80,
        "documents": 40,
        "count_answer_values": sorted(
            {
                int(c["gold"]["claims"][0])
                for c in cases
                if c["type"] == "count" and c["gold"]["expected"] == "answer"
            }
        ),
        "incomplete_list_count_cases": sum(
            c["type"] in {"list", "count"} and c["gold"]["expected"] == "refuse"
            for c in cases
        ),
        "max_pdf_pages_expected": max(d["expected_pages"] or 0 for d in docs),
        "gold_human_review_pending": 80,
        "safety_case_catalog": 40,
        "safety_executed": 0,
        "dataset_hashes_verified": True,
        "parser_kis_mapping": "NOT_CHECKED_BY_DATASET_VALIDATOR",
        "visual_qa": "NOT_CHECKED_BY_DATASET_VALIDATOR",
        "quality_gate": "BLOCKED",
    }


if __name__ == "__main__":
    print(json.dumps(validate_at(REVISION), ensure_ascii=False, indent=2))
