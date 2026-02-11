from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI
import yfinance as yf
import pandas as pd
import numpy as np
import psycopg2
import os
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# ---------------- CONFIG ----------------
app = Flask(__name__)

MAX_LEN = 1500  # veilige WhatsApp limiet

openai_api_key = os.environ.get("OPENAI_API_KEY")
if not openai_api_key:
    raise ValueError("OPENAI_API_KEY ontbreekt.")
client = OpenAI(api_key=openai_api_key)

database_url = os.environ.get("DATABASE_URL")
if not database_url:
    raise ValueError("DATABASE_URL ontbreekt.")
db = psycopg2.connect(database_url)
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
        CREATE TABLE IF NOT EXISTS alerts (
            user_id TEXT,
            ticker TEXT,
            rsi_threshold FLOAT,
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

# ---------------- HELPER ----------------
def send_long_message(resp, text):
    for i in range(0, len(text), MAX_LEN):
        resp.message(text[i:i+MAX_LEN])

# ---------------- MARKET DATA ----------------
def get_technical_data(ticker):
    try:
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

        trend = "bullish" if close.iloc[-1] > sma50.iloc[-1] else "bearish"

        return {
            "price": round(close.iloc[-1], 2),
            "rsi": round(rsi[-1], 1),
            "trend": trend
        }
    except:
        return None

# ---------------- OPENAI ----------------
def ask_gpt(user_id, message, market_data=None):

    system_prompt = """
Je bent een professionele beleggingsassistent.

BELANGRIJK:
- Je krijgt actuele realtime marktdata aangeleverd.
- Deze data is actueel en mag als live beschouwd worden.
- Zeg NOOIT dat je geen toegang hebt tot live data.
- Verwijs NOOIT naar een kennis-cutoff datum.

Gebruik uitsluitend de aangeleverde marktdata indien beschikbaar.
Je geeft GEEN financieel advies.
Leg duidelijk, praktisch en begrijpelijk uit.
"""

    messages = [{"role": "system", "content": system_prompt}]

    history = get_history(user_id, 5)
    for u, b in history:
        messages.append({"role": "user", "content": u})
        messages.append({"role": "assistant", "content": b})

    if market_data:
        market_context = f"""
Realtime marktdata:
Prijs: ${market_data['price']}
RSI: {market_data['rsi']}
Trend: {market_data['trend']}
"""
        messages.append({"role": "system", "content": market_context})

    messages.append({"role": "user", "content": message})

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            temperature=0.6,
            max_tokens=350
        )
        return response.choices[0].message.content
    except:
        return "⚠️ Er ging iets mis bij het analyseren van de markt."

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

# ---------------- ALERTS ----------------
def add_alert(user_id, ticker, rsi_threshold):
    with db.cursor() as c:
        c.execute("""
            INSERT INTO alerts(user_id, ticker, rsi_threshold)
            VALUES(%s,%s,%s)
            ON CONFLICT(user_id, ticker)
            DO UPDATE SET rsi_threshold = EXCLUDED.rsi_threshold;
        """, (user_id, ticker, rsi_threshold))

def get_alerts():
    with db.cursor() as c:
        c.execute("SELECT user_id, ticker, rsi_threshold FROM alerts")
        return c.fetchall()

# ---------------- CHAT HISTORY ----------------
def add_history(user_id, user_msg, bot_msg):
    with db.cursor() as c:
        c.execute(
            "INSERT INTO history (user_id, user_msg, bot_msg, created_at) VALUES (%s,%s,%s,%s)",
            (user_id, user_msg, bot_msg, datetime.now())
        )

def get_history(user_id, limit):
    with db.cursor() as c:
        c.execute("""
            SELECT user_msg, bot_msg 
            FROM history 
            WHERE user_id=%s 
            ORDER BY created_at DESC 
            LIMIT %s
        """, (user_id, limit))
        return c.fetchall()[::-1]

# ---------------- DAILY BRIEF ----------------
def daily_brief():
    tickers = ["AAPL","MSFT","NVDA","TSLA","SPY","BTC-USD"]
    lines = ["📊 *Dagelijkse Marktupdate*\n"]

    for t in tickers:
        d = get_technical_data(t)
        if d:
            emoji = "📈" if d["trend"] == "bullish" else "📉"
            lines.append(
                f"{emoji} *{t}*\n"
                f"Prijs: ${d['price']}\n"
                f"RSI: {d['rsi']}\n"
                f"Trend: {d['trend']}\n"
            )

    lines.append("⚠️ Dit is geen financieel advies.")
    return "\n".join(lines)

# ---------------- WHATSAPP ----------------
@app.route("/whatsapp", methods=["POST"])
def whatsapp():

    user = request.form.get("From")
    msg = request.form.get("Body").strip()
    reply = None

    lower = msg.lower()

    if lower.startswith("koop"):
        try:
            _, ticker, shares = msg.split()
            add_to_portfolio(user, ticker.upper(), float(shares))
            reply = f"✅ {shares} aandelen {ticker.upper()} toegevoegd."
        except:
            reply = "Gebruik: koop <TICKER> <AANTAL>"

    elif lower == "portfolio":
        pf = get_portfolio(user)
        if not pf:
            reply = "📂 Je portfolio is leeg."
        else:
            text = "📂 *Jouw Portfolio*\n\n"
            for t, s in pf:
                text += f"💹 *{t}*\nAantal: {s}\n\n"
            reply = text

    elif lower == "brief":
        reply = daily_brief()

    else:
        words = [w.upper() for w in msg.split() if w.isalpha()]
        market_data = None
        if words:
            market_data = get_technical_data(words[-1])
        reply = ask_gpt(user, msg, market_data)

    add_history(user, msg, reply)

    resp = MessagingResponse()
    send_long_message(resp, reply)
    return str(resp)

# ---------------- START ----------------
if __name__ == "__main__":
    app.run(debug=True)
