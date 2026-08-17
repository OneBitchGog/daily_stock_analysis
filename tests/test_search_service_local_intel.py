# -*- coding: utf-8 -*-
"""Tests for SearchService.search_local_intel direct-connect A-share intel."""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.search_service import SearchService


class TestSearchLocalIntel(unittest.TestCase):
    def _service(self) -> SearchService:
        # Disable public SearXNG instance discovery to keep tests offline.
        return SearchService(searxng_public_instances_enabled=False)

    @patch("src.services.screening.candidate_context.fetch_stock_news_items")
    @patch("src.services.screening.candidate_context.fetch_stock_announcement_items")
    def test_local_intel_returns_news_and_announcements(self, mock_ann, mock_news) -> None:
        mock_news.return_value = [
            {
                "title": "彩虹股份控股股东解押3000万股",
                "date": "2026-07-31",
                "source": "财中社",
                "url": "http://eastmoney.test/a",
                "snippet": "公告摘要",
            }
        ]
        mock_ann.return_value = [
            {
                "title": "彩虹股份2026年半年度业绩预减公告",
                "date": "2026-07-09",
                "source": "巨潮资讯",
                "url": "http://cninfo.test/b",
                "snippet": "",
            }
        ]
        result = self._service().search_local_intel("600707", "彩虹股份")

        self.assertIn("latest_news", result)
        self.assertIn("announcements", result)
        news = result["latest_news"]
        self.assertEqual(news.provider, "akshare_em_news")
        self.assertTrue(news.success)
        self.assertEqual(news.results[0].title, "彩虹股份控股股东解押3000万股")
        self.assertEqual(news.results[0].published_date, "2026-07-31")

        ann = result["announcements"]
        self.assertEqual(ann.provider, "cninfo")
        self.assertTrue(ann.success)
        self.assertEqual(ann.results[0].title, "彩虹股份2026年半年度业绩预减公告")
        self.assertEqual(ann.results[0].published_date, "2026-07-09")

    def test_local_intel_skips_foreign_stock(self) -> None:
        self.assertEqual(self._service().search_local_intel("AAPL", "Apple"), {})


if __name__ == "__main__":
    unittest.main()
