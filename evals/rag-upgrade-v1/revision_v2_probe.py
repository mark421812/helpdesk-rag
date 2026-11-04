"""v2 bundle guard + 既有真實 PG/KIS/Qdrant probe；不修改舊證據工具。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys

import httpx

from build_revision_v2 import REVISION, VERSION
from kis_persistence_probe import ProbeFailure, require, run
from runner import read_json
from tuning_candidates_v2 import collect
from validate_revision_v2 import validate_at


def revision_guards(bundle: dict) -> None:
    validate_at(REVISION)
    require(
        bundle.get("revision") == VERSION
        and bundle.get("dataset_manifest_sha256")
        == hashlib.sha256((REVISION / "manifest.json").read_bytes()).hexdigest(),
        "v2_revision_binding",
    )
    documents = {d["id"]: d for d in read_json(REVISION / "documents.json")}
    fixtures = bundle["fixtures"]
    require(
        len(fixtures) == len(documents) == 40
        and {f["fixture"] for f in fixtures} == set(documents),
        "v2_fixture_identity",
    )
    for fixture in fixtures:
        body = fixture["body"]
        require(
            body["custom_metadata"].get("dataset_version") == VERSION
            and body["title"] == documents[fixture["fixture"]]["title"]
            and body["version_number"] == "v2",
            "v2_metadata_binding",
        )
    observations = bundle["chunk_observations"]
    require(
        len(observations) == 40
        and {o["fixture"] for o in observations} == set(documents),
        "v2_observation_identity",
    )
    for item in observations:
        require(
            item["source_sha256"] == documents[item["fixture"]]["sha256"],
            "v2_source_hash_binding",
        )


def bind_anchors(bundle: dict, mapping: list[dict]) -> list[dict]:
    by_index = {(m["fixture"], m["chunk_index"]): m for m in mapping}
    expected = {
        (f["fixture"], i)
        for f in bundle["fixtures"]
        for i in range(len(f["body"]["chunks"]))
    }
    require(
        len(by_index) == len(mapping) and set(by_index) == expected,
        "v2_mapping_incomplete",
    )
    bodies = {f["fixture"]: f["body"] for f in bundle["fixtures"]}
    result = []
    for observation in bundle["chunk_observations"]:
        fixture = observation["fixture"]
        for anchor, indexes in observation["anchors"].items():
            actual = [
                i
                for i, c in enumerate(bodies[fixture]["chunks"])
                if anchor in c["content"]
            ]
            require(indexes == actual, "v2_anchor_binding_mismatch")
            result.append(
                {
                    "fixture": fixture,
                    "anchor": anchor,
                    "chunk_ids": [by_index[(fixture, i)]["chunk_id"] for i in indexes],
                }
            )
    return result


async def execute(mode: str, bundle: dict) -> dict:
    revision_guards(bundle)
    result = await run(mode, bundle)
    if mode == "verify":
        result["revision"] = VERSION
        result["dataset_manifest_sha256"] = bundle["dataset_manifest_sha256"]
        # run('verify') 已逐字核對 DB canonical chunk；此處不另猜 canonical 內容。
        result["anchor_mapping"] = bind_anchors(bundle, result["mapping"])
        result["chunk_observations"] = bundle["chunk_observations"]
        result["max_chunk_tokens"] = max(
            c["token_count"] for f in bundle["fixtures"] for c in f["body"]["chunks"]
        )
        async with httpx.AsyncClient(
            base_url="http://kis-api:8080",
            trust_env=False,
            follow_redirects=False,
            timeout=60,
            headers={"x-internal-api-key": os.environ["API_INTERNAL_KEY"]},
        ) as client:
            result["tuning_candidates"] = await collect(client, bundle, result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("submit", "wait", "verify"))
    args = parser.parse_args()
    try:
        incoming = sys.stdin.buffer.read(2_000_001)
        require(len(incoming) <= 2_000_000, "input_too_large")
        print(
            json.dumps(
                asyncio.run(execute(args.mode, json.loads(incoming))),
                ensure_ascii=False,
            )
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "error_type": type(exc).__name__,
                    "check": exc.code if isinstance(exc, ProbeFailure) else None,
                }
            )
        )
        raise SystemExit(2) from None
