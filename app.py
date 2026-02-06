from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import openai
import os

app = Flask(__name__)

openai.api_key = os.environ.get("OPENAI_API_KEY")

# Simpel geheugen per gebruiker (telefoonnummer)
user_memory = {}

@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    msg = request.form.get("Body")
    from_number = request.form.get("From")  # WhatsApp-nummer van de gebruiker

    # Haal bestaande context op of start een nieuwe
    if from_number not in user_memory:
        user_memory[from_number] = [
            {"role": "system", "content": "Je bent een grappige, korte, Nederlandstalige WhatsApp-assistent."}
        ]

    # Voeg het nieuwe bericht toe aan de context
    user_memory[from_number].append({"role": "user", "content": msg})

    # Vraag antwoord bij OpenAI
    response = openai.ChatCompletion.create(
        model="gpt-4o-mini",
        messages=user_memory[from_number]
    )

    reply = response.choices[0].message.content

    # Voeg het AI-antwoord toe aan de context
    user_memory[from_number].append({"role": "assistant", "content": reply})

    resp = MessagingResponse()
    resp.message(reply)
    return str(resp)

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
