import os
import time
import math
import threading
import logging
import urllib3
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, jsonify, render_template_string


# ============================================================
# MARKUS TRADE
# Анализ фьючерсов T-Bank
# Юань / Золото / Brent
# 15 минут
# 14 стратегий
# БЕЗ реальных сделок
# ============================================================

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

# ============================================================
# НАСТРОЙКИ
# ============================================================

API_URL = "https://invest-public-api.tbank.ru/rest"

INTERVAL = "CANDLE_INTERVAL_15_MIN"

HISTORY_DAYS = 14
CHUNK_DAYS = 3

UPDATE_SECONDS = 300

POSITION_SIZE = 100000.0
COMMISSION_RATE = 0.001

MIN_TRADES = 5

REQUEST_TIMEOUT = 25


# ============================================================
# РЫНКИ
# ============================================================

MARKETS = {
    "CR": {
        "name": "Юань",
        "queries": [
            "CR",
            "CNY",
            "CNY/RUB",
            "юань",
            "рубль юань"
        ]
    },

    "GD": {
        "name": "Золото",
        "queries": [
            "GD",
            "GOLD",
            "золото"
        ]
    },

    "BR": {
        "name": "Brent",
        "queries": [
            "BR",
            "BRENT",
            "нефть",
            "Brent"
        ]
    }
}


# ============================================================
# ГЛОБАЛЬНОЕ СОСТОЯНИЕ
# ============================================================

session = requests.Session()

# Оставляем False из-за той SSL-проблемы,
# которая уже возникала у тебя на сервере.
session.verify = False

market_data = {}

state = {
    "started": False,
    "status": "Ожидание запуска анализа",
    "last_run": None,
    "global_error": None,
    "started_at": None
}

state_lock = threading.Lock()


# ============================================================
# ТОКЕН
# ============================================================

def get_token():

    possible_names = [
        "TINVEST_TOKEN",
        "TINKOFF_TOKEN",
        "TBANK_TOKEN",
        "API_TOKEN",
        "TOKEN"
    ]

    for name in possible_names:

        value = os.getenv(name)

        if value and value.strip():
            return value.strip()

    return ""


def get_headers():

    token = get_token()

    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }


# ============================================================
# API POST
# ============================================================

def api_post(endpoint, payload):

    token = get_token()

    if not token:
        raise RuntimeError(
            "Токен T-Bank не найден в переменных окружения"
        )

    url = API_URL + endpoint

    response = session.post(
        url,
        json=payload,
        headers=get_headers(),
        timeout=REQUEST_TIMEOUT
    )

    if response.status_code != 200:

        try:
            error_body = response.json()
        except Exception:
            error_body = response.text[:1500]

        raise RuntimeError(
            f"HTTP {response.status_code}: {error_body}"
        )

    try:
        return response.json()

    except Exception as e:

        raise RuntimeError(
            f"API вернул не JSON: {e}"
        )


# ============================================================
# ЧИСЛО ИЗ QUOTATION
# ============================================================

def quotation_to_float(value):

    if value is None:
        return 0.0

    if isinstance(value, (int, float)):
        return float(value)

    if isinstance(value, dict):

        units = value.get("units", 0)
        nano = value.get("nano", 0)

        try:
            return float(units) + float(nano) / 1_000_000_000

        except Exception:
            pass

        if "value" in value:
            return quotation_to_float(value["value"])

    try:
        return float(value)

    except Exception:
        return 0.0


# ============================================================
# ДАТА
# ============================================================

def parse_datetime(value):

    if not value:
        return None

    try:

        return datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )

    except Exception:

        return None


# ============================================================
# ПОИСК ФЬЮЧЕРСОВ
# ============================================================

def find_futures_by_query(query):

    payload = {
        "query": query,
        "instrumentKind": "INSTRUMENT_TYPE_FUTURES",
        "apiTradeAvailableFlag": True
    }

    data = api_post(
        "/tinkoff.public.invest.api.contract.v1.InstrumentsService/FindInstrument",
        payload
    )

    return data.get("instruments", [])


# ============================================================
# ПОЛУЧИТЬ ВСЕ ФЬЮЧЕРСЫ
# ============================================================

def get_all_futures():

    payload = {
        "instrumentStatus": "INSTRUMENT_STATUS_BASE"
    }

    data = api_post(
        "/tinkoff.public.invest.api.contract.v1.InstrumentsService/Futures",
        payload
    )

    return data.get("instruments", [])


# ============================================================
# РЕЙТИНГ КОНТРАКТА
# ============================================================

def contract_score(contract, market_code):

    ticker = str(
        contract.get("ticker", "")
    ).upper()

    name = str(
        contract.get("name", "")
    ).upper()

    basic_asset = str(
        contract.get("basicAsset", "")
    ).upper()

    class_code = str(
        contract.get("classCode", "")
    ).upper()

    text = " ".join([
        ticker,
        name,
        basic_asset,
        class_code
    ])

    score = 0

    # --------------------------------------------------------
    # ЮАНЬ
    # --------------------------------------------------------

    if market_code == "CR":

        if ticker.startswith("CR"):
            score += 150

        if "CNY" in text:
            score += 100

        if "ЮАН" in text:
            score += 100

        if "RUB" in text:
            score += 50

    # --------------------------------------------------------
    # ЗОЛОТО
    # --------------------------------------------------------

    elif market_code == "GD":

        if ticker.startswith("GD"):
            score += 150

        if "GOLD" in text:
            score += 100

        if "ЗОЛОТ" in text:
            score += 100

    # --------------------------------------------------------
    # BRENT
    # --------------------------------------------------------

    elif market_code == "BR":

        if ticker.startswith("BR"):
            score += 150

        if "BRENT" in text:
            score += 100

        if "НЕФТ" in text:
            score += 100

    # --------------------------------------------------------
    # ТОРГОВЛЯ
    # --------------------------------------------------------

    if contract.get("apiTradeAvailableFlag", True):
        score += 20

    # --------------------------------------------------------
    # СРОК ЭКСПИРАЦИИ
    # --------------------------------------------------------

    expiry = parse_datetime(
        contract.get("lastTradeDate")
    )

    now = datetime.now(timezone.utc)

    if expiry:

        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)

        if expiry > now:

            score += 50

            days_left = (
                expiry - now
            ).total_seconds() / 86400

            if days_left < 120:
                score += 20

    return score


# ============================================================
# КАНДИДАТЫ
# ============================================================

def find_contract_candidates(market_code):

    candidates = []
    seen = set()

    # --------------------------------------------------------
    # 1. FIND INSTRUMENT
    # --------------------------------------------------------

    for query in MARKETS[market_code]["queries"]:

        try:

            items = find_futures_by_query(query)

            for item in items:

                uid = (
                    item.get("uid")
                    or item.get("instrumentUid")
                    or item.get("figi")
                )

                if not uid:
                    continue

                if uid in seen:
                    continue

                seen.add(uid)
                candidates.append(item)

        except Exception as e:

            logging.warning(
                "FindInstrument [%s]: %s",
                query,
                e
            )

    # --------------------------------------------------------
    # 2. FALLBACK FUTURES
    # --------------------------------------------------------

    if not candidates:

        try:

            all_futures = get_all_futures()

            for item in all_futures:

                uid = (
                    item.get("uid")
                    or item.get("instrumentUid")
                    or item.get("figi")
                )

                if not uid:
                    continue

                if uid in seen:
                    continue

                seen.add(uid)
                candidates.append(item)

        except Exception as e:

            raise RuntimeError(
                f"Не удалось получить список фьючерсов: {e}"
            )

    # --------------------------------------------------------
    # 3. СОРТИРОВКА
    # --------------------------------------------------------

    candidates.sort(
        key=lambda x: contract_score(
            x,
            market_code
        ),
        reverse=True
    )

    # Берём достаточно кандидатов,
    # чтобы можно было попробовать несколько контрактов.
    return candidates[:20]


# ============================================================
# ID ИНСТРУМЕНТА
# ============================================================

def get_instrument_ids(contract):

    ids = []

    for value in [
        contract.get("uid"),
        contract.get("instrumentUid"),
        contract.get("figi")
    ]:

        if value and value not in ids:
            ids.append(value)

    ticker = contract.get("ticker")
    class_code = contract.get("classCode")

    if ticker and class_code:

        composite = f"{ticker}_{class_code}"

        if composite not in ids:
            ids.append(composite)

    return ids


# ============================================================
# ПОЛУЧЕНИЕ ОДНОГО КУСКА СВЕЧЕЙ
# ============================================================

def request_candle_chunk(
    instrument_id,
    start_dt,
    end_dt
):

    payload = {
        "from": start_dt.isoformat().replace("+00:00", "Z"),
        "to": end_dt.isoformat().replace("+00:00", "Z"),
        "interval": INTERVAL,
        "instrumentId": instrument_id,
        "limit": 2400,
        "candleSourceType": "CANDLE_SOURCE_EXCHANGE"
    }

    try:

        return api_post(
            "/tinkoff.public.invest.api.contract.v1.MarketDataService/GetCandles",
            payload
        )

    except Exception as first_error:

        # Второй запрос без candleSourceType.
        # Это полезно для совместимости API.
        payload.pop("candleSourceType", None)

        try:

            return api_post(
                "/tinkoff.public.invest.api.contract.v1.MarketDataService/GetCandles",
                payload
            )

        except Exception:

            raise first_error


# ============================================================
# ПОЛУЧЕНИЕ ИСТОРИИ
# ============================================================

def get_candles(contract):

    instrument_ids = get_instrument_ids(contract)

    if not instrument_ids:

        raise RuntimeError(
            "У контракта нет UID / FIGI / ticker_class_code"
        )

    errors = []

    all_raw = []

    now = datetime.now(timezone.utc)

    # --------------------------------------------------------
    # Разбиваем 14 дней на маленькие куски.
    # --------------------------------------------------------

    chunks = []

    current_end = now

    history_start = (
        now - timedelta(days=HISTORY_DAYS)
    )

    while current_end > history_start:

        current_start = max(
            history_start,
            current_end - timedelta(days=CHUNK_DAYS)
        )

        chunks.append(
            (current_start, current_end)
        )

        current_end = current_start

    # --------------------------------------------------------
    # Каждый ID
    # --------------------------------------------------------

    for instrument_id in instrument_ids:

        current_result = []

        failed = False

        for start_dt, end_dt in chunks:

            try:

                data = request_candle_chunk(
                    instrument_id,
                    start_dt,
                    end_dt
                )

                candles = data.get(
                    "candles",
                    []
                )

                if candles:
                    current_result.extend(candles)

            except Exception as e:

                errors.append(
                    f"{instrument_id}: {e}"
                )

                failed = True
                break

        if not failed and current_result:

            all_raw = current_result

            logging.info(
                "Получены свечи: %s | %s",
                instrument_id,
                len(all_raw)
            )

            break

    if not all_raw:

        message = (
            "Свечи не получены."
        )

        if errors:

            message += (
                " Ошибки: "
                + " | ".join(errors[-6:])
            )

        raise RuntimeError(message)

    # --------------------------------------------------------
    # Преобразуем
    # --------------------------------------------------------

    result = []

    seen = set()

    for candle in all_raw:

        candle_time = candle.get("time")

        if not candle_time:
            continue

        if candle_time in seen:
            continue

        seen.add(candle_time)

        result.append({
            "time": candle_time,

            "open": quotation_to_float(
                candle.get("open")
            ),

            "high": quotation_to_float(
                candle.get("high")
            ),

            "low": quotation_to_float(
                candle.get("low")
            ),

            "close": quotation_to_float(
                candle.get("close")
            ),

            "volume": quotation_to_float(
                candle.get("volume")
            )
        })

    result.sort(
        key=lambda x: x["time"]
    )

    return result


# ============================================================
# ИНДИКАТОРЫ
# ============================================================

def ema(values, period):

    if not values:
        return 0.0

    alpha = 2 / (period + 1)

    result = values[0]

    for value in values[1:]:

        result = (
            alpha * value
            + (1 - alpha) * result
        )

    return result


def sma(values, period):

    if len(values) < period:
        return None

    return sum(
        values[-period:]
    ) / period


def standard_deviation(values, period):

    if len(values) < period:
        return None

    mean = sma(
        values,
        period
    )

    if mean is None:
        return None

    return math.sqrt(
        sum(
            (x - mean) ** 2
            for x in values[-period:]
        ) / period
    )


def rsi(values, period=14):

    if len(values) < period + 1:
        return None

    gains = []
    losses = []

    for i in range(
        len(values) - period,
        len(values)
    ):

        change = (
            values[i]
            - values[i - 1]
        )

        gains.append(
            max(change, 0)
        )

        losses.append(
            max(-change, 0)
        )

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss

    return 100 - (
        100 / (1 + rs)
    )


def atr(candles, period=14):

    if len(candles) < period + 1:
        return None

    tr_values = []

    for i in range(
        1,
        len(candles)
    ):

        current = candles[i]
        previous = candles[i - 1]

        tr = max(
            current["high"]
            - current["low"],

            abs(
                current["high"]
                - previous["close"]
            ),

            abs(
                current["low"]
                - previous["close"]
            )
        )

        tr_values.append(tr)

    return (
        sum(tr_values[-period:])
        / period
    )


# ============================================================
# 1. МОЯ СТРАТЕГИЯ
# ============================================================

def strategy_my(candles):

    if len(candles) < 8:
        return 0

    last = candles[-8:]

    highs = [
        x["high"]
        for x in last
    ]

    lows = [
        x["low"]
        for x in last
    ]

    closes = [
        x["close"]
        for x in last
    ]

    # SHORT
    short_pattern = (
        highs[3] > highs[2]
        and highs[4] > highs[3]
        and highs[5] > highs[4]
        and closes[-1] < closes[-2]
        and closes[-2] < closes[-3]
    )

    # LONG
    long_pattern = (
        lows[3] < lows[2]
        and lows[4] < lows[3]
        and lows[5] < lows[4]
        and closes[-1] > closes[-2]
        and closes[-2] > closes[-3]
    )

    if short_pattern:
        return -1

    if long_pattern:
        return 1

    return 0


# ============================================================
# 2. EMA
# ============================================================

def strategy_ema(candles):

    if len(candles) < 55:
        return 0

    before = [
        x["close"]
        for x in candles[:-1]
    ]

    current = [
        x["close"]
        for x in candles
    ]

    old_fast = ema(before, 20)
    old_slow = ema(before, 50)

    new_fast = ema(current, 20)
    new_slow = ema(current, 50)

    if (
        new_fast > new_slow
        and old_fast <= old_slow
    ):
        return 1

    if (
        new_fast < new_slow
        and old_fast >= old_slow
    ):
        return -1

    return 0


# ============================================================
# 3. RSI
# ============================================================

def strategy_rsi(candles):

    values = [
        x["close"]
        for x in candles
    ]

    value = rsi(values)

    if value is None:
        return 0

    if value < 30:
        return 1

    if value > 70:
        return -1

    return 0


# ============================================================
# 4. BOLLINGER
# ============================================================

def strategy_bollinger(candles):

    values = [
        x["close"]
        for x in candles
    ]

    middle = sma(
        values,
        20
    )

    deviation = standard_deviation(
        values,
        20
    )

    if middle is None or deviation is None:
        return 0

    upper = middle + 2 * deviation
    lower = middle - 2 * deviation

    price = values[-1]

    if price < lower:
        return 1

    if price > upper:
        return -1

    return 0


# ============================================================
# 5. MACD
# ============================================================

def strategy_macd(candles):

    values = [
        x["close"]
        for x in candles
    ]

    if len(values) < 35:
        return 0

    macd_values = []

    for i in range(
        26,
        len(values) + 1
    ):

        section = values[:i]

        macd_values.append(
            ema(section, 12)
            - ema(section, 26)
        )

    if len(macd_values) < 10:
        return 0

    signal = ema(
        macd_values,
        9
    )

    macd_now = macd_values[-1]
    macd_prev = macd_values[-2]

    signal_prev = ema(
        macd_values[:-1],
        9
    )

    if (
        macd_now > signal
        and macd_prev <= signal_prev
    ):
        return 1

    if (
        macd_now < signal
        and macd_prev >= signal_prev
    ):
        return -1

    return 0


# ============================================================
# 6. DONCHIAN
# ============================================================

def strategy_donchian(candles):

    if len(candles) < 21:
        return 0

    previous = candles[-21:-1]

    highest = max(
        x["high"]
        for x in previous
    )

    lowest = min(
        x["low"]
        for x in previous
    )

    close = candles[-1]["close"]

    if close > highest:
        return 1

    if close < lowest:
        return -1

    return 0


# ============================================================
# 7. MOMENTUM
# ============================================================

def strategy_momentum(candles):

    if len(candles) < 11:
        return 0

    old_price = candles[-11]["close"]
    new_price = candles[-1]["close"]

    if new_price > old_price:
        return 1

    if new_price < old_price:
        return -1

    return 0


# ============================================================
# 8. PRICE ACTION
# ============================================================

def strategy_price_action(candles):

    if len(candles) < 3:
        return 0

    previous = candles[-2]
    current = candles[-1]

    if (
        current["close"] > current["open"]
        and current["close"] > previous["high"]
    ):
        return 1

    if (
        current["close"] < current["open"]
        and current["close"] < previous["low"]
    ):
        return -1

    return 0


# ============================================================
# 9. VWAP
# ============================================================

def strategy_vwap(candles):

    if len(candles) < 10:
        return 0

    recent = candles[-30:]

    volume = sum(
        x.get("volume", 0)
        for x in recent
    )

    if volume <= 0:
        return 0

    weighted = sum(
        x["close"]
        * x.get("volume", 0)
        for x in recent
    )

    vwap = weighted / volume

    close = candles[-1]["close"]

    if close > vwap:
        return 1

    if close < vwap:
        return -1

    return 0


# ============================================================
# 10. EMA + RSI
# ============================================================

def strategy_ema_rsi(candles):

    if len(candles) < 55:
        return 0

    values = [
        x["close"]
        for x in candles
    ]

    ema50 = ema(
        values,
        50
    )

    rsi_value = rsi(
        values
    )

    if rsi_value is None:
        return 0

    price = values[-1]

    if (
        price > ema50
        and rsi_value > 50
    ):
        return 1

    if (
        price < ema50
        and rsi_value < 50
    ):
        return -1

    return 0


# ============================================================
# 11. ADX-ПОДОБНЫЙ TREND
# ============================================================

def strategy_adx(candles):

    if len(candles) < 16:
        return 0

    plus = 0
    minus = 0

    for i in range(
        len(candles) - 14,
        len(candles)
    ):

        current = candles[i]
        previous = candles[i - 1]

        up = (
            current["high"]
            - previous["high"]
        )

        down = (
            previous["low"]
            - current["low"]
        )

        if up > down and up > 0:
            plus += up

        if down > up and down > 0:
            minus += down

    if plus > minus * 1.2:
        return 1

    if minus > plus * 1.2:
        return -1

    return 0


# ============================================================
# 12. EMA 200
# ============================================================

def strategy_ema200(candles):

    if len(candles) < 200:
        return 0

    values = [
        x["close"]
        for x in candles
    ]

    ema200 = ema(
        values,
        200
    )

    price = values[-1]

    if price > ema200:
        return 1

    if price < ema200:
        return -1

    return 0


# ============================================================
# 13. ATR
# ============================================================

def strategy_atr(candles):

    value = atr(
        candles,
        14
    )

    if value is None:
        return 0

    current = candles[-1]

    body = (
        current["close"]
        - current["open"]
    )

    if body > value * 0.8:
        return 1

    if body < -value * 0.8:
        return -1

    return 0


# ============================================================
# 14. BREAKOUT
# ============================================================

def strategy_breakout(candles):

    if len(candles) < 31:
        return 0

    previous = candles[-31:-1]

    highest = max(
        x["high"]
        for x in previous
    )

    lowest = min(
        x["low"]
        for x in previous
    )

    close = candles[-1]["close"]

    if close > highest:
        return 1

    if close < lowest:
        return -1

    return 0


# ============================================================
# СПИСОК СТРАТЕГИЙ
# ============================================================

STRATEGIES = [

    (
        "MY",
        "Моя стратегия",
        strategy_my
    ),

    (
        "EMA",
        "EMA 20/50",
        strategy_ema
    ),

    (
        "RSI",
        "RSI",
        strategy_rsi
    ),

    (
        "BOLLINGER",
        "Bollinger",
        strategy_bollinger
    ),

    (
        "MACD",
        "MACD",
        strategy_macd
    ),

    (
        "DONCHIAN",
        "Donchian",
        strategy_donchian
    ),

    (
        "MOMENTUM",
        "Momentum",
        strategy_momentum
    ),

    (
        "PRICE_ACTION",
        "Price Action",
        strategy_price_action
    ),

    (
        "VWAP",
        "VWAP",
        strategy_vwap
    ),

    (
        "EMA_RSI",
        "EMA + RSI",
        strategy_ema_rsi
    ),

    (
        "ADX",
        "ADX",
        strategy_adx
    ),

    (
        "EMA_200",
        "EMA 200",
        strategy_ema200
    ),

    (
        "ATR",
        "ATR",
        strategy_atr
    ),

    (
        "BREAKOUT",
        "Breakout",
        strategy_breakout
    )
]


# ============================================================
# БЭКТЕСТ
# ============================================================

def backtest(candles, strategy_function):

    if len(candles) < 10:

        return {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0,
            "profit": 0,
            "drawdown": 0,
            "profit_factor": 0,
            "average_trade": 0
        }

    equity = 0
    peak = 0
    drawdown = 0

    trades = []

    # ВАЖНО:
    # стратегии получают всю историю candles[:i],
    # а не последние 8 свечей.
    #
    # Это позволяет EMA/RSI/EMA200 и другим стратегиям
    # нормально работать.

    for i in range(
        2,
        len(candles)
    ):

        history = candles[:i]

        try:

            signal = strategy_function(
                history
            )

        except Exception:

            signal = 0

        if signal == 0:
            continue

        entry = candles[i]["open"]
        exit_price = candles[i]["close"]

        if entry <= 0:
            continue

        price_change = (
            exit_price - entry
        ) / entry

        trade_profit = (
            price_change
            * signal
            * POSITION_SIZE
        )

        commission = (
            POSITION_SIZE
            * COMMISSION_RATE
        )

        trade_profit -= commission

        trades.append(
            trade_profit
        )

        equity += trade_profit

        peak = max(
            peak,
            equity
        )

        current_dd = (
            peak - equity
        )

        drawdown = max(
            drawdown,
            current_dd
        )

    wins = sum(
        1
        for x in trades
        if x > 0
    )

    losses = sum(
        1
        for x in trades
        if x <= 0
    )

    gross_profit = sum(
        x
        for x in trades
        if x > 0
    )

    gross_loss = -sum(
        x
        for x in trades
        if x < 0
    )

    if trades:

        win_rate = (
            wins
            / len(trades)
            * 100
        )

        average_trade = (
            equity
            / len(trades)
        )

    else:

        win_rate = 0
        average_trade = 0

    if gross_loss > 0:

        profit_factor = (
            gross_profit
            / gross_loss
        )

    elif gross_profit > 0:

        profit_factor = 99

    else:

        profit_factor = 0

    return {

        "trades": len(trades),

        "wins": wins,

        "losses": losses,

        "win_rate": round(
            win_rate,
            2
        ),

        "profit": round(
            equity,
            2
        ),

        "drawdown": round(
            drawdown,
            2
        ),

        "profit_factor": round(
            profit_factor,
            2
        ),

        "average_trade": round(
            average_trade,
            2
        )
    }


# ============================================================
# ВЫБОР ЛУЧШЕЙ СТРАТЕГИИ
# ============================================================

def select_strategy(candles):

    statistics = []

    for (
        strategy_code,
        strategy_name,
        strategy_function
    ) in STRATEGIES:

        result = backtest(
            candles,
            strategy_function
        )

        statistics.append({

            "code": strategy_code,

            "name": strategy_name,

            **result

        })

    # Сначала стратегии, где есть хотя бы MIN_TRADES сделок.
    eligible = [
        x
        for x in statistics
        if x["trades"] >= MIN_TRADES
    ]

    if not eligible:

        eligible = statistics

    # --------------------------------------------------------
    # Выбор НЕ только по win rate.
    #
    # Приоритет:
    # 1. прибыль
    # 2. profit factor
    # 3. win rate
    # 4. меньшая просадка
    # 5. количество сделок
    # --------------------------------------------------------

    best = max(
        eligible,
        key=lambda x: (
            x["profit"],
            x["profit_factor"],
            x["win_rate"],
            -x["drawdown"],
            x["trades"]
        )
    )

    # --------------------------------------------------------
    # Текущий сигнал выбранной стратегии
    # --------------------------------------------------------

    current_signal = 0

    for (
        strategy_code,
        strategy_name,
        strategy_function
    ) in STRATEGIES:

        if strategy_code == best["code"]:

            try:

                current_signal = strategy_function(
                    candles
                )

            except Exception:

                current_signal = 0

            break

    return (
        statistics,
        best,
        current_signal
    )


# ============================================================
# АНАЛИЗ ОДНОГО РЫНКА
# ============================================================

def analyze_market(market_code):

    candidates = find_contract_candidates(
        market_code
    )

    if not candidates:

        raise RuntimeError(
            "Фьючерсные контракты не найдены"
        )

    attempts = []

    # --------------------------------------------------------
    # Пробуем несколько кандидатов.
    #
    # Это ключевое исправление:
    # если первый контракт оказался неправильным,
    # бот не останавливается.
    # --------------------------------------------------------

    for contract in candidates:

        ticker = contract.get(
            "ticker",
            "-"
        )

        try:

            candles = get_candles(
                contract
            )

            if len(candles) < 30:

                attempts.append(
                    f"{ticker}: только {len(candles)} свечей"
                )

                continue

            (
                statistics,
                best,
                current_signal
            ) = select_strategy(
                candles
            )

            return {

                "code": market_code,

                "name": MARKETS[
                    market_code
                ]["name"],

                "ticker": ticker,

                "uid": (
                    contract.get("uid")
                    or contract.get(
                        "instrumentUid"
                    )
                    or "-"
                ),

                "figi": contract.get(
                    "figi",
                    "-"
                ),

                "class_code": contract.get(
                    "classCode",
                    "-"
                ),

                "last_trade_date": contract.get(
                    "lastTradeDate",
                    "-"
                ),

                "candles": len(candles),

                "selected": best,

                "signal": current_signal,

                "strategies": statistics,

                "error": None

            }

        except Exception as e:

            attempts.append(
                f"{ticker}: {e}"
            )

            logging.warning(
                "%s -> %s",
                ticker,
                e
            )

    raise RuntimeError(
        "Не удалось получить свечи ни по одному "
        "подходящему контракту. "
        + " | ".join(
            attempts[-8:]
        )
    )


# ============================================================
# ОБЩАЯ СТАТИСТИКА
# ============================================================

def calculate_global_statistics():

    markets = list(
        market_data.values()
    )

    total_trades = 0
    total_wins = 0
    total_losses = 0
    total_profit = 0
    total_drawdown = 0

    for market in markets:

        selected = market.get(
            "selected"
        )

        if not selected:
            continue

        total_trades += selected.get(
            "trades",
            0
        )

        total_wins += selected.get(
            "wins",
            0
        )

        total_losses += selected.get(
            "losses",
            0
        )

        total_profit += selected.get(
            "profit",
            0
        )

        total_drawdown += selected.get(
            "drawdown",
            0
        )

    if total_trades:

        win_rate = (
            total_wins
            / total_trades
            * 100
        )

    else:

        win_rate = 0

    return {

        "trades": total_trades,

        "wins": total_wins,

        "losses": total_losses,

        "win_rate": round(
            win_rate,
            2
        ),

        "profit": round(
            total_profit,
            2
        ),

        "drawdown": round(
            total_drawdown,
            2
        )
    }


# ============================================================
# ОСНОВНОЙ ЦИКЛ
# ============================================================

def run_analysis():

    with state_lock:

        state["started"] = True

        state["started_at"] = (
            datetime.now().strftime(
                "%d.%m.%Y %H:%M:%S"
            )
        )

        state["status"] = (
            "Запуск анализа..."
        )

        state["global_error"] = None

    new_data = {}

    # --------------------------------------------------------
    # АНАЛИЗ ЮАНЯ
    # --------------------------------------------------------

    with state_lock:

        state["status"] = (
            "Анализ Юаня..."
        )

    try:

        new_data["CR"] = analyze_market(
            "CR"
        )

    except Exception as e:

        logging.exception(
            "Ошибка Юань"
        )

        new_data["CR"] = {

            "code": "CR",

            "name": "Юань",

            "ticker": "-",

            "uid": "-",

            "figi": "-",

            "class_code": "-",

            "candles": 0,

            "selected": None,

            "signal": 0,

            "strategies": [],

            "error": str(e)
        }

    # --------------------------------------------------------
    # ЗОЛОТО
    # --------------------------------------------------------

    with state_lock:

        state["status"] = (
            "Анализ Золота..."
        )

    try:

        new_data["GD"] = analyze_market(
            "GD"
        )

    except Exception as e:

        logging.exception(
            "Ошибка Золото"
        )

        new_data["GD"] = {

            "code": "GD",

            "name": "Золото",

            "ticker": "-",

            "uid": "-",

            "figi": "-",

            "class_code": "-",

            "candles": 0,

            "selected": None,

            "signal": 0,

            "strategies": [],

            "error": str(e)
        }

    # --------------------------------------------------------
    # BRENT
    # --------------------------------------------------------

    with state_lock:

        state["status"] = (
            "Анализ Brent..."
        )

    try:

        new_data["BR"] = analyze_market(
            "BR"
        )

    except Exception as e:

        logging.exception(
            "Ошибка Brent"
        )

        new_data["BR"] = {

            "code": "BR",

            "name": "Brent",

            "ticker": "-",

            "uid": "-",

            "figi": "-",

            "class_code": "-",

            "candles": 0,

            "selected": None,

            "signal": 0,

            "strategies": [],

            "error": str(e)
        }

    # --------------------------------------------------------
    # Сохраняем
    # --------------------------------------------------------

    with state_lock:

        market_data.clear()

        market_data.update(
            new_data
        )

        state["last_run"] = (
            datetime.now().strftime(
                "%d.%m.%Y %H:%M:%S"
            )
        )

        errors = [
            x["error"]
            for x in new_data.values()
            if x.get("error")
        ]

        if errors:

            state["status"] = (
                "Анализ завершён с ошибками"
            )

            state["global_error"] = (
                " | ".join(errors)
            )

        else:

            state["status"] = (
                "Анализ завершён"
            )

            state["global_error"] = None


# ============================================================
# ФОНОВЫЙ ПОТОК
# ============================================================

def analysis_worker():

    while True:

        try:

            run_analysis()

        except Exception as e:

            logging.exception(
                "Критическая ошибка анализа"
            )

            with state_lock:

                state["status"] = (
                    "Ошибка анализа"
                )

                state["global_error"] = str(e)

        time.sleep(
            UPDATE_SECONDS
        )


# ============================================================
# HTML
# ============================================================

HTML = """
<!DOCTYPE html>

<html lang="ru">

<head>

<meta charset="UTF-8">

<meta
name="viewport"
content="width=device-width, initial-scale=1.0"
>

<title>Markus Trade</title>

<style>

* {
    box-sizing: border-box;
}

body {

    margin: 0;

    background: #080808;

    color: #eeeeee;

    font-family:
        Arial,
        Helvetica,
        sans-serif;
}

.container {

    max-width: 1250px;

    margin: auto;

    padding: 18px;
}

.header {

    margin-bottom: 18px;
}

.logo {

    font-size: 30px;

    font-weight: bold;

    color: #d6b56a;
}

.subtitle {

    color: #888;

    margin-top: 5px;
}

.status {

    background: #111;

    border: 1px solid #292929;

    border-radius: 12px;

    padding: 15px;

    margin-bottom: 15px;
}

.status-title {

    font-size: 18px;

    font-weight: bold;

    margin-bottom: 8px;
}

.good {

    color: #5bd889;
}

.bad {

    color: #ff6666;
}

.warning {

    color: #e8bd62;
}

.global {

    background: #111;

    border: 1px solid #292929;

    border-radius: 12px;

    padding: 15px;

    margin-bottom: 15px;
}

.global-grid {

    display: grid;

    grid-template-columns:
        repeat(
            auto-fit,
            minmax(
                150px,
                1fr
            )
        );

    gap: 10px;

    margin-top: 12px;
}

.stat {

    background: #181818;

    border-radius: 9px;

    padding: 12px;
}

.stat-label {

    color: #888;

    font-size: 12px;
}

.stat-value {

    margin-top: 5px;

    font-size: 20px;

    font-weight: bold;
}

.markets {

    display: grid;

    grid-template-columns:
        repeat(
            auto-fit,
            minmax(
                350px,
                1fr
            )
        );

    gap: 15px;
}

.card {

    background: #111;

    border: 1px solid #292929;

    border-radius: 12px;

    padding: 15px;

    overflow: hidden;
}

.card-title {

    font-size: 23px;

    font-weight: bold;

    color: #d6b56a;

    margin-bottom: 10px;
}

.contract {

    background: #181818;

    border-radius: 8px;

    padding: 10px;

    font-size: 12px;

    line-height: 1.7;

    word-break: break-word;
}

.selected {

    margin-top: 12px;

    padding: 12px;

    background: #19160f;

    border: 1px solid #705b2c;

    border-radius: 9px;
}

.selected-name {

    font-size: 18px;

    font-weight: bold;

    color: #e0bd70;
}

.signal {

    font-size: 21px;

    font-weight: bold;

    margin-top: 8px;
}

.signal-long {

    color: #54d987;
}

.signal-short {

    color: #ff6565;
}

.signal-none {

    color: #999;
}

.error {

    margin-top: 12px;

    background: #1b0d0d;

    border: 1px solid #542323;

    color: #ff7777;

    padding: 10px;

    border-radius: 8px;

    word-break: break-word;

    line-height: 1.5;
}

details {

    margin-top: 13px;
}

summary {

    cursor: pointer;

    color: #d6b56a;

    padding: 7px 0;
}

.table-wrap {

    overflow-x: auto;
}

table {

    width: 100%;

    border-collapse: collapse;

    min-width: 650px;

    font-size: 12px;
}

th {

    color: #d6b56a;

    text-align: left;

    border-bottom:
        1px solid #333;

    padding: 7px;
}

td {

    padding: 7px;

    border-bottom:
        1px solid #252525;
}

.footer {

    color: #666;

    margin-top: 20px;

    font-size: 11px;

    text-align: center;
}

</style>

</head>

<body>

<div class="container">

<div class="header">

<div class="logo">
MARKUS TRADE
</div>

<div class="subtitle">
Автоматический анализ • 15 минут • 14 стратегий
</div>

</div>


<div id="status"
class="status">

Загрузка...

</div>


<div id="global"
class="global">

Загрузка общей статистики...

</div>


<div
id="markets"
class="markets">

</div>


<div class="footer">

Markus Trade анализирует исторические данные.
Результаты бэктеста не являются гарантией будущей доходности.

</div>

</div>


<script>

function escapeHtml(value) {

    return String(
        value ?? "-"
    )
    .replace(
        /[&<>"']/g,
        function(m) {

            return {

                "&": "&amp;",
                "<": "&lt;",
                ">": "&gt;",
                '"': "&quot;",
                "'": "&#039;"

            }[m];

        }
    );
}


function signalHtml(signal) {

    if (signal === 1) {

        return `
        <div class="signal signal-long">
            LONG ↑
        </div>
        `;

    }

    if (signal === -1) {

        return `
        <div class="signal signal-short">
            SHORT ↓
        </div>
        `;

    }

    return `
    <div class="signal signal-none">
        НЕТ СИГНАЛА
    </div>
    `;
}


function strategyRow(strategy) {

    return `

    <tr>

        <td>
            ${escapeHtml(
                strategy.name
            )}
        </td>

        <td>
            ${strategy.trades}
        </td>

        <td>
            ${strategy.wins}
        </td>

        <td>
            ${strategy.losses}
        </td>

        <td>
            ${strategy.win_rate}%
        </td>

        <td>
            ${strategy.profit}
        </td>

        <td>
            ${strategy.drawdown}
        </td>

        <td>
            ${strategy.profit_factor}
        </td>

    </tr>

    `;
}


function marketHtml(market) {

    if (market.error) {

        return `

        <div class="card">

            <div class="card-title">
                ${escapeHtml(
                    market.name
                )}
            </div>

            <div class="contract">

                Тикер:
                ${escapeHtml(
                    market.ticker
                )}

                <br>

                UID:
                ${escapeHtml(
                    market.uid
                )}

                <br>

                Свечей:
                ${market.candles}

            </div>

            <div class="error">

                ${escapeHtml(
                    market.error
                )}

            </div>

        </div>

        `;
    }


    const selected =
        market.selected;


    return `

    <div class="card">

        <div class="card-title">

            ${escapeHtml(
                market.name
            )}

        </div>


        <div class="contract">

            Тикер:
            <b>
                ${escapeHtml(
                    market.ticker
                )}
            </b>

            <br>

            UID:
            ${escapeHtml(
                market.uid
            )}

            <br>

            Class:
            ${escapeHtml(
                market.class_code
            )}

            <br>

            Свечей:
            <b>
                ${market.candles}
            </b>

            <br>

            Экспирация:
            ${escapeHtml(
                market.last_trade_date
            )}

        </div>


        <div class="selected">

            <div>
                Выбранная стратегия:
            </div>

            <div class="selected-name">

                ${escapeHtml(
                    selected.name
                )}

            </div>


            ${signalHtml(
                market.signal
            )}


            <div>

                Сделок:
                <b>
                    ${selected.trades}
                </b>

                &nbsp; • &nbsp;

                Win rate:
                <b>
                    ${selected.win_rate}%
                </b>

            </div>


            <div>

                Profit:
                <b>
                    ${selected.profit}
                </b>

                &nbsp; • &nbsp;

                Drawdown:
                <b>
                    ${selected.drawdown}
                </b>

                &nbsp; • &nbsp;

                PF:
                <b>
                    ${selected.profit_factor}
                </b>

            </div>

        </div>


        <details>

            <summary>
                Все 14 стратегий
            </summary>

            <div class="table-wrap">

            <table>

                <thead>

                <tr>

                    <th>
                        Стратегия
                    </th>

                    <th>
                        Сделки
                    </th>

                    <th>
                        Win
                    </th>

                    <th>
                        Loss
                    </th>

                    <th>
                        %
                    </th>

                    <th>
                        Profit
                    </th>

                    <th>
                        DD
                    </th>

                    <th>
                        PF
                    </th>

                </tr>

                </thead>

                <tbody>

                    ${market.strategies
                        .map(
                            strategyRow
                        )
                        .join("")}

                </tbody>

            </table>

            </div>

        </details>

    </div>

    `;
}


async function loadStatus() {

    try {

        const response =
            await fetch(
                "/api/status",
                {
                    cache: "no-store"
                }
            );


        const data =
            await response.json();


        const state =
            data.state;


        let statusClass =
            "warning";


        if (
            state.status ===
            "Анализ завершён"
        ) {

            statusClass =
                "good";

        }


        if (
            state.global_error
        ) {

            statusClass =
                "bad";

        }


        document
            .getElementById(
                "status"
            )
            .innerHTML = `

            <div class="status-title ${statusClass}">

                ${escapeHtml(
                    state.status
                )}

            </div>


            <div>

                Токен:

                <span class="${
                    data.token
                    ? "good"
                    : "bad"
                }">

                    ${
                        data.token
                        ? "найден"
                        : "НЕ НАЙДЕН"
                    }

                </span>

            </div>


            <div>

                Последний запуск:

                ${escapeHtml(
                    state.last_run || "-"
                )}

            </div>


            ${
                state.global_error
                ? `

                <div class="error">

                    ${escapeHtml(
                        state.global_error
                    )}

                </div>

                `
                : ""
            }

            `;


        const global =
            data.aggregate;


        document
            .getElementById(
                "global"
            )
            .innerHTML = `

            <b>
                Общая статистика выбранных стратегий
            </b>


            <div class="global-grid">


                <div class="stat">

                    <div class="stat-label">
                        Сделок
                    </div>

                    <div class="stat-value">
                        ${global.trades}
                    </div>

                </div>


                <div class="stat">

                    <div class="stat-label">
                        Прибыль
                    </div>

                    <div class="stat-value">
                        ${global.profit}
                    </div>

                </div>


                <div class="stat">

                    <div class="stat-label">
                        Win rate
                    </div>

                    <div class="stat-value">
                        ${global.win_rate}%
                    </div>

                </div>


                <div class="stat">

                    <div class="stat-label">
                        Побед
                    </div>

                    <div class="stat-value">
                        ${global.wins}
                    </div>

                </div>


                <div class="stat">

                    <div class="stat-label">
                        Убытков
                    </div>

                    <div class="stat-value">
                        ${global.losses}
                    </div>

                </div>


                <div class="stat">

                    <div class="stat-label">
                        Drawdown
                    </div>

                    <div class="stat-value">
                        ${global.drawdown}
                    </div>

                </div>

            </div>

            `;


        const markets =
            document
                .getElementById(
                    "markets"
                );


        markets.innerHTML =
            Object.values(
                data.markets
            )
            .map(
                marketHtml
            )
            .join("");

    }

    catch (error) {

        document
            .getElementById(
                "status"
            )
            .innerHTML = `

            <div class="bad">

                Ошибка соединения
                с сервером приложения:

                ${escapeHtml(
                    error
                )}

            </div>

            `;

    }

}


loadStatus();


setInterval(
    loadStatus,
    5000
);

</script>

</body>

</html>
"""


# ============================================================
# ГЛАВНАЯ
# ============================================================

@app.route("/")
def index():

    return render_template_string(
        HTML
    )


# ============================================================
# API STATUS
# ============================================================

@app.route("/api/status")
def api_status():

    with state_lock:

        current_state = dict(
            state
        )

        current_markets = dict(
            market_data
        )

    return jsonify({

        "state": current_state,

        "token": bool(
            get_token()
        ),

        "markets": current_markets,

        "aggregate":
            calculate_global_statistics()

    })


# ============================================================
# HEALTH
# ============================================================

@app.route("/api/health")
def health():

    return jsonify({

        "ok": True,

        "token": bool(
            get_token()
        ),

        "status":
            state["status"]

    })


# ============================================================
# ЗАПУСК
# ============================================================

if __name__ == "__main__":

    worker_thread = threading.Thread(
        target=analysis_worker,
        daemon=True
    )

    worker_thread.start()

    port = int(
        os.getenv(
            "PORT",
            "10000"
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False
    )
