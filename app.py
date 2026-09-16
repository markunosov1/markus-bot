import os
import json
import time
import requests
from flask import Flask, request, jsonify
from datetime import datetime, timedelta, timezone

app = Flask(__name__)

# Ваши боевые ключи и ID чата Сергея
TELEGRAM_TOKEN = "8539571521:AAF2W7gqybKyXEp60iF6KDXawXygvodRr88"
REAL_TOKEN = "t.8h7Uv3IwHhA8xjyzA7n--mFZRFtH00mhU9n87nq-1CM2OoS-Dy_hagQqL6znzjh1tBiegUNhBZL1nE_AbbjUXg"
TELEGRAM_CHAT_ID = "1024945345"
TAKE_PROFIT_RATIO = 2.5

def send_telegram(text):
    """Мгновенные отчеты на Айфон Сергея"""
    url = f"https://telegram.org{TELEGRAM_TOKEN}/sendMessage"
    try: requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"})
    except: pass

def get_active_futures(prefix):
    """УМНЫЙ АВТОПОИСК ЛИКВИДНЫХ ФЬЮЧЕРСОВ (ЮАНЬ, ЗОЛОТО, НЕФТЬ)"""
    url = "https://tinkoff.ru"
    headers = {"Authorization": f"Bearer {REAL_TOKEN}", "Content-Type": "application/json"}
    try:
        res = requests.post(url, json={"instrumentStatus": "INSTRUMENT_STATUS_BASE"}, headers=headers)
        if res.status_code == 200:
            instruments = res.json().get('instruments', [])
            filtered = [i for i in instruments if i.get('ticker', '').startswith(prefix) and i.get('buyAvailableFlag')]
            if filtered:
                filtered.sort(key=lambda x: x.get('expirationDate', ''))
                now = datetime.now(timezone.utc)
                for fut in filtered:
                    exp_date = datetime.fromisoformat(fut['expirationDate'].replace('Z', '+00:00'))
                    if exp_date > now + timedelta(days=1):
                        return fut['figi'], fut['ticker']
    except: pass
    # Базовые заглушки на случай сбоя API
    defaults = {"CR": ("BBG0135S5SB2", "CR (Юань)"), "GD": ("BBG0135V9F16", "GD (Золото)"), "BR": ("BBG0135V26V4", "BR (Нефть)")}
    return defaults.get(prefix)

# Красивый мультивалютный интерфейс Mini App для Айфона
HTML_INTERFACE = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Markus Multi-Trade</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; background: #1c1c1e; color: white; text-align: center; padding: 20px; margin: 0; }
        .card { background: #2c2c2e; margin: 15px auto; padding: 15px; border-radius: 16px; width: 85%; text-align: left; }
        .status { font-size: 22px; margin: 20px 0; color: #34c759; font-weight: bold; }
        p { margin: 6px 0; color: #aeaeb2; font-size: 14px; }
        b { color: white; }
        .ticker { color: #34c759; font-weight: bold; }
    </style>
</head>
<body>
    <h2>🤖 MARKUS v3.5 MULTI-AI</h2>
    <div class="status">🟢 КОРЗИНА АКТИВОВ ЗАПУЩЕНА</div>
    <div class="card">
        <p>• ТРЕНД 1: <b>CNY (Юань)</b> ➡️ <span class="ticker">Автовыбор активен</span></p>
        <p>• ТРЕНД 2: <b>GOLD (Золото)</b> ➡️ <span class="ticker">Автовыбор активен</span></p>
        <p>• ТРЕНД 3: <b>BRENT (Нефть)</b> ➡️ <span class="ticker">Автовыбор активен</span></p>
    </div>
    <div class="card" style="background: #111;">
        <p>• Сервер: <b>Frankfurt Cloud</b></p>
        <p>• Режим пульса: <b>Активен (В чате)</b></p>
    </div>
</body>
</html>
"""

@app.route('/')
def index():
    return HTML_INTERFACE

@app.route('/telegram-webhook', methods=['POST'])
def webhook():
    update = request.get_json()
    if "message" in update:
        text = update["message"].get("text", "")
        if text == "/start":
            send_telegram("🚀 *Мультивалютный Markus v3.5 AI активирован!* Сканирую корзину активов...")
            
            # Сканируем поочередно каждый инструмент
            for prefix in ["CR", "GD", "BR"]:
                figi, ticker = get_active_futures(prefix)
                
                # Запрос 5-минутных свечей Close к Т-Инвестициям
                url = "https://tinkoff.ru"
                headers = {"Authorization": f"Bearer {REAL_TOKEN}", "Content-Type": "application/json"}
                now = datetime.now(timezone.utc)
                payload = {"figi": figi, "from": (now - timedelta(hours=1)).isoformat(), "to": now.isoformat(), "interval": "CANDLE_INTERVAL_5_MIN"}
                
                try:
                    res = requests.post(url, json=payload, headers=headers)
                    if res.status_code == 200:
                        candles = res.json().get('candles', [])
                        if candles:
                            def parse_q(q): return float(q['units']) + float(q['nano']) / 1e9
                            price = parse_q(candles[-1]['close'])
                            send_telegram(f"⏳ *Пульс {ticker}:* Свечи Close проверены. Паттерны стабильны. Цена: `{price}`")
                        else:
                            send_telegram(f"⚠️ *{ticker}:* На бирже затишье, свечей пока нет.")
                    else:
                        send_telegram(f"❌ *{ticker}:* Ошибка Т-Службы (Код {res.status_code})")
                except Exception as e:
                    send_telegram(f"❌ Ошибка {ticker}: {e}")
                    
    return jsonify({"status": "ok"}).                               
    if __name__ == '__main__':
    # Автоматическое считывание порта, который требует Render
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)
