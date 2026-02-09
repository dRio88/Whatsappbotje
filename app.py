from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI
import yfinance as yf
import pandas as pd
import numpy as np
import redis
import psycopg2
import os
import json
from datetime import datetime

# ---------------- CONFIG ----------------
app = Flask(__name__)
client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

redis_client = redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
db = psycopg2.connect(os.environ["DATABASE_URL"])
db.autocommit = True

# ---------------- DATABASE ----------------
def init_db():
    with db.cursor() as c:
        c.execute("""
        CREATE TABLE IF NOT EXISTS portfolios (
            user_id TEXT,
            ticker TEXT,
            shares FLOAT
        );
        """)
        c.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            user_id TEXT,
            ticker TEXT,
            rsi_threshold FLOAT
        );
        """)

init_db()

# ---------------- MARKET DATA ----------------
def get_technical_data(ticker):
    df = yf.Ticker(ticker).history(period="3mo")
    if df.empty:
        return None

    close = df["Close"]
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    rs = gain.rolling(14).mean() / loss.rolling(14).mean()
    rsi = 100 - (100 / (1 + rs))

    return {
        "price": round(close.iloc[-1], 2),
        "rsi": round(rsi.iloc[-1], 1),
        "trend": "bullish" if close.iloc[-1] > close.mean() else "bearish"
    }

# ---------------- OPENAI ----------------
def ask_gpt(user, message, market=None):
    system = """
Je bent een professionele beleggingsassistent.
Leg duidelijk, praktisch en begrijpelijk uit.
Geen financieel advies.
Gebruik marktdata indien beschikbaar.
"""

    context = ""
    if market:
        context = f"""
Marktdata:
Prijs: {market['price']}
RSI: {market['rsi']}
Trend: {market['trend']}
"""

    messages = [
        {"role": "system", "content": system},
        {"role": "assistant", "content": context},
        {"role": "user", "content": message}
    ]

    response = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=messages
    )

    return response.choices[0].message.content

# ---------------- PORTFOLIO ----------------
def add_to_portfolio(user, ticker, shares):
    with db.cursor() as c:
        c.execute(
            "INSERT INTO portfolios VALUES (%s,%s,%s)",
            (user, ticker, shares)
        )

def get_portfolio(user):
    with db.cursor() as c:
        c.execute(
            "SELECT ticker, shares FROM portfolios WHERE user_id=%s",
            (user,)
        )
        return c.fetchall()

# ---------------- ALERTS ----------------
def add_alert(user, ticker, rsi):
    with db.cursor() as c:
        c.execute(
            "INSERT INTO alerts VALUES (%s,%s,%s)",
            (user, ticker, rsi)
        )

def check_alerts():
    with db.cursor() as c:
        c.execute("SELECT user_id, ticker, rsi_threshold FROM alerts")
        alerts = c.fetchall()

    triggered = []
    for user, ticker, threshold in alerts:
        data = get_technical_data(ticker)
        if data and data["rsi"] < threshold:
            triggered.append((user, ticker, data["rsi"]))
    return triggered

# ---------------- DAILY BRIEF ----------------
def daily_brief():
    tickers = ["AAPL", "MSFT", "SPY", "BTC-USD"]
    brief = "📊 Dagelijkse Marktupdate\n\n"
    for t in tickers:
        d = get_technical_data(t)
        if d:
            brief += f"{t}: ${d['price']} | RSI {d['rsi']} | {d['trend']}\n"
    return brief

# ---------------- WHATSAPP ----------------
@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    msg = request.form.get("Body").strip()
    user = request.form.get("From")

    # geheugen via Redis
    history_key = f"history:{user}"
    history = redis_client.get(history_key)
    history = json.loads(history) if history else []

    reply = ""

    # ---- COMMANDS ----
    if msg.lower().startswith("koop"):
        # koop AAPL 5
        _, ticker, shares = msg.split()
        add_to_portfolio(user, ticker.upper(), float(shares))
        reply = f"✅ Toegevoegd: {shares} aandelen {ticker.upper()}"

    elif msg.lower().startswith("portfolio"):
        pf = get_portfolio(user)
        if not pf:
            reply = "📂 Je portfolio is leeg."
        else:
            reply = "📂 Je portfolio:\n"
            for t, s in pf:
                reply += f"- {t}: {s} aandelen\n"

    elif "waarschuw" in msg.lower():
        # waarschuw AAPL 30
        parts = msg.split()
        ticker = parts[-2].upper()
        rsi = float(parts[-1])
        add_alert(user, ticker, rsi)
        reply = f"⏰ Alert ingesteld: {ticker} bij RSI < {rsi}"

    elif msg.lower() == "brief":
        reply = daily_brief()

    else:
        # normale chat / analyse
        ticker = msg.split()[-1].upper()
        market = get_technical_data(ticker)
        reply = ask_gpt(user, msg, market)

    history.append({"user": msg, "bot": reply})
    redis_client.set(history_key, json.dumps(history[-10:]))

    resp = MessagingResponse()
    resp.message(reply)
    return str(resp)

# ---------------- ALERT CRON ENDPOINT ----------------
@app.route("/check-alerts")
def alerts():
    triggered = check_alerts()
    for user, ticker, rsi in triggered:
        print(f"ALERT → {user}: {ticker} RSI {rsi}")
    return "ok"

# ---------------- START ----------------
if __name__ == "__main__":
    app.run()
