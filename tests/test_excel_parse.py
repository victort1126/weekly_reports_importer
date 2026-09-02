from __future__ import annotations

import unittest
from datetime import datetime, timezone
from io import BytesIO
from unittest.mock import patch

from openpyxl import Workbook

from excel_parse import iso_year_week_from_value, parse_worksheet_tables
from file_ingest import ingest_slack_file


class _Response:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.headers = {"Content-Length": str(len(content))}

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size: int):
        del chunk_size
        yield self.content


class ExcelParseTests(unittest.TestCase):
    def test_chinese_date_becomes_iso_week(self) -> None:
        self.assertEqual(iso_year_week_from_value("2026年8月7日"), "2026-W32")
        self.assertEqual(iso_year_week_from_value("2026-08-28"), "2026-W35")
        self.assertEqual(iso_year_week_from_value("2026-W30"), "2026-W30")

    def test_chinese_collection_form_extracts_each_employee_row(self) -> None:
        """Typical 收集表单 layout: Chinese headers, extra metadata column, multiline cells."""
        parsed = parse_worksheet_tables(
            [
                (
                    "收集表单",
                    [
                        [
                            "汇报日期",
                            "填写者",
                            "提交时间",
                            "本周工作总结",
                            "下周工作计划",
                            "遇到的阻碍/风险/问题",
                            "需要其他人协助的事项",
                        ],
                        [
                            "2026年8月7日",
                            "王怡明",
                            "2026-08-07 15:29",
                            "项目链接：https://example.com\n1.解决侧身下蹲未能识别的问题",
                            "继续测试",
                            "无",
                            "继续让@吴琦芳帮我完成测试工作。",
                        ],
                        [
                            "2026年8月14日",
                            "邝锦明",
                            "2026-08-14 16:19",
                            "机器人平台\n- 服务端缓存",
                            "复刻测试环境",
                            "/",
                            "/",
                        ],
                        [
                            "2026年8月21日",
                            "空任务",
                            "2026-08-21 10:00",
                            "",
                            "有计划但没完成项",
                            "无",
                            "无",
                        ],
                    ],
                )
            ]
        )

        self.assertFalse(parsed.needs_llm)
        self.assertEqual(len(parsed.reports), 2)
        first, second = parsed.reports
        self.assertEqual(first.employee_name, "王怡明")
        self.assertEqual(first.iso_year_week, "2026-W32")
        self.assertIn("解决侧身下蹲未能识别的问题", first.tasks_completed)
        self.assertIn("项目链接", first.tasks_completed)
        self.assertEqual(first.next_steps, "继续测试")
        self.assertIsNone(first.blockers_risks)
        self.assertEqual(first.needs_from_others, "继续让@吴琦芳帮我完成测试工作。")
        self.assertEqual(second.employee_name, "邝锦明")
        self.assertEqual(second.iso_year_week, "2026-W33")
        self.assertIsNone(second.blockers_risks)
        self.assertIn("王怡明", parsed.text)
        self.assertIn("本周工作总结 (tasks_completed):", parsed.text)
        self.assertNotIn("\t", parsed.text.split("王怡明", 1)[1][:80])

    def test_reordered_and_english_headers_still_map(self) -> None:
        parsed = parse_worksheet_tables(
            [
                (
                    "Weekly",
                    [
                        ["Completed", "Next steps", "Name", "Week"],
                        ["Shipped feature A", "Write tests", "Jane Doe", "2026-W30"],
                    ],
                )
            ]
        )
        self.assertFalse(parsed.needs_llm)
        self.assertEqual(len(parsed.reports), 1)
        report = parsed.reports[0]
        self.assertEqual(report.employee_name, "Jane Doe")
        self.assertEqual(report.tasks_completed, "Shipped feature A")
        self.assertEqual(report.next_steps, "Write tests")
        self.assertEqual(report.iso_year_week, "2026-W30")

    def test_title_row_above_headers_is_ignored(self) -> None:
        parsed = parse_worksheet_tables(
            [
                (
                    "Sheet1",
                    [
                        ["工作周报"],
                        ["填写者", "本周工作总结", "汇报日期"],
                        ["何敏", "完成广告排程", "2026年8月28日"],
                    ],
                )
            ]
        )
        self.assertFalse(parsed.needs_llm)
        self.assertEqual(parsed.reports[0].employee_name, "何敏")
        self.assertEqual(parsed.reports[0].iso_year_week, "2026-W35")

    def test_multiline_cells_stay_in_one_labeled_record(self) -> None:
        parsed = parse_worksheet_tables(
            [
                (
                    "Platform",
                    [
                        ["Name", "Completed"],
                        ["Jane", "line 1\nline 2\nline 3"],
                    ],
                )
            ]
        )
        self.assertIn("--- Record 1 ---", parsed.text)
        self.assertIn("line 1\nline 2\nline 3", parsed.text)
        self.assertEqual(parsed.reports[0].tasks_completed, "line 1\nline 2\nline 3")

    def test_unrecognized_sheet_still_renders_for_llm(self) -> None:
        parsed = parse_worksheet_tables(
            [
                (
                    "Notes",
                    [
                        ["random notes from standup"],
                        ["Jane talked about the launch"],
                    ],
                )
            ]
        )
        self.assertTrue(parsed.needs_llm)
        self.assertEqual(parsed.reports, ())
        self.assertIn("Jane talked about the launch", parsed.text)

    def test_merged_header_survives_ingest(self) -> None:
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "收集表单"
        sheet.merge_cells("D1:E1")
        sheet["A1"] = "填写者"
        sheet["B1"] = "汇报日期"
        sheet["C1"] = "本周工作总结"
        sheet["D1"] = "下周工作计划"
        sheet["A2"] = "吴琦芳"
        sheet["B2"] = "2026年8月7日"
        sheet["C2"] = "回测bug"
        sheet["D2"] = "继续测试"
        buffer = BytesIO()
        workbook.save(buffer)
        workbook.close()

        file = {
            "name": "merged.xlsx",
            "url_private_download": "https://files.slack.test/merged.xlsx",
        }
        with patch(
            "file_ingest.requests.get", return_value=_Response(buffer.getvalue())
        ):
            result = ingest_slack_file(
                file,
                "xoxb-test",
                message_date=datetime(2026, 8, 31, tzinfo=timezone.utc),
            )

        self.assertFalse(result.needs_llm)
        self.assertEqual(len(result.reports), 1)
        self.assertEqual(result.reports[0].employee_name, "吴琦芳")
        self.assertEqual(result.reports[0].next_steps, "继续测试")
        self.assertEqual(result.reports[0].iso_year_week, "2026-W32")

    def test_long_header_hint_still_maps_the_column(self) -> None:
        parsed = parse_worksheet_tables(
            [
                (
                    "收集表单",
                    [
                        [
                            "填写者",
                            "本周工作总结（请填写本周完成的工作，可换行）",
                            "汇报日期",
                        ],
                        ["邝锦明", "修复调度器栈溢出", "2026年8月7日"],
                    ],
                )
            ]
        )
        self.assertFalse(parsed.needs_llm)
        self.assertEqual(parsed.reports[0].employee_name, "邝锦明")
        self.assertEqual(parsed.reports[0].tasks_completed, "修复调度器栈溢出")
        self.assertEqual(parsed.reports[0].iso_year_week, "2026-W32")

    def test_cover_sheet_does_not_send_table_workbook_to_llm(self) -> None:
        parsed = parse_worksheet_tables(
            [
                ("封面", [["工作周报.xlsx"], ["请各位周五前提交"]]),
                (
                    "收集表单",
                    [
                        ["填写者", "本周工作总结", "汇报日期"],
                        ["邝锦明", "呼梯日志全链路", "2026年8月28日"],
                    ],
                ),
            ]
        )
        self.assertFalse(parsed.needs_llm)
        self.assertEqual(len(parsed.reports), 1)
        self.assertEqual(parsed.reports[0].tasks_completed, "呼梯日志全链路")
        self.assertNotEqual(parsed.reports[0].iso_year_week, "2026-W36")


if __name__ == "__main__":
    unittest.main()
