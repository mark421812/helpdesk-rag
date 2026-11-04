"""Domain/Application 可預期失敗；Presentation 會把它們映射成安全 HTTP 錯誤。"""

from __future__ import annotations


class AnswerGenerationError(Exception):
    """服務內所有可預期錯誤的共同父類別。"""


class DomainValidationError(AnswerGenerationError):
    """輸入無法形成合法的 Domain object。"""


class UnauthorizedContextError(AnswerGenerationError):
    """Retrieval attestation 無效、過期或與輸入內容不一致。"""


class OutboundPolicyBlockedError(AnswerGenerationError):
    """本機外送政策阻擋問題或 evidence；不得攜帶命中的原始文字。"""

    def __init__(
        self,
        *,
        categories: tuple[str, ...],
        rule_ids: tuple[str, ...],
        policy_version: str,
    ) -> None:
        super().__init__("Outbound content policy blocked generation")
        self.categories = categories
        self.rule_ids = rule_ids
        self.policy_version = policy_version


class ProviderContractError(AnswerGenerationError):
    """Provider 回覆格式、citation 或完成狀態不符合契約。"""


class ProviderUnavailableError(AnswerGenerationError):
    """Provider 暫時無法使用或認證/設定無效。"""


class ProviderTimeoutError(AnswerGenerationError):
    """Provider 未在總 deadline 內完成。"""


class ProviderRateLimitError(AnswerGenerationError):
    """本地並行限制或 Provider quota/rate limit。"""

    def __init__(self, *, retry_after_seconds: int | None = None) -> None:
        super().__init__("Generation provider is rate limited")
        self.retry_after_seconds = retry_after_seconds
