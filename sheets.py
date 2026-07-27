"""Google Sheets access via the OAuth 2.0 Desktop-app flow.

Authenticates as the sheet owner (you) rather than a service account, since
Google blocks service-account key creation by default on personal accounts.
See README.md for the one-time setup and consent steps.
"""

from __future__ import annotations

import difflib
import logging
import re
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config import config
from llm import match_employee_name
from markdown_format import convert_markdown, spans_to_text_format_runs
from models import EnrichedReport, RosterEntry

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Roster tab columns A-D.
_ROSTER_COL_NAME, _ROSTER_COL_LOCATION, _ROSTER_COL_TEAM, _ROSTER_COL_EMAIL = range(4)

# Fuzzy-match threshold for employee-name lookups (0..1, higher = stricter).
_FUZZY_MATCH_CUTOFF = 0.8

# Report Log columns B..J correspond 1:1 with EnrichedReport.to_row(); column B
# is sheet column index 1 (0-based, since A=0).
_REPORT_LOG_FIRST_COLUMN_INDEX = 1

_ROW_INDEX_RE = re.compile(r"![A-Za-z]+(\d+)")


class SheetsError(RuntimeError):
    """Raised for any Google Sheets access failure."""


def _quote_sheet_name(name: str) -> str:
    escaped = name.replace("'", "''")
    return f"'{escaped}'"


def _normalize_name(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())


def _load_credentials() -> Credentials:
    token_path = Path(config.google_token_path)
    client_path = Path(config.google_oauth_client_path)

    creds: Credentials | None = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            token_path.write_text(creds.to_json())
            return creds
        except Exception:
            logger.warning(
                "Stored Google credentials could not be refreshed; "
                "re-running the consent flow."
            )

    if not client_path.exists():
        raise SheetsError(
            f"Google OAuth client file not found at '{client_path}'. "
            "Download it from Google Cloud Console (see README.md) and place "
            "it there, or point GOOGLE_OAUTH_CLIENT_PATH at its location."
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(client_path), SCOPES)
    creds = flow.run_local_server(port=0)

    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json())
    logger.info("Saved Google OAuth refresh token to '%s'.", token_path)
    return creds


class SheetsClient:
    """Wraps Roster lookups and Report Log appends against a single spreadsheet."""

    def __init__(self) -> None:
        creds = _load_credentials()
        self._service = build("sheets", "v4", credentials=creds)
        self._spreadsheet_id = config.google_spreadsheet_id
        self._roster_cache: list[RosterEntry] | None = None
        self._sheet_id_cache: dict[str, int] = {}

    def _values(self):
        return self._service.spreadsheets().values()

    def _fetch_roster(self) -> list[RosterEntry]:
        sheet_range = f"{_quote_sheet_name(config.roster_sheet_name)}!A2:D"
        try:
            result = (
                self._values()
                .get(spreadsheetId=self._spreadsheet_id, range=sheet_range)
                .execute()
            )
        except HttpError as exc:
            raise SheetsError(f"Failed to read the '{config.roster_sheet_name}' tab: {exc}") from exc

        entries: list[RosterEntry] = []
        for row in result.get("values", []):
            name = row[_ROSTER_COL_NAME].strip() if len(row) > _ROSTER_COL_NAME else ""
            if not name:
                continue
            entries.append(
                RosterEntry(
                    name=name,
                    location=(
                        row[_ROSTER_COL_LOCATION].strip()
                        if len(row) > _ROSTER_COL_LOCATION
                        else ""
                    ),
                    team=(
                        row[_ROSTER_COL_TEAM].strip()
                        if len(row) > _ROSTER_COL_TEAM
                        else ""
                    ),
                    email=(
                        row[_ROSTER_COL_EMAIL].strip()
                        if len(row) > _ROSTER_COL_EMAIL
                        else ""
                    ),
                )
            )
        return entries

    def _roster(self, force_refresh: bool = False) -> list[RosterEntry]:
        if force_refresh or self._roster_cache is None:
            self._roster_cache = self._fetch_roster()
        return self._roster_cache

    def lookup_employee(
        self,
        employee_name: str,
        hint_team: str | None = None,
        force_refresh: bool = False,
    ) -> RosterEntry | None:
        """Find a Roster entry matching `employee_name`.

        Tries, in order: an exact case-insensitive match, a cheap character-level
        fuzzy match (for typos), and finally an LLM-based match that can handle
        partial names/aliases (e.g. a Slack handle missing the surname) and
        script/regional variants (e.g. Traditional vs Simplified Chinese) that
        the earlier passes miss.
        """
        roster = self._roster(force_refresh=force_refresh)
        target = _normalize_name(employee_name)

        for entry in roster:
            if _normalize_name(entry.name) == target:
                return entry

        names = [entry.name for entry in roster]
        normalized_to_entry = {_normalize_name(n): e for n, e in zip(names, roster)}
        close = difflib.get_close_matches(
            target, list(normalized_to_entry.keys()), n=1, cutoff=_FUZZY_MATCH_CUTOFF
        )
        if close:
            matched = normalized_to_entry[close[0]]
            logger.info(
                "Fuzzy-matched employee '%s' to roster entry '%s'.",
                employee_name,
                matched.name,
            )
            return matched

        llm_matched_name = match_employee_name(employee_name, roster, hint_team=hint_team)
        if llm_matched_name:
            for entry in roster:
                if entry.name == llm_matched_name:
                    return entry

        return None

    def _get_sheet_id(self, sheet_name: str) -> int:
        if sheet_name not in self._sheet_id_cache:
            try:
                metadata = (
                    self._service.spreadsheets()
                    .get(spreadsheetId=self._spreadsheet_id, fields="sheets.properties")
                    .execute()
                )
            except HttpError as exc:
                raise SheetsError(f"Failed to read spreadsheet metadata: {exc}") from exc

            for sheet in metadata.get("sheets", []):
                props = sheet.get("properties", {})
                title = props.get("title")
                if title is not None and "sheetId" in props:
                    self._sheet_id_cache[title] = props["sheetId"]

        if sheet_name not in self._sheet_id_cache:
            raise SheetsError(
                f"Sheet tab '{sheet_name}' was not found in the spreadsheet."
            )
        return self._sheet_id_cache[sheet_name]

    def append_report(self, report: EnrichedReport) -> None:
        """Append `report` as a new row, rendering any markdown it contains
        (e.g. `*bold*`) as actual Google Sheets rich text instead of raw markup.
        """
        raw_row = report.to_row()
        plain_row: list[str] = []
        spans_by_column: dict[int, list] = {}

        for column_offset, value in enumerate(raw_row):
            plain_text, spans = convert_markdown(value)
            plain_row.append(plain_text)
            if spans:
                spans_by_column[column_offset] = spans

        sheet_range = f"{_quote_sheet_name(config.report_log_sheet_name)}!B:J"
        try:
            append_result = (
                self._values()
                .append(
                    spreadsheetId=self._spreadsheet_id,
                    range=sheet_range,
                    valueInputOption="USER_ENTERED",
                    insertDataOption="INSERT_ROWS",
                    body={"values": [plain_row]},
                )
                .execute()
            )
        except HttpError as exc:
            raise SheetsError(
                f"Failed to append to the '{config.report_log_sheet_name}' tab: {exc}"
            ) from exc

        if spans_by_column:
            try:
                self._apply_rich_text_formatting(append_result, plain_row, spans_by_column)
            except Exception:
                # The row is already saved with correct plain text at this point;
                # a formatting failure shouldn't be treated as a failed import.
                logger.exception(
                    "Saved the report, but failed to apply rich-text formatting "
                    "to it."
                )

    def _apply_rich_text_formatting(
        self,
        append_result: dict,
        plain_row: list[str],
        spans_by_column: dict[int, list],
    ) -> None:
        updated_range = append_result.get("updates", {}).get("updatedRange", "")
        match = _ROW_INDEX_RE.search(updated_range)
        if not match:
            logger.warning(
                "Could not determine the appended row number from '%s'; "
                "skipping rich-text formatting.",
                updated_range,
            )
            return
        row_index = int(match.group(1))

        sheet_id = self._get_sheet_id(config.report_log_sheet_name)

        requests = []
        for column_offset, spans in spans_by_column.items():
            text = plain_row[column_offset]
            runs = spans_to_text_format_runs(spans, len(text))
            if not runs:
                continue
            column_index = _REPORT_LOG_FIRST_COLUMN_INDEX + column_offset
            requests.append(
                {
                    "updateCells": {
                        "rows": [
                            {
                                "values": [
                                    {
                                        "userEnteredValue": {"stringValue": text},
                                        "textFormatRuns": runs,
                                    }
                                ]
                            }
                        ],
                        "fields": "userEnteredValue,textFormatRuns",
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": row_index - 1,
                            "endRowIndex": row_index,
                            "startColumnIndex": column_index,
                            "endColumnIndex": column_index + 1,
                        },
                    }
                }
            )

        if not requests:
            return

        try:
            self._service.spreadsheets().batchUpdate(
                spreadsheetId=self._spreadsheet_id, body={"requests": requests}
            ).execute()
        except HttpError as exc:
            raise SheetsError(f"Failed to apply rich-text formatting: {exc}") from exc


_client: SheetsClient | None = None


def get_sheets_client() -> SheetsClient:
    global _client
    if _client is None:
        _client = SheetsClient()
    return _client
