import os
import time
import uuid
import threading
import logging
import requests

from datetime import datetime, timedelta, timezone
from flask import Flask, jsonify, render_template_string


# ============================================================
# CONFIG
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

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# ВАЖНО:
# false = анализ / тест
# true  = реальные заявки
LIVE_TRADING = os.getenv("LIVE_TRADING", "false").lower() == "true"

LOTS = int(os.getenv("LOTS", "1"))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "30"))

CANDLES_COUNT = 300
TIMEFRAME = "CANDLE_INTERVAL_5_MIN"

# Актуальный REST endpoint T-Bank
API_URL = "https://invest-public-api.tbank.ru/rest"


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

log = logging.getLogger("EVA_TRADING_BOT")


# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)


BOT_STATUS = {
    "running": False,
    "last_update": None,
    "last_error": None,

    "CNY": {
        "ticker": None,
        "uid": None,
        "price": None,
        "candles": 0,
        "error": None
    },

    "GOLD": {
        "ticker": None,
        "uid": None,
        "price": None,
        "candles": 0,
        "error": None
    },

    "BRENT": {
        "ticker": None,
        "uid": None,
        "price": None,
        "candles": 0,
        "error": None
    },

    "strategy": {
        "status": "Запуск...",
        "signal": "Нет сигналов",
        "direction": None,
        "time": None
    }
}


# ============================================================
# AUTH
# ============================================================

def get_headers():

    if not T_BANK_TOKEN:
        raise RuntimeError(
            "T_BANK_TOKEN не найден в Environment Variables Render."
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

    units = int(value.get("units", 0))
    nano = int(value.get("nano", 0))

    return units + nano / 1_000_000_000


# ============================================================
# API REQUEST
# ============================================================

def api_post(path, payload):

    url = f"{API_URL}{path}"

    try:

        response = requests.post(
            url,
            json=payload,
            headers=get_headers(),
            timeout=20
        )

        if response.status_code != 200:

            error_text = response.text[:2000]

            raise RuntimeError(
                f"T-Bank API {response.status_code}: {error_text}"
            )

        return response.json()

    except requests.RequestException as e:

        raise RuntimeError(
            f"Ошибка соединения с T-Bank: {e}"
        )


# ============================================================
# GET FUTURES
# ============================================================

def get_futures():

    path = (
        "/tinkoff.public.invest.api.contract.v1."
        "InstrumentsService/Futures"
    )

    payload = {
        "instrumentStatus": "INSTRUMENT_STATUS_BASE"
    }

    data = api_post(path, payload)

    return data.get("instruments", [])


# ============================================================
# FIND ACTIVE FUTURE
# ============================================================

def find_active_future(prefix):

    futures = get_futures()

    candidates = []

    now = datetime.now(timezone.utc)

    for instrument in futures:

        ticker = str(
            instrument.get("ticker", "")
        ).upper()

        if not ticker.startswith(prefix.upper()):
            continue

        # Только доступные через API
        if instrument.get("apiTradeAvailableFlag") is False:
            continue

        # Некоторые версии API используют эти поля
        if instrument.get("buyAvailableFlag") is False:
            continue

        expiration = instrument.get("expirationDate")

        if expiration:

            try:

                exp_date = datetime.fromisoformat(
                    expiration.replace("Z", "+00:00")
                )

                if exp_date <= now:
                    continue

            except Exception:
                pass

        candidates.append(instrument)

    if not candidates:
        return None

    # Сначала ближайший по экспирации
    def expiration_key(x):

        value = x.get("expirationDate")

        if not value:
            return "9999-12-31"

        return value

    candidates.sort(key=expiration_key)

    return candidates[0]


# ============================================================
# GET LAST PRICE
# ============================================================

def get_last_price(instrument):

    instrument_id = (
        instrument.get("uid")
        or instrument.get("instrumentUid")
        or instrument.get("figi")
    )

    if not instrument_id:
        raise RuntimeError(
            f"У инструмента {instrument.get('ticker')} "
            "нет UID/FIGI."
        )

    path = (
        "/tinkoff.public.invest.api.contract.v1."
        "MarketDataService/GetLastPrices"
    )

    payload = {
        "instrumentId": [
            instrument_id
        ],
        "lastPriceType": "LAST_PRICE_EXCHANGE"
    }

    data = api_post(path, payload)

    prices = data.get("lastPrices", [])

    if not prices:
        return None

    return quotation_to_float(
        prices[0].get("price", {})
    )


# ============================================================
# GET CANDLES
# ============================================================

def get_candles(instrument):

    instrument_id = (
        instrument.get("uid")
        or instrument.get("instrumentUid")
        or instrument.get("figi")
    )

    if not instrument_id:
        raise RuntimeError(
            f"Нет instrumentId для {instrument.get('ticker')}"
        )

    now = datetime.now(timezone.utc)

    # Для 5-минутных свечей берём последние сутки
    start = now - timedelta(hours=24)

    path = (
        "/tinkoff.public.invest.api.contract.v1."
        "MarketDataService/GetCandles"
    )

    payload = {
        "instrumentId": instrument_id,
        "from": start.isoformat(),
        "to": now.isoformat(),
        "interval": TIMEFRAME,
        "limit": CANDLES_COUNT
    }

    data = api_post(path, payload)

    return data.get("candles", [])


# ============================================================
# UPDATE ONE INSTRUMENT
# ============================================================

def update_instrument(
    key,
    prefix
):

    try:

        instrument = find_active_future(prefix)

        if not instrument:

            raise RuntimeError(
                f"Фьючерс с префиксом {prefix} не найден."
            )

        ticker = instrument.get("ticker")

        uid = (
            instrument.get("uid")
            or instrument.get("instrumentUid")
            or instrument.get("figi")
        )

        price = get_last_price(instrument)

        candles = get_candles(instrument)

        BOT_STATUS[key]["ticker"] = ticker
        BOT_STATUS[key]["uid"] = uid
        BOT_STATUS[key]["price"] = price
        BOT_STATUS[key]["candles"] = len(candles)
        BOT_STATUS[key]["error"] = None

        log.info(
            "%s | %s | price=%s | candles=%s",
            key,
            ticker,
            price,
            len(candles)
        )

        return candles

    except Exception as e:

        error = str(e)

        BOT_STATUS[key]["error"] = error

        log.error(
            "%s ERROR: %s",
            key,
            error
        )

        return []


# ============================================================
# SIMPLE SWING ANALYSIS
# ============================================================

def analyze_swing(candles):

    if len(candles) < 20:

        return {
            "status": "Недостаточно свечей",
            "signal": "Нет сигналов",
            "direction": None
        }

    # Последние закрытия
    closes = []

    for candle in candles:

        close = quotation_to_float(
            candle.get("close", {})
        )

        if close > 0:
            closes.append(close)

    if len(closes) < 20:

        return {
            "status": "Недостаточно цен",
            "signal": "Нет сигналов",
            "direction": None
        }

    # --------------------------------------------------------
    # Пока только определяем локальную структуру.
    #
    # Это НЕ открывает сделку.
    # Следующим этапом сюда подключим именно твою
    # Swing-стратегию:
    #
    # SHORT:
    #   каждый новый максимум выше предыдущего
    #   после чего появляется подтверждение падения
    #
    # LONG:
    #   каждый новый минимум ниже предыдущего
    #   после чего появляется подтверждение роста
    # --------------------------------------------------------

    last = closes[-1]
    prev = closes[-2]

    if last > prev:

        return {
            "status": "Анализ Swing",
            "signal": "Наблюдение за ростом",
            "direction": "UP"
        }

    if last < prev:

        return {
            "status": "Анализ Swing",
            "signal": "Наблюдение за снижением",
            "direction": "DOWN"
        }

    return {
        "status": "Анализ Swing",
        "signal": "Нет сигнала",
        "direction": None
    }


# ============================================================
# MARKET DATA
# ============================================================

def update_market():

    BOT_STATUS["last_error"] = None

    cny_candles = update_instrument(
        "CNY",
        "CR"
    )

    update_instrument(
        "GOLD",
        "GD"
    )

    update_instrument(
        "BRENT",
        "BR"
    )

    # Стратегию пока анализируем именно по CNY
    strategy = analyze_swing(cny_candles)

    BOT_STATUS["strategy"] = {
        **strategy,
        "time": datetime.now(
            timezone.utc
        ).isoformat()
    }

    BOT_STATUS["last_update"] = datetime.now(
        timezone.utc
    ).isoformat()


# ============================================================
# REAL ORDER
# ============================================================

def place_market_order(
    instrument_id,
    direction,
    quantity
):

    if not LIVE_TRADING:

        log.warning(
            "[TEST MODE] Заявка НЕ отправлена: %s %s",
            direction,
            quantity
        )

        return {
            "test_mode": True,
            "message": "Реальная торговля выключена."
        }

    if not ACCOUNT_ID:

        raise RuntimeError(
            "ACCOUNT_ID не найден."
        )

    path = (
        "/tinkoff.public.invest.api.contract.v1."
        "OrdersService/PostOrder"
    )

    # Для market order price игнорируется API.
    # Но поле присутствует в актуальной схеме.
    payload = {

        "instrumentId": instrument_id,

        "quantity": str(
            int(quantity)
        ),

        "price": {
            "units": "0",
            "nano": 0
        },

        "direction": direction,

        "accountId": ACCOUNT_ID,

        "orderType": "ORDER_TYPE_MARKET",

        "orderId": str(
            uuid.uuid4()
        ),

        "priceType": "PRICE_TYPE_POINT",

        "confirmMarginTrade": True
    }

    result = api_post(
        path,
        payload
    )

    log.warning(
        "REAL ORDER RESPONSE: %s",
        result
    )

    return result


# ============================================================
# BACKGROUND WORKER
# ============================================================

def background_worker():

    log.info(
        "Eva Trading Bot запущен."
    )

    BOT_STATUS["running"] = True

    while True:

        try:

            update_market()

        except Exception as e:

            BOT_STATUS["last_error"] = str(e)

            log.exception(
                "Ошибка фонового потока"
            )

        time.sleep(
            CHECK_INTERVAL
        )


# ============================================================
# WEB PAGE
# ============================================================

@app.route("/")
def home():

    return render_template_string(
        HTML,
        status=BOT_STATUS,
        trading=LIVE_TRADING
    )


# ============================================================
# JSON STATUS
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
        "bot_running": BOT_STATUS["running"],
        "live_trading": LIVE_TRADING
    })


# ============================================================
# HTML
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

<title>Eva Trading Terminal</title>

<style>

body {

    margin: 0;

    padding: 25px 15px;

    background: #111;

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

    margin-bottom: 5px;

}

.subtitle {

    text-align: center;

    color: #888;

    margin-bottom: 25px;

}

.status {

    display: flex;

    justify-content: space-between;

    gap: 10px;

    margin-bottom: 20px;

}

.badge {

    flex: 1;

    text-align: center;

    padding: 12px;

    border-radius: 25px;

    background: #073b28;

    color: #00ff88;

    font-weight: bold;

}

.live {

    background: #451818;

    color: #ff3333;

}

.card {

    background: #1d1d1d;

    border: 1px solid #303030;

    border-radius: 16px;

    padding: 22px;

    margin-bottom: 15px;

}

.title {

    color: #999;

    font-weight: bold;

    letter-spacing: 1px;

    font-size: 14px;

}

.price {

    font-size: 32px;

    font-weight: bold;

    margin-top: 12px;

}

.ticker {

    color: #00ff88;

    font-size: 14px;

}

.error {

    color: #ff4444;

    font-size: 14px;

    margin-top: 10px;

    word-break: break-word;

}

.ok {

    color: #00ff88;

}

.strategy {

    border: 1px solid #00ff88;

}

button {

    width: 100%;

    padding: 18px;

    border: 0;

    border-radius: 12px;

    background: #00ff88;

    color: #000;

    font-size: 18px;

    font-weight: bold;

}

</style>

</head>

<body>

<div class="container">

<h1>Eva Trading Terminal</h1>

<div class="subtitle">
Система биржевого анализа Swing-точек
</div>


<div class="status">

<div class="badge">

● РАБОТАЕТ

</div>

<div class="badge {% if trading %}live{% endif %}">

{% if trading %}

РЕАЛЬНЫЕ ТОРГИ

{% else %}

TEST / АНАЛИЗ

{% endif %}

</div>

</div>


<!-- CNY -->

<div class="card">

<div class="title">
🇨🇳 ФЬЮЧЕРС ЮАНЬ (CNY)
</div>

<div class="price">

{% if status.CNY.price is not none %}

{{ "%.3f"|format(status.CNY.price) }}

{% else %}

Ошибка

{% endif %}

</div>

<div class="ticker">

{{ status.CNY.ticker or "Не найден" }}

</div>

<div>

Свечей:
{{ status.CNY.candles }}

</div>

{% if status.CNY.error %}

<div class="error">

{{ status.CNY.error }}

</div>

{% endif %}

</div>


<!-- GOLD -->

<div class="card">

<div class="title">
🏆 ФЬЮЧЕРС ЗОЛОТО (GOLD)
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

<div>

Свечей:
{{ status.GOLD.candles }}

</div>

{% if status.GOLD.error %}

<div class="error">

{{ status.GOLD.error }}

</div>

{% endif %}

</div>


<!-- BRENT -->

<div class="card">

<div class="title">
🛢 ФЬЮЧЕРС НЕФТЬ (BRENT)
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

<div>

Свечей:
{{ status.BRENT.candles }}

</div>

{% if status.BRENT.error %}

<div class="error">

{{ status.BRENT.error }}

</div>

{% endif %}

</div>


<!-- STRATEGY -->

<div class="card strategy">

<div class="title">

ПОСЛЕДНИЙ СИГНАЛ СТРАТЕГИИ

</div>

<div style="margin-top:15px;">

Статус:

<span class="ok">

{{ status.strategy.status }}

</span>

</div>

<div style="margin-top:8px;">

Сигнал:

{{ status.strategy.signal }}

</div>

<div style="margin-top:8px;">

Время:

{{ status.strategy.time or "-" }}

</div>

</div>


{% if status.last_error %}

<div class="card">

<div class="title">
ОБЩАЯ ОШИБКА
</div>

<div class="error">

{{ status.last_error }}

</div>

</div>

{% endif %}


<button onclick="location.reload()">

ОБНОВИТЬ ДАННЫЕ

</button>


</div>

</body>

</html>

"""


# ============================================================
# START
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
