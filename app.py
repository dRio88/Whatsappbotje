from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI
import yfinance as yf
import pandas as pd
import numpy as np
import psycopg2
import os
import json
from datetime import datetime

# ---------------- CONFIG ----------------
app = Flask(__name__)
client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
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
        c.execute("""
        CREATE TABLE IF NOT EXISTS history (
            user_id TEXT,
            user_msg TEXT,
            bot_msg TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        );
        """)

init_db()

def import_etoro_csv(user_id, file):
    """Leest Etoro CSV en update portfolio in Postgres"""
    import pandas as pd

    df = pd.read_csv(file)
    required_cols = ['Ticker', 'Shares']
    if not all(col.strip() in df.columns for col in required_cols):
        raise ValueError(f"CSV mist vereiste kolommen: {required_cols}")

    for _, row in df.iterrows():
        ticker = str(row['Ticker']).strip().upper()
        shares = float(row['Shares'])
        add_to_portfolio(user_id, ticker, shares)

    print(f"Portfolio succesvol bijgewerkt voor {user_id}!")

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

# ---------------- ETORO CSV INTEGRATIE ----------------
def import_etoro_csv(user_id, csv_path):
    """Leest Etoro CSV en update portfolio in Postgres"""
    df = pd.read_csv(csv_path)
    required_cols = ['Ticker', 'Shares']
    if not all(col in df.columns for col in required_cols):
        raise ValueError(f"CSV mist vereiste kolommen: {required_cols}")

    for _, row in df.iterrows():
        ticker = row['Ticker'].strip().upper()
        shares = float(row['Shares'])
        # Gebruik bestaande add_to_portfolio functie
        add_to_portfolio(user_id, ticker, shares)
    print(f"Portfolio succesvol bijgewerkt voor {user_id}!")

# ---------------- PORTFOLIO ----------------
def add_to_portfolio(user, ticker, shares):
    """Voegt aandelen toe of update bestaande hoeveelheid"""
    with db.cursor() as c:
        c.execute("""
            INSERT INTO portfolios (user_id, ticker, shares)
            VALUES (%s, %s, %s)
            ON CONFLICT (user_id, ticker)
            DO UPDATE SET shares = portfolios.shares + EXCLUDED.shares;
        """, (user, ticker, shares))

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

# ---------------- CHAT HISTORY ----------------
def add_history(user, user_msg, bot_msg):
    with db.cursor() as c:
        c.execute(
            "INSERT INTO history (user_id, user_msg, bot_msg) VALUES (%s,%s,%s)",
            (user, user_msg, bot_msg)
        )

def get_history(user, limit=10):
    with db.cursor() as c:
        c.execute(
            "SELECT user_msg, bot_msg FROM history WHERE user_id=%s ORDER BY created_at DESC LIMIT %s",
            (user, limit)
        )
        return c.fetchall()[::-1]  # Oudste eerst

# ---------------- WHATSAPP ----------------
@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    msg = request.form.get("Body").strip()
    user = request.form.get("From")

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

    # Opslaan in Postgres in plaats van Redis
    add_history(user, msg, reply)

    resp = MessagingResponse()
    resp.message(reply)
    return str(resp)

@app.route("/import-etoro", methods=["POST"])
def import_etoro():
    if "file" not in request.files:
        return "Geen CSV bestand meegegeven", 400

    file = request.files["file"]
    if file.filename == "":
        return "Geen bestand geselecteerd", 400

    try:
        # Gebruik user_id uit formulier, of fallback naar 'user_etoro'
        user_id = request.form.get("user_id", "user_etoro")
        import_etoro_csv(user_id, file)
        return f"Portfolio succesvol bijgewerkt voor {user_id}!", 200
    except Exception as e:
        return f"Fout bij import: {e}", 500

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
