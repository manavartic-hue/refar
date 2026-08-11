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

# ==========================================
# 1. कॉन्फ़िगरेशन (Configuration)
# ==========================================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "अपना_टोकन_यहाँ_डालें")
ADMIN_IDS = [123456789] # अपनी Telegram ID यहाँ डालें

bot = telebot.TeleBot(BOT_TOKEN)

# ==========================================
# 2. डेटाबेस सेटअप (SQLite)
# ==========================================
def init_db():
    conn = sqlite3.connect('bot_database.db', check_same_thread=False)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (user_id INTEGER PRIMARY KEY, joined_date TEXT, total_extracted INTEGER)''')
    conn.commit()
    conn.close()

init_db()

def add_user(user_id):
    conn = sqlite3.connect('bot_database.db', check_same_thread=False)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (user_id, joined_date, total_extracted) VALUES (?, date('now'), 0)", (user_id,))
    conn.commit()
    conn.close()

def update_extraction_count(user_id, count):
    conn = sqlite3.connect('bot_database.db', check_same_thread=False)
    c = conn.cursor()
    c.execute("UPDATE users SET total_extracted = total_extracted + ? WHERE user_id = ?", (count, user_id))
    conn.commit()
    conn.close()

def get_user_stats(user_id):
    conn = sqlite3.connect('bot_database.db', check_same_thread=False)
    c = conn.cursor()
    c.execute("SELECT total_extracted FROM users WHERE user_id = ?", (user_id,))
    res = c.fetchone()
    conn.close()
    return res[0] if res else 0

user_states = {}

# ==========================================
# 3. यूज़र इंटरफ़ेस (Keyboards)
# ==========================================
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

# ==========================================
# 4. कोर इंजन: WhatsApp Number Extractor (100% Accurate)
# ==========================================
def get_clean_number(text_string):
    """सिर्फ प्योर नंबर्स निकालता है (जैसे +91 हटाकर या साफ करके)"""
    num = re.sub(r'\D', '', text_string)
    return num if len(num) >= 10 else None

def extract_wa_number_strict(url, html_content=""):
    """
    यह फंक्शन 100% स्ट्रिक्ट है। यह सिर्फ उन्हीं नंबर्स को उठाएगा 
    जो WhatsApp के ऑफिशियल लिंक फॉर्मेट से जुड़े हों।
    """
    # Pattern 1: URL के अंदर खोजना
    patterns = [
        r'wa\.me/(\+?\d+)',
        r'api\.whatsapp\.com/send\/?\?phone=(\+?\d+)',
        r'whatsapp://send\/?\?phone=(\+?\d+)'
    ]
    
    for pattern in patterns:
        match = re.search(pattern, url, re.IGNORECASE)
        if match:
            return get_clean_number(match.group(1))

    # Pattern 2: HTML पेज के अंदर खोजना (JS Redirects के लिए)
    if html_content:
        for pattern in patterns:
            match = re.search(pattern, html_content, re.IGNORECASE)
            if match:
                return get_clean_number(match.group(1))
                
        # Pattern 3: कई बार HTML में सीधा "phone=919876543210" छिपा होता है
        phone_param_match = re.search(r'phone=(\d{10,15})', html_content, re.IGNORECASE)
        if phone_param_match:
            return get_clean_number(phone_param_match.group(1))

    return None

# ==========================================
# 5. बॉट कमांड्स और हैंडल्स
# ==========================================
@bot.message_handler(commands=['start'])
def send_welcome(message):
    add_user(message.chat.id)
    text = (
        "✨ 💎 **W H A T S A P P   E X T R A C T O R** 💎 ✨\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👋 **नमस्ते {message.from_user.first_name}!**\n\n"
        "🚀 **एडवांस्ड नंबर एक्सट्रैक्टर (V2.0)**\n"
        "🛡 **फीचर्स:** JavaScript Bypass • 100% एक्यूरेट • .TXT एक्सपोर्ट\n\n"
        "👇 काम शुरू करने के लिए **'नया लिंक भेजें'** पर क्लिक करें।"
    )
    bot.send_message(message.chat.id, text, parse_mode="Markdown", reply_markup=get_main_menu())

@bot.message_handler(func=lambda message: message.text in ["🔗 नया लिंक भेजें", "📊 मेरे आँकड़े (Stats)", "❓ मदद", "📞 सपोर्ट", "❌ रद्द करें"])
def handle_main_buttons(message):
    add_user(message.chat.id)
    text = message.text
    
    if text == "🔗 नया लिंक भेजें":
        bot.reply_to(message, "🔗 **कृपया अपना रोटेटिंग (Rotating) लिंक भेजें:**\n*(जैसे: http://prismatic-hcgyvud.site.je)*", parse_mode="Markdown", reply_markup=telebot.types.ReplyKeyboardRemove())
        user_states[message.chat.id] = {'state': 'waiting_for_link'}
        
    elif text == "📊 मेरे आँकड़े (Stats)":
        stats = get_user_stats(message.chat.id)
        bot.reply_to(message, f"📊 **आपके आँकड़े:**\nआपने अब तक कुल `{stats}` असली नंबर्स निकाले हैं!", parse_mode="Markdown")
        
    elif text == "❓ मदद":
        bot.reply_to(message, "💡 मुझे बस अपना लिंक दें। मेरा एडवांस इंजन उस लिंक के हर रिडायरेक्ट और जावास्क्रिप्ट को बाईपास करके असली WhatsApp नंबर निकाल लाएगा।", reply_markup=get_main_menu())
        
    elif text == "📞 सपोर्ट":
        bot.reply_to(message, "👨‍💻 सपोर्ट के लिए एडमिन से संपर्क करें।", reply_markup=get_main_menu())
        
    elif text == "❌ रद्द करें":
        if message.chat.id in user_states:
            del user_states[message.chat.id]
        bot.reply_to(message, "✅ प्रोसेस रद्द कर दिया गया है। मुख्य मेनू में वापस आ गए हैं।", reply_markup=get_main_menu())

@bot.message_handler(func=lambda message: message.text.startswith('http') or user_states.get(message.chat.id, {}).get('state') == 'waiting_for_link')
def handle_url(message):
    url = message.text
    if not url.startswith('http'):
        bot.reply_to(message, "❌ अमान्य लिंक! कृपया 'http://' या 'https://' वाला लिंक भेजें।")
        return
        
    user_states[message.chat.id] = {'state': 'ready_to_extract', 'url': url}
    
    text = (
        "✨ 💎 **L I N K   S A V E D** 💎 ✨\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🎯 **लिंक:** `{url}`\n\n"
        "👇 कृपया नीचे दिए गए मेनू से चुनें कि आप कितनी बार चेक करना चाहते हैं:"
    )
    bot.send_message(message.chat.id, text, parse_mode="Markdown", disable_web_page_preview=True, reply_markup=get_extraction_menu())

# ==========================================
# 6. बैकग्राउंड थ्रेड प्रोसेस (The Core Logic)
# ==========================================
def background_extraction(chat_id, target_url, count):
    msg = bot.send_message(chat_id, f"⏳ **प्रोसेसिंग शुरू...**\n🔄 लिंक को {count} बार स्कैन किया जा रहा है।", parse_mode="Markdown")
    
    extracted_numbers = set()
    errors = 0
    
    # Cookies और Session बरकरार रखने के लिए (एंटी-बॉट बाईपास)
    session = requests.Session()
    
    for i in range(count):
        try:
            # Cache buster अब नंबर्स में नहीं, बल्कि लेटर्स में है ताकि regex कंफ्यूज न हो
            cache_buster = f"cbx_{random.randint(10000,99999)}"
            separator = "&" if "?" in target_url else "?"
            req_url = f"{target_url}{separator}{cache_buster}"
            
            headers = {
                'User-Agent': f'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/{random.randint(90, 120)}.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.5'
            }
            
            response = session.get(req_url, headers=headers, allow_redirects=True, timeout=15)
            
            # स्ट्रिक्ट फंक्शन से नंबर निकालना
            number = extract_wa_number_strict(response.url, response.text)
            
            if number:
                extracted_numbers.add(number)
                
            # स्पैम से बचने के लिए Telegram मैसेज को हर 10 रिक्वेस्ट पर अपडेट करें
            if (i + 1) % 10 == 0:
                try:
                    bot.edit_message_text(f"⏳ **स्कैनिंग जारी...** ({i+1}/{count} चेक किए गए)\n✅ अब तक मिले असली नंबर्स: {len(extracted_numbers)}", chat_id, msg.message_id, parse_mode="Markdown")
                except:
                    pass
                    
            # बहुत हल्का डिले ताकि सर्वर ब्लॉक न करे
            time.sleep(0.5)
                
        except requests.exceptions.RequestException:
            errors += 1
            
    try:
        bot.delete_message(chat_id, msg.message_id)
    except:
        pass
    
    if extracted_numbers:
        update_extraction_count(chat_id, len(extracted_numbers))
        
        result_text = "\n".join([f"📞 `{num}`" for num in extracted_numbers])
        final_message = (
            f"✨ 💎 **R E S U L T S   R E A D Y** 💎 ✨\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🎯 **यूनिक नंबर्स मिले:** {len(extracted_numbers)}\n"
        )
        if errors > 0: final_message += f"⚠️ **फेल रिक्वेस्ट:** {errors}\n\n"
        else: final_message += "\n"
        
        bot.send_message(chat_id, final_message + result_text[:3000], parse_mode="Markdown", reply_markup=get_main_menu())
        
        # .TXT फाइल बनाना
        filename = f"Numbers_{chat_id}_{int(time.time())}.txt"
        with open(filename, "w", encoding="utf-8") as f:
            f.write("\n".join(extracted_numbers))
            
        with open(filename, "rb") as f:
            bot.send_document(chat_id, f, caption="📁 **आपकी फाइल तैयार है!**\nसारे रियल नंबर्स इस .txt फाइल में सेव हैं।", parse_mode="Markdown")
        
        os.remove(filename) 
        
    else:
        bot.send_message(chat_id, "❌ कोई भी असली WhatsApp नंबर नहीं मिल पाया। लिंक का सर्वर डाउन हो सकता है या उसने ब्लॉक कर दिया है।", reply_markup=get_main_menu())

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
    del user_states[chat_id] 
    
    threading.Thread(target=background_extraction, args=(chat_id, target_url, count)).start()

# ==========================================
# 7. बोट रन करना (With Error Handling)
# ==========================================
try:
    bot.remove_webhook()
    time.sleep(1)
except Exception as e:
    pass

print("Bot is successfully running and ready for Railway...")
bot.polling(none_stop=True, timeout=60, long_polling_timeout=60)
