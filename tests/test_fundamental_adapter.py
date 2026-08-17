# -*- coding: utf-8 -*-
"""
Tests for fundamental adapter helpers.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from data_provider.fundamental_adapter import (
    AkshareFundamentalAdapter,
    _build_dividend_payload,
    _extract_financial_summary_from_wide_table,
    _extract_latest_row,
    _format_cn_amount,
    _is_wide_financial_table,
    _parse_cn_amount,
    _parse_dividend_plan_to_per_share,
)


class TestFundamentalAdapter(unittest.TestCase):
    def test_parse_dividend_plan_to_per_share_supports_cn_patterns(self) -> None:
        self.assertAlmostEqual(_parse_dividend_plan_to_per_share("10派3元(含税)"), 0.3, places=6)
        self.assertAlmostEqual(_parse_dividend_plan_to_per_share("每10股派发2.5元"), 0.25, places=6)
        self.assertAlmostEqual(_parse_dividend_plan_to_per_share("每股派0.8元"), 0.8, places=6)
        self.assertIsNone(_parse_dividend_plan_to_per_share("仅送股，不现金分红"))

    def test_extract_latest_row_returns_none_when_code_mismatch(self) -> None:
        df = pd.DataFrame(
            {
                "股票代码": ["600000", "000001"],
                "值": [1, 2],
            }
        )
        row = _extract_latest_row(df, "600519")
        self.assertIsNone(row)

    def test_extract_latest_row_fallback_when_no_code_column(self) -> None:
        df = pd.DataFrame({"值": [1, 2]})
        row = _extract_latest_row(df, "600519")
        self.assertIsNotNone(row)
        self.assertEqual(row["值"], 1)

    def test_dragon_tiger_no_match_with_code_column_is_ok(self) -> None:
        adapter = AkshareFundamentalAdapter()
        df = pd.DataFrame(
            {
                "股票代码": ["600000"],
                "日期": ["2026-01-01"],
            }
        )
        with patch.object(adapter, "_call_df_candidates", return_value=(df, "stock_lhb_stock_statistic_em", [])):
            result = adapter.get_dragon_tiger_flag("600519")
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["is_on_list"])
        self.assertEqual(result["recent_count"], 0)

    def test_dragon_tiger_match_is_ok(self) -> None:
        adapter = AkshareFundamentalAdapter()
        today = pd.Timestamp.now().strftime("%Y-%m-%d")
        df = pd.DataFrame(
            {
                "股票代码": ["600519"],
                "日期": [today],
            }
        )
        with patch.object(adapter, "_call_df_candidates", return_value=(df, "stock_lhb_stock_statistic_em", [])):
            result = adapter.get_dragon_tiger_flag("600519")
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["is_on_list"])
        self.assertGreaterEqual(result["recent_count"], 1)

    def test_fundamental_bundle_includes_financial_report_and_dividend_payload(self) -> None:
        adapter = AkshareFundamentalAdapter()
        now = datetime.now()
        within_ttm = (now - timedelta(days=30)).strftime("%Y-%m-%d")
        future_day = (now + timedelta(days=10)).strftime("%Y-%m-%d")
        old_day = (now - timedelta(days=500)).strftime("%Y-%m-%d")
        fin_df = pd.DataFrame(
            {
                "股票代码": ["600519"],
                "报告期": [within_ttm],
                "营业总收入": [1000.0],
                "归母净利润": [300.0],
                "经营活动产生的现金流量净额": [500.0],
                "净资产收益率": [18.2],
                "营业收入同比": [12.0],
                "净利润同比": [9.5],
            }
        )
        forecast_df = pd.DataFrame({"股票代码": ["600519"], "预告": ["预增"]})
        quick_df = pd.DataFrame({"股票代码": ["600519"], "快报": ["快报摘要"]})
        dividend_df = pd.DataFrame(
            {
                "股票代码": ["600519", "600519", "600519", "600519"],
                "除息日": [within_ttm, within_ttm, future_day, old_day],
                "分配方案": ["10派3元(含税)", "10派3元(含税)", "10派5元", "10派1元"],
            }
        )

        with patch.object(
            adapter,
            "_call_df_candidates",
            side_effect=[
                (fin_df, "stock_financial_abstract", []),
                (forecast_df, "stock_yjyg_em", []),
                (quick_df, "stock_yjkb_em", []),
                (dividend_df, "stock_fhps_detail_em", []),
                (None, None, []),
                (None, None, []),
            ],
        ):
            result = adapter.get_fundamental_bundle("600519")

        financial_report = result["earnings"].get("financial_report", {})
        self.assertEqual(financial_report.get("report_date"), within_ttm)
        self.assertEqual(financial_report.get("revenue"), 1000.0)
        self.assertEqual(financial_report.get("net_profit_parent"), 300.0)
        self.assertEqual(financial_report.get("operating_cash_flow"), 500.0)
        self.assertEqual(financial_report.get("roe"), 18.2)

        dividend_payload = result["earnings"].get("dividend", {})
        events = dividend_payload.get("events", [])
        self.assertEqual(len(events), 2)  # duplicate + future day filtered
        self.assertEqual(dividend_payload.get("ttm_event_count"), 1)
        self.assertAlmostEqual(dividend_payload.get("ttm_cash_dividend_per_share"), 0.3, places=6)

    def test_build_dividend_payload_returns_empty_when_code_not_matched(self) -> None:
        now = datetime.now().strftime("%Y-%m-%d")
        df = pd.DataFrame(
            {
                "股票代码": ["000001"],
                "除息日": [now],
                "分配方案": ["10派3元(含税)"],
            }
        )

        payload = _build_dividend_payload(df, stock_code="600519")
        self.assertEqual(payload, {})

    def test_build_dividend_payload_skips_after_tax_plan(self) -> None:
        now = datetime.now().strftime("%Y-%m-%d")
        df = pd.DataFrame(
            {
                "股票代码": ["600519"],
                "除息日": [now],
                "分配方案": ["10派3元(税后)"],
            }
        )

        payload = _build_dividend_payload(df, stock_code="600519")
        self.assertEqual(payload, {})

    def test_build_dividend_payload_ttm_window_boundary(self) -> None:
        now = datetime.now()
        day_365 = (now - timedelta(days=365)).strftime("%Y-%m-%d")
        day_366 = (now - timedelta(days=366)).strftime("%Y-%m-%d")
        df = pd.DataFrame(
            {
                "股票代码": ["600519", "600519"],
                "除息日": [day_365, day_366],
                "分配方案": ["10派3元(含税)", "10派5元(含税)"],
            }
        )

        payload = _build_dividend_payload(df, stock_code="600519")
        self.assertEqual(payload.get("ttm_event_count"), 1)
        self.assertAlmostEqual(payload.get("ttm_cash_dividend_per_share"), 0.3, places=6)


class TestWideFinancialTable(unittest.TestCase):
    """Tests for akshare stock_financial_abstract wide-table parsing."""

    @staticmethod
    def _make_wide_df() -> pd.DataFrame:
        cols = ["选项", "指标", "20260331", "20251231", "20250331", "20241231"]
        data = [
            ["常用", "归母净利润", 5.53e6, 3.742e8, 3.2e8, 3.0e8],
            ["常用", "营业总收入", 2.747e9, 1.129e10, 2.965e9, 1.1e10],
            ["常用", "经营现金流量净额", 8.7e8, 4.19e9, 1.0e9, 3.9e9],
            ["常用", "净资产收益率(ROE)", 0.03, 10.0, 11.0, 12.0],
            ["常用", "毛利率", 15.44, 20.0, 20.4, 21.0],
            ["常用", "每股净资产", 6.07, 6.0, 6.12, 6.1],
            ["常用", "资产负债率", 38.65, 39.01, 46.43, 45.0],
            ["常用", "基本每股收益", 0.002, 0.104, 0.106, 0.12],
            ["成长", "营业总收入增长率", -7.39, -3.18, 1.0, 2.0],
            ["成长", "归属母公司净利润增长率", -98.28, -69.82, -70.0, -60.0],
        ]
        return pd.DataFrame(data, columns=cols)

    def test_is_wide_financial_table_detects_wide_shape(self) -> None:
        self.assertTrue(_is_wide_financial_table(self._make_wide_df()))

    def test_is_wide_financial_table_rejects_columnar_table(self) -> None:
        normal = pd.DataFrame({"股票代码": ["600519"], "报告期": ["2026-03-31"], "营业总收入": [1.0]})
        self.assertFalse(_is_wide_financial_table(normal))

    def test_extract_financial_summary_matches_reference_values(self) -> None:
        summary = _extract_financial_summary_from_wide_table(self._make_wide_df())
        self.assertEqual(summary["report_date"], "2026-03-31")
        self.assertAlmostEqual(summary["revenue"], 2.747e9, delta=1e5)
        self.assertAlmostEqual(summary["net_profit_parent"], 5.53e6, delta=1e3)
        self.assertAlmostEqual(summary["operating_cash_flow"], 8.7e8, delta=1e5)
        self.assertAlmostEqual(summary["roe"], 0.03, places=2)
        self.assertAlmostEqual(summary["gross_margin"], 15.44, places=2)
        self.assertAlmostEqual(summary["bps"], 6.07, places=2)
        self.assertAlmostEqual(summary["debt_ratio"], 38.65, places=2)
        self.assertAlmostEqual(summary["revenue_yoy"], -7.39, places=2)
        self.assertAlmostEqual(summary["net_profit_yoy"], -98.28, places=2)

    def test_wide_path_fills_financial_report_in_bundle(self) -> None:
        adapter = AkshareFundamentalAdapter()
        wide = self._make_wide_df()
        forecast_df = pd.DataFrame({"股票代码": ["600707"], "预告": ["预减"]})
        with patch.object(
            adapter,
            "_call_df_candidates",
            side_effect=[
                (wide, "stock_financial_abstract", []),
                (forecast_df, "stock_yjyg_em", []),
                (None, None, []),
                (None, None, []),
                (None, None, []),
                (None, None, []),
            ],
        ):
            result = adapter.get_fundamental_bundle("600707")

        fr = result["earnings"].get("financial_report", {})
        self.assertAlmostEqual(fr.get("revenue"), 2.747e9, delta=1e5)
        self.assertAlmostEqual(fr.get("net_profit_parent"), 5.53e6, delta=1e3)
        self.assertEqual(fr.get("report_date"), "2026-03-31")
        self.assertAlmostEqual(fr.get("gross_margin"), 15.44, places=2)
        self.assertAlmostEqual(fr.get("bps"), 6.07, places=2)
        self.assertAlmostEqual(fr.get("debt_ratio"), 38.65, places=2)
        self.assertEqual(result["growth"]["revenue_yoy"], -7.39)
        self.assertEqual(result["growth"]["net_profit_yoy"], -98.28)

    def test_parse_cn_amount(self) -> None:
        self.assertAlmostEqual(_parse_cn_amount("1.39亿"), 1.39e8, places=2)
        self.assertAlmostEqual(_parse_cn_amount("2909.03万"), 29090300.0, places=2)
        self.assertAlmostEqual(_parse_cn_amount(12345), 12345.0, places=2)
        self.assertIsNone(_parse_cn_amount("-"))

    def test_format_cn_amount(self) -> None:
        self.assertEqual(_format_cn_amount(1.5e8), "1.50亿")
        self.assertEqual(_format_cn_amount(-97180000), "-9718.0万")
        self.assertEqual(_format_cn_amount(123), "123元")

    def test_sina_stock_fund_flow_parse(self) -> None:
        adapter = AkshareFundamentalAdapter()
        payload = [
            {"opendate": "2026-08-17", "netamount": "-14579977.79", "ratioamount": "-0.00940449", "r0_net": "-24204279.45"},
            {"opendate": "2026-08-14", "netamount": "5082830.72", "ratioamount": "0.00806443", "r0_net": "28079643.10"},
            {"opendate": "2026-08-13", "netamount": "-67722514.06", "ratioamount": "-0.0845099", "r0_net": "-29820642.92"},
            {"opendate": "2026-08-12", "netamount": "1234567.00", "ratioamount": "0.001", "r0_net": "1000000.00"},
            {"opendate": "2026-08-11", "netamount": "2000000.00", "ratioamount": "0.002", "r0_net": "1500000.00"},
        ]
        flow = adapter._parse_sina_stock_fund_flow(payload)
        self.assertAlmostEqual(flow["main_net_inflow"], -14579977.79, places=2)
        self.assertAlmostEqual(flow["main_net_inflow_pct"], -0.00940449, places=6)
        self.assertAlmostEqual(flow["super_large_net_inflow"], -24204279.45, places=2)
        self.assertEqual(flow["date"], "2026-08-17")
        # 5-day cumulative over the 5 provided records; 10-day needs >=10.
        expected_5d = sum([-14579977.79, 5082830.72, -67722514.06, 1234567.00, 2000000.00])
        self.assertAlmostEqual(flow["inflow_5d"], expected_5d, places=2)
        self.assertIsNone(flow["inflow_10d"])

    def test_margin_date_candidates_skips_weekends(self) -> None:
        cands = AkshareFundamentalAdapter._margin_date_candidates("2026-08-17")
        self.assertEqual(cands[0], "20260817")
        self.assertEqual(len(cands), 5)
        for c in cands:
            self.assertNotIn(pd.to_datetime(c).weekday(), (5, 6))


if __name__ == "__main__":
    unittest.main()
