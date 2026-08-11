import telebot
from telebot.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
import requests
import time
import os
import random
import re

BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
bot = telebot.TeleBot(BOT_TOKEN)

# Temporary storage for user's target URL
user_url = {}

# ----------------------------------------------------------------------
# Phone number extraction – all the known WhatsApp URL shapes
# ----------------------------------------------------------------------
def extract_phone_number(url: str) -> str | None:
    """Return a normalised phone number (digits only) or None."""
    # wa.me/1234567890
    m = re.search(r'https?://wa\.me/(\+?\d{7,15})', url)
    if m:
        return re.sub(r'\D', '', m.group(1))

    # api.whatsapp.com/send?phone=1234567890
    m = re.search(r'[?&]phone=(\+?\d{7,15})', url)
    if m:
        return re.sub(r'\D', '', m.group(1))

    # whatsapp://send?phone=...
    m = re.search(r'whatsapp://send\?phone=(\+?\d{7,15})', url)
    if m:
        return re.sub(r'\D', '', m.group(1))

    # tel:+1234567890
    m = re.search(r'tel:(\+?\d{7,15})', url)
    if m:
        return re.sub(r'\D', '', m.group(1))

    # Last resort: any standalone 7–15 digit sequence in the path or query
    m = re.search(r'(\d{7,15})', url)
    if m:
        return re.sub(r'\D', '', m.group(1))

    return None

# ----------------------------------------------------------------------
# Start
# ----------------------------------------------------------------------
@bot.message_handler(commands=['start'])
def send_welcome(message):
    welcome_text = (
        "🦊 *WhatsApp Number Extractor*\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "_मुझे वह लिंक भेजें जिससे आप WhatsApp नंबर निकालना चाहते हैं।_"
    )
    bot.reply_to(message, welcome_text, parse_mode="Markdown")

# ----------------------------------------------------------------------
# URL handler – stores the link and shows a reply keyboard with counts
# ----------------------------------------------------------------------
@bot.message_handler(func=lambda m: m.text and m.text.startswith('http'))
def handle_url(message):
    url = message.text.strip()
    user_url[message.chat.id] = url

    markup = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    markup.row(
        KeyboardButton("⚡ 20 बार"),
        KeyboardButton("🔥 50 बार"),
    )
    markup.row(
        KeyboardButton("💎 100 बार"),
    )
    bot.reply_to(
        message,
        "✅ *लिंक सेव हो गया।*\n_कितनी बार नंबर निकालना है?_",
        reply_markup=markup,
        parse_mode="Markdown"
    )

# ----------------------------------------------------------------------
# Count button handler – picks up the count from the button label
# ----------------------------------------------------------------------
@bot.message_handler(func=lambda m: m.text and any(kw in m.text for kw in ("20 बार", "50 बार", "100 बार")))
def handle_count_choice(message):
    chat_id = message.chat.id

    if chat_id not in user_url:
        bot.reply_to(message, "❌ *पहले एक लिंक भेजें।*", parse_mode="Markdown")
        return

    # Extract number from button label (e.g., "⚡ 20 बार" → 20)
    digits = re.search(r'\d+', message.text)
    if not digits:
        bot.reply_to(message, "❌ *अमान्य विकल्प।*", parse_mode="Markdown")
        return
    count = int(digits.group())

    target_url = user_url.pop(chat_id)  # consume the URL so it’s one-shot

    # Remove the reply keyboard immediately
    bot.send_message(chat_id, "⏳ *प्रोसेसिंग शुरू…*", reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown")

    # ------------------------------------------------------------------
    # Extraction loop
    # ------------------------------------------------------------------
    phone_numbers = set()
    errors = 0

    for i in range(count):
        try:
            separator = "&" if "?" in target_url else "?"
            req_url = f"{target_url}{separator}nocache={time.time()}_{random.randint(1000, 9999)}"
            headers = {
                'User-Agent': f'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{random.randint(90, 120)}.0.0.0 Safari/537.36'
            }
            resp = requests.get(req_url, headers=headers, allow_redirects=True, timeout=10)
            final_url = resp.url
            num = extract_phone_number(final_url)
            if num:
                phone_numbers.add(num)
        except requests.exceptions.RequestException:
            errors += 1

    # ------------------------------------------------------------------
    # Format result
    # ------------------------------------------------------------------
    if phone_numbers:
        numbers_list = "\n".join(f"📞 +{num}" for num in sorted(phone_numbers))
        result = (
            f"🎯 *निकाले गए नंबर* ({len(phone_numbers)} यूनिक)\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"{numbers_list}\n"
            f"━━━━━━━━━━━━━━━━━━━"
        )
        if errors:
            result += f"\n⚠️ {errors} रिक्वेस्ट फेल (टाइमआउट / सर्वर डाउन)।"
    else:
        result = "❌ *कोई नंबर नहीं मिला।*"

    # Telegram message limit safety
    if len(result) > 4000:
        result = result[:4000] + "\n\n… (मैसेज बहुत लंबा है, काट दिया गया)"

    bot.send_message(chat_id, result, parse_mode="Markdown")

# ----------------------------------------------------------------------
# Fallback – any other text
# ----------------------------------------------------------------------
@bot.message_handler(func=lambda m: True)
def fallback(message):
    bot.reply_to(message, "👋 कृपया एक वैध *लिंक* भेजें (http से शुरू)।", parse_mode="Markdown")

# ----------------------------------------------------------------------
if __name__ == '__main__':
    print("🦊 Bot is running...")
    bot.polling(none_stop=True)
