"""Download and convert supported Slack file attachments into LLM-readable text."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from io import BytesIO
from pathlib import Path
from typing import Any
from zipfile import BadZipFile, ZipFile

import requests
from openpyxl import load_workbook
from openpyxl.worksheet.worksheet import Worksheet

from excel_parse import ParsedWorkbook, parse_worksheet_tables
from models import WeeklyReport

logger = logging.getLogger(__name__)

_MAX_FILE_BYTES = 15 * 1024 * 1024
_MAX_ZIP_ENTRIES = 100
_MAX_ZIP_MEMBER_BYTES = 15 * 1024 * 1024
_MAX_ZIP_TOTAL_BYTES = 50 * 1024 * 1024
_MAX_WORKBOOK_CELLS = 50_000
_MAX_EXTRACTED_CHARS = 250_000
_MARKDOWN_EXTENSIONS = {".md", ".markdown"}
_WORKBOOK_EXTENSIONS = {".xlsx", ".xslx"}
_ARCHIVE_EXTENSIONS = {".zip"}


class FileIngestionError(RuntimeError):
    """Raised when a supported report file cannot be downloaded or parsed."""


@dataclass(frozen=True)
class IngestedFile:
    name: str
    text: str
    # Populated when an Excel table maps onto WeeklyReport columns; the pipeline
    # then skips the LLM extraction call for this source.
    reports: tuple[WeeklyReport, ...] = ()
    needs_llm: bool = True


def _file_name(file: dict[str, Any]) -> str:
    return str(file.get("name") or file.get("title") or "attached-file")


def _extension(file: dict[str, Any]) -> str:
    suffix = Path(_file_name(file)).suffix.lower()
    if suffix:
        return suffix
    filetype = str(file.get("filetype") or "").lower()
    return f".{filetype}" if filetype else ""


def is_supported_report_file(file: dict[str, Any]) -> bool:
    return _extension(file) in (
        _MARKDOWN_EXTENSIONS | _WORKBOOK_EXTENSIONS | _ARCHIVE_EXTENSIONS
    )


def _download(file: dict[str, Any], slack_bot_token: str) -> bytes:
    url = file.get("url_private_download") or file.get("url_private")
    if not url:
        raise FileIngestionError(
            f"Slack did not provide a download URL for '{_file_name(file)}'."
        )

    try:
        logger.info("Downloading '%s' from Slack...", _file_name(file))
        response = requests.get(
            str(url),
            headers={"Authorization": f"Bearer {slack_bot_token}"},
            timeout=45,
            stream=True,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise FileIngestionError(
            f"Failed to download '{_file_name(file)}' from Slack: {exc}"
        ) from exc

    declared_size = int(response.headers.get("Content-Length") or 0)
    if declared_size > _MAX_FILE_BYTES:
        raise FileIngestionError(
            f"'{_file_name(file)}' is larger than the {_MAX_FILE_BYTES // 1024 // 1024} MB limit."
        )

    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        size += len(chunk)
        if size > _MAX_FILE_BYTES:
            raise FileIngestionError(
                f"'{_file_name(file)}' is larger than the "
                f"{_MAX_FILE_BYTES // 1024 // 1024} MB limit."
            )
        chunks.append(chunk)
    data = b"".join(chunks)
    logger.info("Downloaded '%s' (%s bytes)", _file_name(file), len(data))
    return data


def _decode_markdown(data: bytes, name: str) -> str:
    for encoding in ("utf-8-sig", "utf-16", "utf-8", "cp1252"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise FileIngestionError(f"Could not determine the text encoding of '{name}'.")

    if not text.strip():
        raise FileIngestionError(f"'{name}' is empty.")
    return text


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _merged_value_lookup(worksheet: Worksheet) -> dict[tuple[int, int], Any]:
    """Map every cell in a merged range to the top-left value.

    MergedCell.value is read-only, so we cannot copy into the other cells.
    Reading through this lookup makes a merged header look like a normal column.
    """
    lookup: dict[tuple[int, int], Any] = {}
    for merged_range in worksheet.merged_cells.ranges:
        value = worksheet.cell(merged_range.min_row, merged_range.min_col).value
        if value is None:
            continue
        for row in range(merged_range.min_row, merged_range.max_row + 1):
            for col in range(merged_range.min_col, merged_range.max_col + 1):
                lookup[(row, col)] = value
    return lookup


def _worksheet_rows(worksheet: Worksheet) -> tuple[list[list[str]], int, int]:
    """Return trimmed string rows plus populated cell/character counts."""
    merged = _merged_value_lookup(worksheet)
    rows: list[list[str]] = []
    cell_count = 0
    character_count = 0
    for row in worksheet.iter_rows():
        values = [
            _cell_text(merged.get((cell.row, cell.column), cell.value)) for cell in row
        ]
        while values and not values[-1].strip():
            values.pop()
        if not any(value.strip() for value in values):
            continue
        cell_count += len(values)
        character_count += sum(len(value) for value in values) + len(values)
        rows.append(values)
    return rows, cell_count, character_count


def _parse_workbook(
    data: bytes, name: str, message_date: datetime | None = None
) -> ParsedWorkbook:
    """Load an .xlsx and convert it into labeled records / structured reports."""
    try:
        # data_only so formula cells yield computed values. Not read-only: we
        # need merged_cells access to survive merged-header layout tweaks.
        workbook = load_workbook(BytesIO(data), data_only=True)
    except Exception as exc:  # openpyxl exposes several format-specific exceptions
        raise FileIngestionError(f"Could not read Excel workbook '{name}': {exc}") from exc

    sheets: list[tuple[str, list[list[str]]]] = []
    cell_count = 0
    character_count = 0
    try:
        for worksheet in workbook.worksheets:
            rows, sheet_cells, sheet_chars = _worksheet_rows(worksheet)
            cell_count += sheet_cells
            character_count += sheet_chars
            if cell_count > _MAX_WORKBOOK_CELLS:
                raise FileIngestionError(
                    f"'{name}' contains too many populated cells to process safely."
                )
            if character_count > _MAX_EXTRACTED_CHARS:
                raise FileIngestionError(
                    f"'{name}' contains too much text to process safely."
                )
            if rows:
                sheets.append((worksheet.title, rows))
    finally:
        workbook.close()

    parsed = parse_worksheet_tables(sheets, message_date=message_date)
    if not parsed.text.strip():
        raise FileIngestionError(f"'{name}' contains no readable report data.")
    return parsed


def _zip_to_files(
    data: bytes,
    archive_name: str,
    message_date: datetime | None = None,
) -> list[IngestedFile]:
    """Read supported report files from an archive without extracting to disk."""
    try:
        archive = ZipFile(BytesIO(data))
    except BadZipFile as exc:
        raise FileIngestionError(f"'{archive_name}' is not a valid ZIP archive.") from exc

    ingested: list[IngestedFile] = []
    total_uncompressed = 0
    try:
        members = [member for member in archive.infolist() if not member.is_dir()]
        if len(members) > _MAX_ZIP_ENTRIES:
            raise FileIngestionError(
                f"'{archive_name}' contains more than {_MAX_ZIP_ENTRIES} files."
            )

        for member in members:
            # Nothing is written to disk, so traversal paths cannot escape anywhere.
            # Path(...) also strips archive folder prefixes from display/source names.
            member_name = Path(member.filename.replace("\\", "/")).name
            if not member_name or member_name.startswith((".", "~$")):
                continue

            extension = Path(member_name).suffix.lower()
            if extension not in _WORKBOOK_EXTENSIONS | _MARKDOWN_EXTENSIONS:
                logger.info(
                    "Skipping unsupported ZIP member '%s' in '%s'",
                    member.filename,
                    archive_name,
                )
                continue
            if member.flag_bits & 0x1:
                raise FileIngestionError(
                    f"'{archive_name}' contains encrypted file '{member_name}', "
                    "which cannot be processed."
                )
            if member.file_size > _MAX_ZIP_MEMBER_BYTES:
                raise FileIngestionError(
                    f"'{member_name}' inside '{archive_name}' exceeds the "
                    f"{_MAX_ZIP_MEMBER_BYTES // 1024 // 1024} MB per-file limit."
                )

            total_uncompressed += member.file_size
            if total_uncompressed > _MAX_ZIP_TOTAL_BYTES:
                raise FileIngestionError(
                    f"Uncompressed files in '{archive_name}' exceed the "
                    f"{_MAX_ZIP_TOTAL_BYTES // 1024 // 1024} MB total limit."
                )

            logger.info(
                "Reading ZIP member '%s' (%s bytes)...",
                member.filename,
                member.file_size,
            )
            member_data = archive.read(member)
            source_name = f"{archive_name} / {member.filename}"
            if extension in _MARKDOWN_EXTENSIONS:
                text = _decode_markdown(member_data, source_name)
                ingested.append(IngestedFile(name=source_name, text=text))
            else:
                parsed = _parse_workbook(member_data, source_name, message_date)
                ingested.append(
                    IngestedFile(
                        name=source_name,
                        text=parsed.text,
                        reports=parsed.reports,
                        needs_llm=parsed.needs_llm,
                    )
                )
    finally:
        archive.close()

    if not ingested:
        raise FileIngestionError(
            f"'{archive_name}' contains no supported .xlsx/.xslx or Markdown files."
        )

    logger.info(
        "ZIP '%s' produced %d report source(s)", archive_name, len(ingested)
    )
    return ingested


def ingest_slack_files(
    file: dict[str, Any],
    slack_bot_token: str,
    message_date: datetime | None = None,
) -> list[IngestedFile]:
    """Download one Slack attachment and return one or more report sources."""
    name = _file_name(file)
    extension = _extension(file)
    if not is_supported_report_file(file):
        raise FileIngestionError(
            f"Unsupported report file type for '{name}'. "
            "Supported: .md, .xlsx, and .zip."
        )

    data = _download(file, slack_bot_token)
    if extension in _ARCHIVE_EXTENSIONS:
        logger.info("Parsing '%s' as ZIP archive...", name)
        return _zip_to_files(data, name, message_date)
    if extension in _MARKDOWN_EXTENSIONS:
        logger.info("Parsing '%s' as Markdown...", name)
        text = _decode_markdown(data, name)
        logger.info("File '%s' parsed to %s characters", name, len(text))
        return [IngestedFile(name=name, text=text)]

    logger.info("Parsing '%s' as Excel workbook...", name)
    parsed = _parse_workbook(data, name, message_date)
    logger.info(
        "File '%s' parsed to %s characters (%d structured report(s), needs_llm=%s)",
        name,
        len(parsed.text),
        len(parsed.reports),
        parsed.needs_llm,
    )
    return [
        IngestedFile(
            name=name,
            text=parsed.text,
            reports=parsed.reports,
            needs_llm=parsed.needs_llm,
        )
    ]


def ingest_slack_file(
    file: dict[str, Any],
    slack_bot_token: str,
    message_date: datetime | None = None,
) -> IngestedFile:
    """Backward-compatible helper for attachments that produce one source."""
    ingested = ingest_slack_files(file, slack_bot_token, message_date)
    if len(ingested) != 1:
        raise FileIngestionError(
            f"'{_file_name(file)}' contains {len(ingested)} report files; "
            "use ingest_slack_files() for archives."
        )
    return ingested[0]
