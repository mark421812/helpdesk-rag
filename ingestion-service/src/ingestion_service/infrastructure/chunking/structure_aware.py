"""保留 section/page 語意的 token-aware chunker。"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import groupby

from ingestion_service.domain.entities import Chunk, ParsedBlock, ParsedDocument
from ingestion_service.domain.exceptions import ValidationError

from .token_counter import TokenCounter

_SENTENCE_ENDINGS = frozenset("\u3002\uff01\uff1f!?\uff1b;.")
_PREFERRED_CUT_CHARACTERS = frozenset(" \t\n\u3002\uff01\uff1f!?\uff1b;\uff0c,.:\uff1a")


@dataclass(frozen=True, slots=True)
class ChunkingConfig:
    """Chunk 大小政策；在 ``__post_init__`` 保證不超過 embedding 模型上限。"""

    target_tokens: int = 350
    max_tokens: int = 450
    overlap_tokens: int = 50
    model_max_tokens: int = 512

    def __post_init__(self) -> None:
        if self.target_tokens <= 0:
            raise ValueError("target_tokens must be positive")
        if self.max_tokens < self.target_tokens:
            raise ValueError("max_tokens must be greater than or equal to target_tokens")
        if self.max_tokens > 512:
            raise ValueError("max_tokens must never exceed 512")
        if self.model_max_tokens <= 0 or self.model_max_tokens > 512:
            raise ValueError("model_max_tokens must be between 1 and 512")
        if self.max_tokens > self.model_max_tokens:
            raise ValueError("max_tokens must not exceed model_max_tokens")
        if self.overlap_tokens < 0:
            raise ValueError("overlap_tokens must not be negative")
        if self.overlap_tokens >= self.target_tokens:
            raise ValueError("overlap_tokens must be smaller than target_tokens")


class StructureAwareChunker:
    """可重現、依 token 計算且保留章節／頁碼引用邊界的 chunker。

    演算法先按 ``(section_path, page_number)`` 分組，再拆過長 block、包裝小 pieces、加入
    overlap，最後把章節標題放進 chunk 文字，提升向量檢索時的上下文品質。
    """

    def __init__(
        self,
        token_counter: TokenCounter,
        config: ChunkingConfig | None = None,
    ) -> None:
        self._token_counter = token_counter
        self._config = config or ChunkingConfig()

    def chunk(self, document: ParsedDocument) -> tuple[Chunk, ...]:
        """把 ParsedDocument 轉成不可變的 Chunk tuple。"""

        chunks: list[Chunk] = []

        def key_function(block: ParsedBlock) -> tuple[str | None, int | None]:
            return block.section_path, block.page_number

        # groupby 只合併「相鄰且 key 相同」的元素；parser 已保留原文件順序。
        for (section_path, page_number), grouped_blocks in groupby(
            document.blocks, key=key_function
        ):
            pieces: list[str] = []
            for block in grouped_blocks:
                pieces.extend(self._expand_block(block))
            for packed_content in self._pack_pieces(pieces):
                for content in self._contextualized(section_path, packed_content):
                    token_count = self._count(content)
                    if token_count > self._config.max_tokens or token_count > 512:
                        raise RuntimeError("Chunker produced an over-limit chunk")
                    chunks.append(
                        Chunk(
                            content=content,
                            section_path=section_path,
                            page_number=page_number,
                            token_count=token_count,
                        )
                    )
        return tuple(chunks)

    def _expand_block(self, block: ParsedBlock) -> list[str]:
        """保留小 block；過長 block 先按語句，再以 token 預算強制切分。"""

        content = block.content.strip()
        if not content:
            return []
        if self._count(content) <= self._config.max_tokens:
            return [content]

        separator = "\n" if "\n" in content else " "
        units = self._semantic_units(content)
        bounded_units: list[str] = []
        for unit in units:
            if self._count(unit) <= self._config.target_tokens:
                bounded_units.append(unit)
            else:
                bounded_units.extend(self._force_split(unit, self._config.target_tokens))

        pieces: list[str] = []
        current = ""
        for unit in bounded_units:
            candidate = unit if not current else current + separator + unit
            if self._count(candidate) <= self._config.target_tokens:
                current = candidate
                continue
            if current:
                pieces.append(current)
            current = unit
        if current:
            pieces.append(current)
        return pieces

    def _pack_pieces(self, pieces: list[str]) -> list[str]:
        """把小 pieces 裝到接近 target_tokens，跨 chunk 時加入前文 overlap。"""

        if not pieces:
            return []

        packed: list[str] = []
        current = pieces[0]
        for piece in pieces[1:]:
            candidate = self._join(current, piece)
            candidate_tokens = self._count(candidate)
            current_tokens = self._count(current)
            can_fill_small_chunk = (
                current_tokens < self._config.target_tokens // 2
                and candidate_tokens <= self._config.max_tokens
            )
            if candidate_tokens <= self._config.target_tokens or can_fill_small_chunk:
                current = candidate
                continue

            packed.append(current)
            current = self._with_overlap(current, piece)

        packed.append(current)
        return packed

    def _with_overlap(self, previous: str, next_piece: str) -> str:
        if self._config.overlap_tokens == 0:
            return next_piece
        overlap = self._tail_within(previous, self._config.overlap_tokens)
        if not overlap:
            return next_piece

        candidate = self._join(overlap, next_piece)
        if self._count(candidate) <= self._config.max_tokens:
            return candidate

        # Tokenization at a concatenation boundary is not perfectly additive. Find
        # the largest safe suffix using the final combined string as the authority.
        # Binary search 尋找最大安全 suffix，比逐字測試更有效率。
        low = 0
        high = len(overlap)
        best = ""
        while low <= high:
            midpoint = (low + high) // 2
            suffix = overlap[len(overlap) - midpoint :].lstrip()
            if self._count(self._join(suffix, next_piece)) <= self._config.max_tokens:
                best = suffix
                low = midpoint + 1
            else:
                high = midpoint - 1
        return self._join(best, next_piece) if best else next_piece

    def _contextualized(self, section_path: str | None, content: str) -> list[str]:
        """在每個 chunk 前加入章節路徑；必要時再次分割以遵守 max_tokens。"""

        context = section_path.strip() if section_path else ""
        if not context or self._already_contextualized(context, content):
            return [content]
        rendered = self._join(context, content)
        if self._count(rendered) <= self._config.max_tokens:
            return [rendered]
        if self._count(context) >= self._config.max_tokens:
            raise ValidationError("Document section path leaves no token budget for chunk content")

        remaining = content.strip()
        previous_piece = ""
        rendered_chunks: list[str] = []
        while remaining:
            end = self._largest_contextualized_prefix_end(context, remaining)
            if end <= 0:
                raise ValidationError(
                    "Document section path leaves no token budget for chunk content"
                )
            preferred_end = self._preferred_cut(remaining, end)
            piece = remaining[:preferred_end].rstrip()
            if not piece or self._count(self._join(context, piece)) > self._config.max_tokens:
                preferred_end = end
                piece = remaining[:preferred_end].rstrip()
            if not piece:
                raise RuntimeError("Unable to make progress while adding section context")

            contextual_body = piece
            if previous_piece and self._config.overlap_tokens:
                overlap = self._tail_within(
                    previous_piece,
                    self._config.overlap_tokens,
                )
                candidate_body = self._join(overlap, piece)
                if self._count(self._join(context, candidate_body)) <= self._config.max_tokens:
                    contextual_body = candidate_body
            rendered_chunks.append(self._join(context, contextual_body))
            previous_piece = piece
            remaining = remaining[preferred_end:].lstrip()
        return rendered_chunks

    def _largest_contextualized_prefix_end(self, context: str, text: str) -> int:
        low = 1
        high = len(text)
        best = 0
        while low <= high:
            midpoint = (low + high) // 2
            candidate = text[:midpoint].rstrip()
            if candidate and self._count(self._join(context, candidate)) <= self._config.max_tokens:
                best = midpoint
                low = midpoint + 1
            else:
                high = midpoint - 1
        return best

    @staticmethod
    def _already_contextualized(context: str, content: str) -> bool:
        normalized = content.strip()
        return normalized == context or normalized.startswith(context + "\n")

    def _force_split(self, text: str, token_limit: int) -> list[str]:
        remaining = text.strip()
        pieces: list[str] = []
        while remaining and self._count(remaining) > token_limit:
            end = self._largest_prefix_end(remaining, token_limit)
            if end <= 0:
                raise ValueError("Token budget is too small for any document content")
            preferred_end = self._preferred_cut(remaining, end)
            candidate = remaining[:preferred_end].rstrip()
            if not candidate or self._count(candidate) > token_limit:
                preferred_end = end
                candidate = remaining[:preferred_end].rstrip()
            if not candidate:
                raise RuntimeError("Unable to make progress while splitting document content")
            pieces.append(candidate)
            remaining = remaining[preferred_end:].lstrip()
        if remaining:
            pieces.append(remaining)
        return pieces

    def _largest_prefix_end(self, text: str, token_limit: int) -> int:
        low = 1
        high = len(text)
        best = 0
        while low <= high:
            midpoint = (low + high) // 2
            candidate = text[:midpoint].rstrip()
            if candidate and self._count(candidate) <= token_limit:
                best = midpoint
                low = midpoint + 1
            else:
                high = midpoint - 1
        return best

    @staticmethod
    def _preferred_cut(text: str, maximum_end: int) -> int:
        minimum_end = max(1, int(maximum_end * 0.65))
        for index in range(maximum_end, minimum_end - 1, -1):
            if text[index - 1] in _PREFERRED_CUT_CHARACTERS:
                return index
        return maximum_end

    def _tail_within(self, text: str, token_limit: int) -> str:
        if token_limit <= 0 or not text:
            return ""
        low = 0
        high = len(text)
        best = ""
        while low <= high:
            length = (low + high) // 2
            suffix = text[len(text) - length :].lstrip() if length else ""
            if self._count(suffix) <= token_limit:
                best = suffix
                low = length + 1
            else:
                high = length - 1
        if not best:
            return ""

        # Avoid beginning the overlap in the middle of a word when a nearby natural
        # boundary is available. Moving forward can only reduce the token count.
        search_limit = max(1, len(best) // 4)
        for index, character in enumerate(best[:search_limit]):
            if character in _PREFERRED_CUT_CHARACTERS:
                natural = best[index + 1 :].lstrip()
                if natural:
                    return natural
        return best

    @staticmethod
    def _semantic_units(text: str) -> list[str]:
        units: list[str] = []
        buffer: list[str] = []

        def flush() -> None:
            content = "".join(buffer).strip()
            if content:
                units.append(content)
            buffer.clear()

        for index, character in enumerate(text):
            buffer.append(character)
            if character == "\n":
                flush()
                continue
            if character not in _SENTENCE_ENDINGS:
                continue
            next_character = text[index + 1] if index + 1 < len(text) else ""
            if character != "." or not next_character or next_character.isspace():
                flush()
        flush()
        return units or [text.strip()]

    @staticmethod
    def _join(left: str, right: str) -> str:
        if not left:
            return right.strip()
        if not right:
            return left.strip()
        return f"{left.rstrip()}\n\n{right.lstrip()}"

    def _count(self, text: str) -> int:
        count = self._token_counter.count(text)
        if count < 0:
            raise ValueError("Token counter returned a negative value")
        return count
