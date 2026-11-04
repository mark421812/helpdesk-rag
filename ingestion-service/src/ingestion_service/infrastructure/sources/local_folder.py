"""安全掃描唯讀本機資料夾的 SourceScanner adapter。"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from ingestion_service.domain.entities import DiscoveredFile, Source
from ingestion_service.domain.exceptions import ValidationError

_TEMPORARY_SUFFIXES = (".part", ".swp", ".tmp", "~")


class LocalFolderScanner:
    """只發現一般檔案，且不跟隨可能逃出允許根目錄的 symbolic links。"""

    def __init__(self, allowed_root: Path) -> None:
        self._allowed_root = allowed_root.resolve(strict=True)
        if not self._allowed_root.is_dir():
            raise ValidationError(f"Configured document root is not a directory: {allowed_root}")

    def scan(self, source: Source) -> tuple[DiscoveredFile, ...]:
        """遞迴掃描整個 source，回傳依相對路徑排序的不可變結果。"""

        root = Path(source.root_path).resolve(strict=True)
        self._assert_inside_allowed_root(root)
        if not root.is_dir():
            raise ValidationError(f"Source root is not a directory: {root}")

        discovered: list[DiscoveredFile] = []
        for directory, directories, filenames in os.walk(
            root,
            topdown=True,
            followlinks=False,
            onerror=self._raise_scan_error,
        ):
            directory_path = Path(directory)
            # 修改 directories[:] 會告訴 os.walk 不要進入隱藏資料夾或 symlink。
            directories[:] = sorted(
                name
                for name in directories
                if not name.startswith(".") and not (directory_path / name).is_symlink()
            )
            for filename in sorted(filenames):
                if self._ignore_filename(filename):
                    continue
                path = directory_path / filename
                if path.is_symlink() or path.suffix.casefold() not in source.allowed_extensions:
                    continue
                resolved = path.resolve(strict=True)
                self._assert_inside_source_root(resolved, root)
                if not resolved.is_file():
                    continue
                discovered.append(self._discovered_file(resolved, root))
        return tuple(sorted(discovered, key=lambda item: item.source_item_id))

    def discover(self, source: Source, source_item_id: str) -> DiscoveredFile | None:
        """不掃完整目錄，直接安全解析一個標準化相對路徑。"""

        root = Path(source.root_path).resolve(strict=True)
        self._assert_inside_allowed_root(root)
        if not root.is_dir():
            raise ValidationError(f"Source root is not a directory: {root}")
        relative = PurePosixPath(source_item_id)
        if (
            not source_item_id
            or relative.is_absolute()
            or ".." in relative.parts
            or relative.as_posix() != source_item_id
            or any(part.startswith(".") for part in relative.parts)
            or self._ignore_filename(relative.name)
        ):
            raise ValidationError("source_item_id must be a visible normalized relative POSIX path")
        if relative.suffix.casefold() not in source.allowed_extensions:
            return None

        # 逐層檢查 symlink，而不是只檢查最後檔案，避免中間資料夾繞過 root。
        candidate = root
        for part in relative.parts:
            candidate /= part
            if candidate.is_symlink():
                raise ValidationError(f"Symbolic links are not accepted: {candidate}")
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError:
            return None
        self._assert_inside_source_root(resolved, root)
        if not resolved.is_file():
            return None
        return self._discovered_file(resolved, root)

    @staticmethod
    def _discovered_file(path: Path, root: Path) -> DiscoveredFile:
        stat = path.stat()
        relative_path = path.relative_to(root).as_posix()
        return DiscoveredFile(
            source_item_id=relative_path,
            relative_path=relative_path,
            absolute_path=path,
            file_size=stat.st_size,
            modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
        )

    def _assert_inside_allowed_root(self, root: Path) -> None:
        try:
            root.relative_to(self._allowed_root)
        except ValueError as exc:
            raise ValidationError(f"Source root escapes configured document root: {root}") from exc

    @staticmethod
    def _assert_inside_source_root(path: Path, root: Path) -> None:
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValidationError(f"Document escapes configured source root: {path}") from exc

    @staticmethod
    def _ignore_filename(filename: str) -> bool:
        lowered = filename.casefold()
        return (
            filename.startswith(".")
            or filename.startswith("~$")
            or lowered.endswith(_TEMPORARY_SUFFIXES)
            or lowered.endswith(".metadata.yaml")
        )

    @staticmethod
    def _raise_scan_error(error: OSError) -> None:
        raise ValidationError(f"Unable to scan local folder: {error}") from error
