from __future__ import annotations

import ast
from pathlib import Path

CORE_ROOTS = (
    Path("src/retrieval_service/domain"),
    Path("src/retrieval_service/application"),
)
BANNED_FRAMEWORKS = {
    "fastapi",
    "httpx",
    "prometheus_client",
    "pydantic",
    "qdrant_client",
    "sqlalchemy",
    "starlette",
    "structlog",
}


def test_domain_and_application_do_not_import_frameworks() -> None:
    violations: list[str] = []
    for root in CORE_ROOTS:
        for source_file in root.rglob("*.py"):
            tree = ast.parse(source_file.read_text(encoding="utf-8"), filename=str(source_file))
            for node in ast.walk(tree):
                imported_roots: list[str] = []
                if isinstance(node, ast.Import):
                    imported_roots = [alias.name.partition(".")[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported_roots = [node.module.partition(".")[0]]
                for imported_root in imported_roots:
                    if imported_root in BANNED_FRAMEWORKS:
                        violations.append(f"{source_file}: imports {imported_root}")

    assert violations == []
