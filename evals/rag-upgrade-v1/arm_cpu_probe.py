"""無網路 ARM 容器的 installed adapter probe；不啟動服務或呼叫外部 API。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import importlib.metadata
import json
import logging
import math
import os
import platform
import resource
import time
from pathlib import Path

from audit_parsers import MODEL_ID, MODEL_REVISION

MODULES = {
    "ingestion": (
        "ingestion_service.domain.entities",
        "ingestion_service.application.use_cases",
        "ingestion_service.infrastructure.parsers.pdf",
        "ingestion_service.infrastructure.parsers.docx",
        "ingestion_service.infrastructure.parsers.registry",
        "ingestion_service.infrastructure.chunking.structure_aware",
        "ingestion_service.infrastructure.chunking.token_counter",
        "ingestion_service.infrastructure.metadata.yaml_resolver",
    ),
    "embedding": (
        "knowledge_index_service.infrastructure.embedding.sentence_transformer",
        "knowledge_index_service.infrastructure.config",
        "knowledge_index_service.domain.enums",
        "knowledge_index_service.domain.model_info",
    ),
}
PACKAGES = {
    "ingestion": ("pypdf", "python-docx", "transformers", "tokenizers"),
    "embedding": ("torch", "sentence-transformers", "transformers", "tokenizers"),
}
STABLE_FIELDS = (
    "fixture",
    "chunk_index",
    "page_number",
    "token_count",
    "content_sha256",
    "section_sha256",
)
STARTUP_STAGES = (
    "python_ready",
    "source_hashes_ready",
    "v2_payload_import_started",
    "v2_payload_import_ready",
    "tokenizer_import_started",
    "tokenizer_import_ready",
    "transformers_import_started",
    "transformers_import_ready",
    "auto_tokenizer_import_started",
    "auto_tokenizer_import_ready",
    "tokenizer_load_started",
    "tokenizer_load_ready",
    "token_count_ready",
)


def require(condition: bool, code: str) -> None:
    if not condition:
        raise ValueError(code)


def startup_diagnostic() -> None:
    """僅定位啟動耗時；只輸出固定 stage、code hash 與 synthetic token 計數。"""
    started = time.monotonic()

    def checkpoint(stage: str, **fields) -> None:
        require(stage in STARTUP_STAGES, "unknown_diagnostic_stage")
        print(
            json.dumps(
                {
                    "diagnostic_stage": stage,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    **fields,
                }
            ),
            flush=True,
        )

    checkpoint("python_ready")
    # 只先讀 installed source bytes，避免 hash 查驗本身匯入慢的第三方套件。
    package = importlib.import_module("ingestion_service")
    package_root = Path(package.__file__).parent
    hashes = {
        name: hashlib.sha256(
            (
                package_root / (name.split(".", 1)[1].replace(".", "/") + ".py")
            ).read_bytes()
        ).hexdigest()
        for name in MODULES["ingestion"]
    }
    checkpoint(
        "source_hashes_ready",
        installed_modules_sha256=hashes,
        machine=platform.machine(),
        python=platform.python_version(),
    )
    checkpoint("v2_payload_import_started")
    importlib.import_module("revision_v2_payload")
    checkpoint("v2_payload_import_ready")
    checkpoint("tokenizer_import_started")
    from ingestion_service.infrastructure.chunking.token_counter import (
        HuggingFaceTokenCounter,
    )

    checkpoint("tokenizer_import_ready")
    checkpoint("transformers_import_started")
    importlib.import_module("transformers")
    checkpoint("transformers_import_ready")
    checkpoint("auto_tokenizer_import_started")
    # 單獨定位 lazy import；仍由原 installed Counter 執行 pinned offline 載入。
    auto_tokenizer = importlib.import_module("transformers").AutoTokenizer
    require(
        callable(auto_tokenizer.from_pretrained), "auto_tokenizer_loader_unavailable"
    )
    checkpoint("auto_tokenizer_import_ready")
    checkpoint("tokenizer_load_started")
    counter = HuggingFaceTokenCounter(MODEL_ID, MODEL_REVISION, local_files_only=True)
    checkpoint("tokenizer_load_ready")
    count = counter.count("U0 synthetic ARM startup probe.")
    require(counter.model_max_length == 512 and count > 0, "tokenizer_probe_invalid")
    checkpoint(
        "token_count_ready",
        model_max_length=counter.model_max_length,
        synthetic_tokens=count,
    )


def ingestion() -> dict:
    from revision_v2_payload import build

    bundle = build()  # 真正 installed parser/chunker；全文只在此程序記憶體。
    records = []
    for fixture in bundle["fixtures"]:
        for index, chunk in enumerate(fixture["body"]["chunks"]):
            records.append(
                {
                    "fixture": fixture["fixture"],
                    "chunk_index": index,
                    "page_number": chunk["page_number"],
                    "token_count": chunk["token_count"],
                    "content_sha256": hashlib.sha256(
                        chunk["content"].encode()
                    ).hexdigest(),
                    "section_sha256": hashlib.sha256(
                        (chunk["section_path"] or "").encode()
                    ).hexdigest(),
                }
            )
    return {
        "documents": len(bundle["fixtures"]),
        "chunks": len(records),
        "records": records,
        "dataset_manifest_sha256": bundle["dataset_manifest_sha256"],
        "chunking_config": bundle["chunking_config"],
        "chunking_config_source": bundle["config_source"],
        "missing_anchor_count": sum(
            len(r["missing_anchors"]) for r in bundle["chunk_observations"]
        ),
    }


async def embedding() -> dict:
    import torch
    from knowledge_index_service.domain.enums import EmbeddingInputType
    from knowledge_index_service.domain.exceptions import CapacityExceededError
    from knowledge_index_service.infrastructure.config import Settings
    from knowledge_index_service.infrastructure.embedding.sentence_transformer import (
        SentenceTransformerEmbeddingModel,
    )

    require(
        torch.version.cuda is None and not torch.cuda.is_available(),
        "cpu_only_required",
    )
    settings = Settings(
        _env_file=None,
        model_id=MODEL_ID,
        model_revision=MODEL_REVISION,
        model_device="cpu",
        model_batch_size=2,
        torch_num_threads=1,
        embedding_max_concurrency=1,
        model_max_sequence_length=512,
        normalize_embeddings=True,
    )
    model = SentenceTransformerEmbeddingModel(settings)
    started = time.monotonic()
    info = await model.model_info()
    cold_seconds = time.monotonic() - started
    require(
        info.model_id == MODEL_ID
        and info.revision == MODEL_REVISION
        and info.dimension == 384
        and info.device == "cpu",
        "model_identity_changed",
    )
    started = time.monotonic()
    vectors = []
    for kind in (EmbeddingInputType.QUERY, EmbeddingInputType.DOCUMENT):
        batch = await model.embed(["U0 synthetic ARM CPU probe."], kind)
        require(
            len(batch) == 1
            and len(batch[0]) == 384
            and all(math.isfinite(value) for value in batch[0]),
            "vector_shape_or_finiteness",
        )
        vectors.extend(batch)
    warm_seconds = time.monotonic() - started
    norms = [math.sqrt(sum(value * value for value in vector)) for vector in vectors]
    require(all(abs(norm - 1) <= 0.0001 for norm in norms), "vector_normalization")
    over_limit_rejected = False
    try:
        await model.embed(["synthetic " * 600], EmbeddingInputType.QUERY)
    except CapacityExceededError:
        over_limit_rejected = True
    require(over_limit_rejected, "token_over_limit_not_rejected")
    return {
        "model_id": info.model_id,
        "model_revision": info.revision,
        "dimension": info.dimension,
        "device": info.device,
        "vector_count": len(vectors),
        "vectors_finite": True,
        "vector_norms": norms,
        "cold_model_load_seconds": round(cold_seconds, 3),
        "two_warm_encode_calls_seconds": round(warm_seconds, 3),
        "token_over_limit_rejected": over_limit_rejected,
        "cuda_version": torch.version.cuda,
        "vectors_sha256": hashlib.sha256(json.dumps(vectors).encode()).hexdigest(),
        "input_source": "fixed_tool_synthetic_literal_not_business_questions",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode", choices=("ingestion", "embedding", "diagnostic-startup")
    )
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    started = time.monotonic()
    try:
        require(
            platform.machine() == "aarch64"
            and platform.python_version().startswith("3.11."),
            "arm_python_311_required",
        )
        require(
            os.environ.get("HF_HUB_OFFLINE") == "1"
            and os.environ.get("TRANSFORMERS_OFFLINE") == "1",
            "offline_required",
        )
        if args.mode == "diagnostic-startup":
            startup_diagnostic()
            return 0  # 完成診斷，不輸出或宣稱 compatibility PASSED。
        result = ingestion() if args.mode == "ingestion" else asyncio.run(embedding())
        result.update(
            {
                "status": "PASSED",
                "mode": args.mode,
                "machine": platform.machine(),
                "python": platform.python_version(),
                "packages": {
                    name: importlib.metadata.version(name)
                    for name in PACKAGES[args.mode]
                },
                "installed_modules_sha256": {
                    name: hashlib.sha256(
                        Path(importlib.import_module(name).__file__).read_bytes()
                    ).hexdigest()
                    for name in MODULES[args.mode]
                },
                "process_peak_rss_bytes": resource.getrusage(
                    resource.RUSAGE_SELF
                ).ru_maxrss
                * 1024,
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }
        )
        print(json.dumps(result))
        return 0
    except Exception as exc:
        # 不打印 traceback、輸入、path、model下載URL或設定。
        print(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "error_type": type(exc).__name__,
                    "check": "arm_installed_adapter_probe_failed",
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
