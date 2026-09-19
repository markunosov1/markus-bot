import os
import time
import logging
import requests
import threading

from datetime import datetime, timedelta, timezone
from flask import Flask, jsonify, render_template_string


# ============================================================
# EVA TRADING TERMINAL
# T-BANK / T-INVEST API
# ============================================================


# ============================================================
# НАСТРОЙКИ
# ============================================================

T_BANK_TOKEN = (
    os.getenv("T_BANK_TOKEN")
    or os.getenv("T_INVEST_TOKEN")
    or os.getenv("INVEST_TOKEN")
)

ACCOUNT_ID = (
    os.getenv("T_BANK_ACCOUNT_ID")
    or os.getenv("T_INVEST_ACCOUNT_ID")
    or os.getenv("ACCOUNT_ID")
)

# Пока обязательно FALSE
LIVE_TRADING = (
    os.getenv("LIVE_TRADING", "false").lower() == "true"
)

LOTS = int(os.getenv("LOTS", "1"))

CHECK_INTERVAL = int(
    os.getenv("CHECK_INTERVAL", "30")
)

# 5-минутные свечи
TIMEFRAME = "CANDLE_INTERVAL_5_MIN"

# Сколько свечей хотим получить
CANDLES_COUNT = 300

# Актуальный REST API T-Bank
API_URL = "https://invest-public-api.tbank.ru/rest"


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

log = logging.getLogger("EVA_TRADING")


# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)


# ============================================================
# СОСТОЯНИЕ БОТА
# ============================================================

BOT_STATUS = {

    "running": False,

    "last_update": None,

    "global_error": None,

    "CNY": {
        "ticker": None,
        "uid": None,
        "class_code": None,
        "price": None,
        "candles": 0,
        "error": None
    },

    "GOLD": {
        "ticker": None,
        "uid": None,
        "class_code": None,
        "price": None,
        "candles": 0,
        "error": None
    },

    "BRENT": {
        "ticker": None,
        "uid": None,
        "class_code": None,
        "price": None,
        "candles": 0,
        "error": None
    },

    "strategy": {
        "status": "Ожидание данных...",
        "signal": "Нет сигналов",
        "direction": None,
        "time": None
    }
}


# ============================================================
# ПРОВЕРКА ТОКЕНА
# ============================================================

def get_headers():

    if not T_BANK_TOKEN:

        raise RuntimeError(
            "T_BANK_TOKEN не найден. "
            "Проверь Environment Variables в Render."
        )

    return {
        "Authorization": f"Bearer {T_BANK_TOKEN}",
        "Content-Type": "application/json"
    }


# ============================================================
# QUOTATION -> FLOAT
# ============================================================

def quotation_to_float(value):

    if not value:
        return 0.0

    units = int(
        value.get("units", 0)
    )

    nano = int(
        value.get("nano", 0)
    )

    return units + nano / 1_000_000_000


# ============================================================
# ЗАПРОС К T-BANK API
# ============================================================

def api_post(path, payload):

    url = API_URL + path

    log.info(
        "API REQUEST: %s",
        path
    )

    try:

        response = requests.post(
            url,
            json=payload,
            headers=get_headers(),
            timeout=20
        )

    except requests.exceptions.RequestException as e:

        raise RuntimeError(
            f"Ошибка соединения с T-Bank: {e}"
        )

    if response.status_code != 200:

        try:
            error_data = response.json()

            error_text = str(
                error_data
            )

        except Exception:

            error_text = response.text

        raise RuntimeError(
            f"T-Bank API HTTP {response.status_code}: "
            f"{error_text[:1500]}"
        )

    try:

        return response.json()

    except Exception:

        raise RuntimeError(
            "T-Bank вернул ответ, который "
            "не удалось прочитать как JSON."
        )


# ============================================================
# ПОИСК ИНСТРУМЕНТА
#
# Используем официальный FindInstrument.
# T-Bank поддерживает поиск по ticker/name/UID и т.д.
# ============================================================

def find_instrument(query):

    path = (
        "/tinkoff.public.invest.api.contract.v1."
        "InstrumentsService/FindInstrument"
    )

    payload = {

        "query": query,

        "instrumentKind":
            "INSTRUMENT_TYPE_FUTURES",

        "apiTradeAvailableFlag": True
    }

    data = api_post(
        path,
        payload
    )

    instruments = data.get(
        "instruments",
        []
    )

    return instruments


# ============================================================
# ПОЛУЧИТЬ СПИСОК ФЬЮЧЕРСОВ
# ============================================================

def get_all_futures():

    path = (
        "/tinkoff.public.invest.api.contract.v1."
        "InstrumentsService/Futures"
    )

    payload = {
        "instrumentStatus":
            "INSTRUMENT_STATUS_BASE"
    }

    data = api_post(
        path,
        payload
    )

    return data.get(
        "instruments",
        []
    )


# ============================================================
# ПОИСК АКТИВНОГО ФЬЮЧЕРСА
# ============================================================

from datetime import datetime, timezone

# Словарь для перевода "человеческих" префиксов в реальные коды тикеров Мосбиржи
FUTURES_MAPPING = {
    "CNY": "CR",      # Юань
    "BRENT": "BR",    # Нефть Brent
    "GOLD": "GD",     # Золото (также обрабатывается отдельно ниже)
    "USD": "SI",      # Доллар
    "EUR": "ED",      # Евро
}

def find_active_future(prefix):

    log.info(
        "Ищу фьючерс: %s",
        prefix
    )

    prefix_upper = prefix.upper()
    # Получаем биржевой префикс из словаря. Если его там нет — используем исходный
    search_prefix = FUTURES_MAPPING.get(prefix_upper, prefix_upper)

    # --------------------------------------------------------
    # Сначала пробуем официальный FindInstrument
    # --------------------------------------------------------

    try:

        found = find_instrument(
            prefix
        )

        if found:

            candidates = []

            now = datetime.now(
                timezone.utc
            )

            for instrument in found:

                ticker = str(
                    instrument.get(
                        "ticker",
                        ""
                    )
                ).upper()

                # Умная проверка тикера с учетом особенностей GOLD и префиксов
                if prefix_upper == "GOLD":
                    if not (ticker.startswith("GD") or ticker.startswith("GOLD")):
                        continue
                else:
                    if not ticker.startswith(search_prefix):
                        continue

                # Проверяем доступность API
                if (
                    instrument.get(
                        "apiTradeAvailableFlag"
                    )
                    is False
                ):
                    continue

                expiration = (
                    instrument.get(
                        "expirationDate"
                    )
                    or instrument.get(
                        "expiration_date"
                    )
                )

                if expiration:

                    try:

                        expiration_date = (
                            datetime.fromisoformat(
                                expiration.replace(
                                    "Z",
                                    "+00:00"
                                )
                            )
                        )

                        if expiration_date <= now:
                            continue

                    except Exception:

                        pass

                candidates.append(
                    instrument
                )

            if candidates:

                candidates.sort(
                    key=lambda x:
                    (
                        x.get(
                            "expirationDate"
                        )
                        or x.get(
                            "expiration_date"
                        )
                        or "9999-12-31"
                    )
                )

                selected = candidates[0]

                log.info(
                    "НАЙДЕН: %s | UID=%s",
                    selected.get("ticker"),
                    selected.get("uid")
                )

                return selected

    except Exception as e:

        log.warning(
            "FindInstrument не дал результат: %s",
            e
        )


    # --------------------------------------------------------
    # РЕЗЕРВНЫЙ ВАРИАНТ
    # Получаем список всех фьючерсов
    # --------------------------------------------------------

    futures = get_all_futures()

    candidates = []

    now = datetime.now(
        timezone.utc
    )

    for instrument in futures:

        ticker = str(
            instrument.get(
                "ticker",
                ""
            )
        ).upper()

        # Умная проверка тикера для резервного списка
        if prefix_upper == "GOLD":
            if not (ticker.startswith("GD") or ticker.startswith("GOLD")):
                continue
        else:
            if not ticker.startswith(search_prefix):
                continue

        if (
            instrument.get(
                "apiTradeAvailableFlag"
            )
            is False
        ):
            continue

        expiration = (
            instrument.get(
                "expirationDate"
            )
            or instrument.get(
                "expiration_date"
            )
        )

        if expiration:

            try:

                expiration_date = (
                    datetime.fromisoformat(
                        expiration.replace(
                            "Z",
                            "+00:00"
                        )
                    )
                )

                if expiration_date <= now:
                    continue

            except Exception:

                pass

        candidates.append(
            instrument
        )

    if not candidates:

        # Собираем информацию для диагностики
        examples = []

        for instrument in futures:

            ticker = instrument.get(
                "ticker"
            )

            if ticker:

                examples.append(
                    str(ticker)
                )

        raise RuntimeError(
            f"Фьючерс {prefix} не найден. "
            f"T-Bank вернул {len(futures)} "
            f"фьючерсов. "
            f"Примеры: "
            f"{', '.join(examples[:40])}"
        )

    candidates.sort(
        key=lambda x:
        (
            x.get(
                "expirationDate"
            )
            or x.get(
                "expiration_date"
            )
            or "9999-12-31"
        )
    )

    selected = candidates[0]

    log.info(
        "НАЙДЕН РЕЗЕРВНЫМ СПОСОБОМ: %s | UID=%s",
        selected.get("ticker"),
        selected.get("uid")
    )

    return selected


# ============================================================
# ПОЛУЧЕНИЕ ПОСЛЕДНЕЙ ЦЕНЫ
# ============================================================

def get_last_price(instrument):

    uid = instrument.get(
        "uid"
    )

    figi = instrument.get(
        "figi"
    )

    ticker = instrument.get(
        "ticker"
    )

    # Основной вариант — UID, запасной — FIGI.
    # Конструкция ticker_class_code удалена, так как метод GetLastPrices её не поддерживает.
    instrument_id = uid or figi

    if not instrument_id:

        raise RuntimeError(
            f"У инструмента {ticker} "
            "нет UID или FIGI. Запрос цены невозможен."
        )


    path = (
        "/tinkoff.public.invest.api.contract.v1."
        "MarketDataService/GetLastPrices"
    )

    payload = {

        "instrumentId": [
            instrument_id
        ],

        "lastPriceType":
            "LAST_PRICE_EXCHANGE",

        "instrumentStatus":
            "INSTRUMENT_STATUS_BASE"
    }

    data = api_post(
        path,
        payload
    )

    prices = data.get(
        "lastPrices",
        []
    )

    if not prices:

        raise RuntimeError(
            f"T-Bank не вернул цену "
            f"для {ticker}."
        )

    price = quotation_to_float(
        prices[0].get(
            "price",
            {}
        )
    )

    return price



# ============================================================
# ПОЛУЧЕНИЕ СВЕЧЕЙ
# ============================================================

def get_candles(instrument):

    uid = instrument.get(
        "uid"
    )

    figi = instrument.get(
        "figi"
    )

    ticker = instrument.get(
        "ticker"
    )

    class_code = instrument.get(
        "classCode"
    ) or instrument.get(
        "class_code"
    )


    instrument_id = uid

    if not instrument_id:

        instrument_id = figi

    if (
        not instrument_id
        and ticker
        and class_code
    ):

        instrument_id = (
            f"{ticker}_{class_code}"
        )

    if not instrument_id:

        raise RuntimeError(
            f"Нет идентификатора "
            f"для свечей {ticker}."
        )


    now = datetime.now(
        timezone.utc
    )

    start = (
        now - timedelta(
            hours=48
        )
    )


    path = (
        "/tinkoff.public.invest.api.contract.v1."
        "MarketDataService/GetCandles"
    )

    payload = {

        "from":
            start.isoformat(),

        "to":
            now.isoformat(),

        "interval":
            TIMEFRAME,

        "instrumentId":
            instrument_id,

        "limit":
            CANDLES_COUNT,

        "candleSourceType":
            "CANDLE_SOURCE_EXCHANGE"
    }

    data = api_post(
        path,
        payload
    )

    candles = data.get(
        "candles",
        []
    )

    return candles


# ============================================================
# ОБНОВЛЕНИЕ ОДНОГО ФЬЮЧЕРСА
# ============================================================

def update_instrument(
    key,
    prefix
):

    try:

        instrument = (
            find_active_future(
                prefix
            )
        )

        if not instrument:

            raise RuntimeError(
                f"{prefix}: инструмент не найден."
            )


        ticker = instrument.get(
            "ticker"
        )

        uid = instrument.get(
            "uid"
        )

        class_code = (
            instrument.get(
                "classCode"
            )
            or instrument.get(
                "class_code"
            )
        )


        # Цена
        price = get_last_price(
            instrument
        )


        # Свечи
        candles = get_candles(
            instrument
        )


        BOT_STATUS[key][
            "ticker"
        ] = ticker

        BOT_STATUS[key][
            "uid"
        ] = uid

        BOT_STATUS[key][
            "class_code"
        ] = class_code

        BOT_STATUS[key][
            "price"
        ] = price

        BOT_STATUS[key][
            "candles"
        ] = len(candles)

        BOT_STATUS[key][
            "error"
        ] = None


        log.info(
            "%s | %s | Цена=%s | Свечей=%s",
            key,
            ticker,
            price,
            len(candles)
        )


        return candles


    except Exception as e:

        error = str(e)

        BOT_STATUS[key][
            "error"
        ] = error

        BOT_STATUS[key][
            "price"
        ] = None

        BOT_STATUS[key][
            "candles"
        ] = 0


        log.error(
            "%s ERROR: %s",
            key,
            error
        )


        return []


# ============================================================
# SWING АНАЛИЗ
# ============================================================

def analyze_swing(candles):

    if len(candles) < 10:

        return {

            "status":
                f"Получено свечей: {len(candles)}",

            "signal":
                "Недостаточно данных",

            "direction":
                None
        }


    closes = []

    for candle in candles:

        value = quotation_to_float(
            candle.get(
                "close",
                {}
            )
        )

        if value > 0:

            closes.append(
                value
            )


    if len(closes) < 10:

        return {

            "status":
                "Нет корректных цен",

            "signal":
                "Нет сигналов",

            "direction":
                None
        }


    # --------------------------------------------------------
    # Пока НЕ торгуем.
    #
    # Здесь только проверяем направление последних свечей.
    # Твою точную Swing-логику подключим после того,
    # как данные T-Bank начнут нормально приходить.
    # --------------------------------------------------------

    last = closes[-1]

    previous = closes[-2]

    if last > previous:

        return {

            "status":
                "Анализ Swing-точек",

            "signal":
                "Наблюдение за ростом",

            "direction":
                "UP"
        }


    if last < previous:

        return {

            "status":
                "Анализ Swing-точек",

            "signal":
                "Наблюдение за падением",

            "direction":
                "DOWN"
        }


    return {

        "status":
            "Анализ Swing-точек",

        "signal":
            "Движения нет",

        "direction":
            None
    }


# ============================================================
# ОБНОВЛЕНИЕ РЫНКА
# ============================================================

def update_market():

    BOT_STATUS[
        "global_error"
    ] = None


    # --------------------------------------------------------
    # ЮАНЬ
    # --------------------------------------------------------

    cny_candles = update_instrument(
        "CNY",
        "CR"
    )


    # --------------------------------------------------------
    # GOLD
    # --------------------------------------------------------

    update_instrument(
        "GOLD",
        "GD"
    )


    # --------------------------------------------------------
    # BRENT
    # --------------------------------------------------------

    update_instrument(
        "BRENT",
        "BR"
    )


    # --------------------------------------------------------
    # SWING
    # --------------------------------------------------------

    strategy = analyze_swing(
        cny_candles
    )


    BOT_STATUS[
        "strategy"
    ] = {

        "status":
            strategy["status"],

        "signal":
            strategy["signal"],

        "direction":
            strategy["direction"],

        "time":
            datetime.now(
                timezone.utc
            ).isoformat()
    }


    BOT_STATUS[
        "last_update"
    ] = datetime.now(
        timezone.utc
    ).isoformat()


# ============================================================
# ФОНОВЫЙ ПОТОК
# ============================================================

def background_worker():

    log.info(
        "================================="
    )

    log.info(
        "EVA TRADING BOT ЗАПУЩЕН"
    )

    log.info(
        "LIVE_TRADING = %s",
        LIVE_TRADING
    )

    log.info(
        "================================="
    )


    BOT_STATUS[
        "running"
    ] = True


    while True:

        try:

            update_market()

        except Exception as e:

            BOT_STATUS[
                "global_error"
            ] = str(e)

            log.exception(
                "Ошибка фонового обновления"
            )


        time.sleep(
            CHECK_INTERVAL
        )


# ============================================================
# WEB INTERFACE
# ============================================================

HTML = """

<!DOCTYPE html>

<html lang="ru">

<head>

<meta charset="UTF-8">

<meta name="viewport"
content="width=device-width,
initial-scale=1.0">

<meta http-equiv="refresh"
content="30">

<title>
Eva Trading Terminal
</title>


<style>

* {
    box-sizing: border-box;
}

body {

    margin: 0;

    padding: 20px 15px 50px;

    background: #111111;

    color: white;

    font-family:
        -apple-system,
        BlinkMacSystemFont,
        Arial,
        sans-serif;
}


.container {

    max-width: 700px;

    margin: auto;
}


h1 {

    text-align: center;

    color: #00ff88;

    font-size: 30px;

    margin:
        10px 0 5px;
}


.subtitle {

    text-align: center;

    color: #888888;

    font-size: 16px;

    margin-bottom: 25px;
}


.status-row {

    display: flex;

    gap: 12px;

    margin-bottom: 20px;
}


.badge {

    flex: 1;

    text-align: center;

    padding: 12px 8px;

    border-radius: 30px;

    background:
        rgba(0,255,136,0.12);

    color: #00ff88;

    font-weight: bold;

    font-size: 13px;
}


.badge-live {

    background:
        rgba(255,50,50,0.12);

    color: #ff4444;
}


.card {

    background: #1d1d1d;

    border:
        1px solid #303030;

    border-radius: 18px;

    padding: 22px;

    margin-bottom: 15px;

    box-shadow:
        0 5px 20px
        rgba(0,0,0,0.25);
}


.card-title {

    color: #999999;

    font-size: 14px;

    font-weight: bold;

    letter-spacing: 1.5px;

    text-transform:
        uppercase;
}


.price {

    font-size: 34px;

    font-weight: bold;

    margin-top: 12px;
}


.ticker {

    color: #00ff88;

    font-size: 15px;

    margin-top: 5px;
}


.info {

    color: #bbbbbb;

    font-size: 14px;

    margin-top: 10px;
}


.error {

    color: #ff4444;

    font-size: 13px;

    line-height: 1.5;

    margin-top: 12px;

    word-break: break-word;
}


.strategy {

    border:
        1px solid #00ff88;
}


.strategy-value {

    margin-top: 12px;

    font-size: 17px;
}


.green {

    color: #00ff88;
}


.red {

    color: #ff4444;
}


button {

    width: 100%;

    border: none;

    border-radius: 13px;

    padding: 18px;

    background: #00ff88;

    color: #000000;

    font-size: 18px;

    font-weight: bold;

    cursor: pointer;
}


button:active {

    transform:
        scale(0.98);
}


.global-error {

    border:
        1px solid #ff4444;
}

</style>

</head>


<body>


<div class="container">


<h1>
Eva Trading Terminal
</h1>


<div class="subtitle">
Система биржевого анализа Swing-точек
</div>


<div class="status-row">


<div class="badge">
● РАБОТАЕТ
</div>


<div class="badge
{% if trading %}
badge-live
{% endif %}">


{% if trading %}

РЕАЛЬНЫЕ ТОРГИ

{% else %}

TEST / АНАЛИЗ

{% endif %}


</div>

</div>


<!-- ===================== -->
<!-- CNY -->
<!-- ===================== -->

<div class="card">


<div class="card-title">
🇨🇳 Фьючерс Юань (CNY)
</div>


<div class="price">


{% if status.CNY.price is not none %}

{{ "%.4f"|format(status.CNY.price) }}

{% else %}

Ошибка

{% endif %}


</div>


<div class="ticker">

{{ status.CNY.ticker or "Не найден" }}

</div>


<div class="info">

UID:
{{ status.CNY.uid or "—" }}

</div>


<div class="info">

Свечей:
{{ status.CNY.candles }}

</div>


{% if status.CNY.error %}

<div class="error">

{{ status.CNY.error }}

</div>

{% endif %}


</div>


<!-- ===================== -->
<!-- GOLD -->
<!-- ===================== -->

<div class="card">


<div class="card-title">
🏆 Фьючерс Золото (GOLD)
</div>


<div class="price">


{% if status.GOLD.price is not none %}

{{ "%.2f"|format(status.GOLD.price) }}

{% else %}

Ошибка

{% endif %}


</div>


<div class="ticker">

{{ status.GOLD.ticker or "Не найден" }}

</div>


<div class="info">

UID:
{{ status.GOLD.uid or "—" }}

</div>


<div class="info">

Свечей:
{{ status.GOLD.candles }}

</div>


{% if status.GOLD.error %}

<div class="error">

{{ status.GOLD.error }}

</div>

{% endif %}


</div>


<!-- ===================== -->
<!-- BRENT -->
<!-- ===================== -->

<div class="card">


<div class="card-title">
🛢 Фьючерс Нефть (BRENT)
</div>


<div class="price">


{% if status.BRENT.price is not none %}

{{ "%.2f"|format(status.BRENT.price) }}

{% else %}

Ошибка

{% endif %}


</div>


<div class="ticker">

{{ status.BRENT.ticker or "Не найден" }}

</div>


<div class="info">

UID:
{{ status.BRENT.uid or "—" }}

</div>


<div class="info">

Свечей:
{{ status.BRENT.candles }}

</div>


{% if status.BRENT.error %}

<div class="error">

{{ status.BRENT.error }}

</div>

{% endif %}


</div>


<!-- ===================== -->
<!-- STRATEGY -->
<!-- ===================== -->

<div class="card strategy">


<div class="card-title">

Последний сигнал стратегии

</div>


<div class="strategy-value">

Статус:

<span class="green">

{{ status.strategy.status }}

</span>

</div>


<div class="strategy-value">

Сигнал:

{{ status.strategy.signal }}

</div>


<div class="strategy-value">

Направление:

{{ status.strategy.direction or "—" }}

</div>


</div>


<!-- ===================== -->
<!-- GLOBAL ERROR -->
<!-- ===================== -->

{% if status.global_error %}

<div class="card global-error">


<div class="card-title">

Ошибка системы

</div>


<div class="error">

{{ status.global_error }}

</div>


</div>

{% endif %}


<button
onclick="location.reload()">

ОБНОВИТЬ ДАННЫЕ

</button>


</div>


</body>

</html>

"""


# ============================================================
# ГЛАВНАЯ СТРАНИЦА
# ============================================================

@app.route("/")
def home():

    return render_template_string(

        HTML,

        status=BOT_STATUS,

        trading=LIVE_TRADING

    )


# ============================================================
# API STATUS
# ============================================================

@app.route("/api/status")
def api_status():

    return jsonify(
        BOT_STATUS
    )


# ============================================================
# HEALTH
# ============================================================

@app.route("/health")
def health():

    return jsonify({

        "status": "ok",

        "bot_running":
            BOT_STATUS["running"],

        "live_trading":
            LIVE_TRADING,

        "last_update":
            BOT_STATUS["last_update"],

        "error":
            BOT_STATUS["global_error"]
    })


# ============================================================
# ЗАПУСК
# ============================================================

if __name__ == "__main__":


    worker = threading.Thread(

        target=background_worker,

        daemon=True

    )

    worker.start()


    port = int(

        os.environ.get(
            "PORT",
            "5000"
        )

    )


    app.run(

        host="0.0.0.0",

        port=port

    )
