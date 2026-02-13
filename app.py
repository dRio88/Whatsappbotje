import os
import re
from contextlib import contextmanager
from datetime import datetime

import numpy as np
import psycopg2
from flask import Flask, request
from openai import OpenAI
from twilio.twiml.messaging_response import MessagingResponse

# ---------------- CONFIG ----------------
app = Flask(__name__)
MAX_MESSAGE_CHARS = 900
DEFAULT_HISTORY_LIMIT = 8
BRIEF_TICKERS = ["AAPL", "MSFT", "NVDA", "TSLA", "SPY", "BTC-USD"]
COMMANDS = {"KOOP", "PORTFOLIO", "BRIEF", "HELP"}
MAX_AI_REPLY_CHARS = 780

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_FALLBACK_MODELS = [
    model.strip()
    for model in os.getenv("OPENAI_FALLBACK_MODELS", "gpt-4o,gpt-4.1-mini").split(",")
    if model.strip()
]

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY ontbreekt")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL ontbreekt")

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
            CREATE TABLE IF NOT EXISTS portfolios (
                user_id TEXT,
                ticker TEXT,
                shares DOUBLE PRECISION,
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

# ---------------- HELPERS ----------------
def chunk_message(text: str, max_len: int = MAX_MESSAGE_CHARS):
    if not text:
        return [""]
    return [text[i:i+max_len] for i in range(0, len(text), max_len)]

def send_long_message(resp, text):
    for chunk in chunk_message(text):
        resp.message(chunk)

def shorten_reply(text: str, limit: int = MAX_AI_REPLY_CHARS):
    cleaned = (text or "").strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit-1].rstrip() + "…"

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
    if not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,14}", ticker):
        raise ValueError("Ticker ongeldig. Voorbeelden: AAPL, BTC-USD, BRK.B")
    raw_amount = parts[2].replace(",", ".")
    try:
        shares = float(raw_amount)
    except ValueError as exc:
        raise ValueError("Aantal moet een geldig getal zijn.") from exc
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
    if not ticker:
        return None
    try:
        import yfinance as yf
        frame = yf.Ticker(ticker).history(period="3mo")
        if frame.empty or len(frame) < 20:
            return None
        close = frame["Close"]
        delta = close.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.ewm(com=13, adjust=False).mean()
        avg_loss = loss.ewm(com=13, adjust=False).mean()
        rs = np.where(avg_loss == 0, np.inf, avg_gain / avg_loss)
        rsi = 100 - (100 / (1 + rs))
        sma20 = close.rolling(20).mean()
        sma50 = close.rolling(50).mean()
        trend = "📈 bullish" if close.iloc[-1] > sma50.iloc[-1] else "📉 bearish"
        return {
            "ticker": ticker,
            "price": round(float(close.iloc[-1]),2),
            "rsi": round(float(rsi[-1]),1),
            "sma20": round(float(sma20.iloc[-1]),2) if not np.isnan(sma20.iloc[-1]) else None,
            "sma50": round(float(sma50.iloc[-1]),2) if not np.isnan(sma50.iloc[-1]) else None,
            "trend": trend
        }
    except Exception as exc:
        print(f"[Market Error] {exc}")
        return None

# ---------------- AI ----------------
def build_system_prompt():
    return (
        "You are a witty but responsible investment assistant. "
        "Explain trends, RSI/SMA, diversification. "
        "Never give guaranteed returns. "
        "Always add a disclaimer."
    )

def build_user_prompt(message, market_data=None):
    if not market_data:
        return message
    return (
        f"{message}\n\n[Market Data]\n"
        f"Ticker: {market_data['ticker']}\n"
        f"Price: ${market_data['price']}\n"
        f"RSI(14): {market_data['rsi']}\n"
        f"SMA20: {market_data['sma20']}\n"
        f"SMA50: {market_data['sma50']}\n"
        f"Trend: {market_data['trend']}"
    )

def ask_gpt(user_id, message, market_data=None):
    system_prompt = build_system_prompt()
    history_lines = []
    try:
        for u,b in get_history(user_id):
            history_lines.append(f"User: {u}")
            history_lines.append(f"Assistant: {b}")
    except:
        pass
    prompt_text = build_user_prompt(message, market_data)
    conversation_text = "\n".join(history_lines + [f"User: {prompt_text}"])
    models_to_try = [OPENAI_MODEL] + [m for m in OPENAI_FALLBACK_MODELS if m != OPENAI_MODEL]
    for model_name in models_to_try:
        try:
            resp = client.responses.create(
                model=model_name,
                temperature=0.3,
                max_output_tokens=420,
                input=[
                    {"role":"system","content":[{"type":"input_text","text":system_prompt}]},
                    {"role":"user","content":[{"type":"input_text","text":conversation_text}]}
                ]
            )
            if getattr(resp,"output_text",None):
                return resp.output_text
        except Exception as exc:
            print(f"[TrendGuard AI Error] {exc}")
    return "⚠️ AI kon je vraag niet verwerken."

# ---------------- RESPONSE BUILDERS ----------------
def format_portfolio(rows):
    if not rows:
        return "📂 Je portfolio is leeg."
    lines = ["📂 *Jouw Portfolio*"]
    for ticker, shares in rows:
        if shares.is_integer():
            shares_text = str(int(shares))
        else:
            shares_text = f"{shares:.4f}".rstrip("0").rstrip(".")
        lines.append(f"💹 {ticker}: {shares_text} aandelen")
    lines.append("\n⚠️ Dit is geen financieel advies.")
    return "\n".join(lines)

def daily_brief():
    lines = ["📊 *Dagelijkse Marktupdate*\n"]
    for ticker in BRIEF_TICKERS:
        data = get_technical_data(ticker)
        if not data:
            continue
        lines.append(
            f"{data['trend']} *{ticker}*\nPrijs: ${data['price']}\nRSI: {data['rsi']}\nSMA20: {data['sma20']}\nSMA50: {data['sma50']}\n"
        )
    lines.append("⚠️ Dit is geen financieel advies.")
    return "\n".join(lines)

def help_text():
    return (
        "📚 *Beschikbare commando's*\n"
        "• portfolio\n• koop <TICKER> <AANTAL>\n• brief\n• help\n"
        "Voor vrije vragen: bv 'Wat vind je van NVDA trend?'\n"
    )

# ---------------- ROUTES ----------------
@app.route("/health", methods=["GET"])
def health():
    return {"status":"ok"},200

@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    user = request.form.get("From")
    msg_raw = request.form.get("Body") or ""
    resp = MessagingResponse()

    if not user:
        resp.message("⚠️ Kan je nummer niet detecteren.")
        return str(resp)

    msg = msg_raw.strip()
    if not msg:
        resp.message("⚠️ Geen bericht ontvangen.")
        return str(resp)

    msg_lower = msg.lower()
    try:
        if msg_lower == "portfolio":
            reply = format_portfolio(get_portfolio(user))
        elif msg_lower.startswith("koop"):
            try:
                ticker, shares = parse_buy_command(msg)
                add_to_portfolio(user, ticker, shares)
                reply = f"✅ {shares} aandelen {ticker} toegevoegd."
            except ValueError as exc:
                reply = f"⚠️ {exc}"
        elif msg_lower == "brief":
            reply = daily_brief()
        elif msg_lower == "help":
            reply = help_text()
        else:
            ticker = detect_ticker_from_text(msg)
            market_data = get_technical_data(ticker) if ticker else None
            reply = shorten_reply(ask_gpt(user,msg,market_data))
    except Exception as exc:
        print(f"[App Error] {exc}")
        reply = "⚠️ Er ging iets mis. Probeer later opnieuw."

    # History ops
    try:
        add_history(user, msg, reply)
    except Exception as exc:
        print(f"[History Write Error] {exc}")

    # Twilio: split lange berichten
    send_long_message(resp, reply)
    return str(resp)

if __name__ == "__main__":
    port = int(os.getenv("PORT","5000"))
    app.run(host="0.0.0.0", port=port)
