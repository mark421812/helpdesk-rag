"""Composition Root：依設定選擇 Provider 並組裝所有 Ports 與 Use Case。"""

from __future__ import annotations

from dataclasses import dataclass

from answer_generation_service.application.dtos import GenerationPolicy
from answer_generation_service.application.ports import (
    ContextAttestationVerifierPort,
    LLMProviderPort,
    OutboundPolicyPort,
)
from answer_generation_service.application.prompt_builder import GroundedPromptBuilder
from answer_generation_service.application.use_cases import GenerateGroundedAnswer
from answer_generation_service.infrastructure.attestation.ed25519_verifier import (
    Ed25519ContextAttestationVerifier,
)
from answer_generation_service.infrastructure.config import Settings
from answer_generation_service.infrastructure.llm.fake import FakeLLMProvider
from answer_generation_service.infrastructure.llm.groq_hosted import GroqHostedProvider
from answer_generation_service.infrastructure.llm.nvidia_hosted import NvidiaHostedNimProvider
from answer_generation_service.infrastructure.outbound_policy.rule_based_policy import (
    RuleBasedOutboundPolicy,
)
from answer_generation_service.infrastructure.tokenization import ConservativeTokenCounter


@dataclass(slots=True)
class Runtime:
    provider: LLMProviderPort
    attestation_verifier: ContextAttestationVerifierPort
    outbound_policy: OutboundPolicyPort
    generate_answer: GenerateGroundedAnswer

    def readiness_checks(self) -> dict[str, bool]:
        return {
            "provider_configured": self.provider.is_ready(),
            "attestation_keys": self.attestation_verifier.is_ready(),
            "outbound_policy": self.outbound_policy.is_ready(),
        }

    async def close(self) -> None:
        await self.provider.close()


def build_runtime(settings: Settings) -> Runtime:
    verifier = Ed25519ContextAttestationVerifier(
        public_key_paths=settings.attestation_public_key_paths,
        issuer=settings.attestation_issuer,
        audience=settings.attestation_audience,
        context_schema_version=settings.attestation_context_schema_version,
        accepted_policy_versions=tuple(settings.attestation_accepted_policy_versions),
        active_prompt_version=settings.prompt_version,
        policy_prompt_bindings={
            policy: tuple(prompts)
            for policy, prompts in settings.attestation_policy_prompt_bindings.items()
        },
        clock_skew_seconds=settings.attestation_clock_skew_seconds,
        max_token_lifetime_seconds=settings.attestation_max_lifetime_seconds,
    )
    outbound_policy = RuleBasedOutboundPolicy(
        policy_path=settings.outbound_policy_path,
        # Fake provider 完全離線，不屬於資料外送；外部 provider 則維持 fail closed。
        external_llm_approved=(settings.llm_provider == "fake" or settings.external_llm_approved),
    )
    provider: LLMProviderPort
    if settings.llm_provider == "fake":
        provider = FakeLLMProvider()
    elif settings.llm_provider == "nvidia_hosted":
        provider = NvidiaHostedNimProvider(
            base_url=settings.nvidia_base_url,
            api_key=settings.nvidia_api_key or "",
            model=settings.llm_model,
            connect_timeout_seconds=settings.llm_connect_timeout_seconds,
            read_timeout_seconds=settings.llm_read_timeout_seconds,
            total_timeout_seconds=settings.llm_total_timeout_seconds,
            max_response_bytes=settings.llm_max_response_bytes,
            total_attempts=settings.llm_total_attempts,
            retry_backoff_seconds=settings.llm_retry_backoff_seconds,
            retry_max_backoff_seconds=settings.llm_retry_max_backoff_seconds,
            max_concurrency=settings.max_generation_concurrency,
            max_queue_size=settings.max_generation_queue_size,
            queue_wait_timeout_seconds=settings.llm_queue_wait_timeout_seconds,
        )
    else:
        provider = GroqHostedProvider(
            base_url=settings.groq_base_url,
            api_key=settings.groq_api_key or "",
            model=settings.llm_model,
            connect_timeout_seconds=settings.llm_connect_timeout_seconds,
            read_timeout_seconds=settings.llm_read_timeout_seconds,
            total_timeout_seconds=settings.llm_total_timeout_seconds,
            max_response_bytes=settings.llm_max_response_bytes,
            total_attempts=settings.llm_total_attempts,
            retry_backoff_seconds=settings.llm_retry_backoff_seconds,
            retry_max_backoff_seconds=settings.llm_retry_max_backoff_seconds,
            max_concurrency=settings.max_generation_concurrency,
            max_queue_size=settings.max_generation_queue_size,
            queue_wait_timeout_seconds=settings.llm_queue_wait_timeout_seconds,
        )
    generate_answer = GenerateGroundedAnswer(
        attestation_verifier=verifier,
        outbound_policy=outbound_policy,
        token_counter=ConservativeTokenCounter(),
        prompt_builder=GroundedPromptBuilder(prompt_version=settings.prompt_version),
        provider=provider,
        generation_policy=GenerationPolicy(
            max_context_tokens=settings.max_context_tokens,
            max_prompt_tokens=settings.max_prompt_tokens,
            max_output_tokens=settings.llm_max_output_tokens,
            max_answer_characters=settings.max_answer_characters,
            temperature=settings.generation_temperature,
            prompt_version=settings.prompt_version,
        ),
    )
    return Runtime(
        provider=provider,
        attestation_verifier=verifier,
        outbound_policy=outbound_policy,
        generate_answer=generate_answer,
    )
