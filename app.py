from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import openai
import os

app = Flask(__name__)

openai.api_key = os.environ.get("OPENAI_API_KEY")

@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    msg = request.form.get("Body")

    response = openai.ChatCompletion.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": "Je bent een Grappige, korte, Nederlandstalige WhatsApp-assistent."
            },
            {
                "role": "user",
                "content": msg
            }
        ]
    )

    reply = response.choices[0].message.content

    resp = MessagingResponse()
    resp.message(reply)
    return str(resp)

if __name__ == "__main__":
    app.run()
