"""Phase 5 orchestration 的 failure 與秘密資料隔離測試。"""

from __future__ import annotations

import pytest

from chat_service.domain.exceptions import DependencyError
from tests.unit.test_use_cases import make_command, make_submitter


@pytest.mark.asyncio
async def test_untrusted_dependency_code_and_retry_after_are_not_persisted() -> None:
    submitter, _store, _retrieval, answer, _gate = make_submitter()
    answer.error = DependencyError(
        "provider-secret\nraw-body",
        "provider raw body containing an api key",
        retry_after_seconds=10**10,
    )

    result = await submitter.execute(make_command())

    assert result.error is not None
    assert result.error.code == "answer_service_error"
    assert result.error.retry_after_seconds is None
    assert "provider-secret" not in repr(result)
    assert "provider raw body" not in repr(result)
