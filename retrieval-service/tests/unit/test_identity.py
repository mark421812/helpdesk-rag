from __future__ import annotations

import pytest

from retrieval_service.application.dtos import IdentityInput
from retrieval_service.domain.exceptions import UnauthorizedPrincipalError
from retrieval_service.infrastructure.identity.development import (
    DevelopmentHeaderIdentityProvider,
)


@pytest.mark.asyncio
async def test_development_identity_requires_user() -> None:
    provider = DevelopmentHeaderIdentityProvider()

    with pytest.raises(UnauthorizedPrincipalError):
        await provider.resolve(IdentityInput())


@pytest.mark.asyncio
async def test_development_identity_rejects_unverified_bearer_token() -> None:
    provider = DevelopmentHeaderIdentityProvider()

    with pytest.raises(UnauthorizedPrincipalError):
        await provider.resolve(
            IdentityInput(
                bearer_token="unverified-token",
                development_user_id="developer",
            )
        )


@pytest.mark.asyncio
async def test_development_identity_normalizes_trusted_headers() -> None:
    provider = DevelopmentHeaderIdentityProvider()

    principal = await provider.resolve(
        IdentityInput(
            development_user_id=" developer ",
            development_group_ids=(" all-employees ", " helpdesk "),
        )
    )

    assert principal.user_id == "developer"
    assert principal.group_ids == ("all-employees", "helpdesk")
