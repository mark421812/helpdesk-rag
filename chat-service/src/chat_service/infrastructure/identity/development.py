"""Chat Service 的 development-header identity adapter。

這個 adapter 只把已由 Presentation 讀取的 header 值轉成受驗證的 Domain ``Principal``。
它不驗證 JWT，也不把 header 本身當成 production 身分；``APP_ENV=production`` 由設定與
composition root fail closed，因此不會意外以可偽造的 development headers 啟動。
"""

from __future__ import annotations

from chat_service.application.dtos import IdentityInput
from chat_service.application.ports import IdentityProviderPort
from chat_service.domain.entities import Principal
from chat_service.domain.exceptions import UnauthorizedPrincipalError


class DevelopmentHeaderIdentityProvider(IdentityProviderPort):
    """將 development headers 驗證成固定 issuer/subject 的 Principal。"""

    def __init__(
        self,
        *,
        issuer: str = "urn:helpdesk:development",
        max_user_id_length: int = 512,
        max_group_id_length: int = 512,
        max_role_length: int = 128,
        max_groups: int = 100,
        max_roles: int = 20,
    ) -> None:
        self._issuer = issuer
        self._max_user_id_length = max_user_id_length
        self._max_group_id_length = max_group_id_length
        self._max_role_length = max_role_length
        self._max_groups = max_groups
        self._max_roles = max_roles

    async def resolve(self, identity: IdentityInput) -> Principal:
        """拒絕 Bearer token，並驗證 user/group/role 的長度、空白與控制字元。"""

        if identity.bearer_token is not None:
            raise UnauthorizedPrincipalError(
                "Bearer tokens are not accepted by the development identity adapter"
            )
        if identity.development_user_id is None:
            raise UnauthorizedPrincipalError("The x-dev-user-id header is required")
        user_id = self._validated(identity.development_user_id, "user ID", self._max_user_id_length)
        if len(identity.development_group_ids) > self._max_groups:
            raise UnauthorizedPrincipalError(
                f"At most {self._max_groups} development groups are allowed"
            )
        if len(identity.development_roles) > self._max_roles:
            raise UnauthorizedPrincipalError(
                f"At most {self._max_roles} development roles are allowed"
            )
        groups = tuple(
            self._validated(value, "group ID", self._max_group_id_length)
            for value in identity.development_group_ids
        )
        roles = tuple(
            self._validated(value, "role", self._max_role_length)
            for value in identity.development_roles
        )
        if len(set(groups)) != len(groups):
            raise UnauthorizedPrincipalError("Development group IDs must be unique")
        if len(set(roles)) != len(roles):
            raise UnauthorizedPrincipalError("Development roles must be unique")
        return Principal(
            issuer=self._issuer,
            subject=user_id,
            tenant_id=None,
            group_ids=groups,
            roles=roles,
        )

    @staticmethod
    def _validated(value: str, label: str, maximum: int) -> str:
        """清理單一 header identifier；控制字元會被拒絕以避免 header injection。"""

        if not isinstance(value, str):
            raise UnauthorizedPrincipalError(f"Development {label} must be text")
        normalized = value.strip()
        if not normalized:
            raise UnauthorizedPrincipalError(f"Development {label} must not be blank")
        if len(normalized) > maximum:
            raise UnauthorizedPrincipalError(f"Development {label} exceeds {maximum} characters")
        if any(ord(character) < 0x20 or 0x7F <= ord(character) <= 0x9F for character in normalized):
            raise UnauthorizedPrincipalError(f"Development {label} contains a control character")
        return normalized


class FailClosedIdentityProvider(IdentityProviderPort):
    """保留 production identity port，但在尚未實作 Entra JWT 前永遠拒絕。"""

    async def resolve(self, identity: IdentityInput) -> Principal:
        """不解析任何 token，避免用假 parser 讓 production 進入服務。"""

        del identity
        raise UnauthorizedPrincipalError("The configured identity adapter is not implemented")
