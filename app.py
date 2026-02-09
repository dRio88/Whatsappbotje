from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import pandas as pd
import psycopg2
import os
import yfinance as yf
from datetime import datetime

app = Flask(__name__)
db = psycopg2.connect(os.environ["DATABASE_URL"])
db.autocommit = True

MAX_LEN = 1500  # max chars per WhatsApp bericht

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

# ---------------- PORTFOLIO ----------------
def add_to_portfolio(user, ticker, shares):
    with db.cursor() as c:
        c.execute("""
            INSERT INTO portfolios(user_id, ticker, shares)
            VALUES(%s,%s,%s)
            ON CONFLICT(user_id, ticker)
            DO UPDATE SET shares = portfolios.shares + EXCLUDED.shares;
        """, (user, ticker, shares))

def get_portfolio(user):
    with db.cursor() as c:
        c.execute("SELECT ticker, shares FROM portfolios WHERE user_id=%s", (user,))
        return c.fetchall()

# ---------------- CSV IMPORT ----------------
def import_csv_for_user(user_id, file):
    df = pd.read_csv(file)
    if not all(col in df.columns for col in ['Ticker','Shares']):
        raise ValueError("CSV moet kolommen 'Ticker' en 'Shares' bevatten")
    for _, row in df.iterrows():
        ticker = str(row['Ticker']).strip().upper()
        shares = float(row['Shares'])
        add_to_portfolio(user_id, ticker, shares)

# ---------------- CHAT HISTORY ----------------
def add_history(user, user_msg, bot_msg):
    with db.cursor() as c:
        c.execute("INSERT INTO history(user_id,user_msg,bot_msg) VALUES(%s,%s,%s)",
                  (user, user_msg, bot_msg))

# ---------------- MARKET DATA ----------------
def get_technical_data(ticker):
    df = yf.Ticker(ticker).history(period="3mo")
    if df.empty: return None
    close = df["Close"]
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    rs = gain.rolling(14).mean() / loss.rolling(14).mean()
    rsi = 100 - (100/(1+rs))
    return {
        "price": round(close.iloc[-1],2),
        "rsi": round(rsi.iloc[-1],1),
        "trend": "bullish" if close.iloc[-1]>close.mean() else "bearish"
    }

# ---------------- HELPER: SPLIT BERICHT ----------------
def send_long_message(resp, text):
    """Verdeel lange tekst in meerdere WhatsApp-berichten"""
    for i in range(0, len(text), MAX_LEN):
        resp.message(text[i:i+MAX_LEN])

# ---------------- WHATSAPP ----------------
@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    user = request.form.get("From")  # WhatsApp nummer = user_id
    msg = request.form.get("Body").strip()
    reply = ""

    if msg.lower().startswith("koop"):
        try:
            _, ticker, shares = msg.split()
            add_to_portfolio(user, ticker.upper(), float(shares))
            reply = f"✅ Toegevoegd: {shares} aandelen {ticker.upper()}"
        except:
            reply = "⚠️ Gebruik: koop <TICKER> <AANTAL>"

    elif msg.lower().startswith("portfolio"):
        pf = get_portfolio(user)
        if not pf:
            reply = "📂 Je portfolio is leeg."
        else:
            reply = "📂 Je portfolio:\n"
            for t, s in pf:
                reply += f"💹 {t}: {s} aandelen\n"

    elif msg.lower().startswith("brief"):
        tickers = ["AAPL","MSFT","SPY","BTC-USD"]
        reply = "📊 Dagelijkse Marktupdate\n\n"
        for t in tickers:
            d = get_technical_data(t)
            if d:
                trend_emoji = "📈" if d["trend"]=="bullish" else "📉"
                reply += f"{trend_emoji} {t}: ${d['price']} | RSI {d['rsi']} | {d['trend']}\n"

    else:
        reply = "🤖 Gebruik 'koop <ticker> <aantal>', 'portfolio' of 'brief'"

    add_history(user, msg, reply)
    resp = MessagingResponse()
    send_long_message(resp, reply)
    return str(resp)

# ---------------- CSV UPLOAD ----------------
@app.route("/import-etoro", methods=["POST"])
def import_etoro():
    if "file" not in request.files or "from_whatsapp" not in request.form:
        return "Bestand en from_whatsapp verplicht", 400

    file = request.files["file"]
    user_id = request.form["from_whatsapp"]
    if file.filename == "":
        return "Geen bestand geselecteerd", 400

    try:
        import_csv_for_user(user_id, file)
        return f"✅ Portfolio bijgewerkt voor {user_id}", 200
    except Exception as e:
        return f"Fout bij import: {e}", 500

# ---------------- START ----------------
if __name__ == "__main__":
    app.run()
