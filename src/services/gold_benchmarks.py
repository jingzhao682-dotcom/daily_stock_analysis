# -*- coding: utf-8 -*-
"""Independent gold benchmark observations. Never equate ETF shares with gold spot.

XAU/USD: international gold spot (USD per troy ounce; timestamped spot API primary).
AU0: SHFE active/main gold futures series (CNY per gram, Sina via AKShare);
this is NOT the Shanghai Gold Exchange Au99.99 physical spot price.
All quotes show provider date and fail closed if unavailable.
"""
import math
from datetime import date
from typing import Any


def _valid_number(value: Any):
    try:
        result = float(value)
        return result if math.isfinite(result) and result > 0 else None
    except (TypeError, ValueError):
        return None


def _format_row(name, unit, source, dt, close):
    if close is None or not dt:
        return f"| {name} | 数据缺失 | {unit} | {source} | 不可核验，不替代为ETF价格 |"
    return f"| {name} | {close:.2f} | {unit} | {source} | {dt} |"


def build_gold_benchmark_section() -> str:
    """Fetch separate gold benchmarks; provider errors do not abort market recap."""
    rows = []
    # Timestamped XAU/USD spot reference first; never use ETF or futures.
    try:
        import json
        from datetime import datetime, timezone, timedelta
        from urllib.request import Request, urlopen
        request = Request("https://api.gold-api.com/price/XAU", headers={
            "Accept": "application/json",
            "User-Agent": "DSA-Investment-Research/1.0"
        })
        with urlopen(request, timeout=10) as response:
            payload = json.load(response)
        price = _valid_number(payload.get("price"))
        unit = str(payload.get("currency") or "").upper()
        asset = str(payload.get("symbol") or "").upper()
        stamp = str(payload.get("updatedAt") or "")
        timestamp = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        age = datetime.now(timezone.utc) - timestamp.astimezone(timezone.utc)
        if price is None or unit != "USD" or asset != "XAU" or not (
            -timedelta(minutes=10) <= age <= timedelta(hours=12)
        ):
            raise ValueError("invalid or stale XAU/USD spot quote")
        rows.append(_format_row("国际黄金现货 XAU/USD（参考报价）", "美元/金衡盎司",
                                "Gold-API 现货 XAU", timestamp.strftime("%Y-%m-%d %H:%M UTC"),
                                price))
    except Exception:
        try:
            import yfinance as yf
            hist = yf.Ticker("XAUUSD=X").history(period="7d", auto_adjust=False)
            if hist is None or hist.empty:
                raise ValueError("empty XAUUSD=X series")
            hist = hist.sort_index()
            latest = hist.iloc[-1]
            price = _valid_number(latest.get("Close"))
            quote_date = hist.index[-1].date().isoformat()
            rows.append(_format_row("国际黄金现货 XAU/USD", "美元/金衡盎司", "Yahoo Finance XAUUSD=X", quote_date, price))
        except Exception:
            # Yahoo may not publish XAUUSD=X. A separate spot-source fallback is
            # explicit and never silently substitutes GLD/GLDM or COMEX futures.
            try:
                import pandas as pd
                from datetime import timedelta
                from urllib.parse import urlencode
                end = date.today()
                start = end - timedelta(days=14)
                url = "https://stooq.com/q/d/l/?" + urlencode({
                    "s": "xauusd", "d1": start.strftime("%Y%m%d"),
                    "d2": end.strftime("%Y%m%d"), "i": "d"
                })
                frame = pd.read_csv(url)
                if frame.empty or not {"Date", "Close"}.issubset(frame.columns):
                    raise ValueError("Stooq spot series unavailable")
                frame["Date"] = pd.to_datetime(frame["Date"], errors="coerce")
                frame = frame.dropna(subset=["Date", "Close"]).sort_values("Date")
                if frame.empty:
                    raise ValueError("Stooq spot series invalid")
                last = frame.iloc[-1]
                rows.append(_format_row("国际黄金现货 XAU/USD", "美元/金衡盎司",
                                        "Stooq xauusd", last["Date"].date().isoformat(),
                                        _valid_number(last["Close"])))
            except Exception:
                rows.append(_format_row("国际黄金现货 XAU/USD", "美元/金衡盎司",
                                        "Yahoo Finance XAUUSD=X / Stooq xauusd", None, None))
    try:
        import akshare as ak
        import pandas as pd
        today = date.today()
        hist = ak.futures_main_sina(symbol="AU0", start_date=(today.replace(year=today.year - 1)).strftime("%Y%m%d"), end_date=today.strftime("%Y%m%d"))
        if hist is None or hist.empty:
            raise ValueError("empty AU0 series")
        dt_col = next((x for x in ("date", "日期", "trade_date") if x in hist.columns), None)
        price_col = next((x for x in ("close", "收盘价", "收盘") if x in hist.columns), None)
        if dt_col is None or price_col is None:
            raise ValueError("AU0 missing date or close columns")
        last = hist.sort_values(dt_col).iloc[-1]
        price = _valid_number(last[price_col])
        dt = pd.Timestamp(last[dt_col]).date().isoformat()
        rows.append(_format_row("上海期货交易所沪金主力 AU0", "人民币/克", "新浪财经 / AKShare AU0", dt, price))
    except Exception:
        rows.append(_format_row("上海期货交易所沪金主力 AU0", "人民币/克", "新浪财经 / AKShare AU0", None, None))
    return (
        "### 黄金基准（独立行情，不等同于 GLD/GLDM 基金）\n"
        "| 品种 | 最近可用报价 | 单位 | 来源 | 报价时点 / 交易日期 |\n"
        "|------|---------:|------|------|--------------------|\n"
        + "\n".join(rows)
        + "\n\n注：XAU/USD 为国际现货参考报价，可能是盘中时点；AU0 为上期所黄金主力连续期货收盘价，"
          "并非上海黄金交易所 Au99.99 现货。交易日、时区、币种和单位不同，不比较两者价格绝对值。"
    )
