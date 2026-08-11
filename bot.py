import telebot
from telebot.types import ReplyKeyboardMarkup, KeyboardButton
import requests
import time
import os
import random
import re
from urllib.parse import urlparse, parse_qs

# Railway के Environment Variables से टोकन लेगा
BOT_TOKEN = os.environ.get("BOT_TOKEN", "यहाँअपनाटोकनडालेंअगरलोकलटेस्टकररहेहैं")
bot = telebot.TeleBot(BOT_TOKEN)

# यूज़र का डेटा स्टोर करने के लिए डिक्शनरी
user_data = {}

# बॉट का हेडर (इमेज वाले स्टाइल में)
HEADER = "✨ 💎 W H A T S A P P   E X T R A C T O R 💎\n_____________________________________\n"


# ---------- मेन्यू (ReplyKeyboard) ----------
def main_menu():
    """नीचे वाला बड़ा बटन-कीबोर्ड (इनलाइन नहीं)।"""
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(
        KeyboardButton("🔢 Extract Numbers"),
        KeyboardButton("📊 Stats"),
    )
    markup.add(
        KeyboardButton("📋 My Extractions"),
        KeyboardButton("🔗 Referral QR"),
    )
    markup.row(
        KeyboardButton("❓ Help"),
        KeyboardButton("🌐 Language"),
        KeyboardButton("🆘 Support"),
    )
    return markup


def count_menu():
    """गिनती चुनने वाला कीबोर्ड।"""
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(
        KeyboardButton("⏪ 20 बार (Min)"),
        KeyboardButton("⏫ 50 बार"),
    )
    markup.add(
        KeyboardButton("🔥 100 बार (Max)"),
    )
    markup.row(
        KeyboardButton("🔙 Back")
    )
    return markup


# ---------- हेल्पर: URL से नंबर निकालना ----------
def extract_phone(final_url):
    """
    WhatsApp के अलग-अलग URL फॉर्मैट से फ़ोन नंबर निकालता है।
    उदा.:
      [wa.me](https://wa.me/919876543210)
      [api.whatsapp.com](https://api.whatsapp.com/send?phone=919876543210)
      [api.whatsapp.com](https://api.whatsapp.com/send/?phone=919876543210&text=hi)
    रिटर्न: नंबर स्ट्रिंग (बिना +), या कुछ नहीं मिले तो None।
    """
    # 1) wa.me/NUMBER  या  wa.me/+NUMBER
    m = re.search(r'wa\.me/(\+?\d+)', final_url)
    if m:
        return m.group(1).lstrip('+')

    # 2) ?phone=NUMBER  या  &phone=NUMBER
    parsed = urlparse(final_url)
    qs = parse_qs(parsed.query)
    if 'phone' in qs and qs['phone'][0]:
        return qs['phone'][0].lstrip('+')

    # 3) फॉलबैक: अगर URL में कहीं भी 8-15 अंकों का ब्लॉक हो
    m = re.search(r'(\d{8,15})', final_url)
    if m:
        return m.group(1)

    return None


def format_phone(num):
    """नंबर को पढ़ने में आसान बनाता है: +91 98765 43210 जैसा।"""
    if num and num.startswith('91') and len(num) >= 12:
        return f"+91 {num[2:7]} {num[7:]}"
    return f"+{num}" if num else ""


# ---------- हैंडलर्स ----------
@bot.message_handler(commands=['start'])
def send_welcome(message):
    text = (
        HEADER +
        "🚀 इस बोट से आप किसी भी लिंक से WhatsApp नंबर निकाल सकते हैं।\n\n"
        "🛡️ Features: OTP resend • Multiple counts • Deduplication • "
        "Number format • Stats • Referral QR • Multi-language\n\n"
        "👇 नीचे दिए बटन में से चुनें।"
    )
    bot.send_message(message.chat.id, text, reply_markup=main_menu())


@bot.message_handler(func=lambda m: m.text == "🔢 Extract Numbers")
def ask_url(message):
    user_data.pop(message.chat.id, None)
    bot.send_message(
        message.chat.id,
        "🔗 वह लिंक भेजें जिससे आप WhatsApp नंबर निकालना चाहते हैं:"
    )


@bot.message_handler(func=lambda m: m.text and m.text.startswith('http'))
def handle_url(message):
    url = message.text.strip()
    user_data[message.chat.id] = {
        'url': url,
        'total_requests': 0,
        'unique_numbers': 0,
        'errors': 0,
        'history': [],
    }
    bot.send_message(
        message.chat.id,
        "✅ लिंक सेव हो गया। आप इस लिंक से कितनी बार नंबर निकालना चाहते हैं?",
        reply_markup=count_menu(),
    )


@bot.message_handler(func=lambda m: m.text in
                     ("⏪ 20 बार (Min)", "⏫ 50 बार", "🔥 100 बार (Max)"))
def handle_extraction(message):
    chat_id = message.chat.id

    if chat_id not in user_data or 'url' not in user_data[chat_id]:
        bot.send_message(
            chat_id, "❌ कृपया पहले एक लिंक भेजें।",
            reply_markup=count_menu()
        )
        return

    # टेक्स्ट से काउंट निकालो
    m = re.search(r'(\d+)', message.text)
    if not m:
        return
    count = int(m.group(1))

    target_url = user_data[chat_id]['url']
    user_data[chat_id]['total_requests'] += count

    bot.send_message(
        chat_id,
        f"⏳ प्रोसेसिंग शुरू...\nलिंक को {count} बार चेक किया जा रहा है। कृपया प्रतीक्षा करें।",
        reply_markup=main_menu(),
    )

    extracted = set()   # यूनिक नंबर्स के लिए सेट
    errors = 0

    for _ in range(count):
        try:
            # कैशिंग बायपास के लिए रैंडम पैरामीटर
            sep = "&" if "?" in target_url else "?"
            req_url = f"{target_url}{sep}nocache={time.time()}{random.randint(1000, 9999)}"

            headers = {
                'User-Agent': (f'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                               f'AppleWebKit/537.36 (KHTML, like Gecko) '
                               f'Chrome/{random.randint(90, 120)}.0.0.0 Safari/537.36')
            }

            # allow_redirects=True से फाइनल WhatsApp लिंक तक पहुँचता है
            resp = requests.get(req_url, headers=headers,
                                allow_redirects=True, timeout=10)
            final_url = resp.url

            phone = extract_phone(final_url)
            if phone:
                extracted.add(phone)

        except requests.exceptions.RequestException:
            errors += 1

    # रिज़ल्ट बनाना — अब नंबर्स में, लिंक्स में नहीं
    user_data[chat_id]['errors'] += errors

    if extracted:
        lines = [format_phone(p) for p in sorted(extracted)]
        result_text = "\n".join(lines)
        final_msg = (
            HEADER +
            f"🎯 निकाले गए नंबर्स ({len(extracted)} यूनिक):\n\n"
            f"{result_text}"
        )
        if errors > 0:
            final_msg += f"\n\n⚠️ {errors} रिक्वेस्ट फेल हो गईं (सर्वर/टाइमआउट)।"

        user_data[chat_id]['unique_numbers'] += len(extracted)
        user_data[chat_id]['history'].append({
            'count': count,
            'found': len(extracted),
        })
    else:
        final_msg = "❌ कोई भी नंबर एक्सट्रैक्ट नहीं हो पाया।"

    # Telegram 4096 कैरेक्टर लिमिट
    if len(final_msg) > 4000:
        bot.send_message(
            chat_id,
            final_msg[:4000] + "\n\n... (मैसेज बहुत लंबा होने के कारण कट गया है)"
        )
    else:
        bot.send_message(chat_id, final_msg)


@bot.message_handler(func=lambda m: m.text == "📊 Stats")
def show_stats(message):
    d = user_data.get(message.chat.id, {})
    text = (
        HEADER +
        f"📊 आपकी Stats:\n\n"
        f"• कुल रिक्वेस्ट भेजीं: {d.get('total_requests', 0)}\n"
        f"• यूनिक नंबर्स मिले: {d.get('unique_numbers', 0)}\n"
        f"• फेल रिक्वेस्ट: {d.get('errors', 0)}\n"
        f"• सेशन्स: {len(d.get('history', []))}"
    )
    bot.send_message(message.chat.id, text, reply_markup=main_menu())


@bot.message_handler(func=lambda m: m.text == "📋 My Extractions")
def show_history(message):
    d = user_data.get(message.chat.id, {})
    history = d.get('history', [])
    if not history:
        bot.send_message(message.chat.id,
                         "📋 अभी तक कोई एक्सट्रैक्शन नहीं हुआ।",
                         reply_markup=main_menu())
        return
    lines = [f"{i+1}. {h['count']} बार → {h['found']} नंबर"
             for i, h in enumerate(history)]
    text = HEADER + "📋 आपके एक्सट्रैक्शन:\n\n" + "\n".join(lines)
    bot.send_message(message.chat.id, text, reply_markup=main_menu())


@bot.message_handler(func=lambda m: m.text == "🔗 Referral QR")
def referral_qr(message):
    text = (
        HEADER +
        "🔗 आपका रेफरल कोड: WLRPSY\n\n"
        "इस कोड को शेयर करके दोस्तों को जोड़ें।"
    )
    bot.send_message(message.chat.id, text, reply_markup=main_menu())


@bot.message_handler(func=lambda m: m.text == "❓ Help")
def show_help(message):
    text = (
        HEADER +
        "❓ मदद:\n\n"
        "1️⃣ '🔢 Extract Numbers' पर टैप करें।\n"
        "2️⃣ वह लिंक भेजें जिससे नंबर निकालने हैं।\n"
        "3️⃣ काउंट चुनें (20 / 50 / 100)।\n"
        "4️⃣ रिज़ल्ट में यूनिक नंबर्स मिलेंगे।\n\n"
        "📊 Stats से अपनी जानकारी देखें।"
    )
    bot.send_message(message.chat.id, text, reply_markup=main_menu())


@bot.message_handler(func=lambda m: m.text == "🌐 Language")
def choose_language(message):
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(
        KeyboardButton("🇮🇳 हिंदी"),
        KeyboardButton("🇬🇧 English"),
    )
    markup.row(KeyboardButton("🔙 Back"))
    bot.send_message(message.chat.id, "🌐 भाषा चुनें:", reply_markup=markup)


@bot.message_handler(func=lambda m: m.text == "🆘 Support")
def support(message):
    text = (
        HEADER +
        "🆘 सपोर्ट:\n\n"
        "किसी भी समस्या के लिए संपर्क करें:\n"
        "📧 @YourSupportHandle"
    )
    bot.send_message(message.chat.id, text, reply_markup=main_menu())


@bot.message_handler(func=lambda m: m.text == "🔙 Back")
def back_to_main(message):
    bot.send_message(message.chat.id, "🏠 मुख्य मेन्यू:",
                     reply_markup=main_menu())


# ---------- स्टार्ट ----------
if __name__ == '__main__':
    print("Bot chalu ho gaya...")
    bot.polling(none_stop=True)
