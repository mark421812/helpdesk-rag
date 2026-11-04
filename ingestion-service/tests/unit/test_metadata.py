from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from ingestion_service.domain.entities import (
    DiscoveredFile,
    ParsedBlock,
    ParsedDocument,
    Source,
)
from ingestion_service.domain.exceptions import MetadataMissingError, ValidationError
from ingestion_service.infrastructure.metadata.yaml_resolver import YamlMetadataResolver


def _source(root: Path) -> Source:
    return Source(id=uuid4(), name="documents", root_path=str(root))


def _file(path: Path, root: Path) -> DiscoveredFile:
    stat = path.stat()
    return DiscoveredFile(
        source_item_id=path.relative_to(root).as_posix(),
        relative_path=path.relative_to(root).as_posix(),
        absolute_path=path,
        file_size=stat.st_size,
        modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
    )


def _parsed() -> ParsedDocument:
    return ParsedDocument(
        blocks=(ParsedBlock("公司規章內容"),),
        parser_name="test",
        parser_version="1",
        mime_type="text/plain",
        suggested_title="不得用來猜測標題",
    )


def test_folder_defaults_and_file_sidecar_are_merged_in_precedence_order(
    tmp_path: Path,
) -> None:
    department = tmp_path / "hr"
    department.mkdir()
    document = department / "leave.md"
    document.write_text("請假規章", encoding="utf-8")
    (tmp_path / ".ingestion.yaml").write_text(
        """metadata:
  document_type: policy
  owner_department: Operations
  document_owner: records@example.com
  confidentiality: internal
  status: published
  custom_metadata:
    country: TW
""",
        encoding="utf-8",
    )
    (department / ".ingestion.yaml").write_text(
        """metadata:
  owner_department: Human Resources
  custom_metadata:
    business_unit: Taiwan
""",
        encoding="utf-8",
    )
    (department / "leave.md.metadata.yaml").write_text(
        """metadata:
  title: 請假管理辦法
  version_number: "2.1"
  custom_metadata:
    country: ROC
    approved_on: 2026-08-02
""",
        encoding="utf-8",
    )

    metadata = YamlMetadataResolver().load(
        _source(tmp_path),
        _file(document, tmp_path),
        _parsed(),
    )

    assert metadata.title == "請假管理辦法"
    assert metadata.owner_department == "Human Resources"
    assert metadata.version_number == "2.1"
    assert metadata.custom_metadata == {
        "country": "ROC",
        "business_unit": "Taiwan",
        "approved_on": "2026-08-02",
    }


def test_missing_required_metadata_does_not_use_parser_suggestion(tmp_path: Path) -> None:
    document = tmp_path / "policy.txt"
    document.write_text("內容", encoding="utf-8")
    resolver = YamlMetadataResolver(
        {
            "document_type": "policy",
            "owner_department": "HR",
            "document_owner": "hr@example.com",
        }
    )

    with pytest.raises(MetadataMissingError) as captured:
        resolver.load(_source(tmp_path), _file(document, tmp_path), _parsed())

    assert captured.value.missing_fields == ("title",)


def test_unknown_metadata_field_is_rejected(tmp_path: Path) -> None:
    document = tmp_path / "policy.txt"
    document.write_text("內容", encoding="utf-8")
    (tmp_path / "policy.txt.metadata.yaml").write_text(
        """metadata:
  title: Policy
  document_type: policy
  owner_department: HR
  document_owner: hr@example.com
  made_up_field: value
""",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="Unknown metadata fields"):
        YamlMetadataResolver().load(
            _source(tmp_path),
            _file(document, tmp_path),
            _parsed(),
        )


def test_document_symlink_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "real.txt"
    target.write_text("內容", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(target)
    stat = target.stat()
    discovered = DiscoveredFile(
        source_item_id="link.txt",
        relative_path="link.txt",
        absolute_path=link,
        file_size=stat.st_size,
        modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
    )

    with pytest.raises(ValidationError, match="Symbolic links"):
        YamlMetadataResolver().load(_source(tmp_path), discovered, _parsed())
