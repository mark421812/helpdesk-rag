"""保留 heading hierarchy、忽略純格式符號的 Markdown parser。"""

from __future__ import annotations

import re
from pathlib import Path

from ingestion_service.domain.entities import ParsedBlock, ParsedDocument

from .base import (
    DocumentParsingError,
    normalize_text,
    suggested_title_from_path,
    validate_input_file,
)
from .text import TextDocumentParser

_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_FENCE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})")
_LIST_RE = re.compile(r"^[ \t]*(?:[-+*]|\d+[.)])[ \t]+")
_TABLE_SEPARATOR_RE = re.compile(
    r"^[ \t]*\|?[ \t]*:?-{3,}:?[ \t]*(?:\|[ \t]*:?-{3,}:?[ \t]*)+\|?[ \t]*$"
)


class MarkdownDocumentParser:
    """以小型 state machine 保留 heading、code fence、table 與 list 結構。"""

    extensions = frozenset({".md", ".markdown"})
    mime_types = frozenset({"text/markdown", "text/x-markdown"})
    parser_name = "markdown"
    parser_version = "1"

    def parse(self, path: Path) -> ParsedDocument:
        validate_input_file(path)
        text = normalize_text(TextDocumentParser._decode(path.read_bytes(), path))
        if not text:
            raise DocumentParsingError(f"Markdown document is empty: {path}")

        blocks: list[ParsedBlock] = []
        headings: list[str] = []
        suggested_title: str | None = None
        buffer: list[str] = []
        buffer_type = "paragraph"
        in_fence = False
        fence_marker = ""

        def section_path() -> str | None:
            # 內部函式（closure）可直接讀取外層 headings，不需額外建立 class。
            populated = [heading for heading in headings if heading]
            return " > ".join(populated) if populated else None

        def flush() -> None:
            # nonlocal 表示要重新指定外層函式的變數，而非建立同名區域變數。
            nonlocal buffer, buffer_type
            content = normalize_text("\n".join(buffer))
            if content:
                blocks.append(
                    ParsedBlock(
                        content=content,
                        section_path=section_path(),
                        block_type=buffer_type,
                    )
                )
            buffer = []
            buffer_type = "paragraph"

        lines = text.splitlines()
        index = 0
        # 需要一次消耗多行 table/code block，因此以 while 手動控制 index。
        while index < len(lines):
            line = lines[index]
            fence_match = _FENCE_RE.match(line)
            if in_fence:
                buffer.append(line)
                if fence_match and line.lstrip().startswith(fence_marker):
                    flush()
                    in_fence = False
                    fence_marker = ""
                index += 1
                continue

            if fence_match:
                flush()
                in_fence = True
                fence_marker = fence_match.group(1)[0] * len(fence_match.group(1))
                buffer_type = "code"
                buffer.append(line)
                index += 1
                continue

            heading_match = _HEADING_RE.match(line)
            if heading_match:
                flush()
                level = len(heading_match.group(1))
                heading = heading_match.group(2).strip()
                del headings[level - 1 :]
                while len(headings) < level - 1:
                    headings.append("")
                headings.append(heading)
                if level == 1 and suggested_title is None:
                    suggested_title = heading
                index += 1
                continue

            if not line.strip():
                flush()
                index += 1
                continue

            is_table_start = (
                "|" in line
                and index + 1 < len(lines)
                and _TABLE_SEPARATOR_RE.match(lines[index + 1]) is not None
            )
            if is_table_start:
                flush()
                buffer_type = "table"
                buffer.extend((line, lines[index + 1]))
                index += 2
                while index < len(lines) and "|" in lines[index] and lines[index].strip():
                    buffer.append(lines[index])
                    index += 1
                flush()
                continue

            if _LIST_RE.match(line):
                if buffer and buffer_type != "list":
                    flush()
                buffer_type = "list"
                buffer.append(line)
                index += 1
                continue

            if buffer and buffer_type == "list":
                flush()
            buffer.append(line)
            index += 1

        flush()
        if not blocks:
            raise DocumentParsingError(f"Markdown document contains no indexable content: {path}")

        return ParsedDocument(
            blocks=tuple(blocks),
            parser_name=self.parser_name,
            parser_version=self.parser_version,
            mime_type="text/markdown",
            suggested_title=suggested_title or suggested_title_from_path(path),
        )
