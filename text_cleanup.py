"""Deterministic cleanup applied after LLM extraction."""

from __future__ import annotations

import emoji


def remove_emojis(value: str | None) -> str | None:
    """Remove Unicode emoji while preserving wording, markdown, and line breaks."""
    if value is None:
        return None
    return emoji.replace_emoji(value, replace="").strip()
