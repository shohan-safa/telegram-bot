"""
Personal Assistant Telegram Bot
================================
- Multi-model via OpenRouter. Default model is "openrouter/free" which auto-selects
  a FREE model that supports the features your request needs (including tool calling).
  This fixes the 402 "requires more credits" error.
- Tool calling (the assistant can take ACTIONS): web search, reminders, current time,
  send email, WooCommerce shop orders. Easy to add more.
- Bengali / Banglish / English friendly. Replies in Bengali by default.

Required environment variables (set these on your host, e.g. Render):
    TELEGRAM_BOT_TOKEN   -> from @BotFather
    OPENROUTER_API_KEY   -> from https://openrouter.ai/keys

Optional (only needed for those specific tools):
    EMAIL_ADDRESS        -> your Gmail address       (for send_email)
    EMAIL_APP_PASSWORD   -> Gmail "App Password"      (for send_email)
    WC_URL               -> https://yourshop.com      (for shop_orders)
    WC_KEY               -> WooCommerce REST API key   (for shop_orders)
    WC_SECRET            -> WooCommerce REST API secret (for shop_orders)

Install:
    pip install -r requirements.txt
Run:
    python main.py
"""

import os
import json
import asyncio
import smtplib
import datetime
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters,
)

# ----------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

BD_TZ = ZoneInfo("Asia/Dhaka")

# Models the user can switch between with /model.
# "openrouter/free" auto-picks a FREE model that supports tools -> avoids 402.
# The named ":free" models below are the ones that handle tool calling reliably.
# The "(Paid)" ones need a credit balance but are the most stable for actions.
MODELS = {
    "Auto (Free)":              "openrouter/free",
    "DeepSeek V3 (Free)":       "deepseek/deepseek-chat-v3-0324:free",
    "Llama 4 Maverick (Free)":  "meta-llama/llama-4-maverick:free",
    "Qwen3 235B (Free)":        "qwen/qwen3-235b-a22b:free",
    "GPT-4o mini (Paid)":       "openai/gpt-4o-mini",
    "Gemini Flash (Paid)":      "google/gemini-2.0-flash-001",
}
DEFAULT_MODEL = "openrouter/free"
MAX_TOKENS = 1024          # lower this if you hit free-tier credit limits
MAX_TOOL_LOOPS = 5         # how many tool round-trips per message
HISTORY_TURNS = 10         # remember last N exchanges per user

SYSTEM_PROMPT = (
    "তুমি একজন বুদ্ধিমান, কাজের পার্সোনাল অ্যাসিস্ট্যান্ট। "
    "ব্যবহারকারী বাংলা, বাংলিশ বা ইংরেজিতে লিখতে পারে; তুমি সাধারণত পরিষ্কার বাংলায় "
    "(বাংলা হরফে) সংক্ষিপ্তভাবে উত্তর দেবে, যদি না সে অন্য ভাষায় উত্তর চায়। "
    "প্রয়োজনে দেওয়া tool গুলো ব্যবহার করবে — যেমন রিয়েল-টাইম তথ্য বা খবরের জন্য web_search, "
    "সময়ের জন্য current_time, মনে করানোর জন্য set_reminder, ইমেইলের জন্য send_email, "
    "দোকানের অর্ডারের জন্য shop_orders। "
    "কোনো তথ্য না জানলে আন্দাজে বানিয়ে বলবে না — দরকারে আগে web_search করবে।"
)

# ----------------------------------------------------------------------
# Per-user state (in memory; resets if the bot restarts)
# ----------------------------------------------------------------------
USER_STATE: dict[int, dict] = {}


def get_state(user_id: int) -> dict:
    if user_id not in USER_STATE:
        USER_STATE[user_id] = {"model": DEFAULT_MODEL, "history": []}
    return USER_STATE[user_id]


# ----------------------------------------------------------------------
# Tool implementations (the "actions")
# ----------------------------------------------------------------------
def tool_web_search(query: str) -> str:
    """Free web search via DuckDuckGo (no API key needed)."""
    try:
        try:
            from ddgs import DDGS          # current package name
        except ImportError:
            from duckduckgo_search import DDGS  # older name
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=5))
        if not results:
            return "কোনো ফলাফল পাওয়া যায়নি।"
        lines = []
        for r in results:
            lines.append(f"- {r.get('title')}: {r.get('body')} ({r.get('href')})")
        return "\n".join(lines)
    except Exception as e:
        return f"Search error: {e}"


def tool_current_time() -> str:
    now = datetime.datetime.now(BD_TZ)
    return now.strftime("আজ %A, %d %B %Y — সময় %I:%M %p (Bangladesh)")


def tool_send_email(to: str, subject: str, body: str) -> str:
    sender = os.environ.get("EMAIL_ADDRESS")
    password = os.environ.get("EMAIL_APP_PASSWORD")
    if not sender or not password:
        return ("Email পাঠানো যায়নি — সেটআপ করা নেই। হোস্টে EMAIL_ADDRESS এবং "
                "EMAIL_APP_PASSWORD (Gmail App Password) যোগ করো।")
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = to
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender, password)
            server.sendmail(sender, [to], msg.as_string())
        return f"Email পাঠানো হয়েছে → {to}"
    except Exception as e:
        return f"Email error: {e}"


def tool_shop_orders(status: str = "any", limit: int = 5) -> str:
    base = os.environ.get("WC_URL")
    key = os.environ.get("WC_KEY")
    secret = os.environ.get("WC_SECRET")
    if not (base and key and secret):
        return ("Shop data আনা যায়নি — WooCommerce API সেটআপ করা নেই। হোস্টে "
                "WC_URL, WC_KEY, WC_SECRET যোগ করো (WooCommerce > Settings > "
                "Advanced > REST API থেকে key বানাও)।")
    try:
        params = {"per_page": max(1, min(limit, 20))}
        if status and status != "any":
            params["status"] = status
        resp = requests.get(
            f"{base.rstrip('/')}/wp-json/wc/v3/orders",
            params=params, auth=(key, secret), timeout=20,
        )
        resp.raise_for_status()
        orders = resp.json()
        if not orders:
            return "কোনো order পাওয়া যায়নি।"
        lines = []
        for o in orders:
            name = o.get("billing", {}).get("first_name", "")
            lines.append(
                f"#{o['id']} — {o.get('status')} — "
                f"{o.get('total')} {o.get('currency')} — {name}"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"Shop error: {e}"


# ----------------------------------------------------------------------
# Reminders (need bot context + chat_id, so handled inline in the dispatcher)
# ----------------------------------------------------------------------
async def reminder_callback(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    await context.bot.send_message(job.chat_id, f"⏰ মনে করিয়ে দিচ্ছি: {job.data}")


def schedule_reminder(context, chat_id: int, minutes: int, text: str) -> str:
    if minutes <= 0:
        return "মিনিট সংখ্যা ঠিক নেই।"
    context.job_queue.run_once(
        reminder_callback,
        when=minutes * 60,
        chat_id=chat_id,
        data=text,
        name=f"rem_{chat_id}_{datetime.datetime.now().timestamp()}",
    )
    return f"{minutes} মিনিট পরে মনে করিয়ে দেব: {text}"


def list_reminders_text(context, chat_id: int) -> str:
    jobs = [j for j in context.job_queue.jobs() if getattr(j, "chat_id", None) == chat_id]
    if not jobs:
        return "কোনো সক্রিয় reminder নেই।"
    lines = []
    for j in jobs:
        when = j.next_t.astimezone(BD_TZ).strftime("%I:%M %p") if j.next_t else "?"
        lines.append(f"- {j.data} ({when})")
    return "\n".join(lines)


# ----------------------------------------------------------------------
# Tool schema (OpenAI / OpenRouter function-calling format) + dispatcher
# ----------------------------------------------------------------------
TOOLS = [
    {"type": "function", "function": {
        "name": "web_search",
        "description": "ইন্টারনেটে রিয়েল-টাইম তথ্য, খবর, দাম বা সাম্প্রতিক ঘটনা খুঁজতে।",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "যা খুঁজতে চাও"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "current_time",
        "description": "এখনকার তারিখ ও সময় (বাংলাদেশ) জানাতে।",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "set_reminder",
        "description": "নির্দিষ্ট মিনিট পরে ব্যবহারকারীকে মনে করিয়ে দিতে।",
        "parameters": {"type": "object", "properties": {
            "minutes": {"type": "integer", "description": "কত মিনিট পরে"},
            "text": {"type": "string", "description": "কী মনে করিয়ে দিতে হবে"}},
            "required": ["minutes", "text"]}}},
    {"type": "function", "function": {
        "name": "list_reminders",
        "description": "এখনকার সক্রিয় reminder গুলোর তালিকা।",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "send_email",
        "description": "একটি ইমেইল পাঠাতে।",
        "parameters": {"type": "object", "properties": {
            "to": {"type": "string"},
            "subject": {"type": "string"},
            "body": {"type": "string"}},
            "required": ["to", "subject", "body"]}}},
    {"type": "function", "function": {
        "name": "shop_orders",
        "description": "WooCommerce দোকানের সাম্প্রতিক order দেখতে।",
        "parameters": {"type": "object", "properties": {
            "status": {"type": "string", "description": "processing/completed/any"},
            "limit": {"type": "integer", "description": "কয়টা order"}}}}},
]


async def dispatch_tool(name: str, args: dict, context, chat_id: int) -> str:
    if name == "web_search":
        return await asyncio.to_thread(tool_web_search, args.get("query", ""))
    if name == "current_time":
        return tool_current_time()
    if name == "send_email":
        return await asyncio.to_thread(
            tool_send_email, args.get("to", ""), args.get("subject", ""), args.get("body", ""))
    if name == "shop_orders":
        return await asyncio.to_thread(
            tool_shop_orders, args.get("status", "any"), int(args.get("limit", 5) or 5))
    if name == "set_reminder":
        return schedule_reminder(context, chat_id, int(args.get("minutes", 0) or 0), args.get("text", ""))
    if name == "list_reminders":
        return list_reminders_text(context, chat_id)
    return f"Unknown tool: {name}"


# ----------------------------------------------------------------------
# OpenRouter call (robust against the 402 / non-JSON errors you were hitting)
# ----------------------------------------------------------------------
def call_openrouter(model: str, messages: list, tools=None) -> dict:
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        # Optional but recommended by OpenRouter:
        "HTTP-Referer": "https://t.me/your_bot",
        "X-Title": "Personal Assistant Bot",
    }
    payload = {"model": model, "messages": messages, "max_tokens": MAX_TOKENS}
    if tools:
        payload["tools"] = tools

    resp = requests.post(OPENROUTER_URL, headers=headers, json=payload, timeout=60)

    # OpenRouter usually returns JSON even on errors, but an HTML/empty body
    # is what caused your "Expecting value: line 1 column 1 (char 0)" crash.
    try:
        data = resp.json()
    except json.JSONDecodeError:
        raise RuntimeError(
            f"OpenRouter থেকে অপ্রত্যাশিত উত্তর (status {resp.status_code})। "
            "একটু পরে আবার চেষ্টা করো, বা /model দিয়ে অন্য model বেছে নাও।")

    if resp.status_code != 200 or "error" in data:
        msg = (data.get("error") or {}).get("message", f"status {resp.status_code}")
        if resp.status_code == 402:
            msg += ("  → Free limit শেষ বা credit নেই। /model দিয়ে 'Auto (Free)' বেছে নাও, "
                    "অথবা openrouter.ai এ একবার $10 add করো।")
        raise RuntimeError(f"OpenRouter: {msg}")
    return data


# ----------------------------------------------------------------------
# Assistant loop: ask model -> run tools -> ask again -> final answer
# ----------------------------------------------------------------------
async def run_assistant(update: Update, context, user_text: str) -> str:
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    state = get_state(user_id)
    model = state["model"]

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(state["history"])
    messages.append({"role": "user", "content": user_text})

    for _ in range(MAX_TOOL_LOOPS):
        data = await asyncio.to_thread(call_openrouter, model, messages, TOOLS)
        choice = data["choices"][0]["message"]
        messages.append(choice)  # assistant turn (may carry tool_calls)

        tool_calls = choice.get("tool_calls")
        if not tool_calls:
            final = choice.get("content") or "..."
            # Save a clean history (user + final only) for the next message.
            state["history"].append({"role": "user", "content": user_text})
            state["history"].append({"role": "assistant", "content": final})
            state["history"] = state["history"][-(HISTORY_TURNS * 2):]
            return final

        # Execute each requested tool and feed results back to the model.
        for tc in tool_calls:
            fn = tc["function"]["name"]
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            result = await dispatch_tool(fn, args, context, chat_id)
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": str(result),
            })

    return "অনেকবার চেষ্টা করেও কাজটা শেষ করতে পারলাম না। একটু অন্যভাবে বলো।"


# ----------------------------------------------------------------------
# Telegram handlers
# ----------------------------------------------------------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "হ্যালো! আমি তোমার পার্সোনাল অ্যাসিস্ট্যান্ট 🤖\n"
        "যা বলবে চেষ্টা করব করে দিতে — search, reminder, email, shop ইত্যাদি।\n\n"
        "/model — AI model পাল্টাও\n"
        "/reset — কথা মুছে নতুন শুরু\n"
        "/reminders — reminder তালিকা\n"
        "/help — সাহায্য"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "আমি যা পারি:\n"
        "• প্রশ্নের উত্তর ও আড্ডা\n"
        "• ইন্টারনেট থেকে খবর/তথ্য খোঁজা\n"
        "• X মিনিট পরে মনে করিয়ে দেওয়া (যেমন: '১০ মিনিট পরে পানি খেতে মনে করিয়ে দাও')\n"
        "• ইমেইল পাঠানো (সেটআপ লাগবে)\n"
        "• দোকানের order দেখা (সেটআপ লাগবে)\n\n"
        "Model পাল্টাতে /model"
    )


async def model_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = get_state(update.effective_user.id)
    buttons = []
    for label, mid in MODELS.items():
        mark = "✅ " if mid == state["model"] else ""
        buttons.append([InlineKeyboardButton(f"{mark}{label}", callback_data=f"setmodel|{mid}")])
    await update.message.reply_text(
        "কোন AI model ব্যবহার করবে?", reply_markup=InlineKeyboardMarkup(buttons))


async def model_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, mid = query.data.split("|", 1)
    state = get_state(query.from_user.id)
    state["model"] = mid
    label = next((k for k, v in MODELS.items() if v == mid), mid)
    await query.edit_message_text(f"Model সেট হয়েছে → {label}")


async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    get_state(update.effective_user.id)["history"] = []
    await update.message.reply_text("ঠিক আছে, আগের কথা মুছে ফেললাম। নতুন করে বলো।")


async def reminders_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(list_reminders_text(context, update.effective_chat.id))


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_chat.send_action(ChatAction.TYPING)
    try:
        reply = await run_assistant(update, context, update.message.text)
    except Exception as e:
        reply = f"দুঃখিত, সমস্যা হলো: {e}"
    # Telegram caps messages at 4096 chars.
    for i in range(0, len(reply), 4000):
        await update.message.reply_text(reply[i:i + 4000])


def main() -> None:
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("model", model_cmd))
    app.add_handler(CommandHandler("models", model_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.add_handler(CommandHandler("reminders", reminders_cmd))
    app.add_handler(CallbackQueryHandler(model_callback, pattern=r"^setmodel\|"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    print("Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
