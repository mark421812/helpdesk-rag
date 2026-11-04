"""Domain 使用的有限狀態值，避免散落容易拼錯的字串。"""

from enum import StrEnum


class Confidentiality(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


class AnswerStatus(StrEnum):
    ANSWERED = "answered"
    UNABLE_TO_ANSWER = "unable_to_answer"
