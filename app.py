import os
import time
import threading
import logging
import requests
from datetime import datetime, timedelta, timezone
from flask import Flask, jsonify, render_template_string

# ============================================================
# НАСТРОЙКИ (Берутся из настроек Render Environment)
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

LIVE_TRADING = os.getenv("LIVE_TRADING", "false").lower() == "true"
BASE_TICKER = "Si"
TIMEFRAME = "CANDLE_INTERVAL_5_MIN"
CANDLES_COUNT = 300
SWING_WINDOW = 3
MIN_MOVE_PERCENT = 0.15
LOTS = int(os.getenv("LOTS", "1"))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "30"))

API_URL = "https://tbank.ru"

# ============================================================
# LOGGING & FLASK INITIALIZATION
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("TRADING_BOT")

app = Flask(__name__)

BOT_STATUS = {
    "running": False,
    "instrument": None,
    "last_price": None,
    "last_signal": None,
    "last_signal_time": None,
    "live_trading": LIVE_TRADING,
}

# ============================================================
# ВСПЕМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================
def headers():
    if not T_BANK_TOKEN:
        raise RuntimeError("Не найден токен T-Bank в Environment на Render.")
    return {
        "Authorization": f"Bearer {T_BANK_TOKEN}",
        "Content-Type": "application/json"
    }

def quotation_to_float(value):
    if not value:
        return 0.0
    return int(value.get("units", 0)) + int(value.get("nano", 0)) / 1_000_000_000

# ============================================================
# T-BANK API VIA REQUESTS
# ============================================================
def get_active_futures(prefix):
    url = f"{API_URL}/tinkoff.public.invest.api.contract.v1.InstrumentsService/Futures"
    try:
        res = requests.post(url, json={"instrumentStatus": "INSTRUMENT_STATUS_BASE"}, headers=headers(), timeout=10)
        if res.status_code == 200:
            instruments = res.json().get('instruments', [])
            filtered = [i for i in instruments if i.get('ticker', '').upper().startswith(prefix.upper()) and i.get('buyAvailableFlag')]
            if filtered:
                filtered.sort(key=lambda x: x.get('expirationDate', ''))
                now = datetime.now(timezone.utc)
                for fut in filtered:
                    exp_date = datetime.fromisoformat(fut['expirationDate'].replace('Z', '+00:00'))
                    if exp_date > now + timedelta(days=1):
                        return fut['figi'], fut['ticker']
        return None, None
    except Exception as e:
        log.error(f"Ошибка получения фьючерса: {e}")
        return None, None

def get_market_prices():
    prices = {}
    for prefix in ["CR", "GD", "BR"]:
        figi, ticker = get_active_futures(prefix)
        if not figi:
            prices[prefix] = "Ошибка"
            continue
            
        url = f"{API_URL}/tinkoff.public.invest.api.contract.v1.MarketDataService/GetCandles"
        now = datetime.now(timezone.utc)
        payload = {
            "figi": figi,
            "from": (now - timedelta(hours=24)).isoformat(),
            "to": now.isoformat(),
            "interval": "CANDLE_INTERVAL_1_MIN"
        }
        
        try:
            res = requests.post(url, json=payload, headers=headers(), timeout=5)
            if res.status_code == 200:
                candles = res.json().get('candles', [])
                if candles:
                    price = quotation_to_float(candles[-1].get('close', {}))
                    prices[prefix] = f"{price:.2f}"
                    if prefix == "CR":
                        BOT_STATUS["last_price"] = f"{price:.2f}"
                        BOT_STATUS["instrument"] = ticker
                else:
                    prices[prefix] = "Нет данных"
            else:
                prices[prefix] = "Ошибка API"
        except Exception:
            prices[prefix] = "Ошибка сети"
    return prices

# ============================================================
# FLASK WEB INTERFACE
# ============================================================
@app.route("/")
def home():
    prices = get_market_prices()
    
    html_template = """
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Eva Trading Terminal</title>
        <style>
            body { font-family: -apple-system, BlinkMacSystemFont, Arial, sans-serif; background: #121212; color: #fff; text-align: center; padding: 20px; margin: 0; }
            h1 { color: #00ff88; font-size: 24px; margin-bottom: 5px; font-weight: 600; }
            .bot-name { color: #888; font-size: 14px; margin-bottom: 20px; }
            .card { background: #1e1e1e; padding: 20px; margin: 15px auto; max-width: 340px; border-radius: 12px; border: 1px solid #2a2a2a; box-shadow: 0 4px 15px rgba(0,0,0,0.5); text-align: left; }
            .card h3 { margin: 0 0 10px 0; color: #888; font-size: 12px; text-transform: uppercase; letter-spacing: 1px; }
            .card h2 { margin: 0; color: #fff; font-size: 26px; font-weight: bold; display: flex; justify-content: space-between; }
            .ticker-label { color: #00ff88; font-size: 14px; align-self: center; }
            .status-container { margin: 20px auto; max-width: 340px; display: flex; justify-content: space-between; }
            .status-badge { background: rgba(0,255,136,0.1); color: #00ff88; padding: 6px 14px; border-radius: 20px; font-size: 12px; font-weight: bold; }
            .mode-badge { background: rgba(255,191,0,0.1); color: #ffbf00; padding: 6px 14px; border-radius: 20px; font-size: 12px; font-weight: bold; }
            .mode-badge.live { background: rgba(255,51,51,0.1); color: #ff3333; }
            button { background: #00ff88; color: #000; padding: 14px 30px; border: none; border-radius: 8px; font-size: 16px; font-weight: bold; cursor: pointer; margin-top: 15px; width: 100%; max-width: 340px; box-shadow: 0 4px 10px rgba(0,255,136,0.2); }
            button:active { transform: scale(0.98); background: #00cc6e; }
        </style>
    </head>
    <body>
        <h1>Eva Trading Terminal</h1>
        <div class="bot-name">Система биржевого анализа Swing-точек</div>
        
        <div class="status-container">
            <div class="status-badge">● РАБОТАЕТ</div>
            <div class="mode-badge {% if trading %}live{% endif %}">
                {% if trading %}РЕАЛЬНЫЕ ТОРГИ{% else %}АНАЛИТИКА (TEST){% endif %}
            </div>
        </div>
        
        <div class="card">
            <h3>🇨🇳 Фьючерс Юань (CNY)</h3>
            <h2>{{ prices.get('CR', 'Ошибка') }} <span class="ticker-label">{{ instrument if instrument else '' }}</span></h2>
        </div>
        <div class="card">
            <h3>🏆 Фьючерс Золото (Gold)</h3>
            <h2>{{ prices.get('GD', 'Ошибка') }}</h2>
        </div>
        <div class="card">
            <h3>🛢 Фьючерс Нефть (Brent)</h3>
            <h2>{{ prices.get('BR', 'Ошибка') }}</h2>
        </div>
        
        <div class="card" style="background: #171717;">
            <h3>Последний сигнал стратегии</h3>
            <div style="font-size: 15px; color: #ccc; margin-top: 5px;">
                Статус: <span style="color: #00ff88;">Поиск Swing-точек...</span><br>
                Сигнал: <span style="color: #fff;">{{ last_signal if last_signal else 'Нет сигналов' }}</span>
            </div>
        </div>
        
        <button onclick="window.location.reload()">ОБНОВИТЬ ДАННЫЕ</button>
    </body>
    </html>
    """
    return render_template_string(
        html_template, 
        prices=prices, 
        trading=LIVE_TRADING, 
        instrument=BOT_STATUS["instrument"],
        last_signal=BOT_STATUS["last_signal"]
    )

@app.route("/health")
def health():
    return jsonify({"status": "ok", "bot_running": True})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
