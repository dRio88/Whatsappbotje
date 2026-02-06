from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import openai
import os

app = Flask(__name__)

openai.api_key = os.environ.get("OPENAI_API_KEY")

@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    try:
        msg = request.form.get("Body")
        if not msg:
            return "No message body", 400

        response = openai.ChatCompletion.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Je bent een Grappige, korte, Nederlandstalige WhatsApp-assistent."},
                {"role": "user", "content": msg}
            ]
        )

        reply = response.choices[0].message.content

        resp = MessagingResponse()
        resp.message(reply)
        return str(resp)
    except Exception as e:
        print("ERROR:", e)
        return f"Internal server error: {e}", 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

