"""Slack Bolt app (Socket Mode) that turns forwarded weekly-report DMs into
Google Sheet rows.

Pipeline: Slack message -> slack_handler (text/files) -> llm (extraction) ->
sheets (roster lookup + append) -> thread reply.
"""

from __future__ import annotations

import logging
import threading
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
    collect_report_files,
    get_file_links,
    is_ignorable_message,
    merge_demo_links,
    parse_retry_command,
)
from text_cleanup import is_file_reference_only

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("weekly_importer")

app = App(token=config.slack_bot_token)

# In-flight imports keyed by (channel, parent thread_ts). Socket Mode runs
# listeners on a thread pool, so a second "retry" while the first is still
# running must not start another pipeline against the same message.
_ACTIVE_IMPORTS: set[tuple[str, str]] = set()
_ACTIVE_IMPORTS_LOCK = threading.Lock()


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


def _build_replace_reply(enriched: EnrichedReport) -> str:
    return (
        ":arrows_counterclockwise: Replaced the existing row for "
        f"*{enriched.employee_name}* ({enriched.iso_year_week})."
    )


def _file_label(file: dict) -> str:
    return str(file.get("name") or file.get("title") or "file")


def _known_file_names(files: list[dict]) -> tuple[str, ...]:
    names: list[str] = []
    for file in files:
        for key in ("name", "title"):
            value = str(file.get(key) or "").strip()
            if value:
                names.append(value)
    return tuple(names)


def _hydrate_slack_file(file: dict, client) -> dict:
    """Fill in download URLs / filetype for stub file objects from forwarded messages."""
    has_url = bool(file.get("url_private_download") or file.get("url_private"))
    recognized = is_supported_report_file(file)
    file_id = file.get("id")
    if file_id and (not has_url or not recognized):
        response = client.files_info(file=file_id)
        return response.get("file", file)
    return file


def _load_report_sources(event: dict, client) -> tuple[list[IngestedFile], list[str]]:
    """Build LLM input sources from message text and supported attachments."""
    message_text = build_report_text(event)
    candidates = collect_report_files(event)
    logger.info(
        "Building report sources: message text=%s chars, candidate file(s)=%d",
        len(message_text),
        len(candidates),
    )
    sources: list[IngestedFile] = []
    errors: list[str] = []

    hydrated: list[dict] = []
    for file in candidates:
        file_label = _file_label(file)
        try:
            file = _hydrate_slack_file(file, client)
        except Exception as exc:  # Slack SDK exception types vary by adapter
            errors.append(
                f"Could not retrieve Slack metadata for '{file_label}': {exc}"
            )
            continue
        hydrated.append(file)

    supported_files = [file for file in hydrated if is_supported_report_file(file)]
    known_names = _known_file_names(list(candidates) + hydrated)
    logger.info(
        "After hydration: %d supported report file(s) of %d candidate(s)",
        len(supported_files),
        len(hydrated),
    )

    for file_metadata in supported_files:
        file_label = _file_label(file_metadata)
        logger.info("Ingesting attachment '%s'...", file_label)
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
            if message_text and not is_file_reference_only(message_text, known_names):
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
    # A forwarded fallback whose only content is a filename must never become
    # an LLM source — that is how stub Report Log rows were created.
    if not sources and not supported_files and message_text:
        if is_file_reference_only(message_text, known_names):
            label = known_names[0] if known_names else "the attached report"
            errors.append(
                f"Found a reference to '{label}' but could not download it. "
                "Please upload the file directly into the channel."
            )
            logger.warning("File-reference-only message with no ingestible attachment")
        else:
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
def handle_report_message(event: dict, say, client, context=None, **_kwargs) -> None:
    if is_ignorable_message(event):
        return

    bot_user_id = (context or {}).get("bot_user_id")
    if bot_user_id and event.get("user") == bot_user_id:
        return

    if config.slack_channel_id and event.get("channel") != config.slack_channel_id:
        return

    is_thread_reply = bool(
        event.get("thread_ts") and event.get("thread_ts") != event.get("ts")
    )
    command = parse_retry_command(event.get("text"))

    if is_thread_reply:
        if command is None:
            return
        _handle_retry(event, say, client, force=command == "force")
        return

    if command is not None:
        say(
            text=(
                ":information_source: Reply `retry` in an import thread to "
                "re-run that import. Use `retry force` to overwrite an "
                "existing Report Log row."
            ),
            thread_ts=event.get("ts"),
        )
        return

    _run_import(event, say, client, thread_ts=event.get("ts"))


def _handle_retry(event: dict, say, client, *, force: bool) -> None:
    """Fetch the thread parent and re-run the import against that message."""
    thread_ts = event.get("thread_ts")
    channel = event.get("channel")
    try:
        response = client.conversations_replies(
            channel=channel, ts=thread_ts, limit=1, inclusive=True
        )
    except Exception as exc:  # Slack SDK exception types vary by adapter
        logger.warning("Retry: failed to fetch parent message: %s", exc)
        say(
            text=f":x: Could not fetch the original message to retry: {exc}",
            thread_ts=thread_ts,
        )
        return

    messages = response.get("messages") or []
    parent = messages[0] if messages else None
    if not isinstance(parent, dict) or parent.get("ts") != thread_ts:
        say(
            text=":x: Could not find the original message to retry.",
            thread_ts=thread_ts,
        )
        return

    parent = dict(parent)
    parent["channel"] = channel

    if is_ignorable_message(parent):
        say(
            text=":x: The original message is a bot message and cannot be retried.",
            thread_ts=thread_ts,
        )
        return
    if parse_retry_command(parent.get("text")) is not None:
        say(
            text=(
                ":x: The original message is itself a retry command and "
                "cannot be retried."
            ),
            thread_ts=thread_ts,
        )
        return

    logger.info(
        "=== Retry requested (channel=%s, thread_ts=%s, force=%s) ===",
        channel,
        thread_ts,
        force,
    )
    say(text=":arrows_counterclockwise: Retrying import...", thread_ts=thread_ts)
    _run_import(parent, say, client, thread_ts=thread_ts, force=force)


def _run_import(event: dict, say, client, *, thread_ts, force: bool = False) -> None:
    """Extract reports from `event` and write them to the Report Log."""
    channel = str(event.get("channel") or "?")
    import_key = (channel, str(thread_ts or ""))
    with _ACTIVE_IMPORTS_LOCK:
        if import_key in _ACTIVE_IMPORTS:
            say(
                text=":hourglass: An import for this message is already running.",
                thread_ts=thread_ts,
            )
            return
        _ACTIVE_IMPORTS.add(import_key)

    try:
        _run_import_body(event, say, client, thread_ts=thread_ts, force=force)
    finally:
        with _ACTIVE_IMPORTS_LOCK:
            _ACTIVE_IMPORTS.discard(import_key)


def _run_import_body(
    event: dict, say, client, *, thread_ts, force: bool = False
) -> None:
    channel = event.get("channel", "?")
    logger.info(
        "=== Weekly report message received (channel=%s, ts=%s, force=%s) ===",
        channel,
        thread_ts,
        force,
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

    file_links = get_file_links(collect_report_files(event, log=False))
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
    replaced_count = 0
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
            if force:
                if sheets_client.replace_report(enriched):
                    replaced_count += 1
                    logger.info(
                        "Replaced row for %s (%s)",
                        enriched.employee_name,
                        enriched.iso_year_week,
                    )
                    replies.append(_build_replace_reply(enriched))
                    continue
            elif sheets_client.report_exists(
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
        "=== Message done (ts=%s): %d saved, %d replaced, %d skipped (duplicates), "
        "%d error(s), %d total tokens used (prompt=%d, completion=%d) ===",
        thread_ts,
        saved_count,
        replaced_count,
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
