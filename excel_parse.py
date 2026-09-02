"""Turn Excel worksheets into labeled records and, when possible, WeeklyReports.

Workbook ingest used to dump every sheet as tab-separated rows. That breaks as
soon as a cell contains a line break (the rest of the cell looks like a new
employee row) or a header is renamed/reordered. This module:

- Detects a header row even when a title row sits above it.
- Maps Chinese/English header variants onto the canonical WeeklyReport fields.
- Renders each data row as a labeled record so multiline cells stay attached
  to their column.
- Builds WeeklyReport objects directly when a name + completed-work column can
  be identified, so a well-formed collection form does not need an LLM call.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Iterable

from models import WeeklyReport

logger = logging.getLogger(__name__)

# Canonical field -> header phrases people actually put in collection forms.
# Matching is case/punctuation-insensitive; longer aliases win over shorter ones
# so "本周工作总结" is not stolen by a generic "工作" token.
_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "employee_name": (
        "填写者",
        "姓名",
        "员工姓名",
        "员工",
        "汇报人",
        "提交人",
        "上报人",
        "employee name",
        "full name",
        "submitter",
        "reporter",
        "author",
        "employee",
        "name",
    ),
    "team": (
        "当前团队",
        "所属团队",
        "团队",
        "组别",
        "部门",
        "小组",
        "product",
        "squad",
        "group",
        "team",
    ),
    "iso_year_week": (
        "汇报日期",
        "报告日期",
        "工作日期",
        "所属周",
        "iso year-week",
        "iso week",
        "year week",
        "report date",
        "work date",
        "周次",
        "年周",
        "week",
        "日期",
    ),
    "tasks_completed": (
        "本周工作总结",
        "本周完成工作",
        "本周完成",
        "本周工作",
        "本周进展",
        "本周任务",
        "工作总结",
        "完成事项",
        "工作内容",
        "tasks completed",
        "this week",
        "accomplishments",
        "completed",
        "summary",
        "progress",
    ),
    "next_steps": (
        "下周工作计划",
        "下周计划",
        "下周工作",
        "下步计划",
        "下一步",
        "next steps",
        "next week",
        "next",
        "plans",
        "plan",
    ),
    "blockers_risks": (
        "遇到的阻碍/风险/问题",
        "阻碍/风险/问题",
        "blockers/risks",
        "blockers risks",
        "blockers",
        "risks",
        "阻塞",
        "阻碍",
        "风险",
        "问题",
    ),
    "needs_from_others": (
        "需要其他人协助的事项",
        "需要其他人协助",
        "需要协助",
        "协助事项",
        "needs from others",
        "help needed",
        "dependencies",
        "needs",
    ),
    "demo_links": (
        "demo links",
        "screenshots",
        "demo",
        "链接",
        "links",
    ),
}

# Metadata columns that look date-like but are not the week the report covers.
_IGNORED_HEADERS = {
    "提交时间",
    "submittime",
    "submissiontime",
    "timestamp",
    "createdat",
    "id",
    "序号",
    "编号",
}

# Optional-field placeholders used in Chinese forms; treat as "not stated".
_BLANK_PLACEHOLDERS = {
    "",
    "/",
    "\\",
    "-",
    "—",
    "–",
    "无",
    "無",
    "无。",
    "暂无",
    "暫無",
    "n/a",
    "na",
    "none",
    "nil",
    "null",
    "暂不需要",
    "暫不需要",
    "暂无需要",
    "不需要",
}

_CHINESE_DATE_RE = re.compile(
    r"(?P<year>\d{4})\s*年\s*(?P<month>\d{1,2})\s*月\s*(?P<day>\d{1,2})\s*日?"
)
_ISO_DATE_RE = re.compile(r"(?P<year>\d{4})[-/.](?P<month>\d{1,2})[-/.](?P<day>\d{1,2})")
_ISO_WEEK_RE = re.compile(r"(?P<year>\d{4})-W(?P<week>\d{2})", re.IGNORECASE)
_HEADER_SCAN_ROWS = 15
# Only cells this short are treated as possible column labels. Body text in a
# 本周工作总结 cell is much longer; matching aliases inside it would steal the
# header row (e.g. the word "问题" in a work summary).
_LABEL_CELL_MAX_LEN = 80

# Latin aliases shorter than this are exact-match only. Otherwise "week" inside
# "nextweek" would map a Next-week column onto iso_year_week.
_SUBSTRING_LATIN_MIN = 8


@dataclass(frozen=True)
class ParsedWorkbook:
    """Labeled text for the LLM, plus structured reports when the table is recognized."""

    text: str
    reports: tuple[WeeklyReport, ...] = ()
    needs_llm: bool = True


def _normalize_header(value: str) -> str:
    """Collapse whitespace/punctuation so '本周工作总结' and '本周 工作-总结' match."""
    collapsed = re.sub(r"[\s:：\-_/\\|.,，。()\[\]（）【】]+", "", value.strip().lower())
    return collapsed


def _cjk_len(value: str) -> int:
    return sum(1 for char in value if "\u4e00" <= char <= "\u9fff")


def _alias_allows_substring(alias: str) -> bool:
    if _cjk_len(alias) >= 2:
        return True
    latin_len = len(re.sub(r"[^a-z0-9]", "", alias))
    return latin_len >= _SUBSTRING_LATIN_MIN


def _build_alias_index() -> list[tuple[str, str, bool]]:
    """Return (canonical field, normalized alias, substring-ok) sorted longest-first."""
    index: list[tuple[str, str, bool]] = []
    for field, aliases in _FIELD_ALIASES.items():
        for alias in aliases:
            normalized = _normalize_header(alias)
            if normalized:
                index.append((field, normalized, _alias_allows_substring(alias)))
    index.sort(key=lambda item: len(item[1]), reverse=True)
    return index


_ALIAS_INDEX = _build_alias_index()


def _match_field(header: str, *, allow_substring: bool = True) -> str | None:
    """Return the canonical field for a header, or None if it is unknown/metadata."""
    normalized = _normalize_header(header)
    if not normalized or normalized in _IGNORED_HEADERS:
        return None

    for field, alias, _substring_ok in _ALIAS_INDEX:
        if normalized == alias:
            return field

    if not allow_substring:
        return None
    for field, alias, substring_ok in _ALIAS_INDEX:
        if substring_ok and alias in normalized:
            return field
    return None


def _is_label_cell(value: str) -> bool:
    """True when a cell is short enough to be a column header rather than body text."""
    stripped = value.strip()
    if not stripped or len(stripped) > _LABEL_CELL_MAX_LEN:
        return False
    return stripped.count("\n") <= 1


def _is_header_like(values: list[str]) -> bool:
    """True when a row looks like labels rather than report body text."""
    populated = [value.strip() for value in values if value.strip()]
    if len(populated) < 2:
        return False
    return all(_is_label_cell(value) for value in populated)


def _cell_header_field(cell: str) -> str | None:
    """Map one cell onto a canonical field without matching inside report body text."""
    stripped = cell.strip()
    if not stripped:
        return None
    if _is_label_cell(stripped):
        return _match_field(stripped)
    # Long cells: only the first line, exact alias match. Substring matching
    # would treat a summary that happens to contain "问题" as a Blockers header.
    first_line = stripped.split("\n", 1)[0].strip()
    if first_line and len(first_line) <= _LABEL_CELL_MAX_LEN:
        return _match_field(first_line, allow_substring=False)
    return None


def _row_field_mapping(row: list[str]) -> dict[int, str]:
    mapping: dict[int, str] = {}
    for col, cell in enumerate(row):
        field = _cell_header_field(cell)
        if field and field not in mapping.values():
            mapping[col] = field
    return mapping


def _find_header_row(rows: list[list[str]]) -> tuple[int, dict[int, str]] | None:
    """Pick the row that maps onto the most canonical fields.

    Collection forms often put a title in row 1 and the real headers in row 2.
    One slightly long header cell (a parenthetical hint) must not disqualify
    the row — only cells that look like labels are eligible to match.
    """
    best: tuple[int, dict[int, str], int] | None = None
    scanned = 0
    for index, row in enumerate(rows):
        if not any(cell.strip() for cell in row):
            continue
        scanned += 1
        mapping = _row_field_mapping(row)
        score = len(mapping)
        if score >= 2 and (best is None or score > best[2]):
            best = (index, mapping, score)
        if scanned >= _HEADER_SCAN_ROWS:
            break

    if best is None:
        return None
    return best[0], best[1]


def iso_year_week_from_value(
    value: Any, fallback: datetime | date | None = None
) -> str | None:
    """Parse a cell (datetime, '2026年8月7日', '2026-08-07', '2026-W32') into 'YYYY-Www'."""
    if isinstance(value, datetime):
        iso = value.date().isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    if isinstance(value, date):
        iso = value.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"

    text = str(value or "").strip()
    if text:
        week_match = _ISO_WEEK_RE.search(text)
        if week_match:
            return f"{week_match.group('year')}-W{int(week_match.group('week')):02d}"

        for pattern in (_CHINESE_DATE_RE, _ISO_DATE_RE):
            match = pattern.search(text)
            if match:
                parsed = date(
                    int(match.group("year")),
                    int(match.group("month")),
                    int(match.group("day")),
                )
                iso = parsed.isocalendar()
                return f"{iso.year}-W{iso.week:02d}"

        try:
            parsed = date.fromisoformat(text[:10])
        except ValueError:
            parsed = None
        if parsed is not None:
            iso = parsed.isocalendar()
            return f"{iso.year}-W{iso.week:02d}"

    if fallback is None:
        return None
    fallback_date = fallback.date() if isinstance(fallback, datetime) else fallback
    iso = fallback_date.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _is_blank(value: str | None) -> bool:
    if value is None:
        return True
    stripped = value.strip()
    return not stripped or stripped.lower() in _BLANK_PLACEHOLDERS


def _optional_text(value: str | None) -> str | None:
    if _is_blank(value):
        return None
    return value.strip()


def _row_values(row: Iterable[Any]) -> list[str]:
    values = ["" if cell is None else str(cell) for cell in row]
    while values and not values[-1].strip():
        values.pop()
    return values


def _record_label(headers: list[str], col: int) -> str:
    if col < len(headers) and headers[col].strip():
        return headers[col].strip()
    return f"Column {col + 1}"


def _render_record(
    record_number: int,
    headers: list[str],
    values: list[str],
    field_by_col: dict[int, str],
) -> str:
    """Render one data row as a labeled block, keeping multiline cell text intact."""
    lines = [f"--- Record {record_number} ---"]
    width = max(len(headers), len(values))
    for col in range(width):
        value = values[col] if col < len(values) else ""
        if not value.strip():
            continue
        label = _record_label(headers, col)
        field = field_by_col.get(col)
        heading = f"{label} ({field})" if field else label
        stripped = value.strip()
        if "\n" in stripped:
            lines.append(f"{heading}:")
            lines.append(stripped)
        else:
            lines.append(f"{heading}: {stripped}")
    return "\n".join(lines)


def _report_from_mapped_row(
    values: list[str],
    field_by_col: dict[int, str],
    message_date: datetime | None,
) -> WeeklyReport | None:
    """Build a WeeklyReport from one mapped row, or None if required fields are missing."""
    by_field: dict[str, str] = {}
    for col, field in field_by_col.items():
        if col >= len(values):
            continue
        text = values[col].strip()
        if not text:
            continue
        existing = by_field.get(field)
        by_field[field] = f"{existing}\n{text}" if existing else text

    name = by_field.get("employee_name")
    tasks = by_field.get("tasks_completed")
    if _is_blank(name) or _is_blank(tasks):
        return None

    iso_week = iso_year_week_from_value(
        by_field.get("iso_year_week"),
        fallback=message_date or datetime.now(timezone.utc),
    )
    if not iso_week:
        return None

    return WeeklyReport(
        employee_name=name.strip(),
        team=_optional_text(by_field.get("team")),
        iso_year_week=iso_week,
        tasks_completed=tasks.strip(),
        next_steps=_optional_text(by_field.get("next_steps")),
        blockers_risks=_optional_text(by_field.get("blockers_risks")),
        needs_from_others=_optional_text(by_field.get("needs_from_others")),
        demo_links=_optional_text(by_field.get("demo_links")),
    )


def parse_worksheet_tables(
    sheets: list[tuple[str, list[list[str]]]],
    message_date: datetime | None = None,
) -> ParsedWorkbook:
    """Convert worksheets into labeled text and extract reports from recognized tables.

    A sheet is "recognized" when some header row maps onto both employee_name
    and tasks_completed. Recognized sheets are turned into WeeklyReport objects
    and do not need the LLM. Unrecognized sheets are still rendered as labeled
    records so a later LLM pass can try.
    """
    sections_all: list[str] = []
    reports: list[WeeklyReport] = []
    record_number = 0

    for title, rows in sheets:
        populated = [row for row in (_row_values(row) for row in rows) if any(cell.strip() for cell in row)]
        if not populated:
            continue

        header_info = _find_header_row(populated)
        field_by_col: dict[int, str] = {}
        header_index = -1
        headers: list[str] = []
        recognized = False

        if header_info is not None:
            header_index, field_by_col = header_info
            headers = populated[header_index]
            recognized = (
                "employee_name" in field_by_col.values()
                and "tasks_completed" in field_by_col.values()
            )
        elif len(populated) > 1 and _is_header_like(populated[0]):
            # First row looks like labels but none map onto known fields; still
            # use it as display headers so the LLM sees column names.
            headers = populated[0]
            header_index = 0

        sheet_blocks: list[str] = []
        data_rows = populated[header_index + 1 :] if header_index >= 0 else populated
        extracted_here = 0
        for row in data_rows:
            if not any(cell.strip() for cell in row):
                continue
            record_number += 1
            sheet_blocks.append(
                _render_record(record_number, headers, row, field_by_col)
            )
            if recognized:
                report = _report_from_mapped_row(row, field_by_col, message_date)
                if report is None:
                    logger.info(
                        "Skipping incomplete Excel row on '%s' (missing name or tasks)",
                        title,
                    )
                    continue
                reports.append(report)
                extracted_here += 1

        if sheet_blocks:
            mapping_note = ""
            if field_by_col:
                mapped = ", ".join(
                    f"{headers[col]}->{field}"
                    for col, field in sorted(field_by_col.items())
                    if col < len(headers)
                )
                mapping_note = f"\nDetected columns: {mapped}"
            sections_all.append(
                f"=== Worksheet: {title} ==={mapping_note}\n" + "\n\n".join(sheet_blocks)
            )
            if recognized:
                logger.info(
                    "Excel sheet '%s' recognized as a weekly-report table: %d report(s)",
                    title,
                    extracted_here,
                )

    if not sections_all:
        return ParsedWorkbook(text="", reports=(), needs_llm=True)

    # Table-extracted reports already have verbatim cell text. Calling the LLM
    # as well would let it invent a stub from the Slack filename/message date
    # and save that instead of (or in addition to) the workbook contents.
    if reports:
        logger.info(
            "Workbook extracted %d report(s) from table columns; LLM extraction not needed",
            len(reports),
        )
        return ParsedWorkbook(
            text="\n\n".join(sections_all),
            reports=tuple(reports),
            needs_llm=False,
        )

    return ParsedWorkbook(
        text="\n\n".join(sections_all),
        reports=(),
        needs_llm=True,
    )
