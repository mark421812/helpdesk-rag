"""逐頁擷取文字並保留頁碼的 PDF parser；沒有文字時要求 OCR。"""

from __future__ import annotations

from pathlib import Path

from ingestion_service.domain.entities import ParsedBlock, ParsedDocument
from ingestion_service.domain.exceptions import ParserRequiresOcrError

from .base import (
    DocumentParsingError,
    normalize_text,
    split_paragraphs,
    suggested_title_from_path,
    validate_input_file,
)


class PdfDocumentParser:
    """用 pypdf 逐頁擷取文字；頁碼會保留到 ParsedBlock 供 citation 使用。"""

    extensions = frozenset({".pdf"})
    mime_types = frozenset({"application/pdf"})
    parser_name = "pypdf"
    parser_version = "1"

    def parse(self, path: Path) -> ParsedDocument:
        validate_input_file(path)
        try:
            from pypdf import PdfReader

            reader = PdfReader(path)
            if reader.is_encrypted:
                raise DocumentParsingError(f"Encrypted PDF is not supported: {path}")
        except DocumentParsingError:
            raise
        except ImportError as exc:  # pragma: no cover - declared runtime dependency
            raise DocumentParsingError("PDF parser dependency 'pypdf' is unavailable") from exc
        except Exception as exc:
            raise DocumentParsingError(f"Unable to open PDF document: {path}") from exc

        blocks: list[ParsedBlock] = []
        # enumerate(..., start=1) 同時取得人類使用的一基頁碼與 page object。
        for page_number, page in enumerate(reader.pages, start=1):
            try:
                extracted = normalize_text(page.extract_text() or "")
            except Exception as exc:
                raise DocumentParsingError(
                    f"Unable to extract text from PDF page {page_number}: {path}"
                ) from exc
            for paragraph in split_paragraphs(extracted):
                blocks.append(
                    ParsedBlock(
                        content=paragraph,
                        page_number=page_number,
                        block_type="paragraph",
                    )
                )

        # 第一版不做 OCR；完全抽不到文字通常代表掃描型 PDF，明確標記等待第二階段處理。
        if not blocks:
            raise ParserRequiresOcrError(
                f"PDF contains no extractable text and requires OCR: {path}"
            )

        metadata = getattr(reader, "metadata", None)
        title = normalize_text(str(getattr(metadata, "title", "") or ""))
        return ParsedDocument(
            blocks=tuple(blocks),
            parser_name=self.parser_name,
            parser_version=self.parser_version,
            mime_type="application/pdf",
            suggested_title=title or suggested_title_from_path(path),
        )
