from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI
import pandas as pd
import numpy as np
import psycopg2
import os
import requests
from datetime import datetime

# ---------------- CONFIG ----------------
app = Flask(__name__)
MAX_LEN = 1500  # WhatsApp veilige limiet per bericht

# OpenAI setup
client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

# Postgres setup
db = psycopg2.connect(os.environ["DATABASE_URL"])
db.autocommit = True

# Base44 setup
BASE44_APP_ID = "698c993f605f6ce2ca5c8c85"
BASE44_API_KEY = "26f6dced974a4b6f9e808116aa25e243"
BASE44_BASE_URL = "https://app.base44.com/api"

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
        CREATE TABLE IF NOT EXISTS alerts (
            user_id TEXT,
            ticker TEXT,
            rsi_threshold FLOAT,
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

# ---------------- BASE44 PORTFOLIO ----------------
def fetch_base44_portfolio(user_id):
    """Haalt portfolio op van Base44 API"""
    url = f"{BASE44_BASE_URL}/apps/{BASE44_APP_ID}/functions/getPortfolio"
    headers = {
        "api_key": BASE44_API_KEY,
        "Content-Type": "application/json"
    }
    data = {"param1": user_id}  # Base44 verwacht parameter user_id
    try:
        response = requests.post(url, headers=headers, json=data)
        response.raise_for_status()
        return response.json().get("positions", [])
    except Exception as e:
        print(f"[BASE44 FETCH ERROR] {e}")
        return []

def sync_portfolio(user_id):
    """Sync Base44 portfolio naar lokale Postgres"""
    items = fetch_base44_portfolio(user_id)
    for item in items:
        ticker = item.get("ticker", "").upper()
        shares = float(item.get("shares", 0))
        if ticker and shares > 0:
            add_to_portfolio(user_id, ticker, shares)

def update_base44_portfolio(user_id, ticker, shares):
    """Stuur portfolio update naar Base44"""
    url = f"{BASE44_BASE_URL}/apps/{BASE44_APP_ID}/functions/updatePortfolio"
    headers = {
        "api_key": BASE44_API_KEY,
        "Content-Type": "application/json"
    }
    data = {"user_id": user_id, "ticker": ticker, "shares": shares}
    try:
        response = requests.post(url, headers=headers, json=data)
        response.raise_for_status()
        print(f"[BASE44 SYNC] {ticker} {shares} aandelen bijgewerkt voor {user_id}")
    except Exception as e:
        print(f"[BASE44 UPDATE ERROR] {e}")

# ---------------- PORTFOLIO ----------------
def add_to_portfolio(user_id, ticker, shares):
    with db.cursor() as c:
        c.execute("""
            INSERT INTO portfolios(user_id, ticker, shares)
            VALUES(%s,%s,%s)
            ON CONFLICT(user_id, ticker)
            DO UPDATE SET shares = portfolios.shares + EXCLUDED.shares;
        """, (user_id, ticker, shares))
    update_base44_portfolio(user_id, ticker, shares)  # sync direct

def get_portfolio(user_id):
    with db.cursor() as c:
        c.execute("SELECT ticker, shares FROM portfolios WHERE user_id=%s", (user_id,))
        return c.fetchall()

# ---------------- HELPER ----------------
def send_long_message(resp, text):
    for i in range(0, len(text), MAX_LEN):
        resp.message(text[i:i+MAX_LEN])

# ---------------- WHATSAPP ----------------
@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    user = request.form.get("From")
    msg = request.form.get("Body").strip()
    lower = msg.lower()
    reply = ""

    if lower == "portfolio":
        sync_portfolio(user)  # haal eerst Base44 portfolio op
        pf = get_portfolio(user)
        if not pf:
            reply = "📂 Je portfolio is leeg."
        else:
            reply = "📂 *Jouw Portfolio*\n"
            for t, s in pf:
                reply += f"💹 {t}: {s} aandelen\n"

    elif lower.startswith("koop"):
        try:
            _, ticker, shares = msg.split()
            shares = float(shares)
            add_to_portfolio(user, ticker.upper(), shares)
            reply = f"✅ {shares} aandelen {ticker.upper()} toegevoegd."
        except:
            reply = "Gebruik: koop <TICKER> <AANTAL>"

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

# ---------------- MARKET DATA ----------------
def get_technical_data(ticker):
    try:
        import yfinance as yf
        df = yf.Ticker(ticker).history(period="3mo")
        if df.empty or len(df) < 20: return None
        close = df["Close"]
        delta = close.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.ewm(com=13, adjust=False).mean()
        avg_loss = loss.ewm(com=13, adjust=False).mean()
        rs = np.where(avg_loss==0, np.inf, avg_gain/avg_loss)
        rsi = 100 - (100 / (1+rs))
        sma50 = close.rolling(50).mean()
        trend = "📈 bullish" if close.iloc[-1] > sma50.iloc[-1] else "📉 bearish"
        return {"price": round(close.iloc[-1],2), "rsi": round(rsi[-1],1), "trend": trend}
    except:
        return None

# ---------------- OPENAI ----------------
def ask_gpt(user_id, message, market_data=None):
    system_prompt = """
Je bent een professionele beleggingsassistent.
BELANGRIJK:
- Gebruik uitsluitend de aangeleverde marktdata.
- Geef GEEN financieel advies.
"""
    messages = [{"role":"system","content":system_prompt}]
    for u,b in get_history(user_id,5):
        messages.append({"role":"user","content":u})
        messages.append({"role":"assistant","content":b})
    if market_data:
        messages.append({"role":"system","content":f"Marktdata:\nPrijs: ${market_data['price']}\nRSI: {market_data['rsi']}\nTrend: {market_data['trend']}"})
    messages.append({"role":"user","content":message})
    try:
        resp = client.chat.completions.create(model="gpt-4o-mini", messages=messages, temperature=0.6, max_tokens=350)
        return resp.choices[0].message.content
    except:
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
        c.execute("INSERT INTO history(user_id,user_msg,bot_msg,created_at) VALUES(%s,%s,%s,%s)", (user_id,u_msg,b_msg,datetime.now()))

def get_history(user_id, limit):
    with db.cursor() as c:
        c.execute("SELECT user_msg,bot_msg FROM history WHERE user_id=%s ORDER BY created_at DESC LIMIT %s",(user_id,limit))
        return c.fetchall()[::-1]

# ---------------- START ----------------
if __name__ == "__main__":
    app.run(debug=True)
