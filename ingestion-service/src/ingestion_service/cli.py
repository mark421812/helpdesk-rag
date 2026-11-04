"""``ingestion`` 命令列介面。

CLI 與 FastAPI 都只是 Presentation adapter，最後呼叫相同的 Application use cases，因此
商業規則不會複製兩份。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import UUID

from ingestion_service.bootstrap import Runtime, build_runtime, ensure_default_source
from ingestion_service.domain.enums import SourceItemStatus, SyncTrigger
from ingestion_service.domain.exceptions import DomainError
from ingestion_service.infrastructure.config import get_settings
from ingestion_service.infrastructure.logging import configure_logging


def build_parser() -> argparse.ArgumentParser:
    """宣告 CLI subcommands 與參數；argparse 會自動產生 ``--help``。"""

    parser = argparse.ArgumentParser(prog="ingestion", description="Ingestion service CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("sources", help="List enabled document sources")

    scan = subparsers.add_parser("scan", help="Scan a source and enqueue changed files")
    scan.add_argument("--source-id", type=UUID)
    scan.add_argument(
        "--allow-mass-deletion",
        action="store_true",
        help="Explicitly bypass the missing-ratio guard for this scan",
    )

    ingest = subparsers.add_parser("ingest", help="Discover and enqueue one relative file path")
    ingest.add_argument("source_item_id")
    ingest.add_argument("--source-id", type=UUID)

    run = subparsers.add_parser("run", help="Show a source scan run")
    run.add_argument("run_id", type=UUID)

    items = subparsers.add_parser("items", help="List source items")
    items.add_argument("--source-id", type=UUID)
    items.add_argument("--status", choices=[status.value for status in SourceItemStatus])
    items.add_argument("--offset", type=int, default=0)
    items.add_argument("--limit", type=int, default=100)

    item = subparsers.add_parser("item", help="Show one source item")
    item.add_argument("item_id", type=UUID)

    job = subparsers.add_parser("job", help="Show one ingestion job")
    job.add_argument("job_id", type=UUID)

    retry = subparsers.add_parser("retry", help="Retry one failed ingestion job")
    retry.add_argument("job_id", type=UUID)
    return parser


async def run_command(args: argparse.Namespace, runtime: Runtime) -> object:
    """將已解析的 CLI command 對應到同一組 Application use cases。"""

    if args.command == "sources":
        return await runtime.list_enabled_sources()
    if args.command == "scan":
        source_id = args.source_id or runtime.default_source_id
        return await runtime.scan_source.execute(
            source_id,
            SyncTrigger.CLI,
            allow_mass_deletion=args.allow_mass_deletion,
        )
    if args.command == "ingest":
        source_id = args.source_id or runtime.default_source_id
        return await runtime.ingest_file.execute(
            source_id,
            args.source_item_id,
            SyncTrigger.CLI,
        )
    if args.command == "run":
        return await runtime.get_run.execute(args.run_id)
    if args.command == "items":
        status = SourceItemStatus(args.status) if args.status else None
        return await runtime.list_items.execute(
            source_id=args.source_id,
            status=status,
            offset=args.offset,
            limit=args.limit,
        )
    if args.command == "item":
        return await runtime.get_item.execute(args.item_id)
    if args.command == "job":
        return await runtime.get_job.execute(args.job_id)
    if args.command == "retry":
        return await runtime.retry_job.execute(args.job_id)
    raise RuntimeError(f"Unsupported CLI command: {args.command}")


async def async_main(argv: list[str] | None = None) -> int:
    """執行 CLI 並以 0/2 exit code 表示成功或 domain error。"""

    parser = build_parser()
    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level)
    runtime = build_runtime(settings)
    try:
        await ensure_default_source(runtime)
        result = await run_command(args, runtime)
        print(json.dumps(_jsonable(result), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except DomainError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        await runtime.close()


def _jsonable(value: Any) -> Any:
    """遞迴把 dataclass、Enum、UUID、日期轉成 json.dumps 可接受的型別。"""

    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (UUID, date, datetime, Path, Enum)):
        return str(value.value) if isinstance(value, Enum) else str(value)
    return value


def main() -> None:
    """Console script 入口；SystemExit 將 integer 傳回 shell。"""

    raise SystemExit(asyncio.run(async_main()))


if __name__ == "__main__":
    main()
