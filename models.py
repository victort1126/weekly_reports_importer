"""Data models shared across the extraction and sheet-append pipeline."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from text_cleanup import remove_emojis

ISO_YEAR_WEEK_RE = re.compile(r"^\d{4}-W\d{2}$")


class WeeklyReport(BaseModel):
    """Structured fields extracted from a forwarded weekly-report message by the LLM."""

    employee_name: str = Field(
        ..., description="The employee's full name as it appears in the report."
    )
    team: str | None = Field(
        None, description="The employee's team/product, if mentioned in the report."
    )
    iso_year_week: str = Field(
        ...,
        description=(
            "The ISO year-week the report covers, formatted as 'YYYY-Www' "
            "(e.g. '2026-W30'). Use the week stated in the report if present, "
            "otherwise infer it from the message date."
        ),
    )
    tasks_completed: str = Field(
        ...,
        description=(
            "What the employee achieved/completed this week, copied verbatim "
            "(not summarized) with original line breaks/bullets preserved. "
            "If a row/section has no completed-work text, omit that report "
            "entirely instead of returning an empty string."
        ),
    )
    next_steps: str | None = Field(
        None,
        description=(
            "What the employee is planning to do next week, if stated, copied "
            "verbatim (not summarized) with original line breaks/bullets preserved."
        ),
    )
    blockers_risks: str | None = Field(
        None,
        description=(
            "Any blockers or risks the employee encountered, if stated, copied "
            "verbatim (not summarized) with original line breaks/bullets preserved."
        ),
    )
    needs_from_others: str | None = Field(
        None,
        description=(
            "Anything the employee needs from other team members, if stated, "
            "copied verbatim (not summarized) with original line breaks/bullets "
            "preserved."
        ),
    )
    demo_links: str | None = Field(
        None, description="Demo links or screenshot references, if provided."
    )

    @field_validator(
        "employee_name",
        "team",
        "tasks_completed",
        "next_steps",
        "blockers_risks",
        "needs_from_others",
        "demo_links",
        mode="before",
    )
    @classmethod
    def _remove_emojis(cls, value: str | None) -> str | None:
        return remove_emojis(value) if isinstance(value, str) else value

    @field_validator("iso_year_week")
    @classmethod
    def _validate_iso_year_week(cls, value: str) -> str:
        value = value.strip()
        if not ISO_YEAR_WEEK_RE.match(value):
            raise ValueError(f"iso_year_week must look like 'YYYY-Www', got {value!r}")
        return value

    @field_validator("employee_name", "tasks_completed")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("must not be blank")
        return value.strip()

    @field_validator("team", "next_steps", "blockers_risks", "needs_from_others", "demo_links")
    @classmethod
    def _blank_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class ReportBatch(BaseModel):
    """One or more employee reports extracted from a message or bundled workbook."""

    reports: list[WeeklyReport] = Field(
        ...,
        min_length=1,
        description=(
            "One WeeklyReport per employee found in the input. A workbook can "
            "contain reports for several employees; do not merge them. Omit "
            "any row that has no employee name or no completed-work text."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _drop_incomplete_reports(cls, data: Any) -> Any:
        """Skip rows the model returned with a blank name or tasks_completed.

        Structured-output parsing used to fail the entire workbook when the
        first report had an empty required field (common with header rows or
        TSV-broken multiline cells). Dropping those stubs lets the remaining
        valid reports through.
        """
        if not isinstance(data, dict):
            return data
        reports = data.get("reports")
        if not isinstance(reports, list):
            return data
        kept: list[Any] = []
        for report in reports:
            if isinstance(report, dict):
                name = str(report.get("employee_name") or "").strip()
                tasks = str(report.get("tasks_completed") or "").strip()
                if not name or not tasks:
                    continue
            kept.append(report)
        return {**data, "reports": kept}


class EmployeeMatch(BaseModel):
    """LLM's chosen best-match roster entry for a candidate employee name."""

    matched_name: str | None = Field(
        None,
        description=(
            "The roster name that best refers to the same person as the "
            "candidate name, copied verbatim from the provided roster list. "
            "Null if none of the roster names plausibly refer to the same "
            "person."
        ),
    )
    reasoning: str = Field(
        "", description="One short sentence explaining the match decision."
    )


class RosterEntry(BaseModel):
    """A single row from the 'Roster' tab (columns A-D)."""

    name: str
    location: str = ""
    team: str = ""
    email: str = ""


class EnrichedReport(BaseModel):
    """A WeeklyReport merged with Roster data, ready to become a Report Log row."""

    iso_year_week: str
    employee_name: str
    team: str = ""
    tasks_completed: str
    next_steps: str = ""
    blockers_risks: str = ""
    blocker_status: str = ""
    needs_from_others: str = ""
    demo_links: str = ""

    roster_matched: bool = False
    email: str = ""
    location: str = ""

    @field_validator(
        "iso_year_week",
        "employee_name",
        "team",
        "tasks_completed",
        "next_steps",
        "blockers_risks",
        "blocker_status",
        "needs_from_others",
        "demo_links",
        "email",
        "location",
        mode="before",
    )
    @classmethod
    def _remove_emojis(cls, value: str) -> str:
        return remove_emojis(value) if isinstance(value, str) else value

    def to_row(self) -> list[str]:
        """Return the values for columns B..J of the 'Report Log' tab, in order."""
        return [
            self.iso_year_week,
            self.employee_name,
            self.team,
            self.tasks_completed,
            self.next_steps,
            self.blockers_risks,
            self.blocker_status,
            self.needs_from_others,
            self.demo_links,
        ]
