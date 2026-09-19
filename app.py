import os
import time
import threading
import logging
import requests
from datetime import datetime, timedelta, timezone

from flask import Flask, jsonify, render_template_string
app = Flask(__name__)






# ============================================================
# НАСТРОЙКИ
# ============================================================

# Render может использовать любое из этих названий.
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


# ------------------------------------------------------------
# ВАЖНО
#
# false = бот только анализирует и присылает сигналы
# true  = бот реально отправляет заявки брокеру
# ------------------------------------------------------------

LIVE_TRADING = (
    os.getenv("LIVE_TRADING", "false").lower()
    == "true"
)


# ------------------------------------------------------------
# Инструмент
# ------------------------------------------------------------

BASE_TICKER = "Si"


# ------------------------------------------------------------
# Таймфрейм
# ------------------------------------------------------------

TIMEFRAME = CandleInterval.CANDLE_INTERVAL_5_MIN


# ------------------------------------------------------------
# Количество свечей
# ------------------------------------------------------------

CANDLES_COUNT = 300


# ------------------------------------------------------------
# Swing
# ------------------------------------------------------------

SWING_WINDOW = 3


# ------------------------------------------------------------
# Минимальное движение между первой
# и третьей точкой
# ------------------------------------------------------------

MIN_MOVE_PERCENT = 0.15


# ------------------------------------------------------------
# Размер позиции
# ------------------------------------------------------------

LOTS = int(
    os.getenv("LOTS", "1")
)


# ------------------------------------------------------------
# Интервал проверки
# ------------------------------------------------------------

CHECK_INTERVAL = int(
    os.getenv("CHECK_INTERVAL", "30")
)


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s | "
        "%(levelname)s | "
        "%(message)s"
    ),
)

log = logging.getLogger("TRADING_BOT")


# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)


BOT_STATUS = {
    "running": False,
    "instrument": None,
    "last_price": None,
    "last_signal": None,
    "last_signal_time": None,
    "live_trading": LIVE_TRADING,
}


@app.route("/")
def home():

    return jsonify({
        "status": "online",
        "bot": "Eva Trading Bot",
        "trading": LIVE_TRADING,
        "instrument": BOT_STATUS["instrument"],
        "last_price": BOT_STATUS["last_price"],
        "last_signal": BOT_STATUS["last_signal"],
        "last_signal_time": BOT_STATUS[
            "last_signal_time"
        ],
    })


@app.route("/health")
def health():

    return jsonify({
        "status": "ok",
        "bot_running": BOT_STATUS["running"],
    })


# ============================================================
# TELEGRAM
# ============================================================

def telegram(message):

    if not TELEGRAM_BOT_TOKEN:
        return

    if not TELEGRAM_CHAT_ID:
        return

    try:

        url = (
            "https://api.telegram.org/bot"
            f"{TELEGRAM_BOT_TOKEN}"
            "/sendMessage"
        )

        requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
            },
            timeout=10,
        )

    except Exception as e:

        log.error(
            "Telegram error: %s",
            e,
        )


# ============================================================
# ПРОВЕРКА ENV
# ============================================================

def check_environment():

    if not T_BANK_TOKEN:

        raise RuntimeError(
            "Не найден токен T-Bank.\n"
            "В Render добавь:\n"
            "T_BANK_TOKEN"
        )

    log.info(
        "T-Bank token найден"
    )

    if ACCOUNT_ID:

        log.info(
            "ACCOUNT_ID задан вручную"
        )

    else:

        log.info(
            "ACCOUNT_ID не задан."
            " Бот попробует определить его сам."
        )

    if TELEGRAM_BOT_TOKEN:

        log.info(
            "Telegram BOT TOKEN найден"
        )

    if TELEGRAM_CHAT_ID:

        log.info(
            "Telegram CHAT ID найден"
        )


# ============================================================
# ПОЛУЧЕНИЕ ACCOUNT ID
# ============================================================

def get_account_id(client):

    if ACCOUNT_ID:

        return ACCOUNT_ID

    response = client.users.get_accounts()

    if not response.accounts:

        raise RuntimeError(
            "У токена нет доступных брокерских счетов."
        )

    # Берем первый доступный счет

    account = response.accounts[0]

    log.info(
        "Найден счет: %s",
        account.id,
    )

    return account.id


# ============================================================
# ПОИСК SI
# ============================================================

def find_si_future(client):

    response = client.instruments.futures()

    candidates = []

    for instrument in response.instruments:

        ticker = (
            instrument.ticker
            or ""
        )

        if not ticker.upper().startswith(
            BASE_TICKER.upper()
        ):
            continue

        if not getattr(
            instrument,
            "api_trade_available_flag",
            True,
        ):
            continue

        candidates.append(
            instrument
        )

    if not candidates:

        raise RuntimeError(
            "Фьючерс Si не найден."
        )

    # Пытаемся выбрать ближайший срок экспирации

    def expiration_key(x):

        expiration = getattr(
            x,
            "expiration_date",
            None,
        )

        if expiration is None:

            return datetime.max.replace(
                tzinfo=timezone.utc
            )

        return expiration

    candidates.sort(
        key=expiration_key
    )

    future = candidates[0]

    log.info(
        "Выбран фьючерс: %s | %s | UID=%s",
        future.ticker,
        future.name,
        future.uid,
    )

    return future


# ============================================================
# ПОЛУЧЕНИЕ СВЕЧЕЙ
# ============================================================

def get_candles(
    client,
    instrument_uid,
):

    now = datetime.now(
        timezone.utc
    )

    start = now - timedelta(
        days=7
    )

    response = (
        client.market_data.get_candles(
            instrument_id=instrument_uid,
            from_=start,
            to=now,
            interval=TIMEFRAME,
        )
    )

    result = []

    for candle in response.candles:

        result.append({
            "time": candle.time,
            "open": float(candle.open),
            "high": float(candle.high),
            "low": float(candle.low),
            "close": float(candle.close),
        })

    return result[-CANDLES_COUNT:]


# ============================================================
# SWING ТОЧКИ
# ============================================================

def find_swings(candles):

    highs = []
    lows = []

    w = SWING_WINDOW

    for i in range(
        w,
        len(candles) - w
    ):

        high = candles[i]["high"]
        low = candles[i]["low"]

        left = candles[
            i - w:i
        ]

        right = candles[
            i + 1:i + w + 1
        ]

        left_high = max(
            x["high"]
            for x in left
        )

        right_high = max(
            x["high"]
            for x in right
        )

        left_low = min(
            x["low"]
            for x in left
        )

        right_low = min(
            x["low"]
            for x in right
        )

        if (
            high > left_high
            and
            high > right_high
        ):

            highs.append(i)

        if (
            low < left_low
            and
            low < right_low
        ):

            lows.append(i)

    return highs, lows


# ============================================================
# SHORT
#
# H1 < H2 < H3
#
# Три последовательных повышающихся максимума.
# ============================================================

def check_short(
    candles,
    highs,
):

    if len(highs) < 3:

        return False

    a = highs[-3]
    b = highs[-2]
    c = highs[-1]

    h1 = candles[a]["high"]
    h2 = candles[b]["high"]
    h3 = candles[c]["high"]

    if not (
        h1 < h2 < h3
    ):

        return False

    movement = (
        (h3 - h1)
        / h1
        * 100
    )

    return (
        movement
        >= MIN_MOVE_PERCENT
    )


# ============================================================
# LONG
#
# L1 > L2 > L3
#
# Три последовательных понижающихся минимума.
# ============================================================

def check_long(
    candles,
    lows,
):

    if len(lows) < 3:

        return False

    a = lows[-3]
    b = lows[-2]
    c = lows[-1]

    l1 = candles[a]["low"]
    l2 = candles[b]["low"]
    l3 = candles[c]["low"]

    if not (
        l1 > l2 > l3
    ):

        return False

    movement = (
        (l1 - l3)
        / l1
        * 100
    )

    return (
        movement
        >= MIN_MOVE_PERCENT
    )


# ============================================================
# ПОЛУЧЕНИЕ ТЕКУЩЕЙ ПОЗИЦИИ
# ============================================================

def get_position(
    client,
    account_id,
    instrument_uid,
):

    response = (
        client.operations.get_positions(
            account_id=account_id
        )
    )

    for position in response.securities:

        if (
            position.instrument_uid
            != instrument_uid
        ):
            continue

        return position.balance

    return 0


# ============================================================
# ОТПРАВКА ЗАЯВКИ
# ============================================================

def send_market_order(
    client,
    account_id,
    instrument_uid,
    direction,
):

    if not LIVE_TRADING:

        log.warning(
            "TEST MODE: заявка НЕ отправлена"
        )

        telegram(
            f"⚠️ СИГНАЛ\n"
            f"{direction}\n"
            f"Инструмент: {BASE_TICKER}\n"
            f"Реальная заявка: НЕТ"
        )

        return None

    if direction == "LONG":

        order_direction = (
            OrderDirection
            .ORDER_DIRECTION_BUY
        )

    else:

        order_direction = (
            OrderDirection
            .ORDER_DIRECTION_SELL
        )

    response = (
        client.orders.post_order(
            account_id=account_id,
            instrument_id=instrument_uid,
            quantity=LOTS,
            direction=order_direction,
            order_type=OrderType
            .ORDER_TYPE_MARKET,
        )
    )

    log.info(
        "Заявка отправлена: %s",
        response.order_id,
    )

    telegram(
        f"🚨 ЗАЯВКА ОТПРАВЛЕНА\n"
        f"Направление: {direction}\n"
        f"Инструмент: {BASE_TICKER}\n"
        f"Лотов: {LOTS}\n"
        f"Order ID: {response.order_id}"
    )

    return response


# ============================================================
# ОСНОВНОЙ ТОРГОВЫЙ ЦИКЛ
# ============================================================

def trading_loop():

    BOT_STATUS["running"] = True

    check_environment()

    with Client(
        T_BANK_TOKEN
    ) as client:

        account_id = (
            get_account_id(client)
        )

        log.info(
            "ACCOUNT_ID = %s",
            account_id,
        )

        future = find_si_future(
            client
        )

        instrument_uid = future.uid

        BOT_STATUS[
            "instrument"
        ] = future.ticker

        telegram(
            "🤖 Торговый бот запущен\n"
            f"Инструмент: {future.ticker}\n"
            f"Режим: "
            f"{'REAL' if LIVE_TRADING else 'TEST'}"
        )

        last_signal = None

        while True:

            try:

                candles = get_candles(
                    client,
                    instrument_uid
                )

                if len(candles) < 30:

                    log.warning(
                        "Недостаточно свечей"
                    )

                    time.sleep(
                        CHECK_INTERVAL
                    )

                    continue

                highs, lows = (
                    find_swings(candles)
                )

                last_candle = candles[-1]

                price = (
                    last_candle["close"]
                )

                BOT_STATUS[
                    "last_price"
                ] = price

                signal = None

                if check_short(
                    candles,
                    highs
                ):

                    signal = "SHORT"

                elif check_long(
                    candles,
                    lows
                ):

                    signal = "LONG"

                log.info(
                    "Цена=%s | "
                    "HIGH=%s | "
                    "LOW=%s | "
                    "SIGNAL=%s",
                    price,
                    len(highs),
                    len(lows),
                    signal or "-",
                )

                if signal:

                    signal_key = (
                        f"{signal}_"
                        f"{last_candle['time']}"
                    )

                    if (
                        signal_key
                        != last_signal
                    ):

                        BOT_STATUS[
                            "last_signal"
                        ] = signal

                        BOT_STATUS[
                            "last_signal_time"
                        ] = str(
                            last_candle[
                                "time"
                            ]
                        )

                        log.warning(
                            "🔥 SIGNAL: %s",
                            signal,
                        )

                        # ------------------------------------------------
                        # Проверяем позицию
                        # ------------------------------------------------

                        position = (
                            get_position(
                                client,
                                account_id,
                                instrument_uid,
                            )
                        )

                        log.info(
                            "Текущая позиция: %s",
                            position,
                        )

                        # ------------------------------------------------
                        # Если позиции нет — открываем
                        # ------------------------------------------------

                        if position == 0:

                            send_market_order(
                                client,
                                account_id,
                                instrument_uid,
                                signal,
                            )

                        else:

                            log.info(
                                "Позиция уже существует. "
                                "Новая не открывается."
                            )

                        last_signal = (
                            signal_key
                        )

                time.sleep(
                    CHECK_INTERVAL
                )

            except Exception as error:

                log.exception(
                    "Ошибка торгового цикла: %s",
                    error,
                )

                telegram(
                    "❌ ОШИБКА БОТА\n"
                    f"{error}"
                )

                time.sleep(30)


# ============================================================
# ЗАПУСК БОТА В ФОНОВОМ ПОТОКЕ
# ============================================================

def start_bot():

    thread = threading.Thread(
        target=trading_loop,
        daemon=True,
    )

    thread.start()


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    start_bot()

    port = int(
        os.getenv(
            "PORT",
            "10000"
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
    )
