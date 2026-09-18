import os
import requests
import pandas as pd
from datetime import datetime, timedelta, timezone

# ============================================================
# НАСТРОЙКИ
# ============================================================

T_BANK_TOKEN = os.getenv("T_BANK_TOKEN")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

API_URL = "https://invest-public-api.tbank.ru/rest"

# Что исследуем
INTERVAL = "CANDLE_INTERVAL_15_MIN"

# Период истории
DAYS_BACK = 180

# Минимальное сильное движение
STRONG_MOVE_PCT = 0.8

# Сколько последовательных экстремумов требуется
REQUIRED_POINTS = 3

# Цель и стоп
TAKE_PROFIT_PCT = 1.0
STOP_LOSS_PCT = 0.5

# Размер окна для поиска локальных экстремумов
PIVOT_WINDOW = 2


# ============================================================
# T-BANK API
# ============================================================

def headers():
    if not T_BANK_TOKEN:
        raise RuntimeError(
            "Не задан T_BANK_TOKEN. "
            "Добавь токен в переменную окружения."
        )

    return {
        "Authorization": f"Bearer {T_BANK_TOKEN}",
        "Content-Type": "application/json"
    }


def get_futures():
    """
    Получаем список доступных фьючерсов.
    """

    url = (
        API_URL
        + "/tinkoff.public.invest.api.contract.v1."
          "InstrumentsService/Futures"
    )

    payload = {
        "instrumentStatus": "INSTRUMENT_STATUS_BASE"
    }

    response = requests.post(
        url,
        json=payload,
        headers=headers(),
        timeout=20
    )

    response.raise_for_status()

    return response.json().get("instruments", [])


def find_cny_future():
    """
    Ищем фьючерс на юань.

    Не зашиваем FIGI вручную:
    выбираем актуальный контракт по списку
    фьючерсов и дате экспирации.
    """

    futures = get_futures()

    candidates = []

    now = datetime.now(timezone.utc)

    for future in futures:

        ticker = future.get("ticker", "")

        # В зависимости от текущего обозначения API
        # ищем CNY/CR.
        if not (
            ticker.upper().startswith("CR")
            or "CNY" in ticker.upper()
        ):
            continue

        expiration = future.get("expirationDate")

        if not expiration:
            continue

        try:
            expiration_dt = datetime.fromisoformat(
                expiration.replace("Z", "+00:00")
            )
        except ValueError:
            continue

        if expiration_dt <= now:
            continue

        candidates.append(
            (expiration_dt, future)
        )

    if not candidates:
        raise RuntimeError(
            "Актуальный фьючерс на юань не найден."
        )

    candidates.sort(key=lambda x: x[0])

    return candidates[0][1]


# ============================================================
# ИСТОРИЧЕСКИЕ СВЕЧИ
# ============================================================

def get_candles(instrument_id, start, end):

    url = (
        API_URL
        + "/tinkoff.public.invest.api.contract.v1."
          "MarketDataService/GetCandles"
    )

    payload = {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "interval": INTERVAL,
        "instrumentId": instrument_id
    }

    response = requests.post(
        url,
        json=payload,
        headers=headers(),
        timeout=30
    )

    response.raise_for_status()

    return response.json().get("candles", [])


def quotation_to_float(value):
    """
    T-Bank возвращает цену в формате:

    {
        "units": "...",
        "nano": "..."
    }
    """

    if not value:
        return None

    units = int(value.get("units", 0))
    nano = int(value.get("nano", 0))

    return units + nano / 1_000_000_000


def load_history(instrument_id):

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=DAYS_BACK)

    candles = get_candles(
        instrument_id,
        start,
        end
    )

    rows = []

    for candle in candles:

        open_price = quotation_to_float(
            candle.get("open")
        )

        high_price = quotation_to_float(
            candle.get("high")
        )

        low_price = quotation_to_float(
            candle.get("low")
        )

        close_price = quotation_to_float(
            candle.get("close")
        )

        if None in (
            open_price,
            high_price,
            low_price,
            close_price
        ):
            continue

        rows.append({
            "time": candle.get("time"),
            "open": open_price,
            "high": high_price,
            "low": low_price,
            "close": close_price
        })

    df = pd.DataFrame(rows)

    if df.empty:
        raise RuntimeError(
            "Исторические свечи не получены."
        )

    df["time"] = pd.to_datetime(df["time"])

    df = df.sort_values("time")
    df = df.drop_duplicates("time")

    df = df.reset_index(drop=True)

    return df


# ============================================================
# ПОИСК ЛОКАЛЬНЫХ ЭКСТРЕМУМОВ
# ============================================================

def find_pivots(df):

    highs = []
    lows = []

    w = PIVOT_WINDOW

    for i in range(w, len(df) - w):

        current_high = df.loc[i, "high"]
        current_low = df.loc[i, "low"]

        left_highs = df.loc[
            i - w:i + w,
            "high"
        ]

        left_lows = df.loc[
            i - w:i + w,
            "low"
        ]

        if current_high == left_highs.max():
            highs.append(i)

        if current_low == left_lows.min():
            lows.append(i)

    return highs, lows


# ============================================================
# ПРОВЕРКА SHORT-ПАТТЕРНА
# ============================================================

def find_short_signals(df, highs, lows):

    signals = []

    for h in range(len(highs)):

        first_high_index = highs[h]

        # Ищем сильное падение после первоначального максимума
        following_lows = [
            x for x in lows
            if x > first_high_index
        ]

        if not following_lows:
            continue

        first_low_index = following_lows[0]

        first_high = df.loc[
            first_high_index, "high"
        ]

        first_low = df.loc[
            first_low_index, "low"
        ]

        fall_pct = (
            (first_high - first_low)
            / first_high
            * 100
        )

        if fall_pct < STRONG_MOVE_PCT:
            continue

        # Теперь ищем последовательные максимумы
        sequence = []

        for candidate in highs:

            if candidate <= first_low_index:
                continue

            price = df.loc[
                candidate,
                "high"
            ]

            if not sequence:
                sequence.append(candidate)
                continue

            previous_price = df.loc[
                sequence[-1],
                "high"
            ]

            if price > previous_price:

                sequence.append(candidate)

            else:

                # Последовательность сломалась
                sequence = [candidate]

            if len(sequence) >= REQUIRED_POINTS:

                signal_index = candidate

                signals.append({
                    "type": "SHORT",
                    "index": signal_index,
                    "time": df.loc[
                        signal_index,
                        "time"
                    ],
                    "entry": df.loc[
                        signal_index,
                        "close"
                    ],
                    "points": len(sequence)
                })

                break

    return signals


# ============================================================
# ПРОВЕРКА LONG-ПАТТЕРНА
# ============================================================

def find_long_signals(df, highs, lows):

    signals = []

    for l in range(len(lows)):

        first_low_index = lows[l]

        following_highs = [
            x for x in highs
            if x > first_low_index
        ]

        if not following_highs:
            continue

        first_high_index = following_highs[0]

        first_low = df.loc[
            first_low_index,
            "low"
        ]

        first_high = df.loc[
            first_high_index,
            "high"
        ]

        rise_pct = (
            (first_high - first_low)
            / first_low
            * 100
        )

        if rise_pct < STRONG_MOVE_PCT:
            continue

        sequence = []

        for candidate in lows:

            if candidate <= first_high_index:
                continue

            price = df.loc[
                candidate,
                "low"
            ]

            if not sequence:

                sequence.append(candidate)
                continue

            previous_price = df.loc[
                sequence[-1],
                "low"
            ]

            if price < previous_price:

                sequence.append(candidate)

            else:

                sequence = [candidate]

            if len(sequence) >= REQUIRED_POINTS:

                signal_index = candidate

                signals.append({
                    "type": "LONG",
                    "index": signal_index,
                    "time": df.loc[
                        signal_index,
                        "time"
                    ],
                    "entry": df.loc[
                        signal_index,
                        "close"
                    ],
                    "points": len(sequence)
                })

                break

    return signals


# ============================================================
# БЭКТЕСТ
# ============================================================

def test_trade(df, signal):

    entry_index = signal["index"]
    entry = signal["entry"]

    if signal["type"] == "SHORT":

        take_profit = (
            entry
            * (1 - TAKE_PROFIT_PCT / 100)
        )

        stop_loss = (
            entry
            * (1 + STOP_LOSS_PCT / 100)
        )

    else:

        take_profit = (
            entry
            * (1 + TAKE_PROFIT_PCT / 100)
        )

        stop_loss = (
            entry
            * (1 - STOP_LOSS_PCT / 100)
        )

    for i in range(
        entry_index + 1,
        len(df)
    ):

        high = df.loc[i, "high"]
        low = df.loc[i, "low"]

        if signal["type"] == "SHORT":

            # В одной свече могли быть задеты
            # и TP, и SL. Консервативно считаем
            # сначала стоп.
            if high >= stop_loss:
                return "LOSS", i

            if low <= take_profit:
                return "WIN", i

        else:

            if low <= stop_loss:
                return "LOSS", i

            if high >= take_profit:
                return "WIN", i

    return "OPEN", None


def run_backtest(df, signals):

    results = []

    for signal in signals:

        result, exit_index = test_trade(
            df,
            signal
        )

        results.append({
            **signal,
            "result": result,
            "exit_index": exit_index
        })

    return pd.DataFrame(results)


# ============================================================
# СТАТИСТИКА
# ============================================================

def print_statistics(results):

    if results.empty:

        print("\nСигналов не найдено.")
        return

    completed = results[
        results["result"].isin(
            ["WIN", "LOSS"]
        )
    ]

    wins = len(
        completed[
            completed["result"] == "WIN"
        ]
    )

    losses = len(
        completed[
            completed["result"] == "LOSS"
        ]
    )

    total = wins + losses

    print("\n==============================")
    print("       MARKUS BACKTEST")
    print("==============================")

    print(
        f"Всего сигналов: {len(results)}"
    )

    print(
        f"Завершённых сделок: {total}"
    )

    print(
        f"WIN: {wins}"
    )

    print(
        f"LOSS: {losses}"
    )

    if total > 0:

        winrate = (
            wins / total * 100
        )

        print(
            f"\nПРОХОДИМОСТЬ: {winrate:.2f}%"
        )

    print(
        "\nLONG:"
    )

    long_results = completed[
        completed["type"] == "LONG"
    ]

    if not long_results.empty:

        long_winrate = (
            (
                long_results["result"] == "WIN"
            ).mean()
            * 100
        )

        print(
            f"{long_winrate:.2f}%"
        )

    print(
        "\nSHORT:"
    )

    short_results = completed[
        completed["type"] == "SHORT"
    ]

    if not short_results.empty:

        short_winrate = (
            (
                short_results["result"] == "WIN"
            ).mean()
            * 100
        )

        print(
            f"{short_winrate:.2f}%"
        )


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):

    if not TELEGRAM_TOKEN:
        print(
            "\nTelegram token не установлен."
        )
        return

    if not TELEGRAM_CHAT_ID:
        print(
            "\nTelegram chat ID не установлен."
        )
        return

    url = (
        "https://api.telegram.org/bot"
        f"{TELEGRAM_TOKEN}/sendMessage"
    )

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message
    }

    try:

        response = requests.post(
            url,
            json=payload,
            timeout=15
        )

        response.raise_for_status()

    except Exception as e:

        print(
            f"Ошибка Telegram: {e}"
        )


# ============================================================
# MAIN
# ============================================================

def main():

    print(
        "\n🤖 MARKUS AI"
    )

    print(
        "Поиск фьючерса на юань..."
    )

    future = find_cny_future()

    ticker = future.get(
        "ticker",
        "UNKNOWN"
    )

    figi = future.get(
        "figi"
    )

    uid = future.get(
        "uid"
    )

    instrument_id = uid or figi

    if not instrument_id:

        raise RuntimeError(
            "У фьючерса нет UID/FIGI."
        )

    print(
        f"Фьючерс: {ticker}"
    )

    print(
        f"ID: {instrument_id}"
    )

    print(
        "\nЗагружаю историю..."
    )

    df = load_history(
        instrument_id
    )

    print(
        f"Получено свечей: {len(df)}"
    )

    print(
        "\nИщу экстремумы..."
    )

    highs, lows = find_pivots(df)

    print(
        f"Максимумов: {len(highs)}"
    )

    print(
        f"Минимумов: {len(lows)}"
    )

    print(
        "\nИщу SHORT..."
    )

    short_signals = find_short_signals(
        df,
        highs,
        lows
    )

    print(
        f"SHORT сигналов: "
        f"{len(short_signals)}"
    )

    print(
        "\nИщу LONG..."
    )

    long_signals = find_long_signals(
        df,
        highs,
        lows
    )

    print(
        f"LONG сигналов: "
        f"{len(long_signals)}"
    )

    signals = (
        short_signals
        + long_signals
    )

    signals.sort(
        key=lambda x: x["index"]
    )

    results = run_backtest(
        df,
        signals
    )

    print_statistics(
        results
    )

    # Сохраняем полный результат
    results.to_csv(
        "markus_backtest_results.csv",
        index=False
    )

    print(
        "\nРезультаты сохранены:"
        " markus_backtest_results.csv"
    )

    if not results.empty:

        completed = results[
            results["result"].isin(
                ["WIN", "LOSS"]
            )
        ]

        if not completed.empty:

            wins = (
                completed["result"]
                == "WIN"
            ).sum()

            total = len(completed)

            winrate = (
                wins / total * 100
            )

            message = (
                "🤖 MARKUS AI\n\n"
                f"Фьючерс: {ticker}\n"
                f"ТФ: {INTERVAL}\n"
                f"Сигналов: {len(results)}\n"
                f"Завершено: {total}\n"
                f"WIN: {wins}\n"
                f"Проходимость: "
                f"{winrate:.2f}%\n\n"
                "⚠️ Это исторический "
                "бэктест, не торговый сигнал."
            )

            send_telegram(
                message
            )


if __name__ == "__main__":
    main()
