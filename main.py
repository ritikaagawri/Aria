import os
import json
import time
import logging
import requests
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import anthropic
import telebot
from apscheduler.schedulers.background import BackgroundScheduler

# ─────────────────────────────────────────────
# SETUP
# ─────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN")
ANTHROPIC_KEY    = os.environ.get("ANTHROPIC_API_KEY")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

IST             = ZoneInfo("Asia/Kolkata")
HOLD_QUEUE_FILE = "hold_queue.json"

bot    = telebot.TeleBot(TELEGRAM_TOKEN)
client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

# ─────────────────────────────────────────────
# NUCL DATA LOADER
# ─────────────────────────────────────────────

def load_nucl_data():
    for fname in ["nucl_data.txt", "nucl_data.json"]:
        if os.path.exists(fname):
            with open(fname) as f:
                data = f.read().strip()
                if data:
                    log.info(f"NUCL data loaded from {fname}")
                    return data
    log.info("No NUCL data file found.")
    return ""

NUCL_DATA = load_nucl_data()

# ─────────────────────────────────────────────
# SYSTEM PROMPT
# ─────────────────────────────────────────────

SYSTEM_PROMPT = """You are ARIA — Ritika's personal AI assistant, available on Telegram 24/7.

WHO YOU SERVE:
Ritika Gawri — Head of HR at Numero Uno Clothing Limited (NUCL), an apparel manufacturer and retailer headquartered in Gurgaon with factories in Manesar and Selaqui, and retail stores pan-India.

NAME VARIATIONS: "NU", "NUCL", "Numero Uno", "Numero", "Numero Uno Clothing", "Numero Uno Clothing Limited" all refer to the same company — always treat them as NUCL.

YOUR DUTIES:

1. HR & COMPLIANCE
   - Indian Labour Code compliance (all 4 codes)
   - Performance management, appraisals, KPIs, increment calculations
   - Drafting HR communications, job descriptions, policies
   - Payroll queries, greytHR and Megasoft support
   - Employee relations, PIP, disciplinary matters

2. NEWS & INTELLIGENCE
   - You deliver real-time news briefs using web search
   - Morning brief auto-sent at 7 AM IST daily
   - Use /news for on-demand brief
   - Covering: geopolitics, Indian economy, markets, labour law, retail and apparel, HR tech
   - Each brief ends with a NUCL IMPACT section flagging what directly affects NUCL
   - Urgent alerts during market hours for breaking developments

3. PERSONAL ASSISTANCE
   - Retirement and financial planning
   - Investment research and portfolio questions
   - Any research, drafting, analysis Ritika needs

YOUR STYLE:
- Direct, practical, no corporate fluff
- Plain language, not formal HR-speak
- Flag compliance risks clearly
- Never guess on legal matters
- Concise unless detail is needed
- No markdown — no **, no ##, no ---
- Use HTML formatting only: <b> for bold"""

if NUCL_DATA:
    SYSTEM_PROMPT += f"""

─────────────────────────────────────
NUCL ORGANISATION DATA
─────────────────────────────────────
{NUCL_DATA}
─────────────────────────────────────"""

# ─────────────────────────────────────────────
# CONVERSATION MEMORY
# ─────────────────────────────────────────────

conversation_history = {}

def get_history(chat_id):
    if chat_id not in conversation_history:
        conversation_history[chat_id] = []
    return conversation_history[chat_id]

def add_to_history(chat_id, role, content):
    get_history(chat_id)
    conversation_history[chat_id].append({"role": role, "content": content})
    if len(conversation_history[chat_id]) > 20:
        conversation_history[chat_id] = conversation_history[chat_id][-20:]

# ─────────────────────────────────────────────
# TELEGRAM HELPERS
# ─────────────────────────────────────────────

def clean_text(text):
    return (text
        .replace("**", "")
        .replace("## ", "")
        .replace("### ", "")
        .replace("# ", "")
        .replace("---", "")
        .replace("***", ""))

def send_message(text, chat_id):
    if not text or not text.strip():
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    text = clean_text(text)
    for chunk in [text[i:i+4000] for i in range(0, len(text), 4000)]:
        try:
            requests.post(url, json={
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "HTML"
            }, timeout=10)
        except Exception as e:
            log.error(f"Telegram send error: {e}")
        time.sleep(0.3)

def send_broadcast(text):
    send_message(text, TELEGRAM_CHAT_ID)

# ─────────────────────────────────────────────
# JSON HELPERS
# ─────────────────────────────────────────────

def load_json(filepath, default):
    try:
        if os.path.exists(filepath):
            with open(filepath) as f:
                return json.load(f)
    except Exception:
        pass
    return default

def save_json(filepath, data):
    try:
        with open(filepath, "w") as f:
            json.dump(data, f)
    except Exception as e:
        log.error(f"Save JSON error: {e}")

# ─────────────────────────────────────────────
# NEWS PROMPTS
# ─────────────────────────────────────────────

NEWS_PROMPT = """You are a news researcher and briefing writer for Ritika Gawri, Head of HR at Numero Uno Clothing Limited (NUCL).

ABOUT NUCL:
- Apparel manufacturer and retailer, headquartered in Gurgaon
- Factories in Manesar and Selaqui, retail stores pan-India
- NAME VARIATIONS: "NU", "NUCL", "Numero Uno", "Numero", "Numero Uno Clothing", "Numero Uno Clothing Limited" all refer to NUCL

YOUR TASK:
Search the web RIGHT NOW for today's latest real news. Use only reliable sources: Reuters, Bloomberg, Economic Times, Business Standard, Mint, Hindu BusinessLine, LiveMint, NDTV Profit, MoneyControl, PTI.

SECTIONS TO COVER:
1. Global — geopolitics, wars, sanctions, major world events (8-10 bullets)
2. India Economy — RBI, rupee, inflation, government policy, budget (8-10 bullets)
3. Markets — Nifty, Sensex, FII/DII flows, key stock moves (3-5 bullets)
4. Retail and Apparel India — textile, fashion, GST, exports, consumer trends (3-5 bullets)
5. Labour and Compliance India — labour codes, EPFO, ESIC, minimum wage, court rulings (3-5 bullets)
6. HR and Workforce India — hiring trends, layoffs, gig economy, workforce data (2-3 bullets)
7. HR Technology — HRMS, payroll tech, Darwinbox, greytHR, AI in HR (2-3 bullets)
8. NUCL Impact — from ALL news above, pick 3-5 items that directly affect NUCL operations, costs, compliance, or workforce. Write each as: [news item] — [specific implication for NUCL in one line]

STRICT RULES:
- ONLY real news from today or yesterday — never fabricate
- No markdown: no **, no ##, no ---, no asterisks
- HTML only: use <b> for section headers
- Each bullet: one line, max 15 words, fact-first, no source name
- If a section has no real news, skip it entirely
- Do not pad with old or irrelevant stories

OUTPUT FORMAT:
<b>📰 News Brief — {DATE} IST</b>

<b>🌍 GLOBAL</b>
• [headline]

<b>🇮🇳 INDIA ECONOMY</b>
• [headline]

<b>📈 MARKETS</b>
• [headline]

<b>🏭 RETAIL & APPAREL</b>
• [headline]

<b>⚖️ LABOUR & COMPLIANCE</b>
• [headline]

<b>👥 HR & WORKFORCE</b>
• [headline]

<b>🤖 HR TECH</b>
• [headline]

<b>🎯 NUCL IMPACT</b>
• [news item] — [implication for NUCL]"""

URGENT_PROMPT = """You are an urgent news detector for Ritika Gawri, Head of HR at Numero Uno Clothing Limited (NUCL), an Indian apparel company.

Search the web RIGHT NOW for breaking news in the last 2 hours crossing these hard thresholds:
- Nifty or Sensex move greater than 1.5% intraday
- RBI unscheduled announcement
- War escalation, major attack, new sanctions
- Brent crude move greater than 3% in a session
- Supreme Court ruling on labour or employment
- EPFO or ESIC rate change or major circular
- US Fed surprise action
- Major country default or financial crisis
- India textile, garment, or GST sudden change
- Any development directly impacting Indian apparel exports or retail

RULES:
- Only report if something genuinely qualifies
- No markdown, no **, no ##
- Use plain text with HTML only

If something qualifies output:
⚡ <b>URGENT — [Category]</b>
• [What happened, max 15 words]
• [Why it matters for NUCL or Indian HR/markets, max 15 words]

If NOTHING qualifies output exactly: NONE"""

# ─────────────────────────────────────────────
# NEWS FETCH VIA CLAUDE WEB SEARCH
# ─────────────────────────────────────────────

def fetch_news_brief(chat_id, triggered=False):
    now_str = datetime.now(IST).strftime("%d %b %Y, %I:%M %p")
    prompt  = NEWS_PROMPT.replace("{DATE}", now_str)
    label   = "on-demand" if triggered else "scheduled"
    log.info(f"Fetching {label} news brief via Claude web search...")

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=3000,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            system=prompt,
            messages=[{
                "role": "user",
                "content": f"Search for today's latest news and send the full brief. Today is {now_str} IST."
            }]
        )

        result = ""
        for block in response.content:
            if hasattr(block, "text"):
                result += block.text

        if result.strip():
            send_message(result.strip(), chat_id)
            log.info("News brief sent.")
        else:
            send_message("Could not retrieve news right now. Try again shortly.", chat_id)

    except Exception as e:
        log.error(f"News fetch error: {e}")
        send_message(f"News fetch failed: {str(e)}", chat_id)

def check_urgent_alerts():
    log.info("Checking urgent alerts...")
    now_str = datetime.now(IST).strftime("%d %b %Y, %I:%M %p")

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=500,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            system=URGENT_PROMPT,
            messages=[{
                "role": "user",
                "content": f"Check for urgent breaking news right now. Time: {now_str} IST."
            }]
        )

        result = ""
        for block in response.content:
            if hasattr(block, "text"):
                result += block.text

        result = result.strip()
        if result and result != "NONE":
            send_broadcast(result)
            log.info("Urgent alert sent.")
        else:
            log.info("No urgent news.")

    except Exception as e:
        log.error(f"Urgent alert error: {e}")

def morning_brief_job():
    fetch_news_brief(chat_id=TELEGRAM_CHAT_ID, triggered=False)

# ─────────────────────────────────────────────
# BOT COMMAND HANDLERS
# ─────────────────────────────────────────────

@bot.message_handler(commands=['start'])
def cmd_start(message):
    send_message(
        "Hi Ritika! ARIA is online.\n\n"
        "<b>Commands:</b>\n"
        "/news — fetch live news brief\n"
        "/clear — clear conversation history\n"
        "/help — show this menu\n\n"
        "Or just type anything — I am here.",
        message.chat.id
    )

@bot.message_handler(commands=['help'])
def cmd_help(message):
    send_message(
        "<b>ARIA — Your Personal AI Assistant</b>\n\n"
        "/news — Live news brief with NUCL impact\n"
        "/clear — Fresh conversation start\n\n"
        "<b>I handle:</b>\n"
        "• HR and labour compliance (NUCL)\n"
        "• Live news briefs at 7 AM IST daily\n"
        "• NUCL impact analysis in every brief\n"
        "• Financial and retirement planning\n"
        "• Any research or drafting you need",
        message.chat.id
    )

@bot.message_handler(commands=['clear'])
def cmd_clear(message):
    conversation_history[message.chat.id] = []
    send_message("Conversation cleared. Fresh start!", message.chat.id)

@bot.message_handler(commands=['news'])
def cmd_news(message):
    send_message("Searching live news... give me a moment.", message.chat.id)
    threading.Thread(
        target=fetch_news_brief,
        kwargs={"chat_id": message.chat.id, "triggered": True},
        daemon=True
    ).start()

# General handler — only non-command messages
@bot.message_handler(func=lambda message: message.text is not None and not message.text.startswith('/'))
def handle_message(message):
    chat_id   = message.chat.id
    user_text = message.text

    add_to_history(chat_id, "user", user_text)

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1500,
            system=SYSTEM_PROMPT,
            messages=get_history(chat_id)
        )
        reply = response.content[0].text
        add_to_history(chat_id, "assistant", reply)
        send_message(reply, chat_id)

    except Exception as e:
        log.error(f"Claude error: {e}")
        send_message(f"Something went wrong: {str(e)}", chat_id)

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

if __name__ == "__main__":
    scheduler = BackgroundScheduler(timezone=IST)

    # Morning brief — 7 AM IST daily
    scheduler.add_job(morning_brief_job, "cron", hour=7, minute=0)

    # Urgent alerts — market hours Mon-Fri
    scheduler.add_job(check_urgent_alerts, "cron",
                      hour="9,10,11,12,13,14,15", minute=30,
                      day_of_week="mon-fri")
    scheduler.add_job(check_urgent_alerts, "cron",
                      hour=9, minute=0, day_of_week="mon-fri")
    scheduler.add_job(check_urgent_alerts, "cron",
                      hour=15, minute=55, day_of_week="mon-fri")

    scheduler.start()
    log.info("ARIA is online.")
    bot.infinity_polling()
