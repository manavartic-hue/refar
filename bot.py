import os
import re
import asyncio
import logging
from urllib.parse import urlparse, parse_qs, unquote

import aiohttp
from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

SCAN_COUNT = 20
REQUEST_TIMEOUT = 20
DELAY_BETWEEN_REQUESTS = 0.8

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)

active_scans = {}


def is_valid_url(text: str) -> bool:
    try:
        parsed = urlparse(text.strip())
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except Exception:
        return False


def normalize_number(raw: str) -> str | None:
    if not raw:
        return None

    number = re.sub(r"[^\d+]", "", raw.strip())

    if number.startswith("00"):
        number = "+" + number[2:]

    digits_only = re.sub(r"\D", "", number)

    if len(digits_only) < 8 or len(digits_only) > 15:
        return None

    if number.startswith("+"):
        return "+" + digits_only

    return digits_only


def extract_numbers_from_text(text: str) -> list[str]:
    found = set()

    patterns = [
        r"wa\.me/(\+?\d{8,15})",
        r"whatsapp\.com/send\?[^\"'\s<>]*phone=(\+?\d{8,15})",
        r"web\.whatsapp\.com/send\?[^\"'\s<>]*phone=(\+?\d{8,15})",
        r"api\.whatsapp\.com/send\?[^\"'\s<>]*phone=(\+?\d{8,15})",
        r"phone=(\+?\d{8,15})",
    ]

    for pattern in patterns:
        matches = re.findall(pattern, text, flags=re.IGNORECASE)
        for match in matches:
            number = normalize_number(unquote(match))
            if number:
                found.add(number)

    possible_numbers = re.findall(r"(?:\+?\d[\d\s().-]{7,20}\d)", text)
    for item in possible_numbers:
        number = normalize_number(item)
        if number:
            found.add(number)

    return sorted(found)


def extract_numbers_from_url(url: str) -> list[str]:
    found = set()

    parsed = urlparse(url)
    query = parse_qs(parsed.query)

    for key in ["phone", "number", "mobile", "wa", "whatsapp"]:
        if key in query:
            for value in query[key]:
                number = normalize_number(value)
                if number:
                    found.add(number)

    url_text_numbers = extract_numbers_from_text(url)
    for number in url_text_numbers:
        found.add(number)

    return sorted(found)


async def fetch_once(session: aiohttp.ClientSession, url: str) -> dict:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 12; Mobile) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0 Mobile Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    result = {
        "final_url": None,
        "status": None,
        "numbers": [],
        "error": None,
    }

    try:
        async with session.get(
            url,
            headers=headers,
            allow_redirects=True,
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
        ) as response:
            result["status"] = response.status
            result["final_url"] = str(response.url)

            content_type = response.headers.get("content-type", "")
            body = ""

            if "text" in content_type or "html" in content_type or "json" in content_type:
                body = await response.text(errors="ignore")

            numbers = set()

            for number in extract_numbers_from_url(str(response.url)):
                numbers.add(number)

            for number in extract_numbers_from_text(body):
                numbers.add(number)

            result["numbers"] = sorted(numbers)

    except Exception as exc:
        result["error"] = str(exc)

    return result


def main_menu() -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("🔍 Start Scanning", callback_data="start_scan"),
            InlineKeyboardButton("⛔ Stop Scanning", callback_data="stop_scan"),
        ],
        [
            InlineKeyboardButton("📋 Help", callback_data="help"),
        ],
    ]

    return InlineKeyboardMarkup(keyboard)


def scanning_menu() -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("⛔ Stop Scanning", callback_data="stop_scan"),
        ]
    ]

    return InlineKeyboardMarkup(keyboard)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()

    text = (
        "👋 <b>WhatsApp Number Scanner Bot</b>\n\n"
        "Send me a redirect link.\n\n"
        "I will scan it <b>20 times</b> and extract WhatsApp numbers from redirects or page content.\n\n"
        "Use the buttons below."
    )

    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu(),
    )


async def help_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "📋 <b>How to use</b>\n\n"
        "1. Send a link that redirects to WhatsApp.\n"
        "2. Press <b>Start Scanning</b>.\n"
        "3. The bot will open the link 20 times.\n"
        "4. It will show unique WhatsApp numbers line by line.\n\n"
        "Supported formats:\n"
        "• wa.me/919999999999\n"
        "• api.whatsapp.com/send?phone=919999999999\n"
        "• web.whatsapp.com/send?phone=919999999999\n\n"
        "Note: JavaScript-only redirects, CAPTCHA, login pages, or blocked websites may not work."
    )

    if update.callback_query:
        await update.callback_query.message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu(),
        )
    else:
        await update.message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu(),
        )


async def receive_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()

    if not is_valid_url(text):
        await update.message.reply_text(
            "❌ Please send a valid link starting with http:// or https://",
            reply_markup=main_menu(),
        )
        return

    context.user_data["scan_url"] = text

    await update.message.reply_text(
        "✅ Link saved.\n\nPress <b>Start Scanning</b> to scan it 20 times.",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu(),
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    data = query.data

    if data == "help":
        await help_message(update, context)
        return

    if data == "stop_scan":
        task = active_scans.get(user_id)

        if task and not task.done():
            task.cancel()
            await query.message.reply_text(
                "⛔ Scanning stopped.",
                reply_markup=main_menu(),
            )
        else:
            await query.message.reply_text(
                "No active scan is running.",
                reply_markup=main_menu(),
            )
        return

    if data == "start_scan":
        scan_url = context.user_data.get("scan_url")

        if not scan_url:
            await query.message.reply_text(
                "Please send a link first.",
                reply_markup=main_menu(),
            )
            return

        existing_task = active_scans.get(user_id)

        if existing_task and not existing_task.done():
            await query.message.reply_text(
                "A scan is already running. Stop it first if you want to start again.",
                reply_markup=scanning_menu(),
            )
            return

        task = asyncio.create_task(run_scan(query.message, context, user_id, scan_url))
        active_scans[user_id] = task


async def run_scan(message, context: ContextTypes.DEFAULT_TYPE, user_id: int, url: str) -> None:
    unique_numbers = []
    unique_set = set()
    errors = 0

    progress_message = await message.reply_text(
        f"🔍 Scanning started...\n\nTotal scans: {SCAN_COUNT}\nFound: 0",
        reply_markup=scanning_menu(),
    )

    try:
        async with aiohttp.ClientSession() as session:
            for i in range(1, SCAN_COUNT + 1):
                task = active_scans.get(user_id)

                if task and task.cancelled():
                    break

                result = await fetch_once(session, url)

                if result.get("error"):
                    errors += 1

                for number in result.get("numbers", []):
                    if number not in unique_set:
                        unique_set.add(number)
                        unique_numbers.append(number)

                await progress_message.edit_text(
                    f"🔍 Scanning...\n\n"
                    f"Completed: {i}/{SCAN_COUNT}\n"
                    f"Unique numbers found: {len(unique_numbers)}\n"
                    f"Errors: {errors}",
                    reply_markup=scanning_menu(),
                )

                await asyncio.sleep(DELAY_BETWEEN_REQUESTS)

        if unique_numbers:
            numbers_text = "\n".join(
                [f"{index + 1}. <code>{number}</code>" for index, number in enumerate(unique_numbers)]
            )

            await message.reply_text(
                f"✅ <b>Scan complete</b>\n\n"
                f"Total scans: {SCAN_COUNT}\n"
                f"Unique numbers found: {len(unique_numbers)}\n\n"
                f"{numbers_text}",
                parse_mode=ParseMode.HTML,
                reply_markup=main_menu(),
            )
        else:
            await message.reply_text(
                "⚠️ Scan complete, but no WhatsApp numbers were found.\n\n"
                "The link may use JavaScript redirect, CAPTCHA, login protection, or may not expose the number directly.",
                reply_markup=main_menu(),
            )

    except asyncio.CancelledError:
        await message.reply_text(
            "⛔ Scanning stopped.",
            reply_markup=main_menu(),
        )

    except Exception as exc:
        logger.exception("Scan failed")

        await message.reply_text(
            f"❌ Scan failed:\n{str(exc)}",
            reply_markup=main_menu(),
        )

    finally:
        active_scans.pop(user_id, None)


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing. Add it in Railway Variables.")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_message))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_link))

    app.run_polling()


if __name__ == "__main__":
    main()
