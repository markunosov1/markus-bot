import os
import time
import threading
import logging
import requests

from datetime import datetime, timedelta, timezone

from flask import Flask, jsonify, render_template_string


# ============================================================
# НАСТРОЙКИ
# ============================================================

API_BASE = "https://invest-public-api.tbank.ru/rest"

CANDLE_INTERVAL = "CANDLE_INTERVAL_15_MIN"

# 14 дней истории
HISTORY_HOURS = 24 * 14

# Обновление данных
UPDATE_SECONDS = 300

# Размер условной позиции для backtest
POSITION_SIZE = 100000.0

# Комиссия
COMMISSION_RATE = 0.001

# Налог на прибыль
TAX_RATE = 0.13

# Минимальное количество сделок,
# чтобы стратегия могла стать победителем
MIN_TRADES = 5


# ============================================================
# ЛОГИ
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("MarkusTrade")


# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)


# ============================================================
# TOKEN
# ============================================================

def get_token():

    names = [
        "TINKOFF_TOKEN",
        "TINVEST_TOKEN",
        "T_BANK_TOKEN",
        "API_TOKEN",
        "TOKEN"
    ]

    for name in names:

        value = os.getenv(name)

        if value:
            return value.strip()

    return ""


TOKEN = get_token()


# ============================================================
# HTTP SESSION
# ============================================================

session = requests.Session()

session.headers.update({
    "Content-Type": "application/json",
    "Accept": "application/json"
})


# ============================================================
# ГЛОБАЛЬНЫЕ ДАННЫЕ
# ============================================================

market_data = {}

data_lock = threading.Lock()


# ============================================================
# БАЗОВЫЙ РЕЗУЛЬТАТ
# ============================================================

def no_signal(description="Сигнал не сформирован"):

    return {
        "signal": "Нет сигналов",
        "direction": "—",
        "description": description
    }


# ============================================================
# 1. ТВОЯ СТРАТЕГИЯ
# ============================================================

def strategy_my(candles):

    if len(candles) < 8:
        return no_signal("Недостаточно свечей")

    last = candles[-8:]

    highs = [float(x["high"]) for x in last]
    lows = [float(x["low"]) for x in last]
    closes = [float(x["close"]) for x in last]

    short_pattern = (
        highs[3] > highs[2]
        and highs[4] > highs[3]
        and highs[5] > highs[4]
        and closes[-1] < closes[-2]
        and closes[-2] < closes[-3]
    )

    long_pattern = (
        lows[3] < lows[2]
        and lows[4] < lows[3]
        and lows[5] < lows[4]
        and closes[-1] > closes[-2]
        and closes[-2] > closes[-3]
    )

    if short_pattern:

        return {
            "signal": "SHORT",
            "direction": "Вниз",
            "description": "Рост максимумов завершился падением"
        }

    if long_pattern:

        return {
            "signal": "LONG",
            "direction": "Вверх",
            "description": "Снижение минимумов завершилось ростом"
        }

    return no_signal()


# ============================================================
# EMA
# ============================================================

def calculate_ema(values, period):

    if not values:
        return 0.0

    k = 2 / (period + 1)

    result = float(values[0])

    for value in values[1:]:

        result = (
            float(value) * k
            + result * (1 - k)
        )

    return result


# ============================================================
# 2. EMA 20/50
# ============================================================

def strategy_ema(candles):

    if len(candles) < 50:
        return no_signal("Нужно минимум 50 свечей")

    closes = [
        float(x["close"])
        for x in candles
    ]

    ema20_now = calculate_ema(closes, 20)
    ema50_now = calculate_ema(closes, 50)

    ema20_prev = calculate_ema(closes[:-1], 20)
    ema50_prev = calculate_ema(closes[:-1], 50)

    if (
        ema20_prev <= ema50_prev
        and ema20_now > ema50_now
    ):

        return {
            "signal": "LONG",
            "direction": "Вверх",
            "description": "EMA20 пересекла EMA50 снизу вверх"
        }

    if (
        ema20_prev >= ema50_prev
        and ema20_now < ema50_now
    ):

        return {
            "signal": "SHORT",
            "direction": "Вниз",
            "description": "EMA20 пересекла EMA50 сверху вниз"
        }

    return no_signal("Пересечения EMA нет")


# ============================================================
# 3. RSI
# ============================================================

def strategy_rsi(candles):

    if len(candles) < 15:
        return no_signal("Нужно минимум 15 свечей")

    closes = [
        float(x["close"])
        for x in candles
    ]

    changes = [
        closes[i] - closes[i - 1]
        for i in range(1, len(closes))
    ]

    gains = [
        max(x, 0)
        for x in changes
    ]

    losses = [
        abs(min(x, 0))
        for x in changes
    ]

    avg_gain = sum(gains[-14:]) / 14
    avg_loss = sum(losses[-14:]) / 14

    if avg_loss == 0:

        rsi = 100.0

    else:

        rs = avg_gain / avg_loss

        rsi = 100 - (
            100 / (1 + rs)
        )

    if rsi < 30:

        return {
            "signal": "LONG",
            "direction": "Вверх",
            "description": f"RSI перепродан: {rsi:.1f}"
        }

    if rsi > 70:

        return {
            "signal": "SHORT",
            "direction": "Вниз",
            "description": f"RSI перекуплен: {rsi:.1f}"
        }

    return no_signal(
        f"RSI: {rsi:.1f}"
    )


# ============================================================
# 4. BOLLINGER
# ============================================================

def strategy_bollinger(candles):

    if len(candles) < 20:
        return no_signal("Нужно минимум 20 свечей")

    closes = [
        float(x["close"])
        for x in candles
    ]

    values = closes[-20:]

    middle = sum(values) / 20

    variance = sum(
        (x - middle) ** 2
        for x in values
    ) / 20

    std = variance ** 0.5

    upper = middle + 2 * std
    lower = middle - 2 * std

    price = closes[-1]

    if price <= lower:

        return {
            "signal": "LONG",
            "direction": "Вверх",
            "description": "Цена у нижней полосы Bollinger"
        }

    if price >= upper:

        return {
            "signal": "SHORT",
            "direction": "Вниз",
            "description": "Цена у верхней полосы Bollinger"
        }

    return no_signal(
        f"Цена {price:.2f}, диапазон {lower:.2f}-{upper:.2f}"
    )


# ============================================================
# 5. MACD
# ============================================================

def strategy_macd(candles):

    if len(candles) < 35:
        return no_signal("Нужно минимум 35 свечей")

    closes = [
        float(x["close"])
        for x in candles
    ]

    ema12 = []
    ema26 = []

    k12 = 2 / 13
    k26 = 2 / 27

    e12 = closes[0]
    e26 = closes[0]

    for price in closes:

        e12 = price * k12 + e12 * (1 - k12)
        e26 = price * k26 + e26 * (1 - k26)

        ema12.append(e12)
        ema26.append(e26)

    macd = [
        ema12[i] - ema26[i]
        for i in range(len(closes))
    ]

    signal_values = []

    k9 = 2 / 10

    signal_value = macd[0]

    for value in macd:

        signal_value = (
            value * k9
            + signal_value * (1 - k9)
        )

        signal_values.append(signal_value)

    if (
        macd[-2] <= signal_values[-2]
        and macd[-1] > signal_values[-1]
    ):

        return {
            "signal": "LONG",
            "direction": "Вверх",
            "description": "MACD пересёк сигнальную линию вверх"
        }

    if (
        macd[-2] >= signal_values[-2]
        and macd[-1] < signal_values[-1]
    ):

        return {
            "signal": "SHORT",
            "direction": "Вниз",
            "description": "MACD пересёк сигнальную линию вниз"
        }

    return no_signal("Пересечения MACD нет")


# ============================================================
# 6. DONCHIAN
# ============================================================

def strategy_donchian(candles):

    if len(candles) < 21:
        return no_signal("Нужно минимум 21 свеча")

    previous = candles[-21:-1]

    highest = max(
        float(x["high"])
        for x in previous
    )

    lowest = min(
        float(x["low"])
        for x in previous
    )

    close = float(candles[-1]["close"])

    if close > highest:

        return {
            "signal": "LONG",
            "direction": "Вверх",
            "description": "Пробой верхней границы Donchian"
        }

    if close < lowest:

        return {
            "signal": "SHORT",
            "direction": "Вниз",
            "description": "Пробой нижней границы Donchian"
        }

    return no_signal("Канал Donchian не пробит")


# ============================================================
# 7. MOMENTUM
# ============================================================

def strategy_momentum(candles):

    if len(candles) < 12:
        return no_signal("Нужно минимум 12 свечей")

    closes = [
        float(x["close"])
        for x in candles
    ]

    current = closes[-1]
    previous = closes[-11]

    momentum = (
        (current - previous)
        / previous
    ) * 100

    if momentum >= 1:

        return {
            "signal": "LONG",
            "direction": "Вверх",
            "description": f"Momentum +{momentum:.2f}%"
        }

    if momentum <= -1:

        return {
            "signal": "SHORT",
            "direction": "Вниз",
            "description": f"Momentum {momentum:.2f}%"
        }

    return no_signal(
        f"Momentum {momentum:.2f}%"
    )


# ============================================================
# 8. PRICE ACTION
# ============================================================

def strategy_price_action(candles):

    if len(candles) < 4:
        return no_signal("Нужно минимум 4 свечи")

    c1 = candles[-3]
    c2 = candles[-2]
    c3 = candles[-1]

    o1 = float(c1["open"])
    cl1 = float(c1["close"])

    o2 = float(c2["open"])
    cl2 = float(c2["close"])

    o3 = float(c3["open"])
    cl3 = float(c3["close"])

    if (
        cl1 > o1
        and cl2 > o2
        and cl3 > o3
        and cl3 > cl2
    ):

        return {
            "signal": "LONG",
            "direction": "Вверх",
            "description": "Три последовательные сильные бычьи свечи"
        }

    if (
        cl1 < o1
        and cl2 < o2
        and cl3 < o3
        and cl3 < cl2
    ):

        return {
            "signal": "SHORT",
            "direction": "Вниз",
            "description": "Три последовательные сильные медвежьи свечи"
        }

    return no_signal()


# ============================================================
# 9. VWAP
# ============================================================

def strategy_vwap(candles):

    if len(candles) < 20:
        return no_signal("Нужно минимум 20 свечей")

    data = candles[-20:]

    total_volume = 0
    total_price_volume = 0

    for candle in data:

        high = float(candle["high"])
        low = float(candle["low"])
        close = float(candle["close"])

        volume = float(
            candle.get("volume", 0)
        )

        typical = (
            high + low + close
        ) / 3

        total_price_volume += (
            typical * volume
        )

        total_volume += volume

    if total_volume <= 0:

        return no_signal(
            "Нет данных объёма"
        )

    vwap = (
        total_price_volume
        / total_volume
    )

    price = float(
        candles[-1]["close"]
    )

    previous_price = float(
        candles[-2]["close"]
    )

    if (
        previous_price <= vwap
        and price > vwap
    ):

        return {
            "signal": "LONG",
            "direction": "Вверх",
            "description": "Цена пересекла VWAP вверх"
        }

    if (
        previous_price >= vwap
        and price < vwap
    ):

        return {
            "signal": "SHORT",
            "direction": "Вниз",
            "description": "Цена пересекла VWAP вниз"
        }

    return no_signal(
        f"VWAP {vwap:.2f}"
    )


# ============================================================
# 10. EMA + RSI
# ============================================================

def strategy_ema_rsi(candles):

    if len(candles) < 50:
        return no_signal("Нужно минимум 50 свечей")

    closes = [
        float(x["close"])
        for x in candles
    ]

    ema20 = calculate_ema(closes, 20)
    ema50 = calculate_ema(closes, 50)

    changes = [
        closes[i] - closes[i - 1]
        for i in range(1, len(closes))
    ]

    gains = [
        max(x, 0)
        for x in changes
    ]

    losses = [
        abs(min(x, 0))
        for x in changes
    ]

    avg_gain = sum(gains[-14:]) / 14
    avg_loss = sum(losses[-14:]) / 14

    if avg_loss == 0:

        rsi = 100

    else:

        rs = avg_gain / avg_loss

        rsi = 100 - (
            100 / (1 + rs)
        )

    if ema20 > ema50 and rsi > 50:

        return {
            "signal": "LONG",
            "direction": "Вверх",
            "description": f"EMA20 > EMA50, RSI {rsi:.1f}"
        }

    if ema20 < ema50 and rsi < 50:

        return {
            "signal": "SHORT",
            "direction": "Вниз",
            "description": f"EMA20 < EMA50, RSI {rsi:.1f}"
        }

    return no_signal(
        f"EMA + RSI: {rsi:.1f}"
    )


# ============================================================
# 11. ADX
# ============================================================

def strategy_adx(candles):

    if len(candles) < 30:
        return no_signal("Нужно минимум 30 свечей")

    period = 14

    trs = []
    plus_dm = []
    minus_dm = []

    for i in range(1, len(candles)):

        current = candles[i]
        previous = candles[i - 1]

        high = float(current["high"])
        low = float(current["low"])

        prev_high = float(previous["high"])
        prev_low = float(previous["low"])
        prev_close = float(previous["close"])

        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close)
        )

        up_move = high - prev_high
        down_move = prev_low - low

        plus = (
            up_move
            if up_move > down_move
            and up_move > 0
            else 0
        )

        minus = (
            down_move
            if down_move > up_move
            and down_move > 0
            else 0
        )

        trs.append(tr)
        plus_dm.append(plus)
        minus_dm.append(minus)

    if len(trs) < period:
        return no_signal("Недостаточно данных ADX")

    atr = sum(
        trs[-period:]
    ) / period

    if atr == 0:
        return no_signal("ATR равен нулю")

    plus_di = (
        (sum(plus_dm[-period:]) / period)
        / atr
    ) * 100

    minus_di = (
        (sum(minus_dm[-period:]) / period)
        / atr
    ) * 100

    denominator = (
        plus_di + minus_di
    )

    if denominator == 0:
        return no_signal("ADX не рассчитан")

    dx = (
        abs(plus_di - minus_di)
        / denominator
    ) * 100

    if dx > 25 and plus_di > minus_di:

        return {
            "signal": "LONG",
            "direction": "Вверх",
            "description": f"ADX вверх: {dx:.1f}"
        }

    if dx > 25 and minus_di > plus_di:

        return {
            "signal": "SHORT",
            "direction": "Вниз",
            "description": f"ADX вниз: {dx:.1f}"
        }

    return no_signal(
        f"ADX {dx:.1f}"
    )


# ============================================================
# 12. EMA 20/50/200
# ============================================================

def strategy_ema_200(candles):

    if len(candles) < 200:
        return no_signal(
            "Нужно минимум 200 свечей"
        )

    closes = [
        float(x["close"])
        for x in candles
    ]

    ema20 = calculate_ema(closes, 20)
    ema50 = calculate_ema(closes, 50)
    ema200 = calculate_ema(closes, 200)

    price = closes[-1]

    if (
        price > ema200
        and ema20 > ema50
        and ema50 > ema200
    ):

        return {
            "signal": "LONG",
            "direction": "Вверх",
            "description": "Цена выше EMA200"
        }

    if (
        price < ema200
        and ema20 < ema50
        and ema50 < ema200
    ):

        return {
            "signal": "SHORT",
            "direction": "Вниз",
            "description": "Цена ниже EMA200"
        }

    return no_signal(
        "Тренд EMA200 не подтверждён"
    )


# ============================================================
# 13. ATR
# ============================================================

def strategy_atr(candles):

    if len(candles) < 20:
        return no_signal("Нужно минимум 20 свечей")

    period = 14

    trs = []

    for i in range(1, len(candles)):

        high = float(
            candles[i]["high"]
        )

        low = float(
            candles[i]["low"]
        )

        previous_close = float(
            candles[i - 1]["close"]
        )

        tr = max(
            high - low,
            abs(high - previous_close),
            abs(low - previous_close)
        )

        trs.append(tr)

    atr = sum(
        trs[-period:]
    ) / period

    current_close = float(
        candles[-1]["close"]
    )

    previous_close = float(
        candles[-2]["close"]
    )

    change = (
        current_close
        - previous_close
    )

    if change > atr:

        return {
            "signal": "LONG",
            "direction": "Вверх",
            "description": "Импульс вверх сильнее ATR"
        }

    if change < -atr:

        return {
            "signal": "SHORT",
            "direction": "Вниз",
            "description": "Импульс вниз сильнее ATR"
        }

    return no_signal(
        "Движение меньше ATR"
    )


# ============================================================
# 14. BREAKOUT
# ============================================================

def strategy_breakout(candles):

    if len(candles) < 21:
        return no_signal(
            "Нужно минимум 21 свеча"
        )

    previous = candles[-21:-1]

    highest = max(
        float(x["high"])
        for x in previous
    )

    lowest = min(
        float(x["low"])
        for x in previous
    )

    close = float(
        candles[-1]["close"]
    )

    if close > highest:

        return {
            "signal": "LONG",
            "direction": "Вверх",
            "description": "Пробой максимума диапазона"
        }

    if close < lowest:

        return {
            "signal": "SHORT",
            "direction": "Вниз",
            "description": "Пробой минимума диапазона"
        }

    return no_signal(
        "Пробоя диапазона нет"
    )


# ============================================================
# СПИСОК ВСЕХ СТРАТЕГИЙ
# ============================================================

STRATEGIES = {

    "MY": strategy_my,

    "EMA": strategy_ema,

    "RSI": strategy_rsi,

    "BOLLINGER": strategy_bollinger,

    "MACD": strategy_macd,

    "DONCHIAN": strategy_donchian,

    "MOMENTUM": strategy_momentum,

    "PRICE_ACTION": strategy_price_action,

    "VWAP": strategy_vwap,

    "EMA_RSI": strategy_ema_rsi,

    "ADX": strategy_adx,

    "EMA_200": strategy_ema_200,

    "ATR": strategy_atr,

    "BREAKOUT": strategy_breakout
}


# ============================================================
# ВАЖНО!
#
# СТАРЫЙ КОД ПРИЛОЖЕНИЯ МОЖЕТ ВЫЗЫВАТЬ:
#
# analyze_strategy(candles)
#
# Поэтому ЭТУ ФУНКЦИЮ НЕ УДАЛЯЕМ.
# ============================================================

def analyze_strategy(candles):

    return strategy_my(candles)


# ============================================================
# ВСПОМОГАТЕЛЬНО:
# ЦЕНА ИЗ СВЕЧИ
# ============================================================

def candle_price(candle):

    return float(candle["close"])


# ============================================================
# BACKTEST ОДНОЙ СТРАТЕГИИ
# ============================================================

def backtest_strategy(candles, strategy_function):

    if len(candles) < 30:

        return {
            "strategy": "",
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0,
            "profit": 0,
            "drawdown": 0
        }

    position = None
    entry_price = 0.0

    trades = []

    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0

    # --------------------------------------------------------
    # ВАЖНО:
    # стратегия получает ВСЮ историю до текущей свечи.
    #
    # Старый вариант candles[i-7:i+1] здесь НЕ используем.
    # --------------------------------------------------------

    for i in range(1, len(candles)):

        history = candles[:i + 1]

        try:

            result = strategy_function(history)

        except Exception:

            continue

        signal = result.get(
            "signal",
            "Нет сигналов"
        )

        price = candle_price(
            candles[i]
        )

        # ----------------------------------------------------
        # LONG
        # ----------------------------------------------------

        if position is None:

            if signal == "LONG":

                position = "LONG"
                entry_price = price

                continue

            if signal == "SHORT":

                position = "SHORT"
                entry_price = price

                continue

        # ----------------------------------------------------
        # LONG -> SHORT
        # ----------------------------------------------------

        elif position == "LONG":

            if signal == "SHORT":

                if entry_price > 0:

                    gross = (
                        (price - entry_price)
                        / entry_price
                    ) * POSITION_SIZE

                    commission = (
                        POSITION_SIZE
                        * COMMISSION_RATE
                        * 2
                    )

                    net = gross - commission

                    trades.append(net)

                    equity += net

                    peak = max(
                        peak,
                        equity
                    )

                    drawdown = peak - equity

                    max_drawdown = max(
                        max_drawdown,
                        drawdown
                    )

                position = "SHORT"
                entry_price = price

        # ----------------------------------------------------
        # SHORT -> LONG
        # ----------------------------------------------------

        elif position == "SHORT":

            if signal == "LONG":

                if entry_price > 0:

                    gross = (
                        (entry_price - price)
                        / entry_price
                    ) * POSITION_SIZE

                    commission = (
                        POSITION_SIZE
                        * COMMISSION_RATE
                        * 2
                    )

                    net = gross - commission

                    trades.append(net)

                    equity += net

                    peak = max(
                        peak,
                        equity
                    )

                    drawdown = peak - equity

                    max_drawdown = max(
                        max_drawdown,
                        drawdown
                    )

                position = "LONG"
                entry_price = price

    # --------------------------------------------------------
    # Если есть незакрытая позиция,
    # закрываем её по последней цене только для теста.
    # --------------------------------------------------------

    if position is not None and entry_price > 0:

        last_price = candle_price(
            candles[-1]
        )

        if position == "LONG":

            gross = (
                (last_price - entry_price)
                / entry_price
            ) * POSITION_SIZE

        else:

            gross = (
                (entry_price - last_price)
                / entry_price
            ) * POSITION_SIZE

        commission = (
            POSITION_SIZE
            * COMMISSION_RATE
            * 2
        )

        net = gross - commission

        trades.append(net)

        equity += net

    # --------------------------------------------------------
    # СТАТИСТИКА
    # --------------------------------------------------------

    wins = len([
        x for x in trades
        if x > 0
    ])

    losses = len([
        x for x in trades
        if x < 0
    ])

    total_trades = len(trades)

    if total_trades > 0:

        win_rate = (
            wins
            / total_trades
        ) * 100

    else:

        win_rate = 0

    gross_profit = sum(trades)

    # Налог только с положительного результата
    tax = 0

    if gross_profit > 0:

        tax = gross_profit * TAX_RATE

    final_profit = (
        gross_profit
        - tax
    )

    return {
        "strategy": "",
        "trades": total_trades,
        "wins": wins,
        "losses": losses,
        "win_rate": round(win_rate, 2),
        "profit": round(final_profit, 2),
        "drawdown": round(max_drawdown, 2)
    }


# ============================================================
# АВТОМАТИЧЕСКИЙ ВЫБОР ЛУЧШЕЙ СТРАТЕГИИ
# ============================================================

def select_best_strategy(candles):

    results = []

    for name, function in STRATEGIES.items():

        try:

            result = backtest_strategy(
                candles,
                function
            )

            result["strategy"] = name

            results.append(result)

        except Exception as e:

            logger.exception(
                "Ошибка стратегии %s: %s",
                name,
                e
            )

    if not results:

        return {
            "name": "MY",
            "function": strategy_my,
            "results": []
        }

    # --------------------------------------------------------
    # Сначала пытаемся выбирать только стратегии,
    # у которых достаточно сделок.
    # --------------------------------------------------------

    valid = [
        x for x in results
        if x["trades"] >= MIN_TRADES
    ]

    if not valid:

        valid = results

    # --------------------------------------------------------
    # Рейтинг:
    #
    # 1. прибыль
    # 2. процент прибыльных сделок
    # 3. меньше просадка
    #
    # Это НЕ гарантия будущей доходности.
    # --------------------------------------------------------

    valid.sort(
        key=lambda x: (
            x["profit"],
            x["win_rate"],
            -x["drawdown"]
        ),
        reverse=True
    )

    winner = valid[0]

    return {
        "name": winner["strategy"],
        "function": STRATEGIES[
            winner["strategy"]
        ],
        "results": results
    }


# ============================================================
# ТЕКУЩИЙ СИГНАЛ АВТОМАТИЧЕСКИ ВЫБРАННОЙ СТРАТЕГИИ
# ============================================================

def analyze_auto_strategy(candles):

    selected = select_best_strategy(
        candles
    )

    function = selected["function"]

    result = function(candles)

    result["strategy"] = selected["name"]

    return result


# ============================================================
# ПОИСК TOKEN / HTTP
# ============================================================

def api_post(endpoint, payload):

    if not TOKEN:

        raise RuntimeError(
            "Не найден T-Bank API TOKEN"
        )

    url = (
        API_BASE
        + endpoint
    )

    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json"
    }

    response = requests.post(
        url,
        json=payload,
        headers=headers,
        timeout=30,
        verify=False
    )

    if response.status_code != 200:

        raise RuntimeError(
            f"HTTP {response.status_code}: "
            f"{response.text[:500]}"
        )

    return response.json()


# ============================================================
# FIND INSTRUMENT
# ============================================================

def find_instrument(query):

    payload = {
        "query": query,
        "instrumentKind": "INSTRUMENT_TYPE_FUTURES",
        "apiTradeAvailableFlag": True
    }

    data = api_post(
        "/tinkoff.public.invest.api.contract.v1.InstrumentsService/FindInstrument",
        payload
    )

    return data


# ============================================================
# ПОЛУЧЕНИЕ ФЬЮЧЕРСОВ
# ============================================================

def get_all_futures():

    payload = {}

    data = api_post(
        "/tinkoff.public.invest.api.contract.v1.InstrumentsService/Futures",
        payload
    )

    return data.get(
        "instruments",
        []
    )


# ============================================================
# ПОИСК АКТИВНОГО ФЬЮЧЕРСА
# ============================================================

def find_active_future(queries):

    candidates = []

    # --------------------------------------------------------
    # Сначала FindInstrument
    # --------------------------------------------------------

    for query in queries:

        try:

            data = find_instrument(
                query
            )

            instruments = data.get(
                "instruments",
                []
            )

            candidates.extend(
                instruments
            )

        except Exception as e:

            logger.warning(
                "FindInstrument %s: %s",
                query,
                e
            )

    # --------------------------------------------------------
    # Если не нашли — получаем Futures
    # --------------------------------------------------------

    if not candidates:

        try:

            futures = get_all_futures()

            for item in futures:

                text = " ".join([
                    str(item.get("ticker", "")),
                    str(item.get("name", "")),
                    str(item.get("basicAsset", "")),
                    str(item.get("basicAssetPositionUid", ""))
                ]).lower()

                for query in queries:

                    if query.lower() in text:

                        candidates.append(item)

                        break

        except Exception as e:

            logger.warning(
                "Futures: %s",
                e
            )

    # --------------------------------------------------------
    # Убираем дубликаты
    # --------------------------------------------------------

    unique = {}

    for item in candidates:

        uid = (
            item.get("uid")
            or item.get("instrumentUid")
            or item.get("figi")
        )

        if uid:

            unique[uid] = item

    candidates = list(
        unique.values()
    )

    if not candidates:

        return None

    # --------------------------------------------------------
    # Пытаемся выбрать ближайший срок экспирации
    # --------------------------------------------------------

    now = datetime.now(
        timezone.utc
    )

    active = []

    for item in candidates:

        date_value = (
            item.get("lastTradeDate")
            or item.get("lastTradeDateTime")
        )

        if date_value:

            try:

                date_text = str(
                    date_value
                )

                if date_text.endswith("Z"):

                    date_text = date_text[:-1]

                expiry = datetime.fromisoformat(
                    date_text
                )

                if expiry.tzinfo is None:

                    expiry = expiry.replace(
                        tzinfo=timezone.utc
                    )

                if expiry > now:

                    active.append(
                        (
                            expiry,
                            item
                        )
                    )

            except Exception:

                pass

    if active:

        active.sort(
            key=lambda x: x[0]
        )

        return active[0][1]

    # Если дату не удалось определить
    return candidates[0]


# ============================================================
# ПОЛУЧЕНИЕ СВЕЧЕЙ
# ============================================================

def quotation_to_float(value):

    if isinstance(value, dict):

        units = float(
            value.get("units", 0)
        )

        nano = float(
            value.get("nano", 0)
        )

        return (
            units
            + nano / 1_000_000_000
        )

    return float(
        value or 0
    )


# ============================================================
# GET CANDLES
# ============================================================

def get_candles(uid):

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
        "interval": CANDLE_INTERVAL,
        "instrumentId": uid,
        "candleSourceType": "CANDLE_SOURCE_EXCHANGE",
        "limit": 2400
    }

    data = api_post(
        "/tinkoff.public.invest.api.contract.v1.MarketDataService/GetCandles",
        payload
    )

    result = []

    for candle in data.get(
        "candles",
        []
    ):

        result.append({
            "time": candle.get(
                "time"
            ),
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
            "volume": float(
                candle.get("volume", 0)
            )
        })

    result.sort(
        key=lambda x: str(
            x.get("time", "")
        )
    )

    return result


# ============================================================
# ДАННЫЕ ИНСТРУМЕНТОВ
# ============================================================

INSTRUMENTS = {

    "CR": {
        "name": "Юань",
        "queries": [
            "CR",
            "CNY",
            "юань",
            "CNY/RUB"
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
            "нефть"
        ]
    }
}


# ============================================================
# ПОЛУЧЕНИЕ СТАТУСА ОДНОГО ФЬЮЧЕРСА
# ============================================================

def get_future_status(code, config):

    result = {
        "code": code,
        "name": config["name"],
        "ticker": "-",
        "uid": "-",
        "candles": 0,
        "error": "",
        "signal": "Нет сигналов",
        "direction": "—",
        "description": "",
        "strategy": "-",
        "strategy_profit": 0,
        "strategy_trades": 0,
        "strategy_wins": 0,
        "strategy_losses": 0,
        "strategy_win_rate": 0,
        "strategy_drawdown": 0,
        "strategies": []
    }

    try:

        future = find_active_future(
            config["queries"]
        )

        if not future:

            result["error"] = (
                "Актуальный контракт не найден"
            )

            return result

        ticker = (
            future.get("ticker")
            or "-"
        )

        uid = (
            future.get("uid")
            or future.get("instrumentUid")
            or future.get("figi")
            or "-"
        )

        result["ticker"] = ticker
        result["uid"] = uid

        if uid == "-":

            result["error"] = (
                "UID фьючерса не найден"
            )

            return result

        candles = get_candles(
            uid
        )

        result["candles"] = len(
            candles
        )

        if len(candles) < 30:

            result["error"] = (
                "Недостаточно свечей"
            )

            return result

        # ----------------------------------------------------
        # АВТОВЫБОР
        # ----------------------------------------------------

        selected = select_best_strategy(
            candles
        )

        winner_name = selected["name"]

        winner_function = selected[
            "function"
        ]

        signal = winner_function(
            candles
        )

        # ----------------------------------------------------
        # Статистика победителя
        # ----------------------------------------------------

        winner_stats = None

        for item in selected["results"]:

            if item["strategy"] == winner_name:

                winner_stats = item

                break

        if winner_stats:

            result[
                "strategy_profit"
            ] = winner_stats["profit"]

            result[
                "strategy_trades"
            ] = winner_stats["trades"]

            result[
                "strategy_wins"
            ] = winner_stats["wins"]

            result[
                "strategy_losses"
            ] = winner_stats["losses"]

            result[
                "strategy_win_rate"
            ] = winner_stats["win_rate"]

            result[
                "strategy_drawdown"
            ] = winner_stats["drawdown"]

        result["strategy"] = winner_name

        result["signal"] = signal.get(
            "signal",
            "Нет сигналов"
        )

        result["direction"] = signal.get(
            "direction",
            "—"
        )

        result["description"] = signal.get(
            "description",
            ""
        )

        # ----------------------------------------------------
        # Рейтинг ВСЕХ стратегий
        # ----------------------------------------------------

        strategies = selected[
            "results"
        ]

        strategies.sort(
            key=lambda x: (
                x["profit"],
                x["win_rate"]
            ),
            reverse=True
        )

        result[
            "strategies"
        ] = strategies

        return result

    except Exception as e:

        logger.exception(
            "%s error",
            code
        )

        result["error"] = str(e)

        return result


# ============================================================
# СБОР ДАННЫХ
# ============================================================

def collect_data():

    global market_data

    new_data = {}

    for code, config in INSTRUMENTS.items():

        logger.info(
            "Анализ %s...",
            config["name"]
        )

        new_data[code] = get_future_status(
            code,
            config
        )

    with data_lock:

        market_data = new_data

    logger.info(
        "Анализ всех инструментов завершён"
    )


# ============================================================
# ФОНОВЫЙ ЦИКЛ
# ============================================================

def background_loop():

    while True:

        try:

            collect_data()

        except Exception as e:

            logger.exception(
                "Background error: %s",
                e
            )

        time.sleep(
            UPDATE_SECONDS
        )


# ============================================================
# API STATUS
# ============================================================

@app.route("/api/status")
def api_status():

    with data_lock:

        data = dict(
            market_data
        )

    return jsonify({
        "ok": True,
        "updated": datetime.now(
            timezone.utc
        ).isoformat(),
        "instruments": data
    })


# ============================================================
# API STRATEGIES
# ============================================================

@app.route("/api/strategies")
def api_strategies():

    result = {}

    with data_lock:

        for code, item in market_data.items():

            result[code] = {
                "name": item.get(
                    "name"
                ),
                "selected_strategy": item.get(
                    "strategy"
                ),
                "profit": item.get(
                    "strategy_profit"
                ),
                "trades": item.get(
                    "strategy_trades"
                ),
                "win_rate": item.get(
                    "strategy_win_rate"
                ),
                "drawdown": item.get(
                    "strategy_drawdown"
                ),
                "all_strategies": item.get(
                    "strategies",
                    []
                )
            }

    return jsonify(result)


# ============================================================
# ГЛАВНАЯ СТРАНИЦА
# ============================================================

HTML = """

<!DOCTYPE html>

<html lang="ru">

<head>

<meta charset="UTF-8">

<meta name="viewport"
content="width=device-width, initial-scale=1.0">

<title>Markus Trade</title>

<style>

body {
    margin: 0;
    background: #080808;
    color: #ffffff;
    font-family: Arial, sans-serif;
}

header {
    padding: 22px;
    text-align: center;
    border-bottom: 1px solid #333;
}

h1 {
    margin: 0;
}

.subtitle {
    color: #999;
    margin-top: 8px;
}

.container {
    max-width: 1100px;
    margin: auto;
    padding: 20px;
}

.card {
    background: #111;
    border: 1px solid #333;
    border-radius: 14px;
    padding: 20px;
    margin-bottom: 20px;
}

.title {
    font-size: 24px;
    font-weight: bold;
}

.strategy {
    margin-top: 10px;
    color: #d7b56d;
    font-size: 18px;
}

.signal {
    font-size: 30px;
    font-weight: bold;
    margin-top: 15px;
}

.long {
    color: #27d17f;
}

.short {
    color: #ff5252;
}

.none {
    color: #999;
}

.stats {
    display: grid;
    grid-template-columns:
        repeat(auto-fit, minmax(130px, 1fr));

    gap: 10px;

    margin-top: 20px;
}

.stat {
    background: #181818;
    border-radius: 10px;
    padding: 12px;
}

.label {
    color: #888;
    font-size: 12px;
}

.value {
    margin-top: 5px;
    font-size: 18px;
    font-weight: bold;
}

.error {
    color: #ff5252;
    margin-top: 15px;
}

button {
    background: #d7b56d;
    border: 0;
    padding: 12px 20px;
    border-radius: 8px;
    cursor: pointer;
}

table {
    width: 100%;
    border-collapse: collapse;
    margin-top: 15px;
}

th,
td {
    padding: 8px;
    border-bottom: 1px solid #333;
    text-align: left;
}

.gold {
    color: #d7b56d;
}

</style>

</head>

<body>

<header>

<h1>MARKUS TRADE</h1>

<div class="subtitle">
Автоматический выбор стратегии
</div>

</header>

<div class="container">

<div id="app">
Загрузка...
</div>

</div>


<script>

async function loadData() {

    try {

        const response =
            await fetch('/api/status');

        const data =
            await response.json();

        const container =
            document.getElementById('app');

        let html = '';

        const instruments =
            data.instruments || {};

        for (
            const code in instruments
        ) {

            const item =
                instruments[code];

            let signalClass = 'none';

            if (
                item.signal === 'LONG'
            ) {

                signalClass = 'long';

            } else if (
                item.signal === 'SHORT'
            ) {

                signalClass = 'short';
            }

            html += `

            <div class="card">

                <div class="title">
                    ${item.name}
                </div>

                <div>
                    Тикер:
                    <span class="gold">
                        ${item.ticker}
                    </span>
                </div>

                <div>
                    Свечей:
                    ${item.candles}
                </div>

                <div class="strategy">

                    Автоматически выбрана:
                    ${item.strategy}

                </div>

                <div class="signal ${signalClass}">

                    ${item.signal}

                </div>

                <div>
                    ${item.description}
                </div>

                <div class="stats">

                    <div class="stat">

                        <div class="label">
                            Результат
                        </div>

                        <div class="value">
                            ${item.strategy_profit} ₽
                        </div>

                    </div>

                    <div class="stat">

                        <div class="label">
                            Сделок
                        </div>

                        <div class="value">
                            ${item.strategy_trades}
                        </div>

                    </div>

                    <div class="stat">

                        <div class="label">
                            Прибыльных
                        </div>

                        <div class="value">
                            ${item.strategy_wins}
                        </div>

                    </div>

                    <div class="stat">

                        <div class="label">
                            Убыточных
                        </div>

                        <div class="value">
                            ${item.strategy_losses}
                        </div>

                    </div>

                    <div class="stat">

                        <div class="label">
                            Win Rate
                        </div>

                        <div class="value">
                            ${item.strategy_win_rate}%
                        </div>

                    </div>

                    <div class="stat">

                        <div class="label">
                            Просадка
                        </div>

                        <div class="value">
                            ${item.strategy_drawdown} ₽
                        </div>

                    </div>

                </div>

                ${
                    item.error
                    ? `<div class="error">
                        ${item.error}
                       </div>`
                    : ''
                }

                <details>

                    <summary>
                        Все стратегии
                    </summary>

                    <table>

                        <tr>
                            <th>Стратегия</th>
                            <th>Прибыль</th>
                            <th>Сделки</th>
                            <th>Win Rate</th>
                            <th>Просадка</th>
                        </tr>

                        ${
                            (item.strategies || [])
                            .map(s => `

                                <tr>

                                    <td>
                                        ${s.strategy}
                                    </td>

                                    <td>
                                        ${s.profit} ₽
                                    </td>

                                    <td>
                                        ${s.trades}
                                    </td>

                                    <td>
                                        ${s.win_rate}%
                                    </td>

                                    <td>
                                        ${s.drawdown} ₽
                                    </td>

                                </tr>

                            `)
                            .join('')
                        }

                    </table>

                </details>

            </div>

            `;
        }

        container.innerHTML = html;

    } catch (error) {

        document.getElementById('app').innerHTML =
            '<div class="error">' +
            'Ошибка загрузки: ' +
            error +
            '</div>';
    }
}


loadData();

setInterval(
    loadData,
    30000
);

</script>

</body>

</html>

"""


# ============================================================
# HOME
# ============================================================

@app.route("/")
def home():

    return render_template_string(
        HTML
    )


# ============================================================
# START
# ============================================================

def start_background():

    thread = threading.Thread(
        target=background_loop,
        daemon=True
    )

    thread.start()


if __name__ == "__main__":

    start_background()

    app.run(
        host="0.0.0.0",
        port=int(
            os.getenv(
                "PORT",
                "5000"
            )
        ),
        debug=False
    )
