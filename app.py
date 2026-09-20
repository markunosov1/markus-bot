import os
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, jsonify, render_template_string


# ============================================================
# НАСТРОЙКИ
# ============================================================

APP_NAME = "Markus Trade"

API_URL = "https://invest-public-api.tbank.ru/rest"

FUTURES_ENDPOINT = (
    API_URL +
    "/tinkoff.public.invest.api.contract.v1.InstrumentsService/Futures"
)

CANDLES_ENDPOINT = (
    API_URL +
    "/tinkoff.public.invest.api.contract.v1.MarketDataService/GetCandles"
)

REQUEST_TIMEOUT = 20

# 15-минутные свечи
CANDLE_INTERVAL = "CANDLE_INTERVAL_15_MIN"

# Сколько свечей пытаемся получить
CANDLE_LIMIT = 500

# Сколько часов истории запрашиваем
HISTORY_HOURS = 24 * 7


# ============================================================
# ЛОГИ
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

log = logging.getLogger("markus_trade")


# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)


# ============================================================
# ПОИСК API-КЛЮЧА
# ============================================================

def get_token():
    """
    Ищем токен в переменных окружения.

    Поддерживаются:
    TINKOFF_TOKEN
    TINVEST_TOKEN
    T_BANK_TOKEN
    API_TOKEN
    TOKEN
    """

    names = [
        "TINKOFF_TOKEN",
        "TINVEST_TOKEN",
        "T_BANK_TOKEN",
        "API_TOKEN",
        "TOKEN",
    ]

    for name in names:
        value = os.getenv(name)

        if value:
            value = value.strip()

            if value:
                log.info("API-токен найден в переменной %s", name)
                return value

    return None


# ============================================================
# HTTP ЗАПРОС
# ============================================================

def api_post(url, payload):
    token = get_token()

    if not token:
        raise RuntimeError(
            "Не найден API-токен. "
            "Проверь переменную TINKOFF_TOKEN / TINVEST_TOKEN / TOKEN."
        )

    headers = {
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
    }

    response = requests.post(
        url,
        headers=headers,
        json=payload,
        timeout=REQUEST_TIMEOUT,
    )

    log.info(
        "API %s -> HTTP %s",
        url.split("/")[-1],
        response.status_code
    )

    if response.status_code != 200:
        text = response.text[:1000]

        raise RuntimeError(
            f"API HTTP {response.status_code}: {text}"
        )

    try:
        return response.json()

    except Exception as e:
        raise RuntimeError(
            f"API вернул некорректный JSON: {e}"
        )


# ============================================================
# ПОЛУЧЕНИЕ ВСЕХ ФЬЮЧЕРСОВ
# ============================================================

def get_all_futures():

    payload = {
        "instrumentStatus": "INSTRUMENT_STATUS_ALL"
    }

    data = api_post(
        FUTURES_ENDPOINT,
        payload
    )

    futures = data.get("futures")

    if futures is None:
        futures = data.get("instruments")

    if futures is None:
        futures = []

    if not isinstance(futures, list):
        futures = []

    log.info(
        "Получено фьючерсов: %s",
        len(futures)
    )

    return futures


# ============================================================
# ПРЕОБРАЗОВАНИЕ ДАТЫ
# ============================================================

def parse_date(value):

    if not value:
        return None

    try:
        # Например:
        # 2026-09-18T00:00:00Z
        return datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )
    except Exception:
        return None


# ============================================================
# ПОЛУЧЕНИЕ СТРОКИ ИЗ ОБЪЕКТА
# ============================================================

def safe_str(obj, key):

    value = obj.get(key)

    if value is None:
        return ""

    return str(value).strip()


# ============================================================
# ПРОВЕРКА ФЬЮЧЕРСА
# ============================================================

def future_matches(future, prefix):

    prefix = prefix.upper()

    ticker = safe_str(future, "ticker").upper()
    name = safe_str(future, "name").upper()
    basic_asset = safe_str(future, "basicAsset").upper()
    basic_asset_position_uid = safe_str(
        future,
        "basicAssetPositionUid"
    ).upper()

    # --------------------------------------------------------
    # 1. Самый надёжный вариант — тикер
    # --------------------------------------------------------

    if ticker.startswith(prefix):
        return True

    # --------------------------------------------------------
    # 2. Основной актив
    # --------------------------------------------------------

    if prefix == "CR":

        keywords = [
            "CNY",
            "YUAN",
            "CNH",
            "КИТАЙСК",
            "ЮАН"
        ]

        for word in keywords:

            if word in name:
                return True

            if word in basic_asset:
                return True

            if word in basic_asset_position_uid:
                return True

    # --------------------------------------------------------
    # GOLD
    # --------------------------------------------------------

    if prefix == "GD":

        keywords = [
            "GOLD",
            "ЗОЛОТ",
            "ЗОЛОТО"
        ]

        for word in keywords:

            if word in name:
                return True

            if word in basic_asset:
                return True

    # --------------------------------------------------------
    # BRENT
    # --------------------------------------------------------

    if prefix == "BR":

        keywords = [
            "BRENT",
            "БРЕНТ"
        ]

        for word in keywords:

            if word in name:
                return True

            if word in basic_asset:
                return True

    return False


# ============================================================
# ПОИСК АКТИВНОГО ФЬЮЧЕРСА
# ============================================================

def find_active_future(prefix):

    log.info(
        "Ищу актуальный фьючерс: %s",
        prefix
    )

    futures = get_all_futures()

    now = datetime.now(timezone.utc)

    candidates = []

    for future in futures:

        if not future_matches(future, prefix):
            continue

        ticker = safe_str(
            future,
            "ticker"
        )

        uid = safe_str(
            future,
            "uid"
        )

        instrument_uid = safe_str(
            future,
            "instrumentUid"
        )

        if not instrument_uid:
            instrument_uid = uid

        if not instrument_uid:
            continue

        last_trade_date = parse_date(
            future.get("lastTradeDate")
        )

        first_trade_date = parse_date(
            future.get("firstTradeDate")
        )

        # Если дата окончания известна
        # и контракт уже закончился — пропускаем.
        if last_trade_date:

            if last_trade_date < now:
                continue

        # Если начало торговли в будущем —
        # тоже пропускаем.
        if first_trade_date:

            if first_trade_date > now:
                continue

        candidate = {
            "ticker": ticker,
            "uid": uid,
            "instrument_uid": instrument_uid,
            "name": safe_str(future, "name"),
            "basic_asset": safe_str(
                future,
                "basicAsset"
            ),
            "class_code": safe_str(
                future,
                "classCode"
            ),
            "first_trade_date": (
                first_trade_date.isoformat()
                if first_trade_date
                else ""
            ),
            "last_trade_date": (
                last_trade_date.isoformat()
                if last_trade_date
                else ""
            ),
            "raw": future,
        }

        candidates.append(candidate)

    if not candidates:

        log.warning(
            "Фьючерс %s не найден",
            prefix
        )

        return None

    # --------------------------------------------------------
    # Сортируем по ближайшей дате экспирации.
    # Это позволяет выбрать актуальный контракт.
    # --------------------------------------------------------

    def expiry_key(item):

        value = parse_date(
            item.get("last_trade_date")
        )

        if value:
            return value

        return datetime.max.replace(
            tzinfo=timezone.utc
        )

    candidates.sort(
        key=expiry_key
    )

    selected = candidates[0]

    log.info(
        "Выбран %s | UID=%s | expiry=%s",
        selected["ticker"],
        selected["instrument_uid"],
        selected["last_trade_date"]
    )

    return selected


# ============================================================
# ПОЛУЧЕНИЕ СВЕЧЕЙ
# ============================================================

def get_candles(instrument_uid):

    now = datetime.now(timezone.utc)

    start = now - timedelta(
        hours=HISTORY_HOURS
    )

    payload = {
        "from": start.isoformat(),
        "to": now.isoformat(),
        "interval": CANDLE_INTERVAL,
        "instrumentId": instrument_uid,
        "limit": CANDLE_LIMIT,
        "candleSourceType": "CANDLE_SOURCE_EXCHANGE"
    }

    data = api_post(
        CANDLES_ENDPOINT,
        payload
    )

    candles = data.get(
        "candles",
        []
    )

    if not isinstance(candles, list):
        candles = []

    log.info(
        "Получено свечей: %s",
        len(candles)
    )

    return candles


# ============================================================
# QUOTATION -> FLOAT
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
            return float(units) + (
                float(nano) / 1_000_000_000
            )
        except Exception:
            return 0.0

    try:
        return float(value)

    except Exception:
        return 0.0


# ============================================================
# ПРЕОБРАЗОВАНИЕ СВЕЧЕЙ
# ============================================================

def normalize_candles(candles):

    result = []

    for candle in candles:

        result.append({
            "time": candle.get("time"),

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

            "volume": int(
                candle.get("volume", 0) or 0
            )
        })

    result.sort(
        key=lambda x: x.get("time") or ""
    )

    return result


# ============================================================
# АНАЛИЗ ТВОЕЙ СТРАТЕГИИ
# ============================================================

def analyze_strategy(candles):

    if len(candles) < 8:

        return {
            "signal": "Нет сигналов",
            "direction": "—",
            "description": "Недостаточно свечей"
        }

    # Последние свечи
    recent = candles[-8:]

    highs = [
        x["high"]
        for x in recent
    ]

    lows = [
        x["low"]
        for x in recent
    ]

    closes = [
        x["close"]
        for x in recent
    ]

    # --------------------------------------------------------
    # Твоя идея SHORT:
    #
    # каждый новый максимум выше предыдущего,
    # затем появляется сильное движение вниз.
    # --------------------------------------------------------

    rising_highs = (
        highs[3] > highs[2] and
        highs[4] > highs[3] and
        highs[5] > highs[4]
    )

    falling_after_high = (
        closes[-1] < closes[-2] and
        closes[-2] < closes[-3]
    )

    # --------------------------------------------------------
    # LONG:
    #
    # последовательные минимумы ниже предыдущих,
    # затем появляется движение вверх.
    # --------------------------------------------------------

    falling_lows = (
        lows[3] < lows[2] and
        lows[4] < lows[3] and
        lows[5] < lows[4]
    )

    rising_after_low = (
        closes[-1] > closes[-2] and
        closes[-2] > closes[-3]
    )

    # --------------------------------------------------------
    # SHORT
    # --------------------------------------------------------

    if rising_highs and falling_after_high:

        return {
            "signal": "SHORT",
            "direction": "ВНИЗ",
            "description": (
                "Обнаружена последовательность "
                "повышающихся максимумов "
                "с последующим снижением."
            )
        }

    # --------------------------------------------------------
    # LONG
    # --------------------------------------------------------

    if falling_lows and rising_after_low:

        return {
            "signal": "LONG",
            "direction": "ВВЕРХ",
            "description": (
                "Обнаружена последовательность "
                "понижающихся минимумов "
                "с последующим ростом."
            )
        }

    # --------------------------------------------------------
    # НЕТ СИГНАЛА
    # --------------------------------------------------------

    return {
        "signal": "Нет сигналов",
        "direction": "—",
        "description": "Условия стратегии пока не выполнены."
    }


# ============================================================
# СОСТОЯНИЕ ИНСТРУМЕНТА
# ============================================================

def get_future_status(prefix, title, emoji):

    base = {
        "prefix": prefix,
        "title": title,
        "emoji": emoji,
        "status": "Ошибка",
        "message": "",
        "uid": "",
        "ticker": "",
        "candles": 0,
        "data": [],
        "strategy": {
            "signal": "Нет сигналов",
            "direction": "—",
            "description": ""
        }
    }

    try:

        future = find_active_future(
            prefix
        )

        if not future:

            base["status"] = "Не найден"
            base["message"] = (
                "Актуальный контракт не найден"
            )

            return base

        base["ticker"] = future["ticker"]
        base["uid"] = future["instrument_uid"]

        candles_raw = get_candles(
            future["instrument_uid"]
        )

        candles = normalize_candles(
            candles_raw
        )

        base["candles"] = len(
            candles
        )

        base["data"] = candles

        if not candles:

            base["status"] = "Нет свечей"

            base["message"] = (
                "Фьючерс найден, "
                "но свечи не получены."
            )

            return base

        base["status"] = "OK"

        base["message"] = (
            "Данные получены"
        )

        base["strategy"] = (
            analyze_strategy(candles)
        )

        return base

    except Exception as e:

        log.exception(
            "Ошибка %s",
            prefix
        )

        base["status"] = "Ошибка"

        base["message"] = str(e)

        return base


# ============================================================
# ВСЕ ДАННЫЕ
# ============================================================

def collect_all_data():

    results = []

    results.append(
        get_future_status(
            "CR",
            "ФЬЮЧЕРС ЮАНЬ (CNY)",
            "🇨🇳"
        )
    )

    results.append(
        get_future_status(
            "GD",
            "ФЬЮЧЕРС ЗОЛОТО (GOLD)",
            "🏆"
        )
    )

    results.append(
        get_future_status(
            "BR",
            "ФЬЮЧЕРС НЕФТЬ (BRENT)",
            "🛢️"
        )
    )

    # Последний общий сигнал
    signal = {
        "signal": "Нет сигналов",
        "direction": "—",
        "description": "Ожидание данных..."
    }

    # Если где-то появился LONG/SHORT,
    # показываем его.
    for item in results:

        item_signal = item.get(
            "strategy",
            {}
        ).get(
            "signal"
        )

        if item_signal in (
            "LONG",
            "SHORT"
        ):

            signal = item[
                "strategy"
            ]

            break

    return {
        "updated": datetime.now(
            timezone.utc
        ).isoformat(),

        "futures": results,

        "last_signal": signal
    }


# ============================================================
# API
# ============================================================

@app.route("/api/status")
def api_status():

    try:

        data = collect_all_data()

        return jsonify(data)

    except Exception as e:

        log.exception(
            "Общая ошибка API"
        )

        return jsonify({
            "error": str(e)
        }), 500


# ============================================================
# ГЛАВНАЯ СТРАНИЦА
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

    background: #0b0b0b;

    color: white;

    font-family:
        -apple-system,
        BlinkMacSystemFont,
        "Segoe UI",
        Arial,
        sans-serif;
}

.header {

    padding: 30px 20px 20px;

    text-align: center;

    border-bottom:
        1px solid #242424;
}

.header h1 {

    margin: 0;

    font-size: 28px;

    font-weight: 700;
}

.header p {

    margin: 7px 0 0;

    color: #888;

    font-size: 15px;
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

    box-shadow:
        0 8px 30px
        rgba(0,0,0,.25);
}

.title {

    color: #999;

    font-size: 17px;

    font-weight: 700;

    letter-spacing: 2px;

    margin-bottom: 18px;
}

.status {

    font-size: 42px;

    font-weight: 800;

    margin-bottom: 8px;
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

    color: #aaa;

    font-size: 17px;
}

.row span {

    color: white;
}

.signal {

    border:
        2px solid #00ff9d;

    border-radius: 24px;

    padding: 24px;

    margin-top: 20px;

    background: #171717;
}

.signal-title {

    color: #999;

    font-weight: 700;

    letter-spacing: 2px;

    margin-bottom: 20px;
}

.signal-value {

    font-size: 25px;

    margin: 10px 0;
}

button {

    width: 100%;

    padding: 15px;

    border: none;

    border-radius: 14px;

    background: #00ff9d;

    color: #000;

    font-size: 17px;

    font-weight: 700;

    margin-top: 15px;
}

.small {

    color: #777;

    font-size: 13px;

    margin-top: 15px;

    text-align: center;
}

</style>

</head>


<body>


<div class="header">

    <h1>Markus Trade</h1>

    <p>мини-приложение</p>

</div>


<div class="container" id="app">

    <div class="card">

        <div class="status info">
            Загрузка...
        </div>

        <div class="row">
            Получаем список фьючерсов
        </div>

    </div>

</div>


<script>

async function loadData() {

    const app =
        document.getElementById("app");

    try {

        const response =
            await fetch("/api/status");

        const data =
            await response.json();

        if (!response.ok) {

            throw new Error(
                data.error || "Ошибка сервера"
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
                    ${escapeHtml(error.message)}
                </div>

            </div>

        `;

    }

}


function render(data) {

    let html = "";

    for (
        const item of data.futures
    ) {

        let statusClass =
            "warning";

        if (
            item.status === "OK"
        ) {

            statusClass = "ok";

        }

        if (
            item.status === "Ошибка"
        ) {

            statusClass = "error";

        }

        html += `

            <div class="card">

                <div class="title">

                    ${item.emoji}
                    ${escapeHtml(item.title)}

                </div>

                <div class="status ${statusClass}">

                    ${escapeHtml(item.status)}

                </div>

                <div class="row">

                    ${escapeHtml(
                        item.message || ""
                    )}

                </div>

                <div class="row">

                    Тикер:
                    <span>
                        ${escapeHtml(
                            item.ticker || "—"
                        )}
                    </span>

                </div>

                <div class="row">

                    UID:
                    <span>
                        ${escapeHtml(
                            item.uid || "—"
                        )}
                    </span>

                </div>

                <div class="row">

                    Свечей:
                    <span>
                        ${item.candles || 0}
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

                ПОСЛЕДНИЙ СИГНАЛ СТРАТЕГИИ

            </div>

            <div class="signal-value">

                Статус:
                <span class="info">

                    ${signal.signal ===
                        "Нет сигналов"
                        ? "Ожидание данных..."
                        : "Сигнал обнаружен"}

                </span>

            </div>

            <div class="signal-value">

                Сигнал:

                <span>

                    ${escapeHtml(
                        signal.signal ||
                        "Нет сигналов"
                    )}

                </span>

            </div>

            <div class="signal-value">

                Направление:

                <span>

                    ${escapeHtml(
                        signal.direction ||
                        "—"
                    )}

                </span>

            </div>

            <div class="row">

                ${escapeHtml(
                    signal.description || ""
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
            ${escapeHtml(
                data.updated || ""
            )}

        </div>

    `;


    document.getElementById(
        "app"
    ).innerHTML = html;

}


function escapeHtml(value) {

    return String(value)

        .replaceAll("&", "&amp;")

        .replaceAll("<", "&lt;")

        .replaceAll(">", "&gt;")

        .replaceAll('"', "&quot;")

        .replaceAll("'", "&#039;");
}


loadData();


// Обновляем каждые 60 секунд

setInterval(
    loadData,
    60000
);

</script>


</body>

</html>
"""


# ============================================================
# WEB
# ============================================================

@app.route("/")
def index():

    return render_template_string(
        HTML
    )


# ============================================================
# ПРОВЕРКА ПРИ ЗАПУСКЕ
# ============================================================

def startup_check():

    log.info(
        "======================================"
    )

    log.info(
        "       MARKUS TRADE START"
    )

    log.info(
        "======================================"
    )

    token = get_token()

    if token:

        log.info(
            "API token: найден"
        )

    else:

        log.warning(
            "API token: НЕ найден"
        )


# ============================================================
# ФОНОВОЙ МОНИТОР
# ============================================================

def background_monitor():

    while True:

        try:

            log.info(
                "Фоновая проверка фьючерсов..."
            )

            data = collect_all_data()

            signal = data.get(
                "last_signal",
                {}
            )

            log.info(
                "Сигнал: %s | Направление: %s",
                signal.get("signal"),
                signal.get("direction")
            )

        except Exception:

            log.exception(
                "Ошибка фонового мониторинга"
            )

        time.sleep(300)


# ============================================================
# ЗАПУСК
# ============================================================

if __name__ == "__main__":

    startup_check()

    # Фоновый поток
    thread = threading.Thread(
        target=background_monitor,
        daemon=True
    )

    thread.start()

    # PythonAnywhere / Render / Railway
    # обычно передают PORT автоматически.

    port = int(
        os.getenv(
            "PORT",
            "5000"
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False
    )
