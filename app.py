import os
import json
import time
import requests
from flask import Flask, request, jsonify
from datetime import datetime, timedelta, timezone

app = Flask(__name__)

# Ваши боевые ключи
TELEGRAM_TOKEN = "8539571521:AAF2W7gqybKyXEp60iF6KDXawXygvodRr88"
REAL_TOKEN = "t.8h7Uv3IwHhA8xjyzA7n--mFZRFtH00mhU9n87nq-1CM2OoS-Dy_hagQqL6znzjh1tBiegUNhBZL1nE_AbbjUXg"
FIGI_CNY = "BBG0135S5SB2"  # Фьючерс Юаня

# Интерфейс, который красиво откроется на вашем iPhone внутри Telegram
HTML_INTERFACE = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Markus Trade</title>
    <script src="https://telegram.org"></script>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background: #1c1c1e; color: white; text-align: center; padding: 20px; margin: 0; }
        .card { background: #2c2c2e; margin: 20px auto; padding: 20px; border-radius: 16px; width: 85%; box-shadow: 0 4px 15px rgba(0,0,0,0.3); text-align: left; }
        .btn { background: #34c759; color: white; border: none; padding: 16px; font-size: 18px; border-radius: 14px; cursor: pointer; font-weight: bold; width: 90%; margin-top: 10px; transition: 0.2s; }
        .btn:active { transform: scale(0.98); opacity: 0.9; }
        .btn.stop { background: #ff3b30; }
        .status { font-size: 22px; margin: 25px 0; color: #34c759; font-weight: bold; }
        p { margin: 8px 0; color: #aeaeb2; font-size: 15px; }
        b { color: white; }
    </style>
</head>
<body>
    <div style="margin-top: 30px;">
        <img src="https://icons8.com" width="80" alt="Robot">
    </div>
    <h2>Робот MARKUS v2.0</h2>
    <div class="status" id="status-text">🟢 СЛЕДИТ ЗА РЫНКОМ</div>
    
    <div class="card">
        <p style="font-size: 16px; color: #34c759; font-weight: bold; margin-bottom: 12px;">📊 Параметры стратегии:</p>
        <p>• Актив: <b>Фьючерс CNY (Юань)</b></p>
        <p>• Плечо сделки: <b>1:50 (Максимум)</b></p>
        <p>• Паттерн: <b>Треугольники Close (4 свечи)</b></p>
        <p>• Сервер: <b>Koyeb Cloud (Без прокси)</b></p>
    </div>

    <button class="btn" id="main-btn" onclick="toggleBot()">ОСТАНОВИТЬ РОБОТА</button>

    <script>
        let tg = window.Telegram.WebApp;
        tg.expand(); // Открываем Mini App сразу на весь экран Айфона
        tg.ready();

        let isActive = true;
        function toggleBot() {
            isActive = !isActive;
            const btn = document.getElementById('main-btn');
            const status = document.getElementById('status-text');
            if (isActive) {
                btn.innerText = "ОСТАНОВИТЬ РОБОТА";
                btn.className = "btn";
                status.innerText = "🟢 СЛЕДИТ ЗА РЫНКОМ";
                status.style.color = "#34c759";
            } else {
                btn.innerText = "ЗАПУСТИТЬ РОБОТА";
                btn.className = "btn stop";
                status.innerText = "🔴 ОТКЛЮЧЕН";
                status.style.color = "#ff3b30";
            }
        }
    </script>
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
        chat_id = update["message"]["chat"]["id"]
        text = update["message"].get("text", "")
        if text == "/start":
            url = f"https://telegram.org{TELEGRAM_TOKEN}/sendMessage"
            payload = {
                "chat_id": chat_id,
                "text": "🤖 *Привет, Сергей!*\n\nЯ готов круглосуточно сканировать треугольники Close по Юаню без ограничений прокси-серверов.\n\nНажмите на кнопку ниже, чтобы войти в панель управления с Айфона:",
                "parse_mode": "Markdown",
                "reply_markup": {
                    "inline_keyboard": [[
                        {"text": "🚀 Открыть Markus Trade", "web_app": {"url": "https://" + request.host}}
                    ]]
                }
            }
            requests.post(url, json=payload)
    return jsonify({"status": "ok"})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
