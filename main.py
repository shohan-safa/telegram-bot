"""
Personal Assistant Telegram Bot  (v2)
=====================================
Capabilities
------------
Chat (multi-model via OpenRouter) + tool/actions:
  • web_search        -> live info / news / prices
  • current_time      -> date & time (Bangladesh)
  • set_reminder      -> "10 মিনিট পরে মনে করিয়ে দাও"
  • list_reminders
  • send_email        -> send a mail            (needs EMAIL_* vars)
  • read_emails       -> read / check inbox      (needs EMAIL_* vars)
  • search_jobs       -> job search (bdjobs etc.)
  • shop_orders       -> your WooCommerce orders (needs WC_* vars)
  • shop_products     -> your WooCommerce products(needs WC_* vars)
Image editing (send a PHOTO with a caption):
  • "background remove"  -> background সরায় (free, local, heavy)
  • "resize 800" / "resize 800x600"
  • "crop"               -> center square crop

Environment variables
----------------------
Required:
  TELEGRAM_BOT_TOKEN, OPENROUTER_API_KEY
Optional (per feature):
  EMAIL_ADDRESS, EMAIL_APP_PASSWORD            (Gmail App Password; for send/read email)
  WC_URL, WC_KEY, WC_SECRET                    (WooCommerce REST API; for shop tools)

Install:  pip install -r requirements.txt
Run:      python main.py
"""

import os
import re
import json
import email
import asyncio
import imaplib
import smtplib
import datetime
from email.mime.text import MIMEText
from email.header import decode_header
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

# /model switcher. "openrouter/free" auto-picks a free, tool-capable model.
# NOTE: free model IDs rotate on OpenRouter. If a pick gives a 402/404 error,
# use "Auto (Free)" or check https://openrouter.ai/models?supported_parameters=tools
# Claude has NO free version -> it only works once you add credits.
MODELS = {
    "Auto (Free)":             "openrouter/free",
    "DeepSeek V3 (Free)":      "deepseek/deepseek-chat-v3-0324:free",
    "DeepSeek R1 (Free)":      "deepseek/deepseek-r1:free",
    "Llama 4 Scout (Free)":    "meta-llama/llama-4-scout:free",
    "Gemini Flash (Free)":     "google/gemini-2.0-flash-exp:free",
    "Gemma 3 12B (Free)":      "google/gemma-3-12b-it:free",
    "Claude Haiku (Paid)":     "anthropic/claude-3.5-haiku",
    "GPT-4o mini (Paid)":      "openai/gpt-4o-mini",
}
DEFAULT_MODEL = "openrouter/free"
MAX_TOKENS = 1024
MAX_TOOL_LOOPS = 5
HISTORY_TURNS = 10

SYSTEM_PROMPT = (
    "তুমি একজন বুদ্ধিমান, কাজের পার্সোনাল অ্যাসিস্ট্যান্ট। "
    "ব্যবহারকারী বাংলা, বাংলিশ বা ইংরেজিতে লিখতে পারে; তুমি সাধারণত পরিষ্কার বাংলায় "
    "সংক্ষিপ্তভাবে উত্তর দেবে, যদি না সে অন্য ভাষা চায়। প্রয়োজনে দেওয়া tool ব্যবহার করবে — "
    "তথ্য/খবরের জন্য web_search, চাকরির জন্য search_jobs, ইমেইল পড়তে read_emails, পাঠাতে send_email, "
    "দোকানের জন্য shop_orders/shop_products, মনে করাতে set_reminder। "
    "ছবি এডিট করতে হলে ব্যবহারকারীকে বলবে ছবিটি caption সহ পাঠাতে (যেমন 'background remove' বা 'resize 800')। "
    "কোনো তথ্য না জানলে বানিয়ে বলবে না — আগে web_search করবে।"
)

USER_STATE: dict[int, dict] = {}


def get_state(user_id: int) -> dict:
    if user_id not in USER_STATE:
        USER_STATE[user_id] = {"model": DEFAULT_MODEL, "history": []}
    return USER_STATE[user_id]


# ----------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------
def _decode_hdr(value: str) -> str:
    if not value:
        return ""
    out = ""
    for text, enc in decode_header(value):
        if isinstance(text, bytes):
            out += text.decode(enc or "utf-8", errors="ignore")
        else:
            out += text
    return out


def _wc_creds():
    return (os.environ.get("WC_URL"), os.environ.get("WC_KEY"), os.environ.get("WC_SECRET"))


def _wc_missing():
    return ("Shop data আনা যায়নি — WooCommerce API সেটআপ করা নেই। হোস্টে WC_URL, WC_KEY, WC_SECRET "
            "যোগ করো (WooCommerce > Settings > Advanced > REST API থেকে key বানাও)।")


def _email_creds():
    return (os.environ.get("EMAIL_ADDRESS"), os.environ.get("EMAIL_APP_PASSWORD"))


# ----------------------------------------------------------------------
# Text tools (used inside the LLM tool-calling loop)
# ----------------------------------------------------------------------
def tool_web_search(query: str) -> str:
    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=5))
        if not results:
            return "কোনো ফলাফল পাওয়া যায়নি।"
        return "\n".join(f"- {r.get('title')}: {r.get('body')} ({r.get('href')})" for r in results)
    except Exception as e:
        return f"Search error: {e}"


def tool_search_jobs(keyword: str, location: str = "Bangladesh") -> str:
    return tool_web_search(f"{keyword} job vacancy {location} bdjobs.com")


def tool_current_time() -> str:
    return datetime.datetime.now(BD_TZ).strftime("আজ %A, %d %B %Y — সময় %I:%M %p (Bangladesh)")


def tool_send_email(to: str, subject: str, body: str) -> str:
    sender, password = _email_creds()
    if not sender or not password:
        return "Email পাঠানো যায়নি — EMAIL_ADDRESS ও EMAIL_APP_PASSWORD সেট করা নেই।"
    try:
        msg = MIMEText(body)
        msg["Subject"], msg["From"], msg["To"] = subject, sender, to
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender, password)
            server.sendmail(sender, [to], msg.as_string())
        return f"Email পাঠানো হয়েছে → {to}"
    except Exception as e:
        return f"Email error: {e}"


def tool_read_emails(query: str = "recent", limit: int = 5) -> str:
    addr, pw = _email_creds()
    if not addr or not pw:
        return "Email পড়া যায়নি — EMAIL_ADDRESS ও EMAIL_APP_PASSWORD সেট করা নেই।"
    try:
        M = imaplib.IMAP4_SSL("imap.gmail.com")
        M.login(addr, pw)
        M.select("INBOX")
        crit = "UNSEEN" if (query or "").lower() in ("unread", "unseen", "new", "নতুন") else "ALL"
        _, data = M.search(None, crit)
        ids = data[0].split()
        if not ids:
            M.logout()
            return "কোনো মেইল পাওয়া যায়নি।"
        ids = ids[-limit:][::-1]
        out = []
        for i in ids:
            # PEEK = পড়া হিসেবে মার্ক হবে না
            _, msg_data = M.fetch(i, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            raw = msg_data[0][1].decode(errors="ignore")
            msg = email.message_from_string(raw)
            out.append(
                f"• {_decode_hdr(msg.get('Subject')) or '(no subject)'}\n"
                f"   — {_decode_hdr(msg.get('From'))} ({msg.get('Date', '')})"
            )
        M.logout()
        return "\n".join(out)
    except Exception as e:
        return f"Email read error: {e}"


def tool_shop_orders(status: str = "any", limit: int = 5) -> str:
    base, key, secret = _wc_creds()
    if not (base and key and secret):
        return _wc_missing()
    try:
        params = {"per_page": max(1, min(limit, 20))}
        if status and status != "any":
            params["status"] = status
        resp = requests.get(f"{base.rstrip('/')}/wp-json/wc/v3/orders",
                            params=params, auth=(key, secret), timeout=20)
        resp.raise_for_status()
        orders = resp.json()
        if not orders:
            return "কোনো order পাওয়া যায়নি।"
        return "\n".join(
            f"#{o['id']} — {o.get('status')} — {o.get('total')} {o.get('currency')} — "
            f"{o.get('billing', {}).get('first_name', '')}" for o in orders)
    except Exception as e:
        return f"Shop error: {e}"


def tool_shop_products(search: str = "", limit: int = 5) -> str:
    base, key, secret = _wc_creds()
    if not (base and key and secret):
        return _wc_missing()
    try:
        params = {"per_page": max(1, min(limit, 20))}
        if search:
            params["search"] = search
        resp = requests.get(f"{base.rstrip('/')}/wp-json/wc/v3/products",
                            params=params, auth=(key, secret), timeout=20)
        resp.raise_for_status()
        prods = resp.json()
        if not prods:
            return "কোনো product পাওয়া যায়নি।"
        return "\n".join(
            f"• {p.get('name')} — দাম: {p.get('price')} — stock: {p.get('stock_status')}"
            for p in prods)
    except Exception as e:
        return f"Shop error: {e}"


# ----------------------------------------------------------------------
# Reminders (need context + chat_id)
# ----------------------------------------------------------------------
async def reminder_callback(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    await context.bot.send_message(job.chat_id, f"⏰ মনে করিয়ে দিচ্ছি: {job.data}")


def schedule_reminder(context, chat_id: int, minutes: int, text: str) -> str:
    if minutes <= 0:
        return "মিনিট সংখ্যা ঠিক নেই।"
    context.job_queue.run_once(
        reminder_callback, when=minutes * 60, chat_id=chat_id, data=text,
        name=f"rem_{chat_id}_{datetime.datetime.now().timestamp()}")
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
# Tool schema + dispatcher
# ----------------------------------------------------------------------
def _fn(name, desc, props=None, required=None):
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props or {}, "required": required or []}}}

TOOLS = [
    _fn("web_search", "ইন্টারনেটে রিয়েল-টাইম তথ্য, খবর, দাম খুঁজতে।",
        {"query": {"type": "string"}}, ["query"]),
    _fn("search_jobs", "চাকরি/job খুঁজতে (বাংলাদেশ, bdjobs ইত্যাদি)।",
        {"keyword": {"type": "string"}, "location": {"type": "string"}}, ["keyword"]),
    _fn("current_time", "এখনকার তারিখ ও সময় (বাংলাদেশ)।"),
    _fn("set_reminder", "নির্দিষ্ট মিনিট পরে মনে করিয়ে দিতে।",
        {"minutes": {"type": "integer"}, "text": {"type": "string"}}, ["minutes", "text"]),
    _fn("list_reminders", "সক্রিয় reminder তালিকা।"),
    _fn("send_email", "একটি ইমেইল পাঠাতে।",
        {"to": {"type": "string"}, "subject": {"type": "string"}, "body": {"type": "string"}},
        ["to", "subject", "body"]),
    _fn("read_emails", "ইনবক্সের সাম্প্রতিক/নতুন ইমেইল পড়তে।",
        {"query": {"type": "string", "description": "recent বা unread"},
         "limit": {"type": "integer"}}),
    _fn("shop_orders", "WooCommerce দোকানের order দেখতে।",
        {"status": {"type": "string"}, "limit": {"type": "integer"}}),
    _fn("shop_products", "WooCommerce দোকানের product দেখতে/খুঁজতে।",
        {"search": {"type": "string"}, "limit": {"type": "integer"}}),
]


async def dispatch_tool(name: str, args: dict, context, chat_id: int) -> str:
    if name == "web_search":
        return await asyncio.to_thread(tool_web_search, args.get("query", ""))
    if name == "search_jobs":
        return await asyncio.to_thread(tool_search_jobs, args.get("keyword", ""),
                                       args.get("location", "Bangladesh"))
    if name == "current_time":
        return tool_current_time()
    if name == "send_email":
        return await asyncio.to_thread(tool_send_email, args.get("to", ""),
                                       args.get("subject", ""), args.get("body", ""))
    if name == "read_emails":
        return await asyncio.to_thread(tool_read_emails, args.get("query", "recent"),
                                       int(args.get("limit", 5) or 5))
    if name == "shop_orders":
        return await asyncio.to_thread(tool_shop_orders, args.get("status", "any"),
                                       int(args.get("limit", 5) or 5))
    if name == "shop_products":
        return await asyncio.to_thread(tool_shop_products, args.get("search", ""),
                                       int(args.get("limit", 5) or 5))
    if name == "set_reminder":
        return schedule_reminder(context, chat_id, int(args.get("minutes", 0) or 0), args.get("text", ""))
    if name == "list_reminders":
        return list_reminders_text(context, chat_id)
    return f"Unknown tool: {name}"


# ----------------------------------------------------------------------
# OpenRouter call (robust against 402 / non-JSON responses)
# ----------------------------------------------------------------------
def call_openrouter(model: str, messages: list, tools=None) -> dict:
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://t.me/your_bot",
        "X-Title": "Personal Assistant Bot",
    }
    payload = {"model": model, "messages": messages, "max_tokens": MAX_TOKENS}
    if tools:
        payload["tools"] = tools
    resp = requests.post(OPENROUTER_URL, headers=headers, json=payload, timeout=60)
    try:
        data = resp.json()
    except json.JSONDecodeError:
        raise RuntimeError(
            f"OpenRouter থেকে অপ্রত্যাশিত উত্তর (status {resp.status_code})। "
            "একটু পরে আবার চেষ্টা করো, বা /model দিয়ে অন্য model নাও।")
    if resp.status_code != 200 or "error" in data:
        msg = (data.get("error") or {}).get("message", f"status {resp.status_code}")
        if resp.status_code == 402:
            msg += "  → Free limit/credit শেষ। /model দিয়ে 'Auto (Free)' নাও বা $10 add করো।"
        raise RuntimeError(f"OpenRouter: {msg}")
    return data


# ----------------------------------------------------------------------
# Assistant loop
# ----------------------------------------------------------------------
async def run_assistant(update: Update, context, user_text: str) -> str:
    state = get_state(update.effective_user.id)
    chat_id = update.effective_chat.id
    model = state["model"]

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(state["history"])
    messages.append({"role": "user", "content": user_text})

    for _ in range(MAX_TOOL_LOOPS):
        data = await asyncio.to_thread(call_openrouter, model, messages, TOOLS)
        choice = data["choices"][0]["message"]
        messages.append(choice)

        tool_calls = choice.get("tool_calls")
        if not tool_calls:
            final = choice.get("content") or "..."
            state["history"].append({"role": "user", "content": user_text})
            state["history"].append({"role": "assistant", "content": final})
            state["history"] = state["history"][-(HISTORY_TURNS * 2):]
            return final

        for tc in tool_calls:
            fn = tc["function"]["name"]
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            result = await dispatch_tool(fn, args, context, chat_id)
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": str(result)})

    return "অনেকবার চেষ্টা করেও কাজটা শেষ করতে পারলাম না। একটু অন্যভাবে বলো।"


# ----------------------------------------------------------------------
# Image editing (free, local) — send a PHOTO with a caption
# ----------------------------------------------------------------------
def img_resize(in_path, out_path, width, height=None):
    from PIL import Image
    im = Image.open(in_path).convert("RGB")
    if height:
        im = im.resize((width, height))
    else:
        ratio = width / im.width
        im = im.resize((width, int(im.height * ratio)))
    im.save(out_path, "JPEG", quality=90)


def img_crop_square(in_path, out_path):
    from PIL import Image
    im = Image.open(in_path).convert("RGB")
    s = min(im.width, im.height)
    left, top = (im.width - s) // 2, (im.height - s) // 2
    im.crop((left, top, left + s, top + s)).save(out_path, "JPEG", quality=90)


def img_remove_bg(in_path, out_path):
    # rembg first run downloads a ~170MB model and needs decent RAM.
    from rembg import remove
    from PIL import Image
    remove(Image.open(in_path)).save(out_path)  # PNG with transparency


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    caption = (update.message.caption or "").lower()
    await update.effective_chat.send_action(ChatAction.UPLOAD_PHOTO)
    mid = update.message.message_id
    in_path = f"/tmp/in_{mid}.jpg"
    tg_file = await update.message.photo[-1].get_file()
    await tg_file.download_to_drive(in_path)

    bg_words = ("background", "bg", "remove", "ব্যাকগ্রাউন্ড", "ব্যাকগ্রাউন্ড সরা")
    dim = re.search(r"(\d{2,4})\s*[x×]\s*(\d{2,4})", caption)
    single = re.search(r"(\d{2,4})", caption)

    try:
        if any(w in caption for w in bg_words):
            out = f"/tmp/out_{mid}.png"
            await asyncio.to_thread(img_remove_bg, in_path, out)
            await update.message.reply_document(open(out, "rb"),
                                                caption="ব্যাকগ্রাউন্ড সরানো হয়েছে ✅")
        elif "resize" in caption or dim:
            if dim:
                w, h = int(dim.group(1)), int(dim.group(2))
            else:
                w, h = (int(single.group(1)) if single else 800), None
            out = f"/tmp/out_{mid}.jpg"
            await asyncio.to_thread(img_resize, in_path, out, w, h)
            label = f"{w}x{h}" if h else str(w)
            await update.message.reply_document(open(out, "rb"), caption=f"Resize হয়েছে → {label} ✅")
        elif "crop" in caption:
            out = f"/tmp/out_{mid}.jpg"
            await asyncio.to_thread(img_crop_square, in_path, out)
            await update.message.reply_document(open(out, "rb"), caption="Center square crop ✅")
        else:
            await update.message.reply_text(
                "ছবি পেয়েছি! caption-এ লেখো কী করব:\n"
                "• 'background remove' — ব্যাকগ্রাউন্ড সরাও\n"
                "• 'resize 800' বা 'resize 800x600'\n"
                "• 'crop' — চারকোনা crop")
    except Exception as e:
        await update.message.reply_text(
            f"ছবি প্রসেস করতে সমস্যা: {e}\n"
            "(background remove-এ অনেক RAM লাগে — হোস্টে কম হলে fail করতে পারে।)")


# ----------------------------------------------------------------------
# Telegram command/message handlers
# ----------------------------------------------------------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "হ্যালো! আমি তোমার পার্সোনাল অ্যাসিস্ট্যান্ট 🤖\n"
        "যা পারি: প্রশ্নের উত্তর, web search, চাকরি খোঁজা, ইমেইল পড়া/পাঠানো, "
        "reminder, দোকানের order/product, আর ছবি এডিট।\n\n"
        "/model — model পাল্টাও\n/reset — নতুন শুরু\n/reminders — reminder\n/help — সাহায্য")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "উদাহরণ:\n"
        "• 'ঢাকায় গ্রাফিক ডিজাইনার job খুঁজে দাও'\n"
        "• 'আমার নতুন ইমেইল চেক করো'\n"
        "• '১৫ মিনিট পরে নামাজের কথা মনে করিয়ে দাও'\n"
        "• 'আমার শপের শেষ ৫টা order দেখাও'\n"
        "• একটা ছবি পাঠাও, caption-এ লেখো 'background remove'\n\n"
        "Model পাল্টাতে /model")


async def model_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = get_state(update.effective_user.id)
    buttons = [[InlineKeyboardButton(("✅ " if mid == state["model"] else "") + label,
                                     callback_data=f"setmodel|{mid}")]
               for label, mid in MODELS.items()]
    await update.message.reply_text("কোন AI model ব্যবহার করবে?",
                                    reply_markup=InlineKeyboardMarkup(buttons))


async def model_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, mid = query.data.split("|", 1)
    get_state(query.from_user.id)["model"] = mid
    label = next((k for k, v in MODELS.items() if v == mid), mid)
    await query.edit_message_text(f"Model সেট হয়েছে → {label}")


async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    get_state(update.effective_user.id)["history"] = []
    await update.message.reply_text("ঠিক আছে, আগের কথা মুছে ফেললাম।")


async def reminders_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(list_reminders_text(context, update.effective_chat.id))


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_chat.send_action(ChatAction.TYPING)
    try:
        reply = await run_assistant(update, context, update.message.text)
    except Exception as e:
        reply = f"দুঃখিত, সমস্যা হলো: {e}"
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
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    print("Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
