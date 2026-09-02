"""Extracts report text, forwarded content, and file links from a Slack message event.

Forwarding a DM into a channel can surface the original content in several
places depending on the Slack client/feature used: plain `text`, legacy
`attachments` (forwarded/shared messages), Block Kit `blocks` (rich_text),
and/or attached `files` (e.g. screenshots). This module normalizes all of
that into a single block of text for the LLM, plus a separate list of file
permalinks for `demo_links`.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from file_ingest import is_supported_report_file

logger = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://\S+")


def is_ignorable_message(event: dict[str, Any]) -> bool:
    """True for messages that should not be treated as a report (bot echoes, edits, etc.)."""
    if event.get("bot_id"):
        return True
    subtype = event.get("subtype")
    if subtype in {"bot_message", "message_changed", "message_deleted", "channel_join"}:
        return True
    return False


def _extract_rich_text_elements(elements: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for element in elements:
        el_type = element.get("type")
        if el_type == "text":
            parts.append(element.get("text", ""))
        elif el_type == "link":
            parts.append(element.get("url", element.get("text", "")))
        elif "elements" in element:
            parts.append(_extract_rich_text_elements(element["elements"]))
    return "".join(parts)


def _extract_blocks_text(blocks: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for block in blocks:
        block_type = block.get("type")
        if block_type == "rich_text":
            text = _extract_rich_text_elements(block.get("elements", []))
            if text.strip():
                lines.append(text.strip())
        elif block_type == "section":
            text_obj = block.get("text", {})
            text = text_obj.get("text", "")
            if text.strip():
                lines.append(text.strip())
    return "\n".join(lines)


def _extract_attachments_text(attachments: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for attachment in attachments:
        header_bits = [
            attachment.get("author_name", ""),
            attachment.get("title", ""),
        ]
        header = " - ".join(bit for bit in header_bits if bit)
        body = attachment.get("text") or attachment.get("fallback") or ""
        chunk = "\n".join(bit for bit in (header, body) if bit)
        if chunk.strip():
            lines.append(chunk.strip())
    return "\n\n".join(lines)


def _file_dedupe_key(file: dict[str, Any]) -> tuple[str, ...]:
    """Prefer Slack file id; fall back to name + download URL for stub objects."""
    file_id = str(file.get("id") or "").strip()
    if file_id:
        return ("id", file_id)
    name = str(file.get("name") or file.get("title") or "")
    url = str(file.get("url_private_download") or file.get("url_private") or "")
    return ("name_url", name, url)


def collect_report_files(
    event: dict[str, Any], *, log: bool = True
) -> list[dict[str, Any]]:
    """Return every file on the message, including nested forwarded attachments.

    Slack puts files from a forwarded/shared message in
    ``attachments[].files``, not the top-level ``event["files"]`` array. Skipping
    that nest is how a workbook upload became a row whose tasks were the
    forwarded fallback string.
    """
    seen: set[tuple[str, ...]] = set()
    collected: list[dict[str, Any]] = []
    n_direct = 0
    n_forwarded = 0

    def _add(file: Any, *, forwarded: bool) -> None:
        nonlocal n_direct, n_forwarded
        if not isinstance(file, dict):
            return
        key = _file_dedupe_key(file)
        if key in seen:
            return
        seen.add(key)
        collected.append(file)
        if forwarded:
            n_forwarded += 1
        else:
            n_direct += 1

    for file in event.get("files") or []:
        _add(file, forwarded=False)

    for attachment in event.get("attachments") or []:
        if not isinstance(attachment, dict):
            continue
        # Flags vary by client (`is_share`, `is_msg_unfurl`, `is_reply_unfurl`),
        # so any attachment that actually carries files is treated as a source.
        for file in attachment.get("files") or []:
            _add(file, forwarded=True)

    if collected and log:
        logger.info(
            "Collected %d file(s): %d on the message, %d nested in forwarded attachments",
            len(collected),
            n_direct,
            n_forwarded,
        )
    return collected


def get_file_links(files: list[dict[str, Any]]) -> list[str]:
    """Return links for non-report attachments such as screenshots/demos."""
    links: list[str] = []
    for file in files:
        if is_supported_report_file(file):
            continue
        link = file.get("permalink") or file.get("url_private")
        if link:
            title = file.get("title") or file.get("name") or ""
            links.append(f"{title}: {link}".strip(": ").strip() if title else link)
    return links


def build_report_text(event: dict[str, Any]) -> str:
    """Combine every text-bearing part of the message into one block for the LLM."""
    sections: list[str] = []

    text = (event.get("text") or "").strip()
    if text:
        sections.append(text)

    blocks_text = _extract_blocks_text(event.get("blocks", []))
    if blocks_text and blocks_text != text:
        sections.append(blocks_text)

    attachments_text = _extract_attachments_text(event.get("attachments", []))
    if attachments_text:
        sections.append(attachments_text)

    files = [
        file
        for file in collect_report_files(event, log=False)
        if not is_supported_report_file(file)
    ]
    if files:
        file_lines = []
        for file in files:
            title = file.get("title") or file.get("name") or "file"
            link = file.get("permalink") or file.get("url_private") or ""
            file_lines.append(f"- {title}: {link}" if link else f"- {title}")
        sections.append("Attached files:\n" + "\n".join(file_lines))

    return "\n\n".join(section for section in sections if section.strip())


def merge_demo_links(llm_value: str | None, file_links: list[str]) -> str | None:
    """Ensure any Slack file permalinks are present in the final demo_links value."""
    existing = (llm_value or "").strip()
    missing = [link for link in file_links if link not in existing]
    if not missing:
        return existing or None
    combined = "\n".join(part for part in [existing, *missing] if part)
    return combined or None


def find_urls(text: str) -> list[str]:
    return _URL_RE.findall(text or "")
