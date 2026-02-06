from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import openai
import os
import random

app = Flask(__name__)

# OpenAI API Key vanuit environment variable
openai.api_key = os.environ.get("OPENAI_API_KEY")

# Chat geheugen per gebruiker (telefoonnummer)
chat_memory = {}

# Helper functie om systeem prompt af te wisselen
def system_prompt():
    prompts = [
        "Je bent een grappige, korte, Nederlandstalige WhatsApp-assistent. Gebruik emoji en maak het leuk.",
        "Je bent een slimme, korte WhatsApp-bot die humor toevoegt.",
        "Je bent een creatieve WhatsApp-assistent, Nederlands, met grappige en vlotte reacties."
    ]
    return random.choice(prompts)

@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    user_number = request.form.get("From")  # telefoonnummer van de gebruiker
    msg = request.form.get("Body")  # bericht van de gebruiker

    # Zorg dat geheugen bestaat voor deze gebruiker
    if user_number not in chat_memory:
        chat_memory[user_number] = []

    # Voeg gebruiker bericht toe aan geheugen
    chat_memory[user_number].append({
        "role": "user",
        "content": msg
    })

    # OpenAI ChatCompletion call
    response = openai.ChatCompletion.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system_prompt()}
        ] + chat_memory[user_number]
    )

    reply = response.choices[0].message.content

    # Voeg assistant antwoord toe aan geheugen
    chat_memory[user_number].append({
        "role": "assistant",
        "content": reply
    })

    # Stuur antwoord terug naar WhatsApp
    resp = MessagingResponse()
    resp.message(reply)
    return str(resp)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
