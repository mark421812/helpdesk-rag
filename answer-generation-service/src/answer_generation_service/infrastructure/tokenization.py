"""不需下載模型的保守 Prompt token 估算器。

Provider 回傳的 usage 才是實際計費 token；這個本機估算只用來在呼叫前限制異常大的 Prompt。
"""

from __future__ import annotations

from answer_generation_service.application.dtos import PromptMessage


class ConservativeTokenCounter:
    def count_messages(self, messages: tuple[PromptMessage, ...]) -> int:
        total = 0
        for message in messages:
            total += 4  # Chat message role/separator 的保守固定成本。
            # 一個 token 不會承載不到一個 UTF-8 byte；以 byte 數當 token 上界雖然
            # 會高估中文，但不會像 chars * 0.3 對高熵 ASCII、程式碼或 Base64 低估。
            total += len(message.content.encode("utf-8"))
        return total
