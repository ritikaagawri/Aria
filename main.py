import os
import anthropic
import telebot
import logging

logging.basicConfig(level=logging.INFO)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY")

bot = telebot.TeleBot(TELEGRAM_TOKEN)
client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

SYSTEM_PROMPT = """You are ARIA — Advanced Regulatory Intelligence Advisor — a smart HR assistant for Numero Uno Clothing Limited (NUCL), an apparel manufacturer and retailer based in Gurgaon with ~727 employees.

You help Ritika, Head of HR, with:
- HR compliance under all 4 Indian Labour Codes
- Performance management, KPIs, appraisals, increment calculations
- Drafting HR communications, JDs, policies
- Payroll and greytHR related queries
- Employee data analysis

Always be direct, practical, and concise. No corporate fluff. Use plain language.
If you don't know something, say so clearly rather than guessing.
Always flag legal compliance risks explicitly."""

conversation_history = {}

@bot.message_handler(commands=['start'])
def start(message):
    bot.reply_to(message, "Hi Ritika! ARIA is back online. How can I help you today?")

@bot.message_handler(commands=['clear'])
def clear(message):
    conversation_history[message.chat.id] = []
    bot.reply_to(message, "Conversation cleared. Fresh start!")

@bot.message_handler(func=lambda message: True)
def handle_message(message):
    chat_id = message.chat.id
    user_text = message.text

    if chat_id not in conversation_history:
        conversation_history[chat_id] = []

    conversation_history[chat_id].append({
        "role": "user",
        "content": user_text
    })

    if len(conversation_history[chat_id]) > 10:
        conversation_history[chat_id] = conversation_history[chat_id][-10:]

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            system=SYSTEM_PROMPT,
            messages=conversation_history[chat_id]
        )

        reply = response.content[0].text

        conversation_history[chat_id].append({
            "role": "assistant",
            "content": reply
        })

        bot.reply_to(message, reply)

    except Exception as e:
        logging.error(f"Error: {e}")
        bot.reply_to(message, f"Something went wrong. Error: {str(e)}")

if __name__ == "__main__":
    logging.info("ARIA is starting...")
    bot.infinity_polling()