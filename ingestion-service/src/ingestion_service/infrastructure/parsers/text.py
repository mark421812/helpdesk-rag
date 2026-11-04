"""UTF-8 純文字 parser。"""

from __future__ import annotations

import codecs
from pathlib import Path

from ingestion_service.domain.entities import ParsedBlock, ParsedDocument

from .base import (
    DocumentParsingError,
    normalize_text,
    split_paragraphs,
    suggested_title_from_path,
    validate_input_file,
)


class TextDocumentParser:
    """讀取純文字、正規化編碼並依空白行切成 paragraphs。"""

    extensions = frozenset({".txt"})
    mime_types = frozenset({"text/plain"})
    parser_name = "plain-text"
    parser_version = "1"

    def parse(self, path: Path) -> ParsedDocument:
        validate_input_file(path)
        text = normalize_text(self._decode(path.read_bytes(), path))
        if not text:
            raise DocumentParsingError(f"Text document is empty: {path}")

        # generator expression 逐項產生 ParsedBlock，最後收斂為 immutable tuple。
        blocks = tuple(
            ParsedBlock(content=paragraph, block_type="paragraph")
            for paragraph in split_paragraphs(text)
        )
        return ParsedDocument(
            blocks=blocks,
            parser_name=self.parser_name,
            parser_version=self.parser_version,
            mime_type="text/plain",
            suggested_title=suggested_title_from_path(path),
        )

    @staticmethod
    def _decode(raw: bytes, path: Path) -> str:
        """先辨識 BOM，再嘗試 UTF-8，最後才用 charset-normalizer 推測編碼。"""

        if raw.startswith(codecs.BOM_UTF32_LE) or raw.startswith(codecs.BOM_UTF32_BE):
            return raw.decode("utf-32")
        if raw.startswith(codecs.BOM_UTF16_LE) or raw.startswith(codecs.BOM_UTF16_BE):
            return raw.decode("utf-16")
        try:
            return raw.decode("utf-8-sig")
        except UnicodeDecodeError as decode_error:
            try:
                from charset_normalizer import from_bytes
            except ImportError as exc:  # pragma: no cover - declared runtime dependency
                raise DocumentParsingError(
                    f"Document is not UTF-8 and charset detection is unavailable: {path}"
                ) from exc

            match = from_bytes(raw).best()
            if match is None:
                raise DocumentParsingError(
                    f"Unable to determine text encoding: {path}"
                ) from decode_error
            return str(match)
