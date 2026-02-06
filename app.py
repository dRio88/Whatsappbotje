from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import openai
import os

app = Flask(__name__)

# OpenAI API key instellen via environment variable
openai.api_key = os.environ.get("OPENAI_API_KEY")

# Simpel geheugen per gebruiker (phone number)
chat_memory = {}

@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    user_number = request.form.get("From")  # unieke gebruiker
    msg = request.form.get("Body")

    # initialiseer geheugen voor deze gebruiker als nieuw
    if user_number not in chat_memory:
        chat_memory[user_number] = [
            {"role": "system", "content": "Je bent een grappige, korte, Nederlandstalige WhatsApp-assistent."}
        ]

    # voeg het nieuwe bericht toe
    chat_memory[user_number].append({"role": "user", "content": msg})

    # OpenAI call
    response = openai.ChatCompletion.create(
        model="gpt-4o-mini",  # je kunt ook gpt-5-nano proberen
        messages=chat_memory[user_number]
    )

    reply = response.choices[0].message.content

    # voeg antwoord toe aan geheugen
    chat_memory[user_number].append({"role": "assistant", "content": reply})

    # terugsturen naar WhatsApp
    resp = MessagingResponse()
    resp.message(reply)
    return str(resp)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
