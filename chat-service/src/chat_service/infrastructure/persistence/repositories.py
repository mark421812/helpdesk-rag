"""Chat PostgreSQL repository adapters 的穩定匯入入口。

實作依 aggregate 分檔，讓 conversation counters、turn/message state 與 admin purge 的
transaction 規則可以各自審查；composition root 則只需從本 module 匯入三個 concrete
adapter，並把同一個 ``AsyncSession`` 交給它們。
"""

from chat_service.infrastructure.persistence.conversation_repository import (
    SqlAlchemyConversationRepository,
)
from chat_service.infrastructure.persistence.purge_repository import (
    SqlAlchemyPurgeRepository,
)
from chat_service.infrastructure.persistence.turn_repository import (
    SqlAlchemyTurnRepository,
)

__all__ = [
    "SqlAlchemyConversationRepository",
    "SqlAlchemyPurgeRepository",
    "SqlAlchemyTurnRepository",
]
