from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from answer_generation_service.infrastructure.config import DEFAULT_POLICY_PATH
from answer_generation_service.infrastructure.outbound_policy.rule_based_policy import (
    RuleBasedOutboundPolicy,
)
from tests.factories import context, question


def _policy(*, path: str = DEFAULT_POLICY_PATH, approved: bool = True) -> RuleBasedOutboundPolicy:
    return RuleBasedOutboundPolicy(policy_path=path, external_llm_approved=approved)


def _enabled_policy_path(tmp_path: Path) -> str:
    """建立不受開發環境全域開關影響的規則測試 policy。"""
    payload = yaml.safe_load(Path(DEFAULT_POLICY_PATH).read_text(encoding="utf-8"))
    payload["enabled"] = True
    enabled_path = tmp_path / "enabled-policy.yaml"
    enabled_path.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    return str(enabled_path)


def test_general_policy_question_is_allowed() -> None:
    decision = _policy().evaluate(question("員工薪資制度如何規定？"), context())

    assert decision.allowed is True


def test_credentials_and_personal_hr_questions_are_blocked(tmp_path: Path) -> None:
    policy = _policy(path=_enabled_policy_path(tmp_path))
    credential = policy.evaluate(question("password=synthetic-secret"), context())
    personal_hr = policy.evaluate(question("王小明的薪資是多少？"), context())

    assert credential.allowed is False
    assert "credential" in credential.categories
    assert personal_hr.allowed is False
    assert "hr_sensitive" in personal_hr.categories


def test_valid_taiwan_id_and_luhn_card_are_blocked(tmp_path: Path) -> None:
    policy = _policy(path=_enabled_policy_path(tmp_path))
    identity = policy.evaluate(question("測試身分證 A123456789"), context())
    payment = policy.evaluate(question("測試卡號 4111-1111-1111-1111"), context())

    assert "personal_data" in identity.categories
    assert "financial" in payment.categories


@pytest.mark.parametrize(
    ("text", "expected_rule"),
    [
        ("contact helpdesk@example.com", "email"),
        ("call 0912-345-678", "taiwan-phone"),
        ("bearer abcdefghijklmnop", "bearer-token"),
        ("postgresql://user:secret@database/helpdesk", "connection-string"),
        ("internal endpoint 192.168.10.20", "private-ip"),
        ("辦公室在台北市測試路 10號", "taiwan-address"),
    ],
)
def test_yaml_declared_regex_rules_are_enforced(
    text: str,
    expected_rule: str,
    tmp_path: Path,
) -> None:
    decision = _policy(path=_enabled_policy_path(tmp_path)).evaluate(question(text), context())

    assert decision.allowed is False
    assert expected_rule in decision.rule_ids


def test_sensitive_evidence_is_blocked_even_when_governance_allowed(tmp_path: Path) -> None:
    decision = _policy(path=_enabled_policy_path(tmp_path)).evaluate(
        question(), context(content="database password=synthetic-secret")
    )

    assert decision.allowed is False
    assert "credential" in decision.categories


def test_combination_rule_cannot_be_split_across_question_and_evidence(tmp_path: Path) -> None:
    decision = _policy(path=_enabled_policy_path(tmp_path)).evaluate(
        question("client"), context(content="contract terms")
    )

    assert decision.allowed is False
    assert decision.rule_ids == ("client-contract",)


def test_invalid_policy_and_missing_approval_fail_closed(tmp_path: Path) -> None:
    invalid_path = tmp_path / "invalid.yaml"
    invalid_path.write_text("not: the expected schema", encoding="utf-8")

    invalid = _policy(path=str(invalid_path)).evaluate(question(), context())
    not_approved = _policy(approved=False).evaluate(question(), context())

    assert invalid.allowed is False
    assert invalid.categories == ("policy_error",)
    assert not_approved.allowed is False
    assert not_approved.categories == ("environment",)


def test_yaml_global_switch_disables_content_rules_only(tmp_path: Path) -> None:
    payload = yaml.safe_load(Path(DEFAULT_POLICY_PATH).read_text(encoding="utf-8"))
    payload["enabled"] = False
    disabled_path = tmp_path / "disabled-policy.yaml"
    disabled_path.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")

    disabled_policy = _policy(path=str(disabled_path))
    sensitive = disabled_policy.evaluate(
        question("password=synthetic-secret"),
        context(content="contact helpdesk@example.com"),
    )
    unapproved = _policy(path=str(disabled_path), approved=False).evaluate(question(), context())

    assert sensitive.allowed is True
    assert sensitive.policy_version == "outbound-policy-v1"
    assert disabled_policy.is_ready() is True
    assert unapproved.allowed is False
    assert unapproved.rule_ids == ("environment_not_approved",)


@pytest.mark.parametrize("enabled", ["false", 0, None])
def test_yaml_switch_must_be_a_real_boolean_and_invalid_config_fails_closed(
    tmp_path: Path,
    enabled: object,
) -> None:
    payload = yaml.safe_load(Path(DEFAULT_POLICY_PATH).read_text(encoding="utf-8"))
    payload["enabled"] = enabled
    invalid_path = tmp_path / "invalid-switch.yaml"
    invalid_path.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")

    decision = _policy(path=str(invalid_path)).evaluate(question(), context())

    assert decision.allowed is False
    assert decision.rule_ids == ("policy_configuration_invalid",)
    assert decision.categories == ("policy_error",)


def test_unknown_rule_validator_fails_closed(tmp_path: Path) -> None:
    payload = yaml.safe_load(Path(DEFAULT_POLICY_PATH).read_text(encoding="utf-8"))
    payload["rules"][0]["validator"] = "execute-arbitrary-code"
    invalid_path = tmp_path / "unknown-validator.yaml"
    invalid_path.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")

    decision = _policy(path=str(invalid_path)).evaluate(question(), context())

    assert decision.allowed is False
    assert decision.rule_ids == ("policy_configuration_invalid",)
