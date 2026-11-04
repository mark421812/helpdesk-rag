"""Phase 6 identity and public projection security tests。"""

from __future__ import annotations

import pytest

from chat_service.application.dtos import IdentityInput
from chat_service.domain.exceptions import UnauthorizedPrincipalError
from chat_service.infrastructure.identity.development import DevelopmentHeaderIdentityProvider


@pytest.mark.asyncio
async def test_development_identity_requires_user_and_rejects_bearer() -> None:
    provider = DevelopmentHeaderIdentityProvider()
    with pytest.raises(UnauthorizedPrincipalError):
        await provider.resolve(IdentityInput())
    with pytest.raises(UnauthorizedPrincipalError):
        await provider.resolve(IdentityInput(bearer_token="token"))


@pytest.mark.asyncio
async def test_identity_bounds_duplicates_and_control_characters() -> None:
    provider = DevelopmentHeaderIdentityProvider()
    with pytest.raises(UnauthorizedPrincipalError):
        await provider.resolve(
            IdentityInput(development_user_id="alice", development_roles=("admin", "admin"))
        )
    with pytest.raises(UnauthorizedPrincipalError):
        await provider.resolve(IdentityInput(development_user_id="alice\nforged"))
    with pytest.raises(UnauthorizedPrincipalError):
        await provider.resolve(IdentityInput(development_user_id="alice" + chr(0x80)))
    with pytest.raises(UnauthorizedPrincipalError):
        await provider.resolve(
            IdentityInput(
                development_user_id="alice", development_group_ids=tuple(str(i) for i in range(101))
            )
        )


@pytest.mark.asyncio
async def test_identity_trims_safe_values_and_keeps_admin_role() -> None:
    principal = await DevelopmentHeaderIdentityProvider().resolve(
        IdentityInput(
            development_user_id=" alice ",
            development_group_ids=(" all-employees ",),
            development_roles=(" helpdesk-admin ",),
        )
    )
    assert principal.subject == "alice"
    assert principal.group_ids == ("all-employees",)
    assert principal.roles == ("helpdesk-admin",)
