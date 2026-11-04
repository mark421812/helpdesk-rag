"""Tokenizer 計數介面與 Hugging Face 實作，確保 chunk 不超過 embedding model 上限。"""

from __future__ import annotations

from typing import Any, Protocol


class TokenCounter(Protocol):
    """Chunker 所需的最小 tokenizer port。"""

    def count(self, text: str) -> int:
        """回傳送進 embedding model 時的精確 token 數。"""
        ...


class HuggingFaceTokenCounter:
    """用固定 revision 的 Hugging Face tokenizer 計數，不載入模型權重。

    預設 ``passage:`` prefix 與 special-token 行為必須和 Knowledge Index Service 的 E5
    embedding adapter 相同，否則 ingestion 計算的上限與實際 embedding token 數會不一致。
    """

    def __init__(
        self,
        model_id: str,
        revision: str,
        *,
        text_prefix: str = "passage: ",
        local_files_only: bool = False,
    ) -> None:
        if not model_id.strip():
            raise ValueError("model_id must not be empty")
        if not revision.strip():
            raise ValueError("revision must be pinned and must not be empty")
        try:
            from transformers import AutoTokenizer
        except ImportError as exc:  # pragma: no cover - declared runtime dependency
            raise RuntimeError("Hugging Face transformers is unavailable") from exc

        self.model_id = model_id
        self.revision = revision
        self.text_prefix = text_prefix
        self._tokenizer: Any = AutoTokenizer.from_pretrained(
            model_id,
            revision=revision,
            local_files_only=local_files_only,
            trust_remote_code=False,
        )

    @property
    def model_max_length(self) -> int:
        return int(self._tokenizer.model_max_length)

    def count(self, text: str) -> int:
        token_ids = self._tokenizer.encode(
            self.text_prefix + text.strip(),
            add_special_tokens=True,
            truncation=False,
        )
        return len(token_ids)
