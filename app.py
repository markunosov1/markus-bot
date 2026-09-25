import os
import json
import time
import logging
import threading
import warnings
from datetime import datetime, timedelta, timezone

import requests
import urllib3
from flask import Flask, jsonify, render_template_string

APP_NAME = "Markus Trade"
API_BASE = "https://invest-public-api.tbank.ru/rest"
FIND_INSTRUMENT_URL = API_BASE + "/tinkoff.public.invest.api.contract.v1.InstrumentsService/FindInstrument"
FUTURES_URL = API_BASE + "/tinkoff.public.invest.api.contract.v1.InstrumentsService/Futures"
SHARES_URL = API_BASE + "/tinkoff.public.invest.api.contract.v1.InstrumentsService/Shares"
CANDLES_URL = API_BASE + "/tinkoff.public.invest.api.contract.v1.MarketDataService/GetCandles"

REQUEST_TIMEOUT = 30
CANDLE_INTERVAL = "CANDLE_INTERVAL_4_HOUR"
HISTORY_HOURS = 24 * 60
UPDATE_SECONDS = 300
POSITION_SIZE_RUBLES = 100000.0
BUY_COMMISSION_PERCENT = 0.10
SELL_COMMISSION_PERCENT = 0.10
TAX_PERCENT = 13.0
HISTORY_FILE = "trade_history.json"
MIN_BACKTEST_TRADES = 3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("MARKUS_TRADE")
app = Flask(__name__)


def get_token():
    for name in ("TINKOFF_TOKEN", "TINVEST_TOKEN", "T_BANK_TOKEN", "API_TOKEN", "TOKEN"):
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return None


def api_post(url, payload):
    token = get_token()
    if not token:
        raise RuntimeError("API-ÑÐ¾ÐºÐµÐ½ Ð½Ðµ Ð½Ð°Ð¹Ð´ÐµÐ½. ÐÑÐ¾Ð²ÐµÑÑ Ð¿ÐµÑÐµÐ¼ÐµÐ½Ð½ÑÑ TINKOFF_TOKEN.")
    response = requests.post(
        url,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        json=payload,
        timeout=REQUEST_TIMEOUT,
        verify=False,
    )
    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:1000]}")
    try:
        return response.json()
    except Exception as exc:
        raise RuntimeError("T-Bank Ð²ÐµÑÐ½ÑÐ» Ð¾ÑÐ²ÐµÑ, ÐºÐ¾ÑÐ¾ÑÑÐ¹ Ð½Ðµ ÑÐ´Ð°Ð»Ð¾ÑÑ Ð¿ÑÐ¾ÑÐ¸ÑÐ°ÑÑ ÐºÐ°Ðº JSON.") from exc


def get_string(obj, key):
    value = obj.get(key)
    return "" if value is None else str(value)


def parse_date(value):
    if not value:
        return None
    try:
        text = str(value)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def quotation_to_float(value):
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        try:
            return float(value.get("units", 0)) + float(value.get("nano", 0)) / 1_000_000_000
        except Exception:
            return 0.0
    try:
        return float(str(value))
    except Exception:
        return 0.0


def get_all_futures():
    for status in ("INSTRUMENT_STATUS_BASE", "INSTRUMENT_STATUS_ALL"):
        try:
            data = api_post(FUTURES_URL, {"instrumentStatus": status})
            futures = data.get("futures", [])
            if isinstance(futures, list):
                return futures
        except Exception as exc:
            log.warning("ÐÑÐ¸Ð±ÐºÐ° Futures (%s): %s", status, exc)
    return []


def matches_future(future, prefix):
    text = " ".join([
        get_string(future, "ticker").upper(),
        get_string(future, "name").upper(),
        get_string(future, "basicAsset").upper(),
    ])
    keywords = {
        "CR": ["CR", "CNY", "YUAN", "CNH", "Ð®ÐÐ", "ÐÐÐ¢ÐÐ"],
        "GD": ["GD", "GOLD", "ÐÐÐÐÐ¢"],
        "BR": ["BR", "BRENT", "ÐÐÐ¤Ð¢"],
    }.get(prefix.upper(), [prefix.upper()])
    return any(word in text for word in keywords)


def find_active_future(prefix):
    queries = {
        "CR": ["CR", "CNY", "ÑÐ°Ð½Ñ", "CNY/RUB"],
        "GD": ["GD", "GOLD", "Ð·Ð¾Ð»Ð¾ÑÐ¾"],
        "BR": ["BR", "BRENT", "Ð½ÐµÑÑÑ"],
    }.get(prefix, [prefix])
    candidates = []
    now = datetime.now(timezone.utc)

    # First use the full futures list; FindInstrument is a fallback.
    for item in get_all_futures():
        if not isinstance(item, dict) or not matches_future(item, prefix):
            continue
        uid = item.get("instrumentUid") or item.get("uid")
        if not uid:
            continue
        last_trade = parse_date(item.get("lastTradeDate"))
        first_trade = parse_date(item.get("firstTradeDate"))
        if first_trade and now < first_trade:
            continue
        if last_trade and now > last_trade:
            continue
        candidates.append({
            "ticker": get_string(item, "ticker"),
            "name": get_string(item, "name"),
            "uid": uid,
            "instrument_uid": uid,
            "first_trade": first_trade,
            "last_trade": last_trade,
            "class_code": get_string(item, "classCode"),
            "basic_asset": get_string(item, "basicAsset"),
        })

    if not candidates:
        for query in queries:
            try:
                data = api_post(FIND_INSTRUMENT_URL, {
                    "query": query,
                    "instrumentKind": "INSTRUMENT_TYPE_FUTURES",
                    "apiTradeAvailableFlag": True,
                })
            except Exception as exc:
                log.warning("FindInstrument %s: %s", query, exc)
                continue
            for item in data.get("instruments", []) if isinstance(data.get("instruments", []), list) else []:
                if not isinstance(item, dict) or not matches_future(item, prefix):
                    continue
                uid = item.get("instrumentUid") or item.get("uid")
                if not uid:
                    continue
                first_trade = parse_date(item.get("firstTradeDate"))
                last_trade = parse_date(item.get("lastTradeDate"))
                if first_trade and now < first_trade:
                    continue
                if last_trade and now > last_trade:
                    continue
                candidates.append({
                    "ticker": get_string(item, "ticker"),
                    "name": get_string(item, "name"),
                    "uid": uid,
                    "instrument_uid": uid,
                    "first_trade": first_trade,
                    "last_trade": last_trade,
                    "class_code": get_string(item, "classCode"),
                    "basic_asset": get_string(item, "basicAsset"),
                })

    unique = {x["instrument_uid"]: x for x in candidates}
    candidates = list(unique.values())
    if not candidates:
        return None
    candidates.sort(key=lambda x: x.get("last_trade") or datetime.max.replace(tzinfo=timezone.utc))
    return candidates[0]


STOCKS = [
    {"code": "SBER", "title": "Ð¡Ð±ÐµÑÐ±Ð°Ð½Ðº", "emoji": "ð¦", "queries": ["SBER", "Ð¡Ð±ÐµÑÐ±Ð°Ð½Ðº"]},
    {"code": "ROSN", "title": "Ð Ð¾ÑÐ½ÐµÑÑÑ", "emoji": "ð¢ï¸", "queries": ["ROSN", "Ð Ð¾ÑÐ½ÐµÑÑÑ"]},
    {"code": "GMKN", "title": "ÐÐ¾ÑÐ½Ð¸ÐºÐµÐ»Ñ", "emoji": "âï¸", "queries": ["GMKN", "NORNICKEL", "ÐÐ¾ÑÐ½Ð¸ÐºÐµÐ»Ñ"]},
]


def find_share(stock):
    candidates = []
    now = datetime.now(timezone.utc)
    for query in stock["queries"]:
        try:
            data = api_post(FIND_INSTRUMENT_URL, {
                "query": query,
                "instrumentKind": "INSTRUMENT_TYPE_SHARE",
                "apiTradeAvailableFlag": True,
            })
        except Exception as exc:
            log.warning("FindInstrument Ð°ÐºÑÐ¸Ð¸ %s: %s", query, exc)
            continue
        instruments = data.get("instruments", [])
        if not isinstance(instruments, list):
            continue
        for item in instruments:
            if not isinstance(item, dict):
                continue
            ticker = get_string(item, "ticker").upper()
            name = get_string(item, "name").upper()
            if stock["code"] not in ticker and stock["title"].upper() not in name:
                continue
            uid = item.get("instrumentUid") or item.get("uid")
            if not uid:
                continue
            first_trade = parse_date(item.get("firstTradeDate"))
            last_trade = parse_date(item.get("lastTradeDate"))
            if first_trade and now < first_trade:
                continue
            if last_trade and now > last_trade:
                continue
            candidates.append({
                "ticker": get_string(item, "ticker"),
                "name": get_string(item, "name"),
                "uid": uid,
                "instrument_uid": uid,
                "figi": get_string(item, "figi"),
                "class_code": get_string(item, "classCode"),
            })
    unique = {x["instrument_uid"]: x for x in candidates}
    candidates = list(unique.values())
    if not candidates:
        return None
    exact = [x for x in candidates if x["ticker"].upper() == stock["code"]]
    return exact[0] if exact else candidates[0]


def get_candles(instrument_uid):
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=HISTORY_HOURS)
    data = api_post(CANDLES_URL, {
        "from": start.isoformat(),
        "to": now.isoformat(),
        "interval": CANDLE_INTERVAL,
        "instrumentId": instrument_uid,
        "candleSourceType": "CANDLE_SOURCE_EXCHANGE",
    })
    candles = data.get("candles", [])
    return candles if isinstance(candles, list) else []


def normalize_candles(candles):
    result = []
    for candle in candles:
        if not isinstance(candle, dict):
            continue
        dt = parse_date(candle.get("time"))
        if not dt:
            continue
        item = {
            "time": dt.isoformat(),
            "open": quotation_to_float(candle.get("open")),
            "high": quotation_to_float(candle.get("high")),
            "low": quotation_to_float(candle.get("low")),
            "close": quotation_to_float(candle.get("close")),
            "volume": quotation_to_float(candle.get("volume")),
        }
        if item["close"] > 0 and item["high"] > 0 and item["low"] > 0:
            result.append(item)
    result.sort(key=lambda x: x["time"])
    return result


def ema(values, period):
    if len(values) < period:
        return None
    k = 2.0 / (period + 1.0)
    value = sum(values[:period]) / period
    for price in values[period:]:
        value = price * k + value * (1 - k)
    return value


def sma(values, period):
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def bollinger_strategy(candles):
    if len(candles) < 20:
        return no_signal("ÐÐµÐ´Ð¾ÑÑÐ°ÑÐ¾ÑÐ½Ð¾ ÑÐ²ÐµÑÐµÐ¹ Ð´Ð»Ñ Bollinger")
    closes = [x["close"] for x in candles[-20:]]
    mid = sum(closes) / 20
    variance = sum((x - mid) ** 2 for x in closes) / 20
    std = variance ** 0.5
    upper = mid + 2 * std
    lower = mid - 2 * std
    last = closes[-1]
    prev = closes[-2]
    if prev <= lower and last > prev:
        return {"signal": "LONG", "direction": "ÐÐ²ÐµÑÑ", "description": f"Ð¦ÐµÐ½Ð° Ð¾ÑÑÐºÐ¾ÑÐ¸Ð»Ð° Ð¾Ñ Ð½Ð¸Ð¶Ð½ÐµÐ¹ Ð¿Ð¾Ð»Ð¾ÑÑ Bollinger ({lower:.2f})"}
    if prev >= upper and last < prev:
        return {"signal": "SHORT", "direction": "ÐÐ½Ð¸Ð·", "description": f"Ð¦ÐµÐ½Ð° ÑÐ°Ð·Ð²ÐµÑÐ½ÑÐ»Ð°ÑÑ Ð¾Ñ Ð²ÐµÑÑÐ½ÐµÐ¹ Ð¿Ð¾Ð»Ð¾ÑÑ Bollinger ({upper:.2f})"}
    return no_signal(f"Ð¦ÐµÐ½Ð° Ð²Ð½ÑÑÑÐ¸ Ð´Ð¸Ð°Ð¿Ð°Ð·Ð¾Ð½Ð° Bollinger: {lower:.2f}â{upper:.2f}")


def no_signal(description="Ð¡Ð¸Ð³Ð½Ð°Ð» Ð½Ðµ ÑÑÐ¾ÑÐ¼Ð¸ÑÐ¾Ð²Ð°Ð½"):
    return {"signal": "ÐÐµÑ ÑÐ¸Ð³Ð½Ð°Ð»Ð¾Ð²", "direction": "â", "description": description}


def user_strategy(candles):
    if len(candles) < 8:
        return no_signal("ÐÐµÐ´Ð¾ÑÑÐ°ÑÐ¾ÑÐ½Ð¾ ÑÐ²ÐµÑÐµÐ¹ Ð´Ð»Ñ Ð°Ð²ÑÐ¾ÑÑÐºÐ¾Ð¹ ÑÑÑÐ°ÑÐµÐ³Ð¸Ð¸")
    last = candles[-8:]
    highs = [x["high"] for x in last]
    lows = [x["low"] for x in last]
    closes = [x["close"] for x in last]
    short_pattern = (
        highs[3] > highs[2] and highs[4] > highs[3] and highs[5] > highs[4]
        and closes[-1] < closes[-2] and closes[-2] < closes[-3]
    )
    long_pattern = (
        lows[3] < lows[2] and lows[4] < lows[3] and lows[5] < lows[4]
        and closes[-1] > closes[-2] and closes[-2] > closes[-3]
    )
    if short_pattern:
        return {"signal": "SHORT", "direction": "ÐÐ½Ð¸Ð·", "description": "ÐÐ²ÑÐ¾ÑÑÐºÐ°Ñ ÑÑÑÐ°ÑÐµÐ³Ð¸Ñ: ÑÐ°ÑÑÑÑÐ¸Ðµ Ð¼Ð°ÐºÑÐ¸Ð¼ÑÐ¼Ñ â Ð¿Ð¾Ð´ÑÐ²ÐµÑÐ¶Ð´ÑÐ½Ð½ÑÐ¹ ÑÐ°Ð·Ð²Ð¾ÑÐ¾Ñ Ð²Ð½Ð¸Ð·"}
    if long_pattern:
        return {"signal": "LONG", "direction": "ÐÐ²ÐµÑÑ", "description": "ÐÐ²ÑÐ¾ÑÑÐºÐ°Ñ ÑÑÑÐ°ÑÐµÐ³Ð¸Ñ: ÑÐ½Ð¸Ð¶Ð°ÑÑÐ¸ÐµÑÑ Ð¼Ð¸Ð½Ð¸Ð¼ÑÐ¼Ñ â Ð¿Ð¾Ð´ÑÐ²ÐµÑÐ¶Ð´ÑÐ½Ð½ÑÐ¹ ÑÐ°Ð·Ð²Ð¾ÑÐ¾Ñ Ð²Ð²ÐµÑÑ"}
    return no_signal("ÐÐµÑ Ð¿Ð¾Ð»Ð½Ð¾Ð³Ð¾ ÑÐ¾Ð²Ð¿Ð°Ð´ÐµÐ½Ð¸Ñ ÑÑÐ»Ð¾Ð²Ð¸Ð¹ Ð°Ð²ÑÐ¾ÑÑÐºÐ¾Ð¹ ÑÑÑÐ°ÑÐµÐ³Ð¸Ð¸")


def ema_trend_strategy(candles):
    if len(candles) < 30:
        return no_signal("ÐÐµÐ´Ð¾ÑÑÐ°ÑÐ¾ÑÐ½Ð¾ ÑÐ²ÐµÑÐµÐ¹ Ð´Ð»Ñ EMA")
    closes = [x["close"] for x in candles]
    e9, e21 = ema(closes, 9), ema(closes, 21)
    if e9 is None or e21 is None:
        return no_signal("EMA ÐµÑÑ Ð½Ðµ ÑÐ°ÑÑÑÐ¸ÑÐ°Ð½Ñ")
    if e9 > e21 and closes[-1] > e9 and closes[-1] > closes[-2]:
        return {"signal": "LONG", "direction": "ÐÐ²ÐµÑÑ", "description": "EMA 9 Ð²ÑÑÐµ EMA 21, ÑÐµÐ½Ð° Ð²ÑÑÐµ EMA 9 Ð¸ ÑÐ°ÑÑÑÑ"}
    if e9 < e21 and closes[-1] < e9 and closes[-1] < closes[-2]:
        return {"signal": "SHORT", "direction": "ÐÐ½Ð¸Ð·", "description": "EMA 9 Ð½Ð¸Ð¶Ðµ EMA 21, ÑÐµÐ½Ð° Ð½Ð¸Ð¶Ðµ EMA 9 Ð¸ ÑÐ½Ð¸Ð¶Ð°ÐµÑÑÑ"}
    return no_signal("ÐÐµÑ Ð¿Ð¾Ð´ÑÐ²ÐµÑÐ¶Ð´ÐµÐ½Ð¸Ñ ÑÑÐµÐ½Ð´Ð° EMA")


def breakout_strategy(candles):
    if len(candles) < 21:
        return no_signal("ÐÐµÐ´Ð¾ÑÑÐ°ÑÐ¾ÑÐ½Ð¾ ÑÐ²ÐµÑÐµÐ¹ Ð´Ð»Ñ Breakout")
    prev = candles[-21:-1]
    last = candles[-1]
    high = max(x["high"] for x in prev)
    low = min(x["low"] for x in prev)
    if last["close"] > high:
        return {"signal": "LONG", "direction": "ÐÐ²ÐµÑÑ", "description": f"ÐÐ°ÐºÑÑÑÐ¸Ðµ Ð¿ÑÐ¾Ð±Ð¸Ð»Ð¾ Ð¼Ð°ÐºÑÐ¸Ð¼ÑÐ¼ 20 Ð¿ÑÐµÐ´ÑÐ´ÑÑÐ¸Ñ ÑÐ²ÐµÑÐµÐ¹ ({high:.2f})"}
    if last["close"] < low:
        return {"signal": "SHORT", "direction": "ÐÐ½Ð¸Ð·", "description": f"ÐÐ°ÐºÑÑÑÐ¸Ðµ Ð¿ÑÐ¾Ð±Ð¸Ð»Ð¾ Ð¼Ð¸Ð½Ð¸Ð¼ÑÐ¼ 20 Ð¿ÑÐµÐ´ÑÐ´ÑÑÐ¸Ñ ÑÐ²ÐµÑÐµÐ¹ ({low:.2f})"}
    return no_signal("ÐÑÐ¾Ð±Ð¾Ñ 20-ÑÐ²ÐµÑÐ½Ð¾Ð³Ð¾ Ð´Ð¸Ð°Ð¿Ð°Ð·Ð¾Ð½Ð° Ð½ÐµÑ")


def rsi_strategy(candles):
    if len(candles) < 16:
        return no_signal("ÐÐµÐ´Ð¾ÑÑÐ°ÑÐ¾ÑÐ½Ð¾ ÑÐ²ÐµÑÐµÐ¹ Ð´Ð»Ñ RSI")
    closes = [x["close"] for x in candles[-15:]]
    gains, losses = [], []
    for a, b in zip(closes[:-1], closes[1:]):
        d = b - a
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_gain = sum(gains) / len(gains)
    avg_loss = sum(losses) / len(losses)
    rsi = 100.0 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss))
    if rsi < 30 and closes[-1] > closes[-2]:
        return {"signal": "LONG", "direction": "ÐÐ²ÐµÑÑ", "description": f"RSI Ð¿ÐµÑÐµÐ¿ÑÐ¾Ð´Ð°Ð½ ({rsi:.1f}) Ð¸ ÑÐµÐ½Ð° ÑÐ°Ð·Ð²Ð¾ÑÐ°ÑÐ¸Ð²Ð°ÐµÑÑÑ Ð²Ð²ÐµÑÑ"}
    if rsi > 70 and closes[-1] < closes[-2]:
        return {"signal": "SHORT", "direction": "ÐÐ½Ð¸Ð·", "description": f"RSI Ð¿ÐµÑÐµÐºÑÐ¿Ð»ÐµÐ½ ({rsi:.1f}) Ð¸ ÑÐµÐ½Ð° ÑÐ°Ð·Ð²Ð¾ÑÐ°ÑÐ¸Ð²Ð°ÐµÑÑÑ Ð²Ð½Ð¸Ð·"}
    return no_signal(f"RSI ÑÐµÐ¹ÑÐ°Ñ {rsi:.1f}; ÑÑÐ»Ð¾Ð²Ð¸Ñ Ð²ÑÐ¾Ð´Ð° Ð½Ðµ Ð²ÑÐ¿Ð¾Ð»Ð½ÐµÐ½Ñ")


def macd_strategy(candles):
    if len(candles) < 36:
        return no_signal("ÐÐµÐ´Ð¾ÑÑÐ°ÑÐ¾ÑÐ½Ð¾ ÑÐ²ÐµÑÐµÐ¹ Ð´Ð»Ñ MACD")
    closes = [x["close"] for x in candles]
    fast, slow = ema(closes, 12), ema(closes, 26)
    prev_fast, prev_slow = ema(closes[:-1], 12), ema(closes[:-1], 26)
    if None in (fast, slow, prev_fast, prev_slow):
        return no_signal("MACD ÐµÑÑ Ð½Ðµ ÑÐ°ÑÑÑÐ¸ÑÐ°Ð½")
    if prev_fast <= prev_slow and fast > slow:
        return {"signal": "LONG", "direction": "ÐÐ²ÐµÑÑ", "description": "EMA 12 Ð¿ÐµÑÐµÑÐµÐºÐ»Ð° EMA 26 ÑÐ½Ð¸Ð·Ñ Ð²Ð²ÐµÑÑ"}
    if prev_fast >= prev_slow and fast < slow:
        return {"signal": "SHORT", "direction": "ÐÐ½Ð¸Ð·", "description": "EMA 12 Ð¿ÐµÑÐµÑÐµÐºÐ»Ð° EMA 26 ÑÐ²ÐµÑÑÑ Ð²Ð½Ð¸Ð·"}
    return no_signal("ÐÐµÑÐµÑÐµÑÐµÐ½Ð¸Ñ MACD Ð½Ð° Ð¿Ð¾ÑÐ»ÐµÐ´Ð½ÐµÐ¹ ÑÐ²ÐµÑÐµ Ð½ÐµÑ")


STRATEGIES = [
    {"name": "Ð¢Ð²Ð¾Ñ ÑÑÑÐ°ÑÐµÐ³Ð¸Ñ", "key": "user", "fn": user_strategy, "min_bars": 8},
    {"name": "EMA Trend", "key": "ema", "fn": ema_trend_strategy, "min_bars": 30},
    {"name": "Breakout", "key": "breakout", "fn": breakout_strategy, "min_bars": 21},
    {"name": "RSI Reversal", "key": "rsi", "fn": rsi_strategy, "min_bars": 16},
    {"name": "MACD", "key": "macd", "fn": macd_strategy, "min_bars": 36},
    {"name": "Bollinger", "key": "bollinger", "fn": bollinger_strategy, "min_bars": 20},
]


def calculate_commission(amount, percent):
    return amount * percent / 100.0


def calculate_trade_result(direction, entry_price, exit_price):
    if entry_price <= 0 or exit_price <= 0:
        return None
    if direction == "LONG":
        price_change_percent = (exit_price - entry_price) / entry_price * 100
    else:
        price_change_percent = (entry_price - exit_price) / entry_price * 100
    gross_result = POSITION_SIZE_RUBLES * price_change_percent / 100
    buy_commission = calculate_commission(POSITION_SIZE_RUBLES, BUY_COMMISSION_PERCENT)
    exit_amount = POSITION_SIZE_RUBLES * (exit_price / entry_price)
    sell_commission = calculate_commission(abs(exit_amount), SELL_COMMISSION_PERCENT)
    tax = max(gross_result, 0) * TAX_PERCENT / 100
    net_result = gross_result - buy_commission - sell_commission - tax
    return {
        "price_change_percent": round(price_change_percent, 4),
        "gross_result": round(gross_result, 2),
        "buy_commission": round(buy_commission, 2),
        "sell_commission": round(sell_commission, 2),
        "tax": round(tax, 2),
        "net_result": round(net_result, 2),
    }


def calculate_statistics(trades):
    total = len(trades)
    if total == 0:
        return {"total": 0, "profitable": 0, "losing": 0, "winrate": 0, "gross": 0, "commission": 0, "tax": 0, "net": 0}
    profitable = sum(1 for t in trades if t.get("net_result", 0) > 0)
    losing = sum(1 for t in trades if t.get("net_result", 0) < 0)
    gross = sum(t.get("gross_result", 0) for t in trades)
    commission = sum(t.get("buy_commission", 0) + t.get("sell_commission", 0) for t in trades)
    tax = sum(t.get("tax", 0) for t in trades)
    net = sum(t.get("net_result", 0) for t in trades)
    return {
        "total": total,
        "profitable": profitable,
        "losing": losing,
        "winrate": round(profitable / total * 100, 2),
        "gross": round(gross, 2),
        "commission": round(commission, 2),
        "tax": round(tax, 2),
        "net": round(net, 2),
    }


def calculate_max_drawdown(trades):
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for trade in trades:
        equity += float(trade.get("net_result", 0))
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    return round(abs(max_dd), 2)


def build_strategy_history(candles, instrument, title, strategy_fn):
    """Backtest strategy on the FULL available history, not only 8 candles."""
    min_bars = 8
    for s in STRATEGIES:
        if s["fn"] is strategy_fn:
            min_bars = s["min_bars"]
            break
    if len(candles) < min_bars:
        return [], None

    trades = []
    current_position = None
    for i in range(min_bars - 1, len(candles)):
        window = candles[: i + 1]
        analysis = strategy_fn(window)
        signal = analysis.get("signal", "ÐÐµÑ ÑÐ¸Ð³Ð½Ð°Ð»Ð¾Ð²")
        candle = candles[i]
        price = candle["close"]
        candle_time = candle["time"]

        if current_position is None:
            if signal in ("LONG", "SHORT"):
                current_position = {
                    "instrument": instrument,
                    "title": title,
                    "direction": signal,
                    "entry_price": price,
                    "entry_time": candle_time,
                }
            continue

        if signal == current_position["direction"] or signal == "ÐÐµÑ ÑÐ¸Ð³Ð½Ð°Ð»Ð¾Ð²":
            continue

        if signal in ("LONG", "SHORT") and signal != current_position["direction"]:
            result = calculate_trade_result(current_position["direction"], current_position["entry_price"], price)
            if result is not None:
                trades.append({
                    "id": len(trades) + 1,
                    "instrument": instrument,
                    "title": title,
                    "direction": current_position["direction"],
                    "entry_time": current_position["entry_time"],
                    "exit_time": candle_time,
                    "entry_price": round(current_position["entry_price"], 8),
                    "exit_price": round(price, 8),
                    "exit_signal": signal,
                    **result,
                })
            current_position = {
                "instrument": instrument,
                "title": title,
                "direction": signal,
                "entry_price": price,
                "entry_time": candle_time,
            }

    return trades, current_position


def evaluate_all_strategies(candles, instrument, title):
    rows = []
    for strategy in STRATEGIES:
        trades, open_position = build_strategy_history(candles, instrument, title, strategy["fn"])
        stats = calculate_statistics(trades)
        drawdown = calculate_max_drawdown(trades)
        last_analysis = strategy["fn"](candles)
        if stats["total"] >= MIN_BACKTEST_TRADES:
            # Transparent technical selection score: historical net profit, win rate and drawdown.
            score = (stats["net"] / (1.0 + drawdown)) * 100 + stats["winrate"] * 2
            eligible = True
            reason = "ÐÑÑÑ Ð¼Ð¸Ð½Ð¸Ð¼ÑÐ¼ 3 Ð·Ð°ÐºÑÑÑÑÐµ ÑÐ´ÐµÐ»ÐºÐ¸; ÑÑÐ¸ÑÑÐ²Ð°ÑÑÑÑ ÑÐ¸ÑÑÑÐ¹ ÑÐµÐ·ÑÐ»ÑÑÐ°Ñ, Ð¿ÑÐ¾ÑÐ¾Ð´Ð¸Ð¼Ð¾ÑÑÑ Ð¸ Ð¿ÑÐ¾ÑÐ°Ð´ÐºÐ°."
        else:
            score = -1e12 + stats["total"] * 1000 + stats["net"]
            eligible = False
            reason = f"ÐÐµÐ´Ð¾ÑÑÐ°ÑÐ¾ÑÐ½Ð¾ Ð·Ð°ÐºÑÑÑÑÑ ÑÐ´ÐµÐ»Ð¾Ðº Ð´Ð»Ñ Ð½Ð°Ð´ÑÐ¶Ð½Ð¾Ð³Ð¾ ÑÑÐ°Ð²Ð½ÐµÐ½Ð¸Ñ: {stats['total']} Ð¸Ð· {MIN_BACKTEST_TRADES}."
        rows.append({
            "name": strategy["name"],
            "key": strategy["key"],
            "statistics": stats,
            "drawdown": drawdown,
            "score": round(score, 4),
            "trades": trades,
            "open_position": open_position,
            "current_signal": last_analysis.get("signal"),
            "current_description": last_analysis.get("description", ""),
            "eligible": eligible,
            "reason": reason,
        })

    eligible_rows = [r for r in rows if r["eligible"]]
    if eligible_rows:
        best = max(eligible_rows, key=lambda x: x["score"])
        selection_reason = (
            f"ÐÑÐ±ÑÐ°Ð½Ð° Ð¿Ð¾ Ð¸ÑÑÐ¾ÑÐ¸ÑÐµÑÐºÐ¾Ð¼Ñ ÑÐµÑÑÑ Ð½Ð° {len(candles)} ÑÐ²ÐµÑÐ°Ñ: "
            f"{best['statistics']['total']} ÑÐ´ÐµÐ»Ð¾Ðº, Ð¿ÑÐ¾ÑÐ¾Ð´Ð¸Ð¼Ð¾ÑÑÑ {best['statistics']['winrate']}%, "
            f"ÑÐ¸ÑÑÑÐ¹ ÑÐµÐ·ÑÐ»ÑÑÐ°Ñ {best['statistics']['net']:.2f} â½, Ð¿ÑÐ¾ÑÐ°Ð´ÐºÐ° {best['drawdown']:.2f} â½."
        )
    else:
        best = max(rows, key=lambda x: (x["statistics"]["total"], x["statistics"]["net"]))
        selection_reason = (
            "ÐÐ¸ Ð¾Ð´Ð½Ð° ÑÑÑÐ°ÑÐµÐ³Ð¸Ñ Ð½Ðµ Ð½Ð°Ð±ÑÐ°Ð»Ð° Ð¼Ð¸Ð½Ð¸Ð¼ÑÐ¼ 3 Ð·Ð°ÐºÑÑÑÑÐµ ÑÐ´ÐµÐ»ÐºÐ¸. "
            f"ÐÑÐµÐ¼ÐµÐ½Ð½Ð¾ Ð²ÑÐ±ÑÐ°Ð½Ð° ÑÑÑÐ°ÑÐµÐ³Ð¸Ñ Ñ Ð½Ð°Ð¸Ð±Ð¾Ð»ÑÑÐ¸Ð¼ ÐºÐ¾Ð»Ð¸ÑÐµÑÑÐ²Ð¾Ð¼ Ð¸ÑÑÐ¾ÑÐ¸ÑÐµÑÐºÐ¸Ñ ÑÐ´ÐµÐ»Ð¾Ðº ({best['statistics']['total']}). "
            "Ð­ÑÐ¾ Ð½Ðµ Ð¾Ð·Ð½Ð°ÑÐ°ÐµÑ, ÑÑÐ¾ Ð¾Ð½Ð° Ð´Ð¾ÐºÐ°Ð·Ð°Ð½Ð½Ð¾ Ð»ÑÑÑÐµ Ð¾ÑÑÐ°Ð»ÑÐ½ÑÑ."
        )
    for row in rows:
        row["is_selected"] = row["key"] == best["key"]
    rows.sort(key=lambda x: (x["is_selected"], x["score"]), reverse=True)
    return rows, best, selection_reason


def strategy_signal_from_best(candles, best):
    for strategy in STRATEGIES:
        if strategy["key"] == best["key"]:
            return strategy["fn"](candles)
    return no_signal()


def analyze_strategy(candles):
    _, best, _ = evaluate_all_strategies(candles, "instrument", "instrument")
    return strategy_signal_from_best(candles, best)


def load_history():
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception as exc:
        log.warning("ÐÑÐ¸Ð±ÐºÐ° ÑÑÐµÐ½Ð¸Ñ Ð¸ÑÑÐ¾ÑÐ¸Ð¸: %s", exc)
        return []


def save_history(history):
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        log.error("ÐÑÐ¸Ð±ÐºÐ° ÑÐ¾ÑÑÐ°Ð½ÐµÐ½Ð¸Ñ Ð¸ÑÑÐ¾ÑÐ¸Ð¸: %s", exc)


def base_result(kind, code, title, emoji):
    return {
        "type": kind, "prefix": code, "title": title, "emoji": emoji,
        "status": "ÐÑÐ¸Ð±ÐºÐ°", "message": "", "ticker": "â", "uid": "â", "candles": 0,
        "strategy": {"signal": "ÐÐµÑ ÑÐ¸Ð³Ð½Ð°Ð»Ð¾Ð²", "direction": "â", "description": ""},
        "selected_strategy": "â", "selection_reason": "â", "strategy_selection": [],
        "history": [], "statistics": calculate_statistics([]), "open_position": None,
    }


def analyze_instrument(result, instrument, instrument_code, title):
    result["ticker"] = instrument["ticker"]
    result["uid"] = instrument["instrument_uid"]
    candles = normalize_candles(get_candles(instrument["instrument_uid"]))
    result["candles"] = len(candles)
    if not candles:
        result["message"] = "Ð¡Ð²ÐµÑÐµÐ¹ 0 â API Ð½Ðµ Ð²ÐµÑÐ½ÑÐ» Ð¸ÑÑÐ¾ÑÐ¸Ñ Ð´Ð»Ñ ÑÑÐ¾Ð³Ð¾ Ð¸Ð½ÑÑÑÑÐ¼ÐµÐ½ÑÐ°."
        return result
    rankings, best, selection_reason = evaluate_all_strategies(candles, instrument_code, title)
    result["strategy"] = strategy_signal_from_best(candles, best)
    result["selected_strategy"] = best["name"]
    result["selection_reason"] = selection_reason
    result["strategy_selection"] = rankings
    result["status"] = "OK"
    result["message"] = f"ÐÐ°Ð³ÑÑÐ¶ÐµÐ½Ð¾ {len(candles)} ÑÐ²ÐµÑÐµÐ¹. ÐÑÐµ {len(STRATEGIES)} ÑÑÑÐ°ÑÐµÐ³Ð¸Ð¹ Ð¿ÑÐ¾ÑÐµÑÑÐ¸ÑÐ¾Ð²Ð°Ð½Ñ Ð½Ð° Ð¾Ð´Ð½Ð¾Ð¹ Ð¸ ÑÐ¾Ð¹ Ð¶Ðµ Ð¸ÑÑÐ¾ÑÐ¸Ð¸."
    result["history"] = best["trades"]
    result["open_position"] = best["open_position"]
    result["statistics"] = best["statistics"]
    return result


def get_future_status(prefix, title, emoji):
    result = base_result("future", prefix, title, emoji)
    try:
        future = find_active_future(prefix)
        if not future:
            result["message"] = "ÐÐºÑÑÐ°Ð»ÑÐ½ÑÐ¹ ÐºÐ¾Ð½ÑÑÐ°ÐºÑ Ð½Ðµ Ð½Ð°Ð¹Ð´ÐµÐ½."
            return result
        return analyze_instrument(result, future, prefix, title)
    except Exception as exc:
        log.exception("ÐÑÐ¸Ð±ÐºÐ° %s", title)
        result["message"] = str(exc)
        return result


def get_share_status(stock):
    result = base_result("share", stock["code"], stock["title"], stock["emoji"])
    try:
        share = find_share(stock)
        if not share:
            result["message"] = "ÐÐºÑÐ¸Ñ Ð½Ðµ Ð½Ð°Ð¹Ð´ÐµÐ½Ð°."
            return result
        return analyze_instrument(result, share, stock["code"], stock["title"])
    except Exception as exc:
        log.exception("ÐÑÐ¸Ð±ÐºÐ° Ð°ÐºÑÐ¸Ð¸ %s", stock["title"])
        result["message"] = str(exc)
        return result


def collect_data():
    futures = [
        get_future_status("CR", "Ð®Ð°Ð½Ñ", "Â¥"),
        get_future_status("GD", "ÐÐ¾Ð»Ð¾ÑÐ¾", "ð¥"),
        get_future_status("BR", "ÐÐµÑÑÑ Brent", "ð¢ï¸"),
    ]
    shares = [get_share_status(stock) for stock in STOCKS]
    instruments = futures + shares
    all_trades = []
    for item in instruments:
        all_trades.extend(item.get("history", []))
    total_statistics = calculate_statistics(all_trades)

    last_signal = {"title": "ÐÐµÑ ÑÐ¸Ð³Ð½Ð°Ð»Ð¾Ð²", "signal": "â", "direction": "â", "description": ""}
    for item in instruments:
        signal = item["strategy"].get("signal")
        if signal in ("LONG", "SHORT"):
            last_signal = {
                "title": item["title"], "signal": signal,
                "direction": item["strategy"].get("direction", "â"),
                "description": item["strategy"].get("description", ""),
            }
            break

    existing_history = load_history()
    existing_keys = {
        (t.get("instrument"), t.get("entry_time"), t.get("exit_time"), t.get("direction"))
        for t in existing_history
    }
    for trade in all_trades:
        key = (trade.get("instrument"), trade.get("entry_time"), trade.get("exit_time"), trade.get("direction"))
        if key not in existing_keys:
            existing_history.append(trade)
            existing_keys.add(key)
    save_history(existing_history[-5000:])

    return {
        "updated": datetime.now(timezone.utc).isoformat(),
        "futures": futures,
        "shares": shares,
        "last_signal": last_signal,
        "statistics": total_statistics,
        "settings": {
            "position_size": POSITION_SIZE_RUBLES,
            "buy_commission": BUY_COMMISSION_PERCENT,
            "sell_commission": SELL_COMMISSION_PERCENT,
            "tax": TAX_PERCENT,
            "candle_interval": "4 ÑÐ°ÑÐ°",
            "history_days": HISTORY_HOURS // 24,
            "exit_rule": "ÐÑÐ¾ÑÐ¸Ð²Ð¾Ð¿Ð¾Ð»Ð¾Ð¶Ð½ÑÐ¹ ÑÐ¸Ð³Ð½Ð°Ð»",
            "strategies_count": len(STRATEGIES),
        },
    }


@app.route("/api/status")
def api_status():
    try:
        return jsonify(collect_data())
    except Exception as exc:
        log.exception("ÐÑÐ¸Ð±ÐºÐ° /api/status")
        return jsonify({"error": str(exc)}), 500


@app.route("/api/history")
def api_history():
    history = load_history()
    return jsonify({"count": len(history), "history": history})


HTML = r"""
<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Markus Trade</title>
<style>
*{box-sizing:border-box}body{margin:0;background:linear-gradient(135deg,#07090d,#10141c);color:#fff;font-family:Arial,sans-serif;min-height:100vh}.container{width:95%;max-width:1500px;margin:auto;padding:25px 0 50px}.header{display:flex;justify-content:space-between;align-items:center;margin-bottom:25px}.logo{font-size:28px;font-weight:800;letter-spacing:1px}.logo span{color:#d7aa52}.updated{color:#8c96a8;font-size:13px}.section-title{margin:28px 0 14px;font-size:23px}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:18px}.card{background:rgba(22,27,36,.95);border:1px solid rgba(255,255,255,.08);border-radius:18px;padding:20px;box-shadow:0 15px 50px rgba(0,0,0,.25)}.card h2{margin-top:0;font-size:20px}.status{display:inline-block;padding:6px 10px;border-radius:20px;font-size:12px;background:#193d2b;color:#66e29a}.error{background:#442020;color:#ff8585}.signal{margin-top:15px;padding:14px;border-radius:14px;background:#111720;font-size:18px;font-weight:bold}.long{color:#52e58a}.short{color:#ff6666}.none{color:#9ca5b4}.info{margin-top:12px;color:#aab3c2;font-size:13px;line-height:1.6}.selected{border-left:3px solid #d7aa52;padding-left:9px}.strategy-box{margin-top:15px;background:#0d1219;border-radius:12px;padding:12px;font-size:12px;overflow:auto}.strategy-row{display:grid;grid-template-columns:1.4fr .7fr .6fr .9fr .8fr 1.5fr;gap:7px;padding:8px 0;border-bottom:1px solid rgba(255,255,255,.06);color:#b8c0cc;min-width:850px}.strategy-row:last-child{border-bottom:0}.positive{color:#52e58a;font-weight:bold}.negative{color:#ff6666;font-weight:bold}.statistics,.history{margin-top:25px}.stats-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.stat{background:#111720;padding:16px;border-radius:14px}.stat-title{font-size:12px;color:#8f99aa;margin-bottom:7px}.stat-value{font-size:20px;font-weight:bold}.table-wrap{overflow-x:auto}table{width:100%;border-collapse:collapse;min-width:900px}th,td{padding:11px;border-bottom:1px solid rgba(255,255,255,.07);text-align:left;font-size:13px}th{color:#9ca5b4;font-weight:normal}.settings{margin-top:20px;color:#858fa0;font-size:13px;line-height:1.7}button{margin-top:20px;border:none;border-radius:12px;padding:12px 20px;background:#d7aa52;color:#111;font-weight:bold;cursor:pointer}@media(max-width:1100px){.grid{grid-template-columns:1fr 1fr}}@media(max-width:700px){.grid{grid-template-columns:1fr}.stats-grid{grid-template-columns:repeat(2,1fr)}.header{align-items:flex-start;gap:10px;flex-direction:column}}
</style></head><body><div class="container"><div class="header"><div class="logo">MARKUS <span>TRADE</span></div><div class="updated" id="updated">ÐÐ°Ð³ÑÑÐ·ÐºÐ°...</div></div>
<div class="section-title">ð Ð¤Ð¬Ð®Ð§ÐÐ Ð¡Ð« â 4 Ð§ÐÐ¡Ð</div><div class="grid" id="futures"></div>
<div class="section-title">ð ÐÐÐ¦ÐÐ â 4 Ð§ÐÐ¡Ð</div><div class="grid" id="shares"></div>
<div class="card statistics"><h2>ð ÐÐ±ÑÐ°Ñ ÑÑÐ°ÑÐ¸ÑÑÐ¸ÐºÐ°</h2><div class="stats-grid" id="statistics"></div></div>
<div class="card history"><h2>ð ÐÑÑÐ¾ÑÐ¸Ñ ÑÐ´ÐµÐ»Ð¾Ðº</h2><div class="table-wrap" id="history"></div></div>
<div class="card settings"><h2>âï¸ ÐÐ°ÑÑÑÐ¾Ð¹ÐºÐ¸</h2>Ð Ð°Ð·Ð¼ÐµÑ Ð²Ð¸ÑÑÑÐ°Ð»ÑÐ½Ð¾Ð¹ Ð¿Ð¾Ð·Ð¸ÑÐ¸Ð¸: <b id="positionSize">â</b> â½<br>ÐÐ¾Ð¼Ð¸ÑÑÐ¸Ñ Ð¿Ð¾ÐºÑÐ¿ÐºÐ¸: <b id="buyCommission">â</b>%<br>ÐÐ¾Ð¼Ð¸ÑÑÐ¸Ñ Ð¿ÑÐ¾Ð´Ð°Ð¶Ð¸: <b id="sellCommission">â</b>%<br>ÐÐ°Ð»Ð¾Ð³: <b id="tax">â</b>%<br>Ð¢Ð°Ð¹Ð¼ÑÑÐµÐ¹Ð¼: <b id="interval">â</b><br>ÐÑÑÐ¾ÑÐ¸Ñ: <b id="historyDays">â</b> Ð´Ð½ÐµÐ¹<br>Ð¡ÑÑÐ°ÑÐµÐ³Ð¸Ð¹: <b id="strategiesCount">â</b><br>ÐÑÑÐ¾Ð´ Ð¸Ð· ÑÐ´ÐµÐ»ÐºÐ¸: <b id="exitRule">â</b><br><button onclick="loadData()">ð ÐÐ±Ð½Ð¾Ð²Ð¸ÑÑ ÑÐµÐ¹ÑÐ°Ñ</button></div></div>
<script>
function money(v){return Number(v||0).toLocaleString('ru-RU',{minimumFractionDigits:2,maximumFractionDigits:2})}function signalClass(s){return s==='LONG'?'long':s==='SHORT'?'short':'none'}
function renderInstrumentCard(item){const signal=item.strategy.signal;const stats=item.statistics||{};let open='ÐÐµÑ';if(item.open_position)open=item.open_position.direction+' Ð¾Ñ '+money(item.open_position.entry_price);return `<div class="card"><h2>${item.emoji} ${item.title}</h2><span class="status ${item.status==='OK'?'':'error'}">${item.status}</span><div class="info">${item.message||''}</div><div class="info">Ð¢Ð¸ÐºÐµÑ: <b>${item.ticker}</b><br>UID: <b>${item.uid}</b><br>4H-ÑÐ²ÐµÑÐµÐ¹: <b>${item.candles}</b></div><div class="signal ${signalClass(signal)}">${signal}</div><div class="info">${item.strategy.description||''}</div><div class="info selected"><b>ð¤ ÐÑÐ±ÑÐ°Ð½Ð° ÑÑÑÐ°ÑÐµÐ³Ð¸Ñ: ${item.selected_strategy}</b><br>${item.selection_reason||''}<br>ÐÐ°ÐºÑÑÑÑÑ ÑÐ´ÐµÐ»Ð¾Ðº: <b>${stats.total||0}</b> Â· ÐÑÐ¾ÑÐ¾Ð´Ð¸Ð¼Ð¾ÑÑÑ: <b>${stats.winrate||0}%</b><br>ÐÑÐ¸Ð±ÑÐ»ÑÐ½ÑÑ: <b>${stats.profitable||0}</b> Â· Ð£Ð±ÑÑÐ¾ÑÐ½ÑÑ: <b>${stats.losing||0}</b><br>Ð§Ð¸ÑÑÑÐ¹ ÑÐµÐ·ÑÐ»ÑÑÐ°Ñ: <b>${money(stats.net)} â½</b><br>ÐÑÐºÑÑÑÐ°Ñ Ð¿Ð¾Ð·Ð¸ÑÐ¸Ñ: <b>${open}</b></div><div class="strategy-box"><b>ð¬ ÐÑÐµ ÑÑÑÐ°ÑÐµÐ³Ð¸Ð¸ â Ð¿Ð¾ÑÐµÐ¼Ñ Ð²ÑÐ±ÑÐ°Ð½Ð° Ð¸Ð¼ÐµÐ½Ð½Ð¾ ÑÑÐ°</b>${(item.strategy_selection||[]).map((r,i)=>`<div class="strategy-row ${r.is_selected?'selected':''}"><span>${r.is_selected?'â­ ':''}${i+1}. ${r.name}</span><span>${r.statistics.total} ÑÐ´ÐµÐ»Ð¾Ðº</span><span>${r.statistics.winrate}%</span><span class="${r.statistics.net>=0?'positive':'negative'}">${money(r.statistics.net)} â½</span><span>DD ${money(r.drawdown)} â½</span><span>${r.reason}</span></div>`).join('')}</div></div>`}
function renderFutures(data){document.getElementById('futures').innerHTML=data.futures.map(renderInstrumentCard).join('')}function renderShares(data){document.getElementById('shares').innerHTML=data.shares.map(renderInstrumentCard).join('')}
function renderStatistics(s){document.getElementById('statistics').innerHTML=`<div class="stat"><div class="stat-title">ÐÑÐµÐ³Ð¾ ÑÐ´ÐµÐ»Ð¾Ðº</div><div class="stat-value">${s.total}</div></div><div class="stat"><div class="stat-title">ÐÑÐ¸Ð±ÑÐ»ÑÐ½ÑÑ</div><div class="stat-value">${s.profitable}</div></div><div class="stat"><div class="stat-title">Ð£Ð±ÑÑÐ¾ÑÐ½ÑÑ</div><div class="stat-value">${s.losing}</div></div><div class="stat"><div class="stat-title">ÐÑÐ¾ÑÐ¾Ð´Ð¸Ð¼Ð¾ÑÑÑ</div><div class="stat-value">${s.winrate}%</div></div><div class="stat"><div class="stat-title">ÐÐ¾ ÑÐ°ÑÑÐ¾Ð´Ð¾Ð²</div><div class="stat-value">${money(s.gross)} â½</div></div><div class="stat"><div class="stat-title">ÐÐ¾Ð¼Ð¸ÑÑÐ¸Ð¸</div><div class="stat-value">${money(s.commission)} â½</div></div><div class="stat"><div class="stat-title">ÐÐ°Ð»Ð¾Ð³</div><div class="stat-value">${money(s.tax)} â½</div></div><div class="stat"><div class="stat-title">Ð§ÐÐ¡Ð¢Ð«Ð Ð ÐÐÐ£ÐÐ¬Ð¢ÐÐ¢</div><div class="stat-value ${s.net>=0?'positive':'negative'}">${money(s.net)} â½</div></div>`}
function renderHistory(data){let all=[];[...data.futures,...data.shares].forEach(x=>all=all.concat(x.history||[]));all.sort((a,b)=>new Date(b.exit_time)-new Date(a.exit_time));const c=document.getElementById('history');if(!all.length){c.innerHTML='ÐÐ¾ÐºÐ° Ð·Ð°ÐºÑÑÑÑÑ ÑÐ´ÐµÐ»Ð¾Ðº Ð½ÐµÑ.';return}let h='<table><thead><tr><th>ÐÐ½ÑÑÑÑÐ¼ÐµÐ½Ñ</th><th>ÐÐ°Ð¿ÑÐ°Ð²Ð»ÐµÐ½Ð¸Ðµ</th><th>ÐÑÐ¾Ð´</th><th>ÐÑÑÐ¾Ð´</th><th>Ð¦ÐµÐ½Ð° Ð²ÑÐ¾Ð´Ð°</th><th>Ð¦ÐµÐ½Ð° Ð²ÑÑÐ¾Ð´Ð°</th><th>Ð ÐµÐ·ÑÐ»ÑÑÐ°Ñ</th><th>ÐÐ¾Ð¼Ð¸ÑÑÐ¸Ñ</th><th>ÐÐ°Ð»Ð¾Ð³</th><th>Ð§Ð¸ÑÑÑÐ¹ ÑÐµÐ·ÑÐ»ÑÑÐ°Ñ</th></tr></thead><tbody>';all.slice(0,100).forEach(t=>{const n=Number(t.net_result||0);const commission=Number(t.buy_commission||0)+Number(t.sell_commission||0);h+=`<tr><td>${t.title}</td><td>${t.direction}</td><td>${t.entry_time}</td><td>${t.exit_time}</td><td>${t.entry_price}</td><td>${t.exit_price}</td><td>${money(t.gross_result)} â½</td><td>${money(commission)} â½</td><td>${money(t.tax)} â½</td><td class="${n>=0?'positive':'negative'}">${money(n)} â½</td></tr>`});c.innerHTML=h+'</tbody></table>'}
async function loadData(){try{const response=await fetch('/api/status');const data=await response.json();if(data.error){console.error(data.error);return}renderFutures(data);renderShares(data);renderStatistics(data.statistics);renderHistory(data);document.getElementById('updated').textContent='ÐÐ±Ð½Ð¾Ð²Ð»ÐµÐ½Ð¾: '+new Date(data.updated).toLocaleString('ru-RU');document.getElementById('positionSize').textContent=money(data.settings.position_size);document.getElementById('buyCommission').textContent=data.settings.buy_commission;document.getElementById('sellCommission').textContent=data.settings.sell_commission;document.getElementById('tax').textContent=data.settings.tax;document.getElementById('interval').textContent=data.settings.candle_interval;document.getElementById('historyDays').textContent=data.settings.history_days;document.getElementById('strategiesCount').textContent=data.settings.strategies_count;document.getElementById('exitRule').textContent=data.settings.exit_rule}catch(e){console.error('ÐÑÐ¸Ð±ÐºÐ° Ð·Ð°Ð³ÑÑÐ·ÐºÐ¸:',e)}}loadData();setInterval(loadData,60000);
</script></body></html>
"""


@app.route("/")
def index():
    return render_template_string(HTML)


def background_monitor():
    while True:
        try:
            data = collect_data()
            log.info("MARKUS TRADE | Ð¾Ð±Ð½Ð¾Ð²Ð»ÐµÐ½Ð¸Ðµ Ð´Ð°Ð½Ð½ÑÑ")
            for item in data["futures"] + data["shares"]:
                log.info("%s | ticker=%s | candles=%s | strategy=%s | signal=%s", item["title"], item["ticker"], item["candles"], item["selected_strategy"], item["strategy"]["signal"])
            stats = data["statistics"]
            log.info("Ð¡Ð¢ÐÐ¢ÐÐ¡Ð¢ÐÐÐ | ÑÐ´ÐµÐ»Ð¾Ðº=%s | winrate=%s%% | ÑÐ¸ÑÑÑÐ¹=%s â½", stats["total"], stats["winrate"], stats["net"])
        except Exception as exc:
            log.exception("ÐÑÐ¸Ð±ÐºÐ° ÑÐ¾Ð½Ð¾Ð²Ð¾Ð³Ð¾ Ð¼Ð¾Ð½Ð¸ÑÐ¾ÑÐ¸Ð½Ð³Ð°: %s", exc)
        time.sleep(UPDATE_SECONDS)


if __name__ == "__main__":
    if not get_token():
        log.error("ÐÐÐÐÐÐÐÐ: API-ÑÐ¾ÐºÐµÐ½ Ð½Ðµ Ð½Ð°Ð¹Ð´ÐµÐ½!")
    else:
        log.info("API-ÑÐ¾ÐºÐµÐ½ Ð½Ð°Ð¹Ð´ÐµÐ½.")
    threading.Thread(target=background_monitor, daemon=True).start()
    port = int(os.environ.get("PORT", "5000"))
    log.info("MARKUS TRADE Ð·Ð°Ð¿ÑÑÐºÐ°ÐµÑÑÑ Ð½Ð° Ð¿Ð¾ÑÑÑ %s", port)
    app.run(host="0.0.0.0", port=port, debug=False)
