import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cli.email_digest import (
    load_cases_from_dir,
    main,
    parse_case_markdown,
    parse_holdings_yaml_light,
    render_digest_html,
    render_digest_text,
)
from cli.quote_fetch import generate_mock_quotes


class EmailDigestTests(unittest.TestCase):
    def test_parse_case_markdown_extracts_score_grade_and_kill_switches(self):
        sample_md = """# [台股紅隊審計] 9999 範例精密光電

- **審計日期**：`2026-11-25`
- **市場類別**：台股（`tw`）
- **目前狀態**：`ACTIVE`
- **紅隊存活分數**：**78.5 / 100**（等級：B）

## 5. 不可妥協的 Kill-Switch
1. **基本面失效**：連續 2 季毛利率跌破 27%
2. **時間失效**：持有滿 4 季新產品占比低於 20%
"""
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "2026-11-25-tw-9999.md"
            p.write_text(sample_md, encoding="utf-8")
            summary = parse_case_markdown(p)

        self.assertEqual(summary.score, 78.5)
        self.assertEqual(summary.grade, "B")
        self.assertEqual(summary.status, "ACTIVE")
        self.assertEqual(len(summary.kill_switches), 2)
        self.assertIn("毛利率跌破 27%", summary.kill_switches[0])

    def test_parse_holdings_yaml_light_skips_placeholders(self):
        sample_yaml = """holdings:
  - id: "POS-001"
    market: "tw"
    ticker: "<TW_TICKER>"
    name: "<placeholder>"
  - id: "POS-002"
    market: "us"
    ticker: "NVDA"
    name: "NVIDIA"
    tier: "satellite"
    target_weight_pct: 6.5
    red_team_grade: "A"
"""
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "holdings.yaml"
            p.write_text(sample_yaml, encoding="utf-8")
            items = parse_holdings_yaml_light(p)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].ticker, "NVDA")
        self.assertEqual(items[0].target_weight_pct, 6.5)
        self.assertEqual(items[0].red_team_grade, "A")

    def test_dry_run_and_html_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            out_html = tmp_path / "preview.html"
            rc = main(
                [
                    "--cases-dir",
                    "examples",
                    "--tw",
                    "2330",
                    "--us",
                    "NVDA",
                    "--mock",
                    "--dry-run",
                    "--out-html",
                    str(out_html),
                ]
            )
            self.assertEqual(rc, 0)
            self.assertTrue(out_html.exists())
            content = out_html.read_text(encoding="utf-8")
            self.assertIn("2330.TW", content)
            self.assertIn("9999", content)

    def test_missing_smtp_env_returns_error_code(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            rc = main(["--tw", "2330", "--mock"])
        self.assertEqual(rc, 2)

    def test_load_cases_ignores_templates_and_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "README.md").write_text("# readme", encoding="utf-8")
            (d / "INDEX.md").write_text("# index", encoding="utf-8")
            (d / "_template.md").write_text("# tpl", encoding="utf-8")
            (d / "2026-11-25-etf-VOO.md").write_text("# VOO Case\n", encoding="utf-8")
            cases = load_cases_from_dir(d)
            self.assertEqual(len(cases), 1)
            self.assertEqual(cases[0].filename, "2026-11-25-etf-VOO.md")

            quotes = generate_mock_quotes([], [], ["VOO"], [])
            txt = render_digest_text(cases, quotes, [], "2026-11-25T00:00:00Z")
            html_out = render_digest_html(cases, quotes, [], "2026-11-25T00:00:00Z")
            self.assertIn("VOO", txt)
            self.assertIn("VOO", html_out)


if __name__ == "__main__":
    unittest.main()
