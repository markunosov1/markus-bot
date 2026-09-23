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
# MARKUS TRADE — НАСТРОЙКИ РЫНКА АКЦИЙ
# ============================================================
APP_NAME = "Markus Trade"
API_BASE = "https://tbank.ru"

FIND_INSTRUMENT_URL = (
   API_BASE +
   "/tinkoff.public.invest.api.contract.v1.InstrumentsService/FindInstrument"
)

# Заменяем FUTURES_URL на SHARES_URL
SHARES_URL = (
   API_BASE +
   "/tinkoff.public.invest.api.contract.v1.InstrumentsService/Shares"
)

CANDLES_URL = (
   API_BASE +
   "/tinkoff.public.invest.api.contract.v1.MarketDataService/GetCandles"
)

REQUEST_TIMEOUT = 30

# Таймфрейм: 4 часа
CANDLE_INTERVAL = "CANDLE_INTERVAL_4_HOUR"

# Сколько часов свечей загружать (60 дней — оптимально для 4-часовиков)
HISTORY_HOURS = 24 * 60

# Как часто обновлять данные (10 минут)
UPDATE_SECONDS = 600

# ============================================================
# ТОРГОВЫЕ НАСТРОЙКИ
# ============================================================
POSITION_SIZE_RUBLES = 100000.0

# Реалистичные комиссии для спот-рынка акций Т-Банка
BUY_COMMISSION_PERCENT = 0.03
SELL_COMMISSION_PERCENT = 0.03
TAX_PERCENT = 13.0

HISTORY_FILE = "trade_history.json"


 
# ============================================================
# ОТКЛЮЧАЕМ ЛИШНИЕ SSL WARNING
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
# ПОЛУЧЕНИЕ ВСЕХ АКЦИИ
# ============================================================
 
def get_all_shares():
   payload = {
       "instrumentStatus": "INSTRUMENT_STATUS_BASE"
   }
   try:
       data = api_post(SHARES_URL, payload)
   except Exception:
       payload = {
           "instrumentStatus": "INSTRUMENT_STATUS_ALL"
       }
       data = api_post(SHARES_URL, payload)

   shares = data.get("instruments", [])
   if not isinstance(shares, list):
       shares = []
   return shares

 
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
# ВАЛИДАЦИЯ ИНСТРУМЕНТА (АКЦИИ)
# ============================================================
 
def matches_future(future, prefix):
    """
    Универсальная проверка для акций. Напрямую сравнивает тикер 
    инструмента с искомым кодом (например, SBER, GAZP, LKOH).
    """
    ticker = get_string(future, "ticker").upper()
    prefix = prefix.upper()
    
    # Картирование старых фьючерсных префиксов на акции (для обратной совместимости)
    legacy_mapping = {
        "CR": "SBER",  # Вместо Юаня берем Сбербанк
        "GD": "GAZP",  # Вместо Золота берем Газпром
        "BR": "LKOH",  # Вместо Нефти берем Лукойл
        "NG": "NVTK",  # Вместо Газа берем Новатэк
        "MX": "YNDX",  # Вместо Индекса берем Яндекс
        "SV": "ROSN"   # Вместо Серебра берем Роснефть
    }
    
    if prefix in legacy_mapping:
        prefix = legacy_mapping[prefix]
        
    return prefix in ticker


# ============================================================
# ПОИСК АКТИВНОЙ АКЦИИ ПО ТИКЕРУ (НОВЫЙ ДВИЖОК)
# ============================================================
 
# ============================================================
# ИСПРАВЛЕННЫЙ ПОИСК АКЦИИ В КАТАЛОГЕ Т-БАНКА
# ============================================================
 
def find_active_share(ticker_name):
   try:
       # Используем более мягкий поисковый запрос, который Т-Банк API
       # гарантированно сопоставит с тикером на Московской бирже
       payload = {
           "query": str(ticker_name).upper(),
           "instrumentKind": "INSTRUMENT_TYPE_SHARE",
           "apiTradeAvailableFlag": False  # Убираем жесткий флаг доступности к торгам на случай выходных/вечерней сессии
       }
       
       data = api_post(FIND_INSTRUMENT_URL, payload)
       instruments = data.get("instruments", [])
       
       # Если через FindInstrument ничего не нашлось, делаем резервный запрос
       if not instruments:
           payload["instrumentKind"] = "INSTRUMENT_KIND_SHARE" # Проверяем альтернативный тип Enum
           data = api_post(FIND_INSTRUMENT_URL, payload)
           instruments = data.get("instruments", [])

       for item in instruments:
           ticker = get_string(item, "ticker").upper()
           name = get_string(item, "name").upper()
           
           # Проверяем строгое совпадение по тикеру
           if ticker == ticker_name.upper():
               instrument_uid = item.get("instrumentUid") or item.get("uid")
               return {
                   "ticker": get_string(item, "ticker"),
                   "name": get_string(item, "name"),
                   "uid": instrument_uid,
                   "instrument_uid": instrument_uid,
                   "class_code": get_string(item, "classCode"),
                   "basic_asset": ""
               }
               
       # Резервный шаг: если точное совпадение не сработало, берем первый инструмент, где тикер содержится внутри
       if instruments:
           item = instruments[0]
           instrument_uid = item.get("instrumentUid") or item.get("uid")
           return {
               "ticker": get_string(item, "ticker"),
               "name": get_string(item, "name"),
               "uid": instrument_uid,
               "instrument_uid": instrument_uid,
               "class_code": get_string(item, "classCode"),
               "basic_asset": ""
           }
           
   except Exception as e:
       log.error("Ошибка поиска акции %s: %s", ticker_name, e)
       
   return None


# ============================================================
# ЗАЩИТНЫЙ МОСТ ДЛЯ СТАРОГО КОДА (Убирает ошибку NameError)
# ============================================================

def find_active_future(prefix):
    """
    Перенаправляет вызовы старой функции find_active_future на новый движок акций.
    Если где-то в коде остался вызов find_active_future('CR'), он автоматически найдет Сбербанк.
    """
    prefix_upper = str(prefix).upper()
    
    # Карта переключения старых префиксов на новые тикеры акций
    legacy_assets = {
        "CR": "SBER",
        "GD": "GAZP",
        "BR": "LKOH",
        "NG": "NVTK",
        "MX": "YNDX",
        "SV": "ROSN"
    }
    
    target_ticker = legacy_assets.get(prefix_upper, prefix_upper)
    return find_active_share(target_ticker)




 
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
           "description": "Недостаточно свечей"
       }

   last = candles[-8:]
   highs = [x["high"] for x in last]
   lows = [x["low"] for x in last]
   closes = [x["close"] for x in last]
   entry_price = closes[-1]

   # Паттерн SHORT (Импульсное истощение покупателей)
   short_pattern = (
       highs[3] > highs[2] and highs[4] > highs[3] and highs[5] > highs[4] and
       closes[-1] < closes[-2] and closes[-2] < closes[-3]
   )

   # Паттерн LONG (Выкуп панических распродаж)
   long_pattern = (
       lows[3] < lows[2] and lows[4] < lows[3] and lows[5] < lows[4] and
       closes[-1] > closes[-2] and closes[-2] > closes[-3]
   )

   if short_pattern:
       stop_loss_price = entry_price * 1.01  # Убыток при росте цены на 1%
       return {
           "signal": "SHORT",
           "direction": "Вниз",
           "description": "Сформирован SHORT-сигнал",
           "entry_price": entry_price,
           "stop_loss": round(stop_loss_price, 4)
       }

   if long_pattern:
       stop_loss_price = entry_price * 0.99  # Убыток при падении цены на 1%
       return {
           "signal": "LONG",
           "direction": "Вверх",
           "description": "Сформирован LONG-сигнал",
           "entry_price": entry_price,
           "stop_loss": round(stop_loss_price, 4)
       }

   return {
       "signal": "Нет сигналов",
       "direction": "—",
       "description": "Сигнал не сформирован"
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
# РАСЧЕТ СДЕЛКИ
# ============================================================
 
def calculate_trade_result(
   direction,
   entry_price,
   exit_price
):
 
   if entry_price <= 0:
       return None
 
   # --------------------------------------------------------
   # Доходность
   # --------------------------------------------------------
 
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
 
   # --------------------------------------------------------
   # Результат до расходов
   # --------------------------------------------------------
 
   gross_result = (
       POSITION_SIZE_RUBLES *
       price_change_percent /
       100.0
   )
 
   # --------------------------------------------------------
   # Комиссия при входе
   # --------------------------------------------------------
 
   buy_commission = calculate_commission(
       POSITION_SIZE_RUBLES,
       BUY_COMMISSION_PERCENT
   )
 
   # --------------------------------------------------------
   # Комиссия при выходе
   # --------------------------------------------------------
 
   exit_amount = (
       POSITION_SIZE_RUBLES
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
 
   # --------------------------------------------------------
   # НАЛОГ
   #
   # По заданному тобой правилу:
   # 13% только если сделка прибыльная.
   # --------------------------------------------------------
 
   if gross_result > 0:
 
       tax = (
           gross_result *
           TAX_PERCENT /
           100.0
       )
 
   else:
 
       tax = 0.0
 
   # --------------------------------------------------------
   # ЧИСТЫЙ РЕЗУЛЬТАТ
   # --------------------------------------------------------
 
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
#
# ВХОД:
# LONG / SHORT
#
# ВЫХОД:
# противоположный сигнал
# ============================================================
 
def build_strategy_history(candles, instrument, title):
   if len(candles) < 8:
       return [], None

   trades = []
   current_position = None

   for i in range(7, len(candles)):
       window = candles[i - 7:i + 1]
       analysis = analyze_strategy(window)
       signal = analysis["signal"]
       candle = candles[i]
       price = candle["close"]
       candle_time = candle["time"]
       
       high_price = candle.get("high", price)
       low_price = candle.get("low", price)

       if current_position is None:
           if signal in ("LONG", "SHORT"):
               current_position = {
                   "instrument": instrument,
                   "title": title,
                   "direction": signal,
                   "entry_price": price,
                   "entry_time": candle_time,
                   "stop_loss": analysis.get("stop_loss")
               }
           continue

       # Проверка срабатывания Стоп-Лосса
       is_sl_triggered = False
       exit_price_sl = price

       if current_position["stop_loss"] is not None:
           if current_position["direction"] == "LONG":
               if low_price <= current_position["stop_loss"]:
                   is_sl_triggered = True
                   exit_price_sl = min(current_position["entry_price"], current_position["stop_loss"])
           elif current_position["direction"] == "SHORT":
               if high_price >= current_position["stop_loss"]:
                   is_sl_triggered = True
                   exit_price_sl = max(current_position["entry_price"], current_position["stop_loss"])

       if is_sl_triggered:
           result = calculate_trade_result(
               current_position["direction"],
               current_position["entry_price"],
               exit_price_sl
           )
           if result is not None:
               trades.append({
                   "id": len(trades) + 1,
                   "instrument": instrument,
                   "title": title,
                   "direction": current_position["direction"],
                   "entry_time": current_position["entry_time"],
                   "exit_time": candle_time,
                   "entry_price": round(current_position["entry_price"], 8),
                   "exit_price": round(exit_price_sl, 8),
                   "exit_signal": "STOP_LOSS",
                   **result
               })
           current_position = None
           continue

       if signal == current_position["direction"]:
           continue

       opposite_signal = (
           current_position["direction"] == "LONG" and signal == "SHORT"
       ) or (
           current_position["direction"] == "SHORT" and signal == "LONG"
       )

       if opposite_signal:
           result = calculate_trade_result(
               current_position["direction"],
               current_position["entry_price"],
               price
           )
           if result is None:
               current_position = None
               continue

           trades.append({
               "id": len(trades) + 1,
               "instrument": instrument,
               "title": title,
               "direction": current_position["direction"],
               "entry_time": current_position["entry_time"],
               "exit_time": candle_time,
               "entry_price": round(current_position["entry_price"], 8),
               "exit_price": round(price, 8),
               "exit_signal": signal,
               **result
           })

           current_position = {
               "instrument": instrument,
               "title": title,
               "direction": signal,
               "entry_price": price,
               "entry_time": candle_time,
               "stop_loss": analysis.get("stop_loss")
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
       profitable /
       total *
       100
   )
 
   return {
       "total": total,
       "profitable": profitable,
       "losing": losing,
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
# АНАЛИЗ ОДНОЙ АКЦИИ (ОБНОВЛЕННЫЙ ДВИЖОК)
# ============================================================
 
def get_future_status(
   prefix,
   title,
   emoji
):
   # Карта переключения старых префиксов на новые тикеры акций
   legacy_assets = {
       "CR": "SBER",  # Вместо Юаня берем Сбербанк
       "GD": "GAZP",  # Вместо Золота берем Газпром
       "BR": "LKOH",  # Вместо Нефти берем Лукойл
       "NG": "NVTK",  # Вместо Газа берем Новатэк
       "MX": "YNDX",  # Вместо Индекса берем Яндекс
       "SV": "ROSN"   # Вместо Серебра берем Роснефть
   }
   
   target_ticker = legacy_assets.get(prefix.upper(), prefix.upper())
 
   result = {
       "prefix": prefix,
       "title": f"{emoji} {title}", # Дефолтный заголовок на случай ошибки
       "emoji": emoji,
       "status": "Ошибка",
       "message": "",
       "ticker": "—",
       "uid": "—",
       "candles": 0,
       "strategy": {
           "signal": "Нет сигналов",
           "direction": "—",
           "description": ""
       },
       "history": [],
       "statistics": {},
       "open_position": None
   }
 
   try:
       # Вызываем функцию поиска акции на рынке спот
       share_info = find_active_share(target_ticker)
 
       if not share_info:
           result["message"] = (
               f"Акция {target_ticker} не найдена в каталоге"
           )
           return result
 
       # Меняем заголовок карточки на РЕАЛЬНОЕ название компании из Т-Банка!
       result["title"] = f"{emoji} {share_info['name']}"
       result["ticker"] = share_info["ticker"]
       result["uid"] = share_info["instrument_uid"]
 
       # Загружаем и нормализуем 4-часовые свечи по UID акции
       candles_raw = get_candles(
           share_info["instrument_uid"]
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
 
       # ----------------------------------------------------
       # Текущий сигнал стратегии Price Action + Стоп-Лосс
       # ----------------------------------------------------
       strategy = analyze_strategy(
           candles
       )
 
       result["strategy"] = strategy
       result["status"] = "OK"
       result["message"] = (
           "Данные получены"
       )
 
       # ----------------------------------------------------
       # История стратегии с учетом лимита Стоп-Лосса 1000 ₽
       # ----------------------------------------------------
       trades, open_position = (
           build_strategy_history(
               candles,
               share_info["ticker"],
               share_info["name"]
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
 
       return result
 
   except Exception as e:
       log.exception(
           "Ошибка обработки акции %s",
           title
       )
       result["message"] = str(e)
       return result
 
 
# ============================================================
# ОБЩИЙ СБОР ДАННЫХ ПО ПОРТФЕЛЮ АКЦИЙ
# ============================================================
 
def collect_data():
   # Передаем старые маркеры, но бэкенд на лету свяжет их с крупнейшими компаниями РФ
   futures = [
       get_future_status("CR", "Сбербанк", "🟢"),
       get_future_status("GD", "Газпром", "🔵"),
       get_future_status("BR", "Лукойл", "🔴"),
       get_future_status("NG", "Новатэк", "🔥"),
       get_future_status("MX", "Яндекс", "🟡"),
       get_future_status("SV", "Роснефть", "🛢️")
   ]
 
   last_signal = {
       "title": "Нет сигналов",
       "signal": "—",
       "direction": "—",
       "description": ""
   }
 
   # Если есть текущий разворотный сигнал, выводим его в топ
   for item in futures:
       if "strategy" in item:
           signal = item["strategy"].get("signal")
           if signal in ("LONG", "SHORT"):
               last_signal = {
                   "title": item["title"],
                   "signal": signal,
                   "direction": item["strategy"].get("direction", "—"),
                   "description": item["strategy"].get("description", "")
               }
               break
 
   # --------------------------------------------------------
   # Общая статистика по всем закрытым сделкам
   # --------------------------------------------------------
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
 
   # --------------------------------------------------------
   # Накопление истории сделок в JSON
   # --------------------------------------------------------
   existing_history = load_history()
   existing_keys = set()
 
   for trade in existing_history:
       key = (
           trade.get("instrument"),
           trade.get("entry_time"),
           trade.get("exit_time"),
           trade.get("direction")
       )
       existing_keys.add(key)
 
   for trade in all_trades:
       key = (
           trade.get("instrument"),
           trade.get("entry_time"),
           trade.get("exit_time"),
           trade.get("direction")
       )
       if key not in existing_keys:
           existing_history.append(trade)
           existing_keys.add(key)
 
   existing_history = existing_history[-5000:]
   save_history(existing_history)
 
   # Возвращаем готовую структуру для Flask-сервера
   return {
       "updated":
           datetime.now(
               timezone.utc
           ).isoformat(),
 
       "futures":  # Оставляем ключ "futures", чтобы фронтенд-шаблон HTML не сломался
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
               "Противоположный сигнал / Stop-Loss 1000₽"
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
               "error": str(e)
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
           "count": len(history),
           "history": history
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
 
<meta name="viewport"
     content="width=device-width, initial-scale=1.0">
 
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
 
   padding: 25px 0 50px;
}
 
.header {
 
   display: flex;
 
   justify-content: space-between;
 
   align-items: center;
 
   margin-bottom: 25px;
}
 
.logo {
 
   font-size: 28px;
 
   font-weight: 800;
 
   letter-spacing: 1px;
}
 
.logo span {
 
   color: #d7aa52;
}
 
.updated {
 
   color: #8c96a8;
 
   font-size: 13px;
}
 
.grid {
 
   display: grid;
 
   grid-template-columns:
       repeat(
           3,
           1fr
       );
 
   gap: 18px;
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
 
   border-radius: 18px;
 
   padding: 20px;
 
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
 
   margin-top: 0;
 
   font-size: 20px;
}
 
.status {
 
   display: inline-block;
 
   padding: 6px 10px;
 
   border-radius: 20px;
 
   font-size: 12px;
 
   background: #193d2b;
 
   color: #66e29a;
}
 
.error {
 
   background: #442020;
 
   color: #ff8585;
}
 
.info {
 
   margin-top: 15px;
 
   color: #aab3c2;
 
   font-size: 13px;
 
   line-height: 1.6;
}
 
.signal {
 
   margin-top: 15px;
 
   padding: 14px;
 
   border-radius: 14px;
 
   background: #111720;
 
   font-size: 18px;
 
   font-weight: bold;
}
 
.long {
 
   color: #52e58a;
}
 
.short {
 
   color: #ff6666;
}
 
.none {
 
   color: #9ca5b4;
}
 
.statistics {
 
   margin-top: 25px;
}
 
.stats-grid {
 
   display: grid;
 
   grid-template-columns:
       repeat(
           4,
           1fr
       );
 
   gap: 12px;
}
 
.stat {
 
   background: #111720;
 
   padding: 16px;
 
   border-radius: 14px;
}
 
.stat-title {
 
   font-size: 12px;
 
   color: #8f99aa;
 
   margin-bottom: 7px;
}
 
.stat-value {
 
   font-size: 20px;
 
   font-weight: bold;
}
 
.history {
 
   margin-top: 25px;
}
 
.table-wrap {
 
   overflow-x: auto;
}
 
table {
 
   width: 100%;
 
   border-collapse: collapse;
 
   min-width: 900px;
}
 
th,
td {
 
   padding: 11px;
 
   border-bottom:
       1px solid
       rgba(
           255,
           255,
           255,
           0.07
       );
 
   text-align: left;
 
   font-size: 13px;
}
 
th {
 
   color: #9ca5b4;
 
   font-weight: normal;
}
 
.positive {
 
   color: #52e58a;
 
   font-weight: bold;
}
 
.negative {
 
   color: #ff6666;
 
   font-weight: bold;
}
 
.settings {
 
   margin-top: 20px;
 
   color: #858fa0;
 
   font-size: 13px;
 
   line-height: 1.7;
}
 
button {
 
   margin-top: 20px;
 
   border: none;
 
   border-radius: 12px;
 
   padding: 12px 20px;
 
   background: #d7aa52;
 
   color: #111;
 
   font-weight: bold;
 
   cursor: pointer;
}
 
@media (
   max-width: 900px
) {
 
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
   >
 
   </div>
 
 
   <div class="card statistics">
 
       <h2>
           📊 Статистика стратегии
       </h2>
 
       <div
           class="stats-grid"
           id="statistics"
       >
       </div>
 
   </div>
 
 
   <div class="card history">
 
       <h2>
           📜 История сделок
       </h2>
 
       <div
           class="table-wrap"
           id="history"
       >
       </div>
 
   </div>
 
 
   <div class="card settings">
 
       <b>Настройки расчёта</b><br>
 
       Размер виртуальной позиции:
       <span id="positionSize">—</span> ₽<br>
 
       Комиссия покупки:
       <span id="buyCommission">—</span>%<br>
 
       Комиссия продажи:
       <span id="sellCommission">—</span>%<br>
 
       Налог:
       <span id="tax">—</span>%<br>
 
       Выход из сделки:
       <span id="exitRule">—</span>
 
   </div>
 
 
   <button onclick="loadData()">
       🔄 Обновить сейчас
   </button>
 
</div>
 
 
<script>
 
function money(value) {
 
   if (
       value === undefined ||
       value === null
   ) {
       return "0.00";
   }
 
   return Number(value)
       .toLocaleString(
           "ru-RU",
           {
               minimumFractionDigits: 2,
               maximumFractionDigits: 2
           }
       );
}
 
 
function signalClass(signal) {
 
   if (
       signal === "LONG"
   ) {
       return "long";
   }
 
   if (
       signal === "SHORT"
   ) {
       return "short";
   }
 
   return "none";
}
 
 
function renderFutures(data) {
 
   const container =
       document.getElementById(
           "futures"
       );
 
   container.innerHTML = "";
 
   data.futures.forEach(
       item => {
 
           const signal =
               item.strategy.signal;
 
           const statusClass =
               item.status === "OK"
               ? "status"
               : "status error";
 
           const card =
               document.createElement(
                   "div"
               );
 
           card.className =
               "card";
 
           let openPosition =
               "Нет";
 
           if (
               item.open_position
           ) {
 
               openPosition =
                   item.open_position.direction
                   +
                   " от "
                   +
                   item.open_position.entry_price;
 
           }
 
           card.innerHTML = `
 
               <h2>
                   ${item.emoji}
                   ${item.title}
               </h2>
 
               <span
                   class="${statusClass}"
               >
                   ${item.status}
               </span>
 
               <div class="info">
 
                   ${item.message}<br>
 
                   Тикер:
                   <b>${item.ticker}</b><br>
 
                   UID:
                   <b>${item.uid}</b><br>
 
                   Свечей:
                   <b>${item.candles}</b>
 
               </div>
 
               <div
                   class="signal ${signalClass(signal)}"
               >
 
                   ${signal}
 
                   <div
                       style="
                       font-size:12px;
                       margin-top:6px;
                       font-weight:normal;
                       "
                   >
 
                       ${
                           item.strategy.description
                           || ""
                       }
 
                   </div>
 
               </div>
 
               <div class="info">
 
                   Закрытых сделок:
                   <b>
                       ${
                           item.statistics.total
                           || 0
                       }
                   </b><br>
 
                   Проходимость:
                   <b>
                       ${
                           item.statistics.winrate
                           || 0
                       }%
                   </b><br>
 
                   Чистый результат:
                   <b>
                       ${
                           money(
                               item.statistics.net
                           )
                       } ₽
                   </b><br>
 
                   Открытая позиция:
                   <b>
                       ${openPosition}
                   </b>
 
               </div>
           `;
 
           container.appendChild(
               card
           );
       }
   );
}
 
 
function renderStatistics(
   statistics
) {
 
   const container =
       document.getElementById(
           "statistics"
       );
 
   container.innerHTML = `
 
       <div class="stat">
 
           <div class="stat-title">
               Всего сделок
           </div>
 
           <div class="stat-value">
               ${statistics.total}
           </div>
 
       </div>
 
 
       <div class="stat">
 
           <div class="stat-title">
               Прибыльных
           </div>
 
           <div class="stat-value positive">
               ${statistics.profitable}
           </div>
 
       </div>
 
 
       <div class="stat">
 
           <div class="stat-title">
               Убыточных
           </div>
 
           <div class="stat-value negative">
               ${statistics.losing}
           </div>
 
       </div>
 
 
       <div class="stat">
 
           <div class="stat-title">
               Проходимость
           </div>
 
           <div class="stat-value">
               ${statistics.winrate}%
           </div>
 
       </div>
 
 
       <div class="stat">
 
           <div class="stat-title">
               До расходов
           </div>
 
           <div class="stat-value">
               ${money(statistics.gross)} ₽
           </div>
 
       </div>
 
 
       <div class="stat">
 
           <div class="stat-title">
               Комиссии
           </div>
 
           <div class="stat-value">
               ${money(statistics.commission)} ₽
           </div>
 
       </div>
 
 
       <div class="stat">
 
           <div class="stat-title">
               Налог
           </div>
 
           <div class="stat-value">
               ${money(statistics.tax)} ₽
           </div>
 
       </div>
 
 
       <div class="stat">
 
           <div class="stat-title">
               ЧИСТЫЙ РЕЗУЛЬТАТ
           </div>
 
           <div
               class="stat-value
               ${
                   statistics.net >= 0
                   ? "positive"
                   : "negative"
               }"
           >
               ${money(statistics.net)} ₽
           </div>
 
       </div>
   `;
}
 
 
function renderHistory(
   data
) {
 
   const container =
       document.getElementById(
           "history"
       );
 
   let allTrades = [];
 
   data.futures.forEach(
       item => {
 
           if (
               item.history
           ) {
 
               allTrades =
                   allTrades.concat(
                       item.history
                   );
           }
       }
   );
 
   allTrades.sort(
       (a, b) =>
           new Date(
               b.exit_time
           )
           -
           new Date(
               a.exit_time
           )
   );
 
   if (
       allTrades.length === 0
   ) {
 
       container.innerHTML =
           "<div class='info'>Пока закрытых сделок нет.</div>";
 
       return;
   }
 
   let html = `
 
       <table>
 
           <thead>
 
               <tr>
 
                   <th>
                       Инструмент
                   </th>
 
                   <th>
                       Направление
                   </th>
 
                   <th>
                       Вход
                   </th>
 
                   <th>
                       Выход
                   </th>
 
                   <th>
                       Цена входа
                   </th>
 
                   <th>
                       Цена выхода
                   </th>
 
                   <th>
                       Результат
                   </th>
 
                   <th>
                       Комиссия
                   </th>
 
                   <th>
                       Налог
                   </th>
 
                   <th>
                       Чистый результат
                   </th>
 
               </tr>
 
           </thead>
 
           <tbody>
   `;
 
   allTrades
       .slice(0, 100)
       .forEach(
           trade => {
 
               const net =
                   Number(
                       trade.net_result
                   );
 
               const cls =
                   net >= 0
                   ? "positive"
                   : "negative";
 
               const commission =
                   Number(
                       trade.buy_commission
                   )
                   +
                   Number(
                       trade.sell_commission
                   );
 
               html += `
 
                   <tr>
 
                       <td>
                           ${trade.title}
                       </td>
 
                       <td
                           class="${
                               trade.direction === "LONG"
                               ? "long"
                               : "short"
                           }"
                       >
                           ${trade.direction}
                       </td>
 
                       <td>
                           ${trade.entry_time}
                       </td>
 
                       <td>
                           ${trade.exit_time}
                       </td>
 
                       <td>
                           ${trade.entry_price}
                       </td>
 
                       <td>
                           ${trade.exit_price}
                       </td>
 
                       <td>
                           ${money(
                               trade.gross_result
                           )} ₽
                       </td>
 
                       <td>
                           ${money(
                               commission
                           )} ₽
                       </td>
 
                       <td>
                           ${money(
                               trade.tax
                           )} ₽
                       </td>
 
                       <td
                           class="${cls}"
                       >
                           ${money(net)} ₽
                       </td>
 
                   </tr>
               `;
           }
       );
 
   html += `
 
           </tbody>
 
       </table>
   `;
 
   container.innerHTML =
       html;
}
 
 
async function loadData() {
 
   try {
 
       const response =
           await fetch(
               "/api/status"
           );
 
       const data =
           await response.json();
 
       if (
           data.error
       ) {
 
           console.error(
               data.error
           );
 
           return;
       }
 
       renderFutures(
           data
       );
 
       renderStatistics(
           data.statistics
       );
 
       renderHistory(
           data
       );
 
       document.getElementById(
           "updated"
       ).textContent =
           "Обновлено: "
           +
           new Date(
               data.updated
           ).toLocaleString(
               "ru-RU"
           );
 
       document.getElementById(
           "positionSize"
       ).textContent =
           money(
               data.settings.position_size
           );
 
       document.getElementById(
           "buyCommission"
       ).textContent =
           data.settings.buy_commission;
 
       document.getElementById(
           "sellCommission"
       ).textContent =
           data.settings.sell_commission;
 
       document.getElementById(
           "tax"
       ).textContent =
           data.settings.tax;
 
       document.getElementById(
           "exitRule"
       ).textContent =
           data.settings.exit_rule;
 
   } catch (error) {
 
       console.error(
           "Ошибка загрузки:",
           error
       );
 
   }
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
# ГЛАВНАЯ СТРАНИЦА
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
 
           data = collect_data()
 
           log.info(
               "MARKUS TRADE | "
               "обновление данных"
           )
 
           for item in data[
               "futures"
           ]:
 
               log.info(
                   "%s | ticker=%s | "
                   "candles=%s | signal=%s",
                   item["title"],
                   item["ticker"],
                   item["candles"],
                   item[
                       "strategy"
                   ]["signal"]
               )
 
           stats = data[
               "statistics"
           ]
 
           log.info(
               "СТАТИСТИКА | "
               "сделок=%s | "
               "winrate=%s%% | "
               "чистый=%s ₽",
               stats["total"],
               stats["winrate"],
               stats["net"]
           )
 
       except Exception as e:
 
           log.exception(
               "Ошибка фонового мониторинга: %s",
               e
           )
 
       time.sleep(
           UPDATE_SECONDS
       )
 
 
# ============================================================
# ЗАПУСК
# ============================================================
 
if __name__ == "__main__":
 
   token = get_token()
 
   if not token:
 
       log.error(
           "ВНИМАНИЕ: API-токен не найден!"
       )
 
   else:
 
       log.info(
           "API-токен найден."
       )
 
   monitor_thread = threading.Thread(
       target=background_monitor,
       daemon=True
   )
 
   monitor_thread.start()
 
   port = int(
       os.environ.get(
           "PORT",
           "5000"
       )
   )
 
   log.info(
       "MARKUS TRADE запускается "
       "на порту %s",
       port
   )
 
   app.run(
       host="0.0.0.0",
       port=port,
       debug=False
   )
