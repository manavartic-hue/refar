import asyncio
import aiohttp
import re
import os
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    CallbackQueryHandler,
    ContextTypes,
)

# ─── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── Config ─────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable is not set!")

# In-memory session store  {user_id: {"url": str}}
user_states: dict[int, dict] = {}

# Batch size – how many requests fire concurrently at once
BATCH_SIZE = 10

# Regex patterns to pull a phone number out of a WhatsApp redirect URL
WA_PATTERNS = [
    re.compile(r"wa\.me/(\d+)"),
    re.compile(r"whatsapp\.com/send\?phone=(\d+)"),
    re.compile(r"api\.whatsapp\.com/send\?phone=(\d+)"),
    re.compile(r"whatsapp://send\?phone=(\d+)"),
]

COUNT_OPTIONS = [20, 40, 60, 80, 100]


# ─── Helpers ────────────────────────────────────────────────────────────────
def extract_from_text(text: str) -> str | None:
    for pat in WA_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(1)
    return None


async def fetch_one(session: aiohttp.ClientSession, url: str) -> str | None:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        async with session.get(
            url,
            allow_redirects=True,
            timeout=aiohttp.ClientTimeout(total=15),
            headers=headers,
        ) as resp:
            # 1. Try final redirect URL
            number = extract_from_text(str(resp.url))
            if number:
                return number
            # 2. Try response body (some pages do JS redirects or embed the link)
            body = await resp.text(errors="ignore")
            return extract_from_text(body)
    except Exception as exc:
        logger.warning("Fetch error: %s", exc)
        return None


async def run_fetches(url: str, count: int) -> list[str]:
    results: list[str] = []
    connector = aiohttp.TCPConnector(ssl=False, limit=BATCH_SIZE)
    async with aiohttp.ClientSession(connector=connector) as session:
        for start in range(0, count, BATCH_SIZE):
            batch = min(BATCH_SIZE, count - start)
            tasks = [fetch_one(session, url) for _ in range(batch)]
            chunk = await asyncio.gather(*tasks)
            results.extend(r for r in chunk if r)
            await asyncio.sleep(0.3)  # small pause between batches
    return results


def build_count_keyboard() -> InlineKeyboardMarkup:
    row1 = [InlineKeyboardButton(f"🔁 {n}x", callback_data=f"count_{n}") for n in COUNT_OPTIONS[:3]]
    row2 = [InlineKeyboardButton(f"🔁 {n}x", callback_data=f"count_{n}") for n in COUNT_OPTIONS[3:]]
    return InlineKeyboardMarkup([row1, row2])


def format_results(numbers: list[str], count: int) -> str:
    unique = list(dict.fromkeys(numbers))  # preserve order, remove duplicates
    lines = [
        f"✅ *Done! Fetched {count}x*",
        f"📞 Unique numbers found: *{len(unique)}*",
        f"📊 Total hits: *{len(numbers)}*\n",
    ]
    for i, num in enumerate(unique, 1):
        lines.append(f"`{i}. +{num}`")
    return "\n".join(lines)


# ─── Handlers ───────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "🤖 *WhatsApp Redirect Number Extractor*\n\n"
        "Send me a *rotating WhatsApp redirect link* and I will:\n"
        "• Fetch it multiple times\n"
        "• Follow every redirect automatically\n"
        "• Collect all unique WhatsApp numbers\n\n"
        "📌 Just paste your link below to get started!"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "ℹ️ *How to use this bot:*\n\n"
        "1️⃣ Send a URL that redirects to WhatsApp\n"
        "2️⃣ Choose how many times to fetch (20 – 100)\n"
        "3️⃣ Wait for results — all unique numbers appear below\n\n"
        "Supported redirect targets:\n"
        "• `wa.me/<number>`\n"
        "• `api.whatsapp.com/send?phone=<number>`\n"
        "• `whatsapp.com/send?phone=<number>`"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()
    user_id = update.effective_user.id

    if text.startswith("http://") or text.startswith("https://"):
        user_states[user_id] = {"url": text}
        await update.message.reply_text(
            f"✅ *Link saved!*\n\n`{text}`\n\n🔢 *How many times should I fetch it?*",
            parse_mode="Markdown",
            reply_markup=build_count_keyboard(),
        )
    else:
        await update.message.reply_text(
            "⚠️ Please send a valid URL starting with `http://` or `https://`",
            parse_mode="Markdown",
        )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id

    if not query.data.startswith("count_"):
        return

    count = int(query.data.split("_")[1])

    if user_id not in user_states:
        await query.message.reply_text(
            "❌ Session expired. Please send your link again."
        )
        return

    url = user_states[user_id]["url"]

    await query.message.edit_text(
        f"⏳ *Fetching {count}x* — please wait…\n\n"
        f"🔗 `{url}`",
        parse_mode="Markdown",
    )

    numbers = await run_fetches(url, count)

    if numbers:
        result = format_results(numbers, count)
        # Telegram limit: 4096 chars
        if len(result) > 4000:
            result = result[:3950] + "\n\n_…list truncated (too many numbers)_"
        await query.message.edit_text(result, parse_mode="Markdown")
    else:
        await query.message.edit_text(
            "❌ *No WhatsApp numbers found.*\n\n"
            "Make sure the link redirects to a WhatsApp URL.\n"
            "Try sending the link again with /start",
            parse_mode="Markdown",
        )


# ─── Main ───────────────────────────────────────────────────────────────────
def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Bot is running…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
