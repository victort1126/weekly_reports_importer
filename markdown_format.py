"""Converts lightweight Markdown/Slack-mrkdwn syntax into plain text plus
Google Sheets `textFormatRuns`, so e.g. `*bold*` renders as actual bold text
in the sheet instead of literal asterisks.

The LLM extraction step is instructed to preserve the report's original
formatting verbatim (including markdown markers), so the conversion from
markup -> rich text happens here, deterministically, right before writing to
the sheet.

Supported syntax:
- `**bold**` or `*bold*` (both common markdown and Slack mrkdwn bold)
- `_italic_`
- `~~strikethrough~~` or `~strikethrough~`
- `` `inline code` `` (rendered in a monospace font)
- `# Heading` ... `###### Heading` (rendered as a bold line; leading `#`s removed)
- `- item` / `* item` / `+ item` bullet lines (rendered as a "- " prefixed line)
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_HEADER_RE = re.compile(r"^(?:\s{0,3})#{1,6}\s+(?P<text>.*)$")
_BULLET_RE = re.compile(r"^(?P<indent>\s*)[-*+]\s+(?P<text>.*)$")

_INLINE_PATTERN = re.compile(
    r"(?P<bold2>\*\*(?P<bold2_text>[^*\n]+?)\*\*)"
    r"|(?P<bold1>\*(?P<bold1_text>[^*\n]+?)\*)"
    r"|(?P<italic>_(?P<italic_text>[^_\n]+?)_)"
    r"|(?P<strike2>~~(?P<strike2_text>[^~\n]+?)~~)"
    r"|(?P<strike1>~(?P<strike1_text>[^~\n]+?)~)"
    r"|(?P<code>`(?P<code_text>[^`\n]+?)`)"
)

_CODE_FONT = "Courier New"


@dataclass
class FormatSpan:
    """A character range (in the final plain text) that carries some styling."""

    start: int
    end: int
    bold: bool = False
    italic: bool = False
    strikethrough: bool = False
    code: bool = False

    def as_tuple(self) -> tuple[bool, bool, bool, bool]:
        return (self.bold, self.italic, self.strikethrough, self.code)


def _parse_inline(content: str, base_offset: int) -> tuple[str, list[FormatSpan]]:
    """Strip inline markdown markers from `content`, recording their spans.

    `base_offset` is where `content` starts within the overall output text,
    so returned span offsets are already in the caller's coordinate space.
    """
    out_parts: list[str] = []
    spans: list[FormatSpan] = []
    pos = 0
    out_len = 0

    for match in _INLINE_PATTERN.finditer(content):
        literal = content[pos : match.start()]
        out_parts.append(literal)
        out_len += len(literal)

        if match.group("bold2") is not None:
            inner = match.group("bold2_text")
            style = dict(bold=True)
        elif match.group("bold1") is not None:
            inner = match.group("bold1_text")
            style = dict(bold=True)
        elif match.group("italic") is not None:
            inner = match.group("italic_text")
            style = dict(italic=True)
        elif match.group("strike2") is not None:
            inner = match.group("strike2_text")
            style = dict(strikethrough=True)
        elif match.group("strike1") is not None:
            inner = match.group("strike1_text")
            style = dict(strikethrough=True)
        else:
            inner = match.group("code_text")
            style = dict(code=True)

        start = base_offset + out_len
        out_parts.append(inner)
        out_len += len(inner)
        end = base_offset + out_len

        spans.append(FormatSpan(start=start, end=end, **style))
        pos = match.end()

    tail = content[pos:]
    out_parts.append(tail)
    out_len += len(tail)

    return "".join(out_parts), spans


def convert_markdown(text: str | None) -> tuple[str, list[FormatSpan]]:
    """Convert markdown/mrkdwn `text` into (plain_text, format_spans).

    `plain_text` has all markup markers removed; `format_spans` describes
    which character ranges of `plain_text` should be bold/italic/etc.
    """
    if not text:
        return text or "", []

    lines = text.split("\n")
    out_lines: list[str] = []
    spans: list[FormatSpan] = []
    offset = 0

    for line in lines:
        header_match = _HEADER_RE.match(line)
        bullet_match = None if header_match else _BULLET_RE.match(line)

        prefix = ""
        content = line
        force_bold_line = False

        if header_match:
            content = header_match.group("text")
            force_bold_line = True
        elif bullet_match:
            prefix = f"{bullet_match.group('indent')}- "
            content = bullet_match.group("text")

        line_start_offset = offset + len(prefix)
        plain_content, content_spans = _parse_inline(content, line_start_offset)

        out_lines.append(prefix + plain_content)
        spans.extend(content_spans)

        if force_bold_line and plain_content:
            spans.append(
                FormatSpan(
                    start=line_start_offset,
                    end=line_start_offset + len(plain_content),
                    bold=True,
                )
            )

        offset = line_start_offset + len(plain_content) + 1  # +1 for the joining '\n'

    return "\n".join(out_lines), spans


def spans_to_text_format_runs(spans: list[FormatSpan], text_length: int) -> list[dict]:
    """Convert overlapping `FormatSpan`s into Google Sheets `textFormatRuns`.

    Each run is `{"startIndex": int, "format": {...}}` and applies until the
    next run's `startIndex` (or the end of the cell text).
    """
    if not spans or text_length <= 0:
        return []

    boundaries = sorted({0, text_length} | {s.start for s in spans} | {s.end for s in spans})
    runs: list[dict] = []
    previous_style: tuple[bool, bool, bool, bool] | None = None

    for start in boundaries[:-1]:
        active = [s for s in spans if s.start <= start < s.end]
        bold = any(s.bold for s in active)
        italic = any(s.italic for s in active)
        strikethrough = any(s.strikethrough for s in active)
        code = any(s.code for s in active)
        style = (bold, italic, strikethrough, code)

        if style == previous_style:
            continue

        text_format: dict = {}
        if bold:
            text_format["bold"] = True
        if italic:
            text_format["italic"] = True
        if strikethrough:
            text_format["strikethrough"] = True
        if code:
            text_format["fontFamily"] = _CODE_FONT

        runs.append({"startIndex": start, "format": text_format})
        previous_style = style

    return runs
