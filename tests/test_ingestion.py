from __future__ import annotations

import unittest
from io import BytesIO
from unittest.mock import patch

from openpyxl import Workbook

from file_ingest import ingest_slack_file, is_supported_report_file
from markdown_format import convert_markdown
from models import EnrichedReport, ReportBatch, WeeklyReport


class _Response:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.headers = {"Content-Length": str(len(content))}

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size: int):
        del chunk_size
        yield self.content


class IngestionTests(unittest.TestCase):
    def test_emojis_are_removed_but_markdown_and_lines_remain(self) -> None:
        report = WeeklyReport(
            employee_name="Jane 👩‍💻",
            team="Platform 🚀",
            iso_year_week="2026-W30",
            tasks_completed="📌 *Completed*\n- shipped feature ✅",
        )

        self.assertEqual(report.employee_name, "Jane")
        self.assertEqual(report.team, "Platform")
        self.assertEqual(report.tasks_completed, "*Completed*\n- shipped feature")

        plain, spans = convert_markdown(report.tasks_completed)
        self.assertEqual(plain, "Completed\n- shipped feature")
        self.assertTrue(spans[0].bold)

    def test_enriched_roster_values_are_also_cleaned(self) -> None:
        report = EnrichedReport(
            iso_year_week="2026-W30",
            employee_name="Jane 🚀",
            team="Platform ✨",
            tasks_completed="Done ✅",
        )
        self.assertEqual(report.to_row()[1:4], ["Jane", "Platform", "Done"])

    def test_markdown_file_download_and_decode(self) -> None:
        content = "# Weekly\n\n*Done*\n- task A".encode()
        file = {
            "name": "weekly.md",
            "url_private_download": "https://files.slack.test/weekly.md",
        }

        with patch("file_ingest.requests.get", return_value=_Response(content)):
            result = ingest_slack_file(file, "xoxb-test")

        self.assertEqual(result.name, "weekly.md")
        self.assertEqual(result.text, "# Weekly\n\n*Done*\n- task A")

    def test_xlsx_bundle_is_rendered_with_all_worksheets_and_rows(self) -> None:
        workbook = Workbook()
        first = workbook.active
        first.title = "Platform"
        first.append(["Name", "Completed", "Next"])
        first.append(["Jane Doe", "Feature A", "Feature B"])
        second = workbook.create_sheet("Growth")
        second.append(["John Smith", "Campaign A", "Campaign B"])
        buffer = BytesIO()
        workbook.save(buffer)
        workbook.close()

        file = {
            "name": "team-weekly.xlsx",
            "url_private_download": "https://files.slack.test/team-weekly.xlsx",
        }
        with patch(
            "file_ingest.requests.get", return_value=_Response(buffer.getvalue())
        ):
            result = ingest_slack_file(file, "xoxb-test")

        self.assertIn("=== Worksheet: Platform ===", result.text)
        self.assertIn("Jane Doe\tFeature A\tFeature B", result.text)
        self.assertIn("=== Worksheet: Growth ===", result.text)
        self.assertIn("John Smith\tCampaign A\tCampaign B", result.text)

    def test_common_xslx_filename_typo_is_accepted(self) -> None:
        self.assertTrue(is_supported_report_file({"name": "bundle.xslx"}))

    def test_report_batch_keeps_employees_separate(self) -> None:
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
        self.assertEqual([report.employee_name for report in batch.reports], ["Jane", "John"])


if __name__ == "__main__":
    unittest.main()
