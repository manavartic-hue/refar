import telebot
from telebot.types import ReplyKeyboardMarkup, KeyboardButton
import requests
import time
import os
import random
import re
from urllib.parse import urlparse, parse_qs, unquote, urljoin
import sqlite3
import threading

# ==========================================
# 1. Configuration 
# ==========================================
# अपना बॉट टोकन यहाँ डालें या Railway Environment Variables में सेट करें
BOT_TOKEN = os.environ.get("BOT_TOKEN", "अपना_टोकन_यहाँ_डालें")
ADMIN_IDS = [123456789] # अपनी Telegram ID यहाँ डालें!

bot = telebot.TeleBot(BOT_TOKEN)

# ==========================================
# 2. Database Setup (SQLite)
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
# 3. User Interface (Keyboards)
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
# 4. Core Engine: JS/Meta Redirect Scanner
# ==========================================
def get_clean_number(text_string):
    """सिर्फ प्योर 10-15 डिजिट नंबर्स निकालता है"""
    num = re.sub(r'\D', '', text_string)
    return num if len(num) >= 10 else None

def extract_wa_number_ultra(start_url, session, headers):
    """
    यह अल्ट्रा-स्कैनर है। यह JavaScript और HTML Meta Redirects को 
    फॉलो करके छिपे हुए असली WhatsApp नंबर को खोज निकालता है।
    """
    current_url = start_url
    
    # 4 बार तक छिपे हुए रिडायरेक्ट्स को फॉलो करेगा (JS / Meta tags)
    for _ in range(4): 
        try:
            response = session.get(current_url, headers=headers, allow_redirects=True, timeout=12)
            
            # HTML को डिकोड करना ताकि छिपे हुए लिंक साफ हो जाएं
            html = unquote(response.text).replace('\\/', '/').replace('\\"', '"')
            final_url = unquote(response.url)
            
            patterns = [
                r'wa\.me/(\d{10,15})', 
                r'phone=(\d{10,15})',
                r'whatsapp://send\?phone=(\d{10,15})',
                r'api\.whatsapp\.com/send\?phone=(\d{10,15})'
            ]
            
            # 1. पहले URL में नंबर खोजें (अगर डायरेक्ट पहुँच गया हो)
            for p in patterns:
                m = re.search(p, final_url, re.IGNORECASE)
                if m: return get_clean_number(m.group(1))
                
            # 2. HTML सोर्स कोड में नंबर खोजें (JS के अंदर छिपा हो तो)
            for p in patterns:
                m = re.search(p, html, re.IGNORECASE)
                if m: return get_clean_number(m.group(1))
                
            # 3. अगर नंबर नहीं मिला, तो HTML Meta Refresh रिडायरेक्ट खोजें
            meta_match = re.search(r'meta.*?url=["\']?([^"\'>]+)["\']?', html, re.IGNORECASE)
            if meta_match:
                current_url = urljoin(response.url, meta_match.group(1))
                continue
                
            # 4. JavaScript रिडायरेक्ट (window.location) खोजें
            js_match = re.search(r'location\.(?:replace|href|assign)\s*=\s*["\']([^"\']+)["\']', html, re.IGNORECASE)
            if js_match:
                current_url = urljoin(response.url, js_match.group(1))
                continue
                
            break # अगर कोई रिडायरेक्ट या नंबर नहीं मिला, तो लूप तोड़ दें
        except Exception:
            break
            
    return None

# ==========================================
# 5. Bot Commands
# ==========================================
@bot.message_handler(commands=['start'])
def send_welcome(message):
    add_user(message.chat.id)
    text = (
        "✨ 💎 **W H A T S A P P   E X T R A C T O R** 💎 ✨\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👋 **नमस्ते {message.from_user.first_name}!**\n\n"
        "🚀 **एडवांस्ड नंबर एक्सट्रैक्टर (Pro JS Bypass)**\n"
        "🛡 **फीचर्स:** Anti-Block • JS Follower • 100% Accuracy\n\n"
        "👇 काम शुरू करने के लिए **'नया लिंक भेजें'** पर क्लिक करें।"
    )
    bot.send_message(message.chat.id, text, parse_mode="Markdown", reply_markup=get_main_menu())

@bot.message_handler(func=lambda message: message.text in ["🔗 नया लिंक भेजें", "📊 मेरे आँकड़े (Stats)", "❓ मदद", "📞 सपोर्ट", "❌ रद्द करें"])
def handle_main_buttons(message):
    add_user(message.chat.id)
    text = message.text
    
    if text == "🔗 नया लिंक भेजें":
        bot.reply_to(message, "🔗 **कृपया अपना रोटेटिंग (Rotating) लिंक भेजें:**", parse_mode="Markdown", reply_markup=telebot.types.ReplyKeyboardRemove())
        user_states[message.chat.id] = {'state': 'waiting_for_link'}
        
    elif text == "📊 मेरे आँकड़े (Stats)":
        stats = get_user_stats(message.chat.id)
        bot.reply_to(message, f"📊 **आपके आँकड़े:**\nआपने अब तक कुल `{stats}` असली नंबर्स निकाले हैं!", parse_mode="Markdown")
        
    elif text == "❓ मदद":
        bot.reply_to(message, "💡 अपना लिंक दें। मेरा नया इंजन JS और Meta redirects को क्रैक करके असली नंबर निकाल लाएगा।", reply_markup=get_main_menu())
        
    elif text == "📞 सपोर्ट":
        bot.reply_to(message, "👨‍💻 सपोर्ट के लिए एडमिन से संपर्क करें।", reply_markup=get_main_menu())
        
    elif text == "❌ रद्द करें":
        if message.chat.id in user_states:
            del user_states[message.chat.id]
        bot.reply_to(message, "✅ प्रोसेस रद्द। मुख्य मेनू चालू है।", reply_markup=get_main_menu())

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
        "👇 कितनी बार चेक करना चाहते हैं?"
    )
    bot.send_message(message.chat.id, text, parse_mode="Markdown", disable_web_page_preview=True, reply_markup=get_extraction_menu())

# ==========================================
# 6. Extraction Process (The Real Magic)
# ==========================================
def background_extraction(chat_id, target_url, count):
    msg = bot.send_message(chat_id, f"⏳ **प्रोसेसिंग शुरू...**\n🔄 लिंक को {count} बार बाईपास किया जा रहा है।", parse_mode="Markdown")
    
    extracted_numbers = set()
    errors = 0
    
    for i in range(count):
        try:
            # हर बार नया सेशन ताकि रोटेटिंग सर्वर को लगे कि एक नया व्यक्ति लिंक खोल रहा है!
            session = requests.Session()
            
            headers = {
                'User-Agent': f'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/{random.randint(90, 120)}.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.5',
                'Connection': 'keep-alive',
                'Upgrade-Insecure-Requests': '1'
            }
            
            # अल्ट्रा स्कैनर फंक्शन को कॉल करें
            number = extract_wa_number_ultra(target_url, session, headers)
            
            if number:
                extracted_numbers.add(number)
                
            # हर 10 रिक्वेस्ट पर अपडेट (ताकि स्पैम बैन न लगे)
            if (i + 1) % 10 == 0:
                try:
                    bot.edit_message_text(f"⏳ **स्कैनिंग जारी...** ({i+1}/{count} चेक किए गए)\n✅ अब तक मिले असली नंबर्स: {len(extracted_numbers)}", chat_id, msg.message_id, parse_mode="Markdown")
                except:
                    pass
                    
            time.sleep(0.5) # Anti-Block Delay
                
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
        
        # .TXT फाइल तैयार करना
        filename = f"Numbers_{chat_id}_{int(time.time())}.txt"
        with open(filename, "w", encoding="utf-8") as f:
            f.write("\n".join(extracted_numbers))
            
        with open(filename, "rb") as f:
            bot.send_document(chat_id, f, caption="📁 **आपकी फाइल तैयार है!**\nसारे रियल नंबर्स इस .txt फाइल में सेव हैं।", parse_mode="Markdown")
        
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
        
    count = 20 if "20" in message.text else (50 if "50" in message.text else (100 if "100" in message.text else 0))
    if count == 0: return

    target_url = state_data['url']
    del user_states[chat_id] 
    
    threading.Thread(target=background_extraction, args=(chat_id, target_url, count)).start()

# ==========================================
# 7. Start Bot (With Conflict Handler)
# ==========================================
try:
    bot.remove_webhook()
    time.sleep(1)
except Exception as e:
    pass

print("Bot is successfully running and ready for Railway...")
bot.polling(none_stop=True, timeout=60, long_polling_timeout=60)
