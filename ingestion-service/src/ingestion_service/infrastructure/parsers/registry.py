"""依副檔名選擇 parser 的 registry adapter。"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from ingestion_service.domain.entities import ParsedDocument

from .base import DocumentParser, UnsupportedDocumentTypeError
from .docx import DocxDocumentParser
from .markdown import MarkdownDocumentParser
from .pdf import PdfDocumentParser
from .text import TextDocumentParser


class ParserRegistry:
    """Registry pattern：集中維護副檔名／MIME type 到 parser 的 mapping。"""

    def __init__(self, parsers: Iterable[DocumentParser]) -> None:
        by_extension: dict[str, DocumentParser] = {}
        by_mime_type: dict[str, DocumentParser] = {}
        # 同一副檔名註冊兩次直接拒絕，避免執行時選擇結果不確定。
        for parser in parsers:
            for raw_extension in parser.extensions:
                extension = self._normalize_extension(raw_extension)
                if extension in by_extension:
                    raise ValueError(f"More than one parser is registered for {extension}")
                by_extension[extension] = parser
            for raw_mime_type in parser.mime_types:
                mime_type = self._normalize_mime_type(raw_mime_type)
                if mime_type in by_mime_type:
                    raise ValueError(f"More than one parser is registered for {mime_type}")
                by_mime_type[mime_type] = parser
        self._by_extension = by_extension
        self._by_mime_type = by_mime_type

    @property
    def supported_extensions(self) -> frozenset[str]:
        return frozenset(self._by_extension)

    def parser_for(self, path: Path) -> DocumentParser:
        extension = self._normalize_extension(path.suffix)
        try:
            return self._by_extension[extension]
        except KeyError as exc:
            raise UnsupportedDocumentTypeError(
                f"Unsupported document extension '{path.suffix or '<none>'}': {path}"
            ) from exc

    def get_parser(self, path: Path, mime_type: str | None = None) -> DocumentParser:
        """優先用副檔名選 parser，找不到時再 fallback 到 MIME type。"""

        extension = self._normalize_extension(path.suffix)
        parser = self._by_extension.get(extension)
        if parser is not None:
            return parser
        parser = self._by_mime_type.get(self._normalize_mime_type(mime_type or ""))
        if parser is not None:
            return parser
        raise UnsupportedDocumentTypeError(
            f"Unsupported document type extension='{path.suffix or '<none>'}', "
            f"mime_type='{mime_type or '<none>'}': {path}"
        )

    def parse(self, path: Path) -> ParsedDocument:
        return self.parser_for(path).parse(path)

    @staticmethod
    def _normalize_extension(extension: str) -> str:
        normalized = extension.strip().casefold()
        if normalized and not normalized.startswith("."):
            normalized = f".{normalized}"
        return normalized

    @staticmethod
    def _normalize_mime_type(mime_type: str) -> str:
        return mime_type.partition(";")[0].strip().casefold()


def create_default_parser_registry() -> ParserRegistry:
    """建立第一版支援 TXT、Markdown、DOCX、PDF 的 registry。"""

    return ParserRegistry(
        (
            TextDocumentParser(),
            MarkdownDocumentParser(),
            DocxDocumentParser(),
            PdfDocumentParser(),
        )
    )
