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
NEWS_API_KEY     = os.environ.get("NEWS_API_KEY")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

IST             = ZoneInfo("Asia/Kolkata")
HOLD_QUEUE_FILE = "hold_queue.json"
SENT_IDS_FILE   = "sent_ids.json"

bot    = telebot.TeleBot(TELEGRAM_TOKEN)
client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

# ─────────────────────────────────────────────
# SYSTEM PROMPT
# ─────────────────────────────────────────────

SYSTEM_PROMPT = """You are ARIA — Ritika's personal AI assistant, available on Telegram 24/7.

WHO YOU SERVE:
Ritika Gawri — Head of HR at Numero Uno Clothing Limited (NUCL), an apparel manufacturer and retailer headquartered in Gurgaon with factories in Manesar and Selaqui, and retail stores pan-India.

YOUR DUTIES:

1. HR & COMPLIANCE (Professional)
   - Indian Labour Code compliance (all 4 codes)
   - Performance management, appraisals, KPIs, increment calculations
   - Drafting HR communications, job descriptions, policies
   - Payroll queries, greytHR and Megasoft support
   - Employee relations, PIP, disciplinary matters

2. NEWS & INTELLIGENCE
   - You deliver morning news briefs at 7 AM IST daily via /news command
   - Covering: geopolitics, Indian economy, markets, labour law, retail & apparel, HR tech
   - Urgent alerts during market hours for breaking developments
   - Always frame news in context of what it means for Ritika, NUCL, or Indian markets

3. PERSONAL ASSISTANCE
   - Retirement and financial planning
   - Investment research and portfolio questions
   - Any research, drafting, analysis Ritika needs

YOUR STYLE:
- Direct, practical, no corporate fluff
- Plain language, not formal HR-speak
- Flag compliance risks clearly
- If unsure, say so — never guess on legal matters
- Keep responses concise unless detail is needed

You are NOT a generic chatbot. You are Ritika's dedicated assistant who knows her context."""

# ─────────────────────────────────────────────
# CONVERSATION MEMORY
# ─────────────────────────────────────────────

conversation_history = {}

def get_history(chat_id):
    if chat_id not in conversation_history:
        conversation_history[chat_id] = []
    return conversation_history[chat_id]

def add_to_history(chat_id, role, content):
    get_history(chat_id).append({"role": role, "content": content})
    if len(conversation_history[chat_id]) > 20:
        conversation_history[chat_id] = conversation_history[chat_id][-20:]

# ─────────────────────────────────────────────
# TELEGRAM HELPERS
# ─────────────────────────────────────────────

def send_message(text, chat_id):
    if not text or not text.strip():
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
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
# BOT COMMANDS
# ─────────────────────────────────────────────

@bot.message_handler(commands=['start'])
def cmd_start(message):
    send_message(
        "Hi Ritika! ARIA is online.\n\n"
        "Commands:\n"
        "/news — fetch today's news brief\n"
        "/clear — clear conversation history\n"
        "/help — show this menu\n\n"
        "Or just type anything — I'm here.",
        message.chat.id
    )

@bot.message_handler(commands=['help'])
def cmd_help(message):
    send_message(
        "ARIA — Your Personal AI Assistant\n\n"
        "/news — On-demand news brief\n"
        "/clear — Fresh conversation start\n\n"
        "I handle:\n"
        "• HR & labour compliance (NUCL)\n"
        "• Morning news briefs at 7 AM IST\n"
        "• Financial & retirement planning\n"
        "• Any research or drafting you need",
        message.chat.id
    )

@bot.message_handler(commands=['clear'])
def cmd_clear(message):
    conversation_history[message.chat.id] = []
    send_message("Conversation cleared. Fresh start!", message.chat.id)

@bot.message_handler(commands=['news'])
def cmd_news(message):
    send_message("Fetching your news brief... give me a moment.", message.chat.id)
    threading.Thread(
        target=morning_brief_job,
        kwargs={"triggered": True, "chat_id": message.chat.id}
    ).start()

@bot.message_handler(func=lambda message: True)
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
# NEWS — FETCH
# ─────────────────────────────────────────────

SEARCH_QUERIES = {
    "GLOBAL": [
        "geopolitics war sanctions oil",
        "US economy Federal Reserve inflation",
        "China economy trade policy",
    ],
    "INDIA": [
        "India economy RBI policy rupee",
        "India government regulation policy",
        "India trade export import tariff",
    ],
    "RETAIL_APPAREL": [
        "India fashion retail apparel textile",
        "India GST garments textile policy",
    ],
    "LABOUR_COMPLIANCE": [
        "India labour code EPFO ESIC minimum wage",
        "India employment law gratuity court ruling",
    ],
    "HR_WORKFORCE": [
        "India hiring layoffs workforce trends",
    ],
    "HR_TECH": [
        "HR technology AI payroll HRMS India",
        "Darwinbox greytHR Keka HR tech",
    ],
    "MARKET": [
        "Nifty Sensex India stock market",
        "FII DII India market flows",
    ],
}

SECTION_LABELS = {
    "GLOBAL":            "🌍 GLOBAL",
    "INDIA":             "🇮🇳 INDIA",
    "RETAIL_APPAREL":    "🏭 RETAIL & APPAREL",
    "LABOUR_COMPLIANCE": "⚖️ LABOUR & COMPLIANCE",
    "HR_WORKFORCE":      "👥 HR & WORKFORCE",
    "HR_TECH":           "🤖 HR TECH",
    "MARKET":            "📈 MARKET WATCH",
}

SECTION_CAPS = {
    "GLOBAL": 8, "INDIA": 8,
    "RETAIL_APPAREL": 3, "LABOUR_COMPLIANCE": 3,
    "HR_WORKFORCE": 3, "HR_TECH": 3, "MARKET": 3,
}

def load_json(filepath, default):
    if os.path.exists(filepath):
        with open(filepath) as f:
            return json.load(f)
    return default

def save_json(filepath, data):
    with open(filepath, "w") as f:
        json.dump(data, f)

def fetch_all_news():
    sent_ids = set(load_json(SENT_IDS_FILE, []))
    all_articles = {}

    for section, queries in SEARCH_QUERIES.items():
        articles = []
        for q in queries:
            try:
                resp = requests.get(
                    "https://newsapi.org/v2/everything",
                    params={
                        "q": q,
                        "language": "en",
                        "sortBy": "publishedAt",
                        "pageSize": 10,
                        "apiKey": NEWS_API_KEY,
                        "from": (datetime.now(IST) - timedelta(hours=20)).strftime("%Y-%m-%dT%H:%M:%S"),
                    },
                    timeout=10
                )
                if resp.ok:
                    for a in resp.json().get("articles", []):
                        uid = a.get("url", "")
                        if uid and uid not in sent_ids:
                            articles.append(a)
                            sent_ids.add(uid)
            except Exception as e:
                log.error(f"News fetch error [{q}]: {e}")
            time.sleep(0.3)
        all_articles[section] = articles

    save_json(SENT_IDS_FILE, list(sent_ids)[-500:])
    return all_articles

# ─────────────────────────────────────────────
# NEWS — CLASSIFY VIA CLAUDE
# ─────────────────────────────────────────────

CLASSIFY_PROMPT = """You are a news classifier for Ritika Gawri, Head of HR at an Indian apparel company.

For each article, classify as:
- URGENT: Breaking, market-moving, or requires immediate attention
- IMPORTANT: Relevant and useful, but not time-critical
- DISCARD: Irrelevant, opinion, celebrity, sports, repetitive

For URGENT and IMPORTANT, write a single bullet line — max 15 words, fact-first, no source name.

Output ONLY valid JSON, no markdown:
{"GLOBAL":[{"headline":"...","class":"URGENT|IMPORTANT|DISCARD"}],"INDIA":[...],"RETAIL_APPAREL":[...],"LABOUR_COMPLIANCE":[...],"HR_WORKFORCE":[...],"HR_TECH":[...],"MARKET":[...]}"""

def classify_news(all_articles):
    input_data = {
        section: [
            {"title": a.get("title", ""), "description": a.get("description", "")}
            for a in articles
        ]
        for section, articles in all_articles.items()
    }

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 4000,
                "system": CLASSIFY_PROMPT,
                "messages": [{"role": "user", "content": json.dumps(input_data)}]
            },
            timeout=40
        )
        if resp.ok:
            text = resp.json()["content"][0]["text"].strip()
            text = text.replace("```json", "").replace("```", "").strip()
            return json.loads(text)
    except Exception as e:
        log.error(f"News classification error: {e}")
    return {}

# ─────────────────────────────────────────────
# NEWS — BRIEF BUILDER
# ─────────────────────────────────────────────

def purge_stale(queue):
    cutoff = datetime.now(IST) - timedelta(hours=36)
    return [s for s in queue if datetime.fromisoformat(s["held_at"]) > cutoff]

def morning_brief_job(triggered=False, chat_id=None):
    target = chat_id or TELEGRAM_CHAT_ID
    log.info(f"Running {'on-demand' if triggered else 'scheduled'} news brief...")

    all_articles = fetch_all_news()
    if not any(all_articles.values()):
        send_message("No new articles found at this time.", target)
        return

    classified = classify_news(all_articles)
    if not classified:
        send_message("News fetched but classification failed. Try again shortly.", target)
        return

    hold_queue = purge_stale(load_json(HOLD_QUEUE_FILE, []))
    now_str    = datetime.now(IST).strftime("%d %b %Y, %I:%M %p")
    label      = "On-Demand Brief" if triggered else "Morning Brief"
    lines      = [f"<b>📰 {label} — {now_str} IST</b>\n"]
    new_held   = []

    for section, sec_label in SECTION_LABELS.items():
        items   = classified.get(section, [])
        bullets = [i["headline"] for i in items if i.get("class") in ("URGENT", "IMPORTANT")]
        cap     = SECTION_CAPS.get(section, 3)

        if bullets:
            lines.append(f"\n<b>{sec_label}</b>")
            for b in bullets[:cap]:
                lines.append(f"• {b}")

        for i in items:
            if i.get("class") == "IMPORTANT":
                new_held.append({
                    "headline": i["headline"],
                    "section": section,
                    "held_at": datetime.now(IST).isoformat()
                })

    if hold_queue and not triggered:
        lines.append("\n<b>📌 HELD FROM YESTERDAY</b>")
        for item in hold_queue:
            lines.append(f"• {item['headline']}")

    if len(lines) <= 1:
        lines.append("Nothing significant to report right now.")

    send_message("\n".join(lines), target)
    save_json(HOLD_QUEUE_FILE, purge_stale(new_held))
    log.info("Brief sent.")

# ─────────────────────────────────────────────
# NEWS — URGENT ALERTS
# ─────────────────────────────────────────────

URGENT_PROMPT = """Detect ONLY truly urgent news for an Indian HR and finance professional.
Hard thresholds: Nifty/Sensex >1.5% intraday move, RBI unscheduled action, war escalation, 
Brent crude >3% move, Supreme Court labour ruling, EPFO/ESIC rate change, 
US Fed surprise, major country default, India textile/GST change.

For each qualifying story output:
Category: [one word]
Line1: [what happened, max 15 words]
Line2: [why it matters for India/markets/HR, max 15 words]

If nothing qualifies output exactly: NONE"""

def check_urgent_alerts():
    log.info("Checking urgent alerts...")
    all_articles = fetch_all_news()
    flat = [a for arts in all_articles.values() for a in arts]
    if not flat:
        return

    input_text = "\n".join([
        f"- {a.get('title', '')} | {a.get('description', '')}"
        for a in flat
    ])

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 1000,
                "system": URGENT_PROMPT,
                "messages": [{"role": "user", "content": input_text}]
            },
            timeout=20
        )
        if resp.ok:
            result = resp.json()["content"][0]["text"].strip()
            if result and result != "NONE":
                category = result.split("Category:")[-1].split("Line1:")[0].strip()
                lines = [f"⚡ <b>URGENT — {category}</b>"]
                for line in result.split("\n"):
                    if line.startswith("Line1:") or line.startswith("Line2:"):
                        lines.append(f"• {line.split(':', 1)[-1].strip()}")
                send_broadcast("\n".join(lines))
                log.info("Urgent alert sent.")
            else:
                log.info("No urgent news.")
    except Exception as e:
        log.error(f"Urgent alert error: {e}")

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

if __name__ == "__main__":
    scheduler = BackgroundScheduler(timezone=IST)

    # Morning brief — 7 AM IST daily
    scheduler.add_job(morning_brief_job, "cron", hour=7, minute=0)

    # Urgent alerts — every 90 min during market hours, Mon-Fri
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
