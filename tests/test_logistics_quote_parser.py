"""回归：物流报价表解析。

上游 ``app/routers/logistics_quote.py`` 从未提交，解析逻辑是按前端类型定义
（``frontend/services/api.ts``）和 ``logistics_quote_books`` 表结构重建的。
本文件锁定重建实现的对外契约，避免以后被静默改坏。
"""

import io
import unittest

from app.services.logistics_quote_parser import ParseError, parse_rate_book


def build_xlsx(rows, sheet_title="报价"):
    """Build a minimal workbook from a list of rows."""
    import openpyxl

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = sheet_title
    for row in rows:
        sheet.append(row)

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


HEADER = ["承运商", "始发城市", "目的城市", "首重(kg)", "首重价格(元)", "续重(kg)", "续重价格(元)"]


class ParseRateBookTests(unittest.TestCase):
    def test_rejects_unsupported_extension(self):
        with self.assertRaises(ParseError):
            parse_rate_book("payload.exe", b"MZ\x90\x00")

    def test_rejects_empty_upload(self):
        with self.assertRaises(ParseError):
            parse_rate_book("a.xlsx", b"")

    def test_rejects_workbook_without_recognisable_header(self):
        content = build_xlsx([["随便", "写点", "东西"], ["a", "b", "c"]])
        with self.assertRaises(ParseError):
            parse_rate_book("a.xlsx", content)

    def test_header_is_found_below_a_title_row(self):
        content = build_xlsx([
            ["顺丰速运 2026 年 9 月报价表"],
            [],
            HEADER,
            ["顺丰速运", "广州市", "深圳市", 1, 12, 1, 2],
        ])
        result = parse_rate_book("sf.xlsx", content)

        self.assertEqual(result["summary"]["total"], 1)
        row = result["rows"][0]
        self.assertEqual(row["carrier"], "顺丰速运")
        self.assertEqual(row["origin_city"], "广州市")
        self.assertEqual(row["destination_city"], "深圳市")
        self.assertEqual(row["first_price"], 12.0)

    def test_rule_type_is_first_additional_when_both_weights_present(self):
        content = build_xlsx([HEADER, ["顺丰", "广州市", "深圳市", 1, 12, 1, 2]])
        result = parse_rate_book("sf.xlsx", content)
        self.assertEqual(result["services"][0]["rule_type"], "first_additional")

    def test_row_missing_destination_is_rejected(self):
        content = build_xlsx([HEADER, ["顺丰", "广州市", None, 1, 12, 1, 2]])
        result = parse_rate_book("sf.xlsx", content)

        self.assertEqual(result["summary"]["rejected"], 1)
        self.assertEqual(result["rows"][0]["review_state"], "rejected")
        self.assertIn("缺少目的地", result["rows"][0]["issues"])

    def test_row_missing_price_is_rejected(self):
        content = build_xlsx([HEADER, ["顺丰", "广州市", "深圳市", 1, None, 1, None]])
        result = parse_rate_book("sf.xlsx", content)

        self.assertEqual(result["rows"][0]["review_state"], "rejected")
        self.assertIn("缺少价格字段", result["rows"][0]["issues"])

    def test_negative_price_is_flagged_but_row_survives(self):
        content = build_xlsx([HEADER, ["顺丰", "广州市", "深圳市", 1, -1, 1, 3]])
        result = parse_rate_book("sf.xlsx", content)

        row = result["rows"][0]
        self.assertIn("首重价格为负数", row["issues"])
        self.assertEqual(row["review_state"], "review")
        self.assertLess(row["confidence"], 1.0)

    def test_fully_valid_row_scores_one(self):
        content = build_xlsx([HEADER, ["顺丰", "广州市", "深圳市", 1, 12, 1, 2]])
        row = parse_rate_book("sf.xlsx", content)["rows"][0]

        self.assertEqual(row["review_state"], "valid")
        self.assertEqual(row["confidence"], 1.0)
        self.assertEqual(row["issues"], [])

    def test_blank_rows_are_skipped(self):
        content = build_xlsx([
            HEADER,
            ["顺丰", "广州市", "深圳市", 1, 12, 1, 2],
            [None, None, None, None, None, None, None],
            ["顺丰", "广州市", "北京市", 1, 18, 1, 5],
        ])
        result = parse_rate_book("sf.xlsx", content)
        self.assertEqual(result["summary"]["total"], 2)

    def test_summary_counts_match_rows(self):
        content = build_xlsx([
            HEADER,
            ["顺丰", "广州市", "深圳市", 1, 12, 1, 2],
            ["顺丰", "广州市", "北京市", 1, 18, 1, 5],
            ["顺丰", "广州市", None, 1, 20, 1, 6],
            ["顺丰", "广州市", "杭州市", 1, -1, 1, 3],
        ])
        summary = parse_rate_book("sf.xlsx", content)["summary"]

        self.assertEqual(summary["total"], 4)
        self.assertEqual(summary["valid"], 2)
        self.assertEqual(summary["review"], 1)
        self.assertEqual(summary["rejected"], 1)
        self.assertEqual(
            summary["valid"] + summary["review"] + summary["rejected"], summary["total"]
        )

    def test_sha256_is_stable_for_identical_content(self):
        content = build_xlsx([HEADER, ["顺丰", "广州市", "深圳市", 1, 12, 1, 2]])
        first = parse_rate_book("sf.xlsx", content)["source"]["sha256"]
        second = parse_rate_book("renamed.xlsx", content)["source"]["sha256"]
        self.assertEqual(first, second)

    def test_carriers_are_aggregated(self):
        content = build_xlsx([
            HEADER,
            ["顺丰", "广州市", "深圳市", 1, 12, 1, 2],
            ["中通", "广州市", "深圳市", 1, 8, 1, 2],
            ["顺丰", "广州市", "北京市", 1, 18, 1, 5],
        ])
        carriers = {item["name"]: item for item in parse_rate_book("sf.xlsx", content)["carriers"]}

        self.assertEqual(carriers["顺丰"]["route_count"], 2)
        self.assertEqual(carriers["中通"]["route_count"], 1)

    def test_csv_is_supported(self):
        csv_text = ",".join(HEADER) + "\n" + "顺丰,广州市,深圳市,1,12,1,2\n"
        result = parse_rate_book("sf.csv", csv_text.encode("utf-8-sig"))

        self.assertEqual(result["summary"]["total"], 1)
        self.assertEqual(result["source"]["file_type"], "csv")

    def test_gbk_csv_is_supported(self):
        csv_text = ",".join(HEADER) + "\n" + "顺丰,广州市,深圳市,1,12,1,2\n"
        result = parse_rate_book("sf.csv", csv_text.encode("gb18030"))
        self.assertEqual(result["summary"]["total"], 1)

    def test_numeric_text_with_units_is_parsed(self):
        content = build_xlsx([HEADER, ["顺丰", "广州市", "深圳市", "1kg", "12元", "1", "2.5"]])
        row = parse_rate_book("sf.xlsx", content)["rows"][0]

        self.assertEqual(row["first_weight_kg"], 1.0)
        self.assertEqual(row["first_price"], 12.0)
        self.assertEqual(row["continued_price"], 2.5)

    def test_combined_place_cell_is_split(self):
        content = build_xlsx([
            ["承运商", "始发", "目的地", "报价(元)"],
            ["德邦物流", "广东省 广州市", "北京市 朝阳区", 35],
        ])
        row = parse_rate_book("db.xlsx", content)["rows"][0]

        self.assertEqual(row["origin_province"], "广东省")
        self.assertEqual(row["origin_city"], "广州市")
        self.assertEqual(row["destination_province"], "北京市")

    def test_response_carries_every_frontend_field(self):
        content = build_xlsx([HEADER, ["顺丰", "广州市", "深圳市", 1, 12, 1, 2]])
        result = parse_rate_book("sf.xlsx", content)

        for key in (
            "success", "mode", "source", "mapping", "summary", "book_kind",
            "service_count", "route_count", "services", "carriers", "rows",
            "sample_row", "warning_count", "warnings",
        ):
            self.assertIn(key, result)

        for key in (
            "filename", "size", "sha256", "file_type", "parser_version", "status",
        ):
            self.assertIn(key, result["source"])


if __name__ == "__main__":
    unittest.main()
