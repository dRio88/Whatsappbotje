import os
import re
from datetime import datetime
from contextlib import contextmanager

import numpy as np
import psycopg2
from flask import Flask, request, Response, stream_with_context
from openai import OpenAI
from twilio.twiml.messaging_response import MessagingResponse

# ---------------- CONFIG ----------------
app = Flask(__name__)
MAX_MESSAGE_CHARS = 900
MAX_AI_REPLY_CHARS = 780
DEFAULT_HISTORY_LIMIT = 15  # multi-turn context
COMMANDS = {"KOOP", "PORTFOLIO", "BRIEF", "HELP", "TRENDGUARD"}

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

if not OPENAI_API_KEY or not DATABASE_URL:
    raise RuntimeError("OPENAI_API_KEY en DATABASE_URL moeten ingesteld zijn.")

client = OpenAI(api_key=OPENAI_API_KEY)
db = psycopg2.connect(DATABASE_URL)
db.autocommit = True

# ---------------- DATABASE ----------------
@contextmanager
def get_cursor():
    with db.cursor() as cursor:
        yield cursor

def init_db():
    with get_cursor() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS history (
                user_id TEXT,
                user_msg TEXT,
                bot_msg TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            );
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS portfolios (
                user_id TEXT,
                ticker TEXT,
                shares DOUBLE PRECISION,
                PRIMARY KEY(user_id, ticker)
            );
        """)

init_db()

# ---------------- HELPERS ----------------
def chunk_message(text, max_len=MAX_MESSAGE_CHARS):
    return [text[i:i+max_len] for i in range(0, len(text), max_len)]

def send_long_message(resp, text):
    for chunk in chunk_message(text):
        resp.message(chunk)

def shorten_reply(text, limit=MAX_AI_REPLY_CHARS):
    cleaned = (text or "").strip()
    return cleaned if len(cleaned) <= limit else cleaned[:limit-1].rstrip() + "…"

def add_history(user_id, user_msg, bot_msg):
    with get_cursor() as c:
        c.execute(
            "INSERT INTO history(user_id, user_msg, bot_msg, created_at) VALUES(%s,%s,%s,%s)",
            (user_id, user_msg, bot_msg, datetime.utcnow())
        )

def get_history(user_id, limit=DEFAULT_HISTORY_LIMIT):
    with get_cursor() as c:
        c.execute(
            "SELECT user_msg, bot_msg FROM history WHERE user_id=%s ORDER BY created_at DESC LIMIT %s",
            (user_id, limit)
        )
        return c.fetchall()[::-1]

def parse_buy_command(message):
    parts = message.strip().split()
    if len(parts) != 3:
        raise ValueError("Gebruik: koop <TICKER> <AANTAL>")
    ticker = parts[1].upper()
    raw_amount = parts[2].replace(",", ".")
    try:
        shares = float(raw_amount)
    except ValueError:
        raise ValueError("Aantal moet een geldig getal zijn.")
    if shares <= 0:
        raise ValueError("Aantal moet groter zijn dan 0.")
    return ticker, shares

def detect_ticker_from_text(message):
    matches = re.findall(r"\b[A-Za-z][A-Za-z0-9.-]{0,14}\b", message or "")
    for token in reversed(matches):
        candidate = token.upper()
        if candidate in COMMANDS:
            continue
        if re.fullmatch(r"[A-Z][A-Z0-9.-]{0,14}", candidate):
            return candidate
    return None

# ---------------- PORTFOLIO ----------------
def add_to_portfolio(user_id, ticker, shares):
    with get_cursor() as c:
        c.execute("""
            INSERT INTO portfolios(user_id, ticker, shares)
            VALUES(%s,%s,%s)
            ON CONFLICT(user_id, ticker)
            DO UPDATE SET shares = portfolios.shares + EXCLUDED.shares;
        """, (user_id, ticker, shares))

def get_portfolio(user_id):
    with get_cursor() as c:
        c.execute("SELECT ticker, shares FROM portfolios WHERE user_id=%s ORDER BY ticker ASC", (user_id,))
        return c.fetchall()

# ---------------- MARKET DATA ----------------
def get_technical_data(ticker):
    try:
        import yfinance as yf
        df = yf.Ticker(ticker).history(period="3mo")
        if df.empty or len(df) < 20:
            return None
        close = df["Close"]
        sma50 = close.rolling(50).mean()
        trend = "📈 bullish" if close.iloc[-1] > sma50.iloc[-1] else "📉 bearish"
        return {
            "ticker": ticker,
            "price": round(float(close.iloc[-1]),2),
            "trend": trend
        }
    except:
        return None

# ---------------- AI / TrendGuard ----------------
def build_system_prompt():
    return (
        "Je bent TrendGuard, een slimme, licht grappige assistent die portfolio-inzicht en risico-analyse geeft. "
        "Gebruik emoji's, wees OpenClaw-style interactief. "
        "Leg trends, volatiliteit, RSI/SMA uit en analyseer scenario's. "
        "Geen gegarandeerde returns, alleen educatieve info. Kort en praktisch."
    )

def build_user_prompt(message, market_data=None):
    if market_data:
        return (f"{message}\n[Market Data]\nTicker: {market_data['ticker']}\n"
                f"Price: ${market_data['price']}\nTrend: {market_data['trend']}")
    return message

def ask_trendguard(user_id, message, market_data=None):
    system_prompt = build_system_prompt()
    conversation = []
    for user_text, bot_text in get_history(user_id):
        conversation.append({"role":"user","content":user_text})
        conversation.append({"role":"assistant","content":bot_text})
    conversation.append({"role":"user","content":build_user_prompt(message, market_data)})

    try:
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role":"system","content":system_prompt}] + conversation,
            temperature=0.3,
            max_completion_tokens=600,
            stream=True  # <-- streaming output
        )
        buffer = ""
        for event in response:
            delta = event.choices[0].delta.get("content")
            if delta:
                buffer += delta
                yield buffer  # direct streaming
    except Exception as e:
        print(f"[TrendGuard AI Error] {e}")
        yield "⚠️ AI kon je vraag niet verwerken."

# ---------------- RESPONSE BUILDERS ----------------
def format_portfolio(rows):
    if not rows:
        return "📂 Je portfolio is leeg. Tijd om je watchlist spieren te trainen 💪📈"
    lines = ["📂 *Jouw Portfolio*"]
    for ticker, shares in rows:
        shares_text = str(int(shares)) if shares.is_integer() else f"{shares:.4f}".rstrip("0").rstrip(".")
        lines.append(f"💹 {ticker}: {shares_text} aandelen")
    lines.append("\n⚠️ Dit is geen financieel advies.")
    return "\n".join(lines)

def help_text():
    return ("📚 *Beschikbare commando's*\n"
            "• /portfolio\n"
            "• /koop <TICKER> <AANTAL>\n"
            "• /trendguard <TICKER>\n"
            "• /help\n"
            "Je kan ook vrije vragen stellen zoals: 'Wat vind je van NVDA trend?' 😄")

# ---------------- ROUTES ----------------
@app.route("/health", methods=["GET"])
def health():
    return {"status":"ok"}, 200

@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    user = request.form.get("From")
    msg_raw = request.form.get("Body")
    if not user or not msg_raw:
        return "OK"

    msg = msg_raw.strip()
    msg_lower = msg.lower()

    try:
        if msg_lower.startswith("/portfolio"):
            reply = format_portfolio(get_portfolio(user))
            resp = MessagingResponse()
            send_long_message(resp, reply)
            return str(resp)

        elif msg_lower.startswith("/koop"):
            try:
                ticker, shares = parse_buy_command(msg)
                add_to_portfolio(user, ticker, shares)
                reply = f"✅ {shares} aandelen {ticker} toegevoegd."
            except ValueError as e:
                reply = f"⚠️ {e}"
            resp = MessagingResponse()
            send_long_message(resp, reply)
            return str(resp)

        elif msg_lower.startswith("/trendguard") or True:
            ticker = detect_ticker_from_text(msg)
            market_data = get_technical_data(ticker) if ticker else None
            resp = MessagingResponse()
            # Streaming generator
            def generate_response():
                text_buffer = ""
                for chunk in ask_trendguard(user, msg, market_data):
                    diff = chunk[len(text_buffer):]
                    if diff:
                        yield diff
                        text_buffer = chunk
            # Collect chunks into one message for Twilio
            reply = "".join(generate_response())
            send_long_message(resp, reply)
            add_history(user, msg, reply)
            return str(resp)

        elif msg_lower.startswith("/help"):
            reply = help_text()
            resp = MessagingResponse()
            send_long_message(resp, reply)
            return str(resp)

    except Exception as e:
        print(f"[App Error] {e}")
        resp = MessagingResponse()
        resp.message("⚠️ Er ging iets mis. Probeer later opnieuw.")
        return str(resp)

if __name__ == "__main__":
    port = int(os.getenv("PORT","5000"))
    app.run(host="0.0.0.0", port=port)
