"""Chat Service 的 isolated Prometheus metrics。

每個 application 使用自己的 ``CollectorRegistry``，避免 import-time default registry 把
多個測試或同一 process 的 service metrics 混在一起。所有 label 都是固定的 method、route
template、status、outcome、dependency 或 error category，不使用 user/query/resource ID。
"""

from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter
from typing import Final

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

_METRICS_CONTENT_TYPE: Final[str] = "text/plain; version=0.0.4"


@dataclass(slots=True)
class Metrics:
    """低 cardinality application metrics facade。"""

    registry: CollectorRegistry
    http_requests: Counter
    http_duration: Histogram
    orchestration_outcomes: Counter
    dependency_duration: Histogram
    dependency_errors: Counter
    active_turns: Gauge
    capacity_rejections: Counter
    persistence_failures: Counter

    @classmethod
    def create(cls) -> Metrics:
        """建立 isolated registry 與 Phase 6 最低必要 metrics。"""

        registry = CollectorRegistry()
        return cls(
            registry=registry,
            http_requests=Counter(
                "chat_http_requests_total",
                "HTTP requests handled by chat-service.",
                ("method", "route", "status"),
                registry=registry,
            ),
            http_duration=Histogram(
                "chat_http_request_duration_seconds",
                "HTTP request latency.",
                ("method", "route"),
                registry=registry,
            ),
            orchestration_outcomes=Counter(
                "chat_orchestration_outcomes_total",
                "Terminal orchestration outcomes.",
                ("outcome",),
                registry=registry,
            ),
            dependency_duration=Histogram(
                "chat_dependency_duration_seconds",
                "Downstream dependency latency.",
                ("dependency",),
                registry=registry,
            ),
            dependency_errors=Counter(
                "chat_dependency_errors_total",
                "Downstream dependency errors by safe category.",
                ("dependency", "category"),
                registry=registry,
            ),
            active_turns=Gauge(
                "chat_active_turns",
                "Active turn leases in this Chat process.",
                registry=registry,
            ),
            capacity_rejections=Counter(
                "chat_capacity_rejections_total",
                "Turns rejected by bounded capacity.",
                ("reason",),
                registry=registry,
            ),
            persistence_failures=Counter(
                "chat_persistence_failures_total",
                "Persistence failures by safe category.",
                ("operation",),
                registry=registry,
            ),
        )

    def observe_http(
        self,
        *,
        method: str,
        route: str,
        status: str,
        elapsed_seconds: float,
    ) -> None:
        """記錄 HTTP count/latency，route 僅使用 template。"""

        self.http_requests.labels(method, route, status).inc()
        self.http_duration.labels(method, route).observe(max(0.0, elapsed_seconds))

    def observe_dependency(self, dependency: str, started: float) -> None:
        """記錄下游 latency；dependency 是固定字串 retrieval/answer。"""

        self.dependency_duration.labels(dependency).observe(max(0.0, perf_counter() - started))

    def record_outcome(self, outcome: str) -> None:
        """記錄安全 business outcome。"""

        self.orchestration_outcomes.labels(outcome).inc()

    def record_dependency_error(self, dependency: str, category: str) -> None:
        """記錄已分類 dependency error，不接受 raw exception text。"""

        self.dependency_errors.labels(dependency, category).inc()

    def exposition(self) -> bytes:
        """輸出 isolated registry 的 Prometheus text exposition。"""

        return generate_latest(self.registry)


METRICS_CONTENT_TYPE = _METRICS_CONTENT_TYPE
