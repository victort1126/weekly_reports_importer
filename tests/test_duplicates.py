from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test")
os.environ.setdefault("SLACK_APP_TOKEN", "xapp-test")
os.environ.setdefault(
    "AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com"
)
os.environ.setdefault("AZURE_OPENAI_API_KEY", "test-key")
os.environ.setdefault("AZURE_OPENAI_DEPLOYMENT", "gpt-5")
os.environ.setdefault("GOOGLE_SPREADSHEET_ID", "test-sheet")

from models import EnrichedReport  # noqa: E402
import sheets  # noqa: E402


class DuplicateDetectionTests(unittest.TestCase):
    def test_report_exists_matches_week_and_name_case_insensitively(self) -> None:
        with (
            patch("sheets._load_credentials", return_value=MagicMock()),
            patch("sheets.build") as mock_build,
        ):
            mock_service = MagicMock()
            mock_build.return_value = mock_service
            mock_service.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = {
                "values": [
                    ["2026-W29", "Jane Doe"],
                    ["2026-W30", "John Smith"],
                    ["", ""],
                ]
            }
            client = sheets.SheetsClient()

            self.assertTrue(client.report_exists("2026-W29", "jane doe"))
            self.assertTrue(client.report_exists("2026-w30", "John  Smith"))
            self.assertFalse(client.report_exists("2026-W31", "Jane Doe"))
            self.assertFalse(client.report_exists("2026-W29", "Someone Else"))

    def test_append_updates_duplicate_cache_for_same_run(self) -> None:
        with (
            patch("sheets._load_credentials", return_value=MagicMock()),
            patch("sheets.build") as mock_build,
        ):
            mock_service = MagicMock()
            mock_build.return_value = mock_service
            mock_service.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = {
                "values": [["2026-W29", "Jane Doe"]]
            }
            mock_service.spreadsheets.return_value.values.return_value.append.return_value.execute.return_value = {
                "updates": {"updatedRange": "'Report Log'!B5:J5"}
            }
            client = sheets.SheetsClient()

            self.assertFalse(client.report_exists("2026-W30", "Jane Doe"))
            client.append_report(
                EnrichedReport(
                    iso_year_week="2026-W30",
                    employee_name="Jane Doe",
                    team="Platform",
                    tasks_completed="New work",
                )
            )
            self.assertTrue(client.report_exists("2026-W30", "Jane Doe"))

    def test_find_report_row_returns_sheet_row_number(self) -> None:
        with (
            patch("sheets._load_credentials", return_value=MagicMock()),
            patch("sheets.build") as mock_build,
        ):
            mock_service = MagicMock()
            mock_build.return_value = mock_service
            mock_service.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = {
                "values": [
                    ["2026-W29", "Jane Doe"],
                    ["2026-W30", "John Smith"],
                ]
            }
            client = sheets.SheetsClient()

            self.assertEqual(client.find_report_row("2026-W29", "jane doe"), 2)
            self.assertEqual(client.find_report_row("2026-W30", "John  Smith"), 3)
            self.assertIsNone(client.find_report_row("2026-W31", "Jane Doe"))

    def test_replace_report_updates_the_matching_row(self) -> None:
        with (
            patch("sheets._load_credentials", return_value=MagicMock()),
            patch("sheets.build") as mock_build,
        ):
            mock_service = MagicMock()
            mock_build.return_value = mock_service
            values_api = mock_service.spreadsheets.return_value.values.return_value
            values_api.get.return_value.execute.return_value = {
                "values": [["2026-W29", "Jane Doe"]]
            }
            values_api.update.return_value.execute.return_value = {}
            client = sheets.SheetsClient()

            replaced = client.replace_report(
                EnrichedReport(
                    iso_year_week="2026-W29",
                    employee_name="Jane Doe",
                    team="Platform",
                    tasks_completed="Corrected work",
                )
            )

            self.assertTrue(replaced)
            values_api.update.assert_called_once()
            update_kwargs = values_api.update.call_args.kwargs
            self.assertIn("B2:J2", update_kwargs["range"])
            self.assertEqual(update_kwargs["body"]["values"][0][3], "Corrected work")

    def test_replace_report_returns_false_when_row_missing(self) -> None:
        with (
            patch("sheets._load_credentials", return_value=MagicMock()),
            patch("sheets.build") as mock_build,
        ):
            mock_service = MagicMock()
            mock_build.return_value = mock_service
            values_api = mock_service.spreadsheets.return_value.values.return_value
            values_api.get.return_value.execute.return_value = {"values": []}
            client = sheets.SheetsClient()

            replaced = client.replace_report(
                EnrichedReport(
                    iso_year_week="2026-W29",
                    employee_name="Jane Doe",
                    team="Platform",
                    tasks_completed="New work",
                )
            )

            self.assertFalse(replaced)
            values_api.update.assert_not_called()


if __name__ == "__main__":
    unittest.main()
