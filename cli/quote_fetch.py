#!/usr/bin/env python3
"""Cross-market public quote fetcher CLI for Investment Red-Team.

Supports Taiwan stocks (tw), US stocks (us), ETFs (etf), and Crypto (crypto)
using Python standard library only (zero external packages, no API keys needed).
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Iterable, List, Optional, Sequence

USER_AGENT = "investment-red-team-cli/0.1 (+https://github.com/987989-droid/investment-red-team)"

CRYPTO_ALIASES = {
    "btc": "bitcoin",
    "eth": "ethereum",
    "sol": "solana",
    "bnb": "binancecoin",
    "xrp": "ripple",
    "ada": "cardano",
    "doge": "dogecoin",
    "avax": "avalanche-2",
    "dot": "polkadot",
    "link": "chainlink",
    "sui": "sui",
    "usdt": "tether",
    "usdc": "usd-coin",
}

BINANCE_SYMBOLS = {
    "bitcoin": "BTCUSDT",
    "ethereum": "ETHUSDT",
    "solana": "SOLUSDT",
    "binancecoin": "BNBUSDT",
    "ripple": "XRPUSDT",
    "cardano": "ADAUSDT",
    "dogecoin": "DOGEUSDT",
    "avalanche-2": "AVAXUSDT",
    "polkadot": "DOTUSDT",
    "chainlink": "LINKUSDT",
    "sui": "SUIUSDT",
}


@dataclass
class QuoteResult:
    market: str
    symbol: str
    name: str
    price: Optional[float]
    change_pct: Optional[float]
    currency: str
    source: str
    timestamp_utc: str
    error: Optional[str] = None


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_symbol_list(values: Optional[Sequence[str]]) -> List[str]:
    if not values:
        return []
    result: List[str] = []
    for item in values:
        for part in item.split(","):
            cleaned = part.strip()
            if cleaned:
                result.append(cleaned)
    return result


def normalize_tw_symbol(raw: str) -> str:
    s = raw.strip().upper()
    if s.endswith(".TW") or s.endswith(".TWO"):
        return s
    return f"{s}.TW"


def normalize_etf_symbol(raw: str) -> str:
    s = raw.strip().upper()
    if s.isdigit() and len(s) >= 4:
        return f"{s}.TW"
    if s.startswith("00") and not (s.endswith(".TW") or s.endswith(".TWO")):
        return f"{s}.TW"
    return s


def normalize_crypto_id(raw: str) -> str:
    key = raw.strip().lower()
    return CRYPTO_ALIASES.get(key, key)


def http_get_json(url: str, timeout: float = 10.0) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw)


def http_get_text(url: str, timeout: float = 10.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse_yahoo_chart_payload(payload: dict, market: str, fallback_symbol: str) -> QuoteResult:
    chart = payload.get("chart") or {}
    results = chart.get("result") or []
    if not results:
        err = (chart.get("error") or {}).get("description") or "No chart result returned"
        return QuoteResult(
            market=market,
            symbol=fallback_symbol,
            name=fallback_symbol,
            price=None,
            change_pct=None,
            currency="N/A",
            source="yahoo-chart",
            timestamp_utc=utc_now_iso(),
            error=err,
        )

    meta = results[0].get("meta") or {}
    price = meta.get("regularMarketPrice")
    prev_close = meta.get("chartPreviousClose") or meta.get("previousClose")
    change_pct: Optional[float] = None
    if isinstance(price, (int, float)) and isinstance(prev_close, (int, float)) and prev_close:
        change_pct = round(((price - prev_close) / prev_close) * 100.0, 2)

    return QuoteResult(
        market=market,
        symbol=str(meta.get("symbol") or fallback_symbol),
        name=str(meta.get("shortName") or meta.get("longName") or fallback_symbol),
        price=round(float(price), 4) if isinstance(price, (int, float)) else None,
        change_pct=change_pct,
        currency=str(meta.get("currency") or "USD"),
        source="yahoo-chart",
        timestamp_utc=utc_now_iso(),
    )


def fetch_yahoo_quote(symbol: str, market: str, timeout: float = 10.0) -> QuoteResult:
    encoded = urllib.parse.quote(symbol, safe="")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}?interval=1d&range=5d"
    payload = http_get_json(url, timeout=timeout)
    return parse_yahoo_chart_payload(payload, market=market, fallback_symbol=symbol)


def fetch_twse_fallback(symbol: str, market: str, timeout: float = 10.0) -> QuoteResult:
    code = symbol.split(".")[0]
    prefix = "otc" if symbol.endswith(".TWO") else "tse"
    url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={prefix}_{code}.tw&json=1&delay=0"
    payload = http_get_json(url, timeout=timeout)
    items = payload.get("msgArray") or []
    if not items:
        raise ValueError("Empty TWSE response")
    item = items[0]
    raw_price = item.get("z")
    if not raw_price or raw_price == "-":
        raw_price = item.get("y")
    prev_close_raw = item.get("y")
    price = float(raw_price) if raw_price and raw_price != "-" else None
    prev_close = float(prev_close_raw) if prev_close_raw and prev_close_raw != "-" else None
    change_pct = None
    if price is not None and prev_close:
        change_pct = round(((price - prev_close) / prev_close) * 100.0, 2)
    return QuoteResult(
        market=market,
        symbol=symbol,
        name=str(item.get("n") or symbol),
        price=round(price, 4) if price is not None else None,
        change_pct=change_pct,
        currency="TWD",
        source="twse-mis",
        timestamp_utc=utc_now_iso(),
    )


def fetch_stooq_fallback(symbol: str, market: str, timeout: float = 10.0) -> QuoteResult:
    stooq_sym = symbol.lower() if "." in symbol else f"{symbol.lower()}.us"
    url = f"https://stooq.com/q/l/?s={urllib.parse.quote(stooq_sym)}&f=sd2t2ohlcv&h&e=csv"
    text = http_get_text(url, timeout=timeout)
    reader = csv.DictReader(io.StringIO(text))
    row = next(reader, None)
    if not row or not row.get("Close") or row["Close"] == "N/D":
        raise ValueError("No Stooq quote data")
    close_val = float(row["Close"])
    open_val = float(row["Open"]) if row.get("Open") and row["Open"] != "N/D" else None
    change_pct = round(((close_val - open_val) / open_val) * 100.0, 2) if open_val else None
    return QuoteResult(
        market=market,
        symbol=symbol.upper(),
        name=symbol.upper(),
        price=round(close_val, 4),
        change_pct=change_pct,
        currency="USD",
        source="stooq",
        timestamp_utc=utc_now_iso(),
    )


def fetch_crypto_quotes(raw_ids: Sequence[str], timeout: float = 10.0) -> List[QuoteResult]:
    if not raw_ids:
        return []
    ids = [normalize_crypto_id(x) for x in raw_ids]
    joined = urllib.parse.quote(",".join(ids))
    url = (
        f"https://api.coingecko.com/api/v3/simple/price"
        f"?ids={joined}&vs_currencies=usd&include_24hr_change=true"
    )
    results: List[QuoteResult] = []
    try:
        payload = http_get_json(url, timeout=timeout)
        for cid in ids:
            entry = payload.get(cid)
            if isinstance(entry, dict) and "usd" in entry:
                chg = entry.get("usd_24h_change")
                results.append(
                    QuoteResult(
                        market="crypto",
                        symbol=cid.upper(),
                        name=cid,
                        price=round(float(entry["usd"]), 6),
                        change_pct=round(float(chg), 2) if isinstance(chg, (int, float)) else None,
                        currency="USD",
                        source="coingecko",
                        timestamp_utc=utc_now_iso(),
                    )
                )
            else:
                results.append(_fetch_binance_fallback(cid, timeout=timeout))
        return results
    except Exception:
        return [_fetch_binance_fallback(cid, timeout=timeout) for cid in ids]


def _fetch_binance_fallback(cid: str, timeout: float = 10.0) -> QuoteResult:
    pair = BINANCE_SYMBOLS.get(cid, f"{cid.upper()}USDT")
    url = f"https://api.binance.com/api/v3/ticker/24hr?symbol={urllib.parse.quote(pair)}"
    try:
        payload = http_get_json(url, timeout=timeout)
        price = float(payload["lastPrice"])
        chg = float(payload["priceChangePercent"])
        return QuoteResult(
            market="crypto",
            symbol=cid.upper(),
            name=pair,
            price=round(price, 6),
            change_pct=round(chg, 2),
            currency="USD",
            source="binance",
            timestamp_utc=utc_now_iso(),
        )
    except Exception as exc:
        return QuoteResult(
            market="crypto",
            symbol=cid.upper(),
            name=cid,
            price=None,
            change_pct=None,
            currency="USD",
            source="crypto-fallback",
            timestamp_utc=utc_now_iso(),
            error=str(exc),
        )


def fetch_equity_quote(symbol: str, market: str, timeout: float = 10.0) -> QuoteResult:
    try:
        res = fetch_yahoo_quote(symbol, market=market, timeout=timeout)
        if res.price is not None:
            return res
    except Exception as yahoo_exc:
        last_err = str(yahoo_exc)
    else:
        last_err = res.error or "Yahoo returned empty price"

    try:
        if symbol.endswith(".TW") or symbol.endswith(".TWO"):
            return fetch_twse_fallback(symbol, market=market, timeout=timeout)
        return fetch_stooq_fallback(symbol, market=market, timeout=timeout)
    except Exception as fallback_exc:
        return QuoteResult(
            market=market,
            symbol=symbol,
            name=symbol,
            price=None,
            change_pct=None,
            currency="TWD" if symbol.endswith((".TW", ".TWO")) else "USD",
            source="unavailable",
            timestamp_utc=utc_now_iso(),
            error=f"primary={last_err}; fallback={fallback_exc}",
        )


def generate_mock_quotes(
    tw: Sequence[str],
    us: Sequence[str],
    etf: Sequence[str],
    crypto: Sequence[str],
) -> List[QuoteResult]:
    """Deterministic offline quote fixtures for testing and demos."""
    now = utc_now_iso()
    mock_prices = {
        "2330.TW": ("TSMC (Mock)", 980.0, 1.24, "TWD"),
        "2454.TW": ("MediaTek (Mock)", 1280.0, -0.78, "TWD"),
        "NVDA": ("NVIDIA Corp (Mock)", 135.4, 2.15, "USD"),
        "AAPL": ("Apple Inc (Mock)", 228.1, 0.45, "USD"),
        "VOO": ("Vanguard S&P 500 ETF (Mock)", 538.6, 0.62, "USD"),
        "0050.TW": ("Yuanta Taiwan Top 50 (Mock)", 188.5, 0.91, "TWD"),
        "BITCOIN": ("bitcoin (Mock)", 89500.0, 3.12, "USD"),
        "ETHEREUM": ("ethereum (Mock)", 3120.5, -1.05, "USD"),
    }
    results: List[QuoteResult] = []
    for raw in tw:
        sym = normalize_tw_symbol(raw)
        name, price, chg, ccy = mock_prices.get(sym, (f"{sym} (Mock)", 100.0, 0.5, "TWD"))
        results.append(QuoteResult("tw", sym, name, price, chg, ccy, "mock", now))
    for raw in us:
        sym = raw.strip().upper()
        name, price, chg, ccy = mock_prices.get(sym, (f"{sym} (Mock)", 150.0, 1.1, "USD"))
        results.append(QuoteResult("us", sym, name, price, chg, ccy, "mock", now))
    for raw in etf:
        sym = normalize_etf_symbol(raw)
        default_ccy = "TWD" if sym.endswith((".TW", ".TWO")) else "USD"
        name, price, chg, ccy = mock_prices.get(sym, (f"{sym} (Mock)", 120.0, 0.35, default_ccy))
        results.append(QuoteResult("etf", sym, name, price, chg, ccy, "mock", now))
    for raw in crypto:
        cid = normalize_crypto_id(raw)
        sym = cid.upper()
        name, price, chg, ccy = mock_prices.get(sym, (f"{cid} (Mock)", 10.25, -2.4, "USD"))
        results.append(QuoteResult("crypto", sym, name, price, chg, ccy, "mock", now))
    return results


def format_price(price: Optional[float]) -> str:
    if price is None:
        return "N/A"
    if price >= 100:
        return f"{price:,.2f}"
    if price >= 1:
        return f"{price:,.4f}".rstrip("0").rstrip(".")
    return f"{price:.6f}".rstrip("0").rstrip(".")


def format_change(change_pct: Optional[float]) -> str:
    if change_pct is None:
        return "N/A"
    sign = "+" if change_pct > 0 else ""
    return f"{sign}{change_pct:.2f}%"


def render_table(quotes: Iterable[QuoteResult]) -> str:
    rows = list(quotes)
    headers = ["Market", "Symbol", "Price", "Change", "CCY", "Source", "Status"]
    data = [
        [
            q.market,
            q.symbol,
            format_price(q.price),
            format_change(q.change_pct),
            q.currency,
            q.source,
            "OK" if not q.error else f"ERR: {q.error[:40]}",
        ]
        for q in rows
    ]
    widths = [max(len(headers[i]), *(len(r[i]) for r in data)) if data else len(headers[i]) for i in range(len(headers))]
    sep = "-+-".join("-" * w for w in widths)
    lines = [
        " | ".join(headers[i].ljust(widths[i]) for i in range(len(headers))),
        sep,
    ]
    for r in data:
        lines.append(" | ".join(r[i].ljust(widths[i]) for i in range(len(headers))))
    return "\n".join(lines)


def render_markdown(quotes: Iterable[QuoteResult]) -> str:
    rows = list(quotes)
    ts = rows[0].timestamp_utc if rows else utc_now_iso()
    lines = [
        f"### 公開市場報價快照（UTC: `{ts}`）",
        "",
        "| Market | Symbol | Name | Price | 24h / 1d Change | Currency | Source |",
        "| :--- | :--- | :--- | ---: | ---: | :---: | :--- |",
    ]
    for q in rows:
        status = format_price(q.price) if not q.error else f"Error (`{q.error}`)"
        lines.append(
            f"| `{q.market}` | **{q.symbol}** | {q.name} | {status} | {format_change(q.change_pct)} | {q.currency} | `{q.source}` |"
        )
    return "\n".join(lines)


def render_json(quotes: Iterable[QuoteResult]) -> str:
    return json.dumps([asdict(q) for q in quotes], ensure_ascii=False, indent=2)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="irt-quote",
        description="Fetch public market quotes for Investment Red-Team across TW, US, ETF, and Crypto.",
    )
    parser.add_argument("--tw", action="append", help="Taiwan stock ticker(s), comma-separated (e.g. 2330,2454)")
    parser.add_argument("--us", action="append", help="US stock ticker(s), comma-separated (e.g. NVDA,MSFT)")
    parser.add_argument("--etf", action="append", help="ETF ticker(s), comma-separated (e.g. 0050.TW,VOO,QQQ)")
    parser.add_argument("--crypto", action="append", help="Crypto id/symbol(s), comma-separated (e.g. bitcoin,eth,sol)")
    parser.add_argument("--format", choices=("table", "markdown", "json"), default="table", help="Output format")
    parser.add_argument("--out", help="Optional path to write output")
    parser.add_argument("--timeout", type=float, default=10.0, help="HTTP request timeout in seconds")
    parser.add_argument("--mock", action="store_true", help="Return deterministic offline mock quotes")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    tw_list = parse_symbol_list(args.tw)
    us_list = parse_symbol_list(args.us)
    etf_list = parse_symbol_list(args.etf)
    crypto_list = parse_symbol_list(args.crypto)

    if not any((tw_list, us_list, etf_list, crypto_list)):
        parser.error("Provide at least one ticker via --tw, --us, --etf, or --crypto.")

    if args.mock:
        quotes = generate_mock_quotes(tw_list, us_list, etf_list, crypto_list)
    else:
        quotes = []
        for raw in tw_list:
            quotes.append(fetch_equity_quote(normalize_tw_symbol(raw), market="tw", timeout=args.timeout))
        for raw in us_list:
            quotes.append(fetch_equity_quote(raw.strip().upper(), market="us", timeout=args.timeout))
        for raw in etf_list:
            quotes.append(fetch_equity_quote(normalize_etf_symbol(raw), market="etf", timeout=args.timeout))
        if crypto_list:
            quotes.extend(fetch_crypto_quotes(crypto_list, timeout=args.timeout))

    if args.format == "json":
        output = render_json(quotes)
    elif args.format == "markdown":
        output = render_markdown(quotes)
    else:
        output = render_table(quotes)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(output + "\n")
    else:
        print(output)

    has_error = any(q.error for q in quotes)
    return 1 if has_error else 0


if __name__ == "__main__":
    sys.exit(main())
