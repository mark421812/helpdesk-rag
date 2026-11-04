"""由 YAML 宣告的 CPU-only outbound 敏感資料掃描器。"""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

import yaml

from answer_generation_service.application.dtos import OutboundPolicyDecision
from answer_generation_service.domain.entities import ApprovedContext, Question

ZERO_WIDTH: Final[re.Pattern[str]] = re.compile(r"[\u200b-\u200f\u2060\ufeff]")
SUPPORTED_RULE_TYPES: Final[frozenset[str]] = frozenset(
    {"regex", "all_regex", "contains", "all_contains"}
)
SUPPORTED_VALIDATORS: Final[frozenset[str]] = frozenset(
    {"taiwan_id_checksum", "luhn_checksum", "private_ip"}
)
MAX_RULES: Final[int] = 500
MAX_PATTERN_LENGTH: Final[int] = 4_096
MAX_LITERAL_LENGTH: Final[int] = 4_096

RuleType = Literal["regex", "all_regex", "contains", "all_contains"]


@dataclass(frozen=True, slots=True)
class ConfiguredRule:
    """一條已通過 schema 驗證並完成 regex 編譯的 YAML 規則。"""

    rule_id: str
    category: str
    rule_type: RuleType
    patterns: tuple[re.Pattern[str], ...] = ()
    values: tuple[str, ...] = ()
    validator: str | None = None

    def matches(self, normalized: str) -> bool:
        """以規則指定的通用 matcher 判斷文字，不在 Python 決定敏感資料種類。"""

        if self.rule_type == "regex":
            pattern = self.patterns[0]
            if self.validator is None:
                return pattern.search(normalized) is not None
            return any(
                _validator_matches(self.validator, match.group())
                for match in pattern.finditer(normalized)
            )
        if self.rule_type == "all_regex":
            return all(pattern.search(normalized) is not None for pattern in self.patterns)
        if self.rule_type == "contains":
            return self.values[0] in normalized
        if self.rule_type == "all_contains":
            return all(value in normalized for value in self.values)
        raise RuntimeError("unsupported configured outbound policy rule type")


class RuleBasedOutboundPolicy:
    """掃描實際會送給 Provider 的所有動態文字，不記錄命中原文。"""

    def __init__(self, *, policy_path: str, external_llm_approved: bool) -> None:
        self._policy_path = Path(policy_path)
        self._external_llm_approved = external_llm_approved
        self._version = "unavailable"
        self._enabled = True
        self._rules: tuple[ConfiguredRule, ...] = ()
        self._configuration_valid = self._load_policy()

    def evaluate(
        self,
        question: Question,
        context: ApprovedContext,
    ) -> OutboundPolicyDecision:
        # YAML 的 scanner 開關不等於外部 Provider 核准；兩者必須分開治理。
        if not self._external_llm_approved:
            return self._blocked("environment_not_approved", "environment")
        if not self._configuration_valid:
            return self._blocked("policy_configuration_invalid", "policy_error")
        if not self._enabled:
            return OutboundPolicyDecision(allowed=True, policy_version=self._version)
        try:
            # 必須掃描完整 outbound payload，避免把組合規則拆散到 question/evidence 來繞過。
            outbound_payload = "\n".join(self._outbound_text(question, context))
            violations = self._scan(outbound_payload)
        except Exception:
            # Scanner 本身失敗也不能當成安全；例外內容不得帶入 public response 或 log。
            return self._blocked("scanner_failure", "policy_error")
        if not violations:
            return OutboundPolicyDecision(allowed=True, policy_version=self._version)
        ordered = sorted(violations)
        return OutboundPolicyDecision(
            allowed=False,
            policy_version=self._version,
            rule_ids=tuple(rule_id for rule_id, _ in ordered),
            categories=tuple(sorted({category for _, category in ordered})),
        )

    def is_ready(self) -> bool:
        """停用 scanner 是有效設定，但無效 YAML 或未核准 Provider 仍 fail closed。"""

        return self._configuration_valid and self._external_llm_approved

    def _load_policy(self) -> bool:
        try:
            payload: Any = yaml.safe_load(self._policy_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or set(payload) != {"version", "enabled", "rules"}:
                return False
            version = _required_text(payload["version"], field="version")
            enabled = payload["enabled"]
            raw_rules = payload["rules"]
            if not isinstance(enabled, bool):
                return False
            if not isinstance(raw_rules, list) or not 1 <= len(raw_rules) <= MAX_RULES:
                return False
            rules = tuple(_parse_rule(item) for item in raw_rules)
            rule_ids = tuple(rule.rule_id for rule in rules)
            if len(set(rule_ids)) != len(rule_ids):
                return False
        except (OSError, UnicodeError, yaml.YAMLError, TypeError, ValueError, KeyError, re.error):
            return False
        self._version = version
        self._enabled = enabled
        self._rules = rules
        return True

    def _scan(self, original: str) -> set[tuple[str, str]]:
        normalized = _normalize(original)
        return {(rule.rule_id, rule.category) for rule in self._rules if rule.matches(normalized)}

    @staticmethod
    def _outbound_text(question: Question, context: ApprovedContext) -> tuple[str, ...]:
        values: list[str] = [question.text]
        if question.language is not None:
            values.append(question.language)
        for item in context.evidence:
            values.extend((item.content, item.document_type, item.citation.title))
            if item.citation.section_path is not None:
                values.append(item.citation.section_path)
        return tuple(values)

    def _blocked(self, rule_id: str, category: str) -> OutboundPolicyDecision:
        return OutboundPolicyDecision(
            allowed=False,
            policy_version=self._version,
            rule_ids=(rule_id,),
            categories=(category,),
        )


def _parse_rule(value: Any) -> ConfiguredRule:
    if not isinstance(value, dict):
        raise ValueError("policy rule must be a mapping")
    rule_type = value.get("type")
    if not isinstance(rule_type, str) or rule_type not in SUPPORTED_RULE_TYPES:
        raise ValueError("policy rule type is unsupported")
    rule_id = _required_text(value.get("id"), field="rule id")
    category = _required_text(value.get("category"), field="rule category")

    if rule_type == "regex":
        allowed_keys = {"id", "category", "type", "pattern", "ignore_case", "validator"}
        required_keys = {"id", "category", "type", "pattern"}
        if not set(value).issubset(allowed_keys) or not required_keys.issubset(value):
            raise ValueError("regex rule has invalid fields")
        pattern = _compile_pattern(value["pattern"], value.get("ignore_case", False))
        validator = value.get("validator")
        if validator is not None and validator not in SUPPORTED_VALIDATORS:
            raise ValueError("regex rule validator is unsupported")
        return ConfiguredRule(
            rule_id=rule_id,
            category=category,
            rule_type="regex",
            patterns=(pattern,),
            validator=validator,
        )

    if rule_type == "all_regex":
        if set(value) != {"id", "category", "type", "patterns", "ignore_case"}:
            raise ValueError("all_regex rule has invalid fields")
        raw_patterns = value["patterns"]
        if not isinstance(raw_patterns, list) or len(raw_patterns) < 2:
            raise ValueError("all_regex requires at least two patterns")
        patterns = tuple(
            _compile_pattern(pattern, value["ignore_case"]) for pattern in raw_patterns
        )
        return ConfiguredRule(
            rule_id=rule_id,
            category=category,
            rule_type="all_regex",
            patterns=patterns,
        )

    if rule_type == "contains":
        if set(value) != {"id", "category", "type", "value"}:
            raise ValueError("contains rule has invalid fields")
        literal = _required_literal(value["value"])
        return ConfiguredRule(
            rule_id=rule_id,
            category=category,
            rule_type="contains",
            values=(_normalize(literal),),
        )

    if set(value) != {"id", "category", "type", "values"}:
        raise ValueError("all_contains rule has invalid fields")
    raw_values = value["values"]
    if not isinstance(raw_values, list) or len(raw_values) < 2:
        raise ValueError("all_contains requires at least two values")
    literals = tuple(_normalize(_required_literal(item)) for item in raw_values)
    return ConfiguredRule(
        rule_id=rule_id,
        category=category,
        rule_type="all_contains",
        values=literals,
    )


def _compile_pattern(value: Any, ignore_case: Any) -> re.Pattern[str]:
    pattern = _required_text(value, field="regex pattern")
    if len(pattern) > MAX_PATTERN_LENGTH:
        raise ValueError("regex pattern is too long")
    if not isinstance(ignore_case, bool):
        raise ValueError("ignore_case must be boolean")
    return re.compile(pattern, re.IGNORECASE if ignore_case else 0)


def _required_text(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-blank text")
    return value.strip()


def _required_literal(value: Any) -> str:
    literal = _required_text(value, field="literal value")
    if len(literal) > MAX_LITERAL_LENGTH:
        raise ValueError("literal value is too long")
    return literal


def _validator_matches(validator: str, value: str) -> bool:
    if validator == "taiwan_id_checksum":
        return _valid_taiwan_id(value)
    if validator == "luhn_checksum":
        return _valid_luhn(value)
    if validator == "private_ip":
        try:
            return ipaddress.ip_address(value).is_private
        except ValueError:
            return False
    raise RuntimeError("unsupported configured outbound policy validator")


def _normalize(value: str) -> str:
    return ZERO_WIDTH.sub("", unicodedata.normalize("NFKC", value)).casefold()


TAIWAN_ID_LETTER_CODES: Final[dict[str, int]] = {
    letter: code
    for letter, code in zip(
        "ABCDEFGHJKLMNPQRSTUVXYWZIO",
        range(10, 36),
        strict=True,
    )
}


def _valid_luhn(value: str) -> bool:
    digits = [int(character) for character in value if character.isdigit()]
    if not 13 <= len(digits) <= 19 or len(set(digits)) == 1:
        return False
    checksum = 0
    parity = len(digits) % 2
    for index, digit in enumerate(digits):
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit
    return checksum % 10 == 0


def _valid_taiwan_id(value: str) -> bool:
    normalized = value.upper()
    if len(normalized) != 10 or normalized[0] not in TAIWAN_ID_LETTER_CODES:
        return False
    code = TAIWAN_ID_LETTER_CODES[normalized[0]]
    digits = [code // 10, code % 10, *(int(character) for character in normalized[1:])]
    weights = (1, 9, 8, 7, 6, 5, 4, 3, 2, 1, 1)
    return sum(digit * weight for digit, weight in zip(digits, weights, strict=True)) % 10 == 0
