"""Metadata loader adapters；目前實作資料夾預設與 YAML sidecar 合併。"""

from ingestion_service.infrastructure.metadata.yaml_resolver import YamlMetadataResolver

__all__ = ["YamlMetadataResolver"]
