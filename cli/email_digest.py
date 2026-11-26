#!/usr/bin/env python3
"""Red-Team Digest Generator & Email Notifier CLI (`irt-notify`).

Zero external dependencies (Python stdlib only) and strictly zero PII:
all SMTP credentials and recipient addresses are read from environment
variables at runtime and never hardcoded.
"""

from __future__ import annotations

import argparse
import html
import os
import re
import smtplib
import ssl
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from cli.quote_fetch import (
    QuoteResult,
    fetch_crypto_quotes,
    fetch_equity_quote,
    format_change,
    format_price,
    generate_mock_quotes,
    normalize_crypto_id,
    normalize_etf_symbol,
    normalize_tw_symbol,
    parse_symbol_list,
)


@dataclass
class CaseSummary:
    filename: str
    title: str
    market: str = "unknown"
    status: str = "WATCHING"
    score: Optional[float] = None
    grade: str = "TBD"
    audit_date: str = ""
    kill_switches: List[str] = field(default_factory=list)


@dataclass
class HoldingItem:
    market: str
    ticker: str
    name: str = ""
    tier: str = ""
    target_weight_pct: Optional[float] = None
    red_team_grade: str = "TBD"


def parse_case_markdown(file_path: Path) -> CaseSummary:
    """Extract structured red-team summary fields from a case Markdown file."""
    text = file_path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()

    title = file_path.stem
    for ln in lines:
        if ln.startswith("# "):
            title = ln[2:].strip()
            break

    market = _extract_field(text, [r"市場類別", r"Market"]) or _infer_market_from_name(file_path.name)
    status = _extract_field(text, [r"目前狀態", r"Status"]) or "WATCHING"
    status_stripped = status.strip("`* ")
    status_clean = re.sub(r"[`（）()].*$", "", status_stripped).strip() or "WATCHING"
    audit_date = _extract_field(text, [r"審計日期", r"建立日期", r"Date"]) or ""
    audit_date = audit_date.strip("` ")

    score = _extract_score(text)
    grade = _extract_grade(text, score)
    kill_switches = _extract_kill_switches(lines)

    return CaseSummary(
        filename=file_path.name,
        title=title,
        market=market.strip("` "),
        status=status_clean,
        score=score,
        grade=grade,
        audit_date=audit_date,
        kill_switches=kill_switches,
    )


def _extract_field(text: str, labels: Sequence[str]) -> Optional[str]:
    joined = "|".join(labels)
    match = re.search(rf"(?:\*\*)?(?:{joined})(?:\*\*)?\s*[:：]\s*(.+)", text, re.IGNORECASE)
    if not match:
        return None
    return match.group(1).strip()


def _infer_market_from_name(filename: str) -> str:
    lower = filename.lower()
    for tag in ("tw", "us", "etf", "crypto"):
        if f"-{tag}-" in lower or lower.startswith(f"{tag}-"):
            return tag
    return "general"


def _extract_score(text: str) -> Optional[float]:
    patterns = (
        r"紅隊存活分數(?:\*\*)?\s*[:：]\s*(?:\*\*)?(\d+(?:\.\d+)?)\s*/\s*100",
        r"加權總分\*\*\s*\|\s*\*\*100%\*\*\s*\|\s*\*\*(\d+(?:\.\d+)?)\*\*",
        r"Survival Score\s*[:：]\s*(\d+(?:\.\d+)?)",
    )
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                pass
    return None


def _extract_grade(text: str, score: Optional[float]) -> str:
    m = re.search(r"等級(?:（[^）]*）)?(?:\*\*)?\s*[:：]\s*[`*]*(A|B|C|D)\b", text)
    if m:
        return m.group(1)
    if score is None:
        return "TBD"
    if score >= 80:
        return "A"
    if score >= 65:
        return "B"
    if score >= 50:
        return "C"
    return "D"


def _extract_kill_switches(lines: Sequence[str]) -> List[str]:
    items: List[str] = []
    in_section = False
    for ln in lines:
        stripped = ln.strip()
        if stripped.startswith("## "):
            in_section = "kill-switch" in stripped.lower()
            continue
        if in_section:
            if stripped.startswith("---"):
                break
            m = re.match(r"^(?:\d+\.|-)\s+(.+)", stripped)
            if m:
                cleaned = re.sub(r"\*\*([^*]+)\*\*", r"\1", m.group(1)).strip()
                if cleaned and not cleaned.endswith("：") and not cleaned.endswith(":"):
                    items.append(cleaned)
    return items[:4]


def load_cases_from_dir(cases_dir: Path) -> List[CaseSummary]:
    if not cases_dir.exists() or not cases_dir.is_dir():
        return []
    summaries: List[CaseSummary] = []
    for fpath in sorted(cases_dir.glob("*.md"), reverse=True):
        if fpath.name.startswith("_") or fpath.name.upper() in {"README.MD", "INDEX.MD"}:
            continue
        summaries.append(parse_case_markdown(fpath))
    return summaries


def parse_holdings_yaml_light(yaml_path: Path) -> List[HoldingItem]:
    """Dependency-free lightweight parser for portfolio/holdings.yaml entries."""
    if not yaml_path.exists() or not yaml_path.is_file():
        return []
    text = yaml_path.read_text(encoding="utf-8", errors="replace")
    items: List[HoldingItem] = []
    current: Dict[str, str] = {}
    in_holdings = False

    def flush() -> None:
        ticker = current.get("ticker", "").strip().strip('"').strip("'")
        market = current.get("market", "").strip().strip('"').strip("'").lower()
        if not ticker or ticker.startswith("<") or market not in {"tw", "us", "etf", "crypto"}:
            return
        weight_raw = current.get("target_weight_pct", "").strip()
        weight: Optional[float] = None
        try:
            weight = float(weight_raw) if weight_raw else None
        except ValueError:
            weight = None
        items.append(
            HoldingItem(
                market=market,
                ticker=ticker,
                name=current.get("name", "").strip().strip('"').strip("'"),
                tier=current.get("tier", "").strip().strip('"').strip("'"),
                target_weight_pct=weight,
                red_team_grade=current.get("red_team_grade", "TBD").strip().strip('"').strip("'"),
            )
        )

    for raw_line in text.splitlines():
        line = re.sub(r"\s+#.*$", "", raw_line)
        stripped = line.strip()
        if not stripped:
            continue
        if stripped == "holdings:":
            in_holdings = True
            continue
        if in_holdings and not raw_line.startswith((" ", "\t", "-")):
            flush()
            current = {}
            in_holdings = False
            continue
        if not in_holdings:
            continue

        if re.match(r"^\s*-\s+id\s*:", raw_line):
            flush()
            current = {}
            continue
        m = re.match(r"^\s*(?:-\s*)?([a-zA-Z0-9_]+)\s*:\s*(.*)$", raw_line)
        if m:
            key, val = m.group(1).strip(), m.group(2).strip()
            if val:
                current[key] = val

    flush()
    return items


def collect_quotes(
    tw: Sequence[str],
    us: Sequence[str],
    etf: Sequence[str],
    crypto: Sequence[str],
    mock: bool = False,
    timeout: float = 10.0,
) -> List[QuoteResult]:
    tw_dedup = list(dict.fromkeys(tw))
    us_dedup = list(dict.fromkeys(us))
    etf_dedup = list(dict.fromkeys(etf))
    crypto_dedup = list(dict.fromkeys(crypto))
    if not any((tw_dedup, us_dedup, etf_dedup, crypto_dedup)):
        return []
    if mock:
        return generate_mock_quotes(tw_dedup, us_dedup, etf_dedup, crypto_dedup)
    quotes: List[QuoteResult] = []
    for s in tw_dedup:
        quotes.append(fetch_equity_quote(normalize_tw_symbol(s), market="tw", timeout=timeout))
    for s in us_dedup:
        quotes.append(fetch_equity_quote(s.strip().upper(), market="us", timeout=timeout))
    for s in etf_dedup:
        quotes.append(fetch_equity_quote(normalize_etf_symbol(s), market="etf", timeout=timeout))
    if crypto_dedup:
        quotes.extend(fetch_crypto_quotes(crypto_dedup, timeout=timeout))
    return quotes


def render_digest_text(
    cases: Sequence[CaseSummary],
    quotes: Sequence[QuoteResult],
    holdings: Sequence[HoldingItem],
    generated_at: str,
) -> str:
    lines = [
        f"Investment Red-Team 分析摘要報告（UTC: {generated_at}）",
        "=" * 56,
        "",
    ]
    if quotes:
        lines.append("[1] 跨市場即時公開報價快照")
        for q in quotes:
            price_str = format_price(q.price) if not q.error else f"ERR ({q.error})"
            lines.append(
                f"  - [{q.market.upper()}] {q.symbol} ({q.name}): {price_str} {q.currency} | {format_change(q.change_pct)}"
            )
        lines.append("")

    lines.append(f"[2] 紅隊個案審計摘要（共 {len(cases)} 筆）")
    if not cases:
        lines.append("  （目前尚無 my-cases/ 個案紀錄，可使用 scripts/new_entry.py 新增第一筆）")
    for c in cases:
        score_txt = f"{c.score:.1f}/100" if c.score is not None else "未評分"
        lines.append(f"  - {c.title} | 狀態: {c.status} | 分數: {score_txt} (等級 {c.grade})")
        for ks in c.kill_switches:
            lines.append(f"      * Kill-Switch: {ks}")
    lines.append("")

    if holdings:
        lines.append(f"[3] 持股追蹤清單（共 {len(holdings)} 檔，不含敏感金額）")
        for h in holdings:
            wt = f"{h.target_weight_pct:.1f}%" if h.target_weight_pct is not None else "N/A"
            lines.append(
                f"  - [{h.market.upper()}] {h.ticker} {h.name} | 層級: {h.tier or '-'} | 目標權重: {wt} | 紅隊評級: {h.red_team_grade}"
            )
        lines.append("")

    lines.append("--")
    lines.append("由 Investment Red-Team 自動化流程產生（僅供紀律審計參考，不構成投資建議）。")
    return "\n".join(lines)


def _grade_badge_style(grade: str) -> Tuple[str, str]:
    styles = {
        "A": ("#065f46", "#d1fae5"),
        "B": ("#1e40af", "#dbeafe"),
        "C": ("#92400e", "#fef3c7"),
        "D": ("#991b1b", "#fee2e2"),
    }
    return styles.get(grade, ("#334155", "#e2e8f0"))


def render_digest_html(
    cases: Sequence[CaseSummary],
    quotes: Sequence[QuoteResult],
    holdings: Sequence[HoldingItem],
    generated_at: str,
) -> str:
    quote_rows = []
    for q in quotes:
        chg_color = "#16a34a" if (q.change_pct or 0) > 0 else ("#dc2626" if (q.change_pct or 0) < 0 else "#64748b")
        price_str = html.escape(format_price(q.price)) if not q.error else f"<span style='color:#dc2626'>Error</span>"
        quote_rows.append(
            f"<tr>"
            f"<td style='padding:8px 10px;border-bottom:1px solid #e2e8f0;font-family:monospace;'>{html.escape(q.market.upper())}</td>"
            f"<td style='padding:8px 10px;border-bottom:1px solid #e2e8f0;font-weight:700;'>{html.escape(q.symbol)}</td>"
            f"<td style='padding:8px 10px;border-bottom:1px solid #e2e8f0;color:#475569;'>{html.escape(q.name)}</td>"
            f"<td style='padding:8px 10px;border-bottom:1px solid #e2e8f0;text-align:right;font-family:monospace;'>{price_str} {html.escape(q.currency)}</td>"
            f"<td style='padding:8px 10px;border-bottom:1px solid #e2e8f0;text-align:right;font-family:monospace;font-weight:700;color:{chg_color};'>{html.escape(format_change(q.change_pct))}</td>"
            f"</tr>"
        )

    quotes_section = ""
    if quote_rows:
        quotes_section = f"""
        <h2 style="font-size:16px;color:#0f172a;margin:22px 0 10px;">1. 跨市場報價快照</h2>
        <table style="width:100%;border-collapse:collapse;font-size:13px;background:#ffffff;border:1px solid #e2e8f0;">
          <thead>
            <tr style="background:#f8fafc;color:#475569;text-align:left;">
              <th style="padding:8px 10px;border-bottom:1px solid #cbd5e1;">市場</th>
              <th style="padding:8px 10px;border-bottom:1px solid #cbd5e1;">代號</th>
              <th style="padding:8px 10px;border-bottom:1px solid #cbd5e1;">名稱</th>
              <th style="padding:8px 10px;border-bottom:1px solid #cbd5e1;text-align:right;">最新價格</th>
              <th style="padding:8px 10px;border-bottom:1px solid #cbd5e1;text-align:right;">漲跌幅</th>
            </tr>
          </thead>
          <tbody>{"".join(quote_rows)}</tbody>
        </table>
        """

    case_cards = []
    for c in cases:
        fg, bg = _grade_badge_style(c.grade)
        score_str = f"{c.score:.1f} / 100" if c.score is not None else "尚未評分"
        ks_html = ""
        if c.kill_switches:
            ks_items = "".join(f"<li style='margin:3px 0;'>{html.escape(k)}</li>" for k in c.kill_switches)
            ks_html = f"<div style='margin-top:8px;font-size:12.5px;color:#334155;'><strong>Kill-Switch 觸發提醒：</strong><ul style='margin:4px 0 0 18px;padding:0;'>{ks_items}</ul></div>"
        case_cards.append(
            f"""
            <div style="border:1px solid #e2e8f0;border-radius:8px;padding:12px 14px;margin-bottom:10px;background:#ffffff;">
              <div style="display:flex;justify-content:space-between;align-items:center;">
                <strong style="font-size:14px;color:#0f172a;">{html.escape(c.title)}</strong>
                <span style="display:inline-block;padding:2px 8px;border-radius:6px;font-size:12px;font-weight:700;color:{fg};background:{bg};">評級 {html.escape(c.grade)}</span>
              </div>
              <div style="font-size:12.5px;color:#64748b;margin-top:4px;">
                狀態：<strong>{html.escape(c.status)}</strong> ｜ 存活分數：<strong>{html.escape(score_str)}</strong> ｜ 檔案：<code>{html.escape(c.filename)}</code>
              </div>
              {ks_html}
            </div>
            """
        )

    cases_body = "".join(case_cards) if case_cards else (
        "<p style='font-size:13px;color:#64748b;background:#f8fafc;padding:12px;border-radius:8px;border:1px solid #e2e8f0;'>"
        "目前尚未偵測到 <code>my-cases/</code> 個案檔案。可執行 <code>python3 scripts/new_entry.py case --market tw --ticker 2330 --name 台積電</code> 建立第一筆紅隊個案。"
        "</p>"
    )

    holdings_section = ""
    if holdings:
        h_rows = "".join(
            f"<tr>"
            f"<td style='padding:7px 10px;border-bottom:1px solid #e2e8f0;'>{html.escape(h.market.upper())}</td>"
            f"<td style='padding:7px 10px;border-bottom:1px solid #e2e8f0;font-weight:700;'>{html.escape(h.ticker)}</td>"
            f"<td style='padding:7px 10px;border-bottom:1px solid #e2e8f0;'>{html.escape(h.name)}</td>"
            f"<td style='padding:7px 10px;border-bottom:1px solid #e2e8f0;'>{html.escape(h.tier or '-')}</td>"
            f"<td style='padding:7px 10px;border-bottom:1px solid #e2e8f0;text-align:right;'>{f'{h.target_weight_pct:.1f}%' if h.target_weight_pct is not None else '-'}</td>"
            f"<td style='padding:7px 10px;border-bottom:1px solid #e2e8f0;text-align:center;font-weight:700;'>{html.escape(h.red_team_grade)}</td>"
            f"</tr>"
            for h in holdings
        )
        holdings_section = f"""
        <h2 style="font-size:16px;color:#0f172a;margin:22px 0 10px;">3. 持股紅隊追蹤概覽（目標權重）</h2>
        <table style="width:100%;border-collapse:collapse;font-size:13px;background:#ffffff;border:1px solid #e2e8f0;">
          <thead>
            <tr style="background:#f8fafc;color:#475569;text-align:left;">
              <th style="padding:7px 10px;border-bottom:1px solid #cbd5e1;">市場</th>
              <th style="padding:7px 10px;border-bottom:1px solid #cbd5e1;">代號</th>
              <th style="padding:7px 10px;border-bottom:1px solid #cbd5e1;">名稱</th>
              <th style="padding:7px 10px;border-bottom:1px solid #cbd5e1;">層級</th>
              <th style="padding:7px 10px;border-bottom:1px solid #cbd5e1;text-align:right;">目標權重</th>
              <th style="padding:7px 10px;border-bottom:1px solid #cbd5e1;text-align:center;">評級</th>
            </tr>
          </thead>
          <tbody>{h_rows}</tbody>
        </table>
        """

    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head><meta charset="UTF-8" /></head>
<body style="margin:0;padding:24px;background:#f1f5f9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Noto Sans TC',sans-serif;color:#0f172a;">
  <div style="max-width:720px;margin:0 auto;background:#ffffff;border-radius:12px;overflow:hidden;border:1px solid #cbd5e1;">
    <div style="background:#0f172a;color:#f8fafc;padding:20px 24px;">
      <div style="font-size:11px;letter-spacing:1px;color:#f87171;font-weight:700;">INVESTMENT RED-TEAM AUTOMATED DIGEST</div>
      <h1 style="margin:6px 0 4px;font-size:20px;">投資紅隊分析結果推播</h1>
      <div style="font-size:12px;color:#94a3b8;">產生時間（UTC）：{html.escape(generated_at)}</div>
    </div>
    <div style="padding:20px 24px;">
      {quotes_section}
      <h2 style="font-size:16px;color:#0f172a;margin:22px 0 10px;">2. 紅隊個案審計與 Kill-Switch 提醒（{len(cases)} 筆）</h2>
      {cases_body}
      {holdings_section}
      <hr style="border:none;border-top:1px solid #e2e8f0;margin:24px 0 14px;" />
      <p style="font-size:11.5px;color:#64748b;margin:0;">
        本郵件由 Investment Red-Team 私有工作區自動化流程寄送。所有數據僅供投資風險紀律覆核，不構成買賣建議。
      </p>
    </div>
  </div>
</body>
</html>
"""


def send_email_smtp(
    subject: str,
    plain_body: str,
    html_body: str,
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_pass: str,
    recipients: Sequence[str],
) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = ", ".join(recipients)
    msg.set_content(plain_body)
    msg.add_alternative(html_body, subtype="html")

    context = ssl.create_default_context()
    if smtp_port == 465:
        with smtplib.SMTP_SSL(smtp_host, smtp_port, context=context, timeout=20) as server:
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)
    else:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="irt-notify",
        description="Generate Investment Red-Team digest and push via SMTP email (zero hardcoded credentials).",
    )
    parser.add_argument("--cases-dir", help="Directory containing red-team Markdown cases (e.g. my-cases/)")
    parser.add_argument("--holdings", help="Optional path to holdings.yaml to auto-include tickers and weight overview")
    parser.add_argument("--tw", action="append", help="Extra Taiwan stock tickers")
    parser.add_argument("--us", action="append", help="Extra US stock tickers")
    parser.add_argument("--etf", action="append", help="Extra ETF tickers")
    parser.add_argument("--crypto", action="append", help="Extra Crypto ids/symbols")
    parser.add_argument("--subject", default="", help="Custom email subject")
    parser.add_argument("--mock", action="store_true", help="Use offline mock quotes for testing")
    parser.add_argument("--dry-run", action="store_true", help="Generate report without sending email")
    parser.add_argument("--out-html", help="Optional path to save rendered HTML preview")
    parser.add_argument("--timeout", type=float, default=10.0, help="HTTP timeout for quote fetching")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    cases = load_cases_from_dir(Path(args.cases_dir)) if args.cases_dir else []
    holdings = parse_holdings_yaml_light(Path(args.holdings)) if args.holdings else []

    tw_list = parse_symbol_list(args.tw) + [h.ticker for h in holdings if h.market == "tw"]
    us_list = parse_symbol_list(args.us) + [h.ticker for h in holdings if h.market == "us"]
    etf_list = parse_symbol_list(args.etf) + [h.ticker for h in holdings if h.market == "etf"]
    crypto_list = parse_symbol_list(args.crypto) + [h.ticker for h in holdings if h.market == "crypto"]

    quotes = collect_quotes(tw_list, us_list, etf_list, crypto_list, mock=args.mock, timeout=args.timeout)

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    today = generated_at[:10]
    subject = args.subject.strip() or f"[Investment Red-Team] {today} 投資紅隊分析結果推播（{len(cases)} 筆個案）"

    plain_body = render_digest_text(cases, quotes, holdings, generated_at)
    html_body = render_digest_html(cases, quotes, holdings, generated_at)

    if args.out_html:
        out_path = Path(args.out_html)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(html_body, encoding="utf-8")
        print(f"[irt-notify] Wrote HTML preview to {out_path}")

    if args.dry_run:
        print(f"[irt-notify] DRY-RUN Subject: {subject}")
        print(plain_body)
        return 0

    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com").strip() or "smtp.gmail.com"
    smtp_port = int(os.environ.get("SMTP_PORT", "465").strip() or "465")
    smtp_user = os.environ.get("SMTP_USER", "").strip()
    smtp_pass = os.environ.get("SMTP_PASS", "").strip()
    raw_to = os.environ.get("NOTIFY_EMAIL_TO", "").strip() or smtp_user
    recipients = [addr.strip() for addr in raw_to.split(",") if addr.strip()]

    if not smtp_user or not smtp_pass or not recipients:
        print(
            "[irt-notify] ERROR: Missing SMTP_USER or SMTP_PASS environment variables. "
            "Configure them in GitHub Actions Secrets (or use --dry-run).",
            file=sys.stderr,
        )
        return 2

    send_email_smtp(
        subject=subject,
        plain_body=plain_body,
        html_body=html_body,
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        smtp_user=smtp_user,
        smtp_pass=smtp_pass,
        recipients=recipients,
    )
    print(f"[irt-notify] SUCCESS: Sent digest email to {len(recipients)} recipient(s) via {smtp_host}:{smtp_port}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
