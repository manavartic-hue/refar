"""
WhatsApp Rotator Bot + Redirect Server
--------------------------------------
- Telegram bot: numbers manage karo, rotating redirect link banao (buttons ke saath)
- Web server: link open hone par round-robin se agle WhatsApp number par redirect
- Railway par ek saath chalta hai (bot polling + web server ek hi process me)

ENV VARIABLES (Railway -> Variables me set karo):
  BOT_TOKEN   = @BotFather se mila token (REQUIRED)
  BASE_URL    = tumhari Railway public URL, e.g. https://myapp.up.railway.app (REQUIRED)
  ADMIN_ID    = tumhara Telegram numeric user id (optional, security ke liye)
  PORT        = Railway automatically deta hai, chhedne ki zarurat nahi
"""

import os
import json
import logging
import asyncio
from urllib.parse import quote

from aiohttp import web

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ------------------------------------------------------------------ #
# Config
# ------------------------------------------------------------------ #
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("wa-rotator")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
BASE_URL = os.environ.get("BASE_URL", "").strip().rstrip("/")
ADMIN_ID = os.environ.get("ADMIN_ID", "").strip()
PORT = int(os.environ.get("PORT", "8080"))

DATA_FILE = "data.json"

DEFAULT_MESSAGE = "Hello"          # WhatsApp par pre-filled message
DEFAULT_MAX_LINKS = 100            # max links jo user ek baar me maang sakta hai
MIN_LINKS = 20                     # minimum links

# ------------------------------------------------------------------ #
# Simple JSON storage
# ------------------------------------------------------------------ #
def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "numbers": [],       # list of WhatsApp numbers, e.g. ["919812345678", ...]
        "counter": 0,        # round-robin pointer (kis number par redirect karna hai)
        "message": DEFAULT_MESSAGE,
        "max_links": DEFAULT_MAX_LINKS,
        "hits": 0,           # total kitni baar link open hua
    }


def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


DATA = load_data()


def is_admin(update: Update) -> bool:
    """Agar ADMIN_ID set hai to sirf wahi use kar paayega."""
    if not ADMIN_ID:
        return True
    return str(update.effective_user.id) == ADMIN_ID


# ------------------------------------------------------------------ #
# WEB SERVER  (redirect engine)
# ------------------------------------------------------------------ #
async def handle_root(request: web.Request):
    return web.Response(text="OK - WhatsApp Rotator is running.")


async def handle_redirect(request: web.Request):
    """
    /go  -> round-robin se agle number par WhatsApp redirect.
    Har hit par counter aage badhta hai, isliye same link bar-bar
    open karne par alag-alag number milta hai.
    """
    numbers = DATA.get("numbers", [])
    if not numbers:
        return web.Response(text="No numbers configured yet.", status=503)

    idx = DATA.get("counter", 0) % len(numbers)
    number = numbers[idx]

    DATA["counter"] = (DATA.get("counter", 0) + 1) % (10 ** 12)
    DATA["hits"] = DATA.get("hits", 0) + 1
    save_data(DATA)

    msg = DATA.get("message", DEFAULT_MESSAGE)
    wa_url = f"https://wa.me/{number}"
    if msg:
        wa_url += f"?text={quote(msg)}"

    log.info("Redirect hit -> number index %s (%s)", idx, number)
    raise web.HTTPFound(location=wa_url)


def build_web_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", handle_root)
    app.router.add_get("/go", handle_redirect)
    return app


# ------------------------------------------------------------------ #
# TELEGRAM BOT
# ------------------------------------------------------------------ #
def main_menu() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton("🔗 Links Banao (Generate)", callback_data="gen")],
        [InlineKeyboardButton("➕ Number Add karo", callback_data="add_help")],
        [InlineKeyboardButton("📋 Numbers dekho", callback_data="list")],
        [InlineKeyboardButton("🗑 Sab Numbers hatao", callback_data="clear")],
        [InlineKeyboardButton("⚙️ Max Links set karo", callback_data="setmax_help")],
        [InlineKeyboardButton("💬 Message set karo", callback_data="setmsg_help")],
        [InlineKeyboardButton("📊 Stats", callback_data="stats")],
    ]
    return InlineKeyboardMarkup(kb)


WELCOME = (
    "👋 *WhatsApp Rotator Bot*\n\n"
    "Ye bot ek rotating redirect link banata hai.\n"
    "Jab bhi koi link open karega, round-robin se *agle WhatsApp number* "
    "par chala jayega.\n\n"
    "*Kaise use kare:*\n"
    "1️⃣ Numbers add karo (`/add`)\n"
    "2️⃣ Links banao (button dabao ya `/gen 20`)\n"
    "3️⃣ Bas! Har click par alag number.\n\n"
    "Neeche buttons se sab control karo 👇"
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔ Access denied.")
        return
    await update.message.reply_text(
        WELCOME, parse_mode="Markdown", reply_markup=main_menu()
    )


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/add 919812345678 919876543210 ... (space ya newline se alag)"""
    if not is_admin(update):
        return
    text = " ".join(context.args) if context.args else ""
    if not text:
        await update.message.reply_text(
            "Numbers bhejo country code ke saath (bina + ke).\n\n"
            "Example:\n`/add 919812345678 919876543210`\n\n"
            "Ya seedha ek message me kai numbers (har line par ek) bhej do.",
            parse_mode="Markdown",
        )
        return
    added = _add_numbers(text)
    await update.message.reply_text(
        f"✅ {added} number add hue.\nAb total: *{len(DATA['numbers'])}*",
        parse_mode="Markdown",
        reply_markup=main_menu(),
    )


def _add_numbers(text: str) -> int:
    """Text me se numbers nikaalo, clean karo, add karo. Return: kitne naye add hue."""
    raw = text.replace(",", " ").replace("\n", " ").split()
    added = 0
    for r in raw:
        cleaned = "".join(ch for ch in r if ch.isdigit())
        if len(cleaned) >= 10 and cleaned not in DATA["numbers"]:
            DATA["numbers"].append(cleaned)
            added += 1
    if added:
        save_data(DATA)
    return added


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Plain text message handle karo — mode ke hisaab se
    (numbers add / max set / message set), warna numbers samajh kar add karo.
    """
    if not is_admin(update):
        return

    mode = context.user_data.get("mode")
    text = update.message.text.strip()

    if mode == "setmax":
        context.user_data["mode"] = None
        if text.isdigit() and int(text) >= MIN_LINKS:
            DATA["max_links"] = int(text)
            save_data(DATA)
            await update.message.reply_text(
                f"✅ Max links set: *{DATA['max_links']}*",
                parse_mode="Markdown", reply_markup=main_menu(),
            )
        else:
            await update.message.reply_text(
                f"❌ Kam se kam {MIN_LINKS} daalo. Dubara try karo (button dabao).",
                reply_markup=main_menu(),
            )
        return

    if mode == "setmsg":
        context.user_data["mode"] = None
        DATA["message"] = text
        save_data(DATA)
        await update.message.reply_text(
            f"✅ WhatsApp message set:\n_{text}_",
            parse_mode="Markdown", reply_markup=main_menu(),
        )
        return

    # default: numbers add karo
    added = _add_numbers(text)
    if added:
        await update.message.reply_text(
            f"✅ {added} number add hue.\nAb total: *{len(DATA['numbers'])}*",
            parse_mode="Markdown", reply_markup=main_menu(),
        )
    else:
        await update.message.reply_text(
            "Kuch valid number nahi mila. Country code ke saath bhejo, e.g. `919812345678`.",
            parse_mode="Markdown", reply_markup=main_menu(),
        )


async def cmd_gen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/gen 20  -> 20 rotating links deta hai."""
    if not is_admin(update):
        return
    n = MIN_LINKS
    if context.args and context.args[0].isdigit():
        n = int(context.args[0])
    await _do_generate(update.message, n)


async def _do_generate(message, n: int):
    if not DATA["numbers"]:
        await message.reply_text(
            "❌ Pehle numbers add karo (`/add` ya button se).",
            parse_mode="Markdown", reply_markup=main_menu(),
        )
        return
    if not BASE_URL:
        await message.reply_text(
            "⚠️ BASE_URL set nahi hai. Railway Variables me apni public URL daalo."
        )
        return

    n = max(MIN_LINKS, min(n, DATA.get("max_links", DEFAULT_MAX_LINKS)))

    link = f"{BASE_URL}/go"
    # Sabhi links same hote hain — kyunki rotation server-side counter par hai.
    # Har open par alag number. Isliye ek hi powerful link chahiye,
    # par tumne bulk maanga to hum utni copies list bhi de dete hain.
    lines = [f"{i+1}. {link}" for i in range(n)]
    body = "\n".join(lines)

    header = (
        f"✅ *{n} rotating links ready!*\n\n"
        f"👉 Main link:\n`{link}`\n\n"
        f"Har baar jab koi ise open karega, *agle WhatsApp number* par jayega "
        f"(round-robin rotation).\n\n"
        f"📋 Bulk list ({n}x):"
    )

    await message.reply_text(header, parse_mode="Markdown")

    # Telegram message limit 4096 chars — bade list ko tod kar bhejo
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) + 1 > 3500:
            await message.reply_text(f"`{chunk}`", parse_mode="Markdown")
            chunk = ""
        chunk += line + "\n"
    if chunk:
        await message.reply_text(f"`{chunk}`", parse_mode="Markdown")

    await message.reply_text("Aur kuch?", reply_markup=main_menu())


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update):
        await query.edit_message_text("⛔ Access denied.")
        return

    data = query.data

    if data == "gen":
        await _do_generate(query.message, MIN_LINKS)

    elif data == "add_help":
        context.user_data["mode"] = None
        await query.message.reply_text(
            "➕ Numbers bhejo (country code ke saath, bina +).\n\n"
            "Ek ya kai — har line par ek, ya space se alag:\n"
            "`919812345678`\n`919876543210`",
            parse_mode="Markdown",
        )

    elif data == "list":
        nums = DATA["numbers"]
        if not nums:
            await query.message.reply_text("📋 Abhi koi number nahi hai.")
        else:
            txt = "\n".join(f"{i+1}. +{x}" for i, x in enumerate(nums))
            await query.message.reply_text(
                f"📋 *Total {len(nums)} numbers:*\n{txt}", parse_mode="Markdown"
            )

    elif data == "clear":
        DATA["numbers"] = []
        DATA["counter"] = 0
        save_data(DATA)
        await query.message.reply_text(
            "🗑 Sab numbers hata diye.", reply_markup=main_menu()
        )

    elif data == "setmax_help":
        context.user_data["mode"] = "setmax"
        await query.message.reply_text(
            f"⚙️ Max links ka number bhejo (kam se kam {MIN_LINKS}).\n"
            f"Abhi: {DATA.get('max_links', DEFAULT_MAX_LINKS)}"
        )

    elif data == "setmsg_help":
        context.user_data["mode"] = "setmsg"
        await query.message.reply_text(
            "💬 WhatsApp par jo pre-filled message aana chahiye wo bhejo.\n"
            f"Abhi: {DATA.get('message', DEFAULT_MESSAGE)}"
        )

    elif data == "stats":
        await query.message.reply_text(
            f"📊 *Stats*\n"
            f"Numbers: {len(DATA['numbers'])}\n"
            f"Total link opens: {DATA.get('hits', 0)}\n"
            f"Max links: {DATA.get('max_links', DEFAULT_MAX_LINKS)}\n"
            f"Message: {DATA.get('message', DEFAULT_MESSAGE)}",
            parse_mode="Markdown",
            reply_markup=main_menu(),
        )


# ------------------------------------------------------------------ #
# RUN both together (bot polling + web server)
# ------------------------------------------------------------------ #
async def run():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN env variable set karo (Railway Variables me).")

    # Web server start
    web_app = build_web_app()
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("Web server started on port %s", PORT)

    # Telegram bot start
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("add", cmd_add))
    application.add_handler(CommandHandler("gen", cmd_gen))
    application.add_handler(CallbackQueryHandler(on_button))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, on_text)
    )

    await application.initialize()
    await application.start()
    await application.updater.start_polling(drop_pending_updates=True)
    log.info("Telegram bot started (polling).")

    # Hamesha chalte raho
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(run())
