# ============================================================
# MARKUS TRADE
# Полный анализатор фьючерсов T-Bank
#
# 3 рынка:
#   CR / CNY — Юань
#   GD / GOLD — Золото
#   BR / BRENT — Нефть Brent
#
# 14 стратегий:
#   1. MY
#   2. EMA
#   3. RSI
#   4. BOLLINGER
#   5. MACD
#   6. DONCHIAN
#   7. MOMENTUM
#   8. PRICE_ACTION
#   9. VWAP
#   10. EMA_RSI
#   11. ADX
#   12. EMA_200
#   13. ATR
#   14. BREAKOUT
#
# ВАЖНО:
# Код НЕ отправляет реальные заявки.
# Это аналитика + backtest.
# ============================================================

import os
import time
import math
import threading
import logging
import warnings
from datetime import datetime, timedelta, timezone

import requests
import urllib3

from flask import Flask, jsonify, render_template_string


# ============================================================
# НАСТРОЙКИ
# ============================================================

API_URL = "https://invest-public-api.tbank.ru/rest"

API_TIMEOUT = 20

# 15 минут
CANDLE_INTERVAL = "CANDLE_INTERVAL_15_MIN"

# 14 дней.
# Для 15-минутных свечей T-Bank допускает до 3 недель.
HISTORY_DAYS = 14

# Максимум свечей
CANDLES_LIMIT = 2400

# Как часто обновлять анализ
UPDATE_SECONDS = 300

# Минимальное количество сделок,
# чтобы стратегия считалась достаточно статистически проверенной
MIN_TRADES = 5

# Условный размер позиции для модельного backtest
POSITION_SIZE = 100000.0

# Условная комиссия на оборот.
# Это НЕ реальная комиссия конкретного фьючерса.
COMMISSION_RATE = 0.001

# Условный налог для отображения модельной чистой прибыли
TAX_RATE = 0.13


# ============================================================
# SSL
# ============================================================

# У тебя ранее была проблема:
# CERTIFICATE_VERIFY_FAILED
#
# Временный обход для окружения, где отсутствует сертификат.
# Для production желательно установить правильный CA.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("markus_trade")


# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)


# ============================================================
# HTTP SESSION
# ============================================================

session = requests.Session()

session.headers.update({
    "Content-Type": "application/json",
    "Accept": "application/json",
})


# ============================================================
# TOKEN
# ============================================================

def get_token():
    """
    Ищем токен в нескольких популярных переменных.
    """

    names = [
        "TINVEST_TOKEN",
        "TINKOFF_TOKEN",
        "TBANK_TOKEN",
        "API_TOKEN",
        "TOKEN",
    ]

    for name in names:
        value = os.getenv(name)

        if value:
            return value.strip()

    return ""


TOKEN = get_token()


# ============================================================
# СОСТОЯНИЕ ПРИЛОЖЕНИЯ
# ============================================================

analysis_state = {
    "running": False,
    "started": None,
    "finished": None,
    "error": "",
    "cycle": 0,
    "message": "Ожидание запуска анализа",
}


state_lock = threading.Lock()


# ============================================================
# РЫНКИ
# ============================================================

MARKETS = {
    "CR": {
        "name": "Юань",
        "queries": [
            "CR",
            "CNY",
            "юань",
            "CNY/RUB",
        ],
    },

    "GD": {
        "name": "Золото",
        "queries": [
            "GD",
            "GOLD",
            "золото",
        ],
    },

    "BR": {
        "name": "Brent",
        "queries": [
            "BR",
            "BRENT",
            "нефть",
        ],
    },
}


# ============================================================
# РЕЗУЛЬТАТЫ
# ============================================================

market_data = {}

market_lock = threading.Lock()


# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================

def now_utc():
    return datetime.now(timezone.utc)


def safe_float(value, default=0.0):
    try:
        if value is None:
            return default

        return float(value)

    except Exception:
        return default


def quotation_to_float(value):
    """
    T-Bank Quotation:
    {
        "units": "123",
        "nano": 450000000
    }
    """

    if value is None:
        return 0.0

    if isinstance(value, (int, float)):
        return float(value)

    if isinstance(value, str):
        return safe_float(value)

    units = safe_float(value.get("units", 0))
    nano = safe_float(value.get("nano", 0))

    return units + nano / 1_000_000_000


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


def format_number(value, digits=2):
    try:
        return f"{float(value):,.{digits}f}".replace(",", " ")
    except Exception:
        return "0"


def format_percent(value):
    try:
        return f"{float(value):.2f}%"
    except Exception:
        return "0.00%"


# ============================================================
# API T-BANK
# ============================================================

def api_post(endpoint, payload):
    """
    Универсальный POST к REST API T-Bank.
    """

    if not TOKEN:
        raise RuntimeError(
            "Не найден токен T-Bank. "
            "Добавь TINVEST_TOKEN в Environment Variables."
        )

    url = API_URL + endpoint

    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    response = session.post(
        url,
        json=payload,
        headers=headers,
        timeout=API_TIMEOUT,
        verify=False,
    )

    if response.status_code != 200:

        text = response.text[:2000]

        raise RuntimeError(
            f"T-Bank API HTTP {response.status_code}: {text}"
        )

    try:
        return response.json()

    except Exception:

        raise RuntimeError(
            "T-Bank API вернул не JSON."
        )


# ============================================================
# FIND INSTRUMENT
# ============================================================

def find_instrument(query):
    endpoint = (
        "/tinkoff.public.invest.api.contract.v1."
        "InstrumentsService/FindInstrument"
    )

    payload = {
        "query": query,
        "instrumentKind": "INSTRUMENT_TYPE_FUTURES",
        "apiTradeAvailableFlag": True,
    }

    return api_post(endpoint, payload)


# ============================================================
# FUTURES LIST
# ============================================================

def get_all_futures():
    endpoint = (
        "/tinkoff.public.invest.api.contract.v1."
        "InstrumentsService/Futures"
    )

    payload = {
        "instrumentStatus": "INSTRUMENT_STATUS_BASE"
    }

    return api_post(endpoint, payload)


# ============================================================
# ПОЛУЧЕНИЕ АКТИВНОГО ФЬЮЧЕРСА
# ============================================================

def find_active_future(market_code):
    """
    Сначала FindInstrument.
    Если не получилось — Futures.

    Выбираем ближайший будущий контракт.
    """

    market = MARKETS[market_code]

    candidates = []

    # --------------------------------------------------------
    # 1. FindInstrument
    # --------------------------------------------------------

    for query in market["queries"]:

        try:

            data = find_instrument(query)

            instruments = data.get("instruments", [])

            for item in instruments:

                if not isinstance(item, dict):
                    continue

                uid = (
                    item.get("uid")
                    or item.get("instrumentUid")
                    or item.get("instrument_uid")
                )

                ticker = item.get("ticker", "")
                name = item.get("name", "")

                if not uid and not ticker:
                    continue

                text = (
                    f"{ticker} "
                    f"{name} "
                    f"{item.get('basicAsset', '')} "
                    f"{item.get('basicAssetPositionUid', '')}"
                ).upper()

                candidates.append(item)

        except Exception as e:

            logger.warning(
                "FindInstrument %s / %s: %s",
                market_code,
                query,
                e,
            )

    # --------------------------------------------------------
    # 2. Если ничего нет — Futures
    # --------------------------------------------------------

    if not candidates:

        try:

            data = get_all_futures()

            futures = data.get("instruments", [])

            candidates.extend(futures)

        except Exception as e:

            logger.warning(
                "Futures list error: %s",
                e,
            )

    # --------------------------------------------------------
    # Удаляем дубликаты
    # --------------------------------------------------------

    unique = {}

    for item in candidates:

        uid = (
            item.get("uid")
            or item.get("instrumentUid")
            or item.get("instrument_uid")
            or item.get("figi")
            or item.get("ticker")
        )

        if uid:
            unique[str(uid)] = item

    candidates = list(unique.values())

    # --------------------------------------------------------
    # Фильтрация по названию
    # --------------------------------------------------------

    filtered = []

    for item in candidates:

        ticker = str(item.get("ticker", "")).upper()
        name = str(item.get("name", "")).upper()
        basic_asset = str(
            item.get("basicAsset", "")
        ).upper()

        text = (
            ticker + " " +
            name + " " +
            basic_asset
        )

        if market_code == "CR":

            if (
                "CNY" in text
                or "YUAN" in text
                or "ЮАН" in text
                or ticker.startswith("CR")
            ):
                filtered.append(item)

        elif market_code == "GD":

            if (
                "GOLD" in text
                or "ЗОЛОТ" in text
                or ticker.startswith("GD")
            ):
                filtered.append(item)

        elif market_code == "BR":

            if (
                "BRENT" in text
                or "НЕФТ" in text
                or ticker.startswith("BR")
            ):
                filtered.append(item)

    if filtered:
        candidates = filtered

    # --------------------------------------------------------
    # Выбираем ближайший контракт
    # --------------------------------------------------------

    current = now_utc()

    future_candidates = []

    for item in candidates:

        expiry = (
            item.get("lastTradeDate")
            or item.get("lastTradeDateTime")
            or item.get("expirationDate")
        )

        expiry_dt = parse_date(expiry)

        if expiry_dt is not None:

            if expiry_dt > current:

                future_candidates.append(
                    (expiry_dt, item)
                )

    if future_candidates:

        future_candidates.sort(
            key=lambda x: x[0]
        )

        item = future_candidates[0][1]

    elif candidates:

        item = candidates[0]

    else:

        raise RuntimeError(
            f"Активный контракт для {market['name']} не найден."
        )

    uid = (
        item.get("uid")
        or item.get("instrumentUid")
        or item.get("instrument_uid")
        or ""
    )

    ticker = item.get("ticker", "")

    figi = item.get("figi", "")

    class_code = (
        item.get("classCode")
        or item.get("class_code")
        or ""
    )

    name = item.get("name", "")

    expiry = (
        item.get("lastTradeDate")
        or item.get("lastTradeDateTime")
        or item.get("expirationDate")
        or ""
    )

    return {
        "uid": uid,
        "ticker": ticker,
        "figi": figi,
        "class_code": class_code,
        "name": name,
        "expiry": expiry,
        "raw": item,
    }


# ============================================================
# GET CANDLES
# ============================================================

def get_candles(instrument):
    """
    Получаем 15-минутные свечи.

    14 дней:
    14 * 24 * 4 = 1344 свечи максимум,
    что находится ниже лимита 2400.
    """

    endpoint = (
        "/tinkoff.public.invest.api.contract.v1."
        "MarketDataService/GetCandles"
    )

    end_time = now_utc()
    start_time = end_time - timedelta(
        days=HISTORY_DAYS
    )

    instrument_id = (
        instrument.get("uid")
        or instrument.get("figi")
    )

    if not instrument_id:

        ticker = instrument.get("ticker")
        class_code = instrument.get("class_code")

        if ticker and class_code:

            instrument_id = (
                f"{ticker}_{class_code}"
            )

    if not instrument_id:

        raise RuntimeError(
            "Нет UID/FIGI/class_code для запроса свечей."
        )

    payload = {
        "from": start_time.isoformat(),
        "to": end_time.isoformat(),
        "interval": CANDLE_INTERVAL,
        "instrumentId": instrument_id,
        "candleSourceType": "CANDLE_SOURCE_EXCHANGE",
        "limit": CANDLES_LIMIT,
    }

    data = api_post(
        endpoint,
        payload,
    )

    raw_candles = data.get(
        "candles",
        []
    )

    candles = []

    for c in raw_candles:

        open_price = quotation_to_float(
            c.get("open")
        )

        high_price = quotation_to_float(
            c.get("high")
        )

        low_price = quotation_to_float(
            c.get("low")
        )

        close_price = quotation_to_float(
            c.get("close")
        )

        if (
            open_price <= 0
            or high_price <= 0
            or low_price <= 0
            or close_price <= 0
        ):
            continue

        candles.append({
            "time": c.get("time", ""),
            "open": open_price,
            "high": high_price,
            "low": low_price,
            "close": close_price,
            "volume": safe_float(
                c.get("volume", 0)
            ),
        })

    candles.sort(
        key=lambda x: x.get("time", "")
    )

    return candles


# ============================================================
# ИНДИКАТОРЫ
# ============================================================

def sma(values, period):
    if len(values) < period:
        return None

    return sum(
        values[-period:]
    ) / period


def ema_series(values, period):
    if len(values) < period:
        return []

    multiplier = 2 / (period + 1)

    result = []

    initial = sum(
        values[:period]
    ) / period

    result.append(initial)

    previous = initial

    for price in values[period:]:

        current = (
            price - previous
        ) * multiplier + previous

        result.append(current)

        previous = current

    return result


def ema(values, period):
    series = ema_series(
        values,
        period
    )

    if not series:
        return None

    return series[-1]


def rsi(values, period=14):
    if len(values) < period + 1:
        return None

    gains = []
    losses = []

    for i in range(
        1,
        len(values)
    ):

        change = (
            values[i] -
            values[i - 1]
        )

        if change > 0:
            gains.append(change)
            losses.append(0)

        else:
            gains.append(0)
            losses.append(abs(change))

    avg_gain = sum(
        gains[:period]
    ) / period

    avg_loss = sum(
        losses[:period]
    ) / period

    for i in range(
        period,
        len(gains)
    ):

        avg_gain = (
            avg_gain *
            (period - 1) +
            gains[i]
        ) / period

        avg_loss = (
            avg_loss *
            (period - 1) +
            losses[i]
        ) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss

    return 100 - (
        100 / (1 + rs)
    )


def atr(candles, period=14):
    if len(candles) < period + 1:
        return None

    trs = []

    for i in range(
        1,
        len(candles)
    ):

        current = candles[i]
        previous = candles[i - 1]

        tr = max(
            current["high"] -
            current["low"],

            abs(
                current["high"] -
                previous["close"]
            ),

            abs(
                current["low"] -
                previous["close"]
            ),
        )

        trs.append(tr)

    if len(trs) < period:
        return None

    return sum(
        trs[-period:]
    ) / period


def bollinger(candles, period=20):
    closes = [
        c["close"]
        for c in candles
    ]

    if len(closes) < period:
        return None, None, None

    values = closes[-period:]

    middle = sum(values) / period

    variance = sum(
        (x - middle) ** 2
        for x in values
    ) / period

    std = math.sqrt(
        variance
    )

    return (
        middle - 2 * std,
        middle,
        middle + 2 * std,
    )


def macd(values):
    if len(values) < 35:
        return None, None

    ema12 = ema_series(
        values,
        12
    )

    ema26 = ema_series(
        values,
        26
    )

    if not ema12 or not ema26:
        return None, None

    macd_values = []

    offset = 26 - 12

    for i in range(
        len(ema26)
    ):

        index12 = i + offset

        if index12 >= len(ema12):
            break

        macd_values.append(
            ema12[index12] -
            ema26[i]
        )

    if len(macd_values) < 9:
        return None, None

    signal = ema(
        macd_values,
        9
    )

    if signal is None:
        return None, None

    return (
        macd_values[-1],
        signal,
    )


def adx(candles, period=14):
    """
    Упрощённый ADX.
    """

    if len(candles) < period + 2:
        return None

    trs = []
    plus_dm = []
    minus_dm = []

    for i in range(
        1,
        len(candles)
    ):

        current = candles[i]
        previous = candles[i - 1]

        up_move = (
            current["high"] -
            previous["high"]
        )

        down_move = (
            previous["low"] -
            current["low"]
        )

        if (
            up_move > down_move
            and up_move > 0
        ):
            plus = up_move
        else:
            plus = 0

        if (
            down_move > up_move
            and down_move > 0
        ):
            minus = down_move
        else:
            minus = 0

        tr = max(
            current["high"] -
            current["low"],

            abs(
                current["high"] -
                previous["close"]
            ),

            abs(
                current["low"] -
                previous["close"]
            ),
        )

        trs.append(tr)
        plus_dm.append(plus)
        minus_dm.append(minus)

    if len(trs) < period:
        return None

    atr_value = sum(
        trs[-period:]
    ) / period

    if atr_value == 0:
        return None

    plus_di = (
        100 *
        (
            sum(plus_dm[-period:]) /
            period
        ) /
        atr_value
    )

    minus_di = (
        100 *
        (
            sum(minus_dm[-period:]) /
            period
        ) /
        atr_value
    )

    denominator = (
        plus_di +
        minus_di
    )

    if denominator == 0:
        return None

    dx = (
        100 *
        abs(
            plus_di -
            minus_di
        ) /
        denominator
    )

    return dx


# ============================================================
# ВСПОМОГАТЕЛЬНЫЙ SIGNAL
# ============================================================

def signal_result(signal, reason):
    return {
        "signal": signal,
        "reason": reason,
    }


# ============================================================
# 1. МОЯ СТРАТЕГИЯ
# ============================================================

def strategy_my(candles):

    if len(candles) < 8:

        return signal_result(
            "NONE",
            "Недостаточно свечей для пользовательской стратегии."
        )

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

    # SHORT:
    # последовательное повышение максимумов,
    # затем три снижающихся закрытия

    short_pattern = (
        highs[3] > highs[2]
        and highs[4] > highs[3]
        and highs[5] > highs[4]
        and closes[-1] < closes[-2]
        and closes[-2] < closes[-3]
    )

    # LONG:
    # последовательное снижение минимумов,
    # затем три повышающихся закрытия

    long_pattern = (
        lows[3] < lows[2]
        and lows[4] < lows[3]
        and lows[5] < lows[4]
        and closes[-1] > closes[-2]
        and closes[-2] > closes[-3]
    )

    if short_pattern:

        return signal_result(
            "SHORT",
            "Моя стратегия: максимумы последовательно росли, "
            "после чего появились три снижающихся закрытия."
        )

    if long_pattern:

        return signal_result(
            "LONG",
            "Моя стратегия: минимумы последовательно снижались, "
            "после чего появились три повышающихся закрытия."
        )

    return signal_result(
        "NONE",
        "Фигура пользовательской стратегии не сформирована."
    )


# Для совместимости со старым кодом
def analyze_strategy(candles):
    return strategy_my(candles)


# ============================================================
# 2. EMA
# ============================================================

def strategy_ema(candles):

    closes = [
        c["close"]
        for c in candles
    ]

    if len(closes) < 55:

        return signal_result(
            "NONE",
            "Недостаточно данных для EMA 20/50."
        )

    fast_now = ema(
        closes,
        20
    )

    fast_prev = ema(
        closes[:-1],
        20
    )

    slow_now = ema(
        closes,
        50
    )

    slow_prev = ema(
        closes[:-1],
        50
    )

    if None in (
        fast_now,
        fast_prev,
        slow_now,
        slow_prev,
    ):
        return signal_result(
            "NONE",
            "EMA ещё не рассчитаны."
        )

    if (
        fast_prev <= slow_prev
        and fast_now > slow_now
    ):

        return signal_result(
            "LONG",
            "EMA 20 пересекла EMA 50 снизу вверх."
        )

    if (
        fast_prev >= slow_prev
        and fast_now < slow_now
    ):

        return signal_result(
            "SHORT",
            "EMA 20 пересекла EMA 50 сверху вниз."
        )

    if fast_now > slow_now:

        return signal_result(
            "LONG",
            "EMA 20 находится выше EMA 50."
        )

    if fast_now < slow_now:

        return signal_result(
            "SHORT",
            "EMA 20 находится ниже EMA 50."
        )

    return signal_result(
        "NONE",
        "Пересечение EMA не подтверждено."
    )


# ============================================================
# 3. RSI
# ============================================================

def strategy_rsi(candles):

    closes = [
        c["close"]
        for c in candles
    ]

    value = rsi(
        closes,
        14
    )

    if value is None:

        return signal_result(
            "NONE",
            "Недостаточно данных для RSI."
        )

    if value < 30:

        return signal_result(
            "LONG",
            f"RSI={value:.2f}: зона перепроданности."
        )

    if value > 70:

        return signal_result(
            "SHORT",
            f"RSI={value:.2f}: зона перекупленности."
        )

    return signal_result(
        "NONE",
        f"RSI={value:.2f}: нейтральная зона."
    )


# ============================================================
# 4. BOLLINGER
# ============================================================

def strategy_bollinger(candles):

    lower, middle, upper = bollinger(
        candles,
        20
    )

    if lower is None:

        return signal_result(
            "NONE",
            "Недостаточно данных для Bollinger Bands."
        )

    price = candles[-1]["close"]

    if price <= lower:

        return signal_result(
            "LONG",
            "Цена коснулась/пробила нижнюю полосу Боллинджера."
        )

    if price >= upper:

        return signal_result(
            "SHORT",
            "Цена коснулась/пробила верхнюю полосу Боллинджера."
        )

    return signal_result(
        "NONE",
        "Цена находится внутри полос Боллинджера."
    )


# ============================================================
# 5. MACD
# ============================================================

def strategy_macd(candles):

    closes = [
        c["close"]
        for c in candles
    ]

    if len(closes) < 40:

        return signal_result(
            "NONE",
            "Недостаточно данных для MACD."
        )

    macd_now, signal_now = macd(
        closes
    )

    macd_prev, signal_prev = macd(
        closes[:-1]
    )

    if (
        macd_now is None
        or signal_now is None
        or macd_prev is None
        or signal_prev is None
    ):

        return signal_result(
            "NONE",
            "MACD ещё не рассчитан."
        )

    if (
        macd_prev <= signal_prev
        and macd_now > signal_now
    ):

        return signal_result(
            "LONG",
            "MACD пересёк сигнальную линию снизу вверх."
        )

    if (
        macd_prev >= signal_prev
        and macd_now < signal_now
    ):

        return signal_result(
            "SHORT",
            "MACD пересёк сигнальную линию сверху вниз."
        )

    if macd_now > signal_now:

        return signal_result(
            "LONG",
            "MACD выше сигнальной линии."
        )

    if macd_now < signal_now:

        return signal_result(
            "SHORT",
            "MACD ниже сигнальной линии."
        )

    return signal_result(
        "NONE",
        "MACD нейтрален."
    )


# ============================================================
# 6. DONCHIAN
# ============================================================

def strategy_donchian(candles):

    period = 20

    if len(candles) < period + 1:

        return signal_result(
            "NONE",
            "Недостаточно данных для Donchian."
        )

    previous = candles[-period - 1:-1]

    upper = max(
        c["high"]
        for c in previous
    )

    lower = min(
        c["low"]
        for c in previous
    )

    close = candles[-1]["close"]

    if close > upper:

        return signal_result(
            "LONG",
            "Цена пробила верхнюю границу Donchian."
        )

    if close < lower:

        return signal_result(
            "SHORT",
            "Цена пробила нижнюю границу Donchian."
        )

    return signal_result(
        "NONE",
        "Пробоя канала Donchian нет."
    )


# ============================================================
# 7. MOMENTUM
# ============================================================

def strategy_momentum(candles):

    period = 10

    if len(candles) < period + 1:

        return signal_result(
            "NONE",
            "Недостаточно данных для Momentum."
        )

    current = candles[-1]["close"]
    previous = candles[-period - 1]["close"]

    if current > previous:

        return signal_result(
            "LONG",
            "Momentum положительный."
        )

    if current < previous:

        return signal_result(
            "SHORT",
            "Momentum отрицательный."
        )

    return signal_result(
        "NONE",
        "Momentum нейтрален."
    )


# ============================================================
# 8. PRICE ACTION
# ============================================================

def strategy_price_action(candles):

    if len(candles) < 4:

        return signal_result(
            "NONE",
            "Недостаточно свечей."
        )

    c1 = candles[-1]
    c2 = candles[-2]
    c3 = candles[-3]

    body1 = c1["close"] - c1["open"]
    body2 = c2["close"] - c2["open"]
    body3 = c3["close"] - c3["open"]

    range1 = max(
        c1["high"] - c1["low"],
        1e-9
    )

    # Сильное бычье движение
    if (
        body1 > 0
        and body2 > 0
        and body3 > 0
        and abs(body1) / range1 > 0.5
    ):

        return signal_result(
            "LONG",
            "Три последовательные бычьи свечи с сильным телом."
        )

    # Сильное медвежье движение
    if (
        body1 < 0
        and body2 < 0
        and body3 < 0
        and abs(body1) / range1 > 0.5
    ):

        return signal_result(
            "SHORT",
            "Три последовательные медвежьи свечи с сильным телом."
        )

    return signal_result(
        "NONE",
        "Сильный Price Action сигнал не сформирован."
    )


# ============================================================
# 9. VWAP
# ============================================================

def strategy_vwap(candles):

    if not candles:

        return signal_result(
            "NONE",
            "Нет свечей."
        )

    total_volume = 0
    total_price_volume = 0

    # Последние 100 свечей
    sample = candles[-100:]

    for c in sample:

        typical_price = (
            c["high"] +
            c["low"] +
            c["close"]
        ) / 3

        volume = max(
            c.get("volume", 0),
            0
        )

        total_volume += volume

        total_price_volume += (
            typical_price *
            volume
        )

    if total_volume <= 0:

        return signal_result(
            "NONE",
            "Нет объёма для расчёта VWAP."
        )

    vwap = (
        total_price_volume /
        total_volume
    )

    price = candles[-1]["close"]

    if price > vwap:

        return signal_result(
            "LONG",
            f"Цена {price:.4f} выше VWAP {vwap:.4f}."
        )

    if price < vwap:

        return signal_result(
            "SHORT",
            f"Цена {price:.4f} ниже VWAP {vwap:.4f}."
        )

    return signal_result(
        "NONE",
        "Цена около VWAP."
    )


# ============================================================
# 10. EMA + RSI
# ============================================================

def strategy_ema_rsi(candles):

    closes = [
        c["close"]
        for c in candles
    ]

    if len(closes) < 55:

        return signal_result(
            "NONE",
            "Недостаточно данных для EMA + RSI."
        )

    ema20 = ema(
        closes,
        20
    )

    ema50 = ema(
        closes,
        50
    )

    rsi_value = rsi(
        closes,
        14
    )

    if (
        ema20 is None
        or ema50 is None
        or rsi_value is None
    ):

        return signal_result(
            "NONE",
            "Индикаторы ещё не рассчитаны."
        )

    if (
        ema20 > ema50
        and rsi_value > 50
    ):

        return signal_result(
            "LONG",
            f"EMA20 выше EMA50 и RSI={rsi_value:.2f} выше 50."
        )

    if (
        ema20 < ema50
        and rsi_value < 50
    ):

        return signal_result(
            "SHORT",
            f"EMA20 ниже EMA50 и RSI={rsi_value:.2f} ниже 50."
        )

    return signal_result(
        "NONE",
        "EMA и RSI не подтверждают направление одновременно."
    )


# ============================================================
# 11. ADX
# ============================================================

def strategy_adx(candles):

    if len(candles) < 20:

        return signal_result(
            "NONE",
            "Недостаточно данных для ADX."
        )

    value = adx(
        candles,
        14
    )

    if value is None:

        return signal_result(
            "NONE",
            "ADX не рассчитан."
        )

    if value < 20:

        return signal_result(
            "NONE",
            f"ADX={value:.2f}: выраженного тренда нет."
        )

    last = candles[-1]

    if last["close"] > last["open"]:

        return signal_result(
            "LONG",
            f"ADX={value:.2f}: тренд сильный, последняя свеча бычья."
        )

    if last["close"] < last["open"]:

        return signal_result(
            "SHORT",
            f"ADX={value:.2f}: тренд сильный, последняя свеча медвежья."
        )

    return signal_result(
        "NONE",
        f"ADX={value:.2f}, направление свечи нейтрально."
    )


# ============================================================
# 12. EMA 200
# ============================================================

def strategy_ema200(candles):

    closes = [
        c["close"]
        for c in candles
    ]

    if len(closes) < 205:

        return signal_result(
            "NONE",
            "Недостаточно свечей для EMA 200."
        )

    value = ema(
        closes,
        200
    )

    price = closes[-1]

    if price > value:

        return signal_result(
            "LONG",
            f"Цена выше EMA200 ({value:.4f})."
        )

    if price < value:

        return signal_result(
            "SHORT",
            f"Цена ниже EMA200 ({value:.4f})."
        )

    return signal_result(
        "NONE",
        "Цена около EMA200."
    )


# ============================================================
# 13. ATR
# ============================================================

def strategy_atr(candles):

    if len(candles) < 20:

        return signal_result(
            "NONE",
            "Недостаточно данных для ATR."
        )

    value = atr(
        candles,
        14
    )

    if value is None:

        return signal_result(
            "NONE",
            "ATR не рассчитан."
        )

    last = candles[-1]

    candle_range = (
        last["high"] -
        last["low"]
    )

    if candle_range > value * 1.5:

        if last["close"] > last["open"]:

            return signal_result(
                "LONG",
                f"Сильная бычья свеча: диапазон {candle_range:.4f} > 1.5 ATR."
            )

        if last["close"] < last["open"]:

            return signal_result(
                "SHORT",
                f"Сильная медвежья свеча: диапазон {candle_range:.4f} > 1.5 ATR."
            )

    return signal_result(
        "NONE",
        f"ATR={value:.4f}: сильного импульса нет."
    )


# ============================================================
# 14. BREAKOUT
# ============================================================

def strategy_breakout(candles):

    period = 10

    if len(candles) < period + 1:

        return signal_result(
            "NONE",
            "Недостаточно данных для Breakout."
        )

    previous = candles[-period - 1:-1]

    highest = max(
        c["high"]
        for c in previous
    )

    lowest = min(
        c["low"]
        for c in previous
    )

    close = candles[-1]["close"]

    if close > highest:

        return signal_result(
            "LONG",
            "Цена пробила максимум последних 10 свечей."
        )

    if close < lowest:

        return signal_result(
            "SHORT",
            "Цена пробила минимум последних 10 свечей."
        )

    return signal_result(
        "NONE",
        "Пробоя диапазона нет."
    )


# ============================================================
# ВСЕ СТРАТЕГИИ
# ============================================================

STRATEGIES = [
    {
        "id": "MY",
        "name": "Моя стратегия",
        "func": strategy_my,
    },
    {
        "id": "EMA",
        "name": "EMA 20/50",
        "func": strategy_ema,
    },
    {
        "id": "RSI",
        "name": "RSI",
        "func": strategy_rsi,
    },
    {
        "id": "BOLLINGER",
        "name": "Bollinger Bands",
        "func": strategy_bollinger,
    },
    {
        "id": "MACD",
        "name": "MACD",
        "func": strategy_macd,
    },
    {
        "id": "DONCHIAN",
        "name": "Donchian",
        "func": strategy_donchian,
    },
    {
        "id": "MOMENTUM",
        "name": "Momentum",
        "func": strategy_momentum,
    },
    {
        "id": "PRICE_ACTION",
        "name": "Price Action",
        "func": strategy_price_action,
    },
    {
        "id": "VWAP",
        "name": "VWAP",
        "func": strategy_vwap,
    },
    {
        "id": "EMA_RSI",
        "name": "EMA + RSI",
        "func": strategy_ema_rsi,
    },
    {
        "id": "ADX",
        "name": "ADX",
        "func": strategy_adx,
    },
    {
        "id": "EMA_200",
        "name": "EMA 200",
        "func": strategy_ema200,
    },
    {
        "id": "ATR",
        "name": "ATR",
        "func": strategy_atr,
    },
    {
        "id": "BREAKOUT",
        "name": "Breakout",
        "func": strategy_breakout,
    },
]


# ============================================================
# BACKTEST
# ============================================================

def calculate_trade_profit(
    entry_price,
    exit_price,
    direction
):
    if entry_price <= 0:
        return 0.0

    if direction == "LONG":

        change = (
            exit_price -
            entry_price
        ) / entry_price

    else:

        change = (
            entry_price -
            exit_price
        ) / entry_price

    gross = (
        POSITION_SIZE *
        change
    )

    commission = (
        POSITION_SIZE *
        COMMISSION_RATE
    )

    return gross - commission


def backtest_strategy(candles, strategy_func):
    """
    Walk-forward backtest.

    ВАЖНО:
    Каждая точка получает всю доступную историю candles[:i+1].
    Это исправляет старую ошибку, когда стратегии EMA200/EMA50
    фактически никогда не получали достаточную историю.
    """

    if len(candles) < 10:

        return {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0,
            "profit": 0,
            "gross_profit": 0,
            "gross_loss": 0,
            "drawdown": 0,
            "profit_factor": 0,
            "avg_trade": 0,
        }

    position = None

    entry_price = 0.0

    profits = []

    equity = 0.0

    peak = 0.0

    max_drawdown = 0.0

    # Начинаем с 8 свечей,
    # чтобы пользовательская стратегия могла работать
    for i in range(
        8,
        len(candles)
    ):

        history = candles[:i + 1]

        try:

            result = strategy_func(
                history
            )

            signal = result.get(
                "signal",
                "NONE"
            )

        except Exception:

            signal = "NONE"

        current_price = candles[i]["close"]

        # ----------------------------------------------------
        # Нет позиции
        # ----------------------------------------------------

        if position is None:

            if signal in (
                "LONG",
                "SHORT"
            ):

                position = signal
                entry_price = current_price

            continue

        # ----------------------------------------------------
        # LONG -> SHORT
        # ----------------------------------------------------

        if (
            position == "LONG"
            and signal == "SHORT"
        ):

            profit = calculate_trade_profit(
                entry_price,
                current_price,
                "LONG"
            )

            profits.append(
                profit
            )

            equity += profit

            peak = max(
                peak,
                equity
            )

            max_drawdown = max(
                max_drawdown,
                peak - equity
            )

            position = "SHORT"
            entry_price = current_price

            continue

        # ----------------------------------------------------
        # SHORT -> LONG
        # ----------------------------------------------------

        if (
            position == "SHORT"
            and signal == "LONG"
        ):

            profit = calculate_trade_profit(
                entry_price,
                current_price,
                "SHORT"
            )

            profits.append(
                profit
            )

            equity += profit

            peak = max(
                peak,
                equity
            )

            max_drawdown = max(
                max_drawdown,
                peak - equity
            )

            position = "LONG"
            entry_price = current_price

            continue

    # --------------------------------------------------------
    # Закрываем последнюю позицию
    # --------------------------------------------------------

    if position is not None:

        final_price = candles[-1]["close"]

        profit = calculate_trade_profit(
            entry_price,
            final_price,
            position
        )

        profits.append(
            profit
        )

        equity += profit

        peak = max(
            peak,
            equity
        )

        max_drawdown = max(
            max_drawdown,
            peak - equity
        )

    trades = len(profits)

    wins = sum(
        1
        for p in profits
        if p > 0
    )

    losses = sum(
        1
        for p in profits
        if p <= 0
    )

    win_rate = (
        wins /
        trades *
        100
        if trades > 0
        else 0
    )

    total_profit = sum(
        profits
    )

    gross_profit = sum(
        p
        for p in profits
        if p > 0
    )

    gross_loss = abs(
        sum(
            p
            for p in profits
            if p < 0
        )
    )

    if gross_loss > 0:

        profit_factor = (
            gross_profit /
            gross_loss
        )

    elif gross_profit > 0:

        profit_factor = 999.0

    else:

        profit_factor = 0.0

    avg_trade = (
        total_profit /
        trades
        if trades > 0
        else 0
    )

    # --------------------------------------------------------
    # Модельный налог
    # --------------------------------------------------------

    estimated_tax = (
        max(total_profit, 0)
        * TAX_RATE
    )

    net_profit = (
        total_profit -
        estimated_tax
    )

    return {
        "trades": trades,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "profit": total_profit,
        "net_profit": net_profit,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "drawdown": max_drawdown,
        "profit_factor": profit_factor,
        "avg_trade": avg_trade,
        "tax": estimated_tax,
    }


# ============================================================
# ТЕКУЩИЙ СИГНАЛ
# ============================================================

def get_current_strategy_signal(
    candles,
    strategy_func
):

    try:

        result = strategy_func(
            candles
        )

        return {
            "signal": result.get(
                "signal",
                "NONE"
            ),
            "reason": result.get(
                "reason",
                ""
            ),
        }

    except Exception as e:

        return {
            "signal": "NONE",
            "reason": f"Ошибка стратегии: {e}",
        }


# ============================================================
# ОЦЕНКА ВСЕХ СТРАТЕГИЙ
# ============================================================

def evaluate_all_strategies(candles):

    results = []

    for strategy in STRATEGIES:

        stats = backtest_strategy(
            candles,
            strategy["func"]
        )

        current = get_current_strategy_signal(
            candles,
            strategy["func"]
        )

        trades = stats["trades"]

        eligible = (
            trades >= MIN_TRADES
        )

        results.append({
            "strategy": strategy["id"],
            "strategy_name": strategy["name"],

            "trades": trades,
            "wins": stats["wins"],
            "losses": stats["losses"],

            "win_rate": stats["win_rate"],

            "profit": stats["profit"],
            "net_profit": stats["net_profit"],

            "drawdown": stats["drawdown"],

            "profit_factor": stats["profit_factor"],

            "avg_trade": stats["avg_trade"],

            "tax": stats["tax"],

            "current_signal": current["signal"],
            "current_reason": current["reason"],

            "eligible": eligible,
        })

    return results


# ============================================================
# ВЫБОР СТРАТЕГИИ
# ============================================================

def select_best_strategy(
    strategy_results
):
    """
    Автоматический выбор на основании исторического backtest.

    Приоритет:
    1. прибыль
    2. Profit Factor
    3. Win Rate
    4. меньшая просадка
    5. больше сделок

    Это техническое правило выбора,
    а не гарантия будущего результата.
    """

    if not strategy_results:

        return None, (
            "Нет результатов стратегий."
        )

    eligible = [
        x
        for x in strategy_results
        if x["trades"] >= MIN_TRADES
    ]

    if not eligible:

        eligible = [
            x
            for x in strategy_results
            if x["trades"] > 0
        ]

    if not eligible:

        return None, (
            "Ни одна стратегия не сформировала "
            "достаточного количества исторических сделок."
        )

    eligible.sort(
        key=lambda x: (
            x["profit"],
            x["profit_factor"],
            x["win_rate"],
            -x["drawdown"],
            x["trades"],
        ),
        reverse=True,
    )

    selected = eligible[0]

    reason = (
        f"Выбрана стратегия «{selected['strategy_name']}» "
        f"по заданному критерию backtest: "
        f"прибыль {selected['profit']:.2f}, "
        f"проходимость {selected['win_rate']:.2f}%, "
        f"Profit Factor {selected['profit_factor']:.2f}, "
        f"сделок {selected['trades']}, "
        f"просадка {selected['drawdown']:.2f}."
    )

    return selected, reason


# ============================================================
# АГРЕГИРОВАННАЯ СТАТИСТИКА
# ============================================================

def aggregate_results(results):

    trades = sum(
        x["trades"]
        for x in results
    )

    wins = sum(
        x["wins"]
        for x in results
    )

    losses = sum(
        x["losses"]
        for x in results
    )

    profit = sum(
        x["profit"]
        for x in results
    )

    net_profit = sum(
        x["net_profit"]
        for x in results
    )

    drawdown = sum(
        x["drawdown"]
        for x in results
    )

    win_rate = (
        wins /
        (wins + losses) *
        100
        if wins + losses > 0
        else 0
    )

    gross_profit = sum(
        x["gross_profit"]
        for x in results
    )

    gross_loss = sum(
        x["gross_loss"]
        for x in results
    )

    profit_factor = (
        gross_profit /
        gross_loss
        if gross_loss > 0
        else (
            999.0
            if gross_profit > 0
            else 0
        )
    )

    return {
        "trades": trades,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "profit": profit,
        "net_profit": net_profit,
        "drawdown": drawdown,
        "profit_factor": profit_factor,
    }


# ============================================================
# GLOBAL STATS
# ============================================================

def build_global_stats():

    selected_results = []

    all_strategy_results = []

    markets_count = 0

    with market_lock:

        snapshot = dict(
            market_data
        )

    for code, data in snapshot.items():

        if not isinstance(data, dict):
            continue

        selected = data.get(
            "selected"
        )

        if selected:

            selected_results.append(
                selected
            )

        for strategy in data.get(
            "strategies",
            []
        ):

            copy_item = dict(
                strategy
            )

            copy_item["market"] = code

            all_strategy_results.append(
                copy_item
            )

        if data.get("candles", 0) > 0:

            markets_count += 1

    # --------------------------------------------------------
    # Общая статистика выбранных стратегий
    # --------------------------------------------------------

    overall = aggregate_results(
        selected_results
    )

    # --------------------------------------------------------
    # Сводка каждой стратегии по всем рынкам
    # --------------------------------------------------------

    strategy_groups = {}

    for item in all_strategy_results:

        sid = item["strategy"]

        if sid not in strategy_groups:

            strategy_groups[sid] = []

        strategy_groups[sid].append(
            item
        )

    strategy_summary = []

    for strategy in STRATEGIES:

        sid = strategy["id"]

        group = strategy_groups.get(
            sid,
            []
        )

        stats = aggregate_results(
            group
        )

        markets = len(
            [
                x
                for x in group
                if x["trades"] > 0
            ]
        )

        strategy_summary.append({
            "strategy": sid,
            "strategy_name": strategy["name"],
            "markets": markets,
            **stats,
        })

    return {
        "markets": markets_count,
        "selected": overall,
        "strategies": strategy_summary,
    }


# ============================================================
# АНАЛИЗ ОДНОГО РЫНКА
# ============================================================

def analyze_market(
    market_code,
    instrument
):

    market_name = MARKETS[
        market_code
    ]["name"]

    logger.info(
        "Получение свечей: %s",
        market_name
    )

    candles = get_candles(
        instrument
    )

    if not candles:

        raise RuntimeError(
            f"{market_name}: T-Bank не вернул свечи."
        )

    logger.info(
        "%s: получено свечей %s",
        market_name,
        len(candles)
    )

    strategies = evaluate_all_strategies(
        candles
    )

    selected, reason = select_best_strategy(
        strategies
    )

    current_signal = {
        "signal": "NONE",
        "reason": "Нет выбранной стратегии.",
    }

    if selected:

        selected_strategy = next(
            (
                x
                for x in STRATEGIES
                if x["id"] ==
                selected["strategy"]
            ),
            None
        )

        if selected_strategy:

            current_signal = get_current_strategy_signal(
                candles,
                selected_strategy["func"]
            )

    return {
        "code": market_code,
        "name": market_name,

        "instrument": {
            "uid": instrument.get(
                "uid",
                ""
            ),
            "ticker": instrument.get(
                "ticker",
                ""
            ),
            "figi": instrument.get(
                "figi",
                ""
            ),
            "class_code": instrument.get(
                "class_code",
                ""
            ),
            "name": instrument.get(
                "name",
                ""
            ),
            "expiry": instrument.get(
                "expiry",
                ""
            ),
        },

        "candles": len(candles),

        "last_price": candles[-1]["close"],

        "last_candle_time": candles[-1]["time"],

        "strategies": strategies,

        "selected": selected,

        "selection_reason": reason,

        "current_signal": current_signal["signal"],

        "current_reason": current_signal["reason"],

        "updated": now_utc().isoformat(),

        "error": "",
    }


# ============================================================
# ФОНОВЫЙ АНАЛИЗ
# ============================================================

def collect_data():

    with state_lock:

        analysis_state["running"] = True
        analysis_state["started"] = (
            now_utc().isoformat()
        )
        analysis_state["finished"] = None
        analysis_state["error"] = ""
        analysis_state["cycle"] += 1
        analysis_state["message"] = (
            "Запуск анализа рынков..."
        )

    logger.info(
        "=============================="
    )

    logger.info(
        "MARKUS TRADE: начало анализа"
    )

    logger.info(
        "=============================="
    )

    errors = []

    for market_code in MARKETS:

        try:

            with state_lock:

                analysis_state["message"] = (
                    f"Анализируется "
                    f"{MARKETS[market_code]['name']}..."
                )

            logger.info(
                "Поиск контракта: %s",
                market_code
            )

            instrument = find_active_future(
                market_code
            )

            logger.info(
                "%s: %s / %s",
                market_code,
                instrument.get(
                    "ticker",
                    "-"
                ),
                instrument.get(
                    "uid",
                    "-"
                )
            )

            result = analyze_market(
                market_code,
                instrument
            )

            with market_lock:

                market_data[
                    market_code
                ] = result

        except Exception as e:

            logger.exception(
                "Ошибка %s",
                market_code
            )

            error_text = str(e)

            errors.append(
                f"{MARKETS[market_code]['name']}: "
                f"{error_text}"
            )

            with market_lock:

                market_data[
                    market_code
                ] = {
                    "code": market_code,
                    "name": MARKETS[
                        market_code
                    ]["name"],

                    "instrument": {
                        "uid": "",
                        "ticker": "",
                        "figi": "",
                        "class_code": "",
                        "name": "",
                        "expiry": "",
                    },

                    "candles": 0,

                    "last_price": 0,

                    "strategies": [],

                    "selected": None,

                    "selection_reason": "",

                    "current_signal": "NONE",

                    "current_reason": "",

                    "updated": now_utc().isoformat(),

                    "error": error_text,
                }

    with state_lock:

        analysis_state["running"] = False

        analysis_state["finished"] = (
            now_utc().isoformat()
        )

        if errors:

            analysis_state["error"] = (
                " | ".join(errors)
            )

            analysis_state["message"] = (
                "Анализ завершён с ошибками."
            )

        else:

            analysis_state["error"] = ""

            analysis_state["message"] = (
                "Анализ всех рынков завершён."
            )

    logger.info(
        "MARKUS TRADE: анализ завершён"
    )


# ============================================================
# ЗАПУСК ФОНА
# ============================================================

def start_background_analysis():

    thread = threading.Thread(
        target=collect_data,
        daemon=True,
    )

    thread.start()

    return thread


# ============================================================
# HTML
# ============================================================

HTML = r"""
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
    background: #070707;
    color: #f2f2f2;
    font-family:
        -apple-system,
        BlinkMacSystemFont,
        "Segoe UI",
        Arial,
        sans-serif;
}

.container {
    max-width: 1500px;
    margin: auto;
    padding: 20px;
}

.header {
    background: #101010;
    border: 1px solid #242424;
    border-radius: 18px;
    padding: 22px;
    margin-bottom: 18px;
}

.logo {
    font-size: 30px;
    font-weight: 800;
}

.subtitle {
    color: #999;
    margin-top: 5px;
}

.status {
    margin-top: 15px;
    padding: 12px;
    border-radius: 12px;
    background: #171717;
}

.status.running {
    border: 1px solid #6b5a22;
}

.status.ok {
    border: 1px solid #285b37;
}

.status.error {
    border: 1px solid #6b2929;
}

.stats {
    display: grid;
    grid-template-columns:
        repeat(
            auto-fit,
            minmax(170px, 1fr)
        );
    gap: 12px;
    margin-bottom: 20px;
}

.stat {
    background: #101010;
    border: 1px solid #242424;
    border-radius: 15px;
    padding: 17px;
}

.stat-title {
    color: #8f8f8f;
    font-size: 13px;
}

.stat-value {
    font-size: 25px;
    font-weight: 800;
    margin-top: 8px;
}

.market-grid {
    display: grid;
    grid-template-columns:
        repeat(
            auto-fit,
            minmax(330px, 1fr)
        );
    gap: 18px;
}

.market {
    background: #101010;
    border: 1px solid #252525;
    border-radius: 18px;
    overflow: hidden;
}

.market-header {
    padding: 18px;
    border-bottom: 1px solid #252525;
}

.market-title {
    font-size: 23px;
    font-weight: 800;
}

.instrument {
    color: #888;
    font-size: 12px;
    margin-top: 6px;
    word-break: break-all;
}

.market-body {
    padding: 18px;
}

.selected {
    background: #151515;
    border: 1px solid #5d4e1c;
    border-radius: 15px;
    padding: 16px;
}

.selected-title {
    color: #b9a04a;
    font-size: 13px;
    font-weight: 700;
    text-transform: uppercase;
}

.selected-name {
    font-size: 21px;
    font-weight: 800;
    margin-top: 6px;
}

.signal {
    margin-top: 12px;
    padding: 10px;
    border-radius: 10px;
    text-align: center;
    font-size: 20px;
    font-weight: 900;
}

.long {
    background: #12351e;
    color: #65e28b;
}

.short {
    background: #3b1717;
    color: #ff7070;
}

.none {
    background: #202020;
    color: #aaa;
}

.reason {
    color: #aaa;
    font-size: 13px;
    line-height: 1.5;
    margin-top: 12px;
}

.metrics {
    display: grid;
    grid-template-columns:
        repeat(
            2,
            1fr
        );
    gap: 8px;
    margin-top: 14px;
}

.metric {
    background: #0b0b0b;
    border-radius: 9px;
    padding: 9px;
}

.metric-name {
    color: #777;
    font-size: 11px;
}

.metric-value {
    font-size: 15px;
    font-weight: 700;
    margin-top: 4px;
}

details {
    margin-top: 15px;
}

summary {
    cursor: pointer;
    color: #c4aa52;
    padding: 10px 0;
    font-weight: 700;
}

.table-wrap {
    overflow-x: auto;
}

table {
    width: 100%;
    border-collapse: collapse;
    min-width: 850px;
}

th,
td {
    padding: 9px 7px;
    border-bottom: 1px solid #252525;
    text-align: left;
    font-size: 12px;
}

th {
    color: #8d8d8d;
    font-weight: 600;
}

tr.selected-row {
    background: #1b1810;
}

.profit-positive {
    color: #68dc8b;
}

.profit-negative {
    color: #ff7070;
}

.error-box {
    background: #321515;
    color: #ff8a8a;
    padding: 12px;
    border-radius: 10px;
    margin-top: 12px;
    font-size: 13px;
    line-height: 1.5;
}

.global {
    margin-top: 22px;
    background: #101010;
    border: 1px solid #252525;
    border-radius: 18px;
    padding: 18px;
}

h2 {
    margin-top: 0;
}

.small {
    color: #777;
    font-size: 12px;
}

.refresh {
    display: inline-block;
    margin-top: 12px;
    color: #bca45a;
    font-size: 12px;
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
            Автоматический анализ 14 торговых стратегий
        </div>

        <div
            id="status"
            class="status running"
        >
            Запуск анализа...
        </div>

        <div
            id="updated"
            class="refresh"
        >
            Загрузка...
        </div>

    </div>


    <div class="stats">

        <div class="stat">
            <div class="stat-title">
                Общая прибыль
            </div>

            <div
                id="totalProfit"
                class="stat-value"
            >
                —
            </div>
        </div>


        <div class="stat">
            <div class="stat-title">
                Общая проходимость
            </div>

            <div
                id="winRate"
                class="stat-value"
            >
                —
            </div>
        </div>


        <div class="stat">
            <div class="stat-title">
                Всего сделок
            </div>

            <div
                id="totalTrades"
                class="stat-value"
            >
                —
            </div>
        </div>


        <div class="stat">
            <div class="stat-title">
                Прибыльных
            </div>

            <div
                id="totalWins"
                class="stat-value"
            >
                —
            </div>
        </div>


        <div class="stat">
            <div class="stat-title">
                Убыточных
            </div>

            <div
                id="totalLosses"
                class="stat-value"
            >
                —
            </div>
        </div>


        <div class="stat">
            <div class="stat-title">
                Общая просадка
            </div>

            <div
                id="drawdown"
                class="stat-value"
            >
                —
            </div>
        </div>

    </div>


    <div
        id="markets"
        class="market-grid"
    ></div>


    <div class="global">

        <h2>
            Сводка всех 14 стратегий
        </h2>

        <div class="small">
            Статистика каждой стратегии по всем доступным фьючерсам.
        </div>

        <div
            id="globalStrategies"
            class="table-wrap"
        >
            Загрузка...
        </div>

    </div>

</div>


<script>

function money(value) {

    if (
        value === undefined ||
        value === null
    ) {
        return "0.00";
    }

    return Number(value).toLocaleString(
        "ru-RU",
        {
            minimumFractionDigits: 2,
            maximumFractionDigits: 2
        }
    );
}


function pct(value) {

    if (
        value === undefined ||
        value === null
    ) {
        return "0.00%";
    }

    return Number(value).toFixed(2) + "%";
}


function profitClass(value) {

    return Number(value) >= 0
        ? "profit-positive"
        : "profit-negative";
}


function signalHtml(signal) {

    if (signal === "LONG") {

        return `
            <div class="signal long">
                ▲ LONG
            </div>
        `;
    }

    if (signal === "SHORT") {

        return `
            <div class="signal short">
                ▼ SHORT
            </div>
        `;
    }

    return `
        <div class="signal none">
            — НЕТ СИГНАЛА
        </div>
    `;
}


function renderMarket(code, data) {

    const instrument =
        data.instrument || {};

    const selected =
        data.selected;

    let selectedHtml = "";

    if (selected) {

        selectedHtml = `

            <div class="selected">

                <div class="selected-title">
                    Выбрана по backtest
                </div>

                <div class="selected-name">
                    ${selected.strategy_name}
                </div>

                ${signalHtml(
                    data.current_signal
                )}

                <div class="reason">
                    ${data.selection_reason || ""}
                </div>

                <div class="metrics">

                    <div class="metric">
                        <div class="metric-name">
                            Проходимость
                        </div>

                        <div class="metric-value">
                            ${pct(selected.win_rate)}
                        </div>
                    </div>


                    <div class="metric">
                        <div class="metric-name">
                            Прибыль
                        </div>

                        <div
                            class="metric-value ${profitClass(selected.profit)}"
                        >
                            ${money(selected.profit)}
                        </div>
                    </div>


                    <div class="metric">
                        <div class="metric-name">
                            Сделки
                        </div>

                        <div class="metric-value">
                            ${selected.trades}
                        </div>
                    </div>


                    <div class="metric">
                        <div class="metric-name">
                            Прибыльных
                        </div>

                        <div class="metric-value">
                            ${selected.wins}
                        </div>
                    </div>


                    <div class="metric">
                        <div class="metric-name">
                            Убыточных
                        </div>

                        <div class="metric-value">
                            ${selected.losses}
                        </div>
                    </div>


                    <div class="metric">
                        <div class="metric-name">
                            Profit Factor
                        </div>

                        <div class="metric-value">
                            ${Number(
                                selected.profit_factor || 0
                            ).toFixed(2)}
                        </div>
                    </div>


                    <div class="metric">
                        <div class="metric-name">
                            Средняя сделка
                        </div>

                        <div class="metric-value">
                            ${money(
                                selected.avg_trade
                            )}
                        </div>
                    </div>


                    <div class="metric">
                        <div class="metric-name">
                            Просадка
                        </div>

                        <div class="metric-value">
                            ${money(
                                selected.drawdown
                            )}
                        </div>
                    </div>

                </div>

                <div class="reason">
                    Текущий сигнал:
                    ${data.current_reason || "—"}
                </div>

            </div>

        `;

    } else {

        selectedHtml = `

            <div class="selected">

                <div class="selected-title">
                    Стратегия
                </div>

                <div class="selected-name">
                    Пока не выбрана
                </div>

                <div class="reason">
                    ${data.selection_reason || ""}
                </div>

            </div>

        `;
    }


    let rows = "";

    const strategies =
        data.strategies || [];


    for (
        const s of strategies
    ) {

        const isSelected =
            selected &&
            selected.strategy ===
            s.strategy;

        rows += `

            <tr
                class="${isSelected ? "selected-row" : ""}"
            >

                <td>
                    ${s.strategy_name}
                </td>

                <td>
                    ${s.trades}
                </td>

                <td>
                    ${s.wins}
                </td>

                <td>
                    ${s.losses}
                </td>

                <td>
                    ${pct(s.win_rate)}
                </td>

                <td
                    class="${profitClass(s.profit)}"
                >
                    ${money(s.profit)}
                </td>

                <td>
                    ${money(s.drawdown)}
                </td>

                <td>
                    ${Number(
                        s.profit_factor || 0
                    ).toFixed(2)}
                </td>

                <td>
                    ${s.current_signal}
                </td>

            </tr>

        `;
    }


    let errorHtml = "";

    if (data.error) {

        errorHtml = `

            <div class="error-box">
                ${data.error}
            </div>

        `;
    }


    return `

        <div class="market">

            <div class="market-header">

                <div class="market-title">
                    ${data.name}
                </div>

                <div class="instrument">

                    Тикер:
                    ${instrument.ticker || "—"}

                    <br>

                    UID:
                    ${instrument.uid || "—"}

                    <br>

                    Свечей:
                    ${data.candles || 0}

                    <br>

                    Цена:
                    ${money(data.last_price || 0)}

                </div>

            </div>


            <div class="market-body">

                ${selectedHtml}

                ${errorHtml}


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
                                        +
                                    </th>

                                    <th>
                                        -
                                    </th>

                                    <th>
                                        %
                                    </th>

                                    <th>
                                        Прибыль
                                    </th>

                                    <th>
                                        DD
                                    </th>

                                    <th>
                                        PF
                                    </th>

                                    <th>
                                        Сигнал
                                    </th>

                                </tr>

                            </thead>

                            <tbody>

                                ${rows}

                            </tbody>

                        </table>

                    </div>

                </details>

            </div>

        </div>

    `;
}


function renderGlobalStrategies(
    strategies
) {

    if (!strategies) {

        return "Нет данных";
    }

    let rows = "";

    for (
        const s of strategies
    ) {

        rows += `

            <tr>

                <td>
                    ${s.strategy_name}
                </td>

                <td>
                    ${s.markets}
                </td>

                <td>
                    ${s.trades}
                </td>

                <td>
                    ${s.wins}
                </td>

                <td>
                    ${s.losses}
                </td>

                <td>
                    ${pct(s.win_rate)}
                </td>

                <td
                    class="${profitClass(s.profit)}"
                >
                    ${money(s.profit)}
                </td>

                <td>
                    ${money(s.drawdown)}
                </td>

                <td>
                    ${Number(
                        s.profit_factor || 0
                    ).toFixed(2)}
                </td>

            </tr>

        `;
    }


    return `

        <table>

            <thead>

                <tr>

                    <th>
                        Стратегия
                    </th>

                    <th>
                        Рынков
                    </th>

                    <th>
                        Сделки
                    </th>

                    <th>
                        +
                    </th>

                    <th>
                        -
                    </th>

                    <th>
                        Проходимость
                    </th>

                    <th>
                        Прибыль
                    </th>

                    <th>
                        Просадка
                    </th>

                    <th>
                        Profit Factor
                    </th>

                </tr>

            </thead>

            <tbody>

                ${rows}

            </tbody>

        </table>

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


        const status =
            document.getElementById(
                "status"
            );


        if (data.state.running) {

            status.className =
                "status running";

            status.innerHTML =
                "⏳ " +
                data.state.message;

        } else if (
            data.state.error
        ) {

            status.className =
                "status error";

            status.innerHTML =
                "⚠️ " +
                data.state.message;

        } else {

            status.className =
                "status ok";

            status.innerHTML =
                "✓ " +
                data.state.message;
        }


        const updated =
            document.getElementById(
                "updated"
            );

        updated.innerHTML =
            "Цикл анализа: " +
            data.state.cycle +
            " · " +
            new Date().toLocaleTimeString();


        const global =
            data.global.selected || {};


        document.getElementById(
            "totalProfit"
        ).innerHTML =
            money(global.profit);


        document.getElementById(
            "winRate"
        ).innerHTML =
            pct(global.win_rate);


        document.getElementById(
            "totalTrades"
        ).innerHTML =
            global.trades || 0;


        document.getElementById(
            "totalWins"
        ).innerHTML =
            global.wins || 0;


        document.getElementById(
            "totalLosses"
        ).innerHTML =
            global.losses || 0;


        document.getElementById(
            "drawdown"
        ).innerHTML =
            money(global.drawdown);


        const markets =
            document.getElementById(
                "markets"
            );

        let html = "";

        for (
            const code of [
                "CR",
                "GD",
                "BR"
            ]
        ) {

            if (
                data.markets &&
                data.markets[code]
            ) {

                html +=
                    renderMarket(
                        code,
                        data.markets[code]
                    );

            } else {

                html += `

                    <div class="market">

                        <div class="market-header">

                            <div class="market-title">
                                ${code}
                            </div>

                        </div>

                        <div class="market-body">

                            Ожидание данных...

                        </div>

                    </div>

                `;
            }
        }


        markets.innerHTML =
            html;


        document.getElementById(
            "globalStrategies"
        ).innerHTML =
            renderGlobalStrategies(
                data.global.strategies
            );


    } catch (error) {

        document.getElementById(
            "status"
        ).innerHTML =
            "Ошибка интерфейса: " +
            error;

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
# ROUTES
# ============================================================

@app.route("/")
def index():

    return render_template_string(
        HTML
    )


@app.route("/api/status")
def api_status():

    with state_lock:

        state = dict(
            analysis_state
        )

    with market_lock:

        markets = dict(
            market_data
        )

    global_stats = build_global_stats()

    return jsonify({
        "state": state,
        "markets": markets,
        "global": global_stats,
    })


@app.route("/api/strategies")
def api_strategies():

    with market_lock:

        markets = dict(
            market_data
        )

    return jsonify({
        "markets": markets,
        "global": build_global_stats(),
    })


@app.route("/api/health")
def health():

    return jsonify({
        "status": "ok",
        "token_configured": bool(TOKEN),
        "time": now_utc().isoformat(),
    })


# ============================================================
# STARTUP
# ============================================================

def startup():

    logger.info(
        "======================================"
    )

    logger.info(
        "MARKUS TRADE START"
    )

    logger.info(
        "Token configured: %s",
        bool(TOKEN)
    )

    logger.info(
        "======================================"
    )

    # Запускаем анализ только один раз
    start_background_analysis()


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    startup()

    port = int(
        os.getenv(
            "PORT",
            "5000"
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        threaded=True,
    )
