"""以 python-docx 讀取段落與 heading 的 DOCX parser。"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ingestion_service.domain.entities import ParsedBlock, ParsedDocument

from .base import (
    DocumentParsingError,
    normalize_text,
    suggested_title_from_path,
    validate_input_file,
)

_HEADING_STYLE_RE = re.compile(r"^heading[ \t]+([1-9])$", re.IGNORECASE)


class DocxDocumentParser:
    """依原文件順序讀取 DOCX 段落與表格，並維護 Heading 階層。"""

    extensions = frozenset({".docx"})
    mime_types = frozenset(
        {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
    )
    parser_name = "python-docx"
    parser_version = "1"
    mime_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

    def parse(self, path: Path) -> ParsedDocument:
        validate_input_file(path)
        try:
            from docx import Document

            # python-docx 的公開型別契約接受路徑字串或 binary stream；轉成
            # str 也能同時相容 Intel macOS 與 Linux ARM 的 pathlib.Path。
            document = Document(str(path))
        except ImportError as exc:  # pragma: no cover - declared runtime dependency
            raise DocumentParsingError(
                "DOCX parser dependency 'python-docx' is unavailable"
            ) from exc
        except Exception as exc:
            raise DocumentParsingError(f"Unable to open DOCX document: {path}") from exc

        blocks: list[ParsedBlock] = []
        headings: list[str] = []
        suggested_title: str | None = None

        for item_kind, item in self._iter_document_items(document):
            if item_kind == "paragraph":
                content = normalize_text(item.text)
                if not content:
                    continue
                style_name = str(getattr(getattr(item, "style", None), "name", "") or "")
                heading_match = _HEADING_STYLE_RE.match(style_name)
                if heading_match:
                    level = int(heading_match.group(1))
                    # slice deletion 移除同級以下舊標題，例如從 2.1 移到新的第 2 章。
                    del headings[level - 1 :]
                    while len(headings) < level - 1:
                        headings.append("")
                    headings.append(content)
                    if level == 1 and suggested_title is None:
                        suggested_title = content
                    continue
                if style_name.casefold() == "title" and suggested_title is None:
                    suggested_title = content
                    continue
                blocks.append(
                    ParsedBlock(
                        content=content,
                        section_path=self._section_path(headings),
                        block_type="paragraph",
                    )
                )
                continue

            rendered_table = self._render_table(item)
            if rendered_table:
                blocks.append(
                    ParsedBlock(
                        content=rendered_table,
                        section_path=self._section_path(headings),
                        block_type="table",
                    )
                )

        if not blocks:
            raise DocumentParsingError(f"DOCX document contains no indexable content: {path}")
        return ParsedDocument(
            blocks=tuple(blocks),
            parser_name=self.parser_name,
            parser_version=self.parser_version,
            mime_type=self.mime_type,
            suggested_title=suggested_title or suggested_title_from_path(path),
        )

    @staticmethod
    def _section_path(headings: list[str]) -> str | None:
        populated = [heading for heading in headings if heading]
        return " > ".join(populated) if populated else None

    @staticmethod
    def _iter_document_items(document: Any) -> Iterator[tuple[str, Any]]:
        """用 generator/yield 保留 paragraph 與 table 在 Word 中的交錯順序。"""

        from docx.oxml.table import CT_Tbl
        from docx.oxml.text.paragraph import CT_P
        from docx.table import Table
        from docx.text.paragraph import Paragraph

        for child in document.element.body.iterchildren():
            if isinstance(child, CT_P):
                yield "paragraph", Paragraph(child, document)
            elif isinstance(child, CT_Tbl):
                yield "table", Table(child, document)

    @staticmethod
    def _render_table(table: Any) -> str:
        """將 Word table 轉成 Markdown-like 文字，讓 embedding 保留欄列關係。"""

        rows: list[str] = []
        for row in table.rows:
            cells = [normalize_text(cell.text).replace("|", r"\|") for cell in row.cells]
            if any(cells):
                rows.append("| " + " | ".join(cells) + " |")
        if not rows:
            return ""
        if len(rows) == 1:
            return rows[0]
        column_count = len(table.rows[0].cells)
        separator = "| " + " | ".join("---" for _ in range(column_count)) + " |"
        return "\n".join((rows[0], separator, *rows[1:]))
