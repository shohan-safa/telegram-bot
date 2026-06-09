import os
import requests
from telegram import Update
from telegram.ext import Application, MessageHandler, ContextTypes, filters

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# ---------- CHATGPT (OpenRouter) ----------
def ask_openrouter(prompt):
    r = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": "openai/gpt-4o-mini",
            "messages": [{"role": "user", "content": prompt}]
        }
    )
    return r.json()["choices"][0]["message"]["content"]

# ---------- GEMINI ----------
def ask_gemini(prompt):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-pro:generateContent?key={GEMINI_API_KEY}"

    r = requests.post(url, json={
        "contents": [{"parts": [{"text": prompt}]}]
    })

    return r.json()["candidates"][0]["content"]["parts"][0]["text"]

# ---------- ROUTER ----------
async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.lower()

    try:
        if text.startswith("gemini:"):
            reply = ask_gemini(text.replace("gemini:", "").strip())

        elif text.startswith("chatgpt:"):
            reply = ask_openrouter(text.replace("chatgpt:", "").strip())

        else:
            # default AI
            reply = ask_openrouter(text)

    except Exception as e:
        reply = f"Error: {str(e)}"

    await update.message.reply_text(reply)

app = Application.builder().token(BOT_TOKEN).build()
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))
app.run_polling()
