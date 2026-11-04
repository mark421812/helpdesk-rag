"""安全讀取、分層合併並驗證 YAML metadata 的 adapter。"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any, TypeVar

import yaml  # type: ignore[import-untyped]

from ingestion_service.domain.entities import (
    AccessControlEntry,
    DiscoveredFile,
    DocumentMetadata,
    ParsedDocument,
    Source,
    SourceItem,
)
from ingestion_service.domain.enums import (
    Confidentiality,
    DocumentStatus,
    PermissionEffect,
    PrincipalType,
)
from ingestion_service.domain.exceptions import ValidationError

FOLDER_METADATA_FILE = ".ingestion.yaml"
FILE_METADATA_SUFFIX = ".metadata.yaml"
MAX_METADATA_FILE_BYTES = 1024 * 1024
_METADATA_FIELDS = frozenset(
    {
        "title",
        "document_type",
        "owner_department",
        "document_owner",
        "version_number",
        "confidentiality",
        "status",
        "language",
        "external_llm_allowed",
        "source_url",
        "effective_at",
        "expires_at",
        "review_at",
        "custom_metadata",
        "acl",
    }
)
EnumT = TypeVar("EnumT", bound=Enum)
# TypeVar 讓 _enum 的「輸入 Enum 類別」與「回傳該 Enum 實例」維持相同精確型別。


class YamlMetadataResolver:
    """依 source defaults → 各層資料夾 → 單檔 sidecar 的優先序解析 metadata。

    越靠近文件的設定優先級越高。必要欄位缺失時 ``DocumentMetadata`` 會拋錯，系統不會
    猜測 owner、機密等級或文件類型。
    """

    def __init__(self, source_defaults: Mapping[str, object] | None = None) -> None:
        self._source_defaults = dict(source_defaults or {})

    def resolve(
        self,
        source: Source,
        item: SourceItem,
        parsed: ParsedDocument,
    ) -> DocumentMetadata:
        file_path = self._validated_file_path(source, Path(item.absolute_path))
        merged: dict[str, Any] = dict(self._source_defaults)
        for metadata_file in self.metadata_files(Path(source.root_path), file_path):
            merged = _deep_merge(merged, self._read_mapping(metadata_file))
        self._reject_unknown_fields(merged, file_path)
        return _document_metadata(merged)

    def load(
        self,
        source: Source,
        file: DiscoveredFile,
        parsed: ParsedDocument,
    ) -> DocumentMetadata:
        """合併與驗證一份已發現文件的 metadata。``parsed`` 保留供未來策略使用。"""

        file_path = self._validated_file_path(source, file.absolute_path)
        merged: dict[str, Any] = dict(self._source_defaults)
        for metadata_file in self.metadata_files(Path(source.root_path), file_path):
            merged = _deep_merge(merged, self._read_mapping(metadata_file))
        self._reject_unknown_fields(merged, file_path)
        return _document_metadata(merged)

    def source_fingerprint(self, source: Source, file: DiscoveredFile) -> str:
        return self.fingerprint(source, file.absolute_path)

    def fingerprint(self, source: Source, file_path: Path) -> str:
        """對 defaults 與所有生效 YAML 計算 hash，偵測 metadata-only change。"""

        resolved_file = self._validated_file_path(source, file_path)
        digest = hashlib.sha256(
            json.dumps(
                self._source_defaults,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        )
        root = Path(source.root_path).resolve(strict=True)
        for metadata_file in self.metadata_files(root, resolved_file):
            relative_name = metadata_file.relative_to(root).as_posix().encode("utf-8")
            digest.update(len(relative_name).to_bytes(4, "big"))
            digest.update(relative_name)
            contents = self._read_limited(metadata_file)
            digest.update(len(contents).to_bytes(8, "big"))
            digest.update(contents)
        return digest.hexdigest()

    @staticmethod
    def metadata_files(root: Path, file_path: Path) -> tuple[Path, ...]:
        """依覆寫順序列出存在的 folder defaults 與單檔 sidecar。"""

        resolved_root = root.resolve(strict=True)
        resolved_file = file_path.resolve(strict=True)
        try:
            relative_parent = resolved_file.parent.relative_to(resolved_root)
        except ValueError as exc:
            raise ValidationError(f"Document escapes configured source root: {file_path}") from exc

        # 例如 root/a/b/file.pdf 依序套用 root、a、b 內的 .ingestion.yaml。
        folders = [resolved_root]
        current = resolved_root
        for part in relative_parent.parts:
            current /= part
            folders.append(current)
        candidates = [folder / FOLDER_METADATA_FILE for folder in folders]
        candidates.append(resolved_file.with_name(resolved_file.name + FILE_METADATA_SUFFIX))
        return tuple(path for path in candidates if path.is_file() and not path.is_symlink())

    @staticmethod
    def _validated_file_path(source: Source, file_path: Path) -> Path:
        if file_path.is_symlink():
            raise ValidationError(f"Symbolic links are not accepted: {file_path}")
        root = Path(source.root_path).resolve(strict=True)
        resolved_file = file_path.resolve(strict=True)
        if not resolved_file.is_file():
            raise ValidationError(f"Document is not a regular file: {file_path}")
        try:
            resolved_file.relative_to(root)
        except ValueError as exc:
            raise ValidationError(f"Document escapes configured source root: {file_path}") from exc
        return resolved_file

    @staticmethod
    def _read_limited(path: Path) -> bytes:
        size = path.stat().st_size
        if size > MAX_METADATA_FILE_BYTES:
            raise ValidationError(f"Metadata file exceeds {MAX_METADATA_FILE_BYTES} bytes: {path}")
        return path.read_bytes()

    def _read_mapping(self, path: Path) -> dict[str, Any]:
        """用 safe_load 讀 YAML，拒絕任意 Python object constructor。"""

        try:
            value = yaml.safe_load(self._read_limited(path).decode("utf-8-sig"))
        except (UnicodeDecodeError, yaml.YAMLError) as exc:
            raise ValidationError(f"Invalid UTF-8 YAML metadata file: {path}") from exc
        if value is None:
            return {}
        if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
            raise ValidationError(f"Metadata YAML root must be a string-keyed mapping: {path}")
        if "metadata" in value:
            if set(value) != {"metadata"} or not isinstance(value["metadata"], dict):
                raise ValidationError(
                    f"When used, 'metadata' must be the only YAML root key: {path}"
                )
            value = value["metadata"]
        return dict(value)

    @staticmethod
    def _reject_unknown_fields(values: Mapping[str, Any], file_path: Path) -> None:
        unknown = set(values).difference(_METADATA_FIELDS)
        if unknown:
            raise ValidationError(
                f"Unknown metadata fields for {file_path}: {', '.join(sorted(unknown))}"
            )


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """遞迴合併 mapping；一般值由 override 取代，且不原地修改輸入物件。"""

    result = dict(base)
    for key, value in override.items():
        existing = result.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            result[key] = _deep_merge(existing, value)
        else:
            result[key] = value
    return result


def _document_metadata(values: Mapping[str, Any]) -> DocumentMetadata:
    """把鬆散 YAML values 轉成強型別 Domain value object。"""

    acl_value = values.get("acl", [])
    if not isinstance(acl_value, list):
        raise ValidationError("Metadata acl must be a list")
    acl = tuple(_acl_entry(value) for value in acl_value)
    custom_metadata = values.get("custom_metadata", {})
    if not isinstance(custom_metadata, dict):
        raise ValidationError("custom_metadata must be a mapping")
    return DocumentMetadata(
        title=_string(values.get("title")),
        document_type=_string(values.get("document_type")),
        owner_department=_string(values.get("owner_department")),
        document_owner=_string(values.get("document_owner")),
        version_number=_optional_string(values.get("version_number")),
        confidentiality=_enum(
            Confidentiality,
            values.get("confidentiality", Confidentiality.INTERNAL.value),
            "confidentiality",
        ),
        status=_enum(
            DocumentStatus,
            values.get("status", DocumentStatus.DRAFT.value),
            "status",
        ),
        language=_string(values.get("language", "zh-TW")),
        external_llm_allowed=_boolean(values.get("external_llm_allowed", False)),
        source_url=_optional_string(values.get("source_url")),
        effective_at=_optional_date(values.get("effective_at"), "effective_at"),
        expires_at=_optional_date(values.get("expires_at"), "expires_at"),
        review_at=_optional_date(values.get("review_at"), "review_at"),
        custom_metadata=_json_mapping(custom_metadata),
        acl=acl,
    )


def _acl_entry(value: object) -> AccessControlEntry:
    if not isinstance(value, dict):
        raise ValidationError("Each ACL entry must be a mapping")
    unknown = set(value).difference({"principal_type", "principal_id", "effect"})
    if unknown:
        raise ValidationError(f"Unknown ACL fields: {', '.join(sorted(unknown))}")
    return AccessControlEntry(
        principal_type=_enum(PrincipalType, value.get("principal_type"), "principal_type"),
        principal_id=_string(value.get("principal_id")),
        effect=_enum(
            PermissionEffect,
            value.get("effect", PermissionEffect.ALLOW.value),
            "effect",
        ),
    )


def _string(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError("Optional metadata string value has an invalid type")
    return value.strip() or None


def _boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise ValidationError("external_llm_allowed must be a boolean")
    return value


def _optional_date(value: object, field_name: str) -> date | None:
    if value is None or isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ValidationError(f"{field_name} must use YYYY-MM-DD") from exc
    raise ValidationError(f"{field_name} must be a date or YYYY-MM-DD string")


def _json_mapping(value: Mapping[object, object]) -> dict[str, Any]:
    if not all(isinstance(key, str) for key in value):
        raise ValidationError("custom_metadata keys must be strings")
    return {str(key): _json_value(item, f"custom_metadata.{key}") for key, item in value.items()}


def _json_value(value: object, path: str) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValidationError(f"{path} must be a finite JSON number")
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, list):
        return [_json_value(item, f"{path}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, dict):
        return _json_mapping(value)
    raise ValidationError(f"{path} contains a value that cannot be represented as JSON")


def _enum(enum_type: type[EnumT], value: object, field_name: str) -> EnumT:
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"Invalid {field_name}: {value}") from exc
