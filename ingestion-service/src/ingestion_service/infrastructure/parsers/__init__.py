"""PDF、DOCX、TXT、Markdown parser adapters。"""

from .base import (
    DocumentParser,
    DocumentParsingError,
    UnsupportedDocumentTypeError,
)
from .docx import DocxDocumentParser
from .markdown import MarkdownDocumentParser
from .pdf import PdfDocumentParser
from .registry import ParserRegistry, create_default_parser_registry
from .text import TextDocumentParser

__all__ = [
    "DocumentParser",
    "DocumentParsingError",
    "DocxDocumentParser",
    "MarkdownDocumentParser",
    "ParserRegistry",
    "PdfDocumentParser",
    "TextDocumentParser",
    "UnsupportedDocumentTypeError",
    "create_default_parser_registry",
]
