"""Download and convert supported Slack file attachments into LLM-readable text."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from io import BytesIO
from pathlib import Path
from typing import Any

import requests
from openpyxl import load_workbook

logger = logging.getLogger(__name__)

_MAX_FILE_BYTES = 15 * 1024 * 1024
_MAX_WORKBOOK_CELLS = 50_000
_MAX_EXTRACTED_CHARS = 250_000
_MARKDOWN_EXTENSIONS = {".md", ".markdown"}
_WORKBOOK_EXTENSIONS = {".xlsx", ".xslx"}


class FileIngestionError(RuntimeError):
    """Raised when a supported report file cannot be downloaded or parsed."""


@dataclass(frozen=True)
class IngestedFile:
    name: str
    text: str


def _file_name(file: dict[str, Any]) -> str:
    return str(file.get("name") or file.get("title") or "attached-file")


def _extension(file: dict[str, Any]) -> str:
    suffix = Path(_file_name(file)).suffix.lower()
    if suffix:
        return suffix
    filetype = str(file.get("filetype") or "").lower()
    return f".{filetype}" if filetype else ""


def is_supported_report_file(file: dict[str, Any]) -> bool:
    return _extension(file) in _MARKDOWN_EXTENSIONS | _WORKBOOK_EXTENSIONS


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


def _workbook_to_text(data: bytes, name: str) -> str:
    try:
        workbook = load_workbook(BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # openpyxl exposes several format-specific exceptions
        raise FileIngestionError(f"Could not read Excel workbook '{name}': {exc}") from exc

    sections: list[str] = []
    cell_count = 0
    character_count = 0

    try:
        for worksheet in workbook.worksheets:
            rows: list[str] = []
            for row in worksheet.iter_rows(values_only=True):
                values = [_cell_text(value) for value in row]
                while values and not values[-1]:
                    values.pop()
                if not any(value.strip() for value in values):
                    continue

                cell_count += len(values)
                if cell_count > _MAX_WORKBOOK_CELLS:
                    raise FileIngestionError(
                        f"'{name}' contains too many populated cells to process safely."
                    )

                row_text = "\t".join(values)
                character_count += len(row_text) + 1
                if character_count > _MAX_EXTRACTED_CHARS:
                    raise FileIngestionError(
                        f"'{name}' contains too much text to process safely."
                    )
                rows.append(row_text)

            if rows:
                sections.append(
                    f"=== Worksheet: {worksheet.title} ===\n" + "\n".join(rows)
                )
    finally:
        workbook.close()

    if not sections:
        raise FileIngestionError(f"'{name}' contains no readable report data.")
    return "\n\n".join(sections)


def ingest_slack_file(
    file: dict[str, Any], slack_bot_token: str
) -> IngestedFile:
    """Download one supported Slack attachment and convert it to text."""
    name = _file_name(file)
    extension = _extension(file)
    if not is_supported_report_file(file):
        raise FileIngestionError(
            f"Unsupported report file type for '{name}'. Supported: .md and .xlsx."
        )

    data = _download(file, slack_bot_token)
    if extension in _MARKDOWN_EXTENSIONS:
        logger.info("Parsing '%s' as Markdown...", name)
        text = _decode_markdown(data, name)
    else:
        logger.info("Parsing '%s' as Excel workbook...", name)
        text = _workbook_to_text(data, name)

    logger.info("File '%s' parsed to %s characters", name, len(text))
    return IngestedFile(name=name, text=text)
