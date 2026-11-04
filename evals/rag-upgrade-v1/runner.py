"""U0 資料集驗證及離線觀察評分。完全不發 HTTP、不呼叫 LLM、不寫 DB。

指標只評分真實測試匯出的 synthetic observations。答案正確性由人工評分，
不能靠答案字串含一個數字、LLM 自評或 Fake 固定答案宣稱品質通過。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TYPES = {
    "fact",
    "list",
    "count",
    "compare",
    "procedure",
    "followup",
    "no_answer",
    "ambiguity",
}


def read_json(path: Path) -> object:
    if path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("Input exceeds 16 MiB")
    return json.loads(
        path.read_text(encoding="utf-8"), parse_constant=lambda _: invalid_json_number()
    )


def invalid_json_number() -> None:
    raise ValueError("Non-finite JSON number")


def cases_for(split: str) -> list[dict]:
    value = read_json(ROOT / "datasets" / f"{split}.json")
    if not isinstance(value, list):
        raise ValueError("Dataset root must be an array")
    return value


def validate() -> dict:
    manifest = read_json(ROOT / "manifest.json")
    for relative, expected in manifest["files"].items():
        path = (ROOT / relative).resolve()
        if not path.is_relative_to(ROOT) or not path.is_file():
            raise ValueError("Invalid manifest path")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("Frozen dataset hash mismatch")
    docs = read_json(ROOT / "documents.json")
    doc_ids = {d["id"] for d in docs}
    if {Path(d["path"]).suffix for d in docs} != {".pdf", ".docx", ".txt", ".md"}:
        raise ValueError("Four source formats are required")
    groups, questions, ids = {}, set(), set()
    pending = 0
    for split in ("tuning", "holdout"):
        cases = cases_for(split)
        if Counter(c["type"] for c in cases) != Counter({kind: 5 for kind in TYPES}):
            raise ValueError("Each split must contain five cases per type")
        if len({c["domain"] for c in cases}) < 2:
            raise ValueError("Two domains per split are required")
        for c in cases:
            if c["id"] in ids or c["split"] != split or not c["question"].strip():
                raise ValueError("Duplicate ID, invalid split, or blank question")
            ids.add(c["id"])
            # followup 字面相同是必要測試；其 history 與來源群組不同。
            identity = (c["question"], json.dumps(c["history"], ensure_ascii=False))
            if identity in questions:
                raise ValueError("Duplicate question plus history")
            questions.add(identity)
            if c["source_group"] in groups and groups[c["source_group"]] != split:
                raise ValueError("Source group leaks across tuning and holdout")
            groups[c["source_group"]] = split
            gold = c["gold"]
            if gold["human_review_status"] != "approved":
                pending += 1
            if not gold["evidence"] or gold["expected"] not in {
                "answer",
                "refuse",
                "clarify",
            }:
                raise ValueError("Gold evidence or outcome missing")
            for evidence in gold["evidence"]:
                if (
                    evidence["document"] not in doc_ids
                    or not evidence["anchors"]
                    or not evidence["version"]
                ):
                    raise ValueError("Gold reference invalid")
                if not evidence["document"].startswith(c["source_group"] + "/"):
                    raise ValueError("Gold references another group")
            if c["type"] in {"list", "count"} and not gold["complete_required"]:
                raise ValueError("List/count completeness missing")
            if c["type"] == "count" and not gold["count_unique_key"]:
                raise ValueError("Count unique key missing")
    safety = read_json(ROOT / "safety-cases.json")
    if len(safety) < 40 or len({c["id"] for c in safety}) != len(safety):
        raise ValueError("Forty distinct safety cases required")
    return {
        "business_cases": len(ids),
        "documents": len(docs),
        "source_groups": len(groups),
        "safety_case_catalog": len(safety),
        "safety_executed": 0,
        "gold_human_review_pending": pending,
        "dataset_hashes_verified": True,
        "quality_gate": "BLOCKED",
    }


def dcg(grades: list[float]) -> float:
    return sum((2**grade - 1) / math.log2(i + 2) for i, grade in enumerate(grades))


def evidence_metrics(case: dict, obs: dict) -> dict:
    """用真實 chunk 已映射的 source anchor 算召回；入口與完整列分開。"""
    required = {anchor for e in case["gold"]["evidence"] for anchor in e["anchors"]}
    candidate_anchors = set().union(
        *(set(c["anchors"]) for c in obs["candidates"][:30])
    )
    selected = set(obs["selected_anchors"])
    # 每個 unique anchor 只計一次 gain，避免重複 chunks 使 nDCG > 1。
    seen, grades = set(), []
    for candidate in obs["candidates"][:10]:
        relevant = (set(candidate["anchors"]) & required) - seen
        grades.append(1.0 if relevant else 0.0)
        seen.update(relevant)
    ideal = dcg([1.0] * min(len(required), 10))
    return {
        "anchor_recall_at30": len(required & candidate_anchors) / len(required),
        "anchor_ndcg_at10_proxy": dcg(grades) / ideal if ideal else 0.0,
        "selected_gold_coverage": required <= selected,
        "complete_claim_valid": not obs["claims_complete"]
        or (
            case["gold"]["complete_required"]
            and required <= selected
            and obs["source_complete"]
        ),
    }


def classify(obs: dict, metrics: dict) -> str | None:
    if obs["dependency_error"]:
        return "dependency_error"
    if not obs["parser_gold_present"]:
        return "parser_loss"
    if metrics["anchor_recall_at30"] < 1:
        return "recall_miss"
    if not metrics["selected_gold_coverage"]:
        return "ranking_or_packing"
    if obs["human_correct"] is False:
        return "generation_error"
    if obs["human_correct"] is None:
        return "manual_review_pending"
    return None


def validate_observation(obs: dict, cases: dict[str, dict], real: bool) -> None:
    allowed = {
        "case_id",
        "run",
        "candidates",
        "selected_anchors",
        "parser_gold_present",
        "source_complete",
        "claims_complete",
        "human_correct",
        "citation_correct",
        "dependency_error",
        "latency_ms",
        "human_reason",
        "provider",
        "model",
        "prompt_hash",
        "policy_hash",
    }
    if set(obs) != allowed or obs["case_id"] not in cases:
        raise ValueError(
            "Observation schema / case ID invalid; raw answer/evidence/token forbidden"
        )
    if type(obs["run"]) is not int or not 1 <= obs["run"] <= 3:
        raise ValueError("Run number must be 1..3")
    for name in (
        "parser_gold_present",
        "source_complete",
        "claims_complete",
        "dependency_error",
    ):
        if type(obs[name]) is not bool:
            raise ValueError("Observation requires strict booleans")
    for name in ("human_correct", "citation_correct"):
        if obs[name] is not None and type(obs[name]) is not bool:
            raise ValueError("Manual grade must be boolean or null")
    latency = obs["latency_ms"]
    if type(latency) not in (float, int) or not math.isfinite(latency) or latency < 0:
        raise ValueError("Invalid latency")
    if real and obs["provider"] not in {"groq", "nvidia"}:
        raise ValueError("Fake provider is not real generation evidence")
    for name in ("prompt_hash", "policy_hash"):
        if (
            not isinstance(obs[name], str)
            or len(obs[name]) != 64
            or any(c not in "0123456789abcdef" for c in obs[name])
        ):
            raise ValueError("Missing configuration fingerprint")
    if not isinstance(obs["model"], str) or not obs["model"]:
        raise ValueError("Missing model identity")
    anchors = {
        a for c in cases.values() for e in c["gold"]["evidence"] for a in e["anchors"]
    }
    if not isinstance(obs["candidates"], list) or len(obs["candidates"]) > 50:
        raise ValueError("Candidate bound exceeded")
    for candidate in obs["candidates"]:
        if set(candidate) != {"anchors"} or not isinstance(candidate["anchors"], list):
            raise ValueError("Raw candidate content forbidden")
        if not set(candidate["anchors"]) <= anchors:
            raise ValueError("Non-fixture anchor forbidden")
    if (
        not isinstance(obs["selected_anchors"], list)
        or not set(obs["selected_anchors"]) <= anchors
    ):
        raise ValueError("Non-fixture selected anchor")
    if obs["human_reason"] not in {
        None,
        "supported",
        "unsupported_claim",
        "missing_claim",
        "wrong_count",
        "wrong_citation",
        "wrong_refusal",
        "wrong_clarification",
    }:
        raise ValueError("Only content-free review reasons allowed")


def score(observations: list[dict], split: str, real: bool) -> dict:
    validate()
    cases = {c["id"]: c for c in cases_for(split)}
    seen, typed, classified, fingerprint = set(), defaultdict(list), Counter(), None
    for obs in observations:
        validate_observation(obs, cases, real)
        key = (obs["case_id"], obs["run"])
        if key in seen:
            raise ValueError("Duplicate run")
        seen.add(key)
        config = tuple(
            obs[k] for k in ("provider", "model", "prompt_hash", "policy_hash")
        )
        if fingerprint is not None and config != fingerprint:
            raise ValueError("Changed configuration must be a separate report")
        fingerprint = config
        metrics = evidence_metrics(cases[obs["case_id"]], obs)
        reason = classify(obs, metrics)
        classified[reason or "no_failure"] += 1
        typed[cases[obs["case_id"]]["type"]].append((obs, metrics))
    expected_runs = 3 if real else 1
    expected = {
        (case_id, run) for case_id in cases for run in range(1, expected_runs + 1)
    }
    if seen != expected:
        raise ValueError(
            "Every case and required run must be present; no cherry-picking"
        )
    result = {
        "split": split,
        "runs": len(seen),
        "real_generation": real,
        "dataset_manifest_sha256": hashlib.sha256(
            (ROOT / "manifest.json").read_bytes()
        ).hexdigest(),
        "failure_categories": dict(classified),
        "by_type": {},
        "quality_gate": "BLOCKED_PENDING_HUMAN_GOLD_AND_INDEPENDENT_REVIEW",
    }
    for kind, values in sorted(typed.items()):
        latencies = sorted(o["latency_ms"] for o, _ in values)
        result["by_type"][kind] = {
            "runs": len(values),
            "human_graded": sum(o["human_correct"] is not None for o, _ in values),
            "correct_runs": sum(
                o["human_correct"] is True and o["citation_correct"] is True
                for o, _ in values
            ),
            "anchor_recall_at30": statistics.mean(
                m["anchor_recall_at30"] for _, m in values
            ),
            "anchor_ndcg_at10_proxy": statistics.mean(
                m["anchor_ndcg_at10_proxy"] for _, m in values
            ),
            "selected_sufficient_rate": statistics.mean(
                m["selected_gold_coverage"] for _, m in values
            ),
            "invalid_complete_claims": sum(
                not m["complete_claim_valid"] for _, m in values
            ),
            "latency_p50_ms": statistics.median(latencies),
            "latency_p95_ms": latencies[math.ceil(len(latencies) * 0.95) - 1],
        }
    # Holdout 只輸出彙總；本函式不輸出問題、答案、逐題失敗或 attestation。
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate")
    p = sub.add_parser("score")
    p.add_argument("--split", choices=("tuning", "holdout"), required=True)
    p.add_argument("--observations", type=Path, required=True)
    p.add_argument("--real-generation", action="store_true")
    args = parser.parse_args()
    try:
        result = (
            validate()
            if args.command == "validate"
            else score(read_json(args.observations), args.split, args.real_generation)
        )
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "FAILED", "error_type": type(exc).__name__}))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
