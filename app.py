from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI
import os
import yfinance as yf
from tinydb import TinyDB, Query

app = Flask(__name__)

# OpenAI client
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

# Chat memory database
db = TinyDB('chat_memory.json')

def get_user_history(user_number):
    records = db.search(Query().user == user_number)
    return [{"role": r["role"], "content": r["content"]} for r in records]

def save_user_message(user_number, role, content):
    db.insert({"user": user_number, "role": role, "content": content})

@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    user_number = request.form.get("From")
    msg = request.form.get("Body")

    # Check for stock queries like "AAPL" or "prijs AAPL"
    if "AAPL" in msg.upper() or "prijs" in msg.lower():
        stock = "AAPL"
        data = yf.Ticker(stock).history(period="1d")
        if not data.empty:
            last_price = data['Close'][-1]
            reply = f"De laatste slotprijs van {stock} is ${last_price:.2f}"
        else:
            reply = "Sorry, ik kan de beursprijs nu niet ophalen."
    else:
        # Haal eerdere context op
        history = get_user_history(user_number)

        # Voeg huidige vraag toe
        history.append({"role": "user", "content": msg})

        # OpenAI chat call
        response = client.chat.completions.create(
            model="gpt-5-nano",
            messages=[
                {"role": "system", "content": "Je bent een grappige, korte, Nederlandstalige WhatsApp-assistent die context onthoudt."}
            ] + history
        )
        reply = response.choices[0].message.content

        # Sla gebruiker + bot antwoord op
        save_user_message(user_number, "user", msg)
        save_user_message(user_number, "assistant", reply)

    resp = MessagingResponse()
    resp.message(reply)
    return str(resp)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
