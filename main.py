import os
import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# টোকেন ও এপিআই কি (আপনার ভিপিএস এনভায়রনমেন্ট থেকে অটো পাবে)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

# বটের মেমোরি বা মনে রাখার ডিকশনারি
user_memory = {}

# পার্সোনাল অ্যাসিস্ট্যান্টের নির্দেশনাবলী
SYSTEM_PROMPT = (
    "You are my dedicated, highly efficient, and intelligent Personal Assistant. "
    "Your tone should be helpful, brief, and professional. "
    "Always remember our previous context in this conversation."
)

def ask_openrouter(user_id, user_message):
    # ইউজার প্রথম মেসেজ দিলে অ্যাসিস্ট্যান্ট প্রম্পট সেট করা
    if user_id not in user_memory:
        user_memory[user_id] = [{"role": "system", "content": SYSTEM_PROMPT}]
    
    # নতুন মেসেজ মেমোরিতে যোগ করা
    user_memory[user_id].append({"role": "user", "content": user_message})
    
    # মেমোরি বেশি বড় হতে না দেওয়া (টোকেন বাঁচাতে শেষ ১০টি চ্যাট রাখবে)
    if len(user_memory[user_id]) > 11:
        user_memory[user_id] = [user_memory[user_id][0]] + user_memory[user_id][-10:]

    url = "https://openrouter.ai"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }
    data = {
        "model": "google/gemini-2.5-flash", 
        "messages": user_memory[user_id]
    }

    try:
        response = requests.post(url, headers=headers, json=data)
        bot_reply = response.json()['choices']['message']['content']
        # বটের উত্তর মেমোরিতে যোগ করা
        user_memory[user_id].append({"role": "assistant", "content": bot_reply})
        return bot_reply
    except Exception as e:
        return "দুঃখিত, ওপেনরাউটার থেকে উত্তর পেতে সমস্যা হচ্ছে।"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    # চ্যাট হিস্ট্রি নতুন করে শুরু করা
    user_memory[user_id] = [{"role": "system", "content": SYSTEM_PROMPT}]
    await update.message.reply_text("হ্যালো! আমি আপনার পার্সোনাল অ্যাসিস্ট্যান্ট। কীভাবে সাহায্য করতে পারি?")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_text = update.message.text
    # টাইপিং স্ট্যাটাস দেখানো
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    reply = ask_openrouter(user_id, user_text)
    await update.message.reply_text(reply)

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()

if __name__ == '__main__':
    main()
