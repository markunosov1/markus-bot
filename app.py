# -*- coding: utf-8 -*-
import os
import sys
import io
import json
import time
import logging
import threading
import warnings
import uuid
from datetime import datetime, timedelta, timezone

import requests
import urllib3
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, jsonify, render_template_string, request


# ============================================================
# UTF-8 ДЛЯ STDOUT/STDERR
# ============================================================
try:
    if sys.stdout.encoding is None or sys.stdout.encoding.lower() != "utf-8":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    if sys.stderr.encoding is None or sys.stderr.encoding.lower() != "utf-8":
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
except Exception:
    pass


# ============================================================
# КОНСТАНТЫ
# ============================================================
APP_NAME = "Markus Trade"
API_BASE = "https://invest-public-api.tbank.ru/rest"

FIND_INSTRUMENT_URL = API_BASE + "/tinkoff.public.invest.api.contract.v1.InstrumentsService/FindInstrument"
FUTURES_URL = API_BASE + "/tinkoff.public.invest.api.contract.v1.InstrumentsService/Futures"
SHARES_URL = API_BASE + "/tinkoff.public.invest.api.contract.v1.InstrumentsService/Shares"
CANDLES_URL = API_BASE + "/tinkoff.public.invest.api.contract.v1.MarketDataService/GetCandles"

REQUEST_TIMEOUT = 30

CANDLE_INTERVAL_DEFAULT = "CANDLE_INTERVAL_4_HOUR"
HISTORY_DAYS_DEFAULT = 60

CANDLE_INTERVALS = {
    "CANDLE_INTERVAL_5_MIN": "5 минут",
    "CANDLE_INTERVAL_15_MIN": "15 минут",
    "CANDLE_INTERVAL_HOUR": "1 час",
    "CANDLE_INTERVAL_4_HOUR": "4 часа",
    "CANDLE_INTERVAL_DAY": "1 день",
}

UPDATE_SECONDS = 300
POSITION_SIZE_RUBLES = 100000.0
BUY_COMMISSION_PERCENT = 0.10
SELL_COMMISSION_PERCENT = 0.10
TAX_PERCENT = 13.0
HISTORY_FILE = "trade_history.json"
SETTINGS_FILE = "settings.json"
MIN_BACKTEST_TRADES = 3

DEFAULT_SETTINGS = {
    "candle_interval": CANDLE_INTERVAL_DEFAULT,
    "history_days": HISTORY_DAYS_DEFAULT,
    "use_stop_loss": True,
    "use_take_profit": True,
    "use_breakeven": True,
    "sl_atr_mult": 2.0,
    "tp_atr_mult": 4.0,
    "breakeven_trigger_atr": 2.0,
}


# ============================================================
# WARNINGS И ЛОГИРОВАНИЕ
# ============================================================
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
warnings.filterwarnings("ignore")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("MARKUS_TRADE")


# ============================================================
# FLASK
# ============================================================
app = Flask(__name__)
try:
    app.json.ensure_ascii = False
except Exception:
    try:
        app.config["JSON_AS_ASCII"] = False
    except Exception:
        pass


# ============================================================
# БАЗА ДАННЫХ (PostgreSQL / Neon)
# ============================================================
DATABASE_URL = os.environ.get("DATABASE_URL")


def get_db_connection():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL не задан в переменных окружения.")
    return psycopg2.connect(DATABASE_URL)


def init_db():
    """Создаёт таблицу для настроек, если её нет."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    data JSONB NOT NULL
                );
            """)
            conn.commit()
    log.info("Таблица settings готова.")

# ============================================================
# РАБОТА С API
# ============================================================
def get_token():
    for name in ("TINKOFF_TOKEN", "TINVEST_TOKEN", "T_BANK_TOKEN", "API_TOKEN", "TOKEN"):
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return None


def api_post(url, payload):
    token = get_token()
    if not token:
        raise RuntimeError("API-токен не найден. Проверь переменную TINKOFF_TOKEN.")

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
        raise RuntimeError("T-Bank вернул ответ, который не удалось прочитать как JSON.") from exc


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


# ============================================================
# ФЬЮЧЕРСЫ
# ============================================================
def get_all_futures():
    for status in ("INSTRUMENT_STATUS_BASE", "INSTRUMENT_STATUS_ALL"):
        try:
            data = api_post(FUTURES_URL, {"instrumentStatus": status})
            futures = data.get("futures", [])
            if isinstance(futures, list):
                return futures
        except Exception as exc:
            log.warning("Ошибка Futures (%s): %s", status, exc)
    return []


def matches_future(future, prefix):
    text = " ".join([
        get_string(future, "ticker").upper(),
        get_string(future, "name").upper(),
        get_string(future, "basicAsset").upper(),
    ])
    keywords = {
        "CR": ["CR", "CNY", "YUAN", "CNH", "ЮАН", "КИТАЙ"],
        "GD": ["GD", "GOLD", "ЗОЛОТ"],
        "BR": ["BR", "BRENT", "НЕФТ"],
    }.get(prefix.upper(), [prefix.upper()])
    return any(word in text for word in keywords)


def find_active_future(prefix):
    queries = {
        "CR": ["CR", "CNY", "юань", "CNY/RUB"],
        "GD": ["GD", "GOLD", "золото"],
        "BR": ["BR", "BRENT", "нефть"],
    }.get(prefix, [prefix])

    candidates = []
    now = datetime.now(timezone.utc)

    def add_candidate(item):
        uid = item.get("instrumentUid") or item.get("uid")
        if not uid:
            return
        last_trade = parse_date(item.get("lastTradeDate"))
        first_trade = parse_date(item.get("firstTradeDate"))
        if first_trade and now < first_trade:
            return
        if last_trade and now > last_trade:
            return
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

    for item in get_all_futures():
        if isinstance(item, dict) and matches_future(item, prefix):
            add_candidate(item)

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
            instruments = data.get("instruments", [])
            if not isinstance(instruments, list):
                continue
            for item in instruments:
                if isinstance(item, dict) and matches_future(item, prefix):
                    add_candidate(item)

    unique = {x["instrument_uid"]: x for x in candidates}
    candidates = list(unique.values())
    if not candidates:
        return None

    mature = [c for c in candidates
              if c.get("first_trade") and (now - c["first_trade"]).days >= 60]
    if mature:
        candidates = mature

    candidates.sort(key=lambda x: x.get("last_trade") or datetime.max.replace(tzinfo=timezone.utc))
    return candidates[0]
# ============================================================
# АКЦИИ
# ============================================================
STOCKS = [
    {"code": "SBER", "title": "Сбербанк", "emoji": "🏦", "queries": ["SBER", "Сбербанк"]},
    {"code": "ROSN", "title": "Роснефть", "emoji": "🛢️", "queries": ["ROSN", "Роснефть"]},
    {"code": "GMKN", "title": "Норникель", "emoji": "⛏️", "queries": ["GMKN", "NORNICKEL", "Норникель"]},
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
            log.warning("FindInstrument акции %s: %s", query, exc)
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


# ============================================================
# СВЕЧИ
# ============================================================
def get_candles(instrument_uid, settings=None):
    if settings is None:
        settings = load_settings()
    now = datetime.now(timezone.utc)
    hours = int(settings.get("history_days", HISTORY_DAYS_DEFAULT)) * 24
    start = now - timedelta(hours=hours)
    interval = settings.get("candle_interval", CANDLE_INTERVAL_DEFAULT)
    if interval not in CANDLE_INTERVALS:
        interval = CANDLE_INTERVAL_DEFAULT

    data = api_post(CANDLES_URL, {
        "from": start.isoformat(),
        "to": now.isoformat(),
        "interval": interval,
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


# ============================================================
# ИНДИКАТОРЫ
# ============================================================
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


def atr(candles, period=14):
    if len(candles) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        high = candles[i]["high"]
        low = candles[i]["low"]
        prev_close = candles[i - 1]["close"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    if not trs:
        return 0.0
    return sum(trs[-period:]) / period
# ============================================================
# СТРАТЕГИИ
# ============================================================
def no_signal(description="Сигнал не сформирован"):
    return {"signal": "Нет сигналов", "direction": "---", "description": description}


def bollinger_strategy(candles):
    if len(candles) < 20:
        return no_signal("Недостаточно свечей для Bollinger")
    closes = [x["close"] for x in candles[-20:]]
    mid = sum(closes) / 20
    variance = sum((x - mid) ** 2 for x in closes) / 20
    std = variance ** 0.5
    upper = mid + 2 * std
    lower = mid - 2 * std
    last = closes[-1]
    prev = closes[-2]
    if prev <= lower and last > prev:
        return {"signal": "LONG", "direction": "Вверх",
                "description": f"Цена отскочила от нижней полосы Bollinger ({lower:.2f})"}
    if prev >= upper and last < prev:
        return {"signal": "SHORT", "direction": "Вниз",
                "description": f"Цена развернулась от верхней полосы Bollinger ({upper:.2f})"}
    return no_signal(f"Цена внутри диапазона Bollinger: {lower:.2f}–{upper:.2f}")


def user_strategy(candles):
    if len(candles) < 8:
        return no_signal("Недостаточно свечей для авторской стратегии")
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
        return {"signal": "SHORT", "direction": "Вниз",
                "description": "Авторская стратегия: растущие максимумы → подтверждённый разворот вниз"}
    if long_pattern:
        return {"signal": "LONG", "direction": "Вверх",
                "description": "Авторская стратегия: снижающиеся минимумы → подтверждённый разворот вверх"}
    return no_signal("Нет полного совпадения условий авторской стратегии")


def ema_trend_strategy(candles):
    if len(candles) < 30:
        return no_signal("Недостаточно свечей для EMA")
    closes = [x["close"] for x in candles]
    e9, e21 = ema(closes, 9), ema(closes, 21)
    if e9 is None or e21 is None:
        return no_signal("EMA ещё не рассчитаны")
    if e9 > e21 and closes[-1] > e9 and closes[-1] > closes[-2]:
        return {"signal": "LONG", "direction": "Вверх",
                "description": "EMA 9 выше EMA 21, цена выше EMA 9 и растёт"}
    if e9 < e21 and closes[-1] < e9 and closes[-1] < closes[-2]:
        return {"signal": "SHORT", "direction": "Вниз",
                "description": "EMA 9 ниже EMA 21, цена ниже EMA 9 и снижается"}
    return no_signal("Нет подтверждения тренда EMA")


def breakout_strategy(candles):
    if len(candles) < 21:
        return no_signal("Недостаточно свечей для Breakout")
    prev = candles[-21:-1]
    last = candles[-1]
    high = max(x["high"] for x in prev)
    low = min(x["low"] for x in prev)
    if last["close"] > high:
        return {"signal": "LONG", "direction": "Вверх",
                "description": f"Закрытие пробило максимум 20 предыдущих свечей ({high:.2f})"}
    if last["close"] < low:
        return {"signal": "SHORT", "direction": "Вниз",
                "description": f"Закрытие пробило минимум 20 предыдущих свечей ({low:.2f})"}
    return no_signal("Пробоя 20-свечного диапазона нет")


def rsi_strategy(candles):
    if len(candles) < 16:
        return no_signal("Недостаточно свечей для RSI")
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
        return {"signal": "LONG", "direction": "Вверх",
                "description": f"RSI перепродан ({rsi:.1f}) и цена разворачивается вверх"}
    if rsi > 70 and closes[-1] < closes[-2]:
        return {"signal": "SHORT", "direction": "Вниз",
                "description": f"RSI перекуплен ({rsi:.1f}) и цена разворачивается вниз"}
    return no_signal(f"RSI сейчас {rsi:.1f}; условия входа не выполнены")


def macd_strategy(candles):
    if len(candles) < 36:
        return no_signal("Недостаточно свечей для MACD")
    closes = [x["close"] for x in candles]
    fast, slow = ema(closes, 12), ema(closes, 26)
    prev_fast, prev_slow = ema(closes[:-1], 12), ema(closes[:-1], 26)
    if None in (fast, slow, prev_fast, prev_slow):
        return no_signal("MACD ещё не рассчитан")
    if prev_fast <= prev_slow and fast > slow:
        return {"signal": "LONG", "direction": "Вверх",
                "description": "EMA 12 пересекла EMA 26 снизу вверх"}
    if prev_fast >= prev_slow and fast < slow:
        return {"signal": "SHORT", "direction": "Вниз",
                "description": "EMA 12 пересекла EMA 26 сверху вниз"}
    return no_signal("Пересечения MACD на последней свече нет")


def hammer_strategy(candles):
    if len(candles) < 5:
        return no_signal("Недостаточно свечей для Hammer")
    last = candles[-1]
    body = abs(last["close"] - last["open"])
    lower_wick = min(last["open"], last["close"]) - last["low"]
    upper_wick = last["high"] - max(last["open"], last["close"])
    total_range = last["high"] - last["low"]
    if total_range == 0 or body == 0:
        return no_signal("Нет чёткой структуры свечи")
    closes = [x["close"] for x in candles[-4:-1]]
    downtrend = closes[0] > closes[1] > closes[2]
    uptrend = closes[0] < closes[1] < closes[2]
    if downtrend and body < total_range * 0.35 and lower_wick >= body * 2 and upper_wick < body * 0.5:
        return {"signal": "LONG", "direction": "Вверх",
                "description": "Hammer: длинная нижняя тень после падения — покупатели откупили цену"}
    if uptrend and body < total_range * 0.35 and upper_wick >= body * 2 and lower_wick < body * 0.5:
        return {"signal": "SHORT", "direction": "Вниз",
                "description": "Inverted Hammer: длинная верхняя тень после роста — продавцы вернули цену"}
    return no_signal("Hammer/Inverted Hammer не сформирован")


def engulfing_strategy(candles):
    if len(candles) < 10:
        return no_signal("Недостаточно свечей для Engulfing")
    prev = candles[-2]
    last = candles[-1]
    prev_body = abs(prev["close"] - prev["open"])
    last_body = abs(last["close"] - last["open"])
    if prev_body == 0 or last_body == 0:
        return no_signal("Нет чёткого тела свечи")
    prev_bullish = prev["close"] > prev["open"]
    last_bullish = last["close"] > last["open"]
    if (not prev_bullish and last_bullish
            and last_body > prev_body * 1.2
            and last["open"] <= prev["close"] and last["close"] >= prev["open"]):
        return {"signal": "LONG", "direction": "Вверх",
                "description": "Bullish Engulfing: зелёная свеча полностью поглотила предыдущую красную"}
    if (prev_bullish and not last_bullish
            and last_body > prev_body * 1.2
            and last["open"] >= prev["close"] and last["close"] <= prev["open"]):
        return {"signal": "SHORT", "direction": "Вниз",
                "description": "Bearish Engulfing: красная свеча полностью поглотила предыдущую зелёную"}
    return no_signal("Engulfing не сформирован")


def double_pattern_strategy(candles):
    if len(candles) < 40:
        return no_signal("Недостаточно свечей для Double Top/Bottom")
    window = candles[-40:]
    highs = [x["high"] for x in window]
    lows = [x["low"] for x in window]
    closes = [x["close"] for x in window]
    first_half_high = max(highs[:20])
    second_half_high = max(highs[20:])
    first_half_low = min(lows[:20])
    second_half_low = min(lows[20:])
    tolerance = 0.015
    if (abs(first_half_high - second_half_high) / first_half_high < tolerance
            and closes[-1] < min(highs[10:30]) * 0.99):
        return {"signal": "SHORT", "direction": "Вниз",
                "description": f"Double Top: два максимума около {second_half_high:.2f} и пробой вниз"}
    if (abs(first_half_low - second_half_low) / first_half_low < tolerance
            and closes[-1] > max(lows[10:30]) * 1.01):
        return {"signal": "LONG", "direction": "Вверх",
                "description": f"Double Bottom: два минимума около {second_half_low:.2f} и пробой вверх"}
    return no_signal("Double Top/Bottom не сформирован")


def supertrend_strategy(candles):
    if len(candles) < 15:
        return no_signal("Недостаточно свечей для SuperTrend")
    period = 10
    multiplier = 2.5
    window = candles[-period:]
    trs = []
    for i in range(1, len(window)):
        high = window[i]["high"]
        low = window[i]["low"]
        prev_close = window[i - 1]["close"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    if not trs:
        return no_signal("ATR не рассчитан")
    atr_val = sum(trs) / len(trs)
    last = candles[-1]
    mid = (last["high"] + last["low"]) / 2
    upper_band = mid + multiplier * atr_val
    lower_band = mid - multiplier * atr_val
    prev = candles[-2]
    prev_mid = (prev["high"] + prev["low"]) / 2
    prev_upper = prev_mid + multiplier * atr_val
    prev_lower = prev_mid - multiplier * atr_val
    if prev["close"] < prev_lower and last["close"] > lower_band:
        return {"signal": "LONG", "direction": "Вверх",
                "description": f"SuperTrend: цена пробила нижнюю полосу (ATR={atr_val:.2f}), тренд вверх"}
    if prev["close"] > prev_upper and last["close"] < upper_band:
        return {"signal": "SHORT", "direction": "Вниз",
                "description": f"SuperTrend: цена пробила верхнюю полосу (ATR={atr_val:.2f}), тренд вниз"}
    if last["close"] > lower_band and last["close"] < upper_band:
        return no_signal(f"SuperTrend в зоне неопределённости: {lower_band:.2f}–{upper_band:.2f}")
    return no_signal("SuperTrend без чёткого сигнала")


STRATEGIES = [
    {"name": "Твоя стратегия", "key": "user", "fn": user_strategy, "min_bars": 8},
    {"name": "EMA Trend", "key": "ema", "fn": ema_trend_strategy, "min_bars": 30},
    {"name": "Breakout", "key": "breakout", "fn": breakout_strategy, "min_bars": 21},
    {"name": "RSI Reversal", "key": "rsi", "fn": rsi_strategy, "min_bars": 16},
    {"name": "MACD", "key": "macd", "fn": macd_strategy, "min_bars": 36},
    {"name": "Bollinger", "key": "bollinger", "fn": bollinger_strategy, "min_bars": 20},
    {"name": "Hammer", "key": "hammer", "fn": hammer_strategy, "min_bars": 5},
    {"name": "Engulfing", "key": "engulfing", "fn": engulfing_strategy, "min_bars": 10},
    {"name": "Double Top/Bottom", "key": "double", "fn": double_pattern_strategy, "min_bars": 40},
    {"name": "SuperTrend", "key": "supertrend", "fn": supertrend_strategy, "min_bars": 15},
]
# ============================================================
# РАСЧЁТ РЕЗУЛЬТАТОВ
# ============================================================
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
        return {"total": 0, "profitable": 0, "losing": 0, "winrate": 0,
                "gross": 0, "commission": 0, "tax": 0, "net": 0}
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


# ============================================================
# НАСТРОЙКИ (PostgreSQL)
# ============================================================
def load_settings():
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT data FROM settings WHERE id = 1;")
                row = cur.fetchone()
                if row and row["data"]:
                    result = dict(DEFAULT_SETTINGS)
                    result.update(row["data"])
                    return result
                else:
                    save_settings(DEFAULT_SETTINGS)
                    return dict(DEFAULT_SETTINGS)
    except Exception as exc:
        log.warning("Ошибка чтения настроек из БД: %s", exc)
        return dict(DEFAULT_SETTINGS)


def save_settings(settings):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO settings (id, data) VALUES (1, %s)
                    ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data;
                    """,
                    (json.dumps(settings, ensure_ascii=False),),
                )
                conn.commit()
    except Exception as exc:
        log.error("Ошибка сохранения настроек в БД: %s", exc)


def load_history():
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception as exc:
        log.warning("Ошибка чтения истории: %s", exc)
        return []


def save_history(history):
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        log.error("Ошибка сохранения истории: %s", exc)


# ============================================================
# ОТКРЫТИЕ ПОЗИЦИИ С РИСК-МЕНЕДЖМЕНТОМ
# ============================================================
def _open_position(direction, price, candle_time, window, use_sl, use_tp, sl_mult, tp_mult):
    atr_val = atr(window, 14) if (use_sl or use_tp) else 0.0
    stop_loss = None
    take_profit = None
    if atr_val > 0:
        if direction == "LONG":
            if use_sl:
                stop_loss = price - sl_mult * atr_val
            if use_tp:
                take_profit = price + tp_mult * atr_val
        else:
            if use_sl:
                stop_loss = price + sl_mult * atr_val
            if use_tp:
                take_profit = price - tp_mult * atr_val
    return {
        "direction": direction,
        "entry_price": price,
        "entry_time": candle_time,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "atr_value": atr_val,
        "breakeven_moved": False,
    }


# ============================================================
# БЭКТЕСТ С РИСК-МЕНЕДЖМЕНТОМ
# ============================================================
def build_strategy_history(candles, instrument, title, strategy_fn, settings=None):
    if settings is None:
        settings = load_settings()
    min_bars = 8
    for s in STRATEGIES:
        if s["fn"] is strategy_fn:
            min_bars = s["min_bars"]
            break
    if len(candles) < min_bars:
        return [], None

    trades = []
    current_position = None
    use_sl = bool(settings.get("use_stop_loss", True))
    use_tp = bool(settings.get("use_take_profit", True))
    use_be = bool(settings.get("use_breakeven", True))
    sl_mult = float(settings.get("sl_atr_mult", 2.0))
    tp_mult = float(settings.get("tp_atr_mult", 4.0))
    be_trigger = float(settings.get("breakeven_trigger_atr", 2.0))

    for i in range(min_bars - 1, len(candles)):
        window = candles[: i + 1]
        analysis = strategy_fn(window)
        signal = analysis.get("signal", "Нет сигналов")
        candle = candles[i]
        price = candle["close"]
        candle_time = candle["time"]

        if current_position is not None:
            direction = current_position["direction"]
            entry = current_position["entry_price"]
            exit_price = None
            exit_reason = None

            if use_sl and current_position.get("stop_loss") is not None:
                sl = current_position["stop_loss"]
                if direction == "LONG" and candle["low"] <= sl:
                    exit_price = sl
                    exit_reason = "Стоп-лосс"
                elif direction == "SHORT" and candle["high"] >= sl:
                    exit_price = sl
                    exit_reason = "Стоп-лосс"

            if exit_price is None and use_tp and current_position.get("take_profit") is not None:
                tp = current_position["take_profit"]
                if direction == "LONG" and candle["high"] >= tp:
                    exit_price = tp
                    exit_reason = "Тейк-профит"
                elif direction == "SHORT" and candle["low"] <= tp:
                    exit_price = tp
                    exit_reason = "Тейк-профит"

            if use_be and exit_price is None and not current_position.get("breakeven_moved", False):
                atr_val = current_position.get("atr_value", 0.0)
                if atr_val > 0:
                    if direction == "LONG":
                        profit_dist = candle["close"] - entry
                    else:
                        profit_dist = entry - candle["close"]
                    if profit_dist >= be_trigger * atr_val:
                        current_position["stop_loss"] = entry
                        current_position["breakeven_moved"] = True

            if exit_price is None and signal in ("LONG", "SHORT") and signal != direction:
                exit_price = price
                exit_reason = "Противоположный сигнал"

            if exit_price is not None:
                result = calculate_trade_result(direction, entry, exit_price)
                if result is not None:
                    trades.append({
                        "id": len(trades) + 1,
                        "instrument": instrument,
                        "title": title,
                        "direction": direction,
                        "entry_time": current_position["entry_time"],
                        "exit_time": candle_time,
                        "entry_price": round(entry, 8),
                        "exit_price": round(exit_price, 8),
                        "exit_reason": exit_reason,
                        "exit_signal": signal,
                        **result,
                    })
                current_position = None
                if signal in ("LONG", "SHORT"):
                    current_position = _open_position(
                        signal, price, candle_time, window,
                        use_sl, use_tp, sl_mult, tp_mult
                    )
                continue

        if current_position is None and signal in ("LONG", "SHORT"):
            current_position = _open_position(
                signal, price, candle_time, window,
                use_sl, use_tp, sl_mult, tp_mult
            )

    return trades, current_position


def evaluate_all_strategies(candles, instrument, title, settings=None):
    if settings is None:
        settings = load_settings()
    rows = []
    for strategy in STRATEGIES:
        trades, open_position = build_strategy_history(
            candles, instrument, title, strategy["fn"], settings
        )
        stats = calculate_statistics(trades)
        drawdown = calculate_max_drawdown(trades)
        last_analysis = strategy["fn"](candles)

        if stats["total"] >= MIN_BACKTEST_TRADES:
            score = (stats["net"] / (1.0 + drawdown)) * 100 + stats["winrate"] * 2
            eligible = True
            reason = "Есть минимум 3 закрытые сделки; учитываются чистый результат, проходимость и просадка."
        else:
            score = -1e12 + stats["total"] * 1000 + stats["net"]
            eligible = False
            reason = f"Недостаточно закрытых сделок: {stats['total']} из {MIN_BACKTEST_TRADES}."

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
            f"Выбрана по историческому тесту на {len(candles)} свечах: "
            f"{best['statistics']['total']} сделок, проходимость {best['statistics']['winrate']}%, "
            f"чистый результат {best['statistics']['net']:.2f} ₽, просадка {best['drawdown']:.2f} ₽."
        )
    else:
        best = max(rows, key=lambda x: (x["statistics"]["total"], x["statistics"]["net"]))
        selection_reason = (
            "Ни одна стратегия не набрала минимум 3 закрытые сделки. "
            f"Временно выбрана стратегия с наибольшим количеством сделок ({best['statistics']['total']})."
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


# ============================================================
# АНАЛИЗ ИНСТРУМЕНТА
# ============================================================
def base_result(kind, code, title, emoji):
    return {
        "type": kind, "prefix": code, "title": title, "emoji": emoji,
        "status": "Ошибка", "message": "", "ticker": "---",
        "uid": "---", "candles": 0,
        "strategy": {"signal": "Нет сигналов", "direction": "---", "description": ""},
        "selected_strategy": "---", "selection_reason": "---", "strategy_selection": [],
        "history": [], "statistics": calculate_statistics([]), "open_position": None,
    }


def analyze_instrument(result, instrument, instrument_code, title, settings):
    result["ticker"] = instrument["ticker"]
    result["uid"] = instrument["instrument_uid"]
    candles = normalize_candles(get_candles(instrument["instrument_uid"], settings))
    result["candles"] = len(candles)
    if not candles:
        result["message"] = "Свечей 0 — API не вернул историю для этого инструмента."
        return result

    rankings, best, selection_reason = evaluate_all_strategies(
        candles, instrument_code, title, settings
    )
    result["strategy"] = strategy_signal_from_best(candles, best)
    result["selected_strategy"] = best["name"]
    result["selection_reason"] = selection_reason
    result["strategy_selection"] = rankings
    result["status"] = "OK"
    result["message"] = f"Загружено {len(candles)} свечей. Все {len(STRATEGIES)} стратегий протестированы."
    result["history"] = best["trades"]
    result["open_position"] = best["open_position"]
    result["statistics"] = best["statistics"]
    return result


def get_future_status(prefix, title, emoji, settings):
    result = base_result("future", prefix, title, emoji)
    try:
        future = find_active_future(prefix)
        if not future:
            result["message"] = "Актуальный контракт не найден."
            return result
        return analyze_instrument(result, future, prefix, title, settings)
    except Exception as exc:
        log.exception("Ошибка %s", title)
        result["message"] = str(exc)
        return result


def get_share_status(stock, settings):
    result = base_result("share", stock["code"], stock["title"], stock["emoji"])
    try:
        share = find_share(stock)
        if not share:
            result["message"] = "Акция не найдена."
            return result
        return analyze_instrument(result, share, stock["code"], stock["title"], settings)
    except Exception as exc:
        log.exception("Ошибка акции %s", stock["title"])
        result["message"] = str(exc)
        return result
# ============================================================
# СБОР ДАННЫХ
# ============================================================
def collect_data():
    settings = load_settings()
    interval_name = CANDLE_INTERVALS.get(
        settings.get("candle_interval"), "4 часа"
    )

    futures = [
        get_future_status("CR", "Юань", "¥", settings),
        get_future_status("GD", "Золото", "🥇", settings),
        get_future_status("BR", "Нефть Brent", "🛢️", settings),
    ]
    shares = [get_share_status(stock, settings) for stock in STOCKS]
    instruments = futures + shares

    all_trades = []
    for item in instruments:
        all_trades.extend(item.get("history", []))
    total_statistics = calculate_statistics(all_trades)

    last_signal = {"title": "Нет сигналов", "signal": "---", "direction": "---", "description": ""}
    for item in instruments:
        signal = item["strategy"].get("signal")
        if signal in ("LONG", "SHORT"):
            last_signal = {
                "title": item["title"], "signal": signal,
                "direction": item["strategy"].get("direction", "---"),
                "description": item["strategy"].get("description", ""),
            }
            break

    existing_history = load_history()
    existing_keys = {
        (t.get("instrument"), t.get("entry_time"), t.get("exit_time"), t.get("direction"))
        for t in existing_history
    }
    for trade in all_trades:
        key = (trade.get("instrument"), trade.get("entry_time"),
               trade.get("exit_time"), trade.get("direction"))
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
        "risk_settings": settings,
        "settings": {
            "position_size": POSITION_SIZE_RUBLES,
            "buy_commission": BUY_COMMISSION_PERCENT,
            "sell_commission": SELL_COMMISSION_PERCENT,
            "tax": TAX_PERCENT,
            "candle_interval": interval_name,
            "history_days": settings.get("history_days", HISTORY_DAYS_DEFAULT),
            "exit_rule": "Стоп-лосс / Тейк-профит / Противоположный сигнал",
            "strategies_count": len(STRATEGIES),
        },
    }


# ============================================================
# API-РОУТЫ
# ============================================================
@app.route("/api/status")
def api_status():
    try:
        return jsonify(collect_data())
    except Exception as exc:
        log.exception("Ошибка /api/status")
        return jsonify({"error": str(exc)}), 500


@app.route("/api/history")
def api_history():
    history = load_history()
    return jsonify({"count": len(history), "history": history})


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "GET":
        return jsonify(load_settings())
    try:
        data = request.get_json(force=True) or {}
        current = load_settings()
        for key in DEFAULT_SETTINGS:
            if key in data:
                current[key] = data[key]
        if current.get("candle_interval") not in CANDLE_INTERVALS:
            current["candle_interval"] = CANDLE_INTERVAL_DEFAULT
        try:
            current["history_days"] = max(5, min(365, int(current.get("history_days", 60))))
        except Exception:
            current["history_days"] = 60
        save_settings(current)
        return jsonify({"ok": True, "settings": current})
    except Exception as exc:
        log.exception("Ошибка сохранения настроек")
        return jsonify({"ok": False, "error": str(exc)}), 500


# ============================================================
# ФОНОВЫЙ МОНИТОРИНГ
# ============================================================
def background_monitor():
    while True:
        try:
            data = collect_data()
            log.info("MARKUS TRADE | обновление данных")
            for item in data["futures"] + data["shares"]:
                log.info("%s | ticker=%s | candles=%s | strategy=%s | signal=%s",
                         item["title"], item["ticker"], item["candles"],
                         item["selected_strategy"], item["strategy"]["signal"])
            stats = data["statistics"]
            log.info("СТАТИСТИКА | сделок=%s | winrate=%s%% | чистый=%s ₽",
                     stats["total"], stats["winrate"], stats["net"])
        except Exception as exc:
            log.exception("Ошибка фонового мониторинга: %s", exc)
        time.sleep(UPDATE_SECONDS)


# ============================================================
# ИНИЦИАЛИЗАЦИЯ БД ПРИ СТАРТЕ (для gunicorn)
# ============================================================
try:
    init_db()
except Exception as _exc:
    log.error("Не удалось инициализировать БД при старте: %s", _exc)


# ============================================================
# HTML
# ============================================================
HTML = r"""
<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Markus Trade</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:linear-gradient(135deg,#07090d,#10141c);color:#fff;font-family:Arial,sans-serif;min-height:100vh}
.container{width:95%;max-width:1500px;margin:auto;padding:25px 0 50px}
.header{display:flex;justify-content:space-between;align-items:center;margin-bottom:25px}
.logo{font-size:28px;font-weight:800;letter-spacing:1px}
.logo span{color:#d7aa52}
.updated{color:#8c96a8;font-size:13px}
.section-title{margin:28px 0 14px;font-size:23px}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:18px}
.card{background:rgba(22,27,36,.95);border:1px solid rgba(255,255,255,.08);border-radius:18px;padding:20px;box-shadow:0 15px 50px rgba(0,0,0,.25)}
.card h2{margin-top:0;font-size:20px}
.status{display:inline-block;padding:6px 10px;border-radius:20px;font-size:12px;background:#193d2b;color:#66e29a}
.error{background:#442020;color:#ff8585}
.signal{margin-top:15px;padding:14px;border-radius:14px;background:#111720;font-size:18px;font-weight:bold}
.long{color:#52e58a}
.short{color:#ff6666}
.none{color:#9ca5b4}
.info{margin-top:12px;color:#aab3c2;font-size:13px;line-height:1.6}
.selected{border-left:3px solid #d7aa52;padding-left:9px}
.strategy-box{margin-top:15px;background:#0d1219;border-radius:12px;padding:12px;font-size:12px;overflow:auto}
.strategy-row{display:grid;grid-template-columns:1.4fr .7fr .6fr .9fr .8fr 1.5fr;gap:7px;padding:8px 0;border-bottom:1px solid rgba(255,255,255,.06);color:#b8c0cc;min-width:850px}
.strategy-row:last-child{border-bottom:0}
.positive{color:#52e58a;font-weight:bold}
.negative{color:#ff6666;font-weight:bold}
.statistics,.history{margin-top:25px}
.stats-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
.stat{background:#111720;padding:16px;border-radius:14px}
.stat-title{font-size:12px;color:#8f99aa;margin-bottom:7px}
.stat-value{font-size:20px;font-weight:bold}
.table-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;min-width:1000px}
th,td{padding:11px;border-bottom:1px solid rgba(255,255,255,.07);text-align:left;font-size:13px}
th{color:#9ca5b4;font-weight:normal}
.settings{margin-top:20px;color:#858fa0;font-size:13px;line-height:1.7}
button{margin-top:10px;margin-right:6px;border:none;border-radius:12px;padding:10px 16px;background:#d7aa52;color:#111;font-weight:bold;cursor:pointer}
input[type=number],select{padding:6px;border-radius:6px;background:#0d1219;color:#fff;border:1px solid rgba(255,255,255,.15)}
@media(max-width:1100px){.grid{grid-template-columns:1fr 1fr}}
@media(max-width:700px){.grid{grid-template-columns:1fr}.stats-grid{grid-template-columns:repeat(2,1fr)}.header{align-items:flex-start;gap:10px;flex-direction:column}}
</style></head><body><div class="container">
<div class="header"><div class="logo">MARKUS <span>TRADE</span></div><div class="updated" id="updated">Загрузка...</div></div>

<div class="card" id="riskCard">
<h2>⚙️ Управление бэктестом</h2>
<div class="info">
<b>Таймфрейм:</b><br>
<select id="intervalSelect" style="width:100%">
<option value="CANDLE_INTERVAL_5_MIN">5 минут</option>
<option value="CANDLE_INTERVAL_15_MIN">15 минут</option>
<option value="CANDLE_INTERVAL_HOUR">1 час</option>
<option value="CANDLE_INTERVAL_4_HOUR">4 часа</option>
<option value="CANDLE_INTERVAL_DAY">1 день</option>
</select>
<br><br>
<b>Глубина истории (дней):</b><br>
<input type="number" id="historyDaysInput" min="5" max="365" step="5" style="width:100%">
<br><br>
<b>🛡️ Риск-менеджмент</b><br><br>
<label><input type="checkbox" id="useSL"> Стоп-лосс</label>
<input type="number" id="slMult" step="0.1" min="0.5" style="width:70px"> × ATR<br>
<label><input type="checkbox" id="useTP"> Тейк-профит</label>
<input type="number" id="tpMult" step="0.1" min="0.5" style="width:70px"> × ATR<br>
<label><input type="checkbox" id="useBE"> Безубыток</label>
<input type="number" id="beTrig" step="0.1" min="0.1" style="width:70px"> × ATR<br><br>
<button onclick="saveRiskSettings()">💾 Сохранить</button>
<button onclick="loadData()">🔄 Пересчитать</button>
</div>
</div>

<div class="section-title">📊 ФЬЮЧЕРСЫ</div><div class="grid" id="futures"></div>
<div class="section-title">📈 АКЦИИ</div><div class="grid" id="shares"></div>

<div class="card statistics"><h2>📊 Общая статистика</h2><div class="stats-grid" id="statistics"></div></div>
<div class="card history"><h2>📜 История сделок</h2><div class="table-wrap" id="history"></div></div>

<div class="card settings"><h2>ℹ️ Параметры</h2>
Размер виртуальной позиции: <b id="positionSize">---</b> ₽<br>
Комиссия покупки: <b id="buyCommission">---</b>%<br>
Комиссия продажи: <b id="sellCommission">---</b>%<br>
Налог: <b id="tax">---</b>%<br>
Таймфрейм: <b id="interval">---</b><br>
История: <b id="historyDays">---</b> дней<br>
Стратегий: <b id="strategiesCount">---</b><br>
Выход из сделки: <b id="exitRule">---</b></div>

</div>

<script>
function money(v){return Number(v||0).toLocaleString('ru-RU',{minimumFractionDigits:2,maximumFractionDigits:2})}
function signalClass(s){return s==='LONG'?'long':s==='SHORT'?'short':'none'}

function renderInstrumentCard(item){
  const signal=item.strategy.signal;
  const stats=item.statistics||{};
  let open='Нет';
  if(item.open_position) open=item.open_position.direction+' от '+money(item.open_position.entry_price);
  return `<div class="card"><h2>${item.emoji} ${item.title}</h2><span class="status ${item.status==='OK'?'':'error'}">${item.status}</span><div class="info">${item.message||''}</div><div class="info">Тикер: <b>${item.ticker}</b><br>UID: <b>${item.uid}</b><br>Свечей: <b>${item.candles}</b></div><div class="signal ${signalClass(signal)}">${signal}</div><div class="info">${item.strategy.description||''}</div><div class="info selected"><b>🤖 Выбрана: ${item.selected_strategy}</b><br>${item.selection_reason||''}<br>Сделок: <b>${stats.total||0}</b> · Winrate: <b>${stats.winrate||0}%</b><br>Прибыльных: <b>${stats.profitable||0}</b> · Убыточных: <b>${stats.losing||0}</b><br>Чистый: <b>${money(stats.net)} ₽</b><br>Открытая позиция: <b>${open}</b></div><div class="strategy-box"><b>🔬 Все стратегии</b>${(item.strategy_selection||[]).map((r,i)=>`<div class="strategy-row ${r.is_selected?'selected':''}"><span>${r.is_selected?'⭐ ':''}${i+1}. ${r.name}</span><span>${r.statistics.total} сдел.</span><span>${r.statistics.winrate}%</span><span class="${r.statistics.net>=0?'positive':'negative'}">${money(r.statistics.net)} ₽</span><span>DD ${money(r.drawdown)} ₽</span><span>${r.reason}</span></div>`).join('')}</div></div>`;
}
function renderFutures(data){document.getElementById('futures').innerHTML=data.futures.map(renderInstrumentCard).join('')}
function renderShares(data){document.getElementById('shares').innerHTML=data.shares.map(renderInstrumentCard).join('')}
function renderStatistics(s){
  document.getElementById('statistics').innerHTML=`<div class="stat"><div class="stat-title">Всего сделок</div><div class="stat-value">${s.total}</div></div><div class="stat"><div class="stat-title">Прибыльных</div><div class="stat-value">${s.profitable}</div></div><div class="stat"><div class="stat-title">Убыточных</div><div class="stat-value">${s.losing}</div></div><div class="stat"><div class="stat-title">Winrate</div><div class="stat-value">${s.winrate}%</div></div><div class="stat"><div class="stat-title">До расходов</div><div class="stat-value">${money(s.gross)} ₽</div></div><div class="stat"><div class="stat-title">Комиссии</div><div class="stat-value">${money(s.commission)} ₽</div></div><div class="stat"><div class="stat-title">Налог</div><div class="stat-value">${money(s.tax)} ₽</div></div><div class="stat"><div class="stat-title">ЧИСТЫЙ РЕЗУЛЬТАТ</div><div class="stat-value ${s.net>=0?'positive':'negative'}">${money(s.net)} ₽</div></div>`;
}
function renderHistory(data){
  let all=[];
  [...data.futures,...data.shares].forEach(x=>all=all.concat(x.history||[]));
  all.sort((a,b)=>new Date(b.exit_time)-new Date(a.exit_time));
  const c=document.getElementById('history');
  if(!all.length){c.innerHTML='Пока закрытых сделок нет.';return}
  let h='<table><thead><tr><th>Инструмент</th><th>Напр.</th><th>Вход</th><th>Выход</th><th>Цена входа</th><th>Цена выхода</th><th>Причина</th><th>Результат</th><th>Комиссия</th><th>Налог</th><th>Чистый</th></tr></thead><tbody>';
  all.slice(0,100).forEach(t=>{
    const n=Number(t.net_result||0);
    const commission=Number(t.buy_commission||0)+Number(t.sell_commission||0);
    h+=`<tr><td>${t.title}</td><td>${t.direction}</td><td>${t.entry_time}</td><td>${t.exit_time}</td><td>${t.entry_price}</td><td>${t.exit_price}</td><td>${t.exit_reason||'---'}</td><td>${money(t.gross_result)} ₽</td><td>${money(commission)} ₽</td><td>${money(t.tax)} ₽</td><td class="${n>=0?'positive':'negative'}">${money(n)} ₽</td></tr>`;
  });
  c.innerHTML=h+'</tbody></table>';
}
function renderRiskSettings(s){
  const sel = document.getElementById('intervalSelect');
  if (sel && s.candle_interval) sel.value = s.candle_interval;
  const hd = document.getElementById('historyDaysInput');
  if (hd && s.history_days) hd.value = s.history_days;
  document.getElementById('useSL').checked = !!s.use_stop_loss;
  document.getElementById('slMult').value = s.sl_atr_mult;
  document.getElementById('useTP').checked = !!s.use_take_profit;
  document.getElementById('tpMult').value = s.tp_atr_mult;
  document.getElementById('useBE').checked = !!s.use_breakeven;
  document.getElementById('beTrig').value = s.breakeven_trigger_atr;
}
async function saveRiskSettings(){
  const payload = {
    candle_interval: document.getElementById('intervalSelect').value,
    history_days: parseInt(document.getElementById('historyDaysInput').value) || 60,
    use_stop_loss: document.getElementById('useSL').checked,
    sl_atr_mult: parseFloat(document.getElementById('slMult').value) || 2.0,
    use_take_profit: document.getElementById('useTP').checked,
    tp_atr_mult: parseFloat(document.getElementById('tpMult').value) || 4.0,
    use_breakeven: document.getElementById('useBE').checked,
    breakeven_trigger_atr: parseFloat(document.getElementById('beTrig').value) || 2.0,
  };
  const r = await fetch('/api/settings', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  });
  const res = await r.json();
  if (res.ok) {
    alert('Настройки сохранены. Пересчитываю...');
    loadData();
  } else {
    alert('Ошибка: ' + res.error);
  }
}
async function loadData(){
  try{
    const response=await fetch('/api/status');
    const data=await response.json();
    if(data.error){console.error(data.error);return}
    renderFutures(data);renderShares(data);renderStatistics(data.statistics);renderHistory(data);
    if (data.risk_settings) renderRiskSettings(data.risk_settings);
    document.getElementById('updated').textContent='Обновлено: '+new Date(data.updated).toLocaleString('ru-RU');
    document.getElementById('positionSize').textContent=money(data.settings.position_size);
    document.getElementById('buyCommission').textContent=data.settings.buy_commission;
    document.getElementById('sellCommission').textContent=data.settings.sell_commission;
    document.getElementById('tax').textContent=data.settings.tax;
    document.getElementById('interval').textContent=data.settings.candle_interval;
    document.getElementById('historyDays').textContent=data.settings.history_days;
    document.getElementById('strategiesCount').textContent=data.settings.strategies_count;
    document.getElementById('exitRule').textContent=data.settings.exit_rule;
  }catch(e){console.error('Ошибка загрузки:',e)}
}
loadData();setInterval(loadData,60000);
</script></body></html>
"""


@app.route("/")
def index():
    return render_template_string(HTML)


# ============================================================
# ЗАПУСК
# ============================================================
if __name__ == "__main__":
    if not get_token():
        log.error("ВНИМАНИЕ: API-токен не найден!")
    else:
        log.info("API-токен найден.")

    try:
        init_db()
    except Exception as exc:
        log.error("Ошибка инициализации БД: %s", exc)

    threading.Thread(target=background_monitor, daemon=True).start()
    port = int(os.environ.get("PORT", "5000"))
    log.info("MARKUS TRADE запускается на порту %s", port)
    app.run(host="0.0.0.0", port=port, debug=False)




