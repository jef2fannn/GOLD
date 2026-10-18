#!/usr/bin/env python3
"""
==============================================================================
 GOLD SIGNAL BOT — Full Edition (XAU only, Telegram signals only)
==============================================================================
 Complete stack (designed so you rarely need to bolt features later):

  Indicators: EMA 9/21/50/100, RSI, MACD, Stochastic, ADX(+DI/-DI), ATR,
              Bollinger, CCI, MFI, Williams %R, Supertrend, Volume SMA
  Price action: pin bar, engulfing, inside bar, reject high/low, HH/HL LH/LL
  Fibonacci: swing high/low → 0.236 / 0.382 / 0.5 / 0.618 / 0.786 + zone hit
  Multi-timeframe: higher-TF trend filter (default 1h)
  Regime: TRENDING vs RANGING via ADX
  Live entry at send time + dead-signal rejection
  Session filter (London/NY), cooldown, daily cap
  Confidence 0–10 from weighted confluence
  Outcome tracking: TP1/TP2/TP3 / SL / TIMEOUT + Telegram alerts
  Hourly reports: market snapshot + honest WR (all / without TO) + expectancy
  State persistence (open/closed history)

 Env required:
   TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

 Optional:
   GOLD_SYMBOL, DATA_SOURCE=binance|yfinance, YFINANCE_SYMBOL=GC=F,
   KLINE_INTERVAL, HTF_INTERVAL, CHECK_INTERVAL_SEC, SESSION_FILTER,
   MIN_CONFIDENCE, COOLDOWN_SEC, MAX_SIGNALS_PER_DAY, MAX_HOLD_MINUTES,
   REPORT_INTERVAL_SEC, GOLD_STATE_PATH
==============================================================================
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("gold_full")

CONFIG: dict[str, Any] = {
    "SYMBOL": os.environ.get("GOLD_SYMBOL", "XAUUSDT").upper(),
    # Default yfinance — Binance often returns 451 on cloud hosts (Railway etc.)
    "DATA_SOURCE": os.environ.get("DATA_SOURCE", "yfinance").lower(),
    "YFINANCE_SYMBOL": os.environ.get("YFINANCE_SYMBOL", "GC=F"),
    "KLINE_INTERVAL": os.environ.get("KLINE_INTERVAL", "15m"),
    "HTF_INTERVAL": os.environ.get("HTF_INTERVAL", "1h"),
    "USE_HTF_FILTER": os.environ.get("USE_HTF_FILTER", "1") not in ("0", "false", "False"),
    "KLINE_LIMIT": 300,
    "CHECK_INTERVAL_SEC": int(os.environ.get("CHECK_INTERVAL_SEC", "60")),
    "EMA_FAST": 9,
    "EMA_MED": 21,
    "EMA_SLOW": 50,
    "EMA_TREND": 100,
    "SMA_VOLUME": 20,
    "RSI_PERIOD": 14,
    "MACD_FAST": 12,
    "MACD_SLOW": 26,
    "MACD_SIGNAL": 9,
    "STOCH_K": 14,
    "STOCH_D": 3,
    "ADX_PERIOD": 14,
    "ATR_PERIOD": 14,
    "BB_PERIOD": 20,
    "BB_STD": 2.0,
    "CCI_PERIOD": 20,
    "MFI_PERIOD": 14,
    "WILLIAMS_PERIOD": 14,
    "SUPERTREND_PERIOD": 10,
    "SUPERTREND_MULT": 3.0,
    # Risk/reward — tuned after TIMEOUT-heavy paper (TP1 was too far for 90m)
    "ATR_SL_MULT": float(os.environ.get("ATR_SL_MULT", "1.4")),
    "ATR_TP1_MULT": float(os.environ.get("ATR_TP1_MULT", "1.25")),
    "ATR_TP2_MULT": float(os.environ.get("ATR_TP2_MULT", "2.2")),
    "ATR_TP3_MULT": float(os.environ.get("ATR_TP3_MULT", "3.5")),
    "MIN_ATR_PCT": 0.04,
    "ADX_TREND_MIN": 18.0,
    # Quality filters (signal improvement)
    "ADX_SIGNAL_MIN": float(os.environ.get("ADX_SIGNAL_MIN", "20")),
    "REQUIRE_HTF": os.environ.get("REQUIRE_HTF", "1") not in ("0", "false", "False"),
    "BLOCK_RANGING": os.environ.get("BLOCK_RANGING", "1") not in ("0", "false", "False"),
    "RANGING_MIN_CONF": float(os.environ.get("RANGING_MIN_CONF", "7.0")),
    "MIN_CONFIDENCE": float(os.environ.get("MIN_CONFIDENCE", "5.5")),
    "SWING_LOOKBACK": 60,
    "SWING_STRENGTH": 3,
    "FIB_LEVELS": [0.236, 0.382, 0.5, 0.618, 0.786],
    "FIB_ZONE_PCT": 0.12,
    # yfinance = closed bars only (no real live tick) — disable live re-anchor by default
    "USE_LIVE_ENTRY": os.environ.get(
        "USE_LIVE_ENTRY",
        "0" if os.environ.get("DATA_SOURCE", "yfinance").lower() == "yfinance" else "1",
    ) not in ("0", "false", "False"),
    "REJECT_IF_PAST_TP1": True,
    "LATE_PATH_FRAC": 0.70,
    "SESSION_FILTER": os.environ.get("SESSION_FILTER", "1"),
    "SESSION_UTC": [(7, 16), (12, 21)],
    "COOLDOWN_SEC": int(os.environ.get("COOLDOWN_SEC", "2700")),
    "MAX_SIGNALS_PER_DAY": int(os.environ.get("MAX_SIGNALS_PER_DAY", "12")),
    "MAX_OPEN_SIGNALS": int(os.environ.get("MAX_OPEN_SIGNALS", "3")),
    "MAX_HOLD_MINUTES": int(os.environ.get("MAX_HOLD_MINUTES", "150")),
    "REPORT_INTERVAL_SEC": int(os.environ.get("REPORT_INTERVAL_SEC", "3600")),
    "OUTCOME_CHECK_EACH_CYCLE": True,
    "STATUS_UPDATE_OPEN": os.environ.get("STATUS_UPDATE_OPEN", "0") not in ("0", "false", "False"),
    # Paper costs for honest expectancy (not broker-exact)
    "FEE_PCT_PER_SIDE": float(os.environ.get("FEE_PCT_PER_SIDE", "0.04")),
    "SLIPPAGE_PCT_PER_SIDE": float(os.environ.get("SLIPPAGE_PCT_PER_SIDE", "0.05")),
    "MIN_RR_TP1": float(os.environ.get("MIN_RR_TP1", "0.80")),
    "MAX_SOURCE_AGE_SEC": int(os.environ.get("MAX_SOURCE_AGE_SEC", "1200")),
    # Light self-tune: if last N closed are mostly TIMEOUT, shrink TP1 slightly
    "AUTO_TUNE": os.environ.get("AUTO_TUNE", "1") not in ("0", "false", "False"),
    "AUTO_TUNE_MIN_CLOSED": 8,
    "TELEGRAM_BOT_TOKEN": os.environ.get("TELEGRAM_BOT_TOKEN", ""),
    "TELEGRAM_CHAT_ID": os.environ.get("TELEGRAM_CHAT_ID", ""),
    "STATE_PATH": os.environ.get("GOLD_STATE_PATH", "/data/gold_signal_state.json"),
}

BINANCE_FAPI = "https://fapi.binance.com"
BINANCE_SPOT = "https://api.binance.com"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _isnan(x: float) -> bool:
    return isinstance(x, float) and (x != x or math.isinf(x))


# ----- indicators -----
def ema_series(values: list[float], period: int) -> list[float]:
    n = len(values)
    out = [float("nan")] * n
    if n < period or period < 1:
        return out
    k = 2.0 / (period + 1)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, n):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def sma_series(values: list[float], period: int) -> list[float]:
    n = len(values)
    out = [float("nan")] * n
    if n < period:
        return out
    s = sum(values[:period])
    out[period - 1] = s / period
    for i in range(period, n):
        s += values[i] - values[i - period]
        out[i] = s / period
    return out


def rsi_series(closes: list[float], period: int = 14) -> list[float]:
    n = len(closes)
    out = [float("nan")] * n
    if n < period + 1:
        return out
    gains, losses = [], []
    for i in range(1, n):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    out[period] = 100.0 if avg_l == 0 else 100 - (100 / (1 + avg_g / avg_l))
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
        out[i + 1] = 100.0 if avg_l == 0 else 100 - (100 / (1 + avg_g / avg_l))
    return out


def macd_series(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9):
    ef, es = ema_series(closes, fast), ema_series(closes, slow)
    n = len(closes)
    line = [float("nan")] * n
    for i in range(n):
        if not _isnan(ef[i]) and not _isnan(es[i]):
            line[i] = ef[i] - es[i]
    valid = [0.0 if _isnan(x) else x for x in line]
    sig = ema_series(valid, signal)
    hist = [float("nan")] * n
    for i in range(n):
        if not _isnan(line[i]) and not _isnan(sig[i]):
            hist[i] = line[i] - sig[i]
    return line, sig, hist


def stochastic_series(highs, lows, closes, k_period=14, d_period=3):
    n = len(closes)
    k_out = [float("nan")] * n
    for i in range(k_period - 1, n):
        hh, ll = max(highs[i - k_period + 1 : i + 1]), min(lows[i - k_period + 1 : i + 1])
        k_out[i] = 50.0 if hh == ll else (closes[i] - ll) / (hh - ll) * 100
    return k_out, sma_series(k_out, d_period)


def true_range(highs, lows, closes) -> list[float]:
    n = len(closes)
    tr = [highs[0] - lows[0]] + [0.0] * (n - 1)
    for i in range(1, n):
        tr[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
    return tr


def atr_series(highs, lows, closes, period=14) -> list[float]:
    tr, n = true_range(highs, lows, closes), len(closes)
    out = [float("nan")] * n
    if n <= period:
        return out
    out[period] = sum(tr[1 : period + 1]) / period
    prev = out[period]
    for i in range(period + 1, n):
        prev = (prev * (period - 1) + tr[i]) / period
        out[i] = prev
    return out


def adx_series(highs, lows, closes, period=14):
    n = len(closes)
    plus_dm, minus_dm = [0.0] * n, [0.0] * n
    for i in range(1, n):
        up, dn = highs[i] - highs[i - 1], lows[i - 1] - lows[i]
        plus_dm[i] = up if up > dn and up > 0 else 0.0
        minus_dm[i] = dn if dn > up and dn > 0 else 0.0
    tr = true_range(highs, lows, closes)

    def wilder(arr, p):
        o = [float("nan")] * n
        if n <= p:
            return o
        o[p] = sum(arr[1 : p + 1])
        for i in range(p + 1, n):
            o[i] = o[i - 1] - (o[i - 1] / p) + arr[i]
        return o

    str_, sp, sm = wilder(tr, period), wilder(plus_dm, period), wilder(minus_dm, period)
    plus_di, minus_di, dx = [float("nan")] * n, [float("nan")] * n, [float("nan")] * n
    for i in range(n):
        if _isnan(str_[i]) or str_[i] == 0:
            continue
        plus_di[i] = 100 * sp[i] / str_[i]
        minus_di[i] = 100 * sm[i] / str_[i]
        s = plus_di[i] + minus_di[i]
        dx[i] = 0.0 if s == 0 else abs(plus_di[i] - minus_di[i]) / s * 100
    adx = [float("nan")] * n
    start = period * 2
    if n > start:
        vals = [dx[i] for i in range(period, start + 1) if not _isnan(dx[i])]
        if vals:
            adx[start] = sum(vals) / len(vals)
            for i in range(start + 1, n):
                if not _isnan(dx[i]) and not _isnan(adx[i - 1]):
                    adx[i] = (adx[i - 1] * (period - 1) + dx[i]) / period
    return adx, plus_di, minus_di


def bollinger(closes, period=20, std_mult=2.0):
    mid, n = sma_series(closes, period), len(closes)
    upper, lower = [float("nan")] * n, [float("nan")] * n
    for i in range(period - 1, n):
        window = closes[i - period + 1 : i + 1]
        m = mid[i]
        sd = math.sqrt(sum((x - m) ** 2 for x in window) / period)
        upper[i], lower[i] = m + std_mult * sd, m - std_mult * sd
    return upper, mid, lower


def cci_series(highs, lows, closes, period=20):
    n = len(closes)
    tp = [(highs[i] + lows[i] + closes[i]) / 3 for i in range(n)]
    out = [float("nan")] * n
    for i in range(period - 1, n):
        window = tp[i - period + 1 : i + 1]
        m = sum(window) / period
        md = sum(abs(x - m) for x in window) / period
        out[i] = 0.0 if md == 0 else (tp[i] - m) / (0.015 * md)
    return out


def mfi_series(highs, lows, closes, volumes, period=14):
    n = len(closes)
    out = [float("nan")] * n
    tp = [(highs[i] + lows[i] + closes[i]) / 3 for i in range(n)]
    raw = [0.0] * n
    for i in range(1, n):
        if tp[i] > tp[i - 1]:
            raw[i] = tp[i] * volumes[i]
        elif tp[i] < tp[i - 1]:
            raw[i] = -tp[i] * volumes[i]
    for i in range(period, n):
        pos = sum(x for x in raw[i - period + 1 : i + 1] if x > 0)
        neg = -sum(x for x in raw[i - period + 1 : i + 1] if x < 0)
        out[i] = 100.0 if neg == 0 else 100 - (100 / (1 + pos / neg))
    return out


def williams_r(highs, lows, closes, period=14):
    n = len(closes)
    out = [float("nan")] * n
    for i in range(period - 1, n):
        hh, ll = max(highs[i - period + 1 : i + 1]), min(lows[i - period + 1 : i + 1])
        out[i] = -50.0 if hh == ll else (hh - closes[i]) / (hh - ll) * -100
    return out


def supertrend_series(highs, lows, closes, period=10, mult=3.0):
    atr, n = atr_series(highs, lows, closes, period), len(closes)
    st, direction = [float("nan")] * n, [0] * n
    for i in range(n):
        if _isnan(atr[i]):
            continue
        mid = (highs[i] + lows[i]) / 2
        upper, lower = mid + mult * atr[i], mid - mult * atr[i]
        if i == 0 or _isnan(st[i - 1]):
            st[i], direction[i] = lower, 1
            continue
        prev_dir, prev_st = direction[i - 1], st[i - 1]
        if prev_dir == 1:
            st[i] = max(lower, prev_st) if closes[i] >= prev_st else upper
            direction[i] = 1 if closes[i] >= st[i] else -1
            if direction[i] == -1:
                st[i] = upper
        else:
            st[i] = min(upper, prev_st) if closes[i] <= prev_st else lower
            direction[i] = -1 if closes[i] <= st[i] else 1
            if direction[i] == 1:
                st[i] = lower
    return st, direction


def detect_price_action(o, h, l, c, i: int) -> dict[str, Any]:
    if i < 1:
        return {"tags": [], "bull": 0.0, "bear": 0.0}
    body, rng = abs(c[i] - o[i]), max(h[i] - l[i], 1e-12)
    upper_wick, lower_wick = h[i] - max(o[i], c[i]), min(o[i], c[i]) - l[i]
    tags, bull, bear = [], 0.0, 0.0
    if lower_wick > body * 2 and lower_wick > upper_wick * 1.5 and body / rng < 0.35:
        tags.append("pin_bull")
        bull += 1.2
    if upper_wick > body * 2 and upper_wick > lower_wick * 1.5 and body / rng < 0.35:
        tags.append("pin_bear")
        bear += 1.2
    prev_body = abs(c[i - 1] - o[i - 1])
    if c[i] > o[i] and c[i - 1] < o[i - 1] and c[i] >= o[i - 1] and o[i] <= c[i - 1] and body > prev_body * 0.9:
        tags.append("engulf_bull")
        bull += 1.5
    if c[i] < o[i] and c[i - 1] > o[i - 1] and c[i] <= o[i - 1] and o[i] >= c[i - 1] and body > prev_body * 0.9:
        tags.append("engulf_bear")
        bear += 1.5
    if h[i] < h[i - 1] and l[i] > l[i - 1]:
        tags.append("inside_bar")
        if c[i] > o[i]:
            bull += 0.3
        else:
            bear += 0.3
    if i >= 10:
        hh10, ll10 = max(h[i - 10 : i]), min(l[i - 10 : i])
        if h[i] >= hh10 * 0.999 and c[i] < o[i] and upper_wick > body:
            tags.append("reject_high")
            bear += 0.8
        if l[i] <= ll10 * 1.001 and c[i] > o[i] and lower_wick > body:
            tags.append("reject_low")
            bull += 0.8
    if i >= 5:
        if h[i] > h[i - 2] and l[i] > l[i - 2]:
            tags.append("hh_hl")
            bull += 0.5
        if h[i] < h[i - 2] and l[i] < l[i - 2]:
            tags.append("lh_ll")
            bear += 0.5
    return {"tags": tags, "bull": bull, "bear": bear}


def find_swings(highs, lows, strength: int = 3):
    sh, sl, n = [], [], len(highs)
    for i in range(strength, n - strength):
        if highs[i] == max(highs[i - strength : i + strength + 1]):
            sh.append(i)
        if lows[i] == min(lows[i - strength : i + strength + 1]):
            sl.append(i)
    return sh, sl


def fibonacci_confluence(highs, lows, closes, lookback, strength, levels, zone_pct):
    n, start = len(closes), max(0, len(closes) - lookback)
    sh, sl = find_swings(highs[start:], lows[start:], strength)
    sh, sl = [i + start for i in sh], [i + start for i in sl]
    if not sh or not sl:
        return {"levels": {}, "near": [], "trend": "none", "swing_high": None, "swing_low": None}
    last_sh, last_sl, price = sh[-1], sl[-1], closes[-1]
    hi, lo = highs[last_sh], lows[last_sl]
    trend = "up" if last_sh > last_sl else "down"
    diff = hi - lo
    if diff <= 0:
        return {"levels": {}, "near": [], "trend": trend, "swing_high": hi, "swing_low": lo}
    fibs, near = {}, []
    for lv in levels:
        val = hi - diff * lv if trend == "up" else lo + diff * lv
        fibs[lv] = val
        if abs(price - val) / price * 100 <= zone_pct:
            near.append(lv)
    return {"levels": fibs, "near": near, "trend": trend, "swing_high": hi, "swing_low": lo}


@dataclass
class Candle:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: int


def interval_to_seconds(interval: str) -> int:
    try:
        n = int(interval[:-1])
    except ValueError:
        return 900
    return n * {"m": 60, "h": 3600, "d": 86400}.get(interval[-1], 60)


def fetch_klines_binance(symbol: str, interval: str, limit: int) -> list[Candle]:
    last_err = None
    for base in (f"{BINANCE_FAPI}/fapi/v1/klines", f"{BINANCE_SPOT}/api/v3/klines"):
        try:
            r = requests.get(base, params={"symbol": symbol, "interval": interval, "limit": limit + 1}, timeout=25)
            r.raise_for_status()
            data, now_ms, out = r.json(), int(time.time() * 1000), []
            for row in data:
                ct = int(row[6])
                if ct > now_ms:
                    continue
                out.append(Candle(int(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5]), ct))
            if len(out) > limit:
                out = out[-limit:]
            if len(out) < 80:
                raise RuntimeError(f"few candles {len(out)}")
            return out
        except Exception as e:
            last_err = e
            log.warning("binance %s: %s", base, e)
    raise RuntimeError(f"binance klines failed: {last_err}")


def fetch_klines_yfinance(interval: str, limit: int) -> list[Candle]:
    try:
        import yfinance as yf  # type: ignore
    except ImportError as e:
        raise RuntimeError("pip install yfinance") from e
    ysym = CONFIG["YFINANCE_SYMBOL"]
    imap = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "60m", "4h": "1h", "1d": "1d"}
    yi = imap.get(interval, "15m")
    period = "7d" if yi in ("1m", "5m", "15m") else "60d"
    df = yf.download(ysym, interval=yi, period=period, progress=False, threads=False)
    if df is None or df.empty:
        raise RuntimeError("yfinance empty")
    if hasattr(df.columns, "levels"):
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    out, sec, now_ms = [], interval_to_seconds(interval), int(time.time() * 1000)
    for ts, row in df.iterrows():
        t = ts.to_pydatetime()
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        ot = int(t.timestamp() * 1000)
        ct = ot + sec * 1000 - 1
        if ct > now_ms:
            continue
        out.append(Candle(ot, float(row["Open"]), float(row["High"]), float(row["Low"]), float(row["Close"]), float(row.get("Volume", 0) or 0), ct))
    if len(out) > limit:
        out = out[-limit:]
    if len(out) < 80:
        raise RuntimeError(f"yfinance few {len(out)}")
    return out


def fetch_klines(interval: Optional[str] = None) -> list[Candle]:
    interval = interval or CONFIG["KLINE_INTERVAL"]
    limit = int(CONFIG["KLINE_LIMIT"])
    src = CONFIG["DATA_SOURCE"]
    if src == "yfinance":
        return fetch_klines_yfinance(interval, limit)
    if src == "binance":
        try:
            return fetch_klines_binance(CONFIG["SYMBOL"], interval, limit)
        except Exception as e:
            log.warning("binance failed (%s) → fallback yfinance", e)
            return fetch_klines_yfinance(interval, limit)
    # auto: try binance then yfinance
    try:
        return fetch_klines_binance(CONFIG["SYMBOL"], interval, limit)
    except Exception as e:
        log.warning("binance failed (%s) → yfinance", e)
        return fetch_klines_yfinance(interval, limit)


def fetch_live_price() -> float:
    src = CONFIG["DATA_SOURCE"]
    if src == "yfinance":
        return fetch_klines()[-1].close
    if src in ("binance", "auto"):
        sym = CONFIG["SYMBOL"]
        for url in (f"{BINANCE_FAPI}/fapi/v1/ticker/price", f"{BINANCE_SPOT}/api/v3/ticker/price"):
            try:
                r = requests.get(url, params={"symbol": sym}, timeout=10)
                r.raise_for_status()
                return float(r.json()["price"])
            except Exception:
                pass
        log.warning("binance ticker failed → yfinance last close")
        return fetch_klines_yfinance(CONFIG["KLINE_INTERVAL"], 50)[-1].close
    return fetch_klines()[-1].close


def in_session() -> bool:
    if str(CONFIG.get("SESSION_FILTER", "1")) in ("0", "false", "False", "off"):
        return True
    h = utc_now().hour
    for a, b in CONFIG["SESSION_UTC"]:
        if a <= b and a <= h < b:
            return True
        if a > b and (h >= a or h < b):
            return True
    return False


@dataclass
class Signal:
    symbol: str
    direction: str
    entry: float
    sl: float
    tp1: float
    tp2: float
    tp3: float
    atr: float
    confidence: float
    regime: str
    reasons: list
    indicators: dict
    price_action: list
    fib_near: list
    candle_close: float
    htf_ok: bool
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def htf_trend(candles: list[Candle]) -> str:
    c = [x.close for x in candles]
    if len(c) < 60:
        return "NEUTRAL"
    e21, e50 = ema_series(c, 21), ema_series(c, 50)
    i = len(c) - 1
    if _isnan(e21[i]) or _isnan(e50[i]):
        return "NEUTRAL"
    if e21[i] > e50[i] and c[i] > e21[i]:
        return "BULL"
    if e21[i] < e50[i] and c[i] < e21[i]:
        return "BEAR"
    return "NEUTRAL"


def analyze(candles: list[Candle], htf: Optional[list[Candle]] = None) -> Optional[Signal]:
    o = [x.open for x in candles]
    h = [x.high for x in candles]
    l = [x.low for x in candles]
    c = [x.close for x in candles]
    v = [x.volume for x in candles]
    i = len(c) - 1

    e9 = ema_series(c, CONFIG["EMA_FAST"])
    e21 = ema_series(c, CONFIG["EMA_MED"])
    e50 = ema_series(c, CONFIG["EMA_SLOW"])
    e100 = ema_series(c, CONFIG["EMA_TREND"])
    rsi = rsi_series(c, CONFIG["RSI_PERIOD"])
    macd_l, macd_s, macd_h = macd_series(c, CONFIG["MACD_FAST"], CONFIG["MACD_SLOW"], CONFIG["MACD_SIGNAL"])
    stoch_k, stoch_d = stochastic_series(h, l, c, CONFIG["STOCH_K"], CONFIG["STOCH_D"])
    adx, pdi, mdi = adx_series(h, l, c, CONFIG["ADX_PERIOD"])
    atr = atr_series(h, l, c, CONFIG["ATR_PERIOD"])
    bb_u, bb_m, bb_l = bollinger(c, CONFIG["BB_PERIOD"], CONFIG["BB_STD"])
    cci = cci_series(h, l, c, CONFIG["CCI_PERIOD"])
    mfi = mfi_series(h, l, c, v, CONFIG["MFI_PERIOD"])
    will = williams_r(h, l, c, CONFIG["WILLIAMS_PERIOD"])
    st_line, st_dir = supertrend_series(h, l, c, CONFIG["SUPERTREND_PERIOD"], CONFIG["SUPERTREND_MULT"])
    vol_sma = sma_series(v, CONFIG["SMA_VOLUME"])

    for x in (e9[i], e21[i], e50[i], e100[i], rsi[i], macd_l[i], atr[i], adx[i]):
        if _isnan(x):
            log.info("indicators warming up")
            return None

    price, atr_v = c[i], atr[i]
    atr_pct = atr_v / price * 100
    if atr_pct < CONFIG["MIN_ATR_PCT"]:
        log.info("ATR flat %.3f%%", atr_pct)
        return None

    adx_v = adx[i]
    regime = "TRENDING" if adx_v >= CONFIG["ADX_TREND_MIN"] else "RANGING"
    pa = detect_price_action(o, h, l, c, i)
    fib = fibonacci_confluence(h, l, c, CONFIG["SWING_LOOKBACK"], CONFIG["SWING_STRENGTH"], list(CONFIG["FIB_LEVELS"]), float(CONFIG["FIB_ZONE_PCT"]))

    long_score = short_score = 0.0
    reasons_l: list[str] = []
    reasons_s: list[str] = []

    if e9[i] > e21[i] > e50[i]:
        long_score += 1.2
        reasons_l.append("EMA stack up")
    if e9[i] < e21[i] < e50[i]:
        short_score += 1.2
        reasons_s.append("EMA stack down")
    if price > e100[i]:
        long_score += 0.5
        reasons_l.append("above EMA100")
    if price < e100[i]:
        short_score += 0.5
        reasons_s.append("below EMA100")
    if e9[i - 1] <= e21[i - 1] and e9[i] > e21[i]:
        long_score += 0.8
        reasons_l.append("EMA9/21 cross up")
    if e9[i - 1] >= e21[i - 1] and e9[i] < e21[i]:
        short_score += 0.8
        reasons_s.append("EMA9/21 cross down")

    if 40 <= rsi[i] <= 65:
        long_score += 0.5
        reasons_l.append(f"RSI {rsi[i]:.0f}")
    if rsi[i] < 35:
        long_score += 0.3
        reasons_l.append("RSI OS zone")
    if 35 <= rsi[i] <= 60:
        short_score += 0.5
        reasons_s.append(f"RSI {rsi[i]:.0f}")
    if rsi[i] > 65:
        short_score += 0.3
        reasons_s.append("RSI OB zone")

    if not _isnan(macd_s[i]) and macd_l[i] > macd_s[i] and macd_h[i] > 0:
        long_score += 0.7
        reasons_l.append("MACD bull")
    if not _isnan(macd_s[i]) and macd_l[i] < macd_s[i] and macd_h[i] < 0:
        short_score += 0.7
        reasons_s.append("MACD bear")
    if i >= 1 and not _isnan(macd_h[i - 1]):
        if macd_h[i - 1] <= 0 < macd_h[i]:
            long_score += 0.4
            reasons_l.append("MACD hist+")
        if macd_h[i - 1] >= 0 > macd_h[i]:
            short_score += 0.4
            reasons_s.append("MACD hist-")

    if not _isnan(stoch_k[i]) and not _isnan(stoch_d[i]):
        if stoch_k[i] > stoch_d[i] and stoch_k[i] < 80:
            long_score += 0.4
            reasons_l.append("Stoch up")
        if stoch_k[i] < stoch_d[i] and stoch_k[i] > 20:
            short_score += 0.4
            reasons_s.append("Stoch down")

    if regime == "TRENDING":
        if not _isnan(pdi[i]) and not _isnan(mdi[i]):
            if pdi[i] > mdi[i]:
                long_score += 0.6
                reasons_l.append(f"ADX+ ({adx_v:.0f})")
            if mdi[i] > pdi[i]:
                short_score += 0.6
                reasons_s.append(f"ADX- ({adx_v:.0f})")
    else:
        if not _isnan(bb_l[i]) and price <= bb_l[i]:
            long_score += 0.5
            reasons_l.append("BB lower")
        if not _isnan(bb_u[i]) and price >= bb_u[i]:
            short_score += 0.5
            reasons_s.append("BB upper")

    if cci[i] > 0:
        long_score += 0.25
    if cci[i] < 0:
        short_score += 0.25
    if not _isnan(mfi[i]):
        if mfi[i] > 50:
            long_score += 0.25
        else:
            short_score += 0.25
    if not _isnan(will[i]):
        if will[i] > -50:
            long_score += 0.2
        else:
            short_score += 0.2

    if st_dir[i] == 1:
        long_score += 0.8
        reasons_l.append("Supertrend bull")
    elif st_dir[i] == -1:
        short_score += 0.8
        reasons_s.append("Supertrend bear")

    if not _isnan(vol_sma[i]) and vol_sma[i] > 0 and v[i] > vol_sma[i] * 1.15:
        long_score += 0.3
        short_score += 0.15
        reasons_l.append("vol spike")
        reasons_s.append("vol spike")

    long_score += pa["bull"]
    short_score += pa["bear"]
    for t in pa["tags"]:
        if "bull" in t or t in ("reject_low", "hh_hl"):
            reasons_l.append(f"PA:{t}")
        if "bear" in t or t in ("reject_high", "lh_ll"):
            reasons_s.append(f"PA:{t}")

    if fib["near"]:
        if fib["trend"] == "up":
            long_score += 0.7
            reasons_l.append(f"Fib {fib['near']}")
        elif fib["trend"] == "down":
            short_score += 0.7
            reasons_s.append(f"Fib {fib['near']}")

    htf_label = "N/A"
    if CONFIG["USE_HTF_FILTER"] and htf:
        htf_label = htf_trend(htf)
        if htf_label == "BULL":
            long_score += 0.6
            reasons_l.append("HTF bull")
            short_score -= 0.8
        elif htf_label == "BEAR":
            short_score += 0.6
            reasons_s.append("HTF bear")
            long_score -= 0.8

    direction, score, reasons = None, 0.0, []
    if long_score >= short_score and long_score >= CONFIG["MIN_CONFIDENCE"]:
        direction, score, reasons = "LONG", min(10.0, long_score), reasons_l
    elif short_score > long_score and short_score >= CONFIG["MIN_CONFIDENCE"]:
        direction, score, reasons = "SHORT", min(10.0, short_score), reasons_s
    else:
        log.info("no confluence L=%.1f S=%.1f min=%.1f", long_score, short_score, CONFIG["MIN_CONFIDENCE"])
        return None

    # --- Quality gates (stricter signal filter) ---
    adx_min = float(CONFIG.get("ADX_SIGNAL_MIN", 20))
    if adx_v < adx_min:
        log.info("reject ADX weak: %.1f < %.1f", adx_v, adx_min)
        return None

    if CONFIG.get("BLOCK_RANGING", True) and regime == "RANGING":
        ranging_need = float(CONFIG.get("RANGING_MIN_CONF", 7.0))
        if score < ranging_need:
            log.info("reject RANGING: conf %.1f < %.1f", score, ranging_need)
            return None

    if CONFIG.get("REQUIRE_HTF", True) and CONFIG.get("USE_HTF_FILTER", True):
        if not htf:
            log.info("reject: HTF data missing")
            return None
        if direction == "LONG" and htf_label != "BULL":
            log.info("reject HTF: LONG needs BULL, got %s", htf_label)
            return None
        if direction == "SHORT" and htf_label != "BEAR":
            log.info("reject HTF: SHORT needs BEAR, got %s", htf_label)
            return None

    sl_m, t1, t2, t3 = CONFIG["ATR_SL_MULT"], CONFIG["ATR_TP1_MULT"], CONFIG["ATR_TP2_MULT"], CONFIG["ATR_TP3_MULT"]
    if direction == "LONG":
        entry, sl = price, price - sl_m * atr_v
        tp1, tp2, tp3 = price + t1 * atr_v, price + t2 * atr_v, price + t3 * atr_v
    else:
        entry, sl = price, price + sl_m * atr_v
        tp1, tp2, tp3 = price - t1 * atr_v, price - t2 * atr_v, price - t3 * atr_v

    ind_snap = {
        "rsi": round(rsi[i], 2),
        "macd": round(macd_l[i], 5),
        "macd_hist": round(macd_h[i], 5) if not _isnan(macd_h[i]) else None,
        "stoch_k": round(stoch_k[i], 2) if not _isnan(stoch_k[i]) else None,
        "adx": round(adx_v, 2),
        "plus_di": round(pdi[i], 2) if not _isnan(pdi[i]) else None,
        "minus_di": round(mdi[i], 2) if not _isnan(mdi[i]) else None,
        "cci": round(cci[i], 2),
        "mfi": round(mfi[i], 2) if not _isnan(mfi[i]) else None,
        "williams": round(will[i], 2) if not _isnan(will[i]) else None,
        "supertrend_dir": int(st_dir[i]),
        "ema9": round(e9[i], 3),
        "ema21": round(e21[i], 3),
        "ema50": round(e50[i], 3),
        "ema100": round(e100[i], 3),
        "bb_upper": round(bb_u[i], 3) if not _isnan(bb_u[i]) else None,
        "bb_mid": round(bb_m[i], 3) if not _isnan(bb_m[i]) else None,
        "bb_lower": round(bb_l[i], 3) if not _isnan(bb_l[i]) else None,
        "atr": round(atr_v, 4),
        "atr_pct": round(atr_pct, 4),
        "htf": htf_label,
        "fib_trend": fib["trend"],
        "fib_near": fib["near"],
    }

    return Signal(
        symbol=CONFIG["SYMBOL"],
        direction=direction,
        entry=round(entry, 3),
        sl=round(sl, 3),
        tp1=round(tp1, 3),
        tp2=round(tp2, 3),
        tp3=round(tp3, 3),
        atr=round(atr_v, 4),
        confidence=round(score, 2),
        regime=regime,
        reasons=reasons[:12],
        indicators=ind_snap,
        price_action=pa["tags"],
        fib_near=list(fib["near"]),
        candle_close=round(price, 3),
        htf_ok=True,
    )


def reanchor_live(sig: Signal, live: float) -> Optional[Signal]:
    if not CONFIG.get("USE_LIVE_ENTRY", True) or live <= 0:
        return sig
    candle = sig.candle_close
    if CONFIG.get("REJECT_IF_PAST_TP1", True):
        if sig.direction == "LONG" and live >= sig.tp1:
            log.info("dead LONG")
            return None
        if sig.direction == "SHORT" and live <= sig.tp1:
            log.info("dead SHORT")
            return None
    frac = float(CONFIG["LATE_PATH_FRAC"])
    if sig.direction == "LONG":
        path = sig.tp1 - candle
        if path > 0 and (live - candle) > frac * path:
            log.info("late LONG")
            return None
    else:
        path = candle - sig.tp1
        if path > 0 and (candle - live) > frac * path:
            log.info("late SHORT")
            return None
    atr_v = sig.atr
    sl_m, t1, t2, t3 = CONFIG["ATR_SL_MULT"], CONFIG["ATR_TP1_MULT"], CONFIG["ATR_TP2_MULT"], CONFIG["ATR_TP3_MULT"]
    if sig.direction == "LONG":
        sig.entry, sig.sl = round(live, 3), round(live - sl_m * atr_v, 3)
        sig.tp1, sig.tp2, sig.tp3 = round(live + t1 * atr_v, 3), round(live + t2 * atr_v, 3), round(live + t3 * atr_v, 3)
    else:
        sig.entry, sig.sl = round(live, 3), round(live + sl_m * atr_v, 3)
        sig.tp1, sig.tp2, sig.tp3 = round(live - t1 * atr_v, 3), round(live - t2 * atr_v, 3), round(live - t3 * atr_v, 3)
    drift = abs(live - candle) / candle * 100 if candle else 0
    sig.reasons.append(f"live entry drift {drift:.2f}%")
    return sig


def send_telegram(text: str) -> bool:
    token, chat = CONFIG["TELEGRAM_BOT_TOKEN"], CONFIG["TELEGRAM_CHAT_ID"]
    if not token or not chat:
        log.error("Telegram env missing")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=20,
        )
        if r.status_code != 200:
            log.error("TG %s %s", r.status_code, r.text[:180])
            return False
        return True
    except Exception as e:
        log.error("TG %s", e)
        return False


def format_msg(sig: Signal) -> str:
    arrow = "🟢 LONG" if sig.direction == "LONG" else "🔴 SHORT"
    risk = abs(sig.entry - sig.sl)
    reward = abs(sig.tp1 - sig.entry)
    rr = reward / risk if risk else 0
    ind = sig.indicators
    reasons = ", ".join(sig.reasons[:8]) if sig.reasons else "-"
    pa = ", ".join(sig.price_action) if sig.price_action else "-"
    fib = ", ".join(str(x) for x in sig.fib_near) if sig.fib_near else "-"
    # Distances for MT5 / any broker — use YOUR chart price as entry
    d_sl = abs(sig.entry - sig.sl)
    d_tp1 = abs(sig.tp1 - sig.entry)
    d_tp2 = abs(sig.tp2 - sig.entry)
    d_tp3 = abs(sig.tp3 - sig.entry)
    return (
        f"<b>GOLD SIGNAL — {sig.symbol}</b>\n"
        f"{arrow} | {sig.regime} | Conf {sig.confidence}/10\n\n"
        f"🎯 Ref Entry: <code>{sig.entry}</code>\n"
        f"🛑 SL: <code>{sig.sl}</code>  (−{d_sl:.2f})\n"
        f"✅ TP1: <code>{sig.tp1}</code>  (+{d_tp1:.2f})\n"
        f"✅ TP2: <code>{sig.tp2}</code>  (+{d_tp2:.2f})\n"
        f"✅ TP3: <code>{sig.tp3}</code>  (+{d_tp3:.2f})\n\n"
        f"<b>MT5 tip:</b> use your broker price as Entry,\n"
        f"then SL −{d_sl:.1f} / TP1 +{d_tp1:.1f} (same direction)\n\n"
        f"📐 ATR {sig.atr} | RR(TP1) {rr:.2f} | hold≤{CONFIG['MAX_HOLD_MINUTES']}m\n"
        f"📊 RSI {ind.get('rsi')} | ADX {ind.get('adx')} | Stoch {ind.get('stoch_k')}\n"
        f"📈 MACD hist {ind.get('macd_hist')} | ST {ind.get('supertrend_dir')} | HTF {ind.get('htf')}\n"
        f"🕯️ PA: {pa}\n"
        f"🔢 Fib near: {fib}\n"
        f"📝 {reasons}\n"
        f"⏰ {datetime.fromtimestamp(sig.ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"⚠️ Signal only — not financial advice"
    )


def maybe_auto_tune(state: dict[str, Any]) -> None:
    """If recent outcomes are TIMEOUT-heavy, slightly reduce TP1 multiplier (runtime only)."""
    if not CONFIG.get("AUTO_TUNE", True):
        return
    closed = list(state.get("closed_signals") or [])
    nmin = int(CONFIG.get("AUTO_TUNE_MIN_CLOSED", 8))
    if len(closed) < nmin:
        return
    recent = closed[-nmin:]
    to_n = sum(1 for x in recent if x.get("status") == "TIMEOUT")
    to_rate = to_n / len(recent)
    if to_rate >= 0.60:
        old = float(CONFIG["ATR_TP1_MULT"])
        new = max(1.0, round(old * 0.92, 3))
        if new < old:
            CONFIG["ATR_TP1_MULT"] = new
            log.info("AUTO_TUNE: TIMEOUT rate %.0f%% → ATR_TP1_MULT %.3f → %.3f", to_rate * 100, old, new)
    elif to_rate <= 0.25:
        # mild expand if targets hit often
        old = float(CONFIG["ATR_TP1_MULT"])
        base = float(os.environ.get("ATR_TP1_MULT", "1.25"))
        new = min(base * 1.15, round(old * 1.03, 3))
        if new > old:
            CONFIG["ATR_TP1_MULT"] = new
            log.info("AUTO_TUNE: low TIMEOUT %.0f%% → ATR_TP1_MULT %.3f → %.3f", to_rate * 100, old, new)


def load_state() -> dict[str, Any]:
    p = CONFIG["STATE_PATH"]
    try:
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            data.setdefault("open_signals", [])
            data.setdefault("closed_signals", [])
            data.setdefault("history", [])
            data.setdefault("last_report_ts", 0)
            data.setdefault("stats", {"sent": 0, "tp": 0, "sl": 0, "timeout": 0})
            return data
    except Exception as e:
        log.warning("state load %s", e)
    return {
        "last_signal_ts": 0,
        "last_direction": "",
        "day": "",
        "day_count": 0,
        "history": [],
        "open_signals": [],
        "closed_signals": [],
        "last_report_ts": 0,
        "stats": {"sent": 0, "tp": 0, "sl": 0, "timeout": 0},
        "uptime_start": time.time(),
    }


def save_state(state: dict[str, Any]) -> None:
    p = CONFIG["STATE_PATH"]
    try:
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    except Exception as e:
        log.warning("state save %s", e)


def can_send(state: dict[str, Any], direction: str) -> tuple[bool, str]:
    day = utc_now().strftime("%Y-%m-%d")
    if state.get("day") != day:
        state["day"], state["day_count"] = day, 0
    if int(state.get("day_count") or 0) >= int(CONFIG["MAX_SIGNALS_PER_DAY"]):
        return False, "daily limit"
    opens = [s for s in (state.get("open_signals") or []) if s.get("status") == "OPEN"]
    if len(opens) >= int(CONFIG.get("MAX_OPEN_SIGNALS", 3)):
        return False, "max open signals"
    last_ts = float(state.get("last_signal_ts") or 0)
    if state.get("last_direction") == direction and (time.time() - last_ts) < float(CONFIG["COOLDOWN_SEC"]):
        return False, "cooldown"
    for s in opens:
        if s.get("direction") == direction:
            return False, "open same direction"
    return True, "ok"


def _pct(entry: float, price: float, direction: str) -> float:
    if not entry:
        return 0.0
    if direction == "LONG":
        return (price - entry) / entry * 100
    return (entry - price) / entry * 100


def _round_trip_cost_pct() -> float:
    return 2.0 * (float(CONFIG.get("FEE_PCT_PER_SIDE", 0.04)) + float(CONFIG.get("SLIPPAGE_PCT_PER_SIDE", 0.05)))


def _close_signal(state: dict[str, Any], s: dict[str, Any], outcome: str, exit_px: float, age: float) -> None:
    entry = float(s.get("entry") or 0)
    direction = s.get("direction") or "LONG"
    gross = round(_pct(entry, exit_px, direction), 3)
    net = round(gross - _round_trip_cost_pct(), 3)
    s["status"] = outcome
    s["exit"] = exit_px
    s["return_pct"] = gross
    s["net_return_pct"] = net
    s["closed_ts"] = time.time()
    closed = state.setdefault("closed_signals", [])
    closed.append(s)
    state["closed_signals"] = closed[-500:]
    stats = state.setdefault("stats", {"sent": 0, "tp": 0, "sl": 0, "timeout": 0})
    if outcome.startswith("TP"):
        stats["tp"] = int(stats.get("tp") or 0) + 1
        emoji = "✅"
    elif outcome == "SL":
        stats["sl"] = int(stats.get("sl") or 0) + 1
        emoji = "❌"
    else:
        stats["timeout"] = int(stats.get("timeout") or 0) + 1
        emoji = "⏱️"
    msg = (
        f"{emoji} <b>GOLD OUTCOME — {s.get('symbol')}</b>\n"
        f"{s.get('direction')} → <b>{outcome}</b>\n"
        f"Entry <code>{entry}</code> → Exit <code>{exit_px}</code>\n"
        f"Gross: <b>{gross:+.2f}%</b> | Net≈ <b>{net:+.2f}%</b>\n"
        f"Hold: {age/60:.0f} min"
    )
    send_telegram(msg)
    log.info("OUTCOME %s %s gross=%+.2f%% net=%+.2f%%", s.get("direction"), outcome, gross, net)


def resolve_open_signals(state: dict[str, Any], live: float) -> dict[str, Any]:
    """Check open signals vs live price → TP/SL/TIMEOUT (for true tick sources)."""
    if live <= 0:
        return state
    open_list = list(state.get("open_signals") or [])
    still_open = []
    max_hold = float(CONFIG["MAX_HOLD_MINUTES"]) * 60
    now = time.time()

    for s in open_list:
        if s.get("status") != "OPEN":
            continue
        entry = float(s.get("entry") or 0)
        sl = float(s.get("sl") or 0)
        tp1 = float(s.get("tp1") or 0)
        tp2 = float(s.get("tp2") or 0)
        tp3 = float(s.get("tp3") or 0)
        direction = s.get("direction") or "LONG"
        ts = float(s.get("ts") or 0)
        age = now - ts
        outcome = None
        exit_px = live

        if direction == "LONG":
            if live <= sl:
                outcome, exit_px = "SL", live
            elif tp3 and live >= tp3:
                outcome, exit_px = "TP3", live
            elif tp2 and live >= tp2:
                outcome, exit_px = "TP2", live
            elif live >= tp1:
                outcome, exit_px = "TP1", live
        else:
            if live >= sl:
                outcome, exit_px = "SL", live
            elif tp3 and live <= tp3:
                outcome, exit_px = "TP3", live
            elif tp2 and live <= tp2:
                outcome, exit_px = "TP2", live
            elif live <= tp1:
                outcome, exit_px = "TP1", live

        if outcome is None and age >= max_hold:
            outcome, exit_px = "TIMEOUT", live

        if outcome is None:
            s["last_price"] = live
            s["unrealized_pct"] = round(_pct(entry, live, direction), 3)
            still_open.append(s)
            continue

        _close_signal(state, s, outcome, exit_px, age)

    state["open_signals"] = still_open
    return state


def resolve_open_signals_candle(state: dict[str, Any], candle: Candle) -> dict[str, Any]:
    """Resolve vs completed candle OHLC — correct for yfinance (no true live tick)."""
    open_list = list(state.get("open_signals") or [])
    still_open = []
    max_hold = float(CONFIG["MAX_HOLD_MINUTES"]) * 60
    now = candle.close_time / 1000.0

    for s in open_list:
        if s.get("status") != "OPEN":
            continue
        entry = float(s.get("entry") or 0)
        sl = float(s.get("sl") or 0)
        tps = [float(s.get(k) or 0) for k in ("tp1", "tp2", "tp3")]
        direction = s.get("direction") or "LONG"
        ts = float(s.get("ts") or 0)
        age = now - ts
        high, low, close = candle.high, candle.low, candle.close

        if direction == "LONG":
            sl_hit = low <= sl
            hits = [i + 1 for i, t in enumerate(tps) if t and high >= t]
        else:
            sl_hit = high >= sl
            hits = [i + 1 for i, t in enumerate(tps) if t and low <= t]

        outcome, exit_px = None, close
        # If both SL and TP in same bar: conservative → count as SL
        if sl_hit and hits:
            outcome, exit_px = "SL", sl
        elif sl_hit:
            outcome, exit_px = "SL", sl
        elif hits:
            level = max(hits)
            outcome, exit_px = f"TP{level}", tps[level - 1]
        elif age >= max_hold:
            outcome, exit_px = "TIMEOUT", close

        if outcome is None:
            s["last_price"] = close
            s["unrealized_pct"] = round(_pct(entry, close, direction), 3)
            still_open.append(s)
            continue

        _close_signal(state, s, outcome, exit_px, age)

    state["open_signals"] = still_open
    return state


def market_snapshot(candles: list[Candle], htf: Optional[list[Candle]] = None) -> dict[str, Any]:
    """Compact analysis for reports."""
    c = [x.close for x in candles]
    h = [x.high for x in candles]
    l = [x.low for x in candles]
    i = len(c) - 1
    e21 = ema_series(c, 21)
    e50 = ema_series(c, 50)
    e100 = ema_series(c, 100)
    rsi = rsi_series(c, 14)
    atr = atr_series(h, l, c, 14)
    adx, pdi, mdi = adx_series(h, l, c, 14)
    st_line, st_dir = supertrend_series(h, l, c, 10, 3.0)
    price = c[i]
    snap = {
        "price": round(price, 3),
        "ema21": round(e21[i], 3) if not _isnan(e21[i]) else None,
        "ema50": round(e50[i], 3) if not _isnan(e50[i]) else None,
        "ema100": round(e100[i], 3) if not _isnan(e100[i]) else None,
        "rsi": round(rsi[i], 1) if not _isnan(rsi[i]) else None,
        "atr": round(atr[i], 4) if not _isnan(atr[i]) else None,
        "adx": round(adx[i], 1) if not _isnan(adx[i]) else None,
        "supertrend": "BULL" if st_dir[i] == 1 else ("BEAR" if st_dir[i] == -1 else "N/A"),
        "regime": "TRENDING" if (not _isnan(adx[i]) and adx[i] >= CONFIG["ADX_TREND_MIN"]) else "RANGING",
        "htf": htf_trend(htf) if htf else "N/A",
        "change_24bars_pct": round((price - c[-25]) / c[-25] * 100, 2) if len(c) > 25 and c[-25] else None,
    }
    return snap


def build_report(state: dict[str, Any], snap: dict[str, Any]) -> str:
    """Honest performance report: WR all closed + without TO + expectancy."""
    closed = list(state.get("closed_signals") or [])
    open_n = len(state.get("open_signals") or [])
    now = time.time()
    # windows
    def window(sec: float):
        rows = [x for x in closed if now - float(x.get("closed_ts") or 0) <= sec]
        return rows

    def stats_of(rows: list) -> dict[str, Any]:
        if not rows:
            return {"n": 0, "tp": 0, "sl": 0, "to": 0, "wr_all": 0.0, "wr_dec": 0.0, "avg": 0.0, "sum": 0.0}
        tp = sum(1 for x in rows if str(x.get("status", "")).startswith("TP"))
        sl = sum(1 for x in rows if x.get("status") == "SL")
        to = sum(1 for x in rows if x.get("status") == "TIMEOUT")
        n = len(rows)
        decided = tp + sl
        rets = [float(x.get("net_return_pct") if x.get("net_return_pct") is not None else x.get("return_pct") or 0) for x in rows]
        return {
            "n": n,
            "tp": tp,
            "sl": sl,
            "to": to,
            "wr_all": (tp / n * 100) if n else 0.0,
            "wr_dec": (tp / decided * 100) if decided else 0.0,
            "avg": sum(rets) / n if n else 0.0,
            "sum": sum(rets),
        }

    d24 = stats_of(window(86400))
    d7 = stats_of(window(7 * 86400))
    dall = stats_of(closed)
    uptime_h = (now - float(state.get("uptime_start") or now)) / 3600

    lines = [
        f"<b>📊 GOLD REPORT — {CONFIG['SYMBOL']}</b>",
        f"⏰ {utc_now().strftime('%Y-%m-%d %H:%M UTC')}",
        f"Uptime: {uptime_h:.1f}h | Open: {open_n}",
        "",
        "<b>📈 Market snapshot</b>",
        f"Price: <code>{snap.get('price')}</code> | Regime: {snap.get('regime')}",
        f"RSI {snap.get('rsi')} | ADX {snap.get('adx')} | ST {snap.get('supertrend')} | HTF {snap.get('htf')}",
        f"EMA21 {snap.get('ema21')} | EMA50 {snap.get('ema50')} | EMA100 {snap.get('ema100')}",
        f"ATR {snap.get('atr')} | Δ~24bars {snap.get('change_24bars_pct')}%",
        "",
        "<b>📡 Performance (honest)</b>",
        (
            f"۲۴س: n={d24['n']} | WR همه={d24['wr_all']:.1f}% | "
            f"WR بدون TO={d24['wr_dec']:.1f}% | میانگین={d24['avg']:+.2f}%"
        ),
        f"     TP={d24['tp']} SL={d24['sl']} TO={d24['to']} | sum={d24['sum']:+.2f}%",
        (
            f"۷روز: n={d7['n']} | WR همه={d7['wr_all']:.1f}% | "
            f"WR بدون TO={d7['wr_dec']:.1f}% | میانگین={d7['avg']:+.2f}%"
        ),
        f"     TP={d7['tp']} SL={d7['sl']} TO={d7['to']} | sum={d7['sum']:+.2f}%",
        (
            f"کل: n={dall['n']} | WR همه={dall['wr_all']:.1f}% | "
            f"WR بدون TO={dall['wr_dec']:.1f}% | میانگین={dall['avg']:+.2f}%"
        ),
        "",
        f"Settings: TF={CONFIG['KLINE_INTERVAL']} | minConf={CONFIG['MIN_CONFIDENCE']} | "
        f"hold={CONFIG['MAX_HOLD_MINUTES']}m | session={CONFIG['SESSION_FILTER']}",
        "⚠️ Signal-only bot — paper outcomes from live price checks",
    ]

    # open positions detail
    opens = state.get("open_signals") or []
    if opens:
        lines.append("")
        lines.append("<b>⏳ Open</b>")
        for s in opens[:8]:
            u = s.get("unrealized_pct")
            u_s = f"{u:+.2f}%" if u is not None else "?"
            lines.append(f"• {s.get('direction')} @ {s.get('entry')} | uPnL {u_s}")

    return "\n".join(lines)


def maybe_send_report(state: dict[str, Any], snap: dict[str, Any]) -> dict[str, Any]:
    now = time.time()
    last = float(state.get("last_report_ts") or 0)
    if now - last < float(CONFIG["REPORT_INTERVAL_SEC"]):
        return state
    msg = build_report(state, snap)
    if send_telegram(msg):
        state["last_report_ts"] = now
        log.info("report sent")
        save_state(state)
    return state


def run_once(state: dict[str, Any]) -> dict[str, Any]:
    candles = None
    htf = None
    live = 0.0
    live_is_tick = False

    try:
        candles = fetch_klines()
        last = candles[-1]
        # yfinance: outcomes from completed candle OHLC (honest). binance: optional live tick.
        if CONFIG["DATA_SOURCE"] == "yfinance":
            if CONFIG.get("OUTCOME_CHECK_EACH_CYCLE", True):
                state = resolve_open_signals_candle(state, last)
            live, live_is_tick = last.close, False
        else:
            try:
                live = fetch_live_price()
                live_is_tick = live > 0
            except Exception as e:
                log.warning("live price: %s", e)
                live, live_is_tick = last.close, False
            if CONFIG.get("OUTCOME_CHECK_EACH_CYCLE", True):
                if live_is_tick:
                    state = resolve_open_signals(state, live)
                else:
                    state = resolve_open_signals_candle(state, last)
        maybe_auto_tune(state)
        save_state(state)

        if CONFIG["USE_HTF_FILTER"]:
            try:
                htf = fetch_klines(CONFIG["HTF_INTERVAL"])
            except Exception as e:
                log.warning("HTF: %s", e)
        snap = market_snapshot(candles, htf)
        snap["source"] = CONFIG["DATA_SOURCE"]
        snap["price_is_live_tick"] = live_is_tick
        state = maybe_send_report(state, snap)
    except Exception as e:
        log.warning("snapshot/report: %s", e)
        return state

    if not in_session():
        log.info("out of session (outcomes/report still run)")
        return state

    if candles is None:
        return state

    last = candles[-1]
    age = max(0.0, time.time() - last.close_time / 1000.0)
    if age > float(CONFIG.get("MAX_SOURCE_AGE_SEC", 1200)):
        log.warning("stale candle age %.0fs — no new signal", age)
        return state

    # Analyze each closed bar only once
    candle_key = int(last.close_time)
    if candle_key <= int(state.get("last_analyzed_candle_close_time") or 0):
        log.info("no new closed candle; skip analysis")
        return state
    state["last_analyzed_candle_close_time"] = candle_key

    log.info(
        "bar close=%.3f @ %s | open=%d | src=%s",
        last.close,
        datetime.fromtimestamp(last.close_time / 1000, tz=timezone.utc).strftime("%H:%M UTC"),
        len(state.get("open_signals") or []),
        CONFIG["DATA_SOURCE"],
    )

    raw = analyze(candles, htf)
    if raw is None:
        save_state(state)
        return state

    ok, why = can_send(state, raw.direction)
    if not ok:
        log.info("block: %s", why)
        save_state(state)
        return state

    # yfinance: entry = last closed bar (not a broker live quote)
    if CONFIG["DATA_SOURCE"] == "yfinance" or not live_is_tick:
        sig = raw
        if not any("completed candle" in r for r in sig.reasons):
            sig.reasons.append("entry = last completed candle (not live broker tick)")
    else:
        sig = reanchor_live(raw, live)
        if sig is None:
            return state

    risk = abs(sig.entry - sig.sl)
    reward = abs(sig.tp1 - sig.entry)
    rr = reward / risk if risk else 0
    if rr < float(CONFIG.get("MIN_RR_TP1", 0.80)):
        log.info("reject RR %.2f < min", rr)
        return state

    if send_telegram(format_msg(sig)):
        log.info("SENT %s conf=%.1f entry=%.3f", sig.direction, sig.confidence, sig.entry)
        state["last_signal_ts"] = time.time()
        state["last_direction"] = sig.direction
        state["day_count"] = int(state.get("day_count") or 0) + 1
        d = sig.to_dict()
        d["status"] = "OPEN"
        d["id"] = f"{sig.symbol}|{sig.direction}|{sig.ts}"
        d["entry_price_type"] = "live_tick" if live_is_tick else "last_closed_candle"
        d["source"] = CONFIG["DATA_SOURCE"]
        hist = state.get("history") or []
        hist.append(d)
        state["history"] = hist[-300:]
        opens = state.get("open_signals") or []
        opens.append(d)
        state["open_signals"] = opens
        stats = state.setdefault("stats", {"sent": 0, "tp": 0, "sl": 0, "timeout": 0})
        stats["sent"] = int(stats.get("sent") or 0) + 1
        if not state.get("uptime_start"):
            state["uptime_start"] = time.time()
        save_state(state)
    return state


def main() -> None:
    log.info(
        "GOLD FULL+REPORTS | %s | %s | src=%s | report=%ss",
        CONFIG["SYMBOL"],
        CONFIG["KLINE_INTERVAL"],
        CONFIG["DATA_SOURCE"],
        CONFIG["REPORT_INTERVAL_SEC"],
    )
    if not CONFIG["TELEGRAM_BOT_TOKEN"] or not CONFIG["TELEGRAM_CHAT_ID"]:
        log.error("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
        return
    state = load_state()
    if not state.get("uptime_start"):
        state["uptime_start"] = time.time()
        save_state(state)
    interval = max(30, int(CONFIG["CHECK_INTERVAL_SEC"]))
    while True:
        try:
            state = run_once(state)
        except Exception:
            log.exception("cycle error")
        time.sleep(interval)


if __name__ == "__main__":
    main()
