import os
import json
import time
import requests
from flask import Flask, request, jsonify
from datetime import datetime, timedelta, timezone

app = Flask(__name__)

TELEGRAM_TOKEN = "8539571521:AAF2W7gqybKyXEp60iF6KDXawXygvodRr88"
REAL_TOKEN = "t.LjJnhIhErtp7NikKcKnYPn2x5fLfd-LguHRfjFXJz3PCLAIBr1k4uo_rxJlorPimprQaZaHEZOGp246HAhXAXA"
TELEGRAM_CHAT_ID = "1024945345"
def get_active_futures(prefix):
    # Меняем старый сайт tinkoff.ru на актуальный адрес T-Invest API
    url = "https://tbank.ru"
    headers = {
        "Authorization": f"Bearer {REAL_TOKEN}",
        "Content-Type": "application/json"
    }

    try:
        # Отправляем правильный запрос на новый адрес
        res = requests.post(url, json={"instrumentStatus": "INSTRUMENT_STATUS_BASE"}, headers=headers, timeout=5)
        if res.status_code == 200:
            instruments = res.json().get('instruments', [])
            # Дальше идет ваша оригинальная логика фильтрации и поиска фьючерса
            filtered = [i for i in instruments if i.get('ticker', '').startswith(prefix) and i.get('buyAvailableFlag')]
            if filtered:
                filtered.sort(key=lambda x: x.get('expirationDate', ''))
                now = datetime.now(timezone.utc)
                for fut in filtered:
                    exp_date = datetime.fromisoformat(fut['expirationDate'].replace('Z', '+00:00'))
                    if exp_date > now + timedelta(days=1):
                        return fut['figi'], fut['ticker']
        else:
            print(f"Ошибка API Т-Банка: {res.status_code} - {res.text}")
            return None, None
    except Exception as e:
        print(f"Ошибка сети при запросе фьючерсов: {e}")
        return None, None

    except:
        pass
    defaults = {
        "CR": ("BBG0135S5SB2", "CR (Юань)"),
        "GD": ("BBG0135V9F16", "GD (Золото)"),
        "BR": ("BBG0135V26V4", "BR (Нефть)")
    }
    return defaults.get(prefix)

def get_market_prices():
    prices = {}
    for prefix in ["CR", "GD", "BR"]:
        figi, ticker = get_active_futures(prefix)
        url = "https://tinkoff.ru"
        headers = {"Authorization": f"Bearer {REAL_TOKEN}", "Content-Type": "application/json"}
        now = datetime.now(timezone.utc)
        payload = {
            "figi": figi,
            "from": (now - timedelta(hours=2)).isoformat(),
            "to": now.isoformat(),
            "interval": "CANDLE_INTERVAL_5_MIN"
        }
        try:
            time.sleep(0.3)
            res = requests.post(url, json=payload, headers=headers, timeout=5)
            if res.status_code == 200:
                candles = res.json().get('candles', [])
                if candles:
                    price = float(candles[-1]['close']['units']) + float(candles[-1]['close']['nano']) / 1e9
                    prices[ticker] = f"{price}"
                else:
                    prices[ticker] = "Нет свечей"
            else:
                prices[ticker] = f"Ошибка {res.status_code}"
        except:
            prices[ticker] = "Ошибка сети"
    return prices

@app.route('/')
def index():
    data = get_market_prices()
    cny_p = data.get("CR (Юань)", "Загрузка...")
    gold_p = data.get("GD (Золото)", "Загрузка...")
    brent_p = data.get("BR (Нефть)", "Загрузка...")
    
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Markus Terminal</title>
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; background: #1c1c1e; color: white; text-align: center; padding: 20px; margin: 0; }}
            .card {{ background: #2c2c2e; margin: 15px auto; padding: 15px; border-radius: 16px; width: 85%; text-align: left; }}
            .status {{ font-size: 22px; margin: 20px 0; color: #34c759; font-weight: bold; }}
            p {{ margin: 8px 0; color: #aeaeb2; font-size: 15px; }}
            .price {{ color: #34c759; font-weight: bold; float: right; }}
            .btn {{ background: #2c2c2e; border: 1px solid #34c759; color: #34c759; padding: 10px; border-radius: 8px; cursor: pointer; margin-top: 10px; }}
        </style>
    </head>
    <body>
        <h2>🤖 MARKUS TERMINAL v3.6</h2>
        <div class="status">🟢 МОНИТОРИНГ РЫНКА АКТИВЕН</div>
        
        <div class="card">
            <p>• Юань (CNY): <span class="price">{cny_p} руб.</span></p>
            <p>• Золото (GOLD): <span class="price">${gold_p}</span></p>
            <p>• Нефть (BRENT): <span class="price">${brent_p}</span></p>
        </div>
        
        <button class="btn" onclick="window.location.reload();">🔄 ОБНОВИТЬ ЦЕНЫ</button>
    </body>
    </html>
    """
    return html

@app.route('/telegram-webhook', methods=['POST'])
def webhook():
    return jsonify({"status": "ok"})

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)
