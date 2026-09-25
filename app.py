import os
import json
import time
import logging
import threading
import warnings
from datetime import datetime, timedelta, timezone

import requests
import urllib3
from flask import Flask, jsonify, render_template

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
        "CR": ["CR", "CNY", "YUAN", "CNH", "ЮАНЬ", "КИТАЙ"],
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

            instruments = data.get("instruments", [])
            if not isinstance(instruments, list):
                continue

            for item in instruments:
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


def no_signal(description="Сигнал не сформирован"):
    return {"signal": "Нет сигналов", "direction": "—", "description": description}


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


STRATEGIES = [
    {"name": "Твоя стратегия", "key": "user", "fn": user_strategy, "min_bars": 8},
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


def build_strategy_history(candles, instrument, title, strategy_fn):
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
        signal = analysis.get("signal", "Нет сигналов")
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

        if signal == current_position["direction"] or signal == "Нет сигналов":
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
            score = (stats["net"] / (1.0 + drawdown)) * 100 + stats["winrate"] * 2
            eligible = True
            reason = "Есть минимум 3 закрытые сделки; учитываются чистый результат, проходимость и просадка."
        else:
            score = -1e12 + stats["total"] * 1000 + stats["net"]
            eligible = False
            reason = f"Недостаточно закрытых сделок для надёжного сравнения: {stats['total']} из {MIN_BACKTEST_TRADES}."

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
            f"Временно выбрана стратегия с наибольшим количеством исторических сделок ({best['statistics']['total']}). "
            "Это не означает, что она доказанно лучше остальных."
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
        log.warning("Ошибка чтения истории: %s", exc)
        return []


def save_history(history):
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        log.error("Ошибка сохранения истории: %s", exc)


def base_result(kind, code, title, emoji):
    return {
        "type": kind, "prefix": code, "title": title, "emoji": emoji,
        "status": "Ошибка", "message": "", "ticker": "—",
        "uid": "—", "candles": 0,
        "strategy": {"signal": "Нет сигналов", "direction": "—", "description": ""},
        "selected_strategy": "—", "selection_reason": "—",
        "strategy_selection": [],
        "history": [], "statistics": calculate_statistics([]),
        "open_position": None,
    }


def analyze_instrument(result, instrument, instrument_code, title):
    result["ticker"] = instrument["ticker"]
    result["uid"] = instrument["instrument_uid"]

    candles = normalize_candles(get_candles(instrument["instrument_uid"]))
    result["candles"] = len(candles)

    if not candles:
        result["message"] = "Свечей 0 — API не вернул историю для этого инструмента."
        return result

    rankings, best, selection_reason = evaluate_all_strategies(candles, instrument_code, title)
    result["strategy"] = strategy_signal_from_best(candles, best)
    result["selected_strategy"] = best["name"]
    result["selection_reason"] = selection_reason
    result["strategy_selection"] = rankings
    result["status"] = "OK"
    result["message"] = f"Загружено {len(candles)} свечей. Все {len(STRATEGIES)} стратегий протестированы на одной и той же истории."
    result["history"] = best["trades"]
    result["open_position"] = best["open_position"]
    result["statistics"] = best["statistics"]
    return result


def get_future_status(prefix, title, emoji):
    result = base_result("future", prefix, title, emoji)
    try:
        future = find_active_future(prefix)
        if not future:
            result["message"] = "Актуальный контракт не найден."
            return result
        return analyze_instrument(result, future, prefix, title)
    except Exception as exc:
        log.exception("Ошибка %s", title)
        result["message"] = str(exc)
        return result


def get_share_status(stock):
    result = base_result("share", stock["code"], stock["title"], stock["emoji"])
    try:
        share = find_share(stock)
        if not share:
            result["message"] = "Акция не найдена."
            return result
        return analyze_instrument(result, share, stock["code"], stock["title"])
    except Exception as exc:
        log.exception("Ошибка акции %s", stock["title"])
        result["message"] = str(exc)
        return result


def collect_data():
    futures = [
        get_future_status("CR", "Юань", "¥"),
        get_future_status("GD", "Золото", "🥇"),
        get_future_status("BR", "Нефть Brent", "🛢️"),
    ]
    shares = [get_share_status(stock) for stock in STOCKS]
    instruments = futures + shares

    all_trades = []
    for item in instruments:
        all_trades.extend(item.get("history", []))

    total_statistics = calculate_statistics(all_trades)

    last_signal = {"title": "Нет сигналов", "signal": "—", "direction": "—", "description": ""}
    for item in instruments:
        signal = item["strategy"].get("signal")
        if signal in ("LONG", "SHORT"):
            last_signal = {
                "title": item["title"], "signal": signal,
                "direction": item["strategy"].get("direction", "—"),
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
            "candle_interval": "4 часа",
            "history_days": HISTORY_HOURS // 24,
            "exit_rule": "Противоположный сигнал",
            "strategies_count": len(STRATEGIES),
        },
    }


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


@app.route("/")
def index():
    return render_template("index.html")


def background_monitor():
    while True:
        try:
            data = collect_data()
            log.info("MARKUS TRADE | обновление данных")
            for item in data["futures"] + data["shares
