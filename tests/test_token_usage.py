from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test")
os.environ.setdefault("SLACK_APP_TOKEN", "xapp-test")
os.environ.setdefault(
    "AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com"
)
os.environ.setdefault("AZURE_OPENAI_API_KEY", "test-key")
os.environ.setdefault("AZURE_OPENAI_DEPLOYMENT", "gpt-5")
os.environ.setdefault("GOOGLE_SPREADSHEET_ID", "test-sheet")

import llm  # noqa: E402
from llm import ReportExtractor, TokenUsage, get_token_usage, reset_token_usage  # noqa: E402
from models import EmployeeMatch, ReportBatch, RosterEntry, WeeklyReport  # noqa: E402


def _usage(prompt: int, completion: int, total: int | None = None) -> MagicMock:
    usage = MagicMock()
    usage.prompt_tokens = prompt
    usage.completion_tokens = completion
    usage.total_tokens = total if total is not None else prompt + completion
    return usage


class TokenUsageModelTests(unittest.TestCase):
    def test_from_completion_reads_numeric_fields(self) -> None:
        completion = MagicMock(usage=_usage(100, 20, 120))
        usage = TokenUsage.from_completion(completion)
        self.assertEqual(usage, TokenUsage(prompt_tokens=100, completion_tokens=20, total_tokens=120))

    def test_from_completion_defaults_when_usage_missing(self) -> None:
        completion = MagicMock(usage=None)
        usage = TokenUsage.from_completion(completion)
        self.assertEqual(usage, TokenUsage())

    def test_from_completion_coerces_non_numeric_to_zero(self) -> None:
        bad_usage = MagicMock()
        bad_usage.prompt_tokens = "not-a-number"
        bad_usage.completion_tokens = None
        bad_usage.total_tokens = object()
        completion = MagicMock(usage=bad_usage)
        usage = TokenUsage.from_completion(completion)
        self.assertEqual(usage, TokenUsage())

    def test_addition_sums_all_fields(self) -> None:
        a = TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15)
        b = TokenUsage(prompt_tokens=3, completion_tokens=2, total_tokens=5)
        self.assertEqual(a + b, TokenUsage(prompt_tokens=13, completion_tokens=7, total_tokens=20))


class TokenUsageAccumulationTests(unittest.TestCase):
    def test_extract_many_and_match_employee_name_accumulate_into_shared_total(self) -> None:
        reset_token_usage()

        batch = ReportBatch(
            reports=[
                WeeklyReport(
                    employee_name="Jane",
                    iso_year_week="2026-W30",
                    tasks_completed="A",
                )
            ]
        )
        extraction_message = MagicMock(parsed=batch, refusal=None)
        extraction_completion = MagicMock(
            choices=[MagicMock(message=extraction_message)],
            usage=_usage(200, 50),
        )
        extraction_client = MagicMock()
        extraction_client.chat.completions.parse.return_value = extraction_completion

        extractor = ReportExtractor.__new__(ReportExtractor)
        extractor._client = extraction_client
        extractor.extract_many(
            "Jane\tA",
            datetime(2026, 7, 22, tzinfo=timezone.utc),
            source_name="weekly.md",
        )

        match_message = MagicMock(
            parsed=EmployeeMatch(matched_name="Jane Doe", reasoning="close enough"),
            refusal=None,
        )
        match_completion = MagicMock(
            choices=[MagicMock(message=match_message)],
            usage=_usage(30, 10),
        )
        match_client = MagicMock()
        match_client.chat.completions.parse.return_value = match_completion

        with patch("llm._get_client", return_value=match_client):
            matched = llm.match_employee_name("Jane", [RosterEntry(name="Jane Doe")])

        self.assertEqual(matched, "Jane Doe")

        total = get_token_usage()
        self.assertEqual(total.prompt_tokens, 230)
        self.assertEqual(total.completion_tokens, 60)
        self.assertEqual(total.total_tokens, 290)

    def test_reset_token_usage_clears_previous_total(self) -> None:
        reset_token_usage()
        llm._record_usage(TokenUsage(10, 5, 15), context="test")
        self.assertEqual(get_token_usage().total_tokens, 15)

        reset_token_usage()
        self.assertEqual(get_token_usage(), TokenUsage())


if __name__ == "__main__":
    unittest.main()
