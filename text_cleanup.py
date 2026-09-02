"""Deterministic cleanup applied after LLM extraction."""

from __future__ import annotations

import re

import emoji

# Slack's forwarded-message fallback looks like:
# "[August 28th, 2026 6:56 PM] kuangjinming: 工作周报.xlsx"
_SLACK_FORWARD_PREFIX_RE = re.compile(r"^\[[^\]]+]\s+\S+:\s*", re.UNICODE)
_BARE_FILENAME_RE = re.compile(
    r"(?is)^[\w. \-()（）]+\.(xlsx|xslx|xls|xlsm|md|markdown|zip|csv|docx?)$"
)


def remove_emojis(value: str | None) -> str | None:
    """Remove Unicode emoji while preserving wording, markdown, and line breaks."""
    if value is None:
        return None
    return emoji.replace_emoji(value, replace="").strip()


def is_file_reference_only(
    text: str | None, known_names: tuple[str, ...] = ()
) -> bool:
    """True when `text` is only a Slack file mention, not a real weekly report.

    Covers a bare filename (``工作周报.xlsx``) and the forwarded-message
    fallback ``[August 28th, 2026 6:56 PM] kuangjinming: 工作周报.xlsx``.
    """
    if text is None:
        return True
    stripped = text.strip()
    if not stripped:
        return True

    remainder = _SLACK_FORWARD_PREFIX_RE.sub("", stripped, count=1).strip()
    names: set[str] = set()
    for name in known_names:
        value = (name or "").strip()
        if not value:
            continue
        names.add(value)
        names.add(value.replace("\\", "/").rsplit("/", 1)[-1])

    if remainder in names:
        return True
    if _BARE_FILENAME_RE.match(remainder):
        return True

    leftover = remainder
    for name in sorted(names, key=len, reverse=True):
        leftover = leftover.replace(name, "")
    leftover = leftover.strip(" \t:-")
    return bool(remainder) and not leftover
