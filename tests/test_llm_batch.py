from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock

# llm.py imports validated application config at module load time.
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test")
os.environ.setdefault("SLACK_APP_TOKEN", "xapp-test")
os.environ.setdefault(
    "AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com"
)
os.environ.setdefault("AZURE_OPENAI_API_KEY", "test-key")
os.environ.setdefault("AZURE_OPENAI_DEPLOYMENT", "gpt-5")
os.environ.setdefault("GOOGLE_SPREADSHEET_ID", "test-sheet")

from llm import ReportExtractor  # noqa: E402
from models import ReportBatch, WeeklyReport  # noqa: E402


class BatchExtractionTests(unittest.TestCase):
    def test_extract_many_returns_every_employee_report(self) -> None:
        batch = ReportBatch(
            reports=[
                WeeklyReport(
                    employee_name="Jane",
                    iso_year_week="2026-W30",
                    tasks_completed="A",
                ),
                WeeklyReport(
                    employee_name="John",
                    iso_year_week="2026-W30",
                    tasks_completed="B",
                ),
            ]
        )
        message = MagicMock(parsed=batch, refusal=None)
        completion = MagicMock(choices=[MagicMock(message=message)])
        client = MagicMock()
        client.chat.completions.parse.return_value = completion

        extractor = ReportExtractor.__new__(ReportExtractor)
        extractor._client = client
        reports = extractor.extract_many(
            "Jane\tA\nJohn\tB",
            datetime(2026, 7, 22, tzinfo=timezone.utc),
            source_name="team-weekly.xlsx",
        )

        self.assertEqual([report.employee_name for report in reports], ["Jane", "John"])
        kwargs = client.chat.completions.parse.call_args.kwargs
        self.assertIs(kwargs["response_format"], ReportBatch)
        self.assertIn("team-weekly.xlsx", kwargs["messages"][1]["content"])

    def test_extract_many_drops_filename_stub_reports(self) -> None:
        batch = ReportBatch(
            reports=[
                WeeklyReport(
                    employee_name="邝锦明",
                    iso_year_week="2026-W36",
                    tasks_completed="工作周报.xlsx",
                ),
                WeeklyReport(
                    employee_name="邝锦明",
                    iso_year_week="2026-W35",
                    tasks_completed="呼梯日志全链路",
                ),
            ]
        )
        message = MagicMock(parsed=batch, refusal=None)
        completion = MagicMock(choices=[MagicMock(message=message)])
        extractor = ReportExtractor.__new__(ReportExtractor)
        extractor._client = MagicMock()
        extractor._client.chat.completions.parse.return_value = completion

        reports = extractor.extract_many(
            "呼梯日志全链路",
            datetime(2026, 9, 2, tzinfo=timezone.utc),
            source_name="工作周报.xlsx",
        )

        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].tasks_completed, "呼梯日志全链路")
        self.assertEqual(reports[0].iso_year_week, "2026-W35")

    def test_extract_many_drops_bracketed_forward_filename_stub(self) -> None:
        batch = ReportBatch(
            reports=[
                WeeklyReport(
                    employee_name="邝锦明",
                    iso_year_week="2026-W35",
                    tasks_completed=(
                        "[August 28th, 2026 6:56 PM] kuangjinming: 工作周报.xlsx"
                    ),
                )
            ]
        )
        message = MagicMock(parsed=batch, refusal=None)
        completion = MagicMock(choices=[MagicMock(message=message)])
        extractor = ReportExtractor.__new__(ReportExtractor)
        extractor._client = MagicMock()
        extractor._client.chat.completions.parse.return_value = completion

        from llm import ReportExtractionError

        with self.assertRaises(ReportExtractionError) as raised:
            extractor.extract_many(
                "[August 28th, 2026 6:56 PM] kuangjinming: 工作周报.xlsx",
                datetime(2026, 8, 28, tzinfo=timezone.utc),
                source_name="工作周报.xlsx",
            )
        self.assertIn("filename", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
