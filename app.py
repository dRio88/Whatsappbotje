from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import openai
import os

app = Flask(__name__)

# Zet je OpenAI API key in Environment variables op Render
openai.api_key = os.environ.get("OPENAI_API_KEY")

# Geheugen per gebruiker (tijdelijk, reset bij server restart)
chat_memory = {}

@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    incoming_msg = request.form.get("Body")
    user_number = request.form.get("From")  # uniek per gebruiker

    # Initialiseer geheugen voor nieuwe gebruiker
    if user_number not in chat_memory:
        chat_memory[user_number] = []

    # Voeg het nieuwe bericht toe
    chat_memory[user_number].append({"role": "user", "content": incoming_msg})

    # Bouw volledige prompt met context
    messages = [
        {
            "role": "system",
            "content": (
                "Je bent een slimme, grappige, creatieve en vriendelijke Nederlandstalige WhatsApp-assistent. "
                "Je antwoorden zijn kort, vriendelijk en soms met een vleugje humor."
            )
        }
    ]
    messages.extend(chat_memory[user_number])

    # OpenAI ChatCompletion request
    response = openai.ChatCompletion.create(
        model="gpt-4o-mini",  # Creatief, geschikt voor chat
        messages=messages,
        temperature=0.8,
        max_tokens=250
    )

    reply = response.choices[0].message.content.strip()

    # Voeg het antwoord toe aan geheugen
    chat_memory[user_number].append({"role": "assistant_]()_


