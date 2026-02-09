from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI
import yfinance as yf
import pandas as pd
import psycopg2
import os
from datetime import datetime
import threading
import time

# ---------------- CONFIG ----------------
app = Flask(__name__)
client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
db = psycopg2.connect(os.environ["DATABASE_URL"])
db.autocommit = True

# Pad naar CSV voor automatische import (optioneel)
ETORO_CSV_PATH = "etoro_portfolio.csv"
AUTO_IMPORT_INTERVAL = 24 * 60 * 60  # 24 uur

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

def get_portfolio(user):
    with db.cursor() as c:
        c.execute(
            "SELECT ticker, shares FROM portfolios WHERE user_id=%s",
            (user,)
        )
        return c.fetchall()

# ---------------- ETORO CSV INTEGRATIE ----------------
def import_etoro_csv(user_id, file):
    """Leest CSV en update portfolio voor de opgegeven gebruiker"""
    df = pd.read_csv(file)
    required_cols = ['Ticker', 'Shares']
    if not all(col.strip() in df.columns for col in required_cols):
        raise ValueError(f"CSV mist vereiste kolommen: {required_cols}")

    for _, row in df.iterrows():
        ticker = str(row['Ticker']).strip().upper()
        shares = float(row['Shares'])
        add_to_portfolio(user_id, ticker, shares)

    print(f"Portfolio succesvol bijgewerkt voor {user_id}!")

def auto_import_etoro():
    """Dagelijkse automatische import van CSV voor een vaste user (optioneel)"""
    while True:
        try:
            user_id = "user_etoro"
            with open(ETORO_CSV_PATH, "r") as f:
                import_etoro_csv(user_id, f)
            print(f"[Auto-import] Portfolio bijgewerkt voor {user_id}")
        except Exception as e:
            print(f"[Auto-import] Fout bij auto-import: {e}")
        time.sleep(AUTO_IMPORT_INTERVAL)

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

# ---------------- WHATSAPP ----------------
@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    msg = request.form.get("Body").strip()
    user = request.form.get("From")  # WhatsApp nummer wordt user_id

    reply = ""

    if msg.lower().startswith("koop"):
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
        parts = msg.split()
        ticker = parts[-2].upper()
        rsi = float(parts[-1])
        add_alert(user, ticker, rsi)
        reply = f"⏰ Alert ingesteld: {ticker} bij RSI < {rsi}"

    elif msg.lower() == "brief":
        reply = daily_brief()

    else:
        ticker = msg.split()[-1].upper()
        market = get_technical_data(ticker)
        reply = ask_gpt(user, msg, market)

    add_history(user, msg, reply)

    resp = MessagingResponse()
    resp.message(reply)
    return str(resp)

# ---------------- ETORO CSV UPLOAD ----------------
@app.route("/import-etoro", methods=["POST"])
def import_etoro():
    if "file" not in request.files:
        return "Geen CSV bestand meegegeven", 400

    file = request.files["file"]
    if file.filename == "":
        return "Geen bestand geselecteerd", 400

    try:
        # Koppel CSV automatisch aan WhatsApp user_id
        user_id = request.form.get("from_whatsapp")
        if not user_id:
            return "user_id ontbreekt. Geef exact WhatsApp From nummer.", 400

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

# ---------------- AUTOMATISCHE CSV IMPORT ----------------
def start_auto_import():
    thread = threading.Thread(target=auto_import_etoro, daemon=True)
    thread.start()

# ---------------- START ----------------
if __name__ == "__main__":
    start_auto_import()
    app.run()
