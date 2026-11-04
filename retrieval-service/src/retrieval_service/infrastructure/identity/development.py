"""僅供本機開發使用的 header identity adapter。

正式環境不能信任 Client 自己填的 user/group headers，所以 Settings 會禁止 production
使用此 adapter；未來應以 Entra JWT adapter 取代。
"""

from __future__ import annotations

from retrieval_service.application.dtos import IdentityInput
from retrieval_service.application.ports import IdentityProviderPort
from retrieval_service.domain.entities import PrincipalContext
from retrieval_service.domain.exceptions import UnauthorizedPrincipalError


class DevelopmentHeaderIdentityProvider(IdentityProviderPort):
    """把 development headers 驗證後轉成可信 PrincipalContext。"""

    def __init__(self, *, max_groups: int = 100, max_identifier_length: int = 512) -> None:
        self._max_groups = max_groups
        self._max_identifier_length = max_identifier_length

    async def resolve(self, identity: IdentityInput) -> PrincipalContext:
        """驗證 header 數量、長度與重複值；失敗時不建立 principal。"""

        if identity.bearer_token is not None:
            raise UnauthorizedPrincipalError(
                "Bearer tokens are not accepted by the development identity adapter"
            )
        if identity.development_user_id is None:
            raise UnauthorizedPrincipalError("The x-dev-user-id header is required")

        user_id = self._validated_identifier(identity.development_user_id, "user ID")
        if len(identity.development_group_ids) > self._max_groups:
            raise UnauthorizedPrincipalError(
                f"At most {self._max_groups} development groups are allowed"
            )
        groups = tuple(
            self._validated_identifier(group_id, "group ID")
            for group_id in identity.development_group_ids
        )
        if len(set(groups)) != len(groups):
            raise UnauthorizedPrincipalError("Development group IDs must be unique")
        return PrincipalContext(user_id=user_id, group_ids=groups)

    def _validated_identifier(self, value: str, label: str) -> str:
        """正規化單一 user/group identifier。"""

        normalized = value.strip()
        if not normalized:
            raise UnauthorizedPrincipalError(f"Development {label} must not be blank")
        if len(normalized) > self._max_identifier_length:
            raise UnauthorizedPrincipalError(
                f"Development {label} exceeds {self._max_identifier_length} characters"
            )
        return normalized
