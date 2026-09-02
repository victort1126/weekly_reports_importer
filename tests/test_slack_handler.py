from __future__ import annotations

import unittest

from slack_handler import collect_report_files
from text_cleanup import is_file_reference_only


class CollectReportFilesTests(unittest.TestCase):
    def test_forwarded_attachment_files_are_collected_when_event_files_empty(self) -> None:
        nested = {
            "id": "F123",
            "name": "工作周报.xlsx",
            "url_private_download": "https://files.slack.test/weekly.xlsx",
        }
        event = {
            "files": [],
            "attachments": [
                {
                    "is_share": True,
                    "is_msg_unfurl": True,
                    "fallback": (
                        "[August 28th, 2026 6:56 PM] kuangjinming: 工作周报.xlsx"
                    ),
                    "files": [nested],
                }
            ],
        }

        collected = collect_report_files(event, log=False)

        self.assertEqual(collected, [nested])

    def test_file_present_at_both_levels_is_returned_once(self) -> None:
        shared = {
            "id": "F123",
            "name": "工作周报.xlsx",
            "url_private_download": "https://files.slack.test/weekly.xlsx",
        }
        event = {
            "files": [shared],
            "attachments": [{"is_share": True, "files": [dict(shared)]}],
        }

        collected = collect_report_files(event, log=False)

        self.assertEqual(len(collected), 1)
        self.assertEqual(collected[0]["id"], "F123")

    def test_attachment_without_share_flags_still_contributes_files(self) -> None:
        nested = {"id": "F9", "name": "notes.md"}
        event = {"attachments": [{"files": [nested]}]}

        collected = collect_report_files(event, log=False)

        self.assertEqual(collected, [nested])


class FileReferenceOnlyTests(unittest.TestCase):
    def test_forwarded_fallback_string_is_a_file_reference(self) -> None:
        text = "[August 28th, 2026 6:56 PM] kuangjinming: 工作周报.xlsx"
        self.assertTrue(is_file_reference_only(text))
        self.assertTrue(is_file_reference_only(text, ("工作周报.xlsx",)))

    def test_bare_filename_is_a_file_reference(self) -> None:
        self.assertTrue(is_file_reference_only("工作周报.xlsx"))

    def test_real_report_text_is_not_a_file_reference(self) -> None:
        self.assertFalse(
            is_file_reference_only("本周完成了呼梯日志全链路开发，并修复了调度器栈溢出。")
        )


if __name__ == "__main__":
    unittest.main()
