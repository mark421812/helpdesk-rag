"""Parser 共用 Protocol、錯誤與文字正規化 helpers。"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Protocol

from ingestion_service.domain.entities import ParsedDocument
from ingestion_service.domain.exceptions import UnsupportedDocumentError, ValidationError


class DocumentParsingError(ValidationError):
    """支援的格式存在，但文件無法被安全解析。"""


class UnsupportedDocumentTypeError(UnsupportedDocumentError):
    """沒有任何 parser 註冊此副檔名／MIME type。"""


class DocumentParser(Protocol):
    """所有檔案 parser 共同遵守的 structural typing contract。"""

    extensions: frozenset[str]
    mime_types: frozenset[str]

    def parse(self, path: Path) -> ParsedDocument:
        """Parse a file into structure-preserving blocks."""
        ...


def validate_input_file(path: Path) -> None:
    """所有 parser 共用的檔案存在性與一般檔案檢查。"""

    if not path.exists():
        raise DocumentParsingError(f"Document does not exist: {path}")
    if not path.is_file():
        raise DocumentParsingError(f"Document is not a regular file: {path}")


def normalize_text(value: str) -> str:
    """正規化 Unicode 與換行，但不破壞有意義的段落／表格結構。"""
    value = unicodedata.normalize("NFC", value)
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = value.replace("\x00", "")
    lines = [re.sub(r"[\t\f\v ]+$", "", line) for line in value.split("\n")]
    return "\n".join(lines).strip()


def split_paragraphs(value: str) -> tuple[str, ...]:
    """以一個以上空白行切段，過濾純空白段落。"""

    normalized = normalize_text(value)
    if not normalized:
        return ()
    return tuple(
        paragraph.strip() for paragraph in re.split(r"\n[\t ]*\n+", normalized) if paragraph.strip()
    )


def suggested_title_from_path(path: Path) -> str:
    """沒有文件內標題時，用檔名產生最低限度的顯示標題建議。"""

    return path.stem.replace("_", " ").replace("-", " ").strip() or path.name
