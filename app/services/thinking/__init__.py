"""The language-model seam shared by the Research, Insights and Personalization Agents."""

from __future__ import annotations

from app.services.thinking.claude_cli import ClaudeCliThinker, extract_json_object
from app.services.thinking.contracts import (
    Thinker,
    ThinkingError,
    ThinkingMalformed,
    ThinkingRefused,
    ThinkingRequest,
    ThinkingResult,
    ThinkingTimeout,
    ThinkingUnavailable,
)

__all__ = [
    "ClaudeCliThinker",
    "Thinker",
    "ThinkingError",
    "ThinkingMalformed",
    "ThinkingRefused",
    "ThinkingRequest",
    "ThinkingResult",
    "ThinkingTimeout",
    "ThinkingUnavailable",
    "extract_json_object",
]
