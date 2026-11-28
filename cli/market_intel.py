#!/usr/bin/env python3
"""Market Intelligence Radar CLI (`irt-intel`) — Technical Regime + Trusted News Shock Detector.

Zero external runtime dependencies (Python stdlib only) and strictly zero PII.
Generates a unified `00_context_packet.md` (and optional JSON) combining:
1. Technical indicators (MA20/MA60/MA200, RSI-14, 20D drawdown, Volume Ratio, Gap %)
2. Trusted-source news filtering (Tier-1 & Tier-2 publishers whitelist)
3. Automatic Event Shock / Reversal regime classification & dynamic weight recommendation
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from cli.quote_fetch import (
    USER_AGENT,
    normalize_etf_symbol,
    normalize_tw_symbol,
    parse_symbol_list,
)

# Trusted news publishers whitelist (Tier 1 = wire/official/top tier; Tier 2 = mainstream financial media)
TIER1_PUBLISHERS = (
    "reuters",
    "bloomberg",
    "wall street journal",
    "wsj",
    "financial times",
    "associated press",
    "ap news",
    "sec.gov",
    "pr newswire",
    "business wire",
    "中央社",
    "cna",
    "公開資訊觀測站",
)

TIER2_PUBLISHERS = (
    "cnbc",
    "yahoo finance",
    "barron",
    "marketwatch",
    "investing.com",
    "nikkei",
    "經濟日報",
    "工商時報",
    "鉅亨網",
    "cnyes",
    "moneydj",
    "財訊",
    "商業周刊",
    "數位時代",
    "科技新報",
)

# High-impact event shock keywords (Governance / Regulatory / Supply Chain / Guidance)
SHOCK_KEYWORDS: Dict[str, Tuple[str, ...]] = {
    "TYPE_A_GOVERNANCE": (
        "resign", "resigns", "resignation", "steps down", "ousted", "fired",
        "returns as ceo", "reinstated", "ceo", "chief executive", "successor",
        "辭職", "請辭", "復職", "回歸", "執行長", "董事長", "換帥", "內鬥", "卸任",
    ),
    "TYPE_B_FUNDAMENTAL_SHOCK": (
        "ban", "export control", "sanction", "antitrust", "investigation", "subpoena",
        "guidance cut", "slashes outlook", "order cut", "recall", "layoff",
        "禁令", "制裁", "出口管制", "砍單", "下修財測", "反壟斷", "調查", "召回",
    ),
    "TYPE_C_MACRO_TARIFF": (
        "tariff", "fed rate", "recession", "war", "geopolitical", "currency",
        "關稅", "升息", "降息", "匯率", "地緣政治", "景氣衰退",
    ),
    "REVERSAL_CATALYST": (
        "reinstated", "returns", "lifted", "approved", "beats estimates", "raises guidance",
        "復職", "回歸", "解禁", "撤銷禁令", "上修財測", "優於預期",
    ),
}


@dataclass
class TechnicalSnapshot:
    market: str
    symbol: str
    name: str
    currency: str
    close: Optional[float]
    change_1d_pct: Optional[float]
    change_5d_pct: Optional[float]
    change_20d_pct: Optional[float]
    ma20: Optional[float]
    ma60: Optional[float]
    ma200: Optional[float]
    rsi14: Optional[float]
    drawdown_20d_pct: Optional[float]
    volume_ratio_20d: Optional[float]
    gap_pct: Optional[float]
    regime: str
    signals: List[str] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class TrustedNewsItem:
    query: str
    title: str
    source: str
    tier: str  # TIER_1 | TIER_2
    published: str
    link: str
    event_tags: List[str] = field(default_factory=list)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def compute_sma(values: Sequence[float], period: int) -> Optional[float]:
    if len(values) < period or period <= 0:
        return None
    return round(sum(values[-period:]) / period, 4)


def compute_rsi(closes: Sequence[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    gains: List[float] = []
    losses: List[float] = []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 2)


def classify_technical_regime(
    close: float,
    change_1d: Optional[float],
    change_5d: Optional[float],
    ma20: Optional[float],
    ma60: Optional[float],
    drawdown_20d: Optional[float],
    vol_ratio: Optional[float],
    gap_pct: Optional[float],
) -> Tuple[str, List[str]]:
    signals: List[str] = []
    if change_1d is not None and change_1d <= -5.0:
        signals.append(f"單日重挫 {change_1d:.2f}%（高波動警示）")
    if change_5d is not None and change_5d <= -10.0:
        signals.append(f"近 5 日急跌 {change_5d:.2f}%（嚴防事件性殺盤）")
    if drawdown_20d is not None and drawdown_20d <= -15.0:
        signals.append(f"距 20 日高點回撤 {drawdown_20d:.2f}%")
    if change_1d is not None and change_1d >= 5.0 and (vol_ratio or 0) >= 1.4:
        signals.append(f"帶量強彈 +{change_1d:.2f}%（量比 {vol_ratio:.2f}x）")
    if gap_pct is not None and abs(gap_pct) >= 3.0:
        direction = "向上" if gap_pct > 0 else "向下"
        signals.append(f"{direction}跳空缺口 {gap_pct:+.2f}%")
    if ma20 is not None and close < ma20:
        signals.append("收盤跌破月線 (20MA)")
    if ma60 is not None and close < ma60:
        signals.append("收盤跌破季線 (60MA)")

    if (change_1d is not None and change_1d >= 5.0 and (vol_ratio or 1.0) >= 1.4) or (
        gap_pct is not None and gap_pct >= 4.0 and (change_1d or 0) >= 4.0
    ):
        return "EVENT_REVERSAL_BREAKOUT", signals
    if (
        (change_1d is not None and change_1d <= -5.0)
        or (change_5d is not None and change_5d <= -10.0)
        or (drawdown_20d is not None and drawdown_20d <= -18.0)
    ):
        return "EVENT_SHOCK_SELLOFF", signals
    if ma20 is not None and ma60 is not None and close > ma20 > ma60:
        return "UPTREND_HEALTHY", signals or ["多頭排列（站穩月線與季線之上）"]
    if ma60 is not None and close < ma60:
        return "DOWNTREND_BELOW_MA60", signals or ["位於季線之下（偏弱防守）"]
    return "CONSOLIDATION", signals or ["區間震盪整理"]


def compute_technical_from_yahoo_chart(payload: dict, market: str, fallback_symbol: str) -> TechnicalSnapshot:
    chart = payload.get("chart") or {}
    results = chart.get("result") or []
    if not results:
        err = (chart.get("error") or {}).get("description") or "No chart data"
        return TechnicalSnapshot(
            market=market, symbol=fallback_symbol, name=fallback_symbol, currency="N/A",
            close=None, change_1d_pct=None, change_5d_pct=None, change_20d_pct=None,
            ma20=None, ma60=None, ma200=None, rsi14=None, drawdown_20d_pct=None,
            volume_ratio_20d=None, gap_pct=None, regime="UNAVAILABLE", error=err,
        )

    res = results[0]
    meta = res.get("meta") or {}
    quote = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    raw_closes = quote.get("close") or []
    raw_opens = quote.get("open") or []
    raw_highs = quote.get("high") or []
    raw_vols = quote.get("volume") or []

    bars: List[Tuple[float, float, float, float]] = []
    for o, h, c, v in zip(raw_opens, raw_highs, raw_closes, raw_vols):
        if isinstance(c, (int, float)) and c > 0:
            bars.append((
                float(o) if isinstance(o, (int, float)) else float(c),
                float(h) if isinstance(h, (int, float)) else float(c),
                float(c),
                float(v) if isinstance(v, (int, float)) else 0.0,
            ))

    if not bars:
        return TechnicalSnapshot(
            market=market, symbol=fallback_symbol, name=fallback_symbol,
            currency=str(meta.get("currency") or "USD"), close=None,
            change_1d_pct=None, change_5d_pct=None, change_20d_pct=None,
            ma20=None, ma60=None, ma200=None, rsi14=None, drawdown_20d_pct=None,
            volume_ratio_20d=None, gap_pct=None, regime="UNAVAILABLE", error="No valid OHLCV bars",
        )

    closes = [b[2] for b in bars]
    highs = [b[1] for b in bars]
    vols = [b[3] for b in bars]

    latest_open, _, latest_close, latest_vol = bars[-1]
    change_1d = round(((latest_close - closes[-2]) / closes[-2]) * 100.0, 2) if len(closes) >= 2 else None
    change_5d = round(((latest_close - closes[-6]) / closes[-6]) * 100.0, 2) if len(closes) >= 6 else None
    change_20d = round(((latest_close - closes[-21]) / closes[-21]) * 100.0, 2) if len(closes) >= 21 else None
    gap_pct = round(((latest_open - closes[-2]) / closes[-2]) * 100.0, 2) if len(closes) >= 2 else None

    ma20 = compute_sma(closes, 20)
    ma60 = compute_sma(closes, 60)
    ma200 = compute_sma(closes, 200)
    rsi14 = compute_rsi(closes, 14)

    recent_high_20 = max(highs[-20:]) if highs else latest_close
    drawdown_20d = round(((latest_close - recent_high_20) / recent_high_20) * 100.0, 2) if recent_high_20 else None

    vol_window = [v for v in vols[-21:-1] if v > 0]
    vol_ratio = round(latest_vol / (sum(vol_window) / len(vol_window)), 2) if vol_window and latest_vol > 0 else None

    regime, signals = classify_technical_regime(
        latest_close, change_1d, change_5d, ma20, ma60, drawdown_20d, vol_ratio, gap_pct
    )

    return TechnicalSnapshot(
        market=market,
        symbol=str(meta.get("symbol") or fallback_symbol),
        name=str(meta.get("shortName") or meta.get("longName") or fallback_symbol),
        currency=str(meta.get("currency") or "USD"),
        close=round(latest_close, 4),
        change_1d_pct=change_1d,
        change_5d_pct=change_5d,
        change_20d_pct=change_20d,
        ma20=ma20,
        ma60=ma60,
        ma200=ma200,
        rsi14=rsi14,
        drawdown_20d_pct=drawdown_20d,
        volume_ratio_20d=vol_ratio,
        gap_pct=gap_pct,
        regime=regime,
        signals=signals,
    )


def fetch_technical_snapshot(symbol: str, market: str, timeout: float = 10.0) -> TechnicalSnapshot:
    encoded = urllib.parse.quote(symbol, safe="")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}?interval=1d&range=1y"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        return compute_technical_from_yahoo_chart(payload, market=market, fallback_symbol=symbol)
    except Exception as exc:
        return TechnicalSnapshot(
            market=market, symbol=symbol, name=symbol,
            currency="TWD" if symbol.endswith((".TW", ".TWO")) else "USD",
            close=None, change_1d_pct=None, change_5d_pct=None, change_20d_pct=None,
            ma20=None, ma60=None, ma200=None, rsi14=None, drawdown_20d_pct=None,
            volume_ratio_20d=None, gap_pct=None, regime="UNAVAILABLE", error=str(exc),
        )


def classify_publisher_tier(source_name: str, title: str = "") -> Optional[str]:
    haystack = f"{source_name} {title}".lower()
    if any(pub in haystack for pub in TIER1_PUBLISHERS):
        return "TIER_1"
    if any(pub in haystack for pub in TIER2_PUBLISHERS):
        return "TIER_2"
    return None


def detect_event_tags(text: str) -> List[str]:
    lower = text.lower()
    tags: List[str] = []
    for tag, keywords in SHOCK_KEYWORDS.items():
        if any(kw in lower for kw in keywords):
            tags.append(tag)
    return tags


def parse_google_news_rss(xml_text: str, query: str, max_items: int = 6) -> List[TrustedNewsItem]:
    root = ET.fromstring(xml_text)
    items: List[TrustedNewsItem] = []
    for elem in root.findall("./channel/item"):
        title = (elem.findtext("title") or "").strip()
        link = (elem.findtext("link") or "").strip()
        pub_date = (elem.findtext("pubDate") or "").strip()
        source_el = elem.find("source")
        source_name = (source_el.text or "").strip() if source_el is not None and source_el.text else ""
        if not source_name and " - " in title:
            source_name = title.rsplit(" - ", 1)[-1].strip()

        tier = classify_publisher_tier(source_name, title)
        if not tier:
            continue  # Strictly filter out unverified Tier-3 rumor/social blogs

        tags = detect_event_tags(title)
        items.append(
            TrustedNewsItem(
                query=query,
                title=title,
                source=source_name or "Trusted",
                tier=tier,
                published=pub_date,
                link=link,
                event_tags=tags,
            )
        )
        if len(items) >= max_items:
            break
    # Sort Tier-1 + event-tagged news first
    items.sort(key=lambda x: (0 if x.tier == "TIER_1" else 1, 0 if x.event_tags else 1))
    return items


def fetch_trusted_news(query: str, lang: str = "en", max_items: int = 5, timeout: float = 10.0) -> List[TrustedNewsItem]:
    encoded_q = urllib.parse.quote(query)
    if lang == "zh-TW":
        url = f"https://news.google.com/rss/search?q={encoded_q}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    else:
        url = f"https://news.google.com/rss/search?q={encoded_q}&hl=en-US&gl=US&ceid=US:en"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            xml_text = resp.read().decode("utf-8", errors="replace")
        return parse_google_news_rss(xml_text, query=query, max_items=max_items)
    except Exception:
        return []


def determine_overall_regime(
    technicals: Sequence[TechnicalSnapshot],
    news_items: Sequence[TrustedNewsItem],
) -> Tuple[str, Dict[str, int], str]:
    has_reversal_news = any("REVERSAL_CATALYST" in n.event_tags and n.tier == "TIER_1" for n in news_items)
    has_shock_news = any(
        any(t in {"TYPE_A_GOVERNANCE", "TYPE_B_FUNDAMENTAL_SHOCK"} for t in n.event_tags)
        and n.tier == "TIER_1"
        for n in news_items
    )
    has_tech_reversal = any(t.regime == "EVENT_REVERSAL_BREAKOUT" for t in technicals)
    has_tech_shock = any(t.regime == "EVENT_SHOCK_SELLOFF" for t in technicals)

    if has_reversal_news or has_tech_reversal:
        return (
            "EVENT_REVERSAL",
            {"news_events": 45, "technical_price": 35, "fundamentals_claude": 20, "chips_risk": 0},
            "偵測到強反轉催化或帶量跳空突破：優先確認反轉消息真實性與右側缺口防守位。",
        )
    if has_shock_news or has_tech_shock:
        return (
            "EVENT_SHOCK",
            {"news_events": 40, "technical_price": 35, "fundamentals_claude": 15, "chips_risk": 10},
            "⚠️ 偵測到重大事件衝擊或異常急殺：強制將財報權重壓低至 15%，嚴禁單憑財報佳績左側硬接飛刀！",
        )
    return (
        "NORMAL",
        {"news_events": 25, "technical_price": 25, "fundamentals_claude": 30, "chips_risk": 20},
        "常態波動期：維持平衡權重（財報底氣上限 30%），依紅隊分數紀律執行。",
    )


def generate_mock_intel(
    tw: Sequence[str], us: Sequence[str], etf: Sequence[str]
) -> Tuple[List[TechnicalSnapshot], List[TrustedNewsItem]]:
    technicals: List[TechnicalSnapshot] = []
    for raw in tw:
        sym = normalize_tw_symbol(raw)
        technicals.append(
            TechnicalSnapshot(
                market="tw", symbol=sym, name=f"{sym} (Mock)", currency="TWD",
                close=960.0, change_1d_pct=-3.2, change_5d_pct=-7.8, change_20d_pct=-5.1,
                ma20=1010.0, ma60=985.0, ma200=890.0, rsi14=36.4,
                drawdown_20d_pct=-11.2, volume_ratio_20d=1.65, gap_pct=-2.4,
                regime="DOWNTREND_BELOW_MA60",
                signals=["收盤跌破月線 (20MA)", "收盤跌破季線 (60MA)"],
            )
        )
    for raw in us:
        sym = raw.strip().upper()
        technicals.append(
            TechnicalSnapshot(
                market="us", symbol=sym, name=f"{sym} (Mock)", currency="USD",
                close=118.5, change_1d_pct=-6.4, change_5d_pct=-14.2, change_20d_pct=-19.5,
                ma20=138.0, ma60=132.0, ma200=115.0, rsi14=28.9,
                drawdown_20d_pct=-22.4, volume_ratio_20d=2.35, gap_pct=-4.1,
                regime="EVENT_SHOCK_SELLOFF",
                signals=["單日重挫 -6.40%（高波動警示）", "近 5 日急跌 -14.20%（嚴防事件性殺盤）", "向下跳空缺口 -4.10%"],
            )
        )
    for raw in etf:
        sym = normalize_etf_symbol(raw)
        technicals.append(
            TechnicalSnapshot(
                market="etf", symbol=sym, name=f"{sym} (Mock)",
                currency="TWD" if sym.endswith((".TW", ".TWO")) else "USD",
                close=535.0, change_1d_pct=-0.8, change_5d_pct=-1.5, change_20d_pct=1.2,
                ma20=532.0, ma60=520.0, ma200=490.0, rsi14=52.0,
                drawdown_20d_pct=-2.1, volume_ratio_20d=1.05, gap_pct=-0.2,
                regime="UPTREND_HEALTHY",
                signals=["多頭排列（站穩月線與季線之上）"],
            )
        )

    news = [
        TrustedNewsItem(
            query=us[0] if us else "NVDA",
            title="Sample Tier-1 Governance Event: Executive transition rattles semiconductor supply chain despite record quarter - Reuters",
            source="Reuters",
            tier="TIER_1",
            published=utc_now_iso(),
            link="https://www.reuters.com/",
            event_tags=["TYPE_A_GOVERNANCE"],
        ),
        TrustedNewsItem(
            query=tw[0] if tw else "2330",
            title="美系大廠消息面震盪引發台股 AI 供應鏈波動，法人關注量縮止跌訊號 - 經濟日報",
            source="經濟日報",
            tier="TIER_2",
            published=utc_now_iso(),
            link="https://money.udn.com/",
            event_tags=[],
        ),
    ]
    return technicals, news


def render_context_markdown(
    technicals: Sequence[TechnicalSnapshot],
    news_items: Sequence[TrustedNewsItem],
    generated_at: str,
) -> str:
    regime, weights, guidance = determine_overall_regime(technicals, news_items)
    lines = [
        f"# 00_Context_Packet｜跨市場技術線型與可信新聞數據包",
        "",
        f"- **產生時間（UTC）**：`{generated_at}`",
        f"- **自動判定市場狀態（Regime）**：**`{regime}`**",
        f"- **動態建議權重**：可信新聞 **{weights['news_events']}%** ｜ 技術線型 **{weights['technical_price']}%** ｜ Claude 財報底氣 **{weights['fundamentals_claude']}%** ｜ 籌碼風控 **{weights['chips_risk']}%**",
        f"- **操作總指引**：{guidance}",
        "",
        "---",
        "",
        "## 一、技術線型與量價結構快照（供 Gemini Pro #2 判讀）",
        "",
        "| 市場 | 代號 | 收盤 | 1D | 5D | 20D回撤 | MA20 | MA60 | RSI14 | 量比 | 價格狀態（Regime） | 關鍵訊號 |",
        "| :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | :--- | :--- |",
    ]
    for t in technicals:
        if t.error:
            lines.append(f"| `{t.market}` | **{t.symbol}** | ERR | - | - | - | - | - | - | - | `{t.regime}` | `{t.error}` |")
            continue
        ma20_s = f"{t.ma20:,.2f}" if t.ma20 is not None else "N/A"
        ma60_s = f"{t.ma60:,.2f}" if t.ma60 is not None else "N/A"
        rsi_s = f"{t.rsi14:.1f}" if t.rsi14 is not None else "N/A"
        vr_s = f"{t.volume_ratio_20d:.2f}x" if t.volume_ratio_20d is not None else "N/A"
        dd_s = f"{t.drawdown_20d_pct:+.1f}%" if t.drawdown_20d_pct is not None else "N/A"
        c1 = f"{t.change_1d_pct:+.2f}%" if t.change_1d_pct is not None else "N/A"
        c5 = f"{t.change_5d_pct:+.2f}%" if t.change_5d_pct is not None else "N/A"
        sig_s = "；".join(t.signals[:2]) if t.signals else "-"
        lines.append(
            f"| `{t.market}` | **{t.symbol}** | {t.close:,.2f} | {c1} | {c5} | {dd_s} | {ma20_s} | {ma60_s} | {rsi_s} | {vr_s} | `{t.regime}` | {sig_s} |"
        )

    lines.extend([
        "",
        "---",
        "",
        "## 二、可信來源新聞事件雷達（僅保留 Tier-1 / Tier-2，已過濾社群傳聞｜供 Gemini Pro #1 判讀）",
        "",
    ])
    if not news_items:
        lines.append("- 目前查詢標的未檢出近期 Tier-1 / Tier-2 重大新聞（或處於離線無新聞結果）。")
    else:
        for idx, n in enumerate(news_items, start=1):
            tag_str = f" `{' / '.join(n.event_tags)}`" if n.event_tags else ""
            lines.append(f"{idx}. **[{n.tier}]**{tag_str} {n.title}")
            lines.append(f"   - 來源：{n.source} ｜ 查詢關鍵字：`{n.query}` ｜ 時間：{n.published}")
            lines.append(f"   - 連結：{n.link}")

    lines.extend([
        "",
        "---",
        "",
        "## 三、四帳號交棒步驟（Shared Hub Workflow）",
        "1. **Gemini Pro #1（新聞事件官）**：讀取第二節 + 即時搜尋，產出 `01_news_event_brief.md`（標註 Type A/B/C/D 與台股供應鏈連動）。",
        "2. **Gemini Pro #2（技術線型官）**：讀取第一節技術矩陣，產出 `02_technical_regime.md`（標出嚴禁接刀區與右側確認點）。",
        "3. **Claude Pro #1（財報底氣官，權重強制鎖定受控）**：評估事件是否摧毀未來 4 季現金流與護城河，產出 `03_fundamental_redteam.md`。",
        "4. **Gemini Pro #3（動態權重總裁判）**：讀取上述三份報告並套用上方動態權重，產出 `04_final_verdict.md` 存入 `my-cases/` 觸發 Email 推播。",
    ])
    return "\n".join(lines)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="irt-intel",
        description="Generate v2 Technical Regime + Trusted News Event context packet for the 4-account Red-Team Hub.",
    )
    parser.add_argument("--tw", action="append", help="Taiwan stock tickers (e.g. 2330,2382)")
    parser.add_argument("--us", action="append", help="US stock tickers (e.g. NVDA,AAPL)")
    parser.add_argument("--etf", action="append", help="ETF tickers (e.g. 0050,VOO)")
    parser.add_argument("--news-query", action="append", help="Extra trusted news search queries")
    parser.add_argument("--max-news", type=int, default=4, help="Max trusted news items per query")
    parser.add_argument("--out-md", help="Write Markdown context packet to file")
    parser.add_argument("--out-json", help="Write JSON context packet to file")
    parser.add_argument("--mock", action="store_true", help="Use deterministic offline mock data")
    parser.add_argument("--timeout", type=float, default=10.0, help="HTTP timeout seconds")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    tw_list = parse_symbol_list(args.tw)
    us_list = parse_symbol_list(args.us)
    etf_list = parse_symbol_list(args.etf)
    extra_queries = parse_symbol_list(args.news_query)

    if not any((tw_list, us_list, etf_list, extra_queries)):
        parser.error("Provide at least one ticker via --tw, --us, --etf, or --news-query.")

    generated_at = utc_now_iso()
    if args.mock:
        technicals, news_items = generate_mock_intel(tw_list, us_list, etf_list)
    else:
        technicals = []
        for raw in tw_list:
            technicals.append(fetch_technical_snapshot(normalize_tw_symbol(raw), market="tw", timeout=args.timeout))
        for raw in us_list:
            technicals.append(fetch_technical_snapshot(raw.strip().upper(), market="us", timeout=args.timeout))
        for raw in etf_list:
            technicals.append(fetch_technical_snapshot(normalize_etf_symbol(raw), market="etf", timeout=args.timeout))

        news_items = []
        seen_titles = set()
        for q in us_list:
            for item in fetch_trusted_news(f"{q} stock", lang="en", max_items=args.max_news, timeout=args.timeout):
                if item.title not in seen_titles:
                    seen_titles.add(item.title)
                    news_items.append(item)
        for q in tw_list:
            for item in fetch_trusted_news(f"{q} 台股", lang="zh-TW", max_items=args.max_news, timeout=args.timeout):
                if item.title not in seen_titles:
                    seen_titles.add(item.title)
                    news_items.append(item)
        for q in extra_queries:
            lang = "zh-TW" if any("\u4e00" <= ch <= "\u9fff" for ch in q) else "en"
            for item in fetch_trusted_news(q, lang=lang, max_items=args.max_news, timeout=args.timeout):
                if item.title not in seen_titles:
                    seen_titles.add(item.title)
                    news_items.append(item)

    md_text = render_context_markdown(technicals, news_items, generated_at)
    regime, weights, guidance = determine_overall_regime(technicals, news_items)

    if args.out_md:
        p = Path(args.out_md)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(md_text + "\n", encoding="utf-8")
        print(f"[irt-intel] Wrote Markdown context packet to {p}")
    else:
        print(md_text)

    if args.out_json:
        pj = Path(args.out_json)
        pj.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "generated_at_utc": generated_at,
            "regime": regime,
            "dynamic_weights": weights,
            "guidance": guidance,
            "technicals": [asdict(t) for t in technicals],
            "trusted_news": [asdict(n) for n in news_items],
        }
        pj.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"[irt-intel] Wrote JSON context packet to {pj}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
