import os
import asyncio
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
    # এপিআই কী চেক
    if not OPENROUTER_API_KEY:
        return "ভুল: VPS-এ OPENROUTER_API_KEY সেট করা নেই বা খালি!"

    if user_id not in user_memory:
        user_memory[user_id] = [{"role": "system", "content": SYSTEM_PROMPT}]
    
    # ইউজার মেসেজ মেমোরিতে যুক্ত করা
    user_memory[user_id].append({"role": "user", "content": user_message})
    
    # মেমোরি লিমিট করা (সিস্টেম প্রম্পট ধরে রেখে শেষ ১০টি মেসেজ রাখা)
    if len(user_memory[user_id]) > 11:
        user_memory[user_id] = [user_memory[user_id][0]] + user_memory[user_id][-10:]

    # OpenRouter API Endpoint ঠিক করা
    url = "https://openrouter.ai/api/v1/chat/completions"
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
        
        # যদি রেসপন্স কোড ২০০ (সফল) না হয়
        if response.status_code != 200:
            return f"ওপেনরাউটার কানেকশন ব্যর্থ! স্ট্যাটাস কোড: {response.status_code}. টেক্সট: {response.text[:100]}"
            
        try:
            response_json = response.json()
        except ValueError:
            return f"ভুল রেসপন্স ফরম্যাট! ওপেনরাউটার থেকে JSON-এর পরিবর্তে অন্য কিছু পাওয়া গেছে। টেক্সট: {response.text[:100]}"
        
        if 'error' in response_json:
            return f"ওপেনরাউটার এরর: {response_json['error'].get('message', 'Unknown error')}"
            
        # Choices একটি list, তাই index 0 ব্যবহার করতে হবে
        bot_reply = response_json['choices'][0]['message']['content']
        user_memory[user_id].append({"role": "assistant", "content": bot_reply})
        return bot_reply
    except Exception as e:
        return f"কানেকশন/ক্র্যাশ এরর: {str(e)}"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_memory[user_id] = [{"role": "system", "content": SYSTEM_PROMPT}]
    await update.message.reply_text("হ্যালো! আমি আপনার পার্সোনাল অ্যাসিস্ট্যান্ট। কীভাবে সাহায্য করতে পারি?")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_text = update.message.text
    # typing অ্যাকশন পাঠানো
    if update.effective_chat:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    
    # Blocking synchronous API call-কে থ্রেডে রান করা যাতে অন্যান্য ইউজারের রিকোয়েস্ট ব্লক না হয়
    reply = await asyncio.to_thread(ask_openrouter, user_id, user_text)
    await update.message.reply_text(reply)

def main():
    if not BOT_TOKEN:
        print("Error: BOT_TOKEN is not set in environment variables.")
        return
    if not OPENROUTER_API_KEY:
        print("Error: OPENROUTER_API_KEY is not set in environment variables.")
        return
        
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Bot is starting...")
    app.run_polling()

if __name__ == '__main__':
    main()
