"""Azure OpenAI (GPT-5) structured extraction of weekly reports."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from openai import AzureOpenAI

from config import config
from models import EmployeeMatch, ReportBatch, RosterEntry, WeeklyReport

logger = logging.getLogger(__name__)


def _coerce_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


@dataclass(frozen=True)
class TokenUsage:
    """Token counts reported by an Azure OpenAI response, if any."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
        )

    @classmethod
    def from_completion(cls, completion: Any) -> "TokenUsage":
        usage = getattr(completion, "usage", None)
        if usage is None:
            return cls()
        return cls(
            prompt_tokens=_coerce_int(getattr(usage, "prompt_tokens", None)),
            completion_tokens=_coerce_int(getattr(usage, "completion_tokens", None)),
            total_tokens=_coerce_int(getattr(usage, "total_tokens", None)),
        )


# Accumulates token usage across every LLM call made while processing a single
# Slack message, so app.py can report a per-import total. Thread-local so
# concurrently-handled messages (Bolt may dispatch events on separate threads)
# never mix their totals.
_usage_context = threading.local()


def reset_token_usage() -> None:
    """Start a fresh token-usage accumulation scope on the current thread."""
    _usage_context.total = TokenUsage()


def get_token_usage() -> TokenUsage:
    """Return the token usage accumulated since the last reset_token_usage()."""
    return getattr(_usage_context, "total", TokenUsage())


def _record_usage(usage: TokenUsage, *, context: str) -> None:
    logger.info(
        "Azure OpenAI tokens used (%s): prompt=%d, completion=%d, total=%d",
        context,
        usage.prompt_tokens,
        usage.completion_tokens,
        usage.total_tokens,
    )
    current = getattr(_usage_context, "total", None)
    if current is not None:
        _usage_context.total = current + usage

_SYSTEM_PROMPT = """You extract structured employee weekly work reports. The input may be \
a forwarded Slack DM, a Markdown file, or a text rendering of an Excel workbook. It may \
contain one employee report or several reports bundled together.

Return exactly one report object per employee found in the input. Never combine different \
employees into one report. Repeated worksheet/header rows are context, not employee reports. \
If a row or section has no employee name or no completed-work text, omit it entirely -- \
never return a report with a blank employee_name or tasks_completed.

Excel collection forms (Chinese or English) may use headers such as 填写者/Name, \
汇报日期/Date, 本周工作总结/Tasks completed, 下周工作计划/Next steps, \
遇到的阻碍/风险/问题/Blockers, 需要其他人协助的事项/Needs from others. Treat each \
labeled record as one employee report. Extra columns (e.g. 提交时间) are metadata, \
not a separate report. Keep multiline cell contents inside the field they belong to.

For each employee report, extract exactly these fields:
- employee_name: the report author's full name.
- team: the employee's team or product, if mentioned.
- iso_year_week: the ISO 8601 year-week the report covers, formatted 'YYYY-Www' (e.g. \
'2026-W30'). Prefer a week explicitly stated in the report. If none is stated, use the \
provided message date to compute it.
- tasks_completed: what the employee achieved/completed this week.
- next_steps: what the employee plans to do next week. Leave blank if not stated.
- blockers_risks: blockers or risks encountered. Leave blank if not stated.
- needs_from_others: anything needed from other team members. Leave blank if not stated.
- demo_links: any demo URLs or screenshot links/references mentioned. Leave blank if none.

Do not invent information that isn't present in the report or supplied context.

Remove decorative emojis from every extracted field. Do not remove ordinary punctuation, \
markdown markers, or meaningful non-emoji symbols.

IMPORTANT: Do not translate anything. Keep every field (including employee_name, team, \
and all free-text fields) in the exact original language/script the report was written \
in. Never convert the text into English or any other language.

IMPORTANT: Do not summarize, condense, or rewrite the report's wording. For the free-text \
fields (tasks_completed, next_steps, blockers_risks, needs_from_others, demo_links), copy \
the relevant original sentences/lines verbatim (light cleanup of stray forwarding artifacts \
is fine) and PRESERVE the original formatting:
- Keep line breaks between separate lines, bullet points, and paragraphs using '\\n' -- do \
not merge multiple lines/bullets into a single run-on paragraph or sentence.
- Keep bullet/numbering markers (e.g. '-', '*', '1.') and section titles/headers exactly as \
written.
- Only place text under the field it actually belongs to; e.g. if the report has separate \
'This week' / 'Next week' sections, split their content into tasks_completed / next_steps \
respectively rather than repeating the whole report in one field."""


_MATCH_SYSTEM_PROMPT = """You match an employee name extracted from a Slack weekly report \
to the correct entry in a company roster.

The candidate name may be a Slack display name/handle rather than a full legal name, so it \
can be:
- Missing a surname, middle name, or alias (e.g. "Jane" for roster entry "Jane Doe").
- A nickname or shortened form of a roster name.
- Written in a different script or regional variant than the roster (e.g. Traditional vs \
Simplified Chinese characters for the same name).
- Slightly misspelled or differently romanized.

Rules:
- Only ever return a name that appears verbatim (character-for-character) in the provided \
roster list.
- If exactly one roster entry plausibly refers to the same person as the candidate, return it.
- If several roster entries could plausibly match, use any team/location context provided to \
pick the single most likely one.
- If no roster entry is a plausible match for the same person, return null. Do not guess."""


class ReportExtractionError(RuntimeError):
    """Raised when the LLM fails to produce a valid WeeklyReport."""


_client: AzureOpenAI | None = None


def _get_client() -> AzureOpenAI:
    global _client
    if _client is None:
        _client = AzureOpenAI(
            azure_endpoint=config.azure_openai_endpoint,
            api_key=config.azure_openai_api_key,
            api_version=config.azure_openai_api_version,
        )
    return _client


def _build_user_prompt(report_text: str, message_date: datetime) -> str:
    return (
        "Message date (for ISO week inference if needed): "
        f"{message_date.date().isoformat()}\n\n"
        f"--- Report text ---\n{report_text.strip()}"
    )


class ReportExtractor:
    """Thin wrapper around the Azure OpenAI structured-output extraction call."""

    def __init__(self) -> None:
        self._client = _get_client()

    def extract_many(
        self,
        report_text: str,
        message_date: datetime | None = None,
        source_name: str | None = None,
    ) -> list[WeeklyReport]:
        if not report_text or not report_text.strip():
            raise ReportExtractionError("Report text is empty; nothing to extract.")

        message_date = message_date or datetime.now(timezone.utc)
        user_prompt = _build_user_prompt(report_text, message_date)
        if source_name:
            user_prompt = f"Source file/message: {source_name}\n{user_prompt}"

        logger.info(
            "Azure OpenAI extraction request (source=%s, input=%s chars, deployment=%s)...",
            source_name or "message",
            len(report_text),
            config.azure_openai_deployment,
        )
        try:
            completion = self._client.chat.completions.parse(
                model=config.azure_openai_deployment,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": user_prompt,
                    },
                ],
                response_format=ReportBatch,
            )
        except Exception as exc:  # noqa: BLE001 - surface all failures uniformly
            logger.exception("Azure OpenAI extraction call failed")
            raise ReportExtractionError(f"LLM call failed: {exc}") from exc

        _record_usage(
            TokenUsage.from_completion(completion),
            context=f"extraction of '{source_name or 'message'}'",
        )

        choice = completion.choices[0]
        if choice.message.refusal:
            raise ReportExtractionError(
                f"LLM refused to extract report: {choice.message.refusal}"
            )

        parsed = choice.message.parsed
        if parsed is None:
            raise ReportExtractionError("LLM did not return a parsed structured result.")

        logger.info(
            "Azure OpenAI extraction returned %d report(s) (source=%s)",
            len(parsed.reports),
            source_name or "message",
        )
        return parsed.reports

    def extract(
        self, report_text: str, message_date: datetime | None = None
    ) -> WeeklyReport:
        """Backward-compatible single-report extraction helper."""
        reports = self.extract_many(report_text, message_date)
        if len(reports) != 1:
            raise ReportExtractionError(
                f"Expected one report but extracted {len(reports)}."
            )
        return reports[0]


_extractor: ReportExtractor | None = None


def get_extractor() -> ReportExtractor:
    global _extractor
    if _extractor is None:
        _extractor = ReportExtractor()
    return _extractor


def extract_report(
    report_text: str, message_date: datetime | None = None
) -> WeeklyReport:
    """Convenience wrapper around a lazily-created module-level ReportExtractor."""
    return get_extractor().extract(report_text, message_date)


def extract_reports(
    report_text: str,
    message_date: datetime | None = None,
    source_name: str | None = None,
) -> list[WeeklyReport]:
    """Extract all employee reports found in one message or attached file."""
    return get_extractor().extract_many(report_text, message_date, source_name)


def _format_roster_line(entry: RosterEntry) -> str:
    details = ", ".join(
        bit
        for bit in [
            f"Team: {entry.team}" if entry.team else "",
            f"Location: {entry.location}" if entry.location else "",
        ]
        if bit
    )
    return f"- {entry.name}" + (f" ({details})" if details else "")


def match_employee_name(
    candidate_name: str,
    roster: list[RosterEntry],
    hint_team: str | None = None,
) -> str | None:
    """Ask the LLM to pick the roster entry that best matches `candidate_name`.

    Handles cases plain string similarity misses: partial names/aliases (e.g. a
    Slack handle missing the surname) and script/regional variants (e.g.
    Traditional vs Simplified Chinese). Returns the matched name exactly as it
    appears in `roster`, or None if no entry is a plausible match (or the call
    fails for any reason -- callers should treat this the same as "no match").
    """
    candidate_name = (candidate_name or "").strip()
    if not candidate_name or not roster:
        return None

    roster_list_text = "\n".join(_format_roster_line(entry) for entry in roster)
    user_content = f"Candidate name (from a Slack message): {candidate_name}"
    if hint_team:
        user_content += (
            f"\nTeam mentioned in the report (may help disambiguate, may be "
            f"inaccurate): {hint_team}"
        )
    user_content += f"\n\nRoster:\n{roster_list_text}"

    try:
        completion = _get_client().chat.completions.parse(
            model=config.azure_openai_deployment,
            messages=[
                {"role": "system", "content": _MATCH_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            response_format=EmployeeMatch,
        )
    except Exception:  # noqa: BLE001 - matching is best-effort, never fatal
        logger.exception("LLM employee-name matching call failed")
        return None

    _record_usage(
        TokenUsage.from_completion(completion),
        context=f"employee match for '{candidate_name}'",
    )

    choice = completion.choices[0]
    if choice.message.refusal or choice.message.parsed is None:
        return None

    matched = choice.message.parsed.matched_name
    if not matched:
        return None

    for entry in roster:
        if entry.name.strip().lower() == matched.strip().lower():
            logger.info(
                "LLM-matched employee '%s' to roster entry '%s' (%s)",
                candidate_name,
                entry.name,
                choice.message.parsed.reasoning,
            )
            return entry.name

    logger.warning(
        "LLM returned a matched_name not present in the roster: %r", matched
    )
    return None
