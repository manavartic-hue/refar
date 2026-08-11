import telebot
from telebot.types import ReplyKeyboardMarkup, KeyboardButton
import requests
import time
import os
import random
import re
from urllib.parse import urlparse, parse_qs
import sqlite3
import threading

# --- कॉन्फ़िगरेशन ---
BOT_TOKEN = os.environ.get("BOT_TOKEN", "अपना_टोकन_यहाँ_डालें")
# नीचे अपना Telegram User ID डालें (एडमिन पैनल एक्सेस के लिए)
ADMIN_IDS = [5888777479] # अपनी ID यहाँ डालें!

bot = telebot.TeleBot(BOT_TOKEN)

# --- डेटाबेस सेटअप (SQLite) ---
def init_db():
    conn = sqlite3.connect('bot_database.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (user_id INTEGER PRIMARY KEY, joined_date TEXT, total_extracted INTEGER, is_banned INTEGER)''')
    conn.commit()
    conn.close()

init_db()

# --- डेटाबेस हेल्पर्स ---
def add_user(user_id):
    conn = sqlite3.connect('bot_database.db')
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (user_id, joined_date, total_extracted, is_banned) VALUES (?, date('now'), 0, 0)", (user_id,))
    conn.commit()
    conn.close()

def update_extraction_count(user_id, count):
    conn = sqlite3.connect('bot_database.db')
    c = conn.cursor()
    c.execute("UPDATE users SET total_extracted = total_extracted + ? WHERE user_id = ?", (count, user_id))
    conn.commit()
    conn.close()

def get_user_stats(user_id):
    conn = sqlite3.connect('bot_database.db')
    c = conn.cursor()
    c.execute("SELECT total_extracted FROM users WHERE user_id = ?", (user_id,))
    res = c.fetchone()
    conn.close()
    return res[0] if res else 0

def get_all_users():
    conn = sqlite3.connect('bot_database.db')
    c = conn.cursor()
    c.execute("SELECT user_id FROM users")
    users = [row[0] for row in c.fetchall()]
    conn.close()
    return users

# यूज़र स्टेट्स (लिंक स्टोर करने और ब्रॉडकास्ट स्टेट के लिए)
user_states = {}

# --- कीबोर्ड (UI/UX) ---
def get_main_menu():
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(
        KeyboardButton("🔗 नया लिंक भेजें"), 
        KeyboardButton("📊 मेरे आँकड़े (Stats)")
    )
    markup.add(
        KeyboardButton("❓ मदद"),
        KeyboardButton("📞 सपोर्ट")
    )
    return markup

def get_extraction_menu():
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(
        KeyboardButton("🚀 20 बार निकालें"),
        KeyboardButton("🚀 50 बार निकालें")
    )
    markup.add(
        KeyboardButton("💎 100 बार निकालें (Max)"),
        KeyboardButton("❌ रद्द करें")
    )
    return markup

def get_admin_menu():
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(
        KeyboardButton("📈 बॉट स्टैट्स"),
        KeyboardButton("📢 ब्रॉडकास्ट")
    )
    markup.add(
        KeyboardButton("🔙 मेनू में जाएँ")
    )
    return markup

# --- कोर लॉजिक ---
def extract_wa_number(url):
    match = re.search(r'wa\.me/(\d+)', url)
    if match: return match.group(1)
    
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    if 'phone' in qs:
        num = re.sub(r'\D', '', qs['phone'][0])
        if num: return num
        
    match_digits = re.search(r'(\d{10,15})', url)
    if match_digits: return match_digits.group(1)
    return None

# --- एडमिन कमांड्स ---
@bot.message_handler(commands=['admin'])
def admin_panel(message):
    if message.chat.id in ADMIN_IDS:
        bot.send_message(message.chat.id, "👑 **एडमिन पैनल में आपका स्वागत है!**", parse_mode="Markdown", reply_markup=get_admin_menu())
    else:
        bot.send_message(message.chat.id, "❌ आपके पास एडमिन अधिकार नहीं हैं।")

@bot.message_handler(func=lambda message: message.text == "📈 बॉट स्टैट्स" and message.chat.id in ADMIN_IDS)
def admin_stats(message):
    users = get_all_users()
    conn = sqlite3.connect('bot_database.db')
    c = conn.cursor()
    c.execute("SELECT SUM(total_extracted) FROM users")
    total_nums = c.fetchone()[0] or 0
    conn.close()
    
    text = (
        "👑 **A D M I N   S T A T S** 👑\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👥 **कुल यूज़र्स:** `{len(users)}`\n"
        f"🎯 **कुल निकाले गए नंबर:** `{total_nums}`"
    )
    bot.send_message(message.chat.id, text, parse_mode="Markdown")

@bot.message_handler(func=lambda message: message.text == "📢 ब्रॉडकास्ट" and message.chat.id in ADMIN_IDS)
def admin_broadcast(message):
    user_states[message.chat.id] = {'state': 'waiting_for_broadcast'}
    bot.send_message(message.chat.id, "📢 कृपया वह मैसेज भेजें जिसे आप सभी यूज़र्स को भेजना चाहते हैं। (रद्द करने के लिए 'cancel' लिखें)", reply_markup=ReplyKeyboardMarkup(resize_keyboard=True).add(KeyboardButton("cancel")))

# --- यूज़र कमांड्स ---
@bot.message_handler(commands=['start'])
def send_welcome(message):
    add_user(message.chat.id)
    text = (
        "✨ 💎 **W H A T S A P P   E X T R A C T O R** 💎 ✨\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👋 **नमस्ते {message.from_user.first_name}!**\n\n"
        "🚀 **दुनिया का सबसे फास्ट नंबर एक्सट्रैक्टर!**\n"
        "🛡 **फीचर्स:** ऑटो-रीडायरेक्ट • डुप्लीकेट रिमूवल • .TXT एक्सपोर्ट\n\n"
        "👇 काम शुरू करने के लिए नीचे दिए गए बटन का उपयोग करें।"
    )
    bot.send_message(message.chat.id, text, parse_mode="Markdown", reply_markup=get_main_menu())

@bot.message_handler(func=lambda message: message.text in ["🔗 नया लिंक भेजें", "📊 मेरे आँकड़े (Stats)", "❓ मदद", "📞 सपोर्ट", "❌ रद्द करें", "🔙 मेनू में जाएँ", "cancel"])
def handle_main_buttons(message):
    add_user(message.chat.id)
    text = message.text
    
    if text == "🔗 नया लिंक भेजें":
        bot.reply_to(message, "🔗 **कृपया अपना रोटेटिंग (Rotating) लिंक भेजें:**", parse_mode="Markdown", reply_markup=telebot.types.ReplyKeyboardRemove())
        user_states[message.chat.id] = {'state': 'waiting_for_link'}
        
    elif text == "📊 मेरे आँकड़े (Stats)":
        stats = get_user_stats(message.chat.id)
        bot.reply_to(message, f"📊 **आपके आँकड़े:**\nआपने अब तक कुल `{stats}` यूनिक नंबर्स निकाले हैं!", parse_mode="Markdown")
        
    elif text == "❓ मदद":
        bot.reply_to(message, "💡 **मदद:**\nमुझे वह लिंक भेजें जो WhatsApp पर ले जाता है। मैं उस लिंक को बैकग्राउंड में कई बार ओपन करूँगा और सारे नंबर्स निकाल कर आपको `.txt` फाइल में दे दूँगा।", reply_markup=get_main_menu())
        
    elif text == "📞 सपोर्ट":
        bot.reply_to(message, "👨‍💻 **सपोर्ट:**\nकिसी भी समस्या के लिए एडमिन से संपर्क करें।", reply_markup=get_main_menu())
        
    elif text in ["❌ रद्द करें", "🔙 मेनू में जाएँ", "cancel"]:
        if message.chat.id in user_states:
            del user_states[message.chat.id]
        if text == "cancel" and message.chat.id in ADMIN_IDS:
            bot.reply_to(message, "✅ ब्रॉडकास्ट रद्द किया गया।", reply_markup=get_admin_menu())
        else:
            bot.reply_to(message, "✅ मुख्य मेनू में वापस आ गए हैं।", reply_markup=get_main_menu())

@bot.message_handler(func=lambda message: user_states.get(message.chat.id, {}).get('state') == 'waiting_for_broadcast')
def handle_broadcast_message(message):
    if message.chat.id not in ADMIN_IDS: return
    
    msg_text = message.text
    users = get_all_users()
    sent = 0
    bot.send_message(message.chat.id, f"⏳ ब्रॉडकास्ट शुरू हो रहा है... ({len(users)} यूज़र्स को)")
    
    for u_id in users:
        try:
            bot.send_message(u_id, f"📢 **एडमिन अपडेट:**\n\n{msg_text}", parse_mode="Markdown")
            sent += 1
        except: pass
        
    del user_states[message.chat.id]
    bot.send_message(message.chat.id, f"✅ **ब्रॉडकास्ट पूरा हुआ!**\nसफलतापूर्वक {sent} यूज़र्स को मैसेज भेजा गया।", parse_mode="Markdown", reply_markup=get_admin_menu())

@bot.message_handler(func=lambda message: message.text.startswith('http') or user_states.get(message.chat.id, {}).get('state') == 'waiting_for_link')
def handle_url(message):
    url = message.text
    if not url.startswith('http'):
        bot.reply_to(message, "❌ यह एक मान्य लिंक नहीं है। कृपया सही (http/https) लिंक भेजें।")
        return
        
    user_states[message.chat.id] = {'state': 'ready_to_extract', 'url': url}
    
    text = (
        "✨ 💎 **L I N K   S A V E D** 💎 ✨\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🎯 **लिंक:** `{url}`\n\n"
        "👇 कृपया नीचे दिए गए मेनू से चुनें कि आप कितनी बार चेक करना चाहते हैं:"
    )
    bot.send_message(message.chat.id, text, parse_mode="Markdown", disable_web_page_preview=True, reply_markup=get_extraction_menu())

# --- एक्सट्रैक्शन प्रोसेस (बैकग्राउंड थ्रेड) ---
def background_extraction(chat_id, target_url, count):
    msg = bot.send_message(chat_id, f"⏳ **प्रोसेसिंग शुरू...**\n🔄 लिंक को {count} बार चेक किया जा रहा है।", parse_mode="Markdown")
    
    extracted_numbers = set()
    errors = 0
    
    for i in range(count):
        try:
            separator = "&" if "?" in target_url else "?"
            req_url = f"{target_url}{separator}nocache={time.time()}_{random.randint(1000, 9999)}"
            headers = {'User-Agent': f'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/{random.randint(90, 120)}.0.0.0'}
            
            response = requests.get(req_url, headers=headers, allow_redirects=True, timeout=10)
            number = extract_wa_number(response.url)
            if number:
                extracted_numbers.add(number)
                
            # हर 20 रिक्वेस्ट पर यूज़र को अपडेट दें (ताकि उसे लगे बॉट अटका नहीं है)
            if (i + 1) % 20 == 0:
                bot.edit_message_text(f"⏳ **प्रोसेसिंग जारी...** ({i+1}/{count} चेक किए गए)\nअब तक मिले नंबर्स: {len(extracted_numbers)}", chat_id, msg.message_id, parse_mode="Markdown")
                
        except requests.exceptions.RequestException:
            errors += 1
            
    bot.delete_message(chat_id, msg.message_id)
    
    if extracted_numbers:
        update_extraction_count(chat_id, len(extracted_numbers))
        
        # 1. मैसेज के रूप में भेजें
        result_text = "\n".join([f"📞 `{num}`" for num in extracted_numbers])
        final_message = (
            f"✨ 💎 **R E S U L T S   R E A D Y** 💎 ✨\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🎯 **यूनिक नंबर्स मिले:** {len(extracted_numbers)}\n"
        )
        if errors > 0: final_message += f"⚠️ **फेल रिक्वेस्ट:** {errors}\n\n"
        else: final_message += "\n"
        
        bot.send_message(chat_id, final_message + result_text[:3500], parse_mode="Markdown", reply_markup=get_main_menu())
        
        # 2. .TXT फाइल बनाकर भेजें (Pro Feature)
        filename = f"Numbers_{chat_id}_{int(time.time())}.txt"
        with open(filename, "w") as f:
            f.write("\n".join(extracted_numbers))
            
        with open(filename, "rb") as f:
            bot.send_document(chat_id, f, caption="📁 **आपकी फाइल तैयार है!**\nसारे नंबर्स इस .txt फाइल में सेव हैं।", parse_mode="Markdown")
        
        os.remove(filename) # फाइल भेजने के बाद सर्वर से डिलीट कर दें
        
    else:
        bot.send_message(chat_id, "❌ कोई भी नया नंबर एक्सट्रैक्ट नहीं हो पाया।", reply_markup=get_main_menu())

@bot.message_handler(func=lambda message: "बार निकालें" in message.text)
def handle_extraction(message):
    chat_id = message.chat.id
    state_data = user_states.get(chat_id, {})
    
    if state_data.get('state') != 'ready_to_extract' or 'url' not in state_data:
        bot.reply_to(message, "❌ **कृपया पहले एक नया लिंक भेजें।**", parse_mode="Markdown", reply_markup=get_main_menu())
        return
        
    count = 20 if "20" in message.text else (50 if "50" in message.text else (100 if "100" in message.text else 0))
    if count == 0: return

    target_url = state_data['url']
    del user_states[chat_id] # प्रिवेंट डबल क्लिक
    
    # थ्रेडिंग का उपयोग (ताकि बॉट दूसरे यूज़र्स के लिए रुके नहीं)
    threading.Thread(target=background_extraction, args=(chat_id, target_url, count)).start()

print("Bot is running...")
bot.polling(none_stop=True)
