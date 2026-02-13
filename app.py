import os
import requests
import psycopg2
import numpy as np
from datetime import datetime
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI

# ---------------- CONFIG ----------------

app = Flask(__name__)
MAX_LEN = 1500

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY ontbreekt")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL ontbreekt")

client = OpenAI(api_key=OPENAI_API_KEY)

db = psycopg2.connect(DATABASE_URL)
db.autocommit = True

# ---------------- DATABASE ----------------

def init_db():
    with db.cursor() as c:
        c.execute("""
        CREATE TABLE IF NOT EXISTS portfolios (
            user_id TEXT,
            ticker TEXT,
            shares FLOAT,
            PRIMARY KEY(user_id, ticker)
        );
        """)
        c.execute("""
        CREATE TABLE IF NOT EXISTS history (
            user_id TEXT,
            user_msg TEXT,
            bot_msg TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        );
        """)
init_db()

# ---------------- HELPERS ----------------

def send_long_message(resp, text):
    for i in range(0, len(text), MAX_LEN):
        resp.message(text[i:i+MAX_LEN])

def add_history(user_id, u_msg, b_msg):
    with db.cursor() as c:
        c.execute(
            "INSERT INTO history(user_id,user_msg,bot_msg,created_at) VALUES(%s,%s,%s,%s)",
            (user_id, u_msg, b_msg, datetime.utcnow())
        )

def get_history(user_id, limit=5):
    with db.cursor() as c:
        c.execute(
            "SELECT user_msg, bot_msg FROM history WHERE user_id=%s ORDER BY created_at DESC LIMIT %s",
            (user_id, limit)
        )
        return c.fetchall()[::-1]

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
            "price": round(float(close.iloc[-1]), 2),
            "rsi": round(float(rsi[-1]), 1),
            "trend": trend
        }

    except Exception as e:
        print(f"[Market Error] {e}")
        return None

# ---------------- GPT ----------------

def ask_gpt(user_id, message, market_data=None):
    system_prompt = """
You are a professional investment assistant.
- Use only provided market data.
- Do NOT give financial advice.
- Keep it concise.
- Respond in user's language.
"""

    messages = [{"role": "system", "content": system_prompt}]

    for u, b in get_history(user_id):
        messages.append({"role": "user", "content": u})
        messages.append({"role": "assistant", "content": b})

    if market_data:
        message += (
            f"\n\n[Market Data]\n"
            f"Price: ${market_data['price']}\n"
            f"RSI: {market_data['rsi']}\n"
            f"Trend: {market_data['trend']}"
        )

    messages.append({"role": "user", "content": message})

    try:
        r
