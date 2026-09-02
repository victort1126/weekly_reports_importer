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
from file_ingest import (
    FileIngestionError,
    IngestedFile,
    ingest_slack_files,
    is_supported_report_file,
)
from llm import ReportExtractionError, extract_reports, get_token_usage, reset_token_usage
from models import EnrichedReport, WeeklyReport
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


def _build_skip_reply(enriched: EnrichedReport) -> str:
    return (
        ":information_source: Skipped *{name}* ({week}) -- already present in "
        "the *Report Log* (same ISO week + employee name)."
    ).format(name=enriched.employee_name, week=enriched.iso_year_week)


def _message_is_attachment_filename(message_text: str, files: list[dict]) -> bool:
    """True when Slack's message text is only the uploaded file's name.

    Slack often sets event.text to '工作周报.xlsx' for a file upload. Feeding
    that to the LLM as 'report context' is how a stub row with the filename as
    tasks_completed gets saved.
    """
    stripped = message_text.strip()
    if not stripped:
        return True
    names: set[str] = set()
    for file in files:
        for key in ("name", "title"):
            value = str(file.get(key) or "").strip()
            if not value:
                continue
            names.add(value)
            names.add(value.replace("\\", "/").rsplit("/", 1)[-1])
    return stripped in names


def _load_report_sources(event: dict, client) -> tuple[list[IngestedFile], list[str]]:
    """Build LLM input sources from message text and supported attachments."""
    message_text = build_report_text(event)
    supported_files = [
        file for file in event.get("files", []) if is_supported_report_file(file)
    ]
    logger.info(
        "Building report sources: message text=%s chars, supported attachment(s)=%d",
        len(message_text),
        len(supported_files),
    )
    sources: list[IngestedFile] = []
    errors: list[str] = []

    for file in supported_files:
        file_label = file.get("name") or file.get("title") or "file"
        logger.info("Ingesting attachment '%s'...", file_label)
        file_metadata = file
        if not (
            file_metadata.get("url_private_download")
            or file_metadata.get("url_private")
        ) and file_metadata.get("id"):
            try:
                response = client.files_info(file=file_metadata["id"])
                file_metadata = response.get("file", file_metadata)
            except Exception as exc:  # Slack SDK exception types vary by adapter
                errors.append(
                    f"Could not retrieve Slack metadata for "
                    f"'{file.get('name') or file.get('title') or 'file'}': {exc}"
                )
                continue

        try:
            ingested_files = ingest_slack_files(
                file_metadata,
                config.slack_bot_token,
                message_date=_message_datetime(event),
            )
        except FileIngestionError as exc:
            logger.warning("Attachment '%s' failed: %s", file_label, exc)
            errors.append(str(exc))
            continue

        logger.info(
            "Attachment '%s' produced %d extraction source(s)",
            file_label,
            len(ingested_files),
        )
        for ingested in ingested_files:
            logger.info(
                "Source '%s' ready for extraction (%s chars)",
                ingested.name,
                len(ingested.text),
            )
            content = ingested.text
            if message_text and not _message_is_attachment_filename(
                message_text, supported_files
            ):
                content = (
                    f"Slack message context:\n{message_text}\n\n"
                    f"Attached file content:\n{content}"
                )
            sources.append(
                IngestedFile(
                    name=ingested.name,
                    text=content,
                    reports=ingested.reports,
                    needs_llm=ingested.needs_llm,
                )
            )

    # For file-based submissions, the message text is context for the file,
    # not a second report. Ordinary forwarded/pasted messages remain one source.
    if not supported_files and message_text:
        sources.append(IngestedFile(name="Slack message", text=message_text))
        logger.info("Using Slack message text only (%s chars)", len(message_text))

    logger.info("Report sources ready: %d source(s), %d error(s)", len(sources), len(errors))
    return sources, errors


def _enrich_report(
    weekly_report: WeeklyReport, file_links: list[str]
) -> EnrichedReport:
    logger.info(
        "Roster lookup for '%s' (hint team=%s)...",
        weekly_report.employee_name,
        weekly_report.team or "(none)",
    )
    sheets_client = get_sheets_client()
    roster_entry = sheets_client.lookup_employee(
        weekly_report.employee_name, hint_team=weekly_report.team
    )
    if roster_entry:
        logger.info("Roster match: '%s' -> '%s'", weekly_report.employee_name, roster_entry.name)
    else:
        logger.info("No roster match for '%s'", weekly_report.employee_name)
    demo_links = merge_demo_links(weekly_report.demo_links, file_links)

    return EnrichedReport(
        iso_year_week=weekly_report.iso_year_week,
        employee_name=(
            roster_entry.name if roster_entry else weekly_report.employee_name
        ),
        team=(
            roster_entry.team
            if roster_entry and roster_entry.team
            else (weekly_report.team or "")
        ),
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


@app.event("message")
def handle_report_message(event: dict, say, client, **_kwargs) -> None:  # noqa: D401
    if is_ignorable_message(event):
        return

    if config.slack_channel_id and event.get("channel") != config.slack_channel_id:
        return

    # Only treat top-level channel messages as new reports; ignore thread replies.
    if event.get("thread_ts") and event.get("thread_ts") != event.get("ts"):
        return

    thread_ts = event.get("ts")
    channel = event.get("channel", "?")
    logger.info(
        "=== Weekly report message received (channel=%s, ts=%s) ===",
        channel,
        thread_ts,
    )
    reset_token_usage()
    sources, errors = _load_report_sources(event, client)
    if not sources:
        if errors:
            say(
                text="\n".join(f":x: {error}" for error in errors),
                thread_ts=thread_ts,
            )
        return

    file_links = get_file_links(event.get("files", []))
    reports: list[tuple[str, WeeklyReport]] = []
    for source in sources:
        try:
            extracted: list[WeeklyReport] = []
            if source.reports:
                logger.info(
                    "Using %d table-extracted report(s) from '%s' (no LLM)",
                    len(source.reports),
                    source.name,
                )
                extracted.extend(source.reports)
            if source.needs_llm:
                logger.info(
                    "LLM extraction starting for '%s' (%s chars)...",
                    source.name,
                    len(source.text),
                )
                extracted.extend(
                    extract_reports(
                        source.text, _message_datetime(event), source_name=source.name
                    )
                )
                logger.info(
                    "LLM extraction finished for '%s': %d report(s)",
                    source.name,
                    len(extracted),
                )
            elif not source.reports:
                errors.append(f"'{source.name}' contained no extractable weekly reports.")
                continue
            else:
                logger.info(
                    "LLM extraction skipped for '%s': table columns already mapped",
                    source.name,
                )
            reports.extend((source.name, report) for report in extracted)
        except ReportExtractionError as exc:
            logger.warning("Extraction failed for %s: %s", source.name, exc)
            errors.append(f"Couldn't parse '{source.name}' as weekly report(s): {exc}")
        except Exception:  # noqa: BLE001
            logger.exception("Unexpected extraction error for %s", source.name)
            errors.append(
                f"Unexpected error while extracting '{source.name}'. Check the app logs."
            )

    replies: list[str] = []
    saved_count = 0
    skipped_count = 0
    for source_name, weekly_report in reports:
        try:
            logger.info(
                "Preparing Report Log save: %s (%s) from '%s'...",
                weekly_report.employee_name,
                weekly_report.iso_year_week,
                source_name,
            )
            enriched = _enrich_report(weekly_report, file_links)
            sheets_client = get_sheets_client()
            if sheets_client.report_exists(
                enriched.iso_year_week, enriched.employee_name
            ):
                skipped_count += 1
                logger.info(
                    "Skipping duplicate: %s (%s) already in Report Log",
                    enriched.employee_name,
                    enriched.iso_year_week,
                )
                replies.append(_build_skip_reply(enriched))
                continue

            sheets_client.append_report(enriched)
            saved_count += 1
            logger.info(
                "Saved row for %s (%s)",
                enriched.employee_name,
                enriched.iso_year_week,
            )
            replies.append(_build_success_reply(enriched))
        except SheetsError as exc:
            logger.exception(
                "Failed to save report for %s from %s",
                weekly_report.employee_name,
                source_name,
            )
            errors.append(
                f"Parsed {weekly_report.employee_name}'s report from "
                f"'{source_name}' but failed to save it: {exc}"
            )

    replies.extend(f":x: {error}" for error in errors)

    usage = get_token_usage()
    if usage.total_tokens:
        replies.append(
            f":receipt: Tokens used for this import: *{usage.total_tokens}* "
            f"(prompt: {usage.prompt_tokens}, completion: {usage.completion_tokens})"
        )

    if replies:
        logger.info("Posting Slack thread reply (%d part(s))...", len(replies))
        say(text="\n\n".join(replies), thread_ts=thread_ts)
    logger.info(
        "=== Message done (ts=%s): %d saved, %d skipped (duplicates), %d error(s), "
        "%d total tokens used (prompt=%d, completion=%d) ===",
        thread_ts,
        saved_count,
        skipped_count,
        len(errors),
        usage.total_tokens,
        usage.prompt_tokens,
        usage.completion_tokens,
    )


def main() -> None:
    logger.info("Starting Weekly Report Importer (Socket Mode)...")
    if config.slack_channel_id:
        logger.info("Channel filter: only processing channel %s", config.slack_channel_id)
    else:
        logger.info(
            "Channel filter: none (any private channel the bot is invited to)"
        )
    handler = SocketModeHandler(app, config.slack_app_token)
    logger.info("Connecting to Slack — idle until a report message arrives...")
    handler.start()


if __name__ == "__main__":
    main()
