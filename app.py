from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI
import os
import re
import yfinance as yf
from tinydb import TinyDB, Query

app = Flask(__name__)

# OpenAI client
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

# Chat memory database
db = TinyDB("chat_memory.json")


# --------- MEMORY HELPERS ---------
def get_user_history(user_number):
    records = db.search(Query().user == user_number)
    return [{"role": r["role"], "content": r["content"]} for r in records]


def save_user_message(user_number, role, content):
    db.insert({
        "user": user_number,
        "role": role,
        "content": content
    })


# --------- WHATSAPP ENDPOINT ---------
@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    user_number = request.form.get("From")
    msg = request.form.get("Body", "").strip()

    # Altijd een response-object maken
    resp = MessagingResponse()

    # Veiligheid: leeg bericht
    if not msg:
        resp.message("🤖 Ik heb geen bericht ontvangen.")
        return str(resp)

    # --------- STOCK DETECTIE ---------
    # Match AAPL, $AAPL, MSFT, TSLA, enz.
    ticker_match = re.search(r'\$?[A-Z]{1,5}', msg.upper())
    ticker = ticker_match.group().replace("$", "") if ticker_match else None

    if ticker:
        try:
            data = yf.Ticker(ticker).history(period="1d")

            if not data.empty:
                last_price = data["Close"].iloc[-1]
                reply = f"📈 {ticker} staat op ${last_price:.2f}"
            else:
                reply = f"❌ Geen koersdata gevonden voor {ticker}"

        except Exception as e:
            reply = "⚠️ Er ging iets mis bij het ophalen van de beursprijs."

    else:
        # --------- CHAT MET CONTEXT ---------
        history = get_user_history(user_number)
        history.append({"role": "user", "content": msg})

        response = client.chat.completions.create(
            model="gpt-5-nano",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Je bent een grappige, korte, Nederlandstalige "
                        "WhatsApp-assistent die context onthoudt."
                    )
                }
            ] + history
        )

        reply = response.choices[0].message.content

        # Context opslaan
        save_user_message(user_number, "user", msg)
        save_user_message(user_number, "assistant", reply)

    resp.message(reply)
    return str(resp)


# --------- APP START ---------
if __name__ == "__main__":
    port = int(os.environ.get("PORT",
