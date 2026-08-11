import telebot
from telebot.types import ReplyKeyboardMarkup, KeyboardButton
import requests
import time
import os
import random
import re
import urllib.parse
from urllib.parse import urlparse, parse_qs, urljoin
import sqlite3
import threading

# ==========================================
# 1. कॉन्फ़िगरेशन (Configuration)
# ==========================================
# अपना बॉट टोकन यहाँ डालें या Railway के Environment Variable में BOT_TOKEN सेट करें
BOT_TOKEN = os.environ.get("BOT_TOKEN", "अपना_टोकन_यहाँ_डालें")

# नीचे अपनी Telegram User ID डालें (एडमिन फीचर्स के लिए)
ADMIN_IDS = [123456789] 

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
# 4. कोर इंजन: Deep WhatsApp Number Extractor
# ==========================================
def get_clean_number(text_string):
    """सिर्फ प्योर 10-15 डिजिट के असली नंबर्स निकालता है (URL Encoded टेक्स्ट को क्लीन करके)"""
    decoded_text = urllib.parse.unquote(text_string)
    num = re.sub(r'\D', '', decoded_text)
    if 10 <= len(num) <= 15:
        return num
    return None

def extract_wa_number_strict(url, html_content=""):
    """
    अल्ट्रा-स्ट्रिक्ट नंबर एक्सट्रैक्टर। यह WhatsApp लिंक्स, Intents और JSON API को भी स्कैन करता है।
    """
    patterns = [
        r'wa\.me/([+%]?\d+)',
        r'api\.whatsapp\.com/send\/?\?phone=([+%]?\d+)',
        r'whatsapp://send\/?\?phone=([+%]?\d+)',
        r'intent://send\/?\?phone=([+%]?\d+)',
        r'(?:phone|number|whatsapp)=([+%]?\d{10,15})',
        r'href=["\'](?:whatsapp|intent)://send\?phone=([+%]?\d+)',
        r'["\'](?:phone|whatsapp|number)["\']\s*:\s*["\']?([+%]?\d{10,15})["\']?' 
    ]
    
    # 1. URL के अंदर चेक करें
    for pattern in patterns:
        match = re.search(pattern, url, re.IGNORECASE)
        if match:
            res = get_clean_number(match.group(1))
            if res: return res

    # 2. HTML या JavaScript सोर्स कोड के अंदर चेक करें
    if html_content:
        for pattern in patterns:
            matches = re.findall(pattern, html_content, re.IGNORECASE)
            for m in matches:
                res = get_clean_number(m)
                if res: return res

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
        "🚀 **एडवांस्ड डीप-स्कैन इंजन (V3.0)**\n"
        "🛡 **फीचर्स:** JS Bypass • Intent Crawler • .TXT एक्सपोर्ट\n\n"
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
        bot.reply_to(message, "💡 अपना लिंक मुझे दें। मेरा एडवांस्ड डीप-स्कैन इंजन ब्राउज़र की तरह काम करता है। यह छुपे हुए जावास्क्रिप्ट और रीडायरेक्ट्स के अंदर घुसकर नंबर निकाल लाएगा।", reply_markup=get_main_menu())
        
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
        "👇 कृपया नीचे दिए गए मेनू से चुनें कि आप कितनी बार स्कैन करना चाहते हैं:"
    )
    bot.send_message(message.chat.id, text, parse_mode="Markdown", disable_web_page_preview=True, reply_markup=get_extraction_menu())

# ==========================================
# 6. बैकग्राउंड थ्रेड प्रोसेस (The Core Javascript/Meta Bypass Engine)
# ==========================================
def background_extraction(chat_id, target_url, count):
    msg = bot.send_message(chat_id, f"⏳ **डीप स्कैनिंग शुरू...**\n🔄 इंजन आपके लिंक को {count} बार प्रोसेस कर रहा है।", parse_mode="Markdown")
    
    extracted_numbers = set()
    errors = 0
    
    for i in range(count):
        try:
            # हर रिक्वेस्ट के लिए बिल्कुल नया सेशन और कुकीज़ ताकि सर्वर ब्लॉक न करे
            session = requests.Session()
            
            # रैंडम मोबाइल User-Agent से रिक्वेस्ट भेजें ताकि हम असली Android यूज़र लगें
            headers = {
                'User-Agent': f'Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{random.randint(110, 125)}.0.0.0 Mobile Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
                'Accept-Language': 'en-IN,en-US;q=0.9,en;q=0.8',
                'Cache-Control': 'no-cache',
                'Pragma': 'no-cache',
                'Upgrade-Insecure-Requests': '1',
                'Sec-Fetch-Dest': 'document',
                'Sec-Fetch-Mode': 'navigate',
                'Sec-Fetch-Site': 'none'
            }
            
            current_url = target_url
            number_found = None
            
            # Hop Loop: जावास्क्रिप्ट और मेटा रीडायरेक्ट्स को ट्रैक करने के लिए 3 लेवल डीप स्कैनिंग
            for hop in range(3):
                response = session.get(current_url, headers=headers, allow_redirects=True, timeout=15)
                html_content = response.text
                final_url = response.url
                
                # 1. पहले डायरेक्ट चेक करें कि क्या नंबर मिल गया है
                number_found = extract_wa_number_strict(final_url, html_content)
                if number_found:
                    break
                    
                # 2. अगर नहीं मिला, तो HTML के अंदर छुपे हुए JavaScript या Meta Refresh को ढूंढें
                meta_match = re.search(r'(?i)<meta[^>]*http-equiv=["\']?refresh["\']?[^>]*content=["\']?\d+;\s*url=([^"\'>]+)["\']?', html_content)
                js_match = re.search(r'(?i)window\.location\.(?:replace|href)\s*=\s*["\']([^"\']+)["\']', html_content)
                
                next_url = None
                if meta_match:
                    next_url = meta_match.group(1).strip()
                elif js_match:
                    next_url = js_match.group(1).strip()
                    
                if next_url:
                    # अगर रीडायरेक्ट सीधा WhatsApp Deep Link है, तो उसमें से नंबर निकाल लें
                    if next_url.startswith('whatsapp://') or next_url.startswith('intent://'):
                        number_found = extract_wa_number_strict(next_url, "")
                        break
                        
                    # अगर रिलेटिव लिंक (/next-page) है तो उसे ओरिजिनल डोमेन से जोड़ें
                    if not next_url.startswith('http'):
                        next_url = urljoin(final_url, next_url)
                        
                    current_url = next_url
                    time.sleep(0.5) # जावास्क्रिप्ट रन होने का सिमुलेटेड टाइम
                else:
                    break # कोई और रीडायरेक्ट नहीं मिला
                    
            if number_found:
                extracted_numbers.add(number_found)
                
            # हर 5 रिक्वेस्ट पर यूज़र को लाइव अपडेट दें
            if (i + 1) % 5 == 0:
                try:
                    bot.edit_message_text(f"⏳ **डीप स्कैनिंग जारी...** ({i+1}/{count})\n✅ अब तक मिले असली नंबर्स: {len(extracted_numbers)}", chat_id, msg.message_id, parse_mode="Markdown")
                except:
                    pass
                    
            # सर्वर के फायरवॉल से बचने के लिए हर रिक्वेस्ट के बीच एक छोटा सा ब्रेक
            time.sleep(1.2)
                
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
        if errors > 0: final_message += f"⚠️ **फेल रिक्वेस्ट (सर्वर ब्लॉक):** {errors}\n\n"
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
        bot.send_message(chat_id, "❌ कोई भी असली WhatsApp नंबर नहीं मिल पाया। लिंक को डीप-स्कैन किया गया, लेकिन रिस्पांस में सिर्फ ब्लैंक पेज या सिक्योरिटी कैप्चा आया है।", reply_markup=get_main_menu())

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

print("Bot is successfully running with Deep-Scan Engine on Railway...")
bot.polling(none_stop=True, timeout=60, long_polling_timeout=60)
