# -*- coding: utf-8 -*-
"""Tests for pipeline local-intel merge and fallback helpers."""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.core.pipeline import StockAnalysisPipeline
from src.search_service import SearchResponse, SearchResult


def _resp(title: str, provider: str) -> SearchResponse:
    return SearchResponse(
        query="q",
        results=[
            SearchResult(
                title=title,
                snippet=title,
                url="",
                source=provider,
                published_date="2026-08-01",
            )
        ],
        provider=provider,
        success=True,
    )


class TestPipelineLocalIntel(unittest.TestCase):
    def test_merge_local_intel_provider_wins_and_local_fills(self) -> None:
        provider_news = _resp("provider news", "web")
        local_news = _resp("local news", "akshare_em_news")
        local_ann = _resp("local ann", "cninfo")
        existing = {"latest_news": provider_news, "market_analysis": None}
        local = {"latest_news": local_news, "announcements": local_ann}

        merged = StockAnalysisPipeline._merge_local_intel(existing, local)

        # Provider content wins for a dimension it already filled.
        self.assertIs(merged["latest_news"], provider_news)
        # Local fills a dimension that was absent / empty.
        self.assertIs(merged["announcements"], local_ann)
        # Untouched dimensions remain.
        self.assertIsNone(merged["market_analysis"])

    @patch("src.services.screening.candidate_context.fetch_stock_news_items")
    @patch("src.services.screening.candidate_context.fetch_stock_announcement_items")
    def test_local_fallback_builds_markdown(self, mock_ann, mock_news) -> None:
        mock_news.return_value = [
            {"title": "新闻A", "date": "2026-08-01", "source": "东财", "url": "", "snippet": ""}
        ]
        mock_ann.return_value = [
            {"title": "公告A", "date": "2026-07-09", "source": "巨潮", "url": "", "snippet": ""}
        ]

        ctx = StockAnalysisPipeline._build_local_ashare_intel_fallback("600707", "彩虹股份")

        self.assertIsNotNone(ctx)
        self.assertIn("彩虹股份", ctx or "")
        self.assertIn("新闻A", ctx or "")
        self.assertIn("公告A", ctx or "")

    def test_local_fallback_returns_none_when_sources_empty(self) -> None:
        with patch("src.services.screening.candidate_context.fetch_stock_news_items", return_value=[]), patch(
            "src.services.screening.candidate_context.fetch_stock_announcement_items", return_value=[]
        ):
            ctx = StockAnalysisPipeline._build_local_ashare_intel_fallback("600707", "彩虹股份")
        self.assertIsNone(ctx)


if __name__ == "__main__":
    unittest.main()
