from __future__ import annotations

import unittest

from slack_handler import collect_report_files, parse_retry_command
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


class ParseRetryCommandTests(unittest.TestCase):
    def test_exact_retry_words_are_recognized(self) -> None:
        self.assertEqual(parse_retry_command("retry"), "retry")
        self.assertEqual(parse_retry_command("Retry"), "retry")
        self.assertEqual(parse_retry_command("重试"), "retry")
        self.assertEqual(parse_retry_command("<@U123> retry"), "retry")

    def test_force_variants_are_recognized(self) -> None:
        self.assertEqual(parse_retry_command("retry force"), "force")
        self.assertEqual(parse_retry_command("retry!"), "force")
        self.assertEqual(parse_retry_command("retry！"), "force")
        self.assertEqual(parse_retry_command("重试 强制"), "force")

    def test_sentences_containing_retry_are_ignored(self) -> None:
        self.assertIsNone(parse_retry_command("retrying the deploy"))
        self.assertIsNone(parse_retry_command("please retry later"))
        self.assertIsNone(parse_retry_command(""))
        self.assertIsNone(parse_retry_command(None))
        self.assertIsNone(parse_retry_command("retry this import please"))


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
