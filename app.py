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

API_BASE = "https://invest-public-api.tbank.ru/rest"

FIND_INSTRUMENT_URL = (
    API_BASE +
    "/tinkoff.public.invest.api.contract.v1.InstrumentsService/FindInstrument"
)

FUTURES_URL = (
    API_BASE +
    "/tinkoff.public.invest.api.contract.v1.InstrumentsService/Futures"
)

CANDLES_URL = (
    API_BASE +
    "/tinkoff.public.invest.api.contract.v1.MarketDataService/GetCandles"
)


# ============================================================
# НАСТРОЙКИ
# ============================================================

REQUEST_TIMEOUT = 30

# Таймфрейм
CANDLE_INTERVAL = "CANDLE_INTERVAL_15_MIN"

# История
HISTORY_HOURS = 24 * 14

# Обновление
UPDATE_SECONDS = 300


# ============================================================
# ТОРГОВЫЕ НАСТРОЙКИ
# ============================================================

# Базовый размер виртуальной сделки
POSITION_SIZE_RUBLES = 100000.0

# Комиссия покупки
BUY_COMMISSION_PERCENT = 0.10

# Комиссия продажи
SELL_COMMISSION_PERCENT = 0.10

# Налог
TAX_PERCENT = 13.0


# ============================================================
# МАРТИНГЕЙЛ
# ============================================================

MARTINGALE_ENABLED = True

# Во сколько раз увеличивать следующую позицию
MARTINGALE_MULTIPLIER = 2.0

# Максимальное количество увеличений
# 0 = 100 000
# 1 = 200 000
# 2 = 400 000
# 3 = 800 000
MARTINGALE_MAX_STEP = 3


# ============================================================
# ИСТОРИЯ
# ============================================================

HISTORY_FILE = "trade_history.json"


# ============================================================
# SSL
# ============================================================

urllib3.disable_warnings(
    urllib3.exceptions.InsecureRequestWarning
)

warnings.filterwarnings("ignore")


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

log = logging.getLogger("MARKUS_TRADE")


# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)


# ============================================================
# ПОЛУЧЕНИЕ TOKEN
# ============================================================

def get_token():

    possible_names = [
        "TINKOFF_TOKEN",
        "TINVEST_TOKEN",
        "T_BANK_TOKEN",
        "API_TOKEN",
        "TOKEN"
    ]

    for name in possible_names:

        value = os.environ.get(name)

        if value:

            value = value.strip()

            if value:
                log.info(
                    "API-токен найден: %s",
                    name
                )

                return value

    return None


# ============================================================
# API POST
# ============================================================

def api_post(url, payload):

    token = get_token()

    if not token:
        raise RuntimeError(
            "API-токен не найден. Проверь переменную TINKOFF_TOKEN."
        )

    headers = {
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json"
    }

    response = requests.post(
        url,
        headers=headers,
        json=payload,
        timeout=REQUEST_TIMEOUT,
        verify=False
    )

    if response.status_code != 200:

        raise RuntimeError(
            f"HTTP {response.status_code}: {response.text[:1000]}"
        )

    try:

        return response.json()

    except Exception:

        raise RuntimeError(
            "T-Bank вернул ответ, который не удалось прочитать как JSON."
        )


# ============================================================
# ПОЛУЧЕНИЕ ВСЕХ ФЬЮЧЕРСОВ
# ============================================================

def get_all_futures():

    payload = {
        "instrumentStatus": "INSTRUMENT_STATUS_BASE"
    }

    try:

        data = api_post(
            FUTURES_URL,
            payload
        )

    except Exception:

        payload = {
            "instrumentStatus": "INSTRUMENT_STATUS_ALL"
        }

        data = api_post(
            FUTURES_URL,
            payload
        )

    futures = data.get(
        "futures",
        []
    )

    if not isinstance(futures, list):
        futures = []

    return futures


# ============================================================
# ПОЛУЧЕНИЕ СТРОКИ
# ============================================================

def get_string(obj, key):

    value = obj.get(key)

    if value is None:
        return ""

    return str(value)


# ============================================================
# ПАРСИНГ ДАТЫ
# ============================================================

def parse_date(value):

    if not value:
        return None

    try:

        text = str(value)

        if text.endswith("Z"):
            text = text[:-1] + "+00:00"

        dt = datetime.fromisoformat(text)

        if dt.tzinfo is None:
            dt = dt.replace(
                tzinfo=timezone.utc
            )

        return dt

    except Exception:

        return None


# ============================================================
# ПРОВЕРКА ФЬЮЧЕРСА
# ============================================================

def matches_future(future, prefix):

    ticker = get_string(
        future,
        "ticker"
    ).upper()

    name = get_string(
        future,
        "name"
    ).upper()

    basic_asset = get_string(
        future,
        "basicAsset"
    ).upper()

    text = " ".join(
        [
            ticker,
            name,
            basic_asset
        ]
    )

    prefix = prefix.upper()

    # ЮАНЬ
    if prefix == "CR":

        keywords = [
            "CR",
            "CNY",
            "YUAN",
            "CNH",
            "ЮАН",
            "КИТАЙ"
        ]

        return any(
            word in text
            for word in keywords
        )

    # ЗОЛОТО
    if prefix == "GD":

        keywords = [
            "GD",
            "GOLD",
            "ЗОЛОТ"
        ]

        return any(
            word in text
            for word in keywords
        )

    # НЕФТЬ
    if prefix == "BR":

        keywords = [
            "BR",
            "BRENT",
            "НЕФТ"
        ]

        return any(
            word in text
            for word in keywords
        )

    return False


# ============================================================
# ПОИСК АКТУАЛЬНОГО ФЬЮЧЕРСА
# ============================================================

def find_active_future(prefix):

    queries = [prefix]

    if prefix == "CR":

        queries = [
            "CR",
            "CNY",
            "юань",
            "CNY/RUB"
        ]

    elif prefix == "GD":

        queries = [
            "GD",
            "GOLD",
            "золото"
        ]

    elif prefix == "BR":

        queries = [
            "BR",
            "BRENT",
            "нефть"
        ]

    candidates = []

    now = datetime.now(
        timezone.utc
    )

    for query in queries:

        try:

            payload = {
                "query": query,
                "instrumentKind":
                    "INSTRUMENT_TYPE_FUTURES",
                "apiTradeAvailableFlag": True
            }

            data = api_post(
                FIND_INSTRUMENT_URL,
                payload
            )

        except Exception as e:

            log.warning(
                "Ошибка FindInstrument %s: %s",
                query,
                e
            )

            continue

        instruments = data.get(
            "instruments",
            []
        )

        if not isinstance(
            instruments,
            list
        ):
            continue

        for item in instruments:

            if not isinstance(
                item,
                dict
            ):
                continue

            if not matches_future(
                item,
                prefix
            ):
                continue

            instrument_uid = (
                item.get("instrumentUid")
                or item.get("uid")
            )

            if not instrument_uid:
                continue

            ticker = get_string(
                item,
                "ticker"
            )

            name = get_string(
                item,
                "name"
            )

            first_trade = parse_date(
                item.get("firstTradeDate")
            )

            last_trade = parse_date(
                item.get("lastTradeDate")
            )

            if first_trade and now < first_trade:
                continue

            if last_trade and now > last_trade:
                continue

            candidates.append(
                {
                    "ticker": ticker,
                    "name": name,
                    "uid": instrument_uid,
                    "instrument_uid": instrument_uid,
                    "first_trade": first_trade,
                    "last_trade": last_trade,
                    "class_code":
                        get_string(
                            item,
                            "classCode"
                        ),
                    "basic_asset":
                        get_string(
                            item,
                            "basicAsset"
                        )
                }
            )

    unique = {}

    for item in candidates:

        unique[
            item["instrument_uid"]
        ] = item

    candidates = list(
        unique.values()
    )

    if not candidates:
        return None

    def expiry_key(item):

        dt = item.get("last_trade")

        if dt:
            return dt

        return datetime.max.replace(
            tzinfo=timezone.utc
        )

    candidates.sort(
        key=expiry_key
    )

    selected = candidates[0]

    log.info(
        "Выбран фьючерс %s | %s | UID=%s",
        selected["ticker"],
        selected["name"],
        selected["instrument_uid"]
    )

    return selected


# ============================================================
# ПОЛУЧЕНИЕ СВЕЧЕЙ
# ============================================================

def get_candles(instrument_uid):

    now = datetime.now(
        timezone.utc
    )

    start = (
        now -
        timedelta(
            hours=HISTORY_HOURS
        )
    )

    payload = {
        "from": start.isoformat(),
        "to": now.isoformat(),
        "interval": CANDLE_INTERVAL,
        "instrumentId": instrument_uid,
        "candleSourceType":
            "CANDLE_SOURCE_EXCHANGE"
    }

    data = api_post(
        CANDLES_URL,
        payload
    )

    candles = data.get(
        "candles",
        []
    )

    if not isinstance(
        candles,
        list
    ):
        return []

    return candles


# ============================================================
# QUOTATION -> FLOAT
# ============================================================

def quotation_to_float(value):

    if value is None:
        return 0.0

    if isinstance(
        value,
        (int, float)
    ):
        return float(value)

    if isinstance(
        value,
        dict
    ):

        units = value.get(
            "units",
            0
        )

        nano = value.get(
            "nano",
            0
        )

        try:

            return (
                float(units)
                +
                float(nano) / 1_000_000_000
            )

        except Exception:

            return 0.0

    try:

        return float(
            str(value)
        )

    except Exception:

        return 0.0


# ============================================================
# НОРМАЛИЗАЦИЯ СВЕЧЕЙ
# ============================================================

def normalize_candles(candles):

    result = []

    for candle in candles:

        if not isinstance(
            candle,
            dict
        ):
            continue

        dt = parse_date(
            candle.get("time")
        )

        if not dt:
            continue

        item = {
            "time": dt.isoformat(),

            "open":
                quotation_to_float(
                    candle.get("open")
                ),

            "high":
                quotation_to_float(
                    candle.get("high")
                ),

            "low":
                quotation_to_float(
                    candle.get("low")
                ),

            "close":
                quotation_to_float(
                    candle.get("close")
                ),

            "volume":
                quotation_to_float(
                    candle.get("volume")
                )
        }

        if item["close"] <= 0:
            continue

        result.append(item)

    result.sort(
        key=lambda x: x["time"]
    )

    return result


# ============================================================
# ТВОЯ СТРАТЕГИЯ
# ============================================================

def analyze_strategy(candles):

    if len(candles) < 8:

        return {
            "signal": "Нет сигналов",
            "direction": "—",
            "description":
                "Недостаточно свечей"
        }

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

    # ========================================================
    # SHORT
    # ========================================================

    short_pattern = (
        highs[3] > highs[2]
        and
        highs[4] > highs[3]
        and
        highs[5] > highs[4]
        and
        closes[-1] < closes[-2]
        and
        closes[-2] < closes[-3]
    )

    # ========================================================
    # LONG
    # ========================================================

    long_pattern = (
        lows[3] < lows[2]
        and
        lows[4] < lows[3]
        and
        lows[5] < lows[4]
        and
        closes[-1] > closes[-2]
        and
        closes[-2] > closes[-3]
    )

    if short_pattern:

        return {
            "signal": "SHORT",
            "direction": "Вниз",
            "description":
                "Сформирован SHORT-сигнал"
        }

    if long_pattern:

        return {
            "signal": "LONG",
            "direction": "Вверх",
            "description":
                "Сформирован LONG-сигнал"
        }

    return {
        "signal": "Нет сигналов",
        "direction": "—",
        "description":
            "Сигнал не сформирован"
    }


# ============================================================
# ИСТОРИЯ
# ============================================================

def load_history():

    if not os.path.exists(
        HISTORY_FILE
    ):
        return []

    try:

        with open(
            HISTORY_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            data = json.load(f)

        if isinstance(
            data,
            list
        ):
            return data

    except Exception as e:

        log.warning(
            "Ошибка чтения истории: %s",
            e
        )

    return []


def save_history(history):

    try:

        with open(
            HISTORY_FILE,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                history,
                f,
                ensure_ascii=False,
                indent=2
            )

    except Exception as e:

        log.error(
            "Ошибка сохранения истории: %s",
            e
        )


# ============================================================
# РАСЧЕТ КОМИССИИ
# ============================================================

def calculate_commission(
    amount,
    percent
):

    return (
        amount *
        percent /
        100.0
    )


# ============================================================
# РАЗМЕР ПОЗИЦИИ МАРТИНГЕЙЛА
# ============================================================

def get_martingale_position_size(step):

    if not MARTINGALE_ENABLED:

        return POSITION_SIZE_RUBLES

    safe_step = max(
        0,
        min(
            int(step),
            MARTINGALE_MAX_STEP
        )
    )

    return (
        POSITION_SIZE_RUBLES
        *
        (
            MARTINGALE_MULTIPLIER
            ** safe_step
        )
    )


# ============================================================
# РАСЧЕТ СДЕЛКИ
# ============================================================

def calculate_trade_result(
    direction,
    entry_price,
    exit_price,
    position_size=None
):

    if entry_price <= 0:
        return None

    if position_size is None:

        position_size = (
            POSITION_SIZE_RUBLES
        )

    if position_size <= 0:
        return None

    if direction == "LONG":

        price_change_percent = (
            (
                exit_price -
                entry_price
            )
            /
            entry_price
        ) * 100

    else:

        price_change_percent = (
            (
                entry_price -
                exit_price
            )
            /
            entry_price
        ) * 100

    gross_result = (
        position_size *
        price_change_percent /
        100.0
    )

    buy_commission = calculate_commission(
        position_size,
        BUY_COMMISSION_PERCENT
    )

    exit_amount = (
        position_size
        *
        (
            exit_price /
            entry_price
        )
    )

    sell_commission = calculate_commission(
        abs(exit_amount),
        SELL_COMMISSION_PERCENT
    )

    if gross_result > 0:

        tax = (
            gross_result *
            TAX_PERCENT /
            100.0
        )

    else:

        tax = 0.0

    net_result = (
        gross_result
        -
        buy_commission
        -
        sell_commission
        -
        tax
    )

    return {
        "position_size":
            round(
                position_size,
                2
            ),

        "price_change_percent":
            round(
                price_change_percent,
                4
            ),

        "gross_result":
            round(
                gross_result,
                2
            ),

        "buy_commission":
            round(
                buy_commission,
                2
            ),

        "sell_commission":
            round(
                sell_commission,
                2
            ),

        "tax":
            round(
                tax,
                2
            ),

        "net_result":
            round(
                net_result,
                2
            )
    }


# ============================================================
# ПОСТРОЕНИЕ ИСТОРИИ СТРАТЕГИИ
# ============================================================

def build_strategy_history(
    candles,
    instrument,
    title
):

    if len(candles) < 8:
        return [], None

    trades = []

    current_position = None

    # ========================================================
    # СОСТОЯНИЕ МАРТИНГЕЙЛА
    # ========================================================

    martingale_step = 0

    consecutive_losses = 0

    for i in range(
        7,
        len(candles)
    ):

        window = candles[
            i - 7:i + 1
        ]

        analysis = analyze_strategy(
            window
        )

        signal = analysis[
            "signal"
        ]

        candle = candles[i]

        price = candle[
            "close"
        ]

        candle_time = candle[
            "time"
        ]

        # ====================================================
        # ОТКРЫТИЕ НОВОЙ ПОЗИЦИИ
        # ====================================================

        if current_position is None:

            if signal in (
                "LONG",
                "SHORT"
            ):

                position_size = (
                    get_martingale_position_size(
                        martingale_step
                    )
                )

                current_position = {
                    "instrument":
                        instrument,

                    "title":
                        title,

                    "direction":
                        signal,

                    "entry_price":
                        price,

                    "entry_time":
                        candle_time,

                    "martingale_step":
                        martingale_step,

                    "consecutive_losses":
                        consecutive_losses,

                    "position_size":
                        position_size
                }

            continue

        # ====================================================
        # ТОТ ЖЕ СИГНАЛ — ОСТАВЛЯЕМ ПОЗИЦИЮ
        # ====================================================

        if signal == current_position[
            "direction"
        ]:

            continue

        # ====================================================
        # ПРОТИВОПОЛОЖНЫЙ СИГНАЛ — ЗАКРЫВАЕМ
        # ====================================================

        opposite_signal = (

            current_position[
                "direction"
            ] == "LONG"

            and

            signal == "SHORT"

        ) or (

            current_position[
                "direction"
            ] == "SHORT"

            and

            signal == "LONG"
        )

        if not opposite_signal:

            continue

        # ====================================================
        # РАСЧЕТ РЕЗУЛЬТАТА
        # ====================================================

        result = calculate_trade_result(

            current_position[
                "direction"
            ],

            current_position[
                "entry_price"
            ],

            price,

            current_position[
                "position_size"
            ]
        )

        if result is None:

            current_position = None

            continue

        # ====================================================
        # СОЗДАЕМ СДЕЛКУ
        # ====================================================

        trade = {

            "id":
                len(trades) + 1,

            "instrument":
                instrument,

            "title":
                title,

            "direction":
                current_position[
                    "direction"
                ],

            "entry_time":
                current_position[
                    "entry_time"
                ],

            "exit_time":
                candle_time,

            "entry_price":
                round(
                    current_position[
                        "entry_price"
                    ],
                    8
                ),

            "exit_price":
                round(
                    price,
                    8
                ),

            "exit_signal":
                signal,

            "martingale_enabled":
                MARTINGALE_ENABLED,

            "martingale_step":
                current_position[
                    "martingale_step"
                ],

            "consecutive_losses_before":
                current_position[
                    "consecutive_losses"
                ],

            **result
        }

        trades.append(
            trade
        )

        # ====================================================
        # ОБНОВЛЯЕМ МАРТИНГЕЙЛ
        # ====================================================

        if result["net_result"] > 0:

            # Прибыльная сделка —
            # возвращаемся к базовой позиции

            consecutive_losses = 0

            martingale_step = 0

        else:

            # Убыточная сделка

            consecutive_losses += 1

            if MARTINGALE_ENABLED:

                martingale_step = min(
                    consecutive_losses,
                    MARTINGALE_MAX_STEP
                )

            else:

                martingale_step = 0

        # ====================================================
        # ОТКРЫВАЕМ НОВУЮ ПОЗИЦИЮ
        # ПО ПРОТИВОПОЛОЖНОМУ СИГНАЛУ
        # ====================================================

        next_position_size = (
            get_martingale_position_size(
                martingale_step
            )
        )

        current_position = {

            "instrument":
                instrument,

            "title":
                title,

            "direction":
                signal,

            "entry_price":
                price,

            "entry_time":
                candle_time,

            "martingale_step":
                martingale_step,

            "consecutive_losses":
                consecutive_losses,

            "position_size":
                next_position_size
        }

    return trades, current_position


# ============================================================
# СТАТИСТИКА
# ============================================================

def calculate_statistics(
    trades
):

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
            "net": 0
        }

    profitable = sum(

        1

        for trade in trades

        if trade.get(
            "net_result",
            0
        ) > 0
    )

    losing = sum(

        1

        for trade in trades

        if trade.get(
            "net_result",
            0
        ) < 0
    )

    gross = sum(

        trade.get(
            "gross_result",
            0
        )

        for trade in trades
    )

    commission = sum(

        trade.get(
            "buy_commission",
            0
        )

        +

        trade.get(
            "sell_commission",
            0
        )

        for trade in trades
    )

    tax = sum(

        trade.get(
            "tax",
            0
        )

        for trade in trades
    )

    net = sum(

        trade.get(
            "net_result",
            0
        )

        for trade in trades
    )

    winrate = (

        profitable
        /
        total
        *
        100
    )

    return {

        "total":
            total,

        "profitable":
            profitable,

        "losing":
            losing,

        "winrate":
            round(
                winrate,
                2
            ),

        "gross":
            round(
                gross,
                2
            ),

        "commission":
            round(
                commission,
                2
            ),

        "tax":
            round(
                tax,
                2
            ),

        "net":
            round(
                net,
                2
            )
    }


# ============================================================
# АНАЛИЗ ОДНОГО ФЬЮЧЕРСА
# ============================================================

def get_future_status(
    prefix,
    title,
    emoji
):

    result = {

        "prefix":
            prefix,

        "title":
            title,

        "emoji":
            emoji,

        "status":
            "Ошибка",

        "message":
            "",

        "ticker":
            "—",

        "uid":
            "—",

        "candles":
            0,

        "strategy": {

            "signal":
                "Нет сигналов",

            "direction":
                "—",

            "description":
                ""
        },

        "history":
            [],

        "statistics":
            {},

        "open_position":
            None
    }

    try:

        # ====================================================
        # ФЬЮЧЕРС
        # ====================================================

        future = find_active_future(
            prefix
        )

        if not future:

            result["message"] = (
                "Актуальный контракт не найден"
            )

            return result

        result["ticker"] = future[
            "ticker"
        ]

        result["uid"] = future[
            "instrument_uid"
        ]

        # ====================================================
        # СВЕЧИ
        # ====================================================

        candles_raw = get_candles(

            future[
                "instrument_uid"
            ]
        )

        candles = normalize_candles(
            candles_raw
        )

        result["candles"] = len(
            candles
        )

        if not candles:

            result["message"] = (
                "Свечей 0"
            )

            return result

        # ====================================================
        # ТЕКУЩИЙ СИГНАЛ
        # ====================================================

        strategy = analyze_strategy(
            candles
        )

        result["strategy"] = strategy

        result["status"] = "OK"

        result["message"] = (
            "Данные получены"
        )

        # ====================================================
        # ИСТОРИЯ
        # ====================================================

        trades, open_position = (
            build_strategy_history(

                candles,

                prefix,

                title
            )
        )

        result["history"] = trades

        result["open_position"] = (
            open_position
        )

        result["statistics"] = (
            calculate_statistics(
                trades
            )
        )

        # ====================================================
        # ИНФОРМАЦИЯ О МАРТИНГЕЙЛЕ
        # ДЛЯ ТЕКУЩЕЙ ОТКРЫТОЙ ПОЗИЦИИ
        # ====================================================

        if open_position:

            result["strategy"][
                "position_size"
            ] = round(
                open_position[
                    "position_size"
                ],
                2
            )

            result["strategy"][
                "martingale_step"
            ] = (
                open_position[
                    "martingale_step"
                ]
            )

            result["strategy"][
                "consecutive_losses"
            ] = (
                open_position[
                    "consecutive_losses"
                ]
            )

        else:

            result["strategy"][
                "position_size"
            ] = (
                POSITION_SIZE_RUBLES
            )

            result["strategy"][
                "martingale_step"
            ] = 0

            result["strategy"][
                "consecutive_losses"
            ] = 0

        return result

    except Exception as e:

        log.exception(
            "Ошибка %s",
            title
        )

        result["message"] = str(e)

        return result


# ============================================================
# ОБЩИЙ СБОР ДАННЫХ
# ============================================================

def collect_data():

    futures = [

        get_future_status(
            "CR",
            "Юань",
            "¥"
        ),

        get_future_status(
            "GD",
            "Золото",
            "🥇"
        ),

        get_future_status(
            "BR",
            "Нефть Brent",
            "🛢️"
        )
    ]

    last_signal = {

        "title":
            "Нет сигналов",

        "signal":
            "—",

        "direction":
            "—",

        "description":
            ""
    }

    for item in futures:

        signal = item[
            "strategy"
        ].get(
            "signal"
        )

        if signal in (
            "LONG",
            "SHORT"
        ):

            last_signal = {

                "title":
                    item["title"],

                "signal":
                    signal,

                "direction":
                    item["strategy"].get(
                        "direction",
                        "—"
                    ),

                "description":
                    item["strategy"].get(
                        "description",
                        ""
                    )
            }

            break

    # ========================================================
    # ОБЩАЯ ИСТОРИЯ
    # ========================================================

    all_trades = []

    for item in futures:

        all_trades.extend(

            item.get(
                "history",
                []
            )
        )

    total_statistics = (
        calculate_statistics(
            all_trades
        )
    )

    # ========================================================
    # СОХРАНЕНИЕ ИСТОРИИ
    # ========================================================

    existing_history = load_history()

    existing_keys = set()

    for trade in existing_history:

        key = (

            trade.get(
                "instrument"
            ),

            trade.get(
                "entry_time"
            ),

            trade.get(
                "exit_time"
            ),

            trade.get(
                "direction"
            )
        )

        existing_keys.add(
            key
        )

    for trade in all_trades:

        key = (

            trade.get(
                "instrument"
            ),

            trade.get(
                "entry_time"
            ),

            trade.get(
                "exit_time"
            ),

            trade.get(
                "direction"
            )
        )

        if key not in existing_keys:

            existing_history.append(
                trade
            )

            existing_keys.add(
                key
            )

    existing_history = (
        existing_history[-5000:]
    )

    save_history(
        existing_history
    )

    return {

        "updated":
            datetime.now(
                timezone.utc
            ).isoformat(),

        "futures":
            futures,

        "last_signal":
            last_signal,

        "statistics":
            total_statistics,

        "settings": {

            "position_size":
                POSITION_SIZE_RUBLES,

            "buy_commission":
                BUY_COMMISSION_PERCENT,

            "sell_commission":
                SELL_COMMISSION_PERCENT,

            "tax":
                TAX_PERCENT,

            "exit_rule":
                "Противоположный сигнал",

            "martingale_enabled":
                MARTINGALE_ENABLED,

            "martingale_multiplier":
                MARTINGALE_MULTIPLIER,

            "martingale_max_step":
                MARTINGALE_MAX_STEP
        }
    }


# ============================================================
# API STATUS
# ============================================================

@app.route(
    "/api/status"
)
def api_status():

    try:

        return jsonify(
            collect_data()
        )

    except Exception as e:

        log.exception(
            "Ошибка /api/status"
        )

        return jsonify(
            {
                "error":
                    str(e)
            }
        ), 500


# ============================================================
# API HISTORY
# ============================================================

@app.route(
    "/api/history"
)
def api_history():

    history = load_history()

    return jsonify(
        {
            "count":
                len(history),

            "history":
                history
        }
    )


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

    background:
        linear-gradient(
            135deg,
            #07090d,
            #10141c
        );

    color: #ffffff;

    font-family:
        Arial,
        sans-serif;

    min-height: 100vh;
}

.container {

    width: 95%;

    max-width: 1400px;

    margin: 0 auto;

    padding:
        25px
        0
        50px;
}

.header {

    display: flex;

    justify-content:
        space-between;

    align-items: center;

    margin-bottom:
        25px;
}

.logo {

    font-size:
        28px;

    font-weight:
        800;

    letter-spacing:
        1px;
}

.logo span {

    color:
        #d7aa52;
}

.updated {

    color:
        #8c96a8;

    font-size:
        13px;
}

.grid {

    display:
        grid;

    grid-template-columns:
        repeat(
            3,
            1fr
        );

    gap:
        18px;
}

.card {

    background:
        rgba(
            22,
            27,
            36,
            0.95
        );

    border:
        1px solid
        rgba(
            255,
            255,
            255,
            0.08
        );

    border-radius:
        18px;

    padding:
        20px;

    box-shadow:
        0 15px 50px
        rgba(
            0,
            0,
            0,
            0.25
        );
}

.card h2 {

    margin-top:
        0;

    font-size:
        20px;
}

.status {

    display:
        inline-block;

    padding:
        6px 10px;

    border-radius:
        20px;

    font-size:
        12px;

    background:
        #193d2b;

    color:
        #66e29a;
}

.error {

    background:
        #442020;

    color:
        #ff8585;
}

.info {

    margin-top:
        15px;

    color:
        #aab3c2;

    font-size:
        13px;

    line-height:
        1.6;
}

.signal {

    margin-top:
        15px;

    padding:
        14px;

    border-radius:
        14px;

    background:
        #111720;

    font-size:
        18px;

    font-weight:
        bold;
}

.long {

    color:
        #52e58a;
}

.short {

    color:
        #ff6666;
}

.none {

    color:
        #9ca5b4;
}

.statistics {

    margin-top:
        25px;
}

.stats-grid {

    display:
        grid;

    grid-template-columns:
        repeat(
            4,
            1fr
        );

    gap:
        12px;
}

.stat {

    background:
        #111720;

    padding:
        16px;

    border-radius:
        14px;
}

.stat-title {

    font-size:
        12px;

    color:
        #8f99aa;

    margin-bottom:
        7px;
}

.stat-value {

    font-size:
        20px;

    font-weight:
        bold;
}

.history {

    margin-top:
        25px;
}

.table-wrap {

    overflow-x:
        auto;
}

table {

    width:
        100%;

    border-collapse:
        collapse;

    min-width:
        1100px;
}

th,
td {

    padding:
        11px;

    border-bottom:
        1px solid
        rgba(
            255,
            255,
            255,
            0.07
        );

    text-align:
        left;

    font-size:
        13px;
}

th {

    color:
        #9ca5b4;

    font-weight:
        normal;
}

.positive {

    color:
        #52e58a;

    font-weight:
        bold;
}

.negative {

    color:
        #ff6666;

    font-weight:
        bold;
}

.settings {

    margin-top:
        20px;

    color:
        #858fa0;

    font-size:
        13px;

    line-height:
        1.7;
}

.martingale-box {

    margin-top:
        12px;

    padding:
        12px;

    border-radius:
        12px;

    background:
        #151b25;

    border:
        1px solid
        rgba(
            215,
            170,
            82,
            0.18
        );
}

.martingale-title {

    color:
        #d7aa52;

    font-weight:
        bold;

    margin-bottom:
        5px;
}

.position-size {

    color:
        #ffffff;

    font-weight:
        bold;
}

button {

    margin-top:
        20px;

    border:
        none;

    border-radius:
        12px;

    padding:
        12px 20px;

    background:
        #d7aa52;

    color:
        #111;

    font-weight:
        bold;

    cursor:
        pointer;
}

@media (max-width:900px) {

    .grid {

        grid-template-columns:
            1fr;
    }

    .stats-grid {

        grid-template-columns:
            repeat(
                2,
                1fr
            );
    }
}

</style>

</head>

<body>

<div class="container">

<div class="header">

<div class="logo">
MARKUS <span>TRADE</span>
</div>

<div
class="updated"
id="updated"
>
Загрузка...
</div>

</div>


<div
class="grid"
id="futures"
></div>


<div class="card statistics">

<h2>
📊 Статистика стратегии
</h2>

<div
class="stats-grid"
id="statistics"
></div>

</div>


<div class="card history">

<h2>
📜 История сделок
</h2>

<div
class="table-wrap"
id="history"
></div>

</div>


<div class="card settings">

<b>
Настройки расчёта
</b>

<br>

Размер базовой виртуальной позиции:

<span id="positionSize">
—
</span>
₽

<br>

Комиссия покупки:

<span id="buyCommission">
—
</span>%

<br>

Комиссия продажи:

<span id="sellCommission">
