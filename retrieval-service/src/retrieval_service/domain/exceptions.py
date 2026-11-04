"""Retrieval 的預期例外階層。

自訂例外讓 Presentation layer 能把不同失敗轉成正確 HTTP status，而不是把 dependency
timeout、契約錯誤與使用者輸入錯誤全部當成 500。
"""

from __future__ import annotations


class RetrievalError(Exception):
    """Base class for expected errors that may cross an application boundary."""


class DomainValidationError(RetrievalError, ValueError):
    """A domain value or policy is invalid."""


class DependencyError(RetrievalError):
    """Base class for a failure reported by an outbound dependency adapter."""

    def __init__(self, dependency: str, message: str | None = None) -> None:
        # 呼叫 ``super()`` 讓父類別 Exception 保存最後要顯示的錯誤訊息。
        normalized_dependency = dependency.strip()
        if not normalized_dependency:
            raise ValueError("dependency must not be blank")
        self.dependency = normalized_dependency
        super().__init__(message or f"{normalized_dependency} dependency failed")


class DependencyUnavailableError(DependencyError):
    """A dependency is unavailable or not ready."""


class DependencyTimeoutError(DependencyError):
    """A dependency did not answer within the configured deadline."""


class DependencyContractError(DependencyError):
    """A dependency returned a response that violates its contract."""


class UnauthorizedPrincipalError(RetrievalError):
    """No trusted end-user principal could be established."""

    def __init__(self, message: str = "A trusted user identity is required") -> None:
        super().__init__(message)
