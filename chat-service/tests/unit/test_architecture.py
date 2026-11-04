"""以 AST 檢查 Clean Architecture core 不會反向依賴 framework/infrastructure。"""

from __future__ import annotations

import ast
from pathlib import Path

CORE_ROOTS = (
    Path(__file__).parents[2] / "src/chat_service/domain",
    Path(__file__).parents[2] / "src/chat_service/application",
)
BANNED_IMPORT_ROOTS = {
    "alembic",
    "asyncpg",
    "fastapi",
    "httpx",
    "prometheus_client",
    "pydantic",
    "pydantic_settings",
    "sqlalchemy",
    "starlette",
    "structlog",
}
BANNED_IMPORT_PREFIXES = (
    "chat_service.infrastructure",
    "chat_service.presentation",
)


def test_domain_and_application_do_not_import_frameworks() -> None:
    """Domain/Application 只能依賴標準庫與自身 core module。"""

    violations: list[str] = []
    for root in CORE_ROOTS:
        for source_file in root.rglob("*.py"):
            tree = ast.parse(source_file.read_text(encoding="utf-8"), filename=str(source_file))
            for node in ast.walk(tree):
                imported_modules: list[str] = []
                if isinstance(node, ast.Import):
                    imported_modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported_modules = [node.module]
                for imported_module in imported_modules:
                    imported_root = imported_module.partition(".")[0]
                    if imported_root in BANNED_IMPORT_ROOTS:
                        violations.append(f"{source_file}: imports {imported_module}")
                    if imported_module.startswith(BANNED_IMPORT_PREFIXES):
                        violations.append(f"{source_file}: imports {imported_module}")

                    if root.name == "domain" and imported_module.startswith("chat_service."):
                        if not imported_module.startswith("chat_service.domain"):
                            violations.append(
                                f"{source_file}: domain imports non-domain {imported_module}"
                            )
                    if root.name == "application" and imported_module.startswith("chat_service."):
                        if not imported_module.startswith(
                            ("chat_service.domain", "chat_service.application")
                        ):
                            violations.append(
                                f"{source_file}: application imports non-core {imported_module}"
                            )

    assert violations == []
