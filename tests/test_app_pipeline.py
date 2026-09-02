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

    def test_filename_only_slack_text_is_not_used_as_report_context(self) -> None:
        event = {
            "type": "message",
            "text": "工作周报.xlsx",
            "files": [
                {
                    "name": "工作周报.xlsx",
                    "url_private_download": "https://files.slack.test/weekly.xlsx",
                }
            ],
        }
        ingested = IngestedFile(
            name="工作周报.xlsx",
            text="填写者: 邝锦明\n本周工作总结: 呼梯日志",
            reports=(),
            needs_llm=True,
        )

        with patch("app.ingest_slack_files", return_value=[ingested]):
            sources, errors = app._load_report_sources(event, MagicMock())

        self.assertEqual(errors, [])
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0].text, ingested.text)
        self.assertNotIn("Slack message context", sources[0].text)

    def test_forwarded_attachment_workbook_is_ingested(self) -> None:
        event = {
            "type": "message",
            "files": [],
            "attachments": [
                {
                    "is_share": True,
                    "fallback": (
                        "[August 28th, 2026 6:56 PM] kuangjinming: 工作周报.xlsx"
                    ),
                    "files": [
                        {
                            "id": "F123",
                            "name": "工作周报.xlsx",
                            "url_private_download": (
                                "https://files.slack.test/weekly.xlsx"
                            ),
                        }
                    ],
                }
            ],
        }
        report = WeeklyReport(
            employee_name="邝锦明",
            iso_year_week="2026-W35",
            tasks_completed="呼梯日志全链路",
        )
        ingested = IngestedFile(
            name="工作周报.xlsx",
            text="workbook",
            reports=(report,),
            needs_llm=False,
        )

        with patch("app.ingest_slack_files", return_value=[ingested]) as ingest:
            sources, errors = app._load_report_sources(event, MagicMock())

        self.assertEqual(errors, [])
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0].name, "工作周报.xlsx")
        self.assertEqual(sources[0].reports, (report,))
        self.assertFalse(sources[0].needs_llm)
        ingest.assert_called_once()

    def test_forwarded_workbook_saves_table_extracted_rows(self) -> None:
        event = {
            "type": "message",
            "channel": app.config.slack_channel_id or "C1",
            "ts": "1785280000.000100",
            "files": [],
            "attachments": [
                {
                    "is_share": True,
                    "fallback": (
                        "[August 28th, 2026 6:56 PM] kuangjinming: 工作周报.xlsx"
                    ),
                    "files": [
                        {
                            "id": "F123",
                            "name": "工作周报.xlsx",
                            "url_private_download": (
                                "https://files.slack.test/weekly.xlsx"
                            ),
                        }
                    ],
                }
            ],
        }
        report = WeeklyReport(
            employee_name="邝锦明",
            iso_year_week="2026-W35",
            tasks_completed="呼梯日志全链路",
        )
        ingested = IngestedFile(
            name="工作周报.xlsx",
            text="workbook",
            reports=(report,),
            needs_llm=False,
        )
        sheets_client = MagicMock()
        sheets_client.lookup_employee.return_value = None
        sheets_client.report_exists.return_value = False
        say = MagicMock()

        with (
            patch("app.ingest_slack_files", return_value=[ingested]),
            patch("app.extract_reports") as extract_reports,
            patch("app.get_sheets_client", return_value=sheets_client),
        ):
            app.handle_report_message(event=event, say=say, client=MagicMock())

        extract_reports.assert_not_called()
        self.assertEqual(sheets_client.append_report.call_count, 1)
        saved = sheets_client.append_report.call_args.args[0]
        self.assertEqual(saved.employee_name, "邝锦明")
        self.assertEqual(saved.tasks_completed, "呼梯日志全链路")
        self.assertEqual(saved.iso_year_week, "2026-W35")

    def test_inaccessible_forwarded_file_does_not_save_a_stub_row(self) -> None:
        event = {
            "type": "message",
            "channel": app.config.slack_channel_id or "C1",
            "ts": "1785280000.000100",
            "files": [],
            "attachments": [
                {
                    "is_share": True,
                    "fallback": (
                        "[August 28th, 2026 6:56 PM] kuangjinming: 工作周报.xlsx"
                    ),
                    "files": [{"id": "F123", "name": "工作周报.xlsx"}],
                }
            ],
        }
        client = MagicMock()
        client.files_info.side_effect = RuntimeError("not_allowed")
        sheets_client = MagicMock()
        say = MagicMock()

        with (
            patch("app.extract_reports") as extract_reports,
            patch("app.get_sheets_client", return_value=sheets_client),
        ):
            app.handle_report_message(event=event, say=say, client=client)

        extract_reports.assert_not_called()
        sheets_client.append_report.assert_not_called()
        reply = say.call_args.kwargs["text"]
        self.assertIn("工作周报.xlsx", reply)
        self.assertIn("could not download", reply.lower())

    def test_recognized_workbook_skips_llm_extraction(self) -> None:
        report = WeeklyReport(
            employee_name="王怡明",
            iso_year_week="2026-W32",
            tasks_completed="完成跌倒识别",
        )
        sheets_client = MagicMock()
        sheets_client.lookup_employee.return_value = None
        sheets_client.report_exists.return_value = False
        say = MagicMock()
        event = {
            "type": "message",
            "channel": app.config.slack_channel_id or "C1",
            "ts": "1785280000.000100",
            "files": [{"name": "工作周报.xlsx"}],
        }

        with (
            patch(
                "app._load_report_sources",
                return_value=(
                    [
                        IngestedFile(
                            name="工作周报.xlsx",
                            text="unused when table-extracted",
                            reports=(report,),
                            needs_llm=False,
                        )
                    ],
                    [],
                ),
            ),
            patch("app.extract_reports") as extract_reports,
            patch("app.get_sheets_client", return_value=sheets_client),
        ):
            app.handle_report_message(
                event=event,
                say=say,
                client=MagicMock(),
            )

        extract_reports.assert_not_called()
        self.assertEqual(sheets_client.append_report.call_count, 1)
        saved = sheets_client.append_report.call_args.args[0]
        self.assertEqual(saved.employee_name, "王怡明")
        self.assertEqual(saved.iso_year_week, "2026-W32")

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

    def _retry_event(self, text: str = "retry") -> dict:
        return {
            "type": "message",
            "channel": app.config.slack_channel_id or "C1",
            "ts": "1785280000.000200",
            "thread_ts": "1785280000.000100",
            "text": text,
            "user": "U123",
        }

    def _parent_message(self) -> dict:
        return {
            "ts": "1785280000.000100",
            "text": "weekly report",
            "user": "U111",
        }

    def test_retry_thread_reply_imports_the_parent_message(self) -> None:
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
        client = MagicMock()
        client.conversations_replies.return_value = {
            "messages": [self._parent_message()]
        }

        with (
            patch(
                "app._load_report_sources",
                return_value=([IngestedFile(name="weekly.md", text="text")], []),
            ) as load_sources,
            patch("app.extract_reports", return_value=[report]),
            patch("app.get_sheets_client", return_value=sheets_client),
        ):
            app.handle_report_message(
                event=self._retry_event(),
                say=say,
                client=client,
            )

        client.conversations_replies.assert_called_once()
        parent_event = load_sources.call_args.args[0]
        self.assertEqual(parent_event["ts"], "1785280000.000100")
        self.assertEqual(parent_event["text"], "weekly report")
        self.assertEqual(sheets_client.append_report.call_count, 1)
        reply_texts = [call.kwargs["text"] for call in say.call_args_list]
        self.assertTrue(any("Retrying import" in text for text in reply_texts))
        self.assertTrue(any("Logged weekly report" in text for text in reply_texts))

    def test_bot_reply_does_not_trigger_retry(self) -> None:
        event = self._retry_event()
        event["bot_id"] = "B123"
        say = MagicMock()
        client = MagicMock()
        sheets_client = MagicMock()

        with patch("app.get_sheets_client", return_value=sheets_client):
            app.handle_report_message(event=event, say=say, client=client)

        client.conversations_replies.assert_not_called()
        sheets_client.append_report.assert_not_called()
        say.assert_not_called()

    def test_self_message_does_not_trigger_retry(self) -> None:
        event = self._retry_event()
        event["user"] = "UBOT"
        say = MagicMock()
        client = MagicMock()
        sheets_client = MagicMock()

        with patch("app.get_sheets_client", return_value=sheets_client):
            app.handle_report_message(
                event=event,
                say=say,
                client=client,
                context={"bot_user_id": "UBOT"},
            )

        client.conversations_replies.assert_not_called()
        sheets_client.append_report.assert_not_called()
        say.assert_not_called()

    def test_ordinary_thread_reply_is_ignored(self) -> None:
        event = self._retry_event(text="thanks, looking into this")
        say = MagicMock()
        client = MagicMock()
        sheets_client = MagicMock()

        with patch("app.get_sheets_client", return_value=sheets_client):
            app.handle_report_message(event=event, say=say, client=client)

        client.conversations_replies.assert_not_called()
        sheets_client.append_report.assert_not_called()
        say.assert_not_called()

    def test_retry_parent_fetch_error_replies_without_saving(self) -> None:
        say = MagicMock()
        client = MagicMock()
        client.conversations_replies.side_effect = RuntimeError("missing_scope")
        sheets_client = MagicMock()

        with patch("app.get_sheets_client", return_value=sheets_client):
            app.handle_report_message(
                event=self._retry_event(),
                say=say,
                client=client,
            )

        sheets_client.append_report.assert_not_called()
        reply = say.call_args.kwargs["text"]
        self.assertIn("Could not fetch the original message", reply)
        self.assertIn("missing_scope", reply)

    def test_retry_force_replaces_existing_row(self) -> None:
        report = WeeklyReport(
            employee_name="Jane",
            team="Platform",
            iso_year_week="2026-W30",
            tasks_completed="Corrected",
        )
        sheets_client = MagicMock()
        sheets_client.lookup_employee.return_value = None
        sheets_client.replace_report.return_value = True
        say = MagicMock()
        client = MagicMock()
        client.conversations_replies.return_value = {
            "messages": [self._parent_message()]
        }

        with (
            patch(
                "app._load_report_sources",
                return_value=([IngestedFile(name="weekly.md", text="text")], []),
            ),
            patch("app.extract_reports", return_value=[report]),
            patch("app.get_sheets_client", return_value=sheets_client),
        ):
            app.handle_report_message(
                event=self._retry_event("retry force"),
                say=say,
                client=client,
            )

        sheets_client.replace_report.assert_called_once()
        sheets_client.append_report.assert_not_called()
        sheets_client.report_exists.assert_not_called()
        reply_texts = [call.kwargs["text"] for call in say.call_args_list]
        self.assertTrue(any("Replaced the existing row" in text for text in reply_texts))

    def test_in_flight_import_rejects_a_second_retry(self) -> None:
        channel = app.config.slack_channel_id or "C1"
        thread_ts = "1785280000.000100"
        key = (channel, thread_ts)
        say = MagicMock()
        client = MagicMock()
        client.conversations_replies.return_value = {
            "messages": [self._parent_message()]
        }
        sheets_client = MagicMock()

        app._ACTIVE_IMPORTS.add(key)
        try:
            with (
                patch("app.get_sheets_client", return_value=sheets_client),
                patch("app.extract_reports") as extract_reports,
            ):
                app.handle_report_message(
                    event=self._retry_event(),
                    say=say,
                    client=client,
                )
        finally:
            app._ACTIVE_IMPORTS.discard(key)

        extract_reports.assert_not_called()
        sheets_client.append_report.assert_not_called()
        reply_texts = [call.kwargs["text"] for call in say.call_args_list]
        self.assertTrue(any("already running" in text.lower() for text in reply_texts))


if __name__ == "__main__":
    unittest.main()
