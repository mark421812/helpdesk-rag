from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from ingestion_service.domain.entities import Source
from ingestion_service.domain.exceptions import ValidationError
from ingestion_service.infrastructure.sources.local_folder import LocalFolderScanner


def test_scanner_returns_supported_regular_files_in_stable_order(tmp_path: Path) -> None:
    root = tmp_path / "documents"
    root.mkdir()
    (root / "z.txt").write_text("z", encoding="utf-8")
    (root / "a.MD").write_text("a", encoding="utf-8")
    (root / "ignore.xlsx").write_text("x", encoding="utf-8")
    (root / "z.txt.metadata.yaml").write_text("title: z", encoding="utf-8")
    (root / ".hidden.txt").write_text("hidden", encoding="utf-8")
    nested = root / "nested"
    nested.mkdir()
    (nested / "guide.pdf").write_bytes(b"pdf")
    (root / "linked.txt").symlink_to(root / "z.txt")
    source = Source(id=uuid4(), name="local", root_path=str(root))

    files = LocalFolderScanner(tmp_path).scan(source)

    assert [item.relative_path for item in files] == [
        "a.MD",
        "nested/guide.pdf",
        "z.txt",
    ]
    assert all(item.source_item_id == item.relative_path for item in files)


def test_scanner_rejects_source_outside_allowed_document_root(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    source = Source(id=uuid4(), name="outside", root_path=str(outside))

    with pytest.raises(ValidationError, match="escapes configured document root"):
        LocalFolderScanner(allowed).scan(source)


def test_discover_resolves_exactly_one_visible_relative_file(tmp_path: Path) -> None:
    root = tmp_path / "documents"
    nested = root / "policies"
    nested.mkdir(parents=True)
    expected = nested / "leave.md"
    expected.write_text("leave policy", encoding="utf-8")
    source = Source(id=uuid4(), name="local", root_path=str(root))
    scanner = LocalFolderScanner(tmp_path)

    discovered = scanner.discover(source, "policies/leave.md")

    assert discovered is not None
    assert discovered.absolute_path == expected
    assert scanner.discover(source, "policies/missing.md") is None
    with pytest.raises(ValidationError, match="normalized relative POSIX path"):
        scanner.discover(source, "../outside.md")
