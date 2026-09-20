import os
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

# Эндпоинт для поиска конкретного инструмента (например, по тикеру "CR")
FIND_INSTRUMENT_URL = (
    API_BASE
    + "/tinkoff.public.invest.api.contract.v1."
    "InstrumentsService/FindInstrument"
)

# Эндпоинт для получения списка всех фьючерсов
FUTURES_URL = (
    API_BASE
    + "/tinkoff.public.invest.api.contract.v1."
    "InstrumentsService/Futures"
)

# Эндпоинт для запроса исторических свечей
CANDLES_URL = (
    API_BASE
    + "/tinkoff.public.invest.api.contract.v1."
    "MarketDataService/GetCandles"
)

REQUEST_TIMEOUT = 30

# 15-минутные свечи
CANDLE_INTERVAL = "CANDLE_INTERVAL_15_MIN"

# История для анализа (в часах за неделю)
HISTORY_HOURS = 24 * 7



# ============================================================
# SSL
# ============================================================

# У тебя была ошибка:
#
# SSLCertVerificationError
# self-signed certificate in certificate chain
#
# Поэтому временно отключаем проверку сертификата.
# После того как API заработает, сделаем нормальную
# проверку сертификата.
#
urllib3.disable_warnings(
    urllib3.exceptions.InsecureRequestWarning
)

warnings.filterwarnings(
    "ignore",
    message="Unverified HTTPS request"
)


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
# ПОЛУЧЕНИЕ ТОКЕНА
# ============================================================

def get_token():

    possible_names = [
        "TINKOFF_TOKEN",
        "TINVEST_TOKEN",
        "T_BANK_TOKEN",
        "API_TOKEN",
        "TOKEN",
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
# ОБЩИЙ POST ЗАПРОС
# ============================================================

def api_post(url, payload):

    token = get_token()

    if not token:

        raise RuntimeError(
            "API-токен не найден. "
            "Проверь переменную с токеном."
        )

    headers = {
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
    }

    log.info(
        "Запрос: %s",
        url
    )

    response = requests.post(
        url,
        headers=headers,
        json=payload,
        timeout=REQUEST_TIMEOUT,
        verify=False
    )

    log.info(
        "Ответ HTTP: %s",
        response.status_code
    )

    if response.status_code != 200:

        raise RuntimeError(
            "HTTP "
            + str(response.status_code)
            + ": "
            + response.text[:2000]
        )

    try:

        return response.json()

    except Exception:

        raise RuntimeError(
            "API вернул не JSON:\n"
            + response.text[:2000]
        )


# ============================================================
# ПОЛУЧЕНИЕ ВСЕХ ФЬЮЧЕРСОВ
# ============================================================

def get_all_futures():

    log.info(
        "Получаю список фьючерсов..."
    )

    payload = {
        "instrumentStatus":
            "INSTRUMENT_STATUS_BASE"
    }

    try:

        data = api_post(
            FUTURES_URL,
            payload
        )

    except Exception as e:

        # Некоторые конфигурации API могут не принять
        # фильтр INSTRUMENT_STATUS_BASE.
        # Тогда пробуем ALL.

        log.warning(
            "Первый запрос Futures не прошёл: %s",
            e
        )

        payload = {
            "instrumentStatus":
                "INSTRUMENT_STATUS_ALL"
        }

        data = api_post(
            FUTURES_URL,
            payload
        )

    futures = data.get(
        "futures",
        []
    )

    if not isinstance(
        futures,
        list
    ):

        futures = []

    log.info(
        "Получено фьючерсов: %s",
        len(futures)
    )

    return futures


# ============================================================
# БЕЗОПАСНОЕ ПОЛУЧЕНИЕ СТРОКИ
# ============================================================

def get_string(obj, key):

    value = obj.get(key)

    if value is None:
        return ""

    return str(value).strip()


# ============================================================
# ДАТА
# ============================================================

def parse_date(value):

    if not value:
        return None

    try:

        return datetime.fromisoformat(
            str(value).replace(
                "Z",
                "+00:00"
            )
        )

    except Exception:

        return None


# ============================================================
# ОПРЕДЕЛЕНИЕ ФЬЮЧЕРСА
# ============================================================

def matches_future(future, prefix):

    prefix = prefix.upper()

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

    # --------------------------------------------------------
    # Сначала ищем непосредственно по тикеру
    # --------------------------------------------------------

    if ticker.startswith(prefix):

        return True

    # --------------------------------------------------------
    # ЮАНЬ
    # --------------------------------------------------------

    if prefix == "CR":

        words = [
            "CNY",
            "YUAN",
            "CNH",
            "ЮАН",
            "КИТАЙ"
        ]

        for word in words:

            if word in ticker:
                return True

            if word in name:
                return True

            if word in basic_asset:
                return True

    # --------------------------------------------------------
    # ЗОЛОТО
    # --------------------------------------------------------

    if prefix == "GD":

        words = [
            "GOLD",
            "ЗОЛОТ"
        ]

        for word in words:

            if word in ticker:
                return True

            if word in name:
                return True

            if word in basic_asset:
                return True

    # --------------------------------------------------------
    # BRENT
    # --------------------------------------------------------

    if prefix == "BR":

        words = [
            "BRENT",
            "БРЕНТ"
        ]

        for word in words:

            if word in ticker:
                return True

            if word in name:
                return True

            if word in basic_asset:
                return True

    return False


# ============================================================
# ПОИСК АКТИВНОГО КОНТРАКТА
# ============================================================

def find_active_future(prefix):

    log.info(
        "======================================"
    )

    log.info(
        "Ищу фьючерс через FindInstrument: %s",
        prefix
    )

    # --------------------------------------------------------
    # Для каждого базового инструмента пробуем несколько
    # вариантов поиска.
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # ПОИСК
    # --------------------------------------------------------

    for query in queries:

        payload = {
            "query": query,
            "instrumentKind":
                "INSTRUMENT_TYPE_FUTURES",
            "apiTradeAvailableFlag": True
        }

        log.info(
            "FindInstrument query=%s",
            query
        )

        try:

            data = api_post(
                FIND_INSTRUMENT_URL,
                payload
            )

        except Exception as e:

            log.warning(
                "Ошибка поиска %s: %s",
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

            instruments = []

        log.info(
            "FindInstrument %s -> найдено: %s",
            query,
            len(instruments)
        )

        # ----------------------------------------------------
        # РАЗБИРАЕМ НАЙДЕННЫЕ ИНСТРУМЕНТЫ
        # ----------------------------------------------------

        for item in instruments:

            ticker = str(
                item.get(
                    "ticker",
                    ""
                )
            ).strip()

            name = str(
                item.get(
                    "name",
                    ""
                )
            ).strip()

            uid = str(
                item.get(
                    "uid",
                    ""
                )
            ).strip()

            instrument_uid = str(
                item.get(
                    "instrumentUid",
                    ""
                )
            ).strip()

            if not instrument_uid:

                instrument_uid = uid

            if not instrument_uid:

                continue

            # ------------------------------------------------
            # ПРОВЕРЯЕМ СООТВЕТСТВИЕ
            # ------------------------------------------------

            text = (
                ticker
                + " "
                + name
            ).upper()

            matched = False

            if prefix == "CR":

                if (
                    ticker.upper().startswith("CR")
                    or
                    "CNY" in text
                    or
                    "ЮАН" in text
                    or
                    "КИТАЙ" in text
                ):

                    matched = True

            elif prefix == "GD":

                if (
                    ticker.upper().startswith("GD")
                    or
                    "GOLD" in text
                    or
                    "ЗОЛОТ" in text
                ):

                    matched = True

            elif prefix == "BR":

                if (
                    ticker.upper().startswith("BR")
                    or
                    "BRENT" in text
                    or
                    "БРЕНТ" in text
                ):

                    matched = True

            if not matched:

                continue

            # ------------------------------------------------
            # ДАТЫ
            # ------------------------------------------------

            first_trade = parse_date(
                item.get(
                    "firstTradeDate"
                )
            )

            last_trade = parse_date(
                item.get(
                    "lastTradeDate"
                )
            )

            now = datetime.now(
                timezone.utc
            )

            # Контракт ещё не начал торговаться
            if first_trade:

                if first_trade > now:

                    continue

            # Контракт уже закончился
            if last_trade:

                if last_trade < now:

                    continue

            candidates.append(
                {
                    "ticker":
                        ticker,

                    "name":
                        name,

                    "uid":
                        uid,

                    "instrument_uid":
                        instrument_uid,

                    "first_trade":
                        first_trade,

                    "last_trade":
                        last_trade,

                    "class_code":
                        str(
                            item.get(
                                "classCode",
                                ""
                            )
                        ),

                    "basic_asset":
                        str(
                            item.get(
                                "basicAsset",
                                ""
                            )
                        )
                }
            )

    # --------------------------------------------------------
    # УДАЛЯЕМ ДУБЛИКАТЫ
    # --------------------------------------------------------

    unique = {}

    for item in candidates:

        unique[
            item["instrument_uid"]
        ] = item

    candidates = list(
        unique.values()
    )

    # --------------------------------------------------------
    # НИЧЕГО НЕ НАШЛИ
    # --------------------------------------------------------

    if not candidates:

        log.error(
            "======================================"
        )

        log.error(
            "ФЬЮЧЕРС %s НЕ НАЙДЕН",
            prefix
        )

        log.error(
            "======================================"
        )

        return None

    # --------------------------------------------------------
    # СОРТИРУЕМ ПО БЛИЖАЙШЕЙ ЭКСПИРАЦИИ
    # --------------------------------------------------------

    def expiry_key(item):

        date = item.get(
            "last_trade"
        )

        if date:

            return date

        return datetime.max.replace(
            tzinfo=timezone.utc
        )

    candidates.sort(
        key=expiry_key
    )

    # --------------------------------------------------------
    # ВЫБИРАЕМ БЛИЖАЙШИЙ АКТУАЛЬНЫЙ
    # --------------------------------------------------------

    selected = candidates[0]

    log.info(
        "======================================"
    )

    log.info(
        "АКТУАЛЬНЫЙ ФЬЮЧЕРС НАЙДЕН"
    )

    log.info(
        "Тикер: %s",
        selected["ticker"]
    )

    log.info(
        "Название: %s",
        selected["name"]
    )

    log.info(
        "UID: %s",
        selected["instrument_uid"]
    )

    log.info(
        "ClassCode: %s",
        selected["class_code"]
    )

    log.info(
        "Экспирация: %s",
        selected["last_trade"]
    )

    log.info(
        "======================================"
    )

    # --------------------------------------------------------
    # ПОКАЗЫВАЕМ ВСЕ НАЙДЕННЫЕ КОНТРАКТЫ В ЛОГЕ
    # --------------------------------------------------------

    log.info(
        "Всего подходящих контрактов: %s",
        len(candidates)
    )

    for item in candidates:

        log.info(
            "Кандидат: %s | UID=%s | expiry=%s",
            item["ticker"],
            item["instrument_uid"],
            item["last_trade"]
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
        now
        - timedelta(
            hours=HISTORY_HOURS
        )
    )

    payload = {
        "from": start.isoformat(),
        "to": now.isoformat(),
        "interval":
            CANDLE_INTERVAL,
        "instrumentId":
            instrument_uid,
        "candleSourceType":
            "CANDLE_SOURCE_EXCHANGE"
    }

    log.info(
        "Запрашиваю свечи для UID: %s",
        instrument_uid
    )

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

        candles = []

    log.info(
        "Получено свечей: %s",
        len(candles)
    )

    return candles


# ============================================================
# QUOTATION → ЧИСЛО
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
                float(nano)
                / 1_000_000_000
            )

        except Exception:

            return 0.0

    try:

        return float(value)

    except Exception:

        return 0.0


# ============================================================
# НОРМАЛИЗАЦИЯ СВЕЧЕЙ
# ============================================================

def normalize_candles(candles):

    result = []

    for candle in candles:

        result.append(
            {
                "time":
                    candle.get("time"),

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
                    int(
                        candle.get(
                            "volume",
                            0
                        ) or 0
                    )
            }
        )

    result.sort(
        key=lambda x:
            x.get("time") or ""
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
            "description": "Недостаточно свечей"
        }

    last = candles[-8:]

    highs = [x["high"] for x in last]
    lows = [x["low"] for x in last]
    closes = [x["close"] for x in last]

    # ========================================================
    # SHORT
    # ========================================================
    rising_highs = (
        highs[3] > highs[2]
        and highs[4] > highs[3]
        and highs[5] > highs[4]
    )

    falling = (
        closes[-1] < closes[-2]
        and closes[-2] < closes[-3]
    )

    if rising_highs and falling:
        return {
            "signal": "SHORT",
            "direction": "ВНИЗ",
            "description": "Обнаружена последовательность повышающихся максимумов с последующим снижением."
        }

    # ========================================================
    # LONG
    # ========================================================
    falling_lows = (
        lows[3] < lows[2]
        and lows[4] < lows[3]
        and lows[5] < lows[4]
    )

    rising = (
        closes[-1] > closes[-2]
        and closes[-2] > closes[-3]
    )

    if falling_lows and rising:
        return {
            "signal": "LONG",
            "direction": "ВВЕРХ",
            "description": "Обнаружена последовательность понижающихся минимумов с последующим ростом."
        }

    return {
        "signal": "Нет сигналов",
        "direction": "—",
        "description": "Условия стратегии пока не выполнены."
    }


# ============================================================
# СТАТУС ОДНОГО ФЬЮЧЕРСА
# ============================================================

def get_future_status(prefix, title, emoji):

    result = {
        "prefix": prefix,
        "title": title,
        "emoji": emoji,
        "status": "Ошибка",
        "message": "",
        "ticker": "",
        "uid": "",
        "candles": 0,
        "strategy": {
            "signal": "Нет сигналов",
            "direction": "—",
            "description": ""
        }
    }

    try:
        future = find_active_future(prefix)

        if not future:
            result["status"] = "Не найден"
            result["message"] = "Актуальный контракт не найден."
            return result

        result["ticker"] = future["ticker"]
        result["uid"] = future["instrument_uid"]

        candles_raw = get_candles(future["instrument_uid"])
        candles = normalize_candles(candles_raw)
        result["candles"] = len(candles)

        if not candles:
            result["status"] = "Нет свечей"
            result["message"] = "Фьючерс найден, но свечи не получены."
            return result

        result["status"] = "OK"
        result["message"] = "Данные получены"
        result["strategy"] = analyze_strategy(candles)
        return result

    except Exception as e:
        log.exception("Ошибка обработки %s", prefix)
        result["status"] = "Ошибка"
        result["message"] = str(e)
        return result


# ============================================================
# ВСЕ ИНСТРУМЕНТЫ
# ============================================================

def collect_data():

    results = []

    # ЮАНЬ
    results.append(get_future_status("CR", "ФЬЮЧЕРС ЮАНЬ (CNY)", "🇨🇳"))

    # ЗОЛОТО
    results.append(get_future_status("GD", "ФЬЮЧЕРС ЗОЛОТО (GOLD)", "🏆"))

    # НЕФТЬ
    results.append(get_future_status("BR", "ФЬЮЧЕРС НЕФТЬ (BRENT)", "🛢️"))

    # --------------------------------------------------------
    # ИЩЕМ ПОСЛЕДНИЙ СИГНАЛ (ИСПРАВЛЕНО)
    # --------------------------------------------------------
    last_signal = {
        "signal": "Нет сигналов",
        "direction": "—",
        "description": "Условия стратегии пока не выполнены во всех инструментах."
    }

    for item in results:
        strategy = item.get("strategy", {})
        signal = strategy.get("signal")

        if signal in ("LONG", "SHORT"):
            last_signal = strategy
            break

    return {
        "updated": datetime.now(timezone.utc).isoformat(),
        "futures": results,
        "last_signal": last_signal
    }


# ============================================================
# API STATUS
# ============================================================

@app.route("/api/status")
def status():

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
# HTML
# ============================================================

HTML = """

<!DOCTYPE html>

<html lang="ru">

<head>

<meta charset="UTF-8">

<meta
    name="viewport"
    content="width=device-width,
    initial-scale=1.0"
>

<title>Markus Trade</title>

<style>

* {
    box-sizing: border-box;
}

body {

    margin: 0;

    background: #0b0b0b;

    color: #ffffff;

    font-family:
        -apple-system,
        BlinkMacSystemFont,
        "Segoe UI",
        Arial,
        sans-serif;
}

.header {

    padding: 28px 20px 20px;

    text-align: center;

    border-bottom:
        1px solid #252525;
}

.header h1 {

    margin: 0;

    font-size: 28px;
}

.header p {

    margin: 6px 0 0;

    color: #888;

}

.container {

    max-width: 900px;

    margin: auto;

    padding: 20px;
}

.card {

    background: #1b1b1b;

    border:
        1px solid #303030;

    border-radius: 24px;

    padding: 24px;

    margin-bottom: 18px;
}

.title {

    color: #999;

    font-size: 17px;

    font-weight: 700;

    letter-spacing: 2px;

    margin-bottom: 20px;
}

.status {

    font-size: 38px;

    font-weight: 800;

    margin-bottom: 10px;
}

.ok {

    color: #00ff9d;
}

.error {

    color: #ff4545;
}

.warning {

    color: #ffc400;
}

.info {

    color: #00ff9d;
}

.row {

    margin-top: 12px;

    color: #999;

    font-size: 16px;

    word-break: break-word;
}

.row span {

    color: #ffffff;
}

.signal {

    border:
        2px solid #00ff9d;

    border-radius: 24px;

    padding: 24px;

    background: #171717;
}

.signal-title {

    color: #999;

    font-weight: 700;

    letter-spacing: 2px;

    margin-bottom: 18px;
}

.signal-value {

    font-size: 23px;

    margin: 12px 0;
}

button {

    width: 100%;

    margin-top: 18px;

    padding: 16px;

    border: none;

    border-radius: 14px;

    background: #00ff9d;

    color: #000;

    font-size: 17px;

    font-weight: 700;
}

.small {

    color: #666;

    text-align: center;

    margin-top: 15px;

    font-size: 12px;
}

</style>

</head>

<body>


<div class="header">

    <h1>Markus Trade</h1>

    <p>мини-приложение</p>

</div>


<div
    class="container"
    id="app"
>

    <div class="card">

        <div class="status info">
            Загрузка...
        </div>

        <div class="row">
            Подключение к T-Bank API
        </div>

    </div>

</div>


<script>

function esc(value) {

    return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");

}


async function loadData() {

    const app =
        document.getElementById(
            "app"
        );

    try {

        const response =
            await fetch(
                "/api/status"
            );

        const data =
            await response.json();

        if (!response.ok) {

            throw new Error(
                data.error ||
                "Ошибка сервера"
            );

        }

        render(data);

    }

    catch (error) {

        app.innerHTML = `

            <div class="card">

                <div class="status error">
                    Ошибка
                </div>

                <div class="row">

                    ${esc(
                        error.message
                    )}

                </div>

            </div>

        `;

    }

}


function render(data) {

    let html = "";

    const futures =
        data.futures || [];


    for (
        const item of futures
    ) {

        let cls = "warning";

        if (
            item.status === "OK"
        ) {

            cls = "ok";

        }

        if (
            item.status === "Ошибка"
        ) {

            cls = "error";

        }


        html += `

            <div class="card">

                <div class="title">

                    ${esc(
                        item.emoji
                    )}

                    ${esc(
                        item.title
                    )}

                </div>


                <div
                    class="status ${cls}"
                >

                    ${esc(
                        item.status
                    )}

                </div>


                <div class="row">

                    ${esc(
                        item.message
                    )}

                </div>


                <div class="row">

                    Тикер:

                    <span>
                        ${esc(
                            item.ticker ||
                            "—"
                        )}
                    </span>

                </div>


                <div class="row">

                    UID:

                    <span>
                        ${esc(
                            item.uid ||
                            "—"
                        )}
                    </span>

                </div>


                <div class="row">

                    Свечей:

                    <span>
                        ${esc(
                            item.candles ||
                            0
                        )}
                    </span>

                </div>

            </div>

        `;

    }


    const signal =
        data.last_signal || {};


    html += `

        <div class="signal">

            <div class="signal-title">

                ПОСЛЕДНИЙ СИГНАЛ
                СТРАТЕГИИ

            </div>


            <div class="signal-value">

                Статус:

                <span class="info">

                    ${
                        signal.signal ===
                        "Нет сигналов"

                        ? "Ожидание данных..."

                        : "Сигнал обнаружен"
                    }

                </span>

            </div>


            <div class="signal-value">

                Сигнал:

                <span>

                    ${esc(
                        signal.signal ||
                        "Нет сигналов"
                    )}

                </span>

            </div>


            <div class="signal-value">

                Направление:

                <span>

                    ${esc(
                        signal.direction ||
                        "—"
                    )}

                </span>

            </div>


            <div class="row">

                ${esc(
                    signal.description ||
                    ""
                )}

            </div>

        </div>


        <button
            onclick="loadData()"
        >

            🔄 Обновить данные

        </button>


        <div class="small">

            Последнее обновление:

            ${esc(
                data.updated || ""
            )}

        </div>

    `;


    document.getElementById(
        "app"
    ).innerHTML = html;

}


loadData();


setInterval(
    loadData,
    60000
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
# ФОНОВЫЙ МОНИТОР
# ============================================================

def background_monitor():

    while True:

        try:

            log.info(
                "======================================"
            )

            log.info(
                "ФОНОВАЯ ПРОВЕРКА"
            )

            data = collect_data()

            signal = data.get(
                "last_signal",
                {}
            )

            log.info(
                "Последний сигнал: %s",
                signal.get(
                    "signal"
                )
            )

        except Exception:

            log.exception(
                "Ошибка фоновой проверки"
            )

        time.sleep(
            300
        )


# ============================================================
# STARTUP
# ============================================================

def startup():

    log.info(
        "======================================"
    )

    log.info(
        "       MARKUS TRADE"
    )

    log.info(
        "       START"
    )

    log.info(
        "======================================"
    )

    token = get_token()

    if token:

        log.info(
            "API TOKEN: найден"
        )

    else:

        log.error(
            "API TOKEN: НЕ НАЙДЕН"
        )


# ============================================================
# ЗАПУСК
# ============================================================

if __name__ == "__main__":

    startup()

    thread = threading.Thread(
        target=background_monitor,
        daemon=True
    )

    thread.start()

    port = int(
        os.environ.get(
            "PORT",
            "5000"
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False
    )
