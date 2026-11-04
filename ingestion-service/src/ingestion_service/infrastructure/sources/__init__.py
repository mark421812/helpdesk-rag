"""文件來源 adapters；第一版為 local folder，未來可加入 SharePoint。"""

from ingestion_service.infrastructure.sources.local_folder import LocalFolderScanner

__all__ = ["LocalFolderScanner"]
