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

with patch("slack_bolt.App") as _app_class:
    _app_class.return_value.event.return_value = lambda function: function
    import app

from file_ingest import IngestedFile
from models import WeeklyReport


class AppPipelineTests(unittest.TestCase):
    def test_one_zip_attachment_becomes_multiple_sources(self) -> None:
        event = {
            "type": "message",
            "files": [
                {
                    "name": "team-weeklies.zip",
                    "url_private_download": "https://files.slack.test/team.zip",
                }
            ],
        }
        zip_sources = [
            IngestedFile(name="team-weeklies.zip / Jane.xlsx", text="Jane"),
            IngestedFile(name="team-weeklies.zip / John.xlsx", text="John"),
        ]

        with patch("app.ingest_slack_files", return_value=zip_sources):
            sources, errors = app._load_report_sources(event, MagicMock())

        self.assertEqual(sources, zip_sources)
        self.assertEqual(errors, [])

    def test_one_workbook_source_appends_every_extracted_employee(self) -> None:
        reports = [
            WeeklyReport(
                employee_name="Jane",
                team="Platform",
                iso_year_week="2026-W30",
                tasks_completed="A",
            ),
            WeeklyReport(
                employee_name="John",
                team="Growth",
                iso_year_week="2026-W30",
                tasks_completed="B",
            ),
        ]
        sheets_client = MagicMock()
        sheets_client.lookup_employee.return_value = None
        say = MagicMock()
        event = {
            "type": "message",
            "channel": app.config.slack_channel_id or "C1",
            "ts": "1785280000.000100",
            "files": [{"name": "team-weekly.xlsx"}],
        }

        with (
            patch(
                "app._load_report_sources",
                return_value=(
                    [IngestedFile(name="team-weekly.xlsx", text="workbook text")],
                    [],
                ),
            ),
            patch("app.extract_reports", return_value=reports),
            patch("app.get_sheets_client", return_value=sheets_client),
        ):
            app.handle_report_message(
                event=event,
                say=say,
                client=MagicMock(),
                logger=app.logger,
            )

        self.assertEqual(sheets_client.append_report.call_count, 2)
        reply = say.call_args.kwargs["text"]
        self.assertIn("Jane", reply)
        self.assertIn("John", reply)


if __name__ == "__main__":
    unittest.main()
