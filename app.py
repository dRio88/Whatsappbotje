from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI
import yfinance as yf
import pandas as pd
import numpy as np # Niet direct gebruikt, maar kan handig zijn
import psycopg2
import os
import json # Niet direct gebruikt, maar kan handig zijn
from datetime import datetime

# Importeer dotenv als je die gebruikt voor het laden van omgevingsvariabelen
from dotenv import load_dotenv
load_dotenv() # Laadt variabelen uit.env bestand (indien aanwezig)

# ---------------- CONFIG ----------------
app = Flask(__name__)

# Controleer of de API sleutel is ingesteld, anders geef een duidelijke foutmelding
openai_api_key = os.environ.get("OPENAI_API_KEY")
if not openai_api_key:
    raise ValueError("OPENAI_API_KEY omgevingsvariabele is niet ingesteld.")
client = OpenAI(api_key=openai_api_key)

# Controleer of de DATABASE_URL is ingesteld
database_url = os.environ.get("DATABASE_URL")
if not database_url:
    raise ValueError("DATABASE_URL omgevingsvariabele is niet ingesteld.")
db = psycopg2.connect(database_url)
db.autocommit = True

# ---------------- DATABASE ----------------
def init_db():
    with db.cursor() as c:
        # Toegevoegd: PRIMARY KEY aan portfolios om duplicaten te voorkomen en UPDATE mogelijk te maken
        c.execute("""
        CREATE TABLE IF NOT EXISTS portfolios (
            user_id TEXT,
            ticker TEXT,
            shares FLOAT,
            PRIMARY KEY(user_id, ticker)
        );
        """)
        # Toegevoegd: PRIMARY KEY aan alerts om duplicaten te voorkomen en UPDATE mogelijk te maken
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

# ---------------- MARKET DATA ----------------
def get_technical_data(ticker):
    try:
        # Een kortere periode kan sneller zijn, 1mo is vaak genoeg voor recente trend/RSI
        df = yf.Ticker(ticker).history(period="3mo")
        if df.empty or 'Close' not in df.columns:
            print(f"DEBUG: Geen data gevonden voor ticker: {ticker} of 'Close' kolom ontbreekt.")
            return None

        close = df["Close"]
        # Zorg ervoor dat er genoeg data is voor de 14-daagse berekening
        if len(close) < 14:
            print(f"DEBUG: Onvoldoende data voor RSI berekening voor ticker: {ticker}")
            return None

        delta = close.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)

        # Bereken de gemiddelde gain en loss voor 14 perioden
        avg_gain = gain.ewm(com=13, adjust=False).mean() # Exponentially Weighted Moving Average is standaard voor RSI
        avg_loss = loss.ewm(com=13, adjust=False).mean()

        # Voorkom delen door nul
        rs = np.where(avg_loss == 0, np.inf, avg_gain / avg_loss)
        rsi = 100 - (100 / (1 + rs))

        # Zorg dat de laatste waardes worden genomen
        last_price = close.iloc[-1]
        last_rsi = rsi.iloc[-1]

        # Trend op basis van 50-dagen Simple Moving Average (SMA)
        # of een andere geschikte periode voor een 'bullish'/'bearish' indicatie
        sma_50 = close.rolling(window=50).mean()
        trend = "bullish" if last_price > sma_50.iloc[-1] else "bearish" if last_price < sma_50.iloc[-1] else "neutraal"
        
        return {
            "price": round(last_price, 2),
            "rsi": round(last_rsi, 1),
            "trend": trend
        }
    except Exception as e:
        print(f"Fout bij ophalen marktdata voor {ticker}: {e}")
        return None

# ---------------- OPENAI ----------------
def ask_gpt(user_id, message, market_data=None):
    system_prompt = """
Je bent een professionele beleggingsassistent.
Leg duidelijk, praktisch en begrijpelijk uit.
Je geeft GEEN financieel advies. Benadruk dit altijd indien relevant.
Gebruik marktdata indien beschikbaar en relevant voor de vraag van de gebruiker.
Als de gebruiker een vraag stelt over een ticker waarvoor geen marktdata beschikbaar is, zeg dan dat je geen data hebt voor die ticker.
"""
    # Haal de recente geschiedenis op voor betere context
    chat_history = get_history(user_id, limit=5) # Haal de laatste 5 conversaties op

    messages = [
        {"role": "system", "content": system_prompt},
    ]

    # Voeg de geschiedenis toe aan de messages array
    for user_msg, bot_msg in chat_history:
        messages.append({"role": "user", "content": user_msg})
        messages.append({"role": "assistant", "content": bot_msg})

    # Voeg de marktdata toe als deze beschikbaar is en relevant kan zijn
    if market_data:
        market_context = f"""
Actuele marktdata voor de besproken ticker:
Prijs: ${market_data['price']}
RSI (Relative Strength Index): {market_data['rsi']}
Trend (gebaseerd op 50-daags SMA): {market_data['trend']}
"""
        messages.append({"role": "system", "content": market_context}) # Als aanvullende info voor systeem

    messages.append({"role": "user", "content": message}) # De huidige vraag van de gebruiker

    try:
        # Modelnaam gecorrigeerd naar een bestaand model. Kies hier gpt-3.5-turbo of gpt-4o-mini voor efficiëntie
        # of gpt-4 voor hogere kwaliteit indien beschikbaar en budget toelaat.
        response = client.chat.completions.create(
            model="gpt-4o-mini", # of "gpt-3.5-turbo"
            messages=messages,
            temperature=0.7, # Balans tussen creativiteit en consistentie
            max_tokens=300 # Limiteer de lengte van het antwoord
        )
        return response.choices[0].message.content
    except Exception as e:
        print(f"Fout bij aanroepen OpenAI API: {e}")
        return "Sorry, ik kon je vraag nu niet beantwoorden door een probleem met mijn AI-brein."

# ---------------- PORTFOLIO ----------------
def add_to_portfolio(user_id, ticker, shares):
    with db.cursor() as c:
        # Gebruikt ON CONFLICT DO UPDATE voor optellen van aandelen
        c.execute("""
            INSERT INTO portfolios(user_id, ticker, shares)
            VALUES(%s, %s, %s)
            ON CONFLICT(user_id, ticker) DO UPDATE SET shares = portfolios.shares + EXCLUDED.shares;
        """, (user_id, ticker, shares))

def get_portfolio(user_id):
    with db.cursor() as c:
        # Optioneel: groep per ticker en tel op als er toch duplicaten zijn (redundant met PRIMARY KEY)
        c.execute(
            "SELECT ticker, SUM(shares) FROM portfolios WHERE user_id=%s GROUP BY ticker",
            (user_id,)
        )
        return c.fetchall()

# ---------------- ALERTS ----------------
def add_alert(user_id, ticker, rsi_threshold):
    with db.cursor() as c:
        # Gebruikt ON CONFLICT DO UPDATE voor bijwerken van alerts
        c.execute("""
            INSERT INTO alerts(user_id, ticker, rsi_threshold)
            VALUES(%s, %s, %s)
            ON CONFLICT(user_id, ticker) DO UPDATE SET rsi_threshold = EXCLUDED.rsi_threshold;
        """, (user_id, ticker, rsi_threshold))

def get_all_alerts():
    with db.cursor() as c:
        c.execute("SELECT user_id, ticker, rsi_threshold FROM alerts")
        return c.fetchall()

def remove_alert(user_id, ticker):
    with db.cursor() as c:
        c.execute("DELETE FROM alerts WHERE user_id = %s AND ticker = %s", (user_id, ticker))
        return c.rowcount > 0 # Geeft True als er een rij is verwijderd

def check_alerts():
    alerts_to_check = get_all_alerts()
    triggered = []
    for user_id, ticker, threshold in alerts_to_check:
        data = get_technical_data(ticker)
        if data and data["rsi"] < threshold:
            triggered.append((user_id, ticker, data["rsi"], threshold)) # Voeg threshold toe voor context
    return triggered

# ---------------- DAILY BRIEF ----------------
def daily_brief():
    # Uitgebreidere lijst met representatieve tickers
    tickers = ["AAPL", "MSFT", "GOOG", "AMZN", "NVDA", "TSLA", "SPY", "BTC-USD", "ETH-USD"]
    brief_lines = ["📊 Dagelijkse Marktupdate\n"]

    for t in tickers:
        d = get_technical_data(t)
        if d:
            brief_lines.append(f"{t}: ${d['price']} | RSI {d['rsi']} | {d['trend']}")
        else:
            brief_lines.append(f"{t}: Geen data beschikbaar.") # Melding voor ontbrekende data
    
    brief_lines.append("\nLet op: Dit is geen financieel advies.")
    return "\n".join(brief_lines)

# ---------------- CHAT HISTORY ----------------
def add_history(user_id, user_msg, bot_msg):
    with db.cursor() as c:
        c.execute(
            "INSERT INTO history (user_id, user_msg, bot_msg, created_at) VALUES (%s,%s,%s,%s)",
            (user_id, user_msg, bot_msg, datetime.now()) # Gebruik datetime.now() voor nauwkeurigheid
        )

def get_history(user_id, limit=10):
    with db.cursor() as c:
        c.execute(
            "SELECT user_msg, bot_msg FROM history WHERE user_id=%s ORDER BY created_at DESC LIMIT %s",
            (user_id, limit)
        )
        return c.fetchall()[::-1] # Oudste eerst voor chronologische context

# ---------------- WHATSAPP ----------------
@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    msg = request.form.get("Body").strip()
    user = request.form.get("From") # WhatsApp nummer = user_id
    reply = ""
    ticker_for_gpt = None # Variabele om geïdentificeerde ticker door te geven aan GPT

    # Functie om commando's te parsen en fouten af te handelen
    # Maakt de hoofdlogica overzichtelijker
    def handle_command(command_msg, user_id):
        lower_msg = command_msg.lower()
        if lower_msg.startswith("koop"):
            try:
                parts = command_msg.split()
                if len(parts) == 3:
                    ticker = parts[1].upper()
                    shares = float(parts[2])
                    add_to_portfolio(user_id, ticker, shares)
                    return f"✅ Toegevoegd: {shares} aandelen {ticker} aan je portfolio."
                else:
                    return "Gebruik: koop <TICKER> <AANTAL> (bijv. 'koop AAPL 5')"
            except ValueError:
                return "Ongeldig aantal aandelen. Gebruik: koop <TICKER> <AANTAL> (bijv. 'koop AAPL 5')"
            except Exception as e:
                print(f"Fout bij 'koop' commando: {e}")
                return "Er ging iets mis bij het toevoegen aan je portfolio."

        elif lower_msg.startswith("portfolio"):
            pf = get_portfolio(user_id)
            if not pf:
                return "📂 Je portfolio is leeg."
            else:
                portfolio_str = "📂 Je portfolio:\n"
                for t, s in pf:
                    portfolio_str += f"- {t}: {s} aandelen\n"
                return portfolio_str

        elif "waarschuw" in lower_msg:
            try:
                parts = lower_msg.split()
                # Zoek naar de ticker en drempelwaarde in de boodschap
                # Voorbeeld: "waarschuw mij als AAPL onder 30 rsi komt" of "waarschuw AAPL 30"
                # Vereenvoudigd voor "waarschuw <TICKER> <RSI_WAARDE>"
                if len(parts) >= 3 and parts[0] == "waarschuw":
                    ticker = parts[1].upper()
                    rsi_threshold = float(parts[2])
                    add_alert(user_id, ticker, rsi_threshold)
                    return f"⏰ Alert ingesteld: {ticker} bij RSI < {rsi_threshold}. Dit vervangt eventuele eerdere alerts voor {ticker}."
                else:
                    return "Gebruik: waarschuw <TICKER> <RSI_WAARDE> (bijv. 'waarschuw AAPL 30')"
            except ValueError:
                return "Ongeldige RSI waarde. Gebruik: waarschuw <TICKER> <RSI_WAARDE> (bijv. 'waarschuw AAPL 30')"
            except Exception as e:
                print(f"Fout bij 'waarschuw' commando: {e}")
                return "Er ging iets mis bij het instellen van de alert."

        elif lower_msg == "brief":
            return daily_brief()

        elif lower_msg.startswith("verwijder alert"):
            try:
                parts = lower_msg.split()
                if len(parts) == 3:
                    ticker = parts[2].upper()
                    if remove_alert(user_id, ticker):
                        return f"❌ Alert voor {ticker} verwijderd."
                    else:
                        return f"Geen actieve alert gevonden voor {ticker} om te verwijderen."
                else:
                    return "Gebruik: verwijder alert <TICKER>"
            except Exception as e:
                print(f"Fout bij 'verwijder alert' commando: {e}")
                return "Er ging iets mis bij het verwijderen van de alert."

        return None # Geen commando herkend

    # Probeer eerst een commando af te handelen
    reply = handle_command(msg, user)

    if reply is None:
        # Als het geen specifiek commando was, stuur het naar GPT
        # Probeer een ticker te detecteren in de boodschap voor marktdata context
        potential_tickers = [word.upper() for word in msg.split() if len(word) > 1 and word.isalpha()]
        market_data = None
        
        # Simpele heuristiek: de laatst genoemde potentiële ticker
        if potential_tickers:
            ticker_for_gpt = potential_tickers[-1]
            market_data = get_technical_data(ticker_for_gpt)
            if not market_data:
                # Als de ticker niet werkt met yfinance, reset deze dan
                ticker_for_gpt = None

        reply = ask_gpt(user, msg, market_data)

    # Opslaan in Postgres
    add_history(user, msg, reply)

    resp = MessagingResponse()
    resp.message(reply)
    return str(resp)

# ---------------- ALERT CRON ENDPOINT ----------------
@app.route("/check-alerts")
def alerts_cron():
    triggered_alerts = check_alerts()
    if not triggered_alerts:
        return "Geen alerts geactiveerd."

    # Optioneel: Stuur daadwerkelijk een WhatsApp bericht naar de gebruiker
    # Hiervoor heb je Twilio's API nodig (niet MessagingResponse, maar Client API)
    # Dit is alleen een voorbeeld van logging
    response_messages = []
    for user_id, ticker, current_rsi, threshold in triggered_alerts:
        alert_msg = f"🔔 ALERT: {ticker} RSI is nu {current_rsi}, wat onder je ingestelde drempel van {threshold} is! Dit is geen financieel advies."
        print(f"Stuur alert naar {user_id}: {alert_msg}")
        response_messages.append(f"Alert: {user_id} - {ticker} RSI {current_rsi} (onder {threshold})")
        # Hier zou je de Twilio Client API aanroepen om daadwerkelijk een bericht te sturen
        # from twilio.rest import Client
        # client = Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])
        # client.messages.create(
        # from_=f'whatsapp:{os.environ["TWILIO_WHATSAPP_NUMBER"]}',
        # body=alert_msg,
        # to=user_id
        # )
        
    return "<br>".join(response_messages)

# ---------------- START ----------------
if __name__ == "__main__":
    app.run(debug=True) # debug=True is handig voor ontwikkeling, zet op False voor productie
