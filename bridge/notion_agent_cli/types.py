"""Response dataclasses.

Kept in a separate module so ``account``, ``provider``, and CLI code
can import them without pulling each other in.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_creation: int = 0


@dataclass(slots=True)
class ChatResponse:
    text: str
    model: str
    thread_id: str
    usage: TokenUsage
    thinking: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
