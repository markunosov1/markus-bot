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
# MARKUS TRADE
# ============================================================
APP_NAME = "Markus Trade"
API_BASE = "https://tbank.ru"
FIND_INSTRUMENT_URL = API_BASE + "/tinkoff.public.invest.api.contract.v1.InstrumentsService/FindInstrument"
FUTURES_URL = API_BASE + "/tinkoff.public.invest.api.contract.v1.InstrumentsService/Futures"
SHARES_URL = API_BASE + "/tinkoff.public.invest.api.contract.v1.InstrumentsService/Shares"
CANDLES_URL = API_BASE + "/tinkoff.public.invest.api.contract.v1.MarketDataService/GetCandles"

# ============================================================
# НАСТРОЙКИ
# ============================================================
REQUEST_TIMEOUT = 30
CANDLE_INTERVAL = "CANDLE_INTERVAL_4_HOUR"
HISTORY_HOURS = 24 * 60
UPDATE_SECONDS = 300

# ============================================================
# ТОРГОВЫЕ НАСТРОЙКИ
# ============================================================
POSITION_SIZE_RUBLES = 100000.0
BUY_COMMISSION_PERCENT = 0.10
SELL_COMMISSION_PERCENT = 0.10
TAX_PERCENT = 13.0
HISTORY_FILE = "trade_history.json"

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
warnings.filterwarnings("ignore")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("MARKUS_TRADE")

app = Flask(__name__)

def get_token():
    possible_names = ["TINKOFF_TOKEN", "TINVEST_TOKEN", "T_BANK_TOKEN", "API_TOKEN", "TOKEN"]
    for name in possible_names:
        value = os.environ.get(name)
        if value:
            value = value.strip()
            if value: return value
    return None

def api_post(url, payload):
    token = get_token()
    if not token:
        raise RuntimeError("API-токен не найден. Проверь переменную TINKOFF_TOKEN.")
    headers = {
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
    }
    response = requests.post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT, verify=False)
    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:1000]}")
    try:
        return response.json()
    except Exception:
        raise RuntimeError("T-Bank вернул ответ, который не удалось прочитать как JSON.")

def get_string(obj, key):
    value = obj.get(key)
    return "" if value is None else str(value)

def parse_date(value):
    if not value: return None
    try:
        text = str(value)
        if text.endswith("Z"): text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None

def quotation_to_float(value):
    if value is None: return 0.0
    if isinstance(value, (int, float)): return float(value)
    if isinstance(value, dict):
        units = value.get("units", 0)
        nano = value.get("nano", 0)
        try: return float(units) + float(nano) / 1_000_000_000
        except Exception: return 0.0
    try: return float(str(value))
    except Exception: return 0.0

# ============================================================
# ФЬЮЧЕРСЫ
# ============================================================
def get_all_futures():
    payload = {"instrumentStatus": "INSTRUMENT_STATUS_BASE"}
    try: data = api_post(FUTURES_URL, payload)
    except Exception:
        payload = {"instrumentStatus": "INSTRUMENT_STATUS_ALL"}
        data = api_post(FUTURES_URL, payload)
    futures = data.get("futures", [])
    return futures if isinstance(futures, list) else []

def matches_future(future, prefix):
    ticker = get_string(future, "ticker").upper()
    name = get_string(future, "name").upper()
    basic_asset = get_string(future, "basicAsset").upper()
    text = " ".join([ticker, name, basic_asset])
    prefix = prefix.upper()
    if prefix == "CR":
        return any(word in text for word in ["CR", "CNY", "YUAN", "CNH", "ЮАН", "КИТАЙ"])
    if prefix == "GD":
        return any(word in text for word in ["GD", "GOLD", "ЗОЛОТ"])
    if prefix == "BR":
        return any(word in text for word in ["BR", "BRENT", "НЕФТ"])
    return False

def find_active_future(prefix):
    queries = [prefix]
    if prefix == "CR": queries = ["CR", "CNY", "юань", "CNY/RUB"]
    elif prefix == "GD": queries = ["GD", "GOLD", "золото"]
    elif prefix == "BR": queries = ["BR", "BRENT", "нефть"]
    candidates = []
    now = datetime.now(timezone.utc)
    for query in queries:
        try:
            payload = {
                "query": query,
                "instrumentKind": "INSTRUMENT_TYPE_FUTURES",
                "apiTradeAvailableFlag": True,
            }
            data = api_post(FIND_INSTRUMENT_URL, payload)
        except Exception as e:
            log.warning("FindInstrument %s: %s", query, e)
            continue
        instruments = data.get("instruments", [])
        if not isinstance(instruments, list): continue
        for item in instruments:
            if not isinstance(item, dict) or not matches_future(item, prefix): continue
            instrument_uid = item.get("instrumentUid") or item.get("uid")
            if not instrument_uid: continue
            first_trade = parse_date(item.get("firstTradeDate"))
            last_trade = parse_date(item.get("lastTradeDate"))
            if first_trade and now < first_trade: continue
            if last_trade and now > last_trade: continue
            candidates.append({
                "ticker": get_string(item, "ticker"),
                "name": get_string(item, "name"),
                "uid": instrument_uid,
                "instrument_uid": instrument_uid,
                "first_trade": first_trade,
                "last_trade": last_trade,
                "class_code": get_string(item, "classCode"),
                "basic_asset": get_string(item, "basicAsset"),
            })
    unique = {item["instrument_uid"]: item for item in candidates}
    candidates = list(unique.values())
    if not candidates: return None
    candidates.sort(key=lambda x: x.get("last_trade") or datetime.max.replace(tzinfo=timezone.utc))
    return candidates

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
            payload = {
                "query": query,
                "instrumentKind": "INSTRUMENT_TYPE_SHARE",
                "apiTradeAvailableFlag": True,
            }
            data = api_post(FIND_INSTRUMENT_URL, payload)
        except Exception as e:
            log.warning("FindInstrument акции %s: %s", query, e)
            continue
        instruments = data.get("instruments", [])
        if not isinstance(instruments, list): continue
        for item in instruments:
            if not isinstance(item, dict): continue
            ticker = get_string(item, "ticker").upper()
            name = get_string(item, "name").upper()
            figi = get_string(item, "figi").upper()
            wanted = stock["code"].upper()
            if wanted not in ticker and wanted not in figi:
                if stock["title"].upper() not in name: continue
            uid = item.get("instrumentUid") or item.get("uid")
            if not uid: continue
            first_trade = parse_date(item.get("firstTradeDate"))
            last_trade = parse_date(item.get("lastTradeDate"))
            if first_trade and now < first_trade: continue
            if last_trade and now > last_trade: continue
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
    if not candidates: return None
    exact = [x for x in candidates if x["ticker"].upper() == stock["code"].upper()]
    return exact if exact else candidates

def get_candles(instrument_uid):
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=HISTORY_HOURS)
    payload = {
        "from": start.isoformat(),
        "to": now.isoformat(),
        "interval": CANDLE_INTERVAL,
        "instrumentId": instrument_uid,
        "candleSourceType": "CANDLE_SOURCE_EXCHANGE",
    }
    data = api_post(CANDLES_URL, payload)
    candles = data.get("candles", [])
    return candles if isinstance(candles, list) else []

def normalize_candles(candles):
    result = []
    for candle in candles:
        if not isinstance(candle, dict): continue
        dt = parse_date(candle.get("time"))
        if not dt: continue
        item = {
            "time": dt.isoformat(),
            "open": quotation_to_float(candle.get("open")),
            "high": quotation_to_float(candle.get("high")),
            "low": quotation_to_float(candle.get("low")),
            "close": quotation_to_float(candle.get("close")),
            "volume": quotation_to_float(candle.get("volume")),
        }
        if item["close"] <= 0: continue
        result.append(item)
    result.sort(key=lambda x: x["time"])
    return result



# ============================================================
# НАБОР СТРАТЕГИЙ И АВТОПОДБОР
# ============================================================

def no_signal(description="Сигнал не сформирован"):
    return {"signal":"Нет сигналов", "direction":"—", "description":description}


def user_strategy(candles):
    if len(candles) < 8:
        return no_signal("Недостаточно свечей")
    last=candles[-8:]
    highs=[x["high"] for x in last]
    lows=[x["low"] for x in last]
    closes=[x["close"] for x in last]
    short_pattern=(highs[3]>highs[2] and highs[4]>highs[3] and highs[5]>highs[4]
                   and closes[-1]<closes[-2] and closes[-2]<closes[-3])
    long_pattern=(lows[3]<lows[2] and lows[4]<lows[3] and lows[5]<lows[4]
                  and closes[-1]>closes[-2] and closes[-2]>closes[-3])
    if short_pattern:
        return {"signal":"SHORT","direction":"Вниз","description":"Твоя стратегия: растущие максимумы → разворот вниз"}
    if long_pattern:
        return {"signal":"LONG","direction":"Вверх","description":"Твоя стратегия: снижающиеся минимумы → разворот вверх"}
    return no_signal()


def ema(values, period):
    if len(values)<period: return None
    k=2/(period+1)
    value=sum(values[:period])/period
    for price in values[period:]: value=price*k+value*(1-k)
    return value


def ema_trend_strategy(candles):
    if len(candles)<30: return no_signal("Недостаточно свечей для EMA")
    closes=[x["close"] for x in candles]
    e9=ema(closes[-30:],9); e21=ema(closes[-30:],21)
    if e9 is None or e21 is None: return no_signal()
    if e9>e21 and closes[-1]>e9 and closes[-1]>closes[-2]:
        return {"signal":"LONG","direction":"Вверх","description":"EMA 9 выше EMA 21 и цена выше EMA 9"}
    if e9<e21 and closes[-1]<e9 and closes[-1]<closes[-2]:
        return {"signal":"SHORT","direction":"Вниз","description":"EMA 9 ниже EMA 21 и цена ниже EMA 9"}
    return no_signal()


def breakout_strategy(candles):
    if len(candles)<21: return no_signal("Недостаточно свечей для Breakout")
    prev=candles[-21:-1]; last=candles[-1]
    high=max(x["high"] for x in prev); low=min(x["low"] for x in prev)
    if last["close"]>high:
        return {"signal":"LONG","direction":"Вверх","description":"Пробой максимума 20 предыдущих свечей"}
    if last["close"]<low:
        return {"signal":"SHORT","direction":"Вниз","description":"Пробой минимума 20 предыдущих свечей"}
    return no_signal()


def rsi_strategy(candles):
    if len(candles)<16: return no_signal("Недостаточно свечей для RSI")
    closes=[x["close"] for x in candles[-15:]]
    gains=[]; losses=[]
    for a,b in zip(closes[:-1],closes[1:]):
        d=b-a; gains.append(max(d,0)); losses.append(max(-d,0))
    avg_gain=sum(gains)/len(gains); avg_loss=sum(losses)/len(losses)
    if avg_loss==0: rsi=100
    else: rsi=100-(100/(1+(avg_gain/avg_loss)))
    if rsi<30 and closes[-1]>closes[-2]:
        return {"signal":"LONG","direction":"Вверх","description":f"RSI перепродан ({rsi:.1f}) и цена разворачивается вверх"}
    if rsi>70 and closes[-1]<closes[-2]:
        return {"signal":"SHORT","direction":"Вниз","description":f"RSI перекуплен ({rsi:.1f}) и цена разворачивается вниз"}
    return no_signal()


def macd_strategy(candles):
    if len(candles)<35: return no_signal("Недостаточно свечей для MACD")
    closes=[x["close"] for x in candles]
    fast=ema(closes[-35:],12); slow=ema(closes[-35:],26)
    if fast is None or slow is None: return no_signal()
    prev_fast=ema(closes[-36:-1],12) if len(closes)>=36 else None
    prev_slow=ema(closes[-36:-1],26) if len(closes)>=36 else None
    if prev_fast is not None and prev_slow is not None:
        if prev_fast<=prev_slow and fast>slow:
            return {"signal":"LONG","direction":"Вверх","description":"MACD пересек нулевую линию вверх"}
        if prev_fast>=prev_slow and fast<slow:
            return {"signal":"SHORT","direction":"Вниз","description":"MACD пересек нулевую линию вниз"}
    return no_signal()


def bollinger_strategy(candles):
    if len(candles)<21: return no_signal("Недостаточно свечей для Bollinger")
    closes=[x["close"] for x in candles[-20:]]; last=candles[-1]["close"]
    mean=sum(closes)/20
    variance=sum((x-mean)**2 for x in closes)/20
    sd=variance**0.5
    upper=mean+2*sd; lower=mean-2*sd
    if last<lower:
        return {"signal":"LONG","direction":"Вверх","description":"Цена ниже нижней полосы Bollinger"}
    if last>upper:
        return {"signal":"SHORT","direction":"Вниз","description":"Цена выше верхней полосы Bollinger"}
    return no_signal()


STRATEGIES=[
    {"name":"Твоя стратегия","key":"user","fn":user_strategy},
    {"name":"EMA Trend","key":"ema","fn":ema_trend_strategy},
    {"name":"Breakout","key":"breakout","fn":breakout_strategy},
    {"name":"RSI Reversal","key":"rsi","fn":rsi_strategy},
    {"name":"MACD","key":"macd","fn":macd_strategy},
    {"name":"Bollinger","key":"bollinger","fn":bollinger_strategy},
]


def calculate_max_drawdown(trades):
    equity=0.0; peak=0.0; max_dd=0.0
    for trade in trades:
        equity += float(trade.get("net_result",0))
        peak=max(peak,equity)
        max_dd=min(max_dd,equity-peak)
    return round(abs(max_dd),2)


def build_strategy_history(candles, instrument, title, strategy_fn):
    if len(candles) < 8:
        return [], None
    
    trades = []
    current_position = None
    
    # Определяем, является ли стратегия вашей авторской (по имени функции или ключу)
    # Ваша функция называется user_strategy
    is_user_strategy = (strategy_fn.__name__ == "user_strategy")
    
    for i in range(7, len(candles)):
        window = candles[:i + 1]
        analysis = strategy_fn(window)
        signal = analysis["signal"]
        
        candle = candles[i]
        price = candle["close"]
        candle_time = candle["time"]
        
        # 1. Если позиции нет — ищем сигнал на вход
        if current_position is None:
            if signal in ("LONG", "SHORT"):
                current_position = {
                    "instrument": instrument,
                    "title": title,
                    "direction": signal,
                    "entry_price": price,
                    "entry_time": candle_time
                }
            continue
            
        # 2. Если позиция открыта — проверяем условия выхода
        if is_user_strategy:
            # ЛОГИКА ДЛЯ ВАШЕЙ СТРАТЕГИИ: строгий выход только по противоположному сигналу
            if signal == current_position["direction"] or signal == "Нет сигналов":
                continue
            opposite_signal = (
                (current_position["direction"] == "LONG" and signal == "SHORT") or
                (current_position["direction"] == "SHORT" and signal == "LONG")
            )
        else:
            # ЛОГИКА ДЛЯ ОСТАЛЬНЫХ СТРАТЕГИЙ: выходим, если сигнал сменился ИЛИ пропал ("Нет сигналов")
            opposite_signal = (signal != current_position["direction"])
            
        # 3. Закрытие сделки, если условие выхода выполнено
        if opposite_signal:
            result = calculate_trade_result(
                current_position["direction"],
                current_position["entry_price"],
                price
            )
            
            if result is None:
                current_position = None
                continue
                
            trade = {
                "id": len(trades) + 1,
                "instrument": instrument,
                "title": title,
                "direction": current_position["direction"],
                "entry_time": current_position["entry_time"],
                "exit_time": candle_time,
                "entry_price": round(current_position["entry_price"], 8),
                "exit_price": round(price, 8),
                "exit_signal": signal,
                **result
            }
            trades.append(trade)
            
            # Если новый сигнал — это LONG или SHORT, сразу открываем новую позицию
            if signal in ("LONG", "SHORT"):
                current_position = {
                    "instrument": instrument,
                    "title": title,
                    "direction": signal,
                    "entry_price": price,
                    "entry_time": candle_time
                }
            else:
                current_position = None
                
    return trades, current_position



def evaluate_all_strategies(candles, instrument, title):
    rows=[]
    for strategy in STRATEGIES:
        trades, open_position=build_strategy_history(candles,instrument,title,strategy["fn"])
        stats=calculate_statistics(trades)
        drawdown=calculate_max_drawdown(trades)
        # Рейтинг не является прогнозом: это технический отбор по историческому тесту.
        # Требуем минимум 3 закрытые сделки; затем учитываем net, winrate и drawdown.
        if stats["total"]>=3:
            score=(stats["net"]/(1+drawdown))*100 + stats["winrate"]*2
        else:
            score=-1e9
        rows.append({"name":strategy["name"],"key":strategy["key"],"statistics":stats,
                     "drawdown":drawdown,"score":round(score,4),"trades":trades,
                     "open_position":open_position})
    rows.sort(key=lambda x:x["score"],reverse=True)
    best=rows[0]
    if best["statistics"]["total"]<3:
        # Если данных мало, выбираем стратегию с наибольшим числом сделок, но явно показываем это.
        best=max(rows,key=lambda x:(x["statistics"]["total"],x["statistics"]["net"]))
    return rows,best


def strategy_signal_from_best(candles,best):
    for strategy in STRATEGIES:
        if strategy["key"]==best["key"]:
            return strategy["fn"](candles)
    return no_signal()


def analyze_strategy(candles):
    # Совместимость со старым интерфейсом.
    _,best=evaluate_all_strategies(candles,"instrument","instrument")
    return strategy_signal_from_best(candles,best)


# ============================================================
# ИСТОРИЯ
# ============================================================

def load_history():
    if not os.path.exists(HISTORY_FILE):
        return []

    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, list):
            return data

    except Exception as e:
        log.warning("Ошибка чтения истории: %s", e)

    return []


def save_history(history):
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(
                history,
                f,
                ensure_ascii=False,
                indent=2,
            )
    except Exception as e:
        log.error("Ошибка сохранения истории: %s", e)


# ============================================================
# РАСЧЕТ СДЕЛКИ
# ============================================================

def calculate_commission(amount, percent):
    return amount * percent / 100.0


def calculate_trade_result(direction, entry_price, exit_price):
    if entry_price <= 0:
        return None

    if direction == "LONG":
        price_change_percent = (
            (exit_price - entry_price) / entry_price
        ) * 100
    else:
        price_change_percent = (
            (entry_price - exit_price) / entry_price
        ) * 100

    gross_result = POSITION_SIZE_RUBLES * price_change_percent / 100.0

    buy_commission = calculate_commission(
        POSITION_SIZE_RUBLES,
        BUY_COMMISSION_PERCENT,
    )

    exit_amount = POSITION_SIZE_RUBLES * (
        exit_price / entry_price
    )

    sell_commission = calculate_commission(
        abs(exit_amount),
        SELL_COMMISSION_PERCENT,
    )

    tax = 0.0

    if gross_result > 0:
        tax = gross_result * TAX_PERCENT / 100.0

    net_result = (
        gross_result
        - buy_commission
        - sell_commission
        - tax
    )

    return {
        "price_change_percent": round(price_change_percent, 4),
        "gross_result": round(gross_result, 2),
        "buy_commission": round(buy_commission, 2),
        "sell_commission": round(sell_commission, 2),
        "tax": round(tax, 2),
        "net_result": round(net_result, 2),
    }


# ============================================================
# ИСТОРИЯ СТРАТЕГИИ
# ============================================================

def build_strategy_history(candles, instrument, title, strategy_fn):
    if len(candles) < 8:
        return [], None

    trades = []
    current_position = None

    for i in range(7, len(candles)):
        # Используем срез из вашей новой логики скользящего окна
        window = candles[i - 7:i + 1]
        
        # ВЫЗЫВАЕМ ПЕРЕДАННУЮ СТРАТЕГИЮ ИЗ ЦИКЛА вместо фиксированной функции
        analysis = strategy_fn(window)
        signal = analysis["signal"]

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

        if signal == current_position["direction"]:
            continue

        opposite_signal = (
            current_position["direction"] == "LONG"
            and signal == "SHORT"
        ) or (
            current_position["direction"] == "SHORT"
            and signal == "LONG"
        )

        if opposite_signal:
            result = calculate_trade_result(
                current_position["direction"],
                current_position["entry_price"],
                price,
            )

            if result is None:
                current_position = None
                continue

            trade = {
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
            }

            trades.append(trade)

            current_position = {
                "instrument": instrument,
                "title": title,
                "direction": signal,
                "entry_price": price,
                "entry_time": candle_time,
            }

    return trades, current_position


# ============================================================
# СТАТИСТИКА
# ============================================================

def calculate_statistics(trades):
    total = len(trades)

    if total == 0:
        return {
            "total": 0,
            "profitable": 0,
            "losing": 0,
            "winrate": 0,
            "gross": 0,
            "commission": 0,
            "tax": 0,
            "net": 0,
        }

    profitable = sum(
        1 for trade in trades
        if trade.get("net_result", 0) > 0
    )

    losing = sum(
        1 for trade in trades
        if trade.get("net_result", 0) < 0
    )

    gross = sum(
        trade.get("gross_result", 0)
        for trade in trades
    )

    commission = sum(
        trade.get("buy_commission", 0)
        + trade.get("sell_commission", 0)
        for trade in trades
    )

    tax = sum(
        trade.get("tax", 0)
        for trade in trades
    )

    net = sum(
        trade.get("net_result", 0)
        for trade in trades
    )

    winrate = profitable / total * 100

    return {
        "total": total,
        "profitable": profitable,
        "losing": losing,
        "winrate": round(winrate, 2),
        "gross": round(gross, 2),
        "commission": round(commission, 2),
        "tax": round(tax, 2),
        "net": round(net, 2),
    }


# ============================================================
# АНАЛИЗ ФЬЮЧЕРСА
# ============================================================

def get_future_status(prefix, title, emoji):
    result = {
        "type": "future",
        "prefix": prefix,
        "title": title,
        "emoji": emoji,
        "status": "Ошибка",
        "message": "",
        "ticker": "—",
        "uid": "—",
        "candles": 0,
        "strategy": {
            "signal": "Нет сигналов",
            "direction": "—",
            "description": "",
        },
        "selected_strategy": "—",
        "strategy_selection": [],
        "history": [],
        "statistics": {},
        "open_position": None,
    }

    try:
        future = find_active_future(prefix)

        if not future:
            result["message"] = "Актуальный контракт не найден"
            return result

        result["ticker"] = future["ticker"]
        result["uid"] = future["instrument_uid"]

        candles = normalize_candles(
            get_candles(future["instrument_uid"])
        )

        result["candles"] = len(candles)

        if not candles:
            result["message"] = "Свечей 0"
            return result

        rankings, best = evaluate_all_strategies(candles, prefix, title)
        result["strategy"] = strategy_signal_from_best(candles, best)
        result["selected_strategy"] = best["name"]
        result["strategy_selection"] = rankings
        result["status"] = "OK"
        result["message"] = "Данные получены. Проверены все стратегии."
        result["history"] = best["trades"]
        result["open_position"] = best["open_position"]
        result["statistics"] = best["statistics"]

        return result

    except Exception as e:
        log.exception("Ошибка %s", title)
        result["message"] = str(e)
        return result


# ============================================================
# АНАЛИЗ АКЦИИ
# ============================================================

def get_share_status(stock):
    result = {
        "type": "share",
        "prefix": stock["code"],
        "title": stock["title"],
        "emoji": stock["emoji"],
        "status": "Ошибка",
        "message": "",
        "ticker": "—",
        "uid": "—",
        "candles": 0,
        "strategy": {
            "signal": "Нет сигналов",
            "direction": "—",
            "description": "",
        },
        "selected_strategy": "—",
        "strategy_selection": [],
        "history": [],
        "statistics": {},
        "open_position": None,
    }

    try:
        share = find_share(stock)

        if not share:
            result["message"] = "Акция не найдена"
            return result

        result["ticker"] = share["ticker"]
        result["uid"] = share["instrument_uid"]

        candles = normalize_candles(
            get_candles(share["instrument_uid"])
        )

        result["candles"] = len(candles)

        if not candles:
            result["message"] = "Свечей 0"
            return result

        rankings, best = evaluate_all_strategies(candles, stock["code"], stock["title"])
        result["strategy"] = strategy_signal_from_best(candles, best)
        result["selected_strategy"] = best["name"]
        result["strategy_selection"] = rankings
        result["status"] = "OK"
        result["message"] = "Данные получены. Проверены все стратегии."
        result["history"] = best["trades"]
        result["open_position"] = best["open_position"]
        result["statistics"] = best["statistics"]

        return result

    except Exception as e:
        log.exception("Ошибка акции %s", stock["title"])
        result["message"] = str(e)
        return result


# ============================================================
# ОБЩИЙ СБОР ДАННЫХ
# ============================================================

def collect_data():
    futures = [
        get_future_status("CR", "Юань", "¥"),
        get_future_status("GD", "Золото", "🥇"),
        get_future_status("BR", "Нефть Brent", "🛢️"),
    ]

    shares = [
        get_share_status(stock)
        for stock in STOCKS
    ]

    instruments = futures + shares

    last_signal = {
        "title": "Нет сигналов",
        "signal": "—",
        "direction": "—",
        "description": "",
    }

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

    # Сохраняем историю
    existing_history = load_history()

    existing_keys = set()

    for trade in existing_history:
        key = (
            trade.get("instrument"),
            trade.get("entry_time"),
            trade.get("exit_time"),
            trade.get("direction"),
        )
        existing_keys.add(key)

    for trade in all_trades:
        key = (
            trade.get("instrument"),
            trade.get("entry_time"),
            trade.get("exit_time"),
            trade.get("direction"),
        )

        if key not in existing_keys:
            existing_history.append(trade)
            existing_keys.add(key)

    existing_history = existing_history[-5000:]
    save_history(existing_history)

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
        },
    }


# ============================================================
# API
# ============================================================

@app.route("/api/status")
def api_status():
    try:
        return jsonify(collect_data())
    except Exception as e:
        log.exception("Ошибка /api/status")
        return jsonify({"error": str(e)}), 500


@app.route("/api/history")
def api_history():
    history = load_history()
    return jsonify({
        "count": len(history),
        "history": history,
    })


# ============================================================
# HTML
# ============================================================

HTML = r"""
<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Markus Trade</title>

<style>
*{box-sizing:border-box}

body{
    margin:0;
    background:linear-gradient(135deg,#07090d,#10141c);
    color:#fff;
    font-family:Arial,sans-serif;
    min-height:100vh;
}

.container{
    width:95%;
    max-width:1400px;
    margin:0 auto;
    padding:25px 0 50px;
}

.header{
    display:flex;
    justify-content:space-between;
    align-items:center;
    margin-bottom:25px;
}

.logo{
    font-size:28px;
    font-weight:800;
    letter-spacing:1px;
}

.logo span{color:#d7aa52}

.updated{
    color:#8c96a8;
    font-size:13px;
}

.section-title{
    margin:28px 0 14px;
    font-size:23px;
}

.grid{
    display:grid;
    grid-template-columns:repeat(3,1fr);
    gap:18px;
}

.card{
    background:rgba(22,27,36,.95);
    border:1px solid rgba(255,255,255,.08);
    border-radius:18px;
    padding:20px;
    box-shadow:0 15px 50px rgba(0,0,0,.25);
}

.card h2{
    margin-top:0;
    font-size:20px;
}

.status{
    display:inline-block;
    padding:6px 10px;
    border-radius:20px;
    font-size:12px;
    background:#193d2b;
    color:#66e29a;
}

.error{
    background:#442020;
    color:#ff8585;
}

.strategy-box{margin-top:14px;background:#0d1219;border-radius:12px;padding:12px;font-size:12px}.strategy-row{display:grid;grid-template-columns:1.6fr .7fr .6fr 1fr .9fr;gap:6px;padding:7px 0;border-bottom:1px solid rgba(255,255,255,.06);color:#b8c0cc}.strategy-row:last-child{border-bottom:0}@media(max-width:600px){.strategy-row{grid-template-columns:1fr 1fr}.strategy-row span:nth-child(n+3){font-size:11px}}

.info{
    margin-top:15px;
    color:#aab3c2;
    font-size:13px;
    line-height:1.6;
}

.signal{
    margin-top:15px;
    padding:14px;
    border-radius:14px;
    background:#111720;
    font-size:18px;
    font-weight:bold;
}

.long{color:#52e58a}
.short{color:#ff6666}
.none{color:#9ca5b4}

.statistics{
    margin-top:25px;
}

.stats-grid{
    display:grid;
    grid-template-columns:repeat(4,1fr);
    gap:12px;
}

.stat{
    background:#111720;
    padding:16px;
    border-radius:14px;
}

.stat-title{
    font-size:12px;
    color:#8f99aa;
    margin-bottom:7px;
}

.stat-value{
    font-size:20px;
    font-weight:bold;
}

.history{
    margin-top:25px;
}

.table-wrap{
    overflow-x:auto;
}

table{
    width:100%;
    border-collapse:collapse;
    min-width:900px;
}

th,td{
    padding:11px;
    border-bottom:1px solid rgba(255,255,255,.07);
    text-align:left;
    font-size:13px;
}

th{
    color:#9ca5b4;
    font-weight:normal;
}

.positive{
    color:#52e58a;
    font-weight:bold;
}

.negative{
    color:#ff6666;
    font-weight:bold;
}

.settings{
    margin-top:20px;
    color:#858fa0;
    font-size:13px;
    line-height:1.7;
}

button{
    margin-top:20px;
    border:none;
    border-radius:12px;
    padding:12px 20px;
    background:#d7aa52;
    color:#111;
    font-weight:bold;
    cursor:pointer;
}

@media(max-width:900px){
    .grid{grid-template-columns:1fr}
    .stats-grid{grid-template-columns:repeat(2,1fr)}
}
</style>
</head>

<body>
<div class="container">

<div class="header">
    <div class="logo">MARKUS <span>TRADE</span></div>
    <div class="updated" id="updated">Загрузка...</div>
</div>

<div class="section-title">📊 ФЬЮЧЕРСЫ — 4 ЧАСА</div>
<div class="grid" id="futures"></div>

<div class="section-title">📈 АКЦИИ — 4 ЧАСА</div>
<div class="grid" id="shares"></div>

<div class="card statistics">
    <h2>📊 Общая статистика</h2>
    <div class="stats-grid" id="statistics"></div>
</div>

<div class="card history">
    <h2>📜 История сделок</h2>
    <div class="table-wrap" id="history"></div>
</div>

<div class="card settings">
    <h2>⚙️ Настройки</h2>
    Размер виртуальной позиции:
    <b id="positionSize">—</b> ₽<br>
    Комиссия покупки:
    <b id="buyCommission">—</b>%<br>
    Комиссия продажи:
    <b id="sellCommission">—</b>%<br>
    Налог:
    <b id="tax">—</b>%<br>
    Таймфрейм:
    <b id="interval">—</b><br>
    История:
    <b id="historyDays">—</b> дней<br>
    Выход из сделки:
    <b id="exitRule">—</b>

    <br>
    <button onclick="loadData()">🔄 Обновить сейчас</button>
</div>

</div>

<script>
function money(value){
    if(value===undefined || value===null) return "0.00";
    return Number(value).toLocaleString("ru-RU",{
        minimumFractionDigits:2,
        maximumFractionDigits:2
    });
}

function signalClass(signal){
    if(signal==="LONG") return "long";
    if(signal==="SHORT") return "short";
    return "none";
}

function renderInstrumentCard(item){
    const signal=item.strategy.signal;
    const statusClass=item.status==="OK" ? "status" : "status error";

    let openPosition="Нет";

    if(item.open_position){
        openPosition =
            item.open_position.direction +
            " от " +
            item.open_position.entry_price;
    }

    const stats=item.statistics || {};

    return `
        <div class="card">
            <h2>${item.emoji} ${item.title}</h2>

            <span class="${statusClass}">
                ${item.status}
            </span>

            <div class="info">${item.message || ""}</div>

            <div class="info">
                Тикер: <b>${item.ticker}</b><br>
                UID: <b>${item.uid}</b><br>
                4H-свечей: <b>${item.candles}</b>
            </div>

            <div class="signal ${signalClass(signal)}">
                ${signal}
            </div>

            <div class="info">
                ${item.strategy.description || ""}
            </div>

            <div class="info">
                <b>🤖 Выбрана стратегия: ${item.selected_strategy || "—"}</b><br>
                Закрытых сделок: <b>${stats.total || 0}</b><br>

                Проходимость:
                <b>${stats.winrate || 0}%</b><br>

                Прибыльных:
                <b>${stats.profitable || 0}</b><br>

                Убыточных:
                <b>${stats.losing || 0}</b><br>

                Чистый результат:
                <b>${money(stats.net)} ₽</b><br>

                Открытая позиция: <b>${openPosition}</b>
            </div>

            <div class="strategy-box">
                <b>🔬 Проверка всех стратегий</b>
                ${(item.strategy_selection || []).map((r,i)=>`
                    <div class="strategy-row">
                        <span>${i+1}. ${r.name}</span>
                        <span>${r.statistics.total} сделок</span>
                        <span>${r.statistics.winrate}%</span>
                        <span class="${r.statistics.net>=0?"positive":"negative"}">${money(r.statistics.net)} ₽</span>
                        <span>DD ${money(r.drawdown)} ₽</span>
                    </div>`).join("")}
            </div>
        </div>
    `;
}

function renderFutures(data){
    const container=document.getElementById("futures");
    container.innerHTML="";

    data.futures.forEach(item=>{
        container.innerHTML += renderInstrumentCard(item);
    });
}

function renderShares(data){
    const container=document.getElementById("shares");
    container.innerHTML="";

    data.shares.forEach(item=>{
        container.innerHTML += renderInstrumentCard(item);
    });
}

function renderStatistics(statistics){
    const container=document.getElementById("statistics");

    container.innerHTML=`
        <div class="stat">
            <div class="stat-title">Всего сделок</div>
            <div class="stat-value">${statistics.total}</div>
        </div>

        <div class="stat">
            <div class="stat-title">Прибыльных</div>
            <div class="stat-value">${statistics.profitable}</div>
        </div>

        <div class="stat">
            <div class="stat-title">Убыточных</div>
            <div class="stat-value">${statistics.losing}</div>
        </div>

        <div class="stat">
            <div class="stat-title">Проходимость</div>
            <div class="stat-value">${statistics.winrate}%</div>
        </div>

        <div class="stat">
            <div class="stat-title">До расходов</div>
            <div class="stat-value">${money(statistics.gross)} ₽</div>
        </div>

        <div class="stat">
            <div class="stat-title">Комиссии</div>
            <div class="stat-value">${money(statistics.commission)} ₽</div>
        </div>

        <div class="stat">
            <div class="stat-title">Налог</div>
            <div class="stat-value">${money(statistics.tax)} ₽</div>
        </div>

        <div class="stat">
            <div class="stat-title">ЧИСТЫЙ РЕЗУЛЬТАТ</div>
            <div class="stat-value ${
                statistics.net>=0 ? "positive" : "negative"
            }">
                ${money(statistics.net)} ₽
            </div>
        </div>
    `;
}

function renderHistory(data){
    const container=document.getElementById("history");

    let allTrades=[];

    [...data.futures,...data.shares].forEach(item=>{
        if(item.history){
            allTrades=allTrades.concat(item.history);
        }
    });

    allTrades.sort(
        (a,b)=>new Date(b.exit_time)-new Date(a.exit_time)
    );

    if(allTrades.length===0){
        container.innerHTML="Пока закрытых сделок нет.";
        return;
    }

    let html=`
        <table>
        <thead>
        <tr>
            <th>Инструмент</th>
            <th>Направление</th>
            <th>Вход</th>
            <th>Выход</th>
            <th>Цена входа</th>
            <th>Цена выхода</th>
            <th>Результат</th>
            <th>Комиссия</th>
            <th>Налог</th>
            <th>Чистый результат</th>
        </tr>
        </thead>
        <tbody>
    `;

    allTrades.slice(0,100).forEach(trade=>{
        const net=Number(trade.net_result);
        const cls=net>=0 ? "positive" : "negative";

        const commission =
            Number(trade.buy_commission || 0) +
            Number(trade.sell_commission || 0);

        html += `
            <tr>
                <td>${trade.title}</td>
                <td>${trade.direction}</td>
                <td>${trade.entry_time}</td>
                <td>${trade.exit_time}</td>
                <td>${trade.entry_price}</td>
                <td>${trade.exit_price}</td>
                <td>${money(trade.gross_result)} ₽</td>
                <td>${money(commission)} ₽</td>
                <td>${money(trade.tax)} ₽</td>
                <td class="${cls}">
                    ${money(net)} ₽
                </td>
            </tr>
        `;
    });

    html += "</tbody></table>";
    container.innerHTML=html;
}

async function loadData(){
    try{
        const response=await fetch("/api/status");
        const data=await response.json();

        if(data.error){
            console.error(data.error);
            return;
        }

        renderFutures(data);
        renderShares(data);
        renderStatistics(data.statistics);
        renderHistory(data);

        document.getElementById("updated").textContent =
            "Обновлено: " +
            new Date(data.updated).toLocaleString("ru-RU");

        document.getElementById("positionSize").textContent =
            money(data.settings.position_size);

        document.getElementById("buyCommission").textContent =
            data.settings.buy_commission;

        document.getElementById("sellCommission").textContent =
            data.settings.sell_commission;

        document.getElementById("tax").textContent =
            data.settings.tax;

        document.getElementById("interval").textContent =
            data.settings.candle_interval;

        document.getElementById("historyDays").textContent =
            data.settings.history_days;

        document.getElementById("exitRule").textContent =
            data.settings.exit_rule;

    }catch(error){
        console.error("Ошибка загрузки:",error);
    }
}

loadData();
setInterval(loadData,60000);
</script>

</body>
</html>
"""

# ============================================================
# ГЛАВНАЯ
# ============================================================

@app.route("/")
def index():
    return render_template_string(HTML)


# ============================================================
# ФОНОВЫЙ МОНИТОР
# ============================================================

def background_monitor():
    while True:
        try:
            data = collect_data()

            log.info("MARKUS TRADE | обновление данных")

            for item in data["futures"] + data["shares"]:
                log.info(
                    "%s | ticker=%s | candles=%s | signal=%s",
                    item["title"],
                    item["ticker"],
                    item["candles"],
                    item["strategy"]["signal"],
                )

            stats = data["statistics"]

            log.info(
                "СТАТИСТИКА | сделок=%s | winrate=%s%% | чистый=%s ₽",
                stats["total"],
                stats["winrate"],
                stats["net"],
            )

        except Exception as e:
            log.exception(
                "Ошибка фонового мониторинга: %s",
                e,
            )

        time.sleep(UPDATE_SECONDS)


# ============================================================
# ЗАПУСК
# ============================================================

if __name__ == "__main__":
    token = get_token()

    if not token:
        log.error(
            "ВНИМАНИЕ: API-токен не найден!"
        )
    else:
        log.info("API-токен найден.")

    monitor_thread = threading.Thread(
        target=background_monitor,
        daemon=True,
    )
    monitor_thread.start()

    port = int(os.environ.get("PORT", "5000"))

    log.info(
        "MARKUS TRADE запускается на порту %s",
        port,
    )

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
    )
