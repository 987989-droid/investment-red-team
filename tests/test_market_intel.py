import json
import tempfile
import unittest
from pathlib import Path

from cli.market_intel import (
    classify_publisher_tier,
    classify_technical_regime,
    compute_rsi,
    compute_sma,
    detect_event_tags,
    determine_overall_regime,
    generate_mock_intel,
    main,
    parse_google_news_rss,
)


class MarketIntelTests(unittest.TestCase):
    def test_sma_and_rsi_calculation(self):
        vals = [float(i) for i in range(1, 25)]
        self.assertEqual(compute_sma(vals, 5), 22.0)
        self.assertIsNone(compute_sma(vals, 30))
        rsi = compute_rsi(vals, 14)
        self.assertEqual(rsi, 100.0)

    def test_publisher_tier_and_event_tags(self):
        self.assertEqual(classify_publisher_tier("Reuters"), "TIER_1")
        self.assertEqual(classify_publisher_tier("經濟日報"), "TIER_2")
        self.assertIsNone(classify_publisher_tier("Random Rumor Blog"))

        tags = detect_event_tags("CEO resigns abruptly amid export ban fears")
        self.assertIn("TYPE_A_GOVERNANCE", tags)
        self.assertIn("TYPE_B_FUNDAMENTAL_SHOCK", tags)

    def test_parse_google_news_rss_filters_untrusted_sources(self):
        sample_xml = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <item>
    <title>Chipmaker CEO returns after 11 days, shares surge - Reuters</title>
    <link>https://example.com/reuters-1</link>
    <pubDate>Thu, 26 Nov 2026 08:00:00 GMT</pubDate>
    <source>Reuters</source>
  </item>
  <item>
    <title>Unverified forum rumor says stock going 10x - RandomBlog</title>
    <link>https://example.com/rumor</link>
    <pubDate>Thu, 26 Nov 2026 08:05:00 GMT</pubDate>
    <source>RandomBlog</source>
  </item>
</channel></rss>"""
        items = parse_google_news_rss(sample_xml, query="NVDA")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].tier, "TIER_1")
        self.assertIn("REVERSAL_CATALYST", items[0].event_tags)

    def test_event_shock_regime_caps_fundamentals_to_15_percent(self):
        technicals, news = generate_mock_intel(["2330"], ["NVDA"], ["VOO"])
        regime, weights, _ = determine_overall_regime(technicals, news)
        self.assertEqual(regime, "EVENT_SHOCK")
        self.assertEqual(weights["fundamentals_claude"], 15)
        self.assertEqual(weights["news_events"], 40)
        self.assertEqual(weights["technical_price"], 35)

    def test_technical_regime_classification(self):
        regime, _ = classify_technical_regime(
            close=100.0, change_1d=8.5, change_5d=4.0,
            ma20=95.0, ma60=98.0, drawdown_20d=-4.0, vol_ratio=2.1, gap_pct=5.2,
        )
        self.assertEqual(regime, "EVENT_REVERSAL_BREAKOUT")

    def test_cli_mock_generates_markdown_and_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            md_path = Path(tmp) / "00_context.md"
            json_path = Path(tmp) / "00_context.json"
            rc = main([
                "--tw", "2330", "--us", "NVDA", "--etf", "VOO",
                "--mock", "--out-md", str(md_path), "--out-json", str(json_path),
            ])
            self.assertEqual(rc, 0)
            self.assertTrue(md_path.exists())
            self.assertTrue(json_path.exists())
            data = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(data["regime"], "EVENT_SHOCK")
            self.assertEqual(len(data["technicals"]), 3)


if __name__ == "__main__":
    unittest.main()
