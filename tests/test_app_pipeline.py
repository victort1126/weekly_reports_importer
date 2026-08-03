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
from llm import TokenUsage
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
        sheets_client.report_exists.return_value = False
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

    def test_existing_week_and_employee_are_skipped(self) -> None:
        reports = [
            WeeklyReport(
                employee_name="Jane",
                team="Platform",
                iso_year_week="2026-W29",
                tasks_completed="Old week",
            ),
            WeeklyReport(
                employee_name="Jane",
                team="Platform",
                iso_year_week="2026-W30",
                tasks_completed="New week",
            ),
        ]
        sheets_client = MagicMock()
        sheets_client.lookup_employee.return_value = None
        sheets_client.report_exists.side_effect = (
            lambda week, name: week == "2026-W29" and name == "Jane"
        )
        say = MagicMock()
        event = {
            "type": "message",
            "channel": app.config.slack_channel_id or "C1",
            "ts": "1785280000.000100",
            "text": "accumulative workbook",
        }

        with (
            patch(
                "app._load_report_sources",
                return_value=(
                    [IngestedFile(name="accum.xlsx", text="workbook text")],
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
            )

        self.assertEqual(sheets_client.append_report.call_count, 1)
        saved = sheets_client.append_report.call_args.args[0]
        self.assertEqual(saved.iso_year_week, "2026-W30")
        reply = say.call_args.kwargs["text"]
        self.assertIn("Skipped", reply)
        self.assertIn("2026-W29", reply)
        self.assertIn("Logged weekly report", reply)
        self.assertIn("2026-W30", reply)

    def test_slack_reply_reports_total_tokens_used(self) -> None:
        report = WeeklyReport(
            employee_name="Jane",
            team="Platform",
            iso_year_week="2026-W30",
            tasks_completed="A",
        )
        sheets_client = MagicMock()
        sheets_client.lookup_employee.return_value = None
        sheets_client.report_exists.return_value = False
        say = MagicMock()
        event = {
            "type": "message",
            "channel": app.config.slack_channel_id or "C1",
            "ts": "1785280000.000100",
            "text": "weekly report",
        }

        def fake_extract_reports(*_args, **_kwargs):
            import llm

            llm._record_usage(
                TokenUsage(prompt_tokens=100, completion_tokens=40, total_tokens=140),
                context="test extraction",
            )
            return [report]

        with (
            patch(
                "app._load_report_sources",
                return_value=([IngestedFile(name="weekly.md", text="text")], []),
            ),
            patch("app.extract_reports", side_effect=fake_extract_reports),
            patch("app.get_sheets_client", return_value=sheets_client),
        ):
            app.handle_report_message(
                event=event,
                say=say,
                client=MagicMock(),
            )

        reply = say.call_args.kwargs["text"]
        self.assertIn("Tokens used for this import", reply)
        self.assertIn("140", reply)
        self.assertIn("prompt: 100", reply)
        self.assertIn("completion: 40", reply)


if __name__ == "__main__":
    unittest.main()
