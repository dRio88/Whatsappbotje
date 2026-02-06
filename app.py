from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import openai
import os
import yfinance as yf

app = Flask(__name__)

openai.api_key = os.environ.get("OPENAI_API_KEY")

chat_memory = {}

@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    user_number = request.form.get("From")
    msg = request.form.get("Body")

    if user_number not in chat_memory:
        chat_memory[user_number] = [
            {"role": "system", "content": "Je bent een grappige, korte, Nederlandstalige WhatsApp-assistent."}
        ]

    chat_memory[user_number].append({"role": "user", "content": msg})

    # Check op aandelen commando: bv. "AAPL prijs"
    if "prijs" in msg.lower():
        words = msg.split()
        ticker = None
        for w in words:
            if w.isupper() and len(w) <= 5:  # simpele detectie van tickers
                ticker = w
                break
        if ticker:
            try:
                stock = yf.Ticker(ticker)
                price = stock.info["regularMarketPrice"]
                reply = f"De huidige prijs van {ticker} is ${price}"
            except Exception as e:
                reply = f"Sorry, kon de prijs van {ticker} niet ophalen."
            resp = MessagingResponse()
            resp.message(reply)
            return str(resp)

    # Anders gebruik OpenAI zoals voorheen
    response = openai.ChatCompletion.create(
        model="gpt-4o-mini",
        messages=chat_memory[user_number]
    )

    reply = response.choices[0].message.content
    chat_memory[user_number].append({"role": "assistant", "content": reply})

    resp = MessagingResponse()
    resp.message(reply)
    return str(resp)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
