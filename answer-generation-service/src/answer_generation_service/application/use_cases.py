"""驗簽、外送政策、Prompt、Provider 與引用驗證的核心 use case。"""

from __future__ import annotations

import json
from typing import Any

from answer_generation_service.application.dtos import (
    GenerateAnswerCommand,
    GenerateAnswerResult,
    GeneratedCitation,
    GenerationDiagnostics,
    GenerationPolicy,
    ProviderGenerationRequest,
)
from answer_generation_service.application.ports import (
    ContextAttestationVerifierPort,
    GenerationTokenCounterPort,
    LLMProviderPort,
    OutboundPolicyPort,
)
from answer_generation_service.application.prompt_builder import GroundedPromptBuilder
from answer_generation_service.domain.entities import AnswerPart, GroundedAnswer
from answer_generation_service.domain.enums import AnswerStatus
from answer_generation_service.domain.exceptions import (
    DomainValidationError,
    OutboundPolicyBlockedError,
    ProviderContractError,
)


class GenerateGroundedAnswer:
    """只有所有本機驗證通過後，才把最小必要內容送到外部 Provider。"""

    def __init__(
        self,
        *,
        attestation_verifier: ContextAttestationVerifierPort,
        outbound_policy: OutboundPolicyPort,
        token_counter: GenerationTokenCounterPort,
        prompt_builder: GroundedPromptBuilder,
        provider: LLMProviderPort,
        generation_policy: GenerationPolicy,
    ) -> None:
        if prompt_builder.prompt_version != generation_policy.prompt_version:
            raise ValueError("Prompt builder version must match the generation policy version")
        self._attestation_verifier = attestation_verifier
        self._outbound_policy = outbound_policy
        self._token_counter = token_counter
        self._prompt_builder = prompt_builder
        self._provider = provider
        self._generation_policy = generation_policy

    async def execute(self, command: GenerateAnswerCommand) -> GenerateAnswerResult:
        # 驗簽與外送政策必須在 Prompt 建立和任何外部 I/O 之前完成。
        self._attestation_verifier.verify(
            command.question,
            command.context,
            command.context_attestation,
        )
        decision = self._outbound_policy.evaluate(command.question, command.context)
        if not decision.allowed:
            raise OutboundPolicyBlockedError(
                categories=decision.categories,
                rule_ids=decision.rule_ids,
                policy_version=decision.policy_version,
            )

        context_tokens = sum(item.token_count for item in command.context.evidence)
        if context_tokens > self._generation_policy.max_context_tokens:
            raise DomainValidationError("evidence exceeds the configured context token budget")

        messages = self._prompt_builder.build(command.question, command.context)
        if self._token_counter.count_messages(messages) > self._generation_policy.max_prompt_tokens:
            raise DomainValidationError("generation prompt exceeds the configured token budget")

        provider_result = await self._provider.generate(
            ProviderGenerationRequest(
                messages=messages,
                max_output_tokens=self._generation_policy.max_output_tokens,
                temperature=self._generation_policy.temperature,
            )
        )
        if provider_result.finish_reason != "stop":
            raise ProviderContractError("Provider did not return a complete grounded answer")

        answer = self._parse_answer(provider_result.content, len(command.context.evidence))
        used_ids = {citation_id for part in answer.parts for citation_id in part.citation_ids}
        citations = tuple(
            GeneratedCitation(
                citation_id=f"C{index}",
                chunk_id=item.chunk_id,
                document_id=item.document_id,
                version_id=item.version_id,
                title=item.citation.title,
                section_path=item.citation.section_path,
                page_number=item.citation.page_number,
            )
            for index, item in enumerate(command.context.evidence, start=1)
            if f"C{index}" in used_ids
        )
        return GenerateAnswerResult(
            answer=answer,
            citations=citations,
            diagnostics=GenerationDiagnostics(
                provider=provider_result.provider,
                model=provider_result.model,
                prompt_version=self._generation_policy.prompt_version,
                finish_reason=provider_result.finish_reason,
                prompt_tokens=provider_result.prompt_tokens,
                completion_tokens=provider_result.completion_tokens,
            ),
        )

    def _parse_answer(self, raw_content: str, evidence_count: int) -> GroundedAnswer:
        if len(raw_content) > self._generation_policy.max_answer_characters:
            raise ProviderContractError("Provider answer exceeded the response contract")
        try:
            payload: Any = json.loads(raw_content)
        except json.JSONDecodeError as exc:
            raise ProviderContractError("Provider returned invalid structured output") from exc
        if not isinstance(payload, dict) or set(payload) != {"status", "answer_parts"}:
            raise ProviderContractError("Provider returned an unexpected answer schema")

        raw_status = payload["status"]
        if raw_status not in {status.value for status in AnswerStatus}:
            raise ProviderContractError("Provider returned an unknown answer status")
        raw_parts = payload["answer_parts"]
        if not isinstance(raw_parts, list):
            raise ProviderContractError("Provider answer_parts must be an array")

        known_citations = {f"C{index}" for index in range(1, evidence_count + 1)}
        parts: list[AnswerPart] = []
        for raw_part in raw_parts:
            if not isinstance(raw_part, dict) or set(raw_part) != {"text", "citation_ids"}:
                raise ProviderContractError("Provider returned an unexpected answer part schema")
            text = raw_part["text"]
            raw_citations = raw_part["citation_ids"]
            if not isinstance(text, str) or not isinstance(raw_citations, list):
                raise ProviderContractError("Provider answer part has invalid field types")
            if not all(isinstance(item, str) for item in raw_citations):
                raise ProviderContractError("Provider citation IDs must be strings")
            citation_ids = tuple(raw_citations)
            if any(item not in known_citations for item in citation_ids):
                raise ProviderContractError("Provider referenced an unknown citation ID")
            try:
                parts.append(AnswerPart(text=text, citation_ids=citation_ids))
            except DomainValidationError as exc:
                raise ProviderContractError("Provider returned an invalid answer part") from exc

        try:
            return GroundedAnswer(status=AnswerStatus(raw_status), parts=tuple(parts))
        except DomainValidationError as exc:
            raise ProviderContractError("Provider answer violates grounded answer rules") from exc
