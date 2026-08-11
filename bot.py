import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import requests
import time
import os
import random

# Railway के Environment Variables से टोकन लेगा
BOT_TOKEN = os.environ.get("BOT_TOKEN", "यहाँ_अपना_टोकन_डालें_अगर_लोकल_टेस्ट_कर_रहे_हैं")
bot = telebot.TeleBot(BOT_TOKEN)

# यूज़र का डेटा स्टोर करने के लिए डिक्शनरी
user_data = {}

@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.reply_to(message, "👋 नमस्ते! मुझे वह **लिंक** भेजें जिससे आप WhatsApp नंबर्स निकालना चाहते हैं।")

@bot.message_handler(func=lambda message: message.text.startswith('http'))
def handle_url(message):
    url = message.text
    user_data[message.chat.id] = {'url': url}
    
    # बटन्स बनाना
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("20 बार (Min)", callback_data="extract_20"),
        InlineKeyboardButton("50 बार", callback_data="extract_50")
    )
    markup.row(
        InlineKeyboardButton("100 बार (Max)", callback_data="extract_100")
    )
    
    bot.reply_to(message, "✅ लिंक सेव हो गया। आप इस लिंक से कितनी बार नंबर निकालना चाहते हैं?", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith('extract_'))
def handle_extraction(call):
    chat_id = call.message.chat.id
    count = int(call.data.split('_')[1])
    
    if chat_id not in user_data or 'url' not in user_data[chat_id]:
        bot.answer_callback_query(call.id, "❌ कृपया लिंक दोबारा भेजें।")
        return
        
    target_url = user_data[chat_id]['url']
    
    bot.edit_message_text(f"⏳ प्रोसेसिंग शुरू हो गई है...\nलिंक को {count} बार चेक किया जा रहा है। कृपया प्रतीक्षा करें।", 
                          chat_id=chat_id, 
                          message_id=call.message.message_id)
    
    extracted_urls = set() # Set का उपयोग ताकि डुप्लीकेट नंबर्स न आएँ
    errors = 0
    
    for i in range(count):
        try:
            # कैशिंग को बायपास करने के लिए रैंडम स्ट्रिंग जोड़ना
            separator = "&" if "?" in target_url else "?"
            req_url = f"{target_url}{separator}nocache={time.time()}_{random.randint(1000, 9999)}"
            
            headers = {
                'User-Agent': f'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{random.randint(90, 120)}.0.0.0 Safari/537.36'
            }
            
            # allow_redirects=True से यह फाइनल WhatsApp लिंक तक जाएगा
            response = requests.get(req_url, headers=headers, allow_redirects=True, timeout=10)
            final_url = response.url
            
            extracted_urls.add(final_url)
            
        except requests.exceptions.RequestException:
            errors += 1
            
    # रिजल्ट को फॉर्मेट करना
    if extracted_urls:
        result_text = "\n".join(extracted_urls)
        final_message = f"🎯 **निकाले गए नंबर्स/लिंक्स ({len(extracted_urls)} यूनिक):**\n\n{result_text}"
        if errors > 0:
            final_message += f"\n\n⚠️ {errors} रिक्वेस्ट फेल हो गईं (शायद सर्वर डाउन या टाइमआउट)।"
    else:
        final_message = "❌ कोई भी लिंक एक्सट्रैक्ट नहीं हो पाया।"
        
    # अगर मैसेज बहुत लंबा है, तो Telegram लिमिट (4096) हैंडल करना
    if len(final_message) > 4000:
        bot.send_message(chat_id, final_message[:4000] + "\n\n... (मैसेज बहुत लंबा होने के कारण कट गया है)")
    else:
        bot.send_message(chat_id, final_message)

bot.polling(none_stop=True)
