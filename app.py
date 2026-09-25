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

# ============================================================
# MARKUS TRADE — CLEAN 4H VERSION
# ============================================================

APP_NAME = "Markus Trade"
API_BASE = "https://invest-public-api.tbank.ru/rest"

FIND_INSTRUMENT_URL = API_BASE + "/tinkoff.public.invest.api.contract.v1.InstrumentsService/FindInstrument"
FUTURES_URL = API_BASE + "/tinkoff.public.invest.api.contract.v1.InstrumentsService/Futures"
SHARES_URL = API_BASE + "/tinkoff.public.invest.api.contract.v1.InstrumentsService/Shares"
CANDLES_URL = API_BASE + "/tinkoff.public.invest.api.contract.v1.MarketDataService/GetCandles"

REQUEST_TIMEOUT = 30
CANDLE_INTERVAL = "CANDLE_INTERVAL_4_HOUR"
HISTORY_DAYS = 60
HISTORY_HOURS = HISTORY_DAYS * 24
UPDATE_SECONDS = 300

POSITION_SIZE_RUBLES = 100000.0
BUY_COMMISSION_PERCENT = 0.10
SELL_COMMISSION_PERCENT = 0.10
TAX_PERCENT = 13.0
HISTORY_FILE = "trade_history.json"

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
warnings.filterwarnings("ignore")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("MARKUS_TRADE")

app = Flask(__name__)

# ============================================================
# TOKEN / API
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
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        },
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


# ============================================================
# HELPERS
# ============================================================

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
# FUTURES
# ============================================================

def get_all_futures():
    for status in ("INSTRUMENT_STATUS_BASE", "INSTRUMENT_STATUS_ALL"):
        try:
            data = api_post(FUTURES_URL, {"instrumentStatus": status})
            futures = data.get("futures", [])
            if isinstance(futures, list):
                return futures
        except Exception as exc:
            log.warning("Не удалось получить список фьючерсов (%s): %s", status, exc)
    return []


def matches_future(future, prefix):
    ticker = get_string(future, "ticker").upper()
    name = get_string(future, "name").upper()
    basic_asset = get_string(future, "basicAsset").upper()
    text = " ".join((ticker, name, basic_asset))

    groups = {
        "CR": ("CR", "CNY", "YUAN", "CNH", "ЮАН", "КИТАЙ"),
        "GD": ("GD", "GOLD", "ЗОЛОТ"),
        "BR": ("BR", "BRENT", "НЕФТ"),
    }
    return any(word in text for word in groups.get(prefix.upper(), (prefix.upper(),)))


def find_active_future(prefix):
    aliases = {
        "CR": ("CR", "CNY", "юань", "CNY/RUB"),
        "GD": ("GD", "GOLD", "золото"),
        "BR": ("BR", "BRENT", "нефть"),
    }.get(prefix, (prefix,))

    candidates = []
    now = datetime.now(timezone.utc)

    for query in aliases:
        try:
            data = api_post(
                FIND_INSTRUMENT_URL,
                {
                    "query": query,
                    "instrumentKind": "INSTRUMENT_TYPE_FUTURES",
                    "apiTradeAvailableFlag": True,
                },
            )
        except Exception as exc:
            log.warning("FindInstrument фьючерс %s: %s", query, exc)
            continue

        for item in data.get("instruments", []):
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

    unique = {item["instrument_uid"]: item for item in candidates}
    candidates = list(unique.values())
    if not candidates:
        return None

    def expiry(item):
        return item["last_trade"] or datetime.max.replace(tzinfo=timezone.utc)

    candidates.sort(key=expiry)
    return candidates[0]


# ============================================================
# SHARES
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
            data = api_post(
                FIND_INSTRUMENT_URL,
                {
                    "query": query,
                    "instrumentKind": "INSTRUMENT_TYPE_SHARE",
                    "apiTradeAvailableFlag": True,
                },
            )
        except Exception as exc:
            log.warning("FindInstrument акции %s: %s", query, exc)
            continue

        for item in data.get("instruments", []):
            if not isinstance(item, dict):
                continue

            ticker = get_string(item, "ticker").upper()
            name = get_string(item, "name").upper()
            figi = get_string(item, "figi").upper()
            wanted = stock["code"].upper()

            if wanted not in ticker and wanted not in figi and stock["title"].upper() not in name:
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

    unique = {item["instrument_uid"]: item for item in candidates}
    candidates = list(unique.values())
    if not candidates:
        return None

    exact = [x for x in candidates if x["ticker"].upper() == stock["code"].upper()]
    return exact[0] if exact else candidates[0]


# ============================================================
# CANDLES — CHUNKED TO AVOID T-BANK PERIOD LIMIT
# ============================================================

def get_candles(instrument_uid):
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=HISTORY_DAYS)

    all_candles = []
    cursor = start
    chunk = timedelta(days=7)

    while cursor < now:
        chunk_end = min(cursor + chunk, now)
        payload = {
            "from": cursor.isoformat(),
            "to": chunk_end.isoformat(),
            "interval": CANDLE_INTERVAL,
            "instrumentId": instrument_uid,
            "candleSourceType": "CANDLE_SOURCE_EXCHANGE",
        }
        data = api_post(CANDLES_URL, payload)
        candles = data.get("candles", [])
        if isinstance(candles, list):
            all_candles.extend(candles)
        cursor = chunk_end

    return all_candles


def normalize_candles(candles):
    result = []
    seen = set()

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
        if item["close"] <= 0:
            continue
        if item["time"] in seen:
            continue
        seen.add(item["time"])
        result.append(item)

    result.sort(key=lambda x: x["time"])
    return result


# ============================================================
# INDICATORS
# ============================================================

def ema(values, period):
    if len(values) < period or period <= 0:
        return None
    multiplier = 2.0 / (period + 1.0)
    value = sum(values[:period]) / period
    for price in values[period:]:
        value = (price - value) * multiplier + value
    return value


def bollinger_values(values, period=20, deviation=2.0):
    if len(values) < period:
        return None
    window = values[-period:]
    mean = sum(window) / period
    variance = sum((x - mean) ** 2 for x in window) / period
    std = variance ** 0.5
    return mean, mean + deviation * std, mean - deviation * std


# ============================================================
# STRATEGIES
# ============================================================

def no_signal(description="Сигнал не сформирован"):
    return {"signal": "Нет сигналов", "direction": "—", "description": description}


def user_strategy(candles):
    if len(candles) < 8:
        return no_signal("Недостаточно свечей")

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
        return {"signal": "SHORT", "direction": "Вниз", "description": "Твоя стратегия: растущие максимумы → разворот вниз"}
    if long_pattern:
        return {"signal": "LONG", "direction": "Вверх", "description": "Твоя стратегия: снижающиеся минимумы → разворот вверх"}
    return no_signal()


def ema_trend_strategy(candles):
    if len(candles) < 30:
        return no_signal("Недостаточно свечей для EMA")
    closes = [x["close"] for x in candles]
    e9 = ema(closes, 9)
    e21 = ema(closes, 21)
    if e9 is None or e21 is None:
        return no_signal()
    if e9 > e21 and closes[-1] > e9 and closes[-1] > closes[-2]:
        return {"signal": "LONG", "direction": "Вверх", "description": "EMA 9 выше EMA 21 и цена выше EMA 9"}
    if e9 < e21 and closes[-1] < e9 and closes[-1] < closes[-2]:
        return {"signal": "SHORT", "direction": "Вниз", "description": "EMA 9 ниже EMA 21 и цена ниже EMA 9"}
    return no_signal()


def breakout_strategy(candles):
    if len(candles) < 21:
        return no_signal("Недостаточно свечей для Breakout")
    prev = candles[-21:-1]
    last = candles[-1]
    high = max(x["high"] for x in prev)
    low = min(x["low"] for x in prev)
    if last["close"] > high:
        return {"signal": "LONG", "direction": "Вверх", "description": "Пробой максимума 20 предыдущих свечей"}
    if last["close"] < low:
        return {"signal": "SHORT", "direction": "Вниз", "description": "Пробой минимума 20 предыдущих свечей"}
    return no_signal()


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
    rsi = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    if rsi < 30 and closes[-1] > closes[-2]:
        return {"signal": "LONG", "direction": "Вверх", "description": f"RSI перепродан ({rsi:.1f}) и цена разворачивается вверх"}
    if rsi > 70 and closes[-1] < closes[-2]:
        return {"signal": "SHORT", "direction": "Вниз", "description": f"RSI перекуплен ({rsi:.1f}) и цена разворачивается вниз"}
    return no_signal()


def macd_strategy(candles):
    if len(candles) < 36:
        return no_signal("Недостаточно свечей для MACD")
    closes = [x["close"] for x in candles]
    fast = ema(closes, 12)
    slow = ema(closes, 26)
    prev_fast = ema(closes[:-1], 12)
    prev_slow = ema(closes[:-1], 26)
    if None in (fast, slow, prev_fast, prev_slow):
        return no_signal()
    if prev_fast <= prev_slow and fast > slow:
        return {"signal": "LONG", "direction": "Вверх", "description": "MACD пересек сигнальную линию вверх"}
    if prev_fast >= prev_slow and fast < slow:
        return {"signal": "SHORT", "direction": "Вниз", "description": "MACD пересек сигнальную линию вниз"}
    return no_signal()


def bollinger_strategy(candles):
    if len(candles) < 21:
        return no_signal("Недостаточно свечей для Bollinger")
    closes = [x["close"] for x in candles]
    values = bollinger_values(closes, 20, 2.0)
    if values is None:
        return no_signal()
    middle, upper, lower = values
    last = closes[-1]
    prev = closes[-2]
    if prev <= lower and last > prev:
        return {"signal": "LONG", "direction": "Вверх", "description": "Цена вышла вверх из нижней полосы Bollinger"}
    if prev >= upper and last < prev:
        return {"signal": "SHORT", "direction": "Вниз", "description": "Цена вышла вниз из верхней полосы Bollinger"}
    return no_signal(f"Bollinger: цена {last:.2f}, середина {middle:.2f}")


STRATEGIES = [
    {"name": "Твоя стратегия", "key": "user", "fn": user_strategy},
    {"name": "EMA Trend", "key": "ema", "fn": ema_trend_strategy},
    {"name": "Breakout", "key": "breakout", "fn": breakout_strategy},
    {"name": "RSI Reversal", "key": "rsi", "fn": rsi_strategy},
    {"name": "MACD", "key": "macd", "fn": macd_strategy},
    {"name": "Bollinger", "key": "bollinger", "fn": bollinger_strategy},
]


# ============================================================
# BACKTEST / STATISTICS
# ============================================================

def calculate_commission(amount, percent):
    return amount * percent / 100.0


def calculate_trade_result(direction, entry_price, exit_price):
    if entry_price <= 0 or exit_price <= 0:
        return None

    if direction == "LONG":
        change = (exit_price - entry_price) / entry_price * 100.0
    else:
        change = (entry_price - exit_price) / entry_price * 100.0

    gross = POSITION_SIZE_RUBLES * change / 100.0
    buy_commission = calculate_commission(POSITION_SIZE_RUBLES, BUY_COMMISSION_PERCENT)
    exit_amount = POSITION_SIZE_RUBLES * (exit_price / entry_price)
    sell_commission = calculate_commission(abs(exit_amount), SELL_COMMISSION_PERCENT)
    tax = gross * TAX_PERCENT / 100.0 if gross > 0 else 0.0
    net = gross - buy_commission - sell_commission - tax

    return {
        "price_change_percent": round(change, 4),
        "gross_result": round(gross, 2),
        "buy_commission": round(buy_commission, 2),
        "sell_commission": round(sell_commission, 2),
        "tax": round(tax, 2),
        "net_result": round(net, 2),
    }


def build_strategy_history(candles, instrument, title, strategy_fn):
    """Единственная функция backtest. Четыре аргумента — без дублирования."""
    if len(candles) < 8:
        return [], None

    trades = []
    position = None

    for i in range(7, len(candles)):
        window = candles[: i + 1]
        analysis = strategy_fn(window)
        signal = analysis.get("signal", "Нет сигналов")
        candle = candles[i]
        price = candle["close"]
        candle_time = candle["time"]

        if position is None:
            if signal in ("LONG", "SHORT"):
                position = {
                    "instrument": instrument,
                    "title": title,
                    "direction": signal,
                    "entry_price": price,
                    "entry_time": candle_time,
                }
            continue

        if signal == position["direction"] or signal == "Нет сигналов":
            continue

        if signal not in ("LONG", "SHORT"):
            continue

        result = calculate_trade_result(position["direction"], position["entry_price"], price)
        if result is None:
            position = None
            continue

        trade = {
            "id": len(trades) + 1,
            "instrument": instrument,
            "title": title,
            "direction": position["direction"],
            "entry_time": position["entry_time"],
            "exit_time": candle_time,
            "entry_price": round(position["entry_price"], 8),
            "exit_price": round(price, 8),
            "exit_signal": signal,
            **result,
        }
        trades.append(trade)

        position = {
            "instrument": instrument,
            "title": title,
            "direction": signal,
            "entry_price": price,
            "entry_time": candle_time,
        }

    return trades, position


def calculate_max_drawdown(trades):
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for trade in trades:
        equity += float(trade.get("net_result", 0))
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    return round(abs(max_dd), 2)


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
        "winrate": round(profitable / total * 100.0, 2),
        "gross": round(gross, 2),
        "commission": round(commission, 2),
        "tax": round(tax, 2),
        "net": round(net, 2),
    }


def make_selection_reason(stats, drawdown, trade_count, selected=False):
    if trade_count < 3:
        return "Мало закрытых сделок для уверенного сравнения"
    if selected:
        return (
            f"Выбрана по историческому тесту: {trade_count} сделок, "
            f"проходимость {stats['winrate']}%, чистый результат {stats['net']:.2f} ₽, "
            f"максимальная просадка {drawdown:.2f} ₽."
        )
    return (
        f"Исторический тест: {trade_count} сделок, "
        f"проходимость {stats['winrate']}%, чистый результат {stats['net']:.2f} ₽, "
        f"просадка {drawdown:.2f} ₽."
    )


def evaluate_all_strategies(candles, instrument, title):
    rows = []

    for strategy in STRATEGIES:
        trades, open_position = build_strategy_history(candles, instrument, title, strategy["fn"])
        stats = calculate_statistics(trades)
        drawdown = calculate_max_drawdown(trades)

        # Не прогноз: только сортировка по уже прошедшему тесту.
        if stats["total"] >= 3:
            score = stats["net"] - drawdown * 0.25 + stats["winrate"] * 10.0
        else:
            score = -1_000_000_000.0 + stats["total"] * 1000.0 + stats["net"]

        rows.append({
            "name": strategy["name"],
            "key": strategy["key"],
            "statistics": stats,
            "drawdown": drawdown,
            "score": round(score, 4),
            "trades": trades,
            "open_position": open_position,
        })

    rows.sort(key=lambda x: x["score"], reverse=True)
    best = rows[0] if rows else None

    if best is not None and best["statistics"]["total"] < 3:
        best = max(rows, key=lambda x: (x["statistics"]["total"], x["statistics"]["net"]))

    for row in rows:
        row["selection_reason"] = make_selection_reason(
            row["statistics"], row["drawdown"], row["statistics"]["total"], row is best
        )

    return rows, best


def strategy_signal_from_best(candles, best):
    if not best:
        return no_signal()
    for strategy in STRATEGIES:
        if strategy["key"] == best["key"]:
            return strategy["fn"](candles)
    return no_signal()


def analyze_strategy(candles):
    _, best = evaluate_all_strategies(candles, "instrument", "instrument")
    return strategy_signal_from_best(candles, best)


# ============================================================
# HISTORY FILE
# ============================================================

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
# INSTRUMENT STATUS
# ============================================================

def empty_status(item_type, prefix, title, emoji):
    return {
        "type": item_type,
        "prefix": prefix,
        "title": title,
        "emoji": emoji,
        "status": "Ошибка",
        "message": "",
        "ticker": "—",
        "uid": "—",
        "candles": 0,
        "strategy": {"signal": "Нет сигналов", "direction": "—", "description": ""},
        "selected_strategy": "—",
        "strategy_selection": [],
        "history": [],
        "statistics": calculate_statistics([]),
        "open_position": None,
    }


def get_future_status(prefix, title, emoji):
    result = empty_status("future", prefix, title, emoji)
    try:
        future = find_active_future(prefix)
        if not future:
            result["message"] = "Актуальный контракт не найден"
            return result

        result["ticker"] = future["ticker"]
        result["uid"] = future["instrument_uid"]
        candles = normalize_candles(get_candles(future["instrument_uid"]))
        result["candles"] = len(candles)

        if not candles:
            result["message"] = "Свечей 0"
            return result

        rankings, best = evaluate_all_strategies(candles, prefix, title)
        result["strategy"] = strategy_signal_from_best(candles, best)
        result["selected_strategy"] = best["name"] if best else "—"
        result["strategy_selection"] = rankings
        result["history"] = best["trades"] if best else []
        result["open_position"] = best["open_position"] if best else None
        result["statistics"] = best["statistics"] if best else calculate_statistics([])
        result["status"] = "OK"
        result["message"] = "Данные получены. Проверены все стратегии."
        return result
    except Exception as exc:
        log.exception("Ошибка %s", title)
        result["message"] = str(exc)
        return result


def get_share_status(stock):
    result = empty_status("share", stock["code"], stock["title"], stock["emoji"])
    try:
        share = find_share(stock)
        if not share:
            result["message"] = "Акция не найдена"
            return result

        result["ticker"] = share["ticker"]
        result["uid"] = share["instrument_uid"]
        candles = normalize_candles(get_candles(share["instrument_uid"]))
        result["candles"] = len(candles)

        if not candles:
            result["message"] = "Свечей 0"
            return result

        rankings, best = evaluate_all_strategies(candles, stock["code"], stock["title"])
        result["strategy"] = strategy_signal_from_best(candles, best)
        result["selected_strategy"] = best["name"] if best else "—"
        result["strategy_selection"] = rankings
        result["history"] = best["trades"] if best else []
        result["open_position"] = best["open_position"] if best else None
        result["statistics"] = best["statistics"] if best else calculate_statistics([])
        result["status"] = "OK"
        result["message"] = "Данные получены. Проверены все стратегии."
        return result
    except Exception as exc:
        log.exception("Ошибка акции %s", stock["title"])
        result["message"] = str(exc)
        return result


# ============================================================
# COLLECT DATA
# ============================================================

def collect_data():
    futures = [
        get_future_status("CR", "Юань", "¥"),
        get_future_status("GD", "Золото", "🥇"),
        get_future_status("BR", "Нефть Brent", "🛢️"),
    ]
    shares = [get_share_status(stock) for stock in STOCKS]
    instruments = futures + shares

    last_signal = {"title": "Нет сигналов", "signal": "—", "direction": "—", "description": ""}
    for item in instruments:
        signal = item["strategy"].get("signal")
        if signal in ("LONG", "SHORT"):
            last_signal = {
                "title": item["title"],
                "signal": signal,
                "direction": item["strategy"].get("direction", "—"),
                "description": item["strategy"].get("description", ""),
            }
            break

    all_trades = []
    for item in instruments:
        all_trades.extend(item.get("history", []))

    total_statistics = calculate_statistics(all_trades)

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
            "candle_interval": "4 часа",
            "history_days": HISTORY_DAYS,
            "exit_rule": "Только противоположный сигнал",
        },
    }


# ============================================================
# API ROUTES
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


# ============================================================
# HTML — UTF-8, RUSSIAN TEXT SAFE
# ============================================================

HTML = r"""
<!doctype html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Type" content="text/html; charset=UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Markus Trade</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:linear-gradient(135deg,#07090d,#10141c);color:#fff;font-family:Arial,sans-serif;min-height:100vh}
.container{width:95%;max-width:1400px;margin:0 auto;padding:25px 0 50px}
.header{display:flex;justify-content:space-between;align-items:center;margin-bottom:25px}
.logo{font-size:28px;font-weight:800;letter-spacing:1px}.logo span{color:#d7aa52}.updated{color:#8c96a8;font-size:13px}
.section-title{margin:28px 0 14px;font-size:23px}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:18px}
.card{background:rgba(22,27,36,.95);border:1px solid rgba(255,255,255,.08);border-radius:18px;padding:20px;box-shadow:0 15px 50px rgba(0,0,0,.25)}
.card h2{margin-top:0;font-size:20px}.status{display:inline-block;padding:6px 10px;border-radius:20px;font-size:12px;background:#193d2b;color:#66e29a}.error{background:#442020;color:#ff8585}
.info{margin-top:15px;color:#aab3c2;font-size:13px;line-height:1.6}.signal{margin-top:15px;padding:14px;border-radius:14px;background:#111720;font-size:18px;font-weight:bold}.long{color:#52e58a}.short{color:#ff6666}.none{color:#9ca5b4}
.strategy-box{margin-top:14px;background:#0d1219;border-radius:12px;padding:12px;font-size:12px;overflow-x:auto}.strategy-row{display:grid;grid-template-columns:1.4fr .65fr .55fr .9fr .9fr;gap:7px;padding:8px 0;border-bottom:1px solid rgba(255,255,255,.06);color:#b8c0cc}.strategy-row:last-child{border-bottom:0}.reason{grid-column:1/-1;color:#7f8a9d;font-size:11px;padding-top:2px}
.statistics{margin-top:25px}.stats-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.stat{background:#111720;padding:16px;border-radius:14px}.stat-title{font-size:12px;color:#8f99aa;margin-bottom:7px}.stat-value{font-size:20px;font-weight:bold}
.history{margin-top:25px}.table-wrap{overflow-x:auto}table{width:100%;border-collapse:collapse;min-width:900px}th,td{padding:11px;border-bottom:1px solid rgba(255,255,255,.07);text-align:left;font-size:13px}th{color:#9ca5b4;font-weight:normal}.positive{color:#52e58a;font-weight:bold}.negative{color:#ff6666;font-weight:bold}
.settings{margin-top:20px;color:#858fa0;font-size:13px;line-height:1.7}button{margin-top:20px;border:none;border-radius:12px;padding:12px 20px;background:#d7aa52;color:#111;font-weight:bold;cursor:pointer}
@media(max-width:900px){.grid{grid-template-columns:1fr}.stats-grid{grid-template-columns:repeat(2,1fr)}}
@media(max-width:600px){.strategy-row{grid-template-columns:1fr 1fr}.strategy-row span:nth-child(n+3){font-size:11px}}
</style>
</head>
<body>
<div class="container">
<div class="header"><div class="logo">MARKUS <span>TRADE</span></div><div class="updated" id="updated">Загрузка...</div></div>
<div class="section-title">📊 ФЬЮЧЕРСЫ — 4 ЧАСА</div><div class="grid" id="futures"></div>
<div class="section-title">📈 АКЦИИ — 4 ЧАСА</div><div class="grid" id="shares"></div>
<div class="card statistics"><h2>📊 Общая статистика</h2><div class="stats-grid" id="statistics"></div></div>
<div class="card history"><h2>📜 История сделок</h2><div class="table-wrap" id="history"></div></div>
<div class="card settings"><h2>⚙️ Настройки</h2>
Размер виртуальной позиции: <b id="positionSize">—</b> ₽<br>
Комиссия покупки: <b id="buyCommission">—</b>%<br>
Комиссия продажи: <b id="sellCommission">—</b>%<br>
Налог: <b id="tax">—</b>%<br>
Таймфрейм: <b id="interval">—</b><br>
История: <b id="historyDays">—</b> дней<br>
Выход из сделки: <b id="exitRule">—</b><br>
<button onclick="loadData()">🔄 Обновить сейчас</button></div>
</div>
<script>
function money(v){if(v===undefined||v===null)return "0.00";return Number(v).toLocaleString("ru-RU",{minimumFractionDigits:2,maximumFractionDigits:2});}
function esc(v){return String(v??"").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;").replaceAll('"',"&quot;");}
function signalClass(s){if(s==="LONG")return "long";if(s==="SHORT")return "short";return "none";}
function renderInstrumentCard(item){
 const signal=item.strategy?.signal||"Нет сигналов";const statusClass=item.status==="OK"?"status":"status error";const stats=item.statistics||{};
 let openPosition="Нет";if(item.open_position)openPosition=item.open_position.direction+" от "+item.open_position.entry_price;
 const rows=(item.strategy_selection||[]).map((r,i)=>`<div class="strategy-row"><span>${i+1}. ${esc(r.name)}</span><span>${r.statistics.total} сделок</span><span>${r.statistics.winrate}%</span><span class="${r.statistics.net>=0?"positive":"negative"}">${money(r.statistics.net)} ₽</span><span>DD ${money(r.drawdown)} ₽</span><div class="reason">${esc(r.selection_reason||"")}</div></div>`).join("");
 return `<div class="card"><h2>${esc(item.emoji)} ${esc(item.title)}</h2><span class="${statusClass}">${esc(item.status)}</span><div class="info">${esc(item.message)}</div><div class="info">Тикер: <b>${esc(item.ticker)}</b><br>UID: <b>${esc(item.uid)}</b><br>4H-свечей: <b>${item.candles}</b></div><div class="signal ${signalClass(signal)}">${esc(signal)}</div><div class="info">${esc(item.strategy?.description||"")}</div><div class="info"><b>🤖 Выбрана стратегия: ${esc(item.selected_strategy||"—")}</b><br>Закрытых сделок: <b>${stats.total||0}</b><br>Проходимость: <b>${stats.winrate||0}%</b><br>Прибыльных: <b>${stats.profitable||0}</b><br>Убыточных: <b>${stats.losing||0}</b><br>Чистый результат: <b>${money(stats.net)} ₽</b><br>Открытая позиция: <b>${esc(openPosition)}</b></div><div class="strategy-box"><b>🔬 Проверка всех стратегий</b>${rows}</div></div>`;
}
function renderFutures(data){document.getElementById("futures").innerHTML=(data.futures||[]).map(renderInstrumentCard).join("");}
function renderShares(data){document.getElementById("shares").innerHTML=(data.shares||[]).map(renderInstrumentCard).join("");}
function renderStatistics(s){document.getElementById("statistics").innerHTML=`<div class="stat"><div class="stat-title">Всего сделок</div><div class="stat-value">${s.total}</div></div><div class="stat"><div class="stat-title">Прибыльных</div><div class="stat-value">${s.profitable}</div></div><div class="stat"><div class="stat-title">Убыточных</div><div class="stat-value">${s.losing}</div></div><div class="stat"><div class="stat-title">Проходимость</div><div class="stat-value">${s.winrate}%</div></div><div class="stat"><div class="stat-title">До расходов</div><div class="stat-value">${money(s.gross)} ₽</div></div><div class="stat"><div class="stat-title">Комиссии</div><div class="stat-value">${money(s.commission)} ₽</div></div><div class="stat"><div class="stat-title">Налог</div><div class="stat-value">${money(s.tax)} ₽</div></div><div class="stat"><div class="stat-title">ЧИСТЫЙ РЕЗУЛЬТАТ</div><div class="stat-value ${s.net>=0?"positive":"negative"}">${money(s.net)} ₽</div></div>`;}
function renderHistory(data){let all=[];[...(data.futures||[]),...(data.shares||[])].forEach(x=>{if(x.history)all=all.concat(x.history);});all.sort((a,b)=>new Date(b.exit_time)-new Date(a.exit_time));const c=document.getElementById("history");if(!all.length){c.innerHTML="Пока закрытых сделок нет.";return;}let html=`<table><thead><tr><th>Инструмент</th><th>Направление</th><th>Вход</th><th>Выход</th><th>Цена входа</th><th>Цена выхода</th><th>Результат</th><th>Комиссия</th><th>Налог</th><th>Чистый результат</th></tr></thead><tbody>`;all.slice(0,100).forEach(t=>{const net=Number(t.net_result||0),commission=Number(t.buy_commission||0)+Number(t.sell_commission||0);html+=`<tr><td>${esc(t.title)}</td><td>${esc(t.direction)}</td><td>${esc(t.entry_time)}</td><td>${esc(t.exit_time)}</td><td>${esc(t.entry_price)}</td><td>${esc(t.exit_price)}</td><td>${money(t.gross_result)} ₽</td><td>${money(commission)} ₽</td><td>${money(t.tax)} ₽</td><td class="${net>=0?"positive":"negative"}">${money(net)} ₽</td></tr>`;});html+=`</tbody></table>`;c.innerHTML=html;}
async function loadData(){try{const response=await fetch("/api/status",{cache:"no-store"});const data=await response.json();if(data.error){document.getElementById("updated").textContent="Ошибка: "+data.error;return;}renderFutures(data);renderShares(data);renderStatistics(data.statistics);renderHistory(data);document.getElementById("updated").textContent="Обновлено: "+new Date(data.updated).toLocaleString("ru-RU");document.getElementById("positionSize").textContent=money(data.settings.position_size);document.getElementById("buyCommission").textContent=data.settings.buy_commission;document.getElementById("sellCommission").textContent=data.settings.sell_commission;document.getElementById("tax").textContent=data.settings.tax;document.getElementById("interval").textContent=data.settings.candle_interval;document.getElementById("historyDays").textContent=data.settings.history_days;document.getElementById("exitRule").textContent=data.settings.exit_rule;}catch(error){document.getElementById("updated").textContent="Ошибка загрузки: "+error;console.error(error);}}
loadData();setInterval(loadData,60000);
</script>
</body></html>
"""


@app.route("/")
def index():
    return render_template_string(HTML)


def background_monitor():
    while True:
        try:
            data = collect_data()
            log.info("MARKUS TRADE | обновление данных")
            for item in data["futures"] + data["shares"]:
                log.info(
                    "%s | ticker=%s | candles=%s | signal=%s",
                    item["title"], item["ticker"], item["candles"], item["strategy"]["signal"],
                )
            stats = data["statistics"]
            log.info(
                "СТАТИСТИКА | сделок=%s | winrate=%s%% | чистый=%s ₽",
                stats["total"], stats["winrate"], stats["net"],
            )
        except Exception as exc:
            log.exception("Ошибка фонового мониторинга: %s", exc)
        time.sleep(UPDATE_SECONDS)


if __name__ == "__main__":
    token = get_token()
    if not token:
        log.warning("API-токен не найден. Добавь TINKOFF_TOKEN в переменные окружения.")
    else:
        log.info("API-токен найден.")

    monitor_thread = threading.Thread(target=background_monitor, daemon=True)
    monitor_thread.start()

    port = int(os.environ.get("PORT", "5000"))
    log.info("%s запускается на порту %s", APP_NAME, port)
    app.run(host="0.0.0.0", port=port, debug=False)
