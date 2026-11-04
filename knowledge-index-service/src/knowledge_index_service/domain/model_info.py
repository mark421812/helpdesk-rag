"""Embedding model 身分、revision、維度與 token 限制的 Domain value object。"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ModelInfo:
    """Embedding adapter 對外公開、可用於驗證 vector collection 的模型資訊。"""

    model_id: str
    revision: str
    dimension: int
    max_sequence_length: int
    normalized: bool
    device: str
