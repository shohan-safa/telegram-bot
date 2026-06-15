import os
import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ভ্যারিয়েবল চেক
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

user_memory = {}

SYSTEM_PROMPT = (
    "You are my dedicated, highly efficient, and intelligent Personal Assistant. "
    "Your tone should be helpful, brief, and professional. "
    "Always remember our previous context in this conversation."
)

def ask_openrouter(user_id, user_message):
    if user_id not in user_memory:
        user_memory[user_id] = [{"role": "system", "content": SYSTEM_PROMPT}]
    
    user_memory[user_id].append({"role": "user", "content": user_message})
    
    if len(user_memory[user_id]) > 11:
        user_memory[user_id] = [user_memory[user_id][0]] + user_memory[user_id][-10:]

    url = "https://openrouter.ai"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }
    
    # এখানে ওপেনরাউটারের সবচেয়ে স্টেবল ও ফ্রি/সস্তা মডেলটি দেওয়া হলো
    data = {
        "model": "google/gemini-2.5-flash", 
        "messages": user_memory[user_id]
    }

    try:
        response = requests.post(url, headers=headers, json=data)
        response_json = response.json()
        
        # যদি ওপেনরাউটার কোনো এরর দেয়, তা মেসেজে দেখাবে
        if 'error' in response_json:
            return f"ওপেনরাউটার এরর: {response_json['error'].get('message', 'Unknown error')}"
            
        bot_reply = response_json['choices'][0]['message']['content']
        user_memory[user_id].append({"role": "assistant", "content": bot_reply})
        return bot_reply
    except Exception as e:
        # আসল পাইথন এররটি চ্যাটে দেখার জন্য
        return f"কানেকশন এরর: {str(e)}"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_memory[user_id] = [{"role": "system", "content": SYSTEM_PROMPT}]
    await update.message.reply_text("হ্যালো! আমি আপনার পার্সোনাল অ্যাসিস্ট্যান্ট। কীভাবে সাহায্য করতে পারি?")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_text = update.message.text
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    reply = ask_openrouter(user_id, user_text)
    await update.message.reply_text(reply)

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()

if __name__ == '__main__':
    main()
