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

try:
    sys.stdout = io.TextIOWrapper(
        sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True
    )
    sys.stderr = io.TextIOWrapper(
        sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True
    )
except Exception:
    pass

os.environ.setdefault("PYTHONIOENCODING", "utf-8")

APP_NAME = "Markus Trade"
API_BASE = "https://invest-public-api.tbank.ru/rest"
FIND_INSTRUMENT_URL = API_BASE + "/tinkoff.public.invest.api.contract.v1.InstrumentsService/FindInstrument"
FUTURES_URL = API_BASE + "/tinkoff.public.invest.api.contract.v1.InstrumentsService/Futures"
SHARES_URL = API_BASE + "/tinkoff.public.invest.api.contract.v1.InstrumentsService/Shares"
CANDLES_URL = API_BASE + "/tinkoff.public.invest.api.contract.v1.MarketDataService/GetCandles"
ACCOUNTS_URL = API_BASE + "/tinkoff.public.invest.api.contract.v1.UsersService/GetAccounts"
POST_ORDER_URL = API_BASE + "/tinkoff.public.invest.api.contract.v1.OrdersService/PostOrder"
CANCEL_ORDER_URL = API_BASE + "/tinkoff.public.invest.api.contract.v1.OrdersService/CancelOrder"
GET_ORDERS_URL = API_BASE + "/tinkoff.public.invest.api.contract.v1.OrdersService/GetOrders"
GET_POSITIONS_URL = API_BASE + "/tinkoff.public.invest.api.contract.v1.OperationsService/GetPositions"
GET_PORTFOLIO_URL = API_BASE + "/tinkoff.public.invest.api.contract.v1.OperationsService/GetPortfolio"

REQUEST_TIMEOUT = 30
CANDLE_INTERVAL_DEFAULT = "CANDLE_INTERVAL_4_HOUR"
HISTORY_DAYS_DEFAULT = 60

CANDLE_INTERVALS = {
    "CANDLE_INTERVAL_5_MIN": "5 мин",
    "CANDLE_INTERVAL_15_MIN": "15 мин",
    "CANDLE_INTERVAL_HOUR": "1 час",
    "CANDLE_INTERVAL_4_HOUR": "4 часа",
    "CANDLE_INTERVAL_DAY": "1 день",
}


MAX_HISTORY_DAYS = {
    "CANDLE_INTERVAL_5_MIN": 7,
    "CANDLE_INTERVAL_15_MIN": 14,
    "CANDLE_INTERVAL_HOUR": 30,
    "CANDLE_INTERVAL_4_HOUR": 90,
    "CANDLE_INTERVAL_DAY": 365,
}

DEFAULT_HISTORY_FOR_INTERVAL = {
    "CANDLE_INTERVAL_5_MIN": 7,
    "CANDLE_INTERVAL_15_MIN": 14,
    "CANDLE_INTERVAL_HOUR": 60,
    "CANDLE_INTERVAL_4_HOUR": 60,
    "CANDLE_INTERVAL_DAY": 60,
}

UPDATE_SECONDS = 300
POSITION_SIZE_RUBLES = 100000.0
BUY_COMMISSION_PERCENT = 0.10
SELL_COMMISSION_PERCENT = 0.10
TAX_PERCENT = 13.0
HISTORY_FILE = "trade_history.json"
SETTINGS_FILE = "settings.json"
# ============================================================
# КОНСТАНТЫ ТОРГОВЛИ
# ============================================================
TRADING_ENABLED = True             # ВНИМАНИЕ: True = РЕАЛЬНАЯ торговля
ALLOWED_TELEGRAM_IDS = [1024945345]
MAX_POSITION_SIZE_RUB = 1000.0     # размер одной сделки в рублях
MAX_DAILY_LOSS_RUB = 1000.0        # дневной лимит убытка
MAX_OPEN_POSITIONS = 2             # максимум открытых позиций
TRADE_LOG_FILE = "trade_log.json"  # лог реальных сделок
DRY_RUN = False                    # False = отправляем ордера реально

TRADING_INTERVAL_CHECK = 300       # как часто проверять сигналы (5 минут)

MIN_BACKTEST_TRADES = 15
MIN_WINRATE = 40.0
MIN_PROFIT_DD_RATIO = 1.0
MIN_NET_RESULT = 0.0

SCREENING_INTERVALS = [
    "CANDLE_INTERVAL_HOUR",
    "CANDLE_INTERVAL_4_HOUR",
    "CANDLE_INTERVAL_DAY",
]

DEFAULT_SETTINGS = {
    "candle_interval": CANDLE_INTERVAL_DEFAULT,
    "history_days": HISTORY_DAYS_DEFAULT,
    "use_stop_loss": True,
    "use_take_profit": True,
    "use_breakeven": True,
    "sl_atr_mult": 2.0,
    "tp_atr_mult": 4.0,
    "breakeven_trigger_atr": 2.0,
    "screening_enabled": True,
}

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
warnings.filterwarnings("ignore")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("MARKUS_TRADE")

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False
app.config["JSONIFY_PRETTYPRINT_REGULAR"] = False
try:
    app.json.ensure_ascii = False
except Exception:
    pass

DATABASE_URL = os.environ.get("DATABASE_URL")


def get_db_connection():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL не задан в переменных окружения.")
    return psycopg2.connect(DATABASE_URL)


def init_db():
    """Создаёт таблицы для настроек и конфига торговли."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    data JSONB NOT NULL
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS trading_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    account_id TEXT,
                    pairs JSONB NOT NULL DEFAULT '[]'::jsonb,
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                );
            """)
            conn.commit()
    log.info("Таблицы settings и trading_config готовы.")

# ============================================================
# ПРОВЕРКА ДОСТУПА ЧЕРЕЗ TELEGRAM
# ============================================================
import hashlib
import hmac
import base64
from urllib.parse import parse_qsl


def get_token_for_webapp():
    return os.environ.get("TELEGRAM_BOT_TOKEN", "").strip() or None


def verify_telegram_init_data(init_data):
    if not init_data:
        return None
    bot_token = get_token_for_webapp()
    if not bot_token:
        log.warning("TELEGRAM_BOT_TOKEN не задан — защита не работает")
        return None
    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = parsed.pop("hash", None)
        if not received_hash:
            return None
        data_check_string = "\n".join(
            "{}={}".format(k, v) for k, v in sorted(parsed.items())
        )
        secret_key = hmac.new(
            b"WebAppData", bot_token.encode(), hashlib.sha256
        ).digest()
        calculated_hash = hmac.new(
            secret_key, data_check_string.encode(), hashlib.sha256
        ).hexdigest()
        if calculated_hash != received_hash:
            log.warning("initData: хеш не совпадает")
            return None
        user_json = parsed.get("user", "{}")
        user = json.loads(user_json)
        user_id = int(user.get("id", 0))
        if ALLOWED_TELEGRAM_IDS and user_id not in ALLOWED_TELEGRAM_IDS:
            log.warning("Отказано: user_id=%s не в белом списке", user_id)
            return None
        return user
    except Exception as exc:
        log.warning("verify_telegram_init_data error: %s", exc)
        return None


def is_authorized(req):
    if not ALLOWED_TELEGRAM_IDS:
        return True
    auth = req.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        log.warning("is_authorized: нет заголовка Authorization")
        return False
    try:
        encoded = auth[7:]
        init_data = base64.b64decode(encoded).decode("utf-8")
    except Exception:
        log.warning("is_authorized: не удалось декодировать initData")
        return False
    user = verify_telegram_init_data(init_data)
    return user is not None

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


def get_candles(instrument_uid, settings=None):
    """Скачивает свечи с авторетраем: если T-Банк говорит 'период слишком большой' — уменьшаем."""
    if settings is None:
        settings = load_settings()
    now = datetime.now(timezone.utc)
    interval = settings.get("candle_interval", CANDLE_INTERVAL_DEFAULT)
    if interval not in CANDLE_INTERVALS:
        interval = CANDLE_INTERVAL_DEFAULT

    max_days = MAX_HISTORY_DAYS.get(interval, 30)
    requested_days = int(settings.get("history_days", HISTORY_DAYS_DEFAULT))
    actual_days = max(1, min(requested_days, max_days))

    # Пробуем уменьшать период, пока T-Банк не согласится
    for attempt, days in enumerate([
        actual_days,
        max(1, actual_days // 2),
        max(1, actual_days // 3),
        max(1, actual_days // 5),
        7, 5, 3, 2, 1,
    ]):
        hours = days * 24
        start = now - timedelta(hours=hours)
        try:
            data = api_post(CANDLES_URL, {
                "from": start.isoformat(),
                "to": now.isoformat(),
                "interval": interval,
                "instrumentId": instrument_uid,
                "candleSourceType": "CANDLE_SOURCE_EXCHANGE",
            })
            candles = data.get("candles", [])
            if attempt > 0:
                log.info("get_candles: уменьшили период до %d дней для %s (%s)",
                         days, instrument_uid[:8], interval)
            return candles if isinstance(candles, list) else []
        except RuntimeError as exc:
            msg = str(exc)
            # Если ошибка про "maximum request period" — пробуем меньший период
            if "maximum request period" in msg or "30014" in msg:
                continue
            # Другая ошибка — пробрасываем наверх
            raise
    log.warning("get_candles: не удалось получить свечи для %s даже за 1 день", instrument_uid[:8])
    return []


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
                "description": "Авторская стратегия: растущие максимумы → разворот вниз"}
    if long_pattern:
        return {"signal": "LONG", "direction": "Вверх",
                "description": "Авторская стратегия: снижающиеся минимумы → разворот вверх"}
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
                "description": f"Закрытие пробило максимум 20 свечей ({high:.2f})"}
    if last["close"] < low:
        return {"signal": "SHORT", "direction": "Вниз",
                "description": f"Закрытие пробило минимум 20 свечей ({low:.2f})"}
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
                "description": f"RSI перепродан ({rsi:.1f}) и разворачивается вверх"}
    if rsi > 70 and closes[-1] < closes[-2]:
        return {"signal": "SHORT", "direction": "Вниз",
                "description": f"RSI перекуплен ({rsi:.1f}) и разворачивается вниз"}
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
                "description": "Hammer: длинная нижняя тень после падения"}
    if uptrend and body < total_range * 0.35 and upper_wick >= body * 2 and lower_wick < body * 0.5:
        return {"signal": "SHORT", "direction": "Вниз",
                "description": "Inverted Hammer: длинная верхняя тень после роста"}
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
                "description": "Bullish Engulfing"}
    if (prev_bullish and not last_bullish
            and last_body > prev_body * 1.2
            and last["open"] >= prev["close"] and last["close"] <= prev["open"]):
        return {"signal": "SHORT", "direction": "Вниз",
                "description": "Bearish Engulfing"}
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
                "description": f"Double Top около {second_half_high:.2f}"}
    if (abs(first_half_low - second_half_low) / first_half_low < tolerance
            and closes[-1] > max(lows[10:30]) * 1.01):
        return {"signal": "LONG", "direction": "Вверх",
                "description": f"Double Bottom около {second_half_low:.2f}"}
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
                "description": f"SuperTrend пробил нижнюю полосу (ATR={atr_val:.2f})"}
    if prev["close"] > prev_upper and last["close"] < upper_band:
        return {"signal": "SHORT", "direction": "Вниз",
                "description": f"SuperTrend пробил верхнюю полосу (ATR={atr_val:.2f})"}
    if last["close"] > lower_band and last["close"] < upper_band:
        return no_signal(f"SuperTrend в зоне: {lower_band:.2f}–{upper_band:.2f}")
    return no_signal("SuperTrend без сигнала")


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
        "total": total, "profitable": profitable, "losing": losing,
        "winrate": round(profitable / total * 100, 2),
        "gross": round(gross, 2), "commission": round(commission, 2),
        "tax": round(tax, 2), "net": round(net, 2),
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


def load_trading_config():
    """Загружает выбранные пары + accountId."""
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT account_id, pairs FROM trading_config WHERE id = 1;")
                row = cur.fetchone()
                if row:
                    return {
                        "account_id": row["account_id"] or "",
                        "pairs": row["pairs"] or [],
                    }
                return {"account_id": "", "pairs": []}
    except Exception as exc:
        log.warning("Ошибка чтения trading_config: %s", exc)
        return {"account_id": "", "pairs": []}


def save_trading_config(account_id, pairs):
    """Сохраняет выбранные пары + accountId."""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO trading_config (id, account_id, pairs, updated_at)
                    VALUES (1, %s, %s, NOW())
                    ON CONFLICT (id) DO UPDATE
                    SET account_id = EXCLUDED.account_id,
                        pairs = EXCLUDED.pairs,
                        updated_at = NOW();
                    """,
                    (account_id, json.dumps(pairs, ensure_ascii=False)),
                )
                conn.commit()
    except Exception as exc:
        log.error("Ошибка сохранения trading_config: %s", exc)


def get_accounts():
    """Запрашивает у Т-Банка список счетов пользователя."""
    try:
        data = api_post(ACCOUNTS_URL, {})
        accounts = data.get("accounts", [])
        result = []
        for acc in accounts:
            result.append({
                "id": acc.get("id", ""),
                "name": acc.get("name", ""),
                "type": acc.get("type", ""),
                "status": acc.get("status", ""),
                "opened_date": acc.get("openedDate", ""),
            })
        return result
    except Exception as exc:
        log.warning("Ошибка get_accounts: %s", exc)
        return []

# ============================================================
# ТОРГОВЫЕ ФУНКЦИИ
# ============================================================
TRADING_LOG_FILE = "trading_log.json"


def load_trading_log():
    if not os.path.exists(TRADING_LOG_FILE):
        return []
    try:
        with open(TRADING_LOG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_trading_log(entries):
    try:
        with open(TRADING_LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(entries[-1000:], f, ensure_ascii=False, indent=2)
    except Exception as exc:
        log.error("Ошибка сохранения trading_log: %s", exc)


def log_trade_event(event_type, details):
    entries = load_trading_log()
    entries.append({
        "time": datetime.now(timezone.utc).isoformat(),
        "type": event_type,
        "details": details,
    })
    save_trading_log(entries)
    log.info("TRADE_EVENT | %s | %s", event_type, json.dumps(details, ensure_ascii=False))


def get_positions(account_id):
    try:
        data = api_post(GET_POSITIONS_URL, {"accountId": account_id})
        positions = data.get("securities", []) + data.get("futures", [])
        return positions
    except Exception as exc:
        log.warning("Ошибка get_positions: %s", exc)
        return []


def place_order(account_id, instrument_uid, direction, quantity):
    order_id = str(uuid.uuid4())
    payload = {
        "instrumentId": instrument_uid,
        "quantity": str(quantity),
        "direction": "ORDER_DIRECTION_BUY" if direction == "BUY" else "ORDER_DIRECTION_SELL",
        "accountId": account_id,
        "orderType": "ORDER_TYPE_MARKET",
        "orderId": order_id,
    }
    log_trade_event("place_order_attempt", {
        "account_id": account_id,
        "instrument_uid": instrument_uid,
        "direction": direction,
        "quantity": quantity,
        "order_id": order_id,
    })
    try:
        result = api_post(POST_ORDER_URL, payload)
        log_trade_event("place_order_success", {"order_id": order_id, "response": result})
        return result
    except Exception as exc:
        log_trade_event("place_order_error", {"order_id": order_id, "error": str(exc)})
        raise


def calculate_lots(price, size_rub, lot_size=1):
    if price <= 0:
        return 0
    one_lot_cost = price * lot_size
    if one_lot_cost <= 0:
        return 0
    return max(0, int(size_rub // one_lot_cost))


def check_daily_loss():
    entries = load_trading_log()
    today = datetime.now(timezone.utc).date().isoformat()
    daily_loss = 0.0
    for e in entries:
        if not e.get("time", "").startswith(today):
            continue
        if e.get("type") == "trade_closed":
            pnl = float(e.get("details", {}).get("pnl", 0))
            if pnl < 0:
                daily_loss += abs(pnl)
    if daily_loss >= MAX_DAILY_LOSS_RUB:
        log.warning("ДНЕВНОЙ ЛИМИТ УБЫТКА: %.2f ₽ >= %.2f ₽", daily_loss, MAX_DAILY_LOSS_RUB)
        return True
    return False


def get_open_positions_count():
    entries = load_trading_log()
    opens = {}
    for e in entries:
        t = e.get("type")
        d = e.get("details", {})
        uid = d.get("instrument_uid")
        if not uid:
            continue
        if t == "position_opened":
            opens[uid] = d
        elif t == "position_closed":
            opens.pop(uid, None)
    return len(opens)


def is_position_open(instrument_uid):
    entries = load_trading_log()
    opens = {}
    for e in entries:
        t = e.get("type")
        d = e.get("details", {})
        uid = d.get("instrument_uid")
        if not uid:
            continue
        if t == "position_opened":
            opens[uid] = d
        elif t == "position_closed":
            opens.pop(uid, None)
    return instrument_uid in opens


def _resolve_instrument_uid(instrument_code):
    try:
        if instrument_code in ("CR", "GD", "BR"):
            instrument = find_active_future(instrument_code)
            return instrument["instrument_uid"] if instrument else None
        stock = None
        for s in STOCKS:
            if s["code"] == instrument_code:
                stock = s
                break
        if not stock:
            return None
        instrument = find_share(stock)
        return instrument["instrument_uid"] if instrument else None
    except Exception as exc:
        log.warning("_resolve_instrument_uid(%s): %s", instrument_code, exc)
        return None

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
        "direction": direction, "entry_price": price, "entry_time": candle_time,
        "stop_loss": stop_loss, "take_profit": take_profit,
        "atr_value": atr_val, "breakeven_moved": False,
    }


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
                        "instrument": instrument, "title": title, "direction": direction,
                        "entry_time": current_position["entry_time"], "exit_time": candle_time,
                        "entry_price": round(entry, 8), "exit_price": round(exit_price, 8),
                        "exit_reason": exit_reason, "exit_signal": signal,
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
            "name": strategy["name"], "key": strategy["key"],
            "statistics": stats, "drawdown": drawdown, "score": round(score, 4),
            "trades": trades, "open_position": open_position,
            "current_signal": last_analysis.get("signal"),
            "current_description": last_analysis.get("description", ""),
            "eligible": eligible, "reason": reason,
        })

    eligible_rows = [r for r in rows if r["eligible"]]
    if eligible_rows:
        best = max(eligible_rows, key=lambda x: x["score"])
        selection_reason = (
            f"Выбрана на {len(candles)} свечах: "
            f"{best['statistics']['total']} сделок, WR {best['statistics']['winrate']}%, "
            f"net {best['statistics']['net']:.2f} ₽, DD {best['drawdown']:.2f} ₽."
        )
    else:
        best = max(rows, key=lambda x: (x["statistics"]["total"], x["statistics"]["net"]))
        selection_reason = (
            "Ни одна стратегия не набрала минимум 3 закрытые сделки. "
            f"Временно выбрана с наибольшим количеством сделок ({best['statistics']['total']})."
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
        result["message"] = "Свечей 0 — API не вернул историю."
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
# АВТОМАТИЧЕСКИЙ СКРИНИНГ
# ============================================================
_SCREENING_CACHE = {"data": None, "updated_at": None}
_SCREENING_LOCK = threading.Lock()


def check_strategy_criteria(stats, drawdown):
    reasons = []
    ok = True
    if stats["total"] < MIN_BACKTEST_TRADES:
        ok = False
        reasons.append(f"мало сделок ({stats['total']}<{MIN_BACKTEST_TRADES})")
    if stats["winrate"] < MIN_WINRATE:
        ok = False
        reasons.append(f"низкий WR ({stats['winrate']}%<{MIN_WINRATE}%)")
    if stats["net"] <= MIN_NET_RESULT:
        ok = False
        reasons.append(f"net <= 0 ({stats['net']:.2f} ₽)")
    if drawdown <= 0:
        ok = False
        reasons.append("нулевая просадка")
    else:
        ratio = stats["net"] / drawdown
        if ratio < MIN_PROFIT_DD_RATIO:
            ok = False
            reasons.append(f"P/DD {ratio:.2f} < {MIN_PROFIT_DD_RATIO}")
    if ok:
        ratio = stats["net"] / max(drawdown, 1)
        return True, f"P/DD={ratio:.2f}, WR={stats['winrate']}%"
    return False, "; ".join(reasons)


def screen_strategies_for_instrument(instrument, title, instrument_code):
    passed = []
    rejected = []
    for interval in SCREENING_INTERVALS:
        interval_label = CANDLE_INTERVALS.get(interval, interval)
        max_days = MAX_HISTORY_DAYS.get(interval, 60)
        temp_settings = dict(DEFAULT_SETTINGS)
        temp_settings["candle_interval"] = interval
        temp_settings["history_days"] = max_days

        try:
            candles_raw = get_candles(instrument["instrument_uid"], temp_settings)
            candles = normalize_candles(candles_raw)
        except Exception as exc:
            log.warning("Ошибка получения свечей %s/%s: %s", title, interval_label, exc)
            candles = []

        if len(candles) < 40:
            rejected.append({
                "instrument": title, "ticker": instrument.get("ticker", "—"),
                "interval": interval_label, "interval_key": interval,
                "strategy": "—", "strategy_key": "—",
                "trades": 0, "winrate": 0, "net": 0, "drawdown": 0, "ratio": 0,
                "candles": len(candles),
                "reason": f"мало свечей ({len(candles)})",
            })
            continue

        for strategy in STRATEGIES:
            trades, _ = build_strategy_history(
                candles, instrument_code, title, strategy["fn"], temp_settings
            )
            stats = calculate_statistics(trades)
            dd = calculate_max_drawdown(trades)
            ok, reason = check_strategy_criteria(stats, dd)
            row = {
                "instrument": title, "ticker": instrument.get("ticker", "—"),
                "interval": interval_label, "interval_key": interval,
                "strategy": strategy["name"], "strategy_key": strategy["key"],
                "trades": stats["total"], "winrate": stats["winrate"],
                "net": stats["net"], "drawdown": dd,
                "ratio": round(stats["net"] / max(dd, 1), 2),
                "candles": len(candles), "reason": reason,
            }
            if ok:
                passed.append(row)
            else:
                rejected.append(row)
    return passed, rejected


def run_full_screening():
    log.info("=" * 60)
    log.info("ЗАПУСК ПОЛНОГО СКРИНИНГА СТРАТЕГИЙ")
    log.info("=" * 60)
    all_passed = []
    all_rejected = []
    instruments_checked = []

    for prefix, title, emoji in [("CR", "Юань", "¥"), ("GD", "Золото", "🥇"), ("BR", "Нефть Brent", "🛢️")]:
        try:
            instrument = find_active_future(prefix)
            if not instrument:
                log.warning("Инструмент %s не найден", title)
                continue
            instruments_checked.append(title)
            log.info("Скрининг %s (%s)...", title, instrument.get("ticker"))
            p, r = screen_strategies_for_instrument(instrument, title, prefix)
            all_passed.extend(p)
            all_rejected.extend(r)
        except Exception as exc:
            log.exception("Ошибка скрининга %s: %s", title, exc)

    for stock in STOCKS:
        try:
            instrument = find_share(stock)
            if not instrument:
                log.warning("Акция %s не найдена", stock["title"])
                continue
            instruments_checked.append(stock["title"])
            log.info("Скрининг %s (%s)...", stock["title"], instrument.get("ticker"))
            p, r = screen_strategies_for_instrument(instrument, stock["title"], stock["code"])
            all_passed.extend(p)
            all_rejected.extend(r)
        except Exception as exc:
            log.exception("Ошибка скрининга %s: %s", stock["title"], exc)

    all_passed.sort(key=lambda x: x["ratio"], reverse=True)
    all_rejected.sort(key=lambda x: x.get("ratio", -999), reverse=True)

    log.info("=" * 60)
    log.info("ИТОГИ СКРИНИНГА: прошло %d, отклонено %d", len(all_passed), len(all_rejected))
    for row in all_passed[:10]:
        log.info("OK %s | %s | %s | сделок=%d, WR=%.1f%%, net=%.0f, DD=%.0f, P/DD=%.2f",
                 row["instrument"], row["interval"], row["strategy"],
                 row["trades"], row["winrate"], row["net"], row["drawdown"], row["ratio"])
    log.info("=" * 60)

    return {
        "passed": all_passed, "rejected": all_rejected[:200],
        "instruments_checked": instruments_checked,
        "passed_count": len(all_passed), "rejected_count": len(all_rejected),
    }


def get_screening_cached(max_age_seconds=3600):
    with _SCREENING_LOCK:
        now = datetime.now(timezone.utc)
        updated = _SCREENING_CACHE.get("updated_at")
        if updated and (now - updated).total_seconds() < max_age_seconds:
            return _SCREENING_CACHE["data"]
        try:
            data = run_full_screening()
            _SCREENING_CACHE["data"] = data
            _SCREENING_CACHE["updated_at"] = now
            return data
        except Exception as exc:
            log.exception("Ошибка скрининга: %s", exc)
            return _SCREENING_CACHE["data"] or {
                "passed": [], "rejected": [], "instruments_checked": [],
                "passed_count": 0, "rejected_count": 0,
            }
def collect_data():
    settings = load_settings()
    interval_name = CANDLE_INTERVALS.get(settings.get("candle_interval"), "4 часа")

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
        "futures": futures, "shares": shares, "last_signal": last_signal,
        "statistics": total_statistics, "risk_settings": settings,
        "settings": {
            "position_size": POSITION_SIZE_RUBLES,
            "buy_commission": BUY_COMMISSION_PERCENT,
            "sell_commission": SELL_COMMISSION_PERCENT,
            "tax": TAX_PERCENT, "candle_interval": interval_name,
            "history_days": settings.get("history_days", HISTORY_DAYS_DEFAULT),
            "exit_rule": "Стоп-лосс / Тейк-профит / Противоположный сигнал",
            "strategies_count": len(STRATEGIES),
        },
    }

@app.route("/ping")
def ping():
    return "pong", 200

@app.route("/api/status")
def api_status():
    if not is_authorized(request):
        return jsonify({"error": "Forbidden"}), 403
    try:
        return jsonify(collect_data())
    except Exception as exc:
        log.exception("Ошибка /api/status")
        return jsonify({"error": str(exc)}), 500

@app.route("/api/history")
def api_history():
    if not is_authorized(request):
        return jsonify({"error": "Forbidden"}), 403
    history = load_history()

@app.route("/api/accounts")
def api_accounts():
    if not is_authorized(request):
        return jsonify({"ok": False, "error": "Forbidden"}), 403
    try:
        accounts = get_accounts()
        return jsonify({"ok": True, "accounts": accounts})
    except Exception as exc:
        log.exception("Ошибка /api/accounts")
        return jsonify({"ok": False, "error": str(exc)}), 500
    
@app.route("/api/trading_log")
def api_trading_log():
    if not is_authorized(request):
        return jsonify({"ok": False, "error": "Forbidden"}), 403
    try:
        entries = load_trading_log()
        return jsonify({
            "ok": True,
            "count": len(entries),
            "trading_enabled": TRADING_ENABLED,
            "entries": entries[-50:],
        })
    except Exception as exc:
        log.exception("Ошибка /api/trading_log")
        return jsonify({"ok": False, "error": str(exc)}), 500

@app.route("/api/trading_config", methods=["GET", "POST"])
def api_trading_config():
    if not is_authorized(request):
        return jsonify({"ok": False, "error": "Forbidden"}), 403
    if request.method == "GET":
        cfg = load_trading_config()
        return jsonify({
            "ok": True,
            "account_id": cfg["account_id"],
            "pairs": cfg["pairs"],
            "trading_enabled": TRADING_ENABLED,
        })
    try:
        data = request.get_json(force=True) or {}
        account_id = str(data.get("account_id", "")).strip()
        pairs = data.get("pairs", [])
        if not isinstance(pairs, list):
            return jsonify({"ok": False, "error": "pairs must be a list"}), 400
        clean_pairs = []
        valid_strategy_keys = [s["key"] for s in STRATEGIES]
        for p in pairs:
            if not isinstance(p, dict):
                continue
            instrument = str(p.get("instrument", "")).strip()
            strategy_key = str(p.get("strategy", "")).strip()
            interval = str(p.get("interval", "")).strip()
            if strategy_key not in valid_strategy_keys:
                continue
            if interval not in CANDLE_INTERVALS:
                continue
            if not instrument:
                continue
            clean_pairs.append({
                "instrument": instrument,
                "strategy": strategy_key,
                "interval": interval,
                "size_rub": float(p.get("size_rub", MAX_POSITION_SIZE_RUB)),
                "use_sl": bool(p.get("use_sl", True)),
                "use_tp": bool(p.get("use_tp", True)),
            })
        save_trading_config(account_id, clean_pairs)
        return jsonify({"ok": True, "pairs": clean_pairs})
    except Exception as exc:
        log.exception("Ошибка /api/trading_config")
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/screening")
def api_screening():
    if not is_authorized(request):
        return jsonify({"error": "Forbidden"}), 403
    try:
        force = request.args.get("force", "").lower() in ("1", "true", "yes")
        if force:
            with _SCREENING_LOCK:
                _SCREENING_CACHE["updated_at"] = None
        data = get_screening_cached()
        updated = _SCREENING_CACHE["updated_at"]
        import json as _json
        body = _json.dumps({
            "updated_at": updated.isoformat() if updated else None,
            **data,
        }, ensure_ascii=False)
        return app.response_class(
            body,
            mimetype="application/json; charset=utf-8"
        )
    except Exception as exc:
        log.exception("Ошибка /api/screening")
        return jsonify({"error": str(exc)}), 500



@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if not is_authorized(request):
        return jsonify({"ok": False, "error": "Forbidden"}), 403
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
        interval_now = current.get("candle_interval", CANDLE_INTERVAL_DEFAULT)
        max_days = MAX_HISTORY_DAYS.get(interval_now, 60)
        try:
            days = int(current.get("history_days", 60))
        except Exception:
            days = 60
        current["history_days"] = max(5, min(days, max_days))
        save_settings(current)
        return jsonify({"ok": True, "settings": current})
    except Exception as exc:
        log.exception("Ошибка сохранения настроек")
        return jsonify({"ok": False, "error": str(exc)}), 500

def _run_trading_check():
    """Проверяет сигналы и ОТПРАВЛЯЕТ реальные ордера."""
    if not TRADING_ENABLED:
        return

    config = load_trading_config()
    pairs = config.get("pairs", [])
    account_id = config.get("account_id", "")

    if not pairs:
        return
    if not account_id:
        log.warning("account_id пуст — торговля невозможна")
        return
    if check_daily_loss():
        log.warning("Дневной лимит убытка — торговля остановлена до завтра")
        return

    for pair in pairs:
        try:
            instrument = pair.get("instrument", "")
            strategy_key = pair.get("strategy", "")
            interval = pair.get("interval", "")
            size_rub = float(pair.get("size_rub", MAX_POSITION_SIZE_RUB))

            if not instrument or not strategy_key or not interval:
                continue

            strategy_fn = None
            for s in STRATEGIES:
                if s["key"] == strategy_key:
                    strategy_fn = s["fn"]
                    break
            if strategy_fn is None:
                continue

            instrument_uid = _resolve_instrument_uid(instrument)
            if not instrument_uid:
                continue

            # ПРОВЕРКА: не открыта ли уже позиция по этой паре
            if is_position_open(instrument_uid):
                continue

            # ПРОВЕРКА: не превышен ли лимит открытых позиций
            if get_open_positions_count() >= MAX_OPEN_POSITIONS:
                log.info("Лимит открытых позиций достигнут (%d)", MAX_OPEN_POSITIONS)
                continue

            temp_settings = dict(DEFAULT_SETTINGS)
            temp_settings["candle_interval"] = interval
            temp_settings["history_days"] = MAX_HISTORY_DAYS.get(interval, 60)

            candles = normalize_candles(get_candles(instrument_uid, temp_settings))
            if len(candles) < 40:
                continue

            analysis = strategy_fn(candles)
            signal = analysis.get("signal", "Нет сигналов")
            if signal not in ("LONG", "SHORT"):
                continue

            last_price = candles[-1]["close"]
            lots = calculate_lots(last_price, size_rub)
            if lots <= 0:
                log.warning("Лотов = 0 для %s при цене %.2f и бюджете %.0f ₽",
                            instrument, last_price, size_rub)
                continue

            direction = "BUY" if signal == "LONG" else "SELL"

            log.info("ОТКРЫТИЕ | %s | %s | %s | %d лотов | %.2f ₽ | итог %.0f ₽",
                     instrument, strategy_key, signal, lots, last_price, lots * last_price)

            result = place_order(account_id, instrument_uid, direction, lots)

            log_trade_event("position_opened", {
                "instrument": instrument,
                "instrument_uid": instrument_uid,
                "strategy": strategy_key,
                "interval": interval,
                "signal": signal,
                "lots": lots,
                "price": last_price,
                "size_rub": size_rub,
                "order_result": result,
            })

        except Exception as exc:
            log.exception("Ошибка торговли %s: %s", pair, exc)

def background_monitor():
    first_run = True
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

        try:
            _run_trading_check()
        except Exception as exc:
            log.exception("Ошибка _run_trading_check: %s", exc)
            with _SCREENING_LOCK:
                now = datetime.now(timezone.utc)
                updated = _SCREENING_CACHE.get("updated_at")
                need_screening = first_run or not updated or \
                                 (now - updated).total_seconds() > 3600
            if need_screening:
                log.info("Пересчёт скрининга стратегий...")
                get_screening_cached(max_age_seconds=0)
                first_run = False
        except Exception as exc:
            log.exception("Ошибка пересчёта скрининга: %s", exc)

        time.sleep(UPDATE_SECONDS)


_BACKGROUND_STARTED = False
_BACKGROUND_LOCK = threading.Lock()


def _start_background_if_needed():
    global _BACKGROUND_STARTED
    with _BACKGROUND_LOCK:
        if _BACKGROUND_STARTED:
            return
        _BACKGROUND_STARTED = True
    try:
        init_db()
    except Exception as exc:
        log.error("Ошибка инициализации БД: %s", exc)
    try:
        t = threading.Thread(target=background_monitor, daemon=True)
        t.start()
        log.info("Фоновый мониторинг запущен (thread=%s)", t.name)
    except Exception as exc:
        log.error("Не удалось запустить фоновый мониторинг: %s", exc)


_start_background_if_needed()


HTML = r"""
<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
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
button{margin-top:10px;margin-right:6px;border:none;border-radius:12px;padding:10px 16px;background:#d7aa52;color:#111;font-weight:bold;cursor:pointer;font-size:13px}
input[type=number],input[type=text],select{padding:6px;border-radius:6px;background:#0d1219;color:#fff;border:1px solid rgba(255,255,255,.15);font-size:13px}
details summary{cursor:pointer;color:#d7aa52;font-weight:bold;margin-top:20px;padding:8px 0;list-style:none}
details summary::-webkit-details-marker{display:none}
details summary::before{content:"[+] ";font-size:12px}
details[open] summary::before{content:"[-] "}
.screening-card{background:#0d1219;border-radius:12px;padding:14px;margin-bottom:10px;border-left:3px solid #52e58a}
.screening-card-rej{background:#0d1219;border-radius:10px;padding:11px;margin-bottom:8px;border-left:3px solid #442020}
.screening-card .sc-title{font-size:15px;font-weight:bold;color:#fff;margin-bottom:8px}
.screening-card .sc-sub{font-size:13px;color:#aab3c2;margin-bottom:6px}
.screening-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;font-size:13px;margin-top:10px}
.screening-grid-2{display:grid;grid-template-columns:1fr 1fr;gap:8px;font-size:13px;margin-top:6px}
.screening-label{color:#8f99aa}
.screening-card-rej .sc-title{font-size:13px;font-weight:bold;color:#fff}
.screening-card-rej .sc-sub{font-size:12px;color:#8f99aa;margin-top:3px}
.screening-card-rej .sc-meta{font-size:12px;color:#aab3c2;margin-top:6px}
.screening-card-rej .sc-reason{font-size:12px;color:#ff8585;margin-top:6px}
.pair-card{background:#0d1219;border-radius:12px;padding:14px;margin-bottom:10px;border-left:3px solid #d7aa52}
.pair-card .pair-label{font-size:11px;color:#8f99aa;margin-bottom:4px}
.pair-row2{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px}
.pair-row3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px}
@media(max-width:1100px){.grid{grid-template-columns:1fr 1fr}}
@media(max-width:700px){.grid{grid-template-columns:1fr}.stats-grid{grid-template-columns:repeat(2,1fr)}.header{align-items:flex-start;gap:10px;flex-direction:column}}
</style></head><body><div class="container">
<div class="header"><div class="logo">MARKUS <span>TRADE</span></div><div class="updated" id="updated">Loading...</div></div>

<div class="card" id="tradingConfigCard">
<h2>Trading Settings</h2>
<div class="info">
<b>Account ID:</b> <input type="text" id="accountIdInput" placeholder="e.g. 2141736260" style="width:180px">
<button onclick="loadAccounts()" style="padding:6px 12px;font-size:12px">Find accounts</button>
<div style="font-size:11px;color:#7a8394;margin-top:6px" id="accountsHint"></div>
<br>
<b>Trading status:</b> <span id="tradingStatus" style="font-weight:bold">LOADING...</span>
<div style="margin-top:8px;padding:8px;border-radius:8px;background:#442020;color:#ff8585;font-size:12px">
⚠️ РЕАЛЬНАЯ ТОРГОВЛЯ ВКЛЮЧЕНА. Робот будет отправлять ордера от вашего имени.<br>
Дневной лимит убытка: 1000 ₽. Максимум позиций: 2.
</div>
</div>

<div style="margin-top:15px;font-size:13px;color:#aab3c2">
<b>Selected pairs for trading</b> (instrument, strategy, timeframe):
</div>

<div id="pairsList" style="margin-top:10px"></div>

<div style="margin-top:15px">
<button onclick="addPair()">+ Add pair</button>
<button onclick="saveTradingConfig()">Save</button>
<button onclick="loadTradingConfig()">Refresh</button>
</div>
</div>

<div class="card" id="screeningCard">
<h2>Auto Strategy Screening</h2>
<div class="info" id="screeningInfo">Screening started. First run may take 1-3 minutes...</div>
<div id="screeningPassed" style="margin-top:15px"></div>
<details>
<summary>Rejected strategies (click to expand)</summary>
<div id="screeningRejected" style="margin-top:12px"></div>
</details>
</div>

<div class="card" id="riskCard">
<h2>Backtest Settings</h2>
<div class="info">
<b>Timeframe (for cards below):</b><br>
<select id="intervalSelect" style="width:100%" onchange="onIntervalChange()">
<option value="CANDLE_INTERVAL_5_MIN">5 min</option>
<option value="CANDLE_INTERVAL_15_MIN">15 min</option>
<option value="CANDLE_INTERVAL_HOUR">1 hour</option>
<option value="CANDLE_INTERVAL_4_HOUR">4 hours</option>
<option value="CANDLE_INTERVAL_DAY">1 day</option>
</select>
<br><br>
<b>History depth (days):</b><br>
<input type="number" id="historyDaysInput" min="1" max="180" step="1" style="width:100%">
<div style="font-size:11px;color:#7a8394;margin-top:4px" id="historyHint"></div>
<br>
<b>Risk Management</b><br><br>
<label><input type="checkbox" id="useSL"> Stop-Loss</label>
<input type="number" id="slMult" step="0.1" min="0.5" style="width:70px"> x ATR<br>
<label><input type="checkbox" id="useTP"> Take-Profit</label>
<input type="number" id="tpMult" step="0.1" min="0.5" style="width:70px"> x ATR<br>
<label><input type="checkbox" id="useBE"> Breakeven</label>
<input type="number" id="beTrig" step="0.1" min="0.1" style="width:70px"> x ATR<br><br>
<button onclick="saveRiskSettings()">Save</button>
<button onclick="loadData()">Recalculate</button>
</div>
</div>

<div class="section-title">FUTURES</div><div class="grid" id="futures"></div>
<div class="section-title">STOCKS</div><div class="grid" id="shares"></div>

<div class="card statistics"><h2>Total Statistics</h2><div class="stats-grid" id="statistics"></div></div>
<div class="card history"><h2>Trade History</h2><div class="table-wrap" id="history"></div></div>
<div class="card" id="tradingLogCard">
<h2>Trading Log (последние 20)</h2>
<div id="tradingLogList" style="font-size:12px;color:#aab3c2">Загрузка...</div>
</div>
<div class="card settings"><h2>Parameters</h2>
Position size: <b id="positionSize">---</b> RUB<br>
Buy commission: <b id="buyCommission">---</b>%<br>
Sell commission: <b id="sellCommission">---</b>%<br>
Tax: <b id="tax">---</b>%<br>
Timeframe: <b id="interval">---</b><br>
History: <b id="historyDays">---</b> days<br>
Strategies: <b id="strategiesCount">---</b><br>
Exit rule: <b id="exitRule">---</b></div>

<script>
function money(v){return Number(v||0).toLocaleString('ru-RU',{minimumFractionDigits:2,maximumFractionDigits:2})}
const _originalFetch = window.fetch.bind(window);
window.fetch = function(url, options) {
  options = options || {};
  const headers = Object.assign({}, options.headers || {});
  try {
    if (url && typeof url === 'string' && url.indexOf('/api/') === 0) {
      if (window.Telegram && window.Telegram.WebApp && window.Telegram.WebApp.initData) {
        headers['Authorization'] = 'Bearer ' + btoa(window.Telegram.WebApp.initData);
      }
    }
  } catch(e) { console.error('initData error:', e); }
  return _originalFetch(url, Object.assign({}, options, { headers: headers }));
};

function signalClass(s){return s==='LONG'?'long':s==='SHORT'?'short':'none'}

const MAX_HISTORY_DAYS_JS = {"CANDLE_INTERVAL_5_MIN":7,"CANDLE_INTERVAL_15_MIN":14,"CANDLE_INTERVAL_HOUR":30,"CANDLE_INTERVAL_4_HOUR":90,"CANDLE_INTERVAL_DAY":365};
const DEFAULT_HISTORY_FOR_INTERVAL_JS = {"CANDLE_INTERVAL_5_MIN":5,"CANDLE_INTERVAL_15_MIN":10,"CANDLE_INTERVAL_HOUR":30,"CANDLE_INTERVAL_4_HOUR":60,"CANDLE_INTERVAL_DAY":60};
const INTERVAL_LABELS = {"CANDLE_INTERVAL_5_MIN":"5 min","CANDLE_INTERVAL_15_MIN":"15 min","CANDLE_INTERVAL_HOUR":"1 hour","CANDLE_INTERVAL_4_HOUR":"4 hours","CANDLE_INTERVAL_DAY":"1 day"};

function updateHistoryHint(interval){
  const hint=document.getElementById('historyHint');
  const maxDays=MAX_HISTORY_DAYS_JS[interval]||60;
  hint.textContent='Maximum for '+(INTERVAL_LABELS[interval]||interval)+': '+maxDays+' days';
}
function onIntervalChange(){
  const interval=document.getElementById('intervalSelect').value;
  const hdInput=document.getElementById('historyDaysInput');
  const maxDays=MAX_HISTORY_DAYS_JS[interval]||60;
  const defDays=DEFAULT_HISTORY_FOR_INTERVAL_JS[interval]||60;
  hdInput.max=maxDays;
  hdInput.value=defDays;
  updateHistoryHint(interval);
}
function renderInstrumentCard(item){
  const signal=item.strategy.signal;
  const stats=item.statistics||{};
  let open='None';
  if(item.open_position) open=item.open_position.direction+' at '+money(item.open_position.entry_price);
  return `<div class="card"><h2>${item.title}</h2><span class="status ${item.status==='OK'?'':'error'}">${item.status}</span><div class="info">${item.message||''}</div><div class="info">Ticker: <b>${item.ticker}</b><br>UID: <b>${item.uid}</b><br>Candles: <b>${item.candles}</b></div><div class="signal ${signalClass(signal)}">${signal}</div><div class="info">${item.strategy.description||''}</div><div class="info selected"><b>Selected: ${item.selected_strategy}</b><br>${item.selection_reason||''}<br>Trades: <b>${stats.total||0}</b> | Winrate: <b>${stats.winrate||0}%</b><br>Winning: <b>${stats.profitable||0}</b> | Losing: <b>${stats.losing||0}</b><br>Net: <b>${money(stats.net)} RUB</b><br>Open position: <b>${open}</b></div><div class="strategy-box"><b>All strategies</b>${(item.strategy_selection||[]).map((r,i)=>`<div class="strategy-row ${r.is_selected?'selected':''}"><span>${r.is_selected?'* ':''}${i+1}. ${r.name}</span><span>${r.statistics.total} tr</span><span>${r.statistics.winrate}%</span><span class="${r.statistics.net>=0?'positive':'negative'}">${money(r.statistics.net)} RUB</span><span>DD ${money(r.drawdown)} RUB</span><span>${r.reason}</span></div>`).join('')}</div></div>`;
}
function renderFutures(data){document.getElementById('futures').innerHTML=data.futures.map(renderInstrumentCard).join('')}
function renderShares(data){document.getElementById('shares').innerHTML=data.shares.map(renderInstrumentCard).join('')}
function renderStatistics(s){
  document.getElementById('statistics').innerHTML=`<div class="stat"><div class="stat-title">Total trades</div><div class="stat-value">${s.total}</div></div><div class="stat"><div class="stat-title">Winning</div><div class="stat-value">${s.profitable}</div></div><div class="stat"><div class="stat-title">Losing</div><div class="stat-value">${s.losing}</div></div><div class="stat"><div class="stat-title">Winrate</div><div class="stat-value">${s.winrate}%</div></div><div class="stat"><div class="stat-title">Gross</div><div class="stat-value">${money(s.gross)} RUB</div></div><div class="stat"><div class="stat-title">Commissions</div><div class="stat-value">${money(s.commission)} RUB</div></div><div class="stat"><div class="stat-title">Tax</div><div class="stat-value">${money(s.tax)} RUB</div></div><div class="stat"><div class="stat-title">NET RESULT</div><div class="stat-value ${s.net>=0?'positive':'negative'}">${money(s.net)} RUB</div></div>`;
}
function renderHistory(data){
  let all=[];
  [...data.futures,...data.shares].forEach(x=>all=all.concat(x.history||[]));
  all.sort((a,b)=>new Date(b.exit_time)-new Date(a.exit_time));
  const c=document.getElementById('history');
  if(!all.length){c.innerHTML='No closed trades yet.';return}
  let h='<table><thead><tr><th>Instrument</th><th>Dir</th><th>Entry</th><th>Exit</th><th>Entry price</th><th>Exit price</th><th>Reason</th><th>Result</th><th>Comm</th><th>Tax</th><th>Net</th></tr></thead><tbody>';
  all.slice(0,100).forEach(t=>{
    const n=Number(t.net_result||0);
    const commission=Number(t.buy_commission||0)+Number(t.sell_commission||0);
    h+=`<tr><td>${t.title}</td><td>${t.direction}</td><td>${t.entry_time}</td><td>${t.exit_time}</td><td>${t.entry_price}</td><td>${t.exit_price}</td><td>${t.exit_reason||'---'}</td><td>${money(t.gross_result)} RUB</td><td>${money(commission)} RUB</td><td>${money(t.tax)} RUB</td><td class="${n>=0?'positive':'negative'}">${money(n)} RUB</td></tr>`;
  });
  c.innerHTML=h+'</tbody></table>';
}
function renderRiskSettings(s){
  const sel=document.getElementById('intervalSelect');
  if(sel && s.candle_interval) sel.value=s.candle_interval;
  const intervalNow=s.candle_interval||'CANDLE_INTERVAL_4_HOUR';
  const hd=document.getElementById('historyDaysInput');
  hd.max=MAX_HISTORY_DAYS_JS[intervalNow]||60;
  if(s.history_days) hd.value=s.history_days;
  updateHistoryHint(intervalNow);
  document.getElementById('useSL').checked=!!s.use_stop_loss;
  document.getElementById('slMult').value=s.sl_atr_mult;
  document.getElementById('useTP').checked=!!s.use_take_profit;
  document.getElementById('tpMult').value=s.tp_atr_mult;
  document.getElementById('useBE').checked=!!s.use_breakeven;
  document.getElementById('beTrig').value=s.breakeven_trigger_atr;
}
function renderScreening(data){
  const info=document.getElementById('screeningInfo');
  const passedEl=document.getElementById('screeningPassed');
  const rejectedEl=document.getElementById('screeningRejected');
  if(!data || data.error){
    info.innerHTML='Error: '+(data && data.error ? data.error : 'no data');
    return;
  }
  const passedCount=data.passed_count||0;
  const rejectedCount=data.rejected_count||0;
  const updatedAt=data.updated_at?new Date(data.updated_at).toLocaleString('en-GB'):'--';
  if(passedCount===0){
    info.innerHTML=`<b style="color:#ff6666">No strategy passed the filter</b><br>Checked ${rejectedCount} combinations.<br>Criteria: >=15 trades, WR >=40%, net positive, P/DD >=1.0.<br><span style="color:#8f99aa;font-size:12px">Updated: ${updatedAt}</span>`;
    passedEl.innerHTML='';
  }else{
    info.innerHTML=`<b style="color:#52e58a">Found working strategies: ${passedCount}</b> (rejected: ${rejectedCount})<br><span style="color:#8f99aa;font-size:12px">Updated: ${updatedAt}. Top-10 by P/DD:</span>`;
    let h='';
    data.passed.slice(0,10).forEach(r=>{
      h+=`<div class="screening-card">
        <div class="sc-title">${r.strategy}</div>
        <div class="sc-sub">${r.instrument} | ${r.interval}</div>
        <div class="screening-grid">
          <div><span class="screening-label">Trades:</span> <b>${r.trades}</b></div>
          <div><span class="screening-label">WR:</span> <b>${r.winrate}%</b></div>
          <div><span class="screening-label">P/DD:</span> <b class="positive">${r.ratio}</b></div>
        </div>
        <div class="screening-grid-2">
          <div><span class="screening-label">Net:</span> <b class="positive">${money(r.net)} RUB</b></div>
          <div><span class="screening-label">DD:</span> <b>${money(r.drawdown)} RUB</b></div>
        </div>
      </div>`;
    });
    passedEl.innerHTML=h;
  }
  if(rejectedEl && data.rejected && data.rejected.length){
    let h='';
    data.rejected.slice(0,60).forEach(r=>{
      const netClass=(r.net||0)>=0?'positive':'negative';
      h+=`<div class="screening-card-rej">
        <div class="sc-title">${r.strategy}</div>
        <div class="sc-sub">${r.instrument} | ${r.interval}</div>
        <div class="sc-meta">
          <span>Trades: <b>${r.trades||0}</b></span> |
          <span>WR: <b>${r.winrate||0}%</b></span> |
          <span>Net: <b class="${netClass}">${money(r.net||0)} RUB</b></span>
        </div>
        <div class="sc-reason">${r.reason||'--'}</div>
      </div>`;
    });
    rejectedEl.innerHTML=h;
  }else if(rejectedEl){
    rejectedEl.innerHTML='No data.';
  }
}

// ============================================================
// TRADING CONFIG
// ============================================================
const AVAILABLE_INSTRUMENTS = [
  {id: "SBER", title: "Сбербанк"},
  {id: "ROSN", title: "Роснефть"},
  {id: "GMKN", title: "Норникель"},
  {id: "CR",   title: "Юань"},
  {id: "GD",   title: "Золото"},
  {id: "BR",   title: "Нефть Brent"}
];
const AVAILABLE_STRATEGIES = [
  {key: "user",       name: "Твоя стратегия"},
  {key: "ema",        name: "EMA Trend"},
  {key: "breakout",   name: "Breakout"},
  {key: "rsi",        name: "RSI Reversal"},
  {key: "macd",       name: "MACD"},
  {key: "bollinger",  name: "Bollinger"},
  {key: "hammer",     name: "Hammer"},
  {key: "engulfing",  name: "Engulfing"},
  {key: "double",     name: "Double Top/Bottom"},
  {key: "supertrend", name: "SuperTrend"}
];
const AVAILABLE_INTERVALS = [
  {key: "CANDLE_INTERVAL_HOUR",   label: "1 час"},
  {key: "CANDLE_INTERVAL_4_HOUR", label: "4 часа"},
  {key: "CANDLE_INTERVAL_DAY",    label: "1 день"}
];
let CURRENT_PAIRS = [];

async function loadAccounts(){
  const hint = document.getElementById('accountsHint');
  hint.textContent = 'Загрузка...';
  try {
    const r = await fetch('/api/accounts');
    const data = await r.json();
    if (!data.ok) {
      hint.innerHTML = '<span style="color:#ff6666">Ошибка: ' + (data.error || 'нет данных') + '</span>';
      return;
    }
    if (!data.accounts || !data.accounts.length) {
      hint.innerHTML = '<span style="color:#ff6666">Счета не найдены</span>';
      return;
    }
    hint.innerHTML = 'Найдено: ' + data.accounts.map(a =>
      '<b>' + a.id + '</b> (' + (a.status || '') + ')'
    ).join(', ');
    if (!document.getElementById('accountIdInput').value && data.accounts[0]) {
      document.getElementById('accountIdInput').value = data.accounts[0].id;
    }
  } catch(e) {
    hint.innerHTML = '<span style="color:#ff6666">Ошибка: ' + e.message + '</span>';
  }
}

function makeSelect(id, options, currentValue, valueField, labelField) {
  let html = '<select id="' + id + '" style="padding:6px;border-radius:6px;background:#0d1219;color:#fff;border:1px solid rgba(255,255,255,.15);width:100%">';
  options.forEach(o => {
    const v = o[valueField];
    const lbl = o[labelField] || v;
    const sel = (v === currentValue) ? ' selected' : '';
    html += '<option value="' + v + '"' + sel + '>' + lbl + '</option>';
  });
  html += '</select>';
  return html;
}

function renderPairs(){
  const el = document.getElementById('pairsList');
  if (!CURRENT_PAIRS.length) {
    el.innerHTML = '<div style="color:#8f99aa;font-size:13px;padding:10px">Пока не выбрано ни одной пары. Нажмите «+ Add pair».</div>';
    return;
  }
  let h = '';
  CURRENT_PAIRS.forEach((p, i) => {
    h += '<div class="pair-card">';
    h += '<div class="pair-row2">';
    h += '<div><div class="pair-label">Инструмент</div>' + makeSelect('pair_inst_' + i, AVAILABLE_INSTRUMENTS, p.instrument, 'id', 'title') + '</div>';
    h += '<div><div class="pair-label">Стратегия</div>' + makeSelect('pair_strat_' + i, AVAILABLE_STRATEGIES, p.strategy, 'key', 'name') + '</div>';
    h += '</div>';
    h += '<div class="pair-row3">';
    h += '<div><div class="pair-label">Таймфрейм</div>' + makeSelect('pair_int_' + i, AVAILABLE_INTERVALS, p.interval, 'key', 'label') + '</div>';
    h += '<div><div class="pair-label">Размер, ₽</div><input type="number" id="pair_size_' + i + '" value="' + (p.size_rub || 1000) + '" style="width:100%"></div>';
    h += '<div style="display:flex;align-items:flex-end"><button onclick="removePair(' + i + ')" style="background:#442020;color:#ff8585;padding:6px 12px">Удалить</button></div>';
    h += '</div>';
    h += '</div>';
  });
  el.innerHTML = h;
}

function collectPairsFromUI(){
  const result = [];
  CURRENT_PAIRS.forEach((p, i) => {
    result.push({
      instrument: document.getElementById('pair_inst_' + i).value,
      strategy: document.getElementById('pair_strat_' + i).value,
      interval: document.getElementById('pair_int_' + i).value,
      size_rub: parseFloat(document.getElementById('pair_size_' + i).value) || 1000,
      use_sl: true,
      use_tp: true
    });
  });
  return result;
}

function addPair(){
  CURRENT_PAIRS = collectPairsFromUI();
  CURRENT_PAIRS.push({
    instrument: "SBER",
    strategy: "engulfing",
    interval: "CANDLE_INTERVAL_4_HOUR",
    size_rub: 1000,
    use_sl: true,
    use_tp: true
  });
  renderPairs();
}

function removePair(index){
  CURRENT_PAIRS = collectPairsFromUI();
  CURRENT_PAIRS.splice(index, 1);
  renderPairs();
}

async function loadTradingConfig(){
  try {
    const r = await fetch('/api/trading_config');
    const data = await r.json();
    if (!data.ok) {
      console.error('Trading config error:', data.error);
      return;
    }
    document.getElementById('accountIdInput').value = data.account_id || '';
    CURRENT_PAIRS = data.pairs || [];
    renderPairs();
    const st = document.getElementById('tradingStatus');
    if (data.trading_enabled) {
      st.textContent = 'LIVE TRADING (реальная торговля)';
      st.style.color = '#ff6666';
      st.style.fontWeight = 'bold';
    } else {
      st.textContent = 'DISABLED (только сигналы)';
      st.style.color = '#52e58a';
    }
  } catch(e) {
    console.error('loadTradingConfig error:', e);
  }
}

async function saveTradingConfig(){
  const payload = {
    account_id: document.getElementById('accountIdInput').value.trim(),
    pairs: collectPairsFromUI()
  };
  const r = await fetch('/api/trading_config', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  });
  const res = await r.json();
  if (res.ok) {
    alert('Сохранено: ' + res.pairs.length + ' пар');
    await loadTradingConfig();
  } else {
    alert('Ошибка: ' + res.error);
  }
}
async function loadTradingLog(){
  try {
    const r = await fetch('/api/trading_log');
    const data = await r.json();
    const el = document.getElementById('tradingLogList');
    if (!data.ok || !data.entries || !data.entries.length) {
      el.innerHTML = '<div style="color:#8f99aa">Пока нет событий</div>';
      return;
    }
    let h = '';
    data.entries.slice(-20).reverse().forEach(e => {
      const t = e.time ? new Date(e.time).toLocaleString('ru-RU') : '';
      const type = e.type || '';
      const d = e.details || {};
      let line = '';
      if (type === 'place_order_attempt') {
        line = 'ОТПРАВКА ОРДЕРА: ' + (d.instrument_uid || '').slice(0,8) + ' | ' + (d.direction || '') + ' x' + (d.quantity || 0);
      } else if (type === 'place_order_success') {
        line = '✅ ОРДЕР ПРИНЯТ: ' + (d.order_id || '').slice(0,8);
      } else if (type === 'place_order_error') {
        line = '❌ ОШИБКА ОРДЕРА: ' + (d.error || '').slice(0,100);
      } else if (type === 'position_opened') {
        line = '🟢 ОТКРЫТА: ' + (d.instrument || '') + ' | ' + (d.signal || '') + ' | ' + (d.lots || 0) + ' лотов';
      } else {
        line = type + ': ' + JSON.stringify(d).slice(0,120);
      }
      h += '<div style="padding:6px 0;border-bottom:1px solid rgba(255,255,255,.06)">';
      h += '<span style="color:#7a8394">' + t + '</span> — ' + line;
      h += '</div>';
    });
    el.innerHTML = h;
  } catch(e) {
    console.error(e);
  }
}

async function saveRiskSettings(){
  const payload={
    candle_interval:document.getElementById('intervalSelect').value,
    history_days:parseInt(document.getElementById('historyDaysInput').value)||60,
    use_stop_loss:document.getElementById('useSL').checked,
    sl_atr_mult:parseFloat(document.getElementById('slMult').value)||2.0,
    use_take_profit:document.getElementById('useTP').checked,
    tp_atr_mult:parseFloat(document.getElementById('tpMult').value)||4.0,
    use_breakeven:document.getElementById('useBE').checked,
    breakeven_trigger_atr:parseFloat(document.getElementById('beTrig').value)||2.0,
  };
  const r=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
  const res=await r.json();
  if(res.ok){alert('Settings saved.');loadData();}
  else{alert('Error: '+res.error);}
}
async function loadData(){
  try{
    const response=await fetch('/api/status');
    const data=await response.json();
    if(data.error){console.error(data.error);return}
    renderFutures(data);renderShares(data);renderStatistics(data.statistics);renderHistory(data);
    if(data.risk_settings) renderRiskSettings(data.risk_settings);
    document.getElementById('updated').textContent='Updated: '+new Date(data.updated).toLocaleString('en-GB');
    document.getElementById('positionSize').textContent=money(data.settings.position_size);
    document.getElementById('buyCommission').textContent=data.settings.buy_commission;
    document.getElementById('sellCommission').textContent=data.settings.sell_commission;
    document.getElementById('tax').textContent=data.settings.tax;
    document.getElementById('interval').textContent=data.settings.candle_interval;
    document.getElementById('historyDays').textContent=data.settings.history_days;
    document.getElementById('strategiesCount').textContent=data.settings.strategies_count;
    document.getElementById('exitRule').textContent=data.settings.exit_rule;
  }catch(e){console.error('Load error:',e)}
}
async function loadScreening(){
  try{
    const r=await fetch('/api/screening');
    const data=await r.json();
    renderScreening(data);
  }catch(e){console.error('Screening load error:',e)}
}
loadData();
loadScreening();
loadTradingConfig();
loadTradingLog();
setInterval(loadData,60000);
setInterval(loadScreening,300000);
setInterval(loadTradingConfig,60000);
setInterval(loadTradingLog,60000);
</script></body></html>
"""


@app.route("/")
def index():
    return app.response_class(
        render_template_string(HTML),
        mimetype="text/html; charset=utf-8"
    )


if __name__ == "__main__":
    if not get_token():
        log.error("ВНИМАНИЕ: API-токен не найден!")
    else:
        log.info("API-токен найден.")
    _start_background_if_needed()
    port = int(os.environ.get("PORT", "5000"))
    log.info("MARKUS TRADE запускается на порту %s", port)
    app.run(host="0.0.0.0", port=port, debug=False)
