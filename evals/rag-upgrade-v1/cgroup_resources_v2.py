"""只讀本次已驗 ownership 的私有 server cgroup，不讀 env、log 或公司資料。"""

from __future__ import annotations

import math

from isolated_kis_audit import AuditFailure, command

COUNTERS = ("usage_usec", "nr_periods", "nr_throttled", "throttled_usec")


def parse_cgroup(raw: str) -> dict:
    try:
        lines = raw.splitlines()
        current, peak = (int(value) for value in lines[:2])
        rows = [line.split() for line in lines[2:]]
        if any(len(row) != 2 for row in rows):
            raise ValueError
        values = {key: int(value) for key, value in rows}
        if len(values) != len(rows) or not set(COUNTERS) <= values.keys():
            raise ValueError
        if current < 0 or peak < current or any(value < 0 for value in values.values()):
            raise ValueError
        return {
            "memory_current_bytes": current,
            "memory_lifetime_peak_bytes": peak,
            "cpu": {key: values[key] for key in COUNTERS},
        }
    except (ValueError, TypeError):
        raise AuditFailure("cgroup_v2_counters_invalid") from None


def snapshot(resources, roles: tuple[str, ...]) -> dict:
    result = {}
    for role in roles:
        container_id = resources.roles[role]
        # inspect 嚴格驗證本次 exact ID + label + network；不能只靠名稱。
        if not resources.inspect(container_id)["running"]:
            raise AuditFailure("cgroup_owned_server_not_running")
        result[role] = parse_cgroup(
            command(
                "exec",
                container_id,
                "cat",
                "/sys/fs/cgroup/memory.current",
                "/sys/fs/cgroup/memory.peak",
                "/sys/fs/cgroup/cpu.stat",
            )
        )
    return result


def window(before: dict, after: dict, elapsed_seconds: float) -> dict:
    if (
        not before
        or before.keys() != after.keys()
        or not math.isfinite(elapsed_seconds)
        or elapsed_seconds <= 0
    ):
        raise AuditFailure("cgroup_window_invalid")
    servers = {}
    for role in before:
        a, b = before[role], after[role]
        delta = {key: b["cpu"][key] - a["cpu"][key] for key in COUNTERS}
        if any(value < 0 for value in delta.values()):
            raise AuditFailure("cgroup_counter_reset")
        servers[role] = {
            "before": a,
            "after": b,
            "cpu_delta": delta,
            "average_cpu_cores": round(delta["usage_usec"] / 1e6 / elapsed_seconds, 6),
        }
    return {
        "source": "owned_linux_cgroup_v2_read_only",
        "elapsed_seconds": round(elapsed_seconds, 6),
        "window": "includes_readiness_load_and_snapshot_overhead_not_queries_only",
        "memory_peak_scope": "container_lifetime_including_setup_not_load_only",
        "servers": servers,
    }
