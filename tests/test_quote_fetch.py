import json
import unittest

from cli.quote_fetch import (
    generate_mock_quotes,
    normalize_crypto_id,
    normalize_etf_symbol,
    normalize_tw_symbol,
    parse_symbol_list,
    parse_yahoo_chart_payload,
    render_json,
    render_markdown,
    render_table,
)


class QuoteFetchTests(unittest.TestCase):
    def test_symbol_normalization(self):
        self.assertEqual(normalize_tw_symbol("2330"), "2330.TW")
        self.assertEqual(normalize_tw_symbol("6488.TWO"), "6488.TWO")
        self.assertEqual(normalize_etf_symbol("0050"), "0050.TW")
        self.assertEqual(normalize_etf_symbol("voo"), "VOO")
        self.assertEqual(normalize_crypto_id("BTC"), "bitcoin")
        self.assertEqual(normalize_crypto_id("ethereum"), "ethereum")

    def test_parse_symbol_list(self):
        self.assertEqual(parse_symbol_list(["2330, 2454", "2317"]), ["2330", "2454", "2317"])

    def test_parse_yahoo_chart_payload(self):
        payload = {
            "chart": {
                "result": [
                    {
                        "meta": {
                            "symbol": "NVDA",
                            "shortName": "NVIDIA Corp",
                            "regularMarketPrice": 110.0,
                            "chartPreviousClose": 100.0,
                            "currency": "USD",
                        }
                    }
                ],
                "error": None,
            }
        }
        res = parse_yahoo_chart_payload(payload, market="us", fallback_symbol="NVDA")
        self.assertEqual(res.symbol, "NVDA")
        self.assertEqual(res.price, 110.0)
        self.assertEqual(res.change_pct, 10.0)
        self.assertIsNone(res.error)

    def test_mock_quotes_and_renderers(self):
        quotes = generate_mock_quotes(["2330"], ["NVDA"], ["0050", "VOO"], ["btc"])
        self.assertEqual(len(quotes), 5)

        table = render_table(quotes)
        self.assertIn("2330.TW", table)
        self.assertIn("BITCOIN", table)

        md = render_markdown(quotes)
        self.assertIn("| `tw` | **2330.TW** |", md)

        raw_json = render_json(quotes)
        parsed = json.loads(raw_json)
        self.assertEqual(len(parsed), 5)
        self.assertEqual(parsed[0]["market"], "tw")


if __name__ == "__main__":
    unittest.main()
