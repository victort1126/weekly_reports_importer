"""Slack Bolt app (Socket Mode) that turns forwarded weekly-report DMs into
Google Sheet rows.

Pipeline: Slack message -> slack_handler (text/files) -> llm (extraction) ->
sheets (roster lookup + append) -> thread reply.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from config import config
from llm import ReportExtractionError, extract_report
from models import EnrichedReport
from sheets import SheetsError, get_sheets_client
from slack_handler import (
    build_report_text,
    get_file_links,
    is_ignorable_message,
    merge_demo_links,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("weekly_importer")

app = App(token=config.slack_bot_token)


def _message_datetime(event: dict) -> datetime:
    ts = event.get("ts")
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


def _build_success_reply(enriched: EnrichedReport) -> str:
    lines = [
        ":white_check_mark: Logged weekly report for "
        f"*{enriched.employee_name}* ({enriched.iso_year_week}), team: "
        f"*{enriched.team or 'unknown'}*.",
    ]
    if not enriched.roster_matched:
        lines.append(
            ":warning: Employee not found in the *Roster* tab -- used the "
            "name/team as extracted from the report. Please check spelling "
            "or add them to the Roster."
        )
    return "\n".join(lines)


@app.event("message")
def handle_report_message(event: dict, say, logger: logging.Logger) -> None:  # noqa: D401
    if is_ignorable_message(event):
        return

    if config.slack_channel_id and event.get("channel") != config.slack_channel_id:
        return

    # Only treat top-level channel messages as new reports; ignore thread replies.
    if event.get("thread_ts") and event.get("thread_ts") != event.get("ts"):
        return

    report_text = build_report_text(event)
    if not report_text.strip():
        return

    thread_ts = event.get("ts")

    try:
        weekly_report = extract_report(report_text, _message_datetime(event))
    except ReportExtractionError as exc:
        logger.warning("Extraction failed: %s", exc)
        say(text=f":x: Couldn't parse this as a weekly report: {exc}", thread_ts=thread_ts)
        return
    except Exception:  # noqa: BLE001
        logger.exception("Unexpected error during extraction")
        say(
            text=":x: Unexpected error while extracting this report. Check the app logs.",
            thread_ts=thread_ts,
        )
        return

    file_links = get_file_links(event.get("files", []))
    demo_links = merge_demo_links(weekly_report.demo_links, file_links)

    try:
        sheets_client = get_sheets_client()
        roster_entry = sheets_client.lookup_employee(
            weekly_report.employee_name, hint_team=weekly_report.team
        )
    except SheetsError as exc:
        logger.exception("Roster lookup failed")
        say(text=f":x: Couldn't reach the Roster sheet: {exc}", thread_ts=thread_ts)
        return

    enriched = EnrichedReport(
        iso_year_week=weekly_report.iso_year_week,
        employee_name=(
            roster_entry.name if roster_entry else weekly_report.employee_name
        ),
        team=(roster_entry.team if roster_entry and roster_entry.team else (weekly_report.team or "")),
        tasks_completed=weekly_report.tasks_completed,
        next_steps=weekly_report.next_steps or "",
        blockers_risks=weekly_report.blockers_risks or "",
        blocker_status="Open" if weekly_report.blockers_risks else "",
        needs_from_others=weekly_report.needs_from_others or "",
        demo_links=demo_links or "",
        roster_matched=roster_entry is not None,
        email=roster_entry.email if roster_entry else "",
        location=roster_entry.location if roster_entry else "",
    )

    try:
        sheets_client.append_report(enriched)
    except SheetsError as exc:
        logger.exception("Append to Report Log failed")
        say(text=f":x: Parsed the report but failed to save it: {exc}", thread_ts=thread_ts)
        return

    say(text=_build_success_reply(enriched), thread_ts=thread_ts)


def main() -> None:
    logger.info("Starting Weekly Report Importer (Socket Mode)...")
    handler = SocketModeHandler(app, config.slack_app_token)
    handler.start()


if __name__ == "__main__":
    main()
