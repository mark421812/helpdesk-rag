"""隔離容器內的 synthetic payload 產生器；stdout 只供 controller 記憶體接收。

使用 image 已安裝的 production parser、chunker、metadata resolver 與 submission
builder。不啟動 Ingestion worker、不寫 ingestion schema、不下載模型、不呼叫 LLM。
不可直接把 stdout 當報告：它含自製 chunk 全文，controller 不會將它寫入 log。
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from datetime import UTC, date, datetime
from enum import Enum
from pathlib import Path
from uuid import UUID, uuid5

from audit_parsers import MODEL_ID, MODEL_REVISION

ROOT = Path(__file__).resolve().parent
SOURCE_ID = UUID("836ff7c0-466b-4e87-a206-e32934b868bc")


def encode(value: object) -> str:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    raise TypeError("Unsupported payload value")


def build() -> dict:
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

    source = Source(SOURCE_ID, "u0-synthetic-only", str(ROOT / "corpus"))
    counter = HuggingFaceTokenCounter(MODEL_ID, MODEL_REVISION, local_files_only=True)
    if counter.model_max_length != 512:
        raise ValueError("Unexpected model token limit")
    config = ChunkingConfig()
    chunker = StructureAwareChunker(counter, config)
    registry = create_default_parser_registry()
    resolver = YamlMetadataResolver()
    fixtures = []
    docs = json.loads((ROOT / "documents.json").read_text())
    for doc in docs:
        path = (ROOT / doc["path"]).resolve()
        if not path.is_relative_to(ROOT / "corpus") or path.is_symlink():
            raise ValueError("Synthetic path escaped corpus")
        if hashlib.sha256(path.read_bytes()).hexdigest() != doc["sha256"]:
            raise ValueError("Synthetic source hash mismatch")
        stat = path.stat()
        relative = str(path.relative_to(ROOT / "corpus"))
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
            or metadata.title != doc["title"]
            or not chunks
            or any(counter.count(c.content) != c.token_count for c in chunks)
            or any(not 0 < c.token_count <= config.max_tokens for c in chunks)
        ):
            raise ValueError("Synthetic metadata/chunk contract mismatch")
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
    return {
        "fixtures": fixtures,
        "chunking_config": dataclasses.asdict(config),
        "config_source": "class_defaults_not_live_settings",
    }


def main() -> int:
    try:
        print(json.dumps(build(), ensure_ascii=False, default=encode))
        return 0
    except Exception as exc:
        # 不輸出 exception message、全文或 traceback。
        print(json.dumps({"status": "BLOCKED", "error_type": type(exc).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
