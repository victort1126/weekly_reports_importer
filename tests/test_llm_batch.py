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


if __name__ == "__main__":
    unittest.main()
