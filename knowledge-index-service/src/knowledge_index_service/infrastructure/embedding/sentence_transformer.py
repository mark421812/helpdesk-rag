"""SentenceTransformer embedding adapter。

此 Infrastructure class 把第三方模型套件包在 Application 的 EmbeddingModelPort 後方，
負責 lazy loading、CPU concurrency、token 上限與 query/passage prefix。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any, cast

from knowledge_index_service.domain.enums import EmbeddingInputType
from knowledge_index_service.domain.exceptions import (
    CapacityExceededError,
    ServiceNotReadyError,
)
from knowledge_index_service.domain.model_info import ModelInfo
from knowledge_index_service.infrastructure.config import Settings


class SentenceTransformerEmbeddingModel:
    """延遲載入模型，並把 CPU-bound encode 移出 asyncio event loop。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._model: Any | None = None
        self._model_info: ModelInfo | None = None
        self._load_lock = asyncio.Lock()
        self._encode_semaphore = asyncio.Semaphore(settings.embedding_max_concurrency)

    async def model_info(self) -> ModelInfo:
        """確保模型已載入後回傳 pinned model metadata。"""

        await self._ensure_loaded()
        if self._model_info is None:
            raise RuntimeError("Embedding model metadata was not initialized")
        return self._model_info

    async def embed(
        self,
        texts: Sequence[str],
        input_type: EmbeddingInputType,
    ) -> list[list[float]]:
        """依 query/document 加正確 prefix，並限制同時 encode 的批次數。"""

        await self._ensure_loaded()
        if self._model is None:
            raise RuntimeError("Embedding model was not initialized")
        prefix = "query: " if input_type == EmbeddingInputType.QUERY else "passage: "
        prepared = [prefix + text.strip() for text in texts]
        # Semaphore 控制同時進入的 coroutine 數；to_thread 避免 CPU 工作阻塞 event loop。
        async with self._encode_semaphore:
            return await asyncio.to_thread(self._encode, prepared)

    async def is_ready(self) -> bool:
        try:
            await self._ensure_loaded()
        except Exception:
            return False
        return self._model is not None

    async def _ensure_loaded(self) -> None:
        """使用 double-checked async lock，確保多 request 同時到達也只載入一次模型。"""

        if self._model is not None:
            return
        async with self._load_lock:
            if self._model is not None:
                return
            try:
                self._model, self._model_info = await asyncio.to_thread(self._load_model)
            except Exception as exc:
                raise ServiceNotReadyError("Embedding model could not be loaded") from exc

    def _load_model(self) -> tuple[Any, ModelInfo]:
        """同步載入第三方模型；只會在 worker thread 中執行。"""

        import torch
        from sentence_transformers import SentenceTransformer

        torch.set_num_threads(self._settings.torch_num_threads)
        model = SentenceTransformer(
            self._settings.model_id,
            revision=self._settings.model_revision,
            device=self._settings.model_device,
        )
        model.max_seq_length = self._settings.model_max_sequence_length
        dimension = model.get_sentence_embedding_dimension()
        if dimension is None:
            raise RuntimeError("Embedding model did not report a vector dimension")
        return model, ModelInfo(
            model_id=self._settings.model_id,
            revision=self._settings.model_revision,
            dimension=dimension,
            normalized=self._settings.normalize_embeddings,
            max_sequence_length=self._settings.model_max_sequence_length,
            device=self._settings.model_device,
        )

    def _encode(self, prepared: list[str]) -> list[list[float]]:
        """先禁止 tokenizer 靜默截斷，再執行 batch encode。"""

        if self._model is None:
            raise RuntimeError("Embedding model was not initialized")
        tokenized = self._model.tokenizer(
            prepared,
            add_special_tokens=True,
            truncation=False,
        )
        over_limit = [
            index
            for index, token_ids in enumerate(tokenized["input_ids"])
            if len(token_ids) > self._settings.model_max_sequence_length
        ]
        if over_limit:
            preview = ", ".join(str(index) for index in over_limit[:10])
            raise CapacityExceededError(
                f"Embedding input exceeds the model token limit at item index(es): {preview}"
            )
        vectors = self._model.encode(
            prepared,
            batch_size=self._settings.model_batch_size,
            normalize_embeddings=self._settings.normalize_embeddings,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return cast(list[list[float]], vectors.tolist())
