from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI
import requests
import pandas as pd
import numpy as np
import psycopg2
import os
from datetime import datetime

# ---------------- CONFIG ----------------
app = Flask(__name__)
MAX_LEN = 1500  # WhatsApp veilige limiet per bericht

# OpenAI GPT-5 setup
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

# Postgres setup
db = psycopg2.connect(os.environ["DATABASE_URL"])
db.autocommit = True

# Base44 setup
BASE44_APP_ID = "698c993f605f6ce2ca5c8c85"
BASE44_API_KEY = "26f6dced974a4b6f9e808116aa25e243"
BASE44_BASE_URL = "https://app-store-boilerplate-copy-ca5c8c85.base44.app/api/apps"

# ---------------- DATABASE ----------------
def init_db():
    with db.cursor() as c:
        c.execute("""
        CREATE TABLE IF NOT EXISTS portfolios (
            user_id TEXT,
            ticker TEXT,
            shares FLOAT,
            PRIMARY KEY(user_id, ticker)
        );""")
        c.execute("""
        CREATE TABLE IF NOT EXISTS history (
            user_id TEXT,
            user_msg TEXT,
            bot_msg TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        );""")
init_db()

# ---------------- HELPER ----------------
def send_long_message(resp, text):
    for i in range(0, len(text), MAX_LEN):
        resp.message(text[i:i+MAX_LEN])

# ---------------- BASE44 PORTFOLIO ----------------
def fetch_base44_portfolio(user_id):
    url = f"{BASE44_BASE_URL}/{BASE44_APP_ID}/functions/getPortfolio"
    headers = {
        "Content-Type": "application/json",
        "api_key": BASE44_API_KEY
    }
    payload = {"user_id": user_id}
    try:
        resp = requests.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
        return data.get("data") or data.get("result")
    except Exception as e:
        print(f"[Base44 ERROR] {e}")
        return None

def sync_portfolio(user_id):
    items = fetch_base44_portfolio(user_id)
    if not items:
        return
    for item in items:
        ticker = item["attributes"]["ticker"].upper()
        shares = float(item["attributes"]["shares"])
        add_to_portfolio(user_id, ticker, shares)
    print(f"[SYNC] Portfolio bijgewerkt voor {user_id}")

# ---------------- PORTFOLIO ----------------
def add_to_portfolio(user_id, ticker, shares):
    with db.cursor() as c:
        c.execute("""
            INSERT INTO portfolios(user_id, ticker, shares)
            VALUES(%s,%s,%s)
            ON CONFLICT(user_id, ticker)
            DO UPDATE SET shares = portfolios.shares + EXCLUDED.shares;
        """, (user_id, ticker, shares))

def get_portfolio(user_id):
    with db.cursor() as c:
        c.execute("SELECT ticker, shares FROM portfolios WHERE user_id=%s", (user_id,))
        return c.fetchall()

# ---------------- MARKET DATA ----------------
def get_technical_data(ticker):
    try:
        import yfinance as yf
        df = yf.Ticker(ticker).history(period="3mo")
        if df.empty or len(df) < 20:
            return None

        close = df["Close"]
        delta = close.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)

        avg_gain = gain.ewm(com=13, adjust=False).mean()
        avg_loss = loss.ewm(com=13, adjust=False).mean()
        rs = np.where(avg_loss == 0, np.inf, avg_gain / avg_loss)
        rsi = 100 - (100 / (1 + rs))

        sma50 = close.rolling(50).mean()
        trend = "📈 bullish" if close.iloc[-1] > sma50.iloc[-1] else "📉 bearish"

        return {
            "price": round(close.iloc[-1], 2),
            "rsi": round(float(rsi[-1]), 1),
            "trend": trend
        }
    except:
        return None

# ---------------- GPT-5 ----------------
def ask_gpt(user_id, message, market_data=None):
    system_prompt = """You are a professional investment assistant.
IMPORTANT:
- Use only the provided market data.
- Do NOT provide financial advice.
- Keep responses concise and helpful.
- Respond in the user's language if possible."""

    history_messages = []

    for u, b in get_history(user_id, 5):
        history_messages.append({"role": "user", "content": u})
        history_messages.append({"role": "assistant", "content": b})

    user_message = message
    if market_data:
        user_message += (
            f"\n\n[Market Data]\n"
            f"Price: ${market_data['price']}\n"
            f"RSI: {market_data['rsi']}\n"
            f"Trend: {market_data['trend']}"
        )

    history_messages.append({"role": "user", "content": user_message})

    try:
        response = client.chat.completions.create(
            model="gpt-5",
            messages=[
                {"role": "system", "content": system_prompt},
                *history_messages
            ],
            max_tokens=350,
            temperature=0.4
        )

        return response.choices[0].message.content

    except Exception as e:
        print(f"[GPT ERROR] {e}")
        return "⚠️ AI kon je vraag niet verwerken."

# ---------------- DAILY BRIEF ----------------
def daily_brief():
    tickers = ["AAPL","MSFT","NVDA","TSLA","SPY","BTC-USD"]
    lines = ["📊 *Dagelijkse Marktupdate*\n"]
    for t in tickers:
        d = get_technical_data(t)
        if d:
            lines.append(f"{d['trend']} *{t}*\nPrijs: ${d['price']}\nRSI: {d['rsi']}\n")
    lines.append("⚠️ Dit is geen financieel advies.")
    return "\n".join(lines)

# ---------------- CHAT HISTORY ----------------
def add_history(user_id, u_msg, b_msg):
    with db.cursor() as c:
        c.execute(
            "INSERT INTO history(user_id,user_msg,bot_msg,created_at) VALUES(%s,%s,%s,%s)",
            (user_id, u_msg, b_msg, datetime.now())
        )

def get_history(user_id, limit):
    with db.cursor() as c:
        c.execute(
            "SELECT user_msg,bot_msg FROM history WHERE user_id=%s ORDER BY created_at DESC LIMIT %s",
            (user_id, limit)
        )
        return c.fetchall()[::-1]

# ---------------- WHATSAPP ----------------
@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    user = request.form.get("From")
    msg = request.form.get("Body").strip().lower()
    reply = ""

    if msg == "portfolio":
        sync_portfolio(user)
        pf = get_portfolio(user)
        if not pf:
            reply = "📂 Je portfolio is leeg of kon niet worden opgehaald."
        else:
            reply = "📂 *Jouw Portfolio*\n"
            for t, s in pf:
                reply += f"💹 {t}: {s} aandelen\n"

    elif msg.startswith("koop"):
        try:
            _, ticker, shares = msg.split()
            add_to_portfolio(user, ticker.upper(), float(shares))
            reply = f"✅ {shares} aandelen {ticker.upper(
