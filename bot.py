import telebot
from telebot.types import ReplyKeyboardMarkup, KeyboardButton
import requests
import time
import os
import random
import re
import threading
import sqlite3
from urllib.parse import urlparse, parse_qs

# ==========================================
# 1. कॉन्फ़िगरेशन
# ==========================================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "अपना_टोकन_यहाँ_डालें")
# एडमिन की Telegram User ID
ADMIN_IDS = [123456789] 

bot = telebot.TeleBot(BOT_TOKEN)
user_states = {}

# ==========================================
# 2. डेटाबेस सेटअप
# ==========================================
def init_db():
    conn = sqlite3.connect('bot_database.db', check_same_thread=False)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, total_extracted INTEGER DEFAULT 0)''')
    conn.commit()
    conn.close()

def add_user(user_id):
    conn = sqlite3.connect('bot_database.db', check_same_thread=False)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (user_id, total_extracted) VALUES (?, 0)", (user_id,))
    conn.commit()
    conn.close()

def update_stats(user_id, count):
    conn = sqlite3.connect('bot_database.db', check_same_thread=False)
    c = conn.cursor()
    c.execute("UPDATE users SET total_extracted = total_extracted + ? WHERE user_id = ?", (count, user_id))
    conn.commit()
    conn.close()

def get_stats(user_id):
    conn = sqlite3.connect('bot_database.db', check_same_thread=False)
    c = conn.cursor()
    c.execute("SELECT total_extracted FROM users WHERE user_id = ?", (user_id,))
    res = c.fetchone()
    conn.close()
    return res[0] if res else 0

init_db()

# ==========================================
# 3. UI कीबोर्ड
# ==========================================
def get_main_menu():
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(KeyboardButton("🔗 नया लिंक भेजें"), KeyboardButton("📊 मेरे आँकड़े"))
    markup.add(KeyboardButton("❓ मदद"), KeyboardButton("📞 सपोर्ट"))
    return markup

def get_extraction_menu():
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(KeyboardButton("🚀 20 बार निकालें"), KeyboardButton("🚀 50 बार निकालें"))
    markup.add(KeyboardButton("💎 100 बार निकालें (Max)"), KeyboardButton("❌ रद्द करें"))
    return markup

# ==========================================
# 4. नंबर एक्सट्रैक्शन लॉजिक (STRICT)
# ==========================================
def get_clean_number(text):
    """केवल अंकों को निकालता है और जांचता है कि यह एक वैध नंबर है या नहीं।"""
    if not text:
        return None
    num = re.sub(r'\D', '', text)
    # भारत के नंबर 91 से शुरू होते हैं, इसलिए कम से कम 10 अंक होने चाहिए
    return num if len(num) >= 10 else None

def extract_number_from_url(url):
    """
    यूआरएल से नंबर निकालता है।
    यह उन पैटर्न को खोजता है जो आपने अपने नेटवर्क लॉग में देखे हैं:
    1. api.whatsapp.com/send/?phone=919264467768
    2. wa.me/919264467768
    3. whatsapp://send?phone=919264467768
    """
    parsed_url = urlparse(url)
    
    # 1. Query parameter 'phone' चेक करें (जैसे api.whatsapp.com/send?phone=...)
    qs = parse_qs(parsed_url.query)
    if 'phone' in qs:
        return get_clean_number(qs['phone'][0])
    
    # 2. Path में नंबर चेक करें (जैसे wa.me/919264467768)
    # यह wa.me या api.whatsapp.com के बाद आने वाले अंकों को खोजता है
    match = re.search(r'(?:wa\.me/|api\.whatsapp\.com/send/|whatsapp://send\?phone=)(\+?\d+)', url, re.IGNORECASE)
    if match:
        return get_clean_number(match.group(1))

    return None

# ==========================================
# 5. बॉट हैंडल्स
# ==========================================
@bot.message_handler(commands=['start'])
def send_welcome(message):
    add_user(message.chat.id)
    bot.send_message(
        message.chat.id, 
        "✨ 💎 **W H A T S A P P   E X T R A C T O R** 💎 ✨\n\n"
        "👋 स्वागत है! कृपया 'नया लिंक भेजें' पर क्लिक करें।", 
        parse_mode="Markdown", 
        reply_markup=get_main_menu()
    )

@bot.message_handler(func=lambda message: message.text in ["🔗 नया लिंक भेजें", "📊 मेरे आँकड़े", "❓ मदद", "📞 सपोर्ट", "❌ रद्द करें"])
def handle_main_buttons(message):
    add_user(message.chat.id)
    if message.text == "🔗 नया लिंक भेजें":
        bot.reply_to(message, "🔗 **कृपया अपना रोटेटिंग लिंक भेजें:**", parse_mode="Markdown", reply_markup=telebot.types.ReplyKeyboardRemove())
        user_states[message.chat.id] = {'state': 'waiting_for_link'}
    elif message.text == "📊 मेरे आँकड़े":
        bot.reply_to(message, f"📊 **आपने कुल `{get_stats(message.chat.id)}` नंबर निकाले हैं!**", parse_mode="Markdown")
    elif message.text == "❌ रद्द करें":
        user_states.pop(message.chat.id, None)
        bot.reply_to(message, "✅ प्रोसेस रद्द कर दिया गया है।", reply_markup=get_main_menu())
    else:
        bot.reply_to(message, "बस लिंक भेजें और मैं नंबर निकाल दूँगा।", reply_markup=get_main_menu())

@bot.message_handler(func=lambda message: message.text.startswith('http') or user_states.get(message.chat.id, {}).get('state') == 'waiting_for_link')
def handle_url(message):
    url = message.text.strip()
    if not url.startswith('http'):
        bot.reply_to(message, "❌ अमान्य लिंक! 'http://' या 'https://' से शुरू होना चाहिए।")
        return
        
    user_states[message.chat.id] = {'state': 'ready_to_extract', 'url': url}
    bot.send_message(
        message.chat.id, 
        f"✨ 💎 **L I N K   S A V E D** 💎 ✨\n\n🎯 **लिंक:** `{url}`\n\n👇 कितनी बार चेक करना है?", 
        parse_mode="Markdown", 
        disable_web_page_preview=True, 
        reply_markup=get_extraction_menu()
    )

# ==========================================
# 6. कोर प्रोसेसिंग थ्रेड
# ==========================================
def background_extraction(chat_id, target_url, count):
    msg = bot.send_message(chat_id, f"⏳ **प्रोसेसिंग शुरू...**\n🔄 लिंक को {count} बार स्कैन किया जा रहा है।", parse_mode="Markdown")
    
    extracted_numbers = set()
    errors = 0
    session = requests.Session()
    
    for i in range(count):
        try:
            # सर्वर कैश को बायपास करने के लिए URL में एक रैंडम पैरामीटर जोड़ें
            # हम 'cb' (cache buster) का उपयोग करते हैं
            sep = "&" if "?" in target_url else "?"
            req_url = f"{target_url}{sep}cb={int(time.time() * 1000)}_{random.randint(1000, 9999)}"
            
            # एक असली ब्राउज़र जैसा दिखने के लिए हेडर
            headers = {
                'User-Agent': f'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{random.randint(110, 125)}.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.9',
                'Upgrade-Insecure-Requests': '1',
                'Sec-Fetch-Dest': 'document',
                'Sec-Fetch-Mode': 'navigate',
                'Sec-Fetch-Site': 'none',
                'Sec-Fetch-User': '?1'
            }
            
            # `allow_redirects=True` महत्वपूर्ण है। यह लिंक को तब तक फॉलो करेगा जब तक वह अंतिम गंतव्य (whatsapp.com) पर न पहुंच जाए।
            response = session.get(req_url, headers=headers, allow_redirects=True, timeout=15)
            
            # प्रतिक्रिया के अंतिम URL (रीडायरेक्ट के बाद) से नंबर निकालने का प्रयास करें
            number = extract_number_from_url(response.url)
            
            # यदि URL में नंबर नहीं है, तो पेज के HTML में खोजने का प्रयास करें
            if not number and response.text:
                # HTML में WhatsApp लिंक खोजें
                html_match = re.search(r'(?:wa\.me/|api\.whatsapp\.com/send\?phone=|whatsapp://send\?phone=)(\+?\d+)', response.text, re.IGNORECASE)
                if html_match:
                     number = get_clean_number(html_match.group(1))

            if number:
                extracted_numbers.add(number)
                
            # हर 10 रिक्वेस्ट पर यूज़र को अपडेट करें
            if (i + 1) % 10 == 0:
                try:
                    bot.edit_message_text(
                        f"⏳ **स्कैनिंग जारी...** ({i+1}/{count})\n✅ अब तक मिले असली नंबर्स: {len(extracted_numbers)}", 
                        chat_id, 
                        msg.message_id, 
                        parse_mode="Markdown"
                    )
                except Exception:
                    pass
                    
            # बहुत तेजी से अनुरोध भेजने से बचने के लिए थोड़ा रुकें
            time.sleep(0.3)
                
        except requests.exceptions.RequestException as e:
            errors += 1
            print(f"Request Error: {e}")
            
    try:
        bot.delete_message(chat_id, msg.message_id)
    except Exception:
        pass
    
    if extracted_numbers:
        update_stats(chat_id, len(extracted_numbers))
        
        result_text = "\n".join([f"📞 `{num}`" for num in extracted_numbers])
        final_message = f"✨ 💎 **R E S U L T S   R E A D Y** 💎 ✨\n\n🎯 **यूनिक नंबर्स मिले:** {len(extracted_numbers)}\n"
        
        if errors > 0:
            final_message += f"⚠️ **फेल रिक्वेस्ट:** {errors}\n\n"
        else:
            final_message += "\n"
        
        bot.send_message(chat_id, final_message + result_text[:3000], parse_mode="Markdown", reply_markup=get_main_menu())
        
        # .txt फ़ाइल बनाएँ
        filename = f"Numbers_{chat_id}.txt"
        with open(filename, "w", encoding="utf-8") as f:
            f.write("\n".join(extracted_numbers))
            
        with open(filename, "rb") as f:
            bot.send_document(chat_id, f, caption="📁 **आपकी फाइल तैयार है!**")
        os.remove(filename) 
        
    else:
        bot.send_message(chat_id, "❌ कोई भी असली WhatsApp नंबर नहीं मिल पाया।", reply_markup=get_main_menu())

@bot.message_handler(func=lambda message: "बार निकालें" in message.text)
def handle_extraction(message):
    chat_id = message.chat.id
    state_data = user_states.get(chat_id, {})
    
    if state_data.get('state') != 'ready_to_extract' or 'url' not in state_data:
        bot.reply_to(message, "❌ **कृपया पहले एक नया लिंक भेजें।**", parse_mode="Markdown", reply_markup=get_main_menu())
        return
        
    count = 0
    if "20" in message.text: count = 20
    elif "50" in message.text: count = 50
    elif "100" in message.text: count = 100
    
    if count == 0: return

    target_url = state_data['url']
    del user_states[chat_id] 
    
    threading.Thread(target=background_extraction, args=(chat_id, target_url, count)).start()

# ==========================================
# 7. बॉट शुरू करें
# ==========================================
if __name__ == "__main__":
    try:
        bot.remove_webhook()
        time.sleep(1)
    except Exception:
        pass

    print("Bot is running perfectly...")
    bot.polling(none_stop=True, timeout=60, long_polling_timeout=60)
