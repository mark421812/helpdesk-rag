"""Installed Ingestion parser/chunker 產生 v2 payload；只經 attach pipe 傳遞。

不寫 payload 檔、不下載、不啟動 Ingestion worker，不核准人工 Gold。
來源列的文字觀察另列，不能冒充 typed lineage 或完整性判定。
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID, uuid5

from audit_parsers import MODEL_ID, MODEL_REVISION
from audit_revision_v2 import row_cells_present
from build_revision_v2 import REVISION, VERSION
from index_fixtures import encode
from runner import read_json
from validate_revision_v2 import validate_at

SOURCE_ID = UUID("ebf5de76-ff8d-4d28-b7b4-8b3d9d78aaf8")


def observe_chunks(
    doc: dict, chunks: list[dict], rows: list[dict], cases: list
) -> dict:
    required = {
        anchor
        for case in cases
        for evidence in case["gold"]["evidence"]
        if evidence["document"] == doc["id"]
        for anchor in evidence["anchors"]
    }
    required.update(row["anchor"] for row in rows)
    anchors = {
        anchor: [i for i, c in enumerate(chunks) if anchor in c["content"]]
        for anchor in sorted(required)
    }
    return {
        "fixture": doc["id"],
        "source_sha256": doc["sha256"],
        "anchors": anchors,
        "missing_anchors": [a for a, indexes in anchors.items() if not indexes],
        "source_rows": len(rows),
        "rows_with_cells_in_one_chunk": sum(
            any(row_cells_present(c["content"], row) for c in chunks) for row in rows
        ),
        "wrong_pdf_row_pages": [
            row["anchor"]
            for row in rows
            if doc["path"].endswith(".pdf")
            and {chunks[i]["page_number"] for i in anchors[row["anchor"]]}
            != {row["page"]}
        ],
    }


def build() -> dict:
    validate_at(REVISION)
    from ingestion_service.application.use_cases import _content_hash, _submission
    from ingestion_service.domain.entities import DiscoveredFile, Source, SourceItem
    from ingestion_service.infrastructure.chunking.structure_aware import (
        ChunkingConfig,
        StructureAwareChunker,
    )
    from ingestion_service.infrastructure.chunking.token_counter import (
        HuggingFaceTokenCounter,
    )
    from ingestion_service.infrastructure.metadata.yaml_resolver import (
        YamlMetadataResolver,
    )
    from ingestion_service.infrastructure.parsers.registry import (
        create_default_parser_registry,
    )

    source = Source(SOURCE_ID, "u0-v2-synthetic-only", str(REVISION / "corpus"))
    counter = HuggingFaceTokenCounter(MODEL_ID, MODEL_REVISION, local_files_only=True)
    if counter.model_max_length != 512:
        raise ValueError("Unexpected tokenizer limit")
    config = ChunkingConfig()
    chunker = StructureAwareChunker(counter, config)
    registry, resolver = create_default_parser_registry(), YamlMetadataResolver()
    documents = read_json(REVISION / "documents.json")
    lineage = {g["group"]: g["rows"] for g in read_json(REVISION / "lineage.json")}
    cases = [
        case
        for split in ("tuning", "holdout")
        for case in read_json(REVISION / "datasets" / f"{split}.json")
    ]
    fixtures, observations = [], []
    for doc in documents:
        unresolved = REVISION / doc["path"]
        path = unresolved.resolve()
        if unresolved.is_symlink() or not path.is_relative_to(REVISION / "corpus"):
            raise ValueError("Synthetic source escaped corpus")
        if hashlib.sha256(path.read_bytes()).hexdigest() != doc["sha256"]:
            raise ValueError("Synthetic source hash mismatch")
        stat = path.stat()
        relative = path.relative_to(REVISION / "corpus").as_posix()
        file = DiscoveredFile(
            relative,
            relative,
            path,
            stat.st_size,
            datetime.fromtimestamp(stat.st_mtime, UTC),
        )
        parsed = registry.parse(path)
        chunks = tuple(chunker.chunk(parsed))
        metadata = resolver.load(source, file, parsed)
        if (
            metadata.external_llm_allowed is not False
            or metadata.custom_metadata.get("synthetic") is not True
            or metadata.custom_metadata.get("dataset_version") != VERSION
            or metadata.title != doc["title"]
            or not chunks
            or any(counter.count(c.content) != c.token_count for c in chunks)
            or any(not 0 < c.token_count <= config.max_tokens for c in chunks)
        ):
            raise ValueError("Synthetic metadata/token contract mismatch")
        item = SourceItem(
            id=uuid5(SOURCE_ID, doc["id"]),
            source_id=SOURCE_ID,
            source_item_id=relative,
            source_path=relative,
            absolute_path=str(path),
            source_version=file.source_version,
            metadata_source_hash=resolver.source_fingerprint(source, file),
            mime_type=parsed.mime_type,
            file_size=file.file_size,
            modified_at=file.modified_at,
        )
        submission = _submission(
            item,
            metadata,
            parsed,
            chunks,
            _content_hash(parsed.parser_name, parsed.parser_version, chunks),
        )
        fixtures.append({"fixture": doc["id"], "body": dataclasses.asdict(submission)})
        rows = lineage[doc["group"]] if path.suffix in {".pdf", ".docx"} else []
        observations.append(
            observe_chunks(doc, [dataclasses.asdict(c) for c in chunks], rows, cases)
        )
    return {
        "revision": VERSION,
        "dataset_manifest_sha256": hashlib.sha256(
            (REVISION / "manifest.json").read_bytes()
        ).hexdigest(),
        "fixtures": fixtures,
        "chunk_observations": observations,
        "chunking_config": dataclasses.asdict(config),
        "config_source": "class_defaults_not_live_settings",
    }


if __name__ == "__main__":
    try:
        print(json.dumps(build(), ensure_ascii=False, default=encode))
    except Exception as exc:
        print(json.dumps({"status": "BLOCKED", "error_type": type(exc).__name__}))
        raise SystemExit(2) from None
