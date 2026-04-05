import os  # v2
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

TELEGRAM_TOKEN    = os.environ.get("TELEGRAM_BOT_TOKEN")
ANTHROPIC_KEY     = os.environ.get("ANTHROPIC_API_KEY")
NEWS_API_KEY      = os.environ.get("NEWS_API_KEY")
TELEGRAM_CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID")

IST = ZoneInfo("Asia/Kolkata")
HOLD_QUEUE_FILE = "hold_queue.json"
SENT_IDS_FILE   = "sent_ids.json"

bot    = telebot.TeleBot(TELEGRAM_TOKEN)
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

# ── TELEGRAM BOT HANDLERS ──────────────────────────────────────

@bot.message_handler(commands=['start'])
def start(message):
    bot.reply_to(message, "Hi Ritika! ARIA is online. How can I help you today?")

@bot.message_handler(commands=['clear'])
def clear(message):
    conversation_history[message.chat.id] = []
    bot.reply_to(message, "Conversation cleared. Fresh start!")

@bot.message_handler(commands=['news'])
def news_now(message):
    bot.reply_to(message, "Fetching news... give me a moment.")
    threading.Thread(target=lambda: morning_brief_job(triggered=True, chat_id=message.chat.id)).start()

@bot.message_handler(func=lambda message: True)
def handle_message(message):
    chat_id  = message.chat.id
    user_text = message.text

    if chat_id not in conversation_history:
        conversation_history[chat_id] = []

    conversation_history[chat_id].append({"role": "user", "content": user_text})

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
        conversation_history[chat_id].append({"role": "assistant", "content": reply})
        bot.reply_to(message, reply)
    except Exception as e:
        log.error(f"Error: {e}")
        bot.reply_to(message, f"Something went wrong. Error: {str(e)}")

# ── NEWS FUNCTIONS ─────────────────────────────────────────────

def load_hold_queue():
    if os.path.exists(HOLD_QUEUE_FILE):
        with open(HOLD_QUEUE_FILE) as f:
            return json.load(f)
    return []

def save_hold_queue(queue):
    with open(HOLD_QUEUE_FILE, "w") as f:
        json.dump(queue, f)

def load_sent_ids():
    if os.path.exists(SENT_IDS_FILE):
        with open(SENT_IDS_FILE) as f:
            return set(json.load(f))
    return set()

def save_sent_ids(ids):
    with open(SENT_IDS_FILE, "w") as f:
        json.dump(list(ids)[-500:], f)

def purge_stale_held(queue):
    cutoff = datetime.now(IST) - timedelta(hours=36)
    return [s for s in queue if datetime.fromisoformat(s["held_at"]) > cutoff]

def send_telegram(text, chat_id=None):
    if not text or not text.strip():
        return
    target = chat_id or TELEGRAM_CHAT_ID
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for chunk in [text[i:i+4000] for i in range(0, len(text), 4000)]:
        requests.post(url, json={"chat_id": target, "text": chunk, "parse_mode": "HTML"})
        time.sleep(0.5)

SEARCH_QUERIES = {
    "GLOBAL":            ["geopolitics war sanctions", "US economy inflation Federal Reserve", "oil price crude gold commodities"],
    "INDIA":             ["India economy RBI policy rupee", "India government policy regulation", "India trade export import tariff"],
    "RETAIL_APPAREL":    ["India fashion retail apparel textile", "India GST garments textile policy"],
    "LABOUR_COMPLIANCE": ["India labour code EPFO ESIC", "India employment law court ruling gratuity"],
    "HR_WORKFORCE":      ["India hiring layoffs workforce", "India unemployment wage workforce data"],
    "HR_TECH":           ["HR technology AI hiring payroll HRMS India", "Darwinbox greytHR Keka HR tech India"],
    "MARKET":            ["Nifty Sensex stock market India", "FII DII India stock market flows"],
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

def fetch_all_news():
    sent_ids = load_sent_ids()
    all_articles = {}
    for section, queries in SEARCH_QUERIES.items():
        articles = []
        for q in queries:
            try:
                resp = requests.get("https://newsapi.org/v2/everything", params={
                    "q": q, "language": "en", "sortBy": "publishedAt",
                    "pageSize": 10, "apiKey": NEWS_API_KEY,
                    "from": (datetime.now(IST) - timedelta(hours=20)).strftime("%Y-%m-%dT%H:%M:%S"),
                }, timeout=10)
                if resp.ok:
                    for a in resp.json().get("articles", []):
                        uid = a.get("url", "")
                        if uid and uid not in sent_ids:
                            articles.append(a)
                            sent_ids.add(uid)
            except Exception as e:
                log.error(f"News fetch error: {e}")
            time.sleep(0.3)
        all_articles[section] = articles
    save_sent_ids(sent_ids)
    return all_articles

CLASSIFY_SYSTEM = """
You are a strict news classifier for a senior HR professional in India.
For each article classify as URGENT | IMPORTANT | DISCARD and write a ONE-LINE bullet (max 15 words). Fact first. No source name.
Output ONLY valid JSON. No markdown. Format:
{"GLOBAL":[{"headline":"...","class":"URGENT|IMPORTANT|DISCARD"}],"INDIA":[...],"RETAIL_APPAREL":[...],"LABOUR_COMPLIANCE":[...],"HR_WORKFORCE":[...],"HR_TECH":[...],"MARKET":[...]}
"""

def classify_news(all_articles):
    input_data = {s: [{"title": a.get("title",""), "description": a.get("description","")} for a in arts] for s, arts in all_articles.items()}
    try:
        resp = requests.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-sonnet-4-20250514", "max_tokens": 4000, "system": CLASSIFY_SYSTEM,
                  "messages": [{"role": "user", "content": json.dumps(input_data)}]},
            timeout=30)
        if resp.ok:
            text = resp.json()["content"][0]["text"].strip().lstrip("```json").rstrip("```").strip()
            return json.loads(text)
    except Exception as e:
        log.error(f"Claude classify error: {e}")
    return {}

def morning_brief_job(triggered=False, chat_id=None):
    log.info("Running news brief...")
    all_articles = fetch_all_news()
    classified   = classify_news(all_articles)
    hold_queue   = load_hold_queue()

    now_str = datetime.now(IST).strftime("%d %b %Y, %I:%M %p")
    lines = [f"<b>📰 {'On-Demand Brief' if triggered else 'Morning Brief'} — {now_str}</b>\n"]

    new_held = []
    for section, label in SECTION_LABELS.items():
        items   = classified.get(section, [])
        bullets = [i["headline"] for i in items if i.get("class") in ("URGENT", "IMPORTANT")]
        cap     = 10 if section in ("GLOBAL", "INDIA") else 3
        if bullets:
            lines.append(f"\n<b>{label}</b>")
            for b in bullets[:cap]:
                lines.append(f"• {b}")
        for i in items:
            if i.get("class") == "IMPORTANT":
                new_held.append({"headline": i["headline"], "section": section, "held_at": datetime.now(IST).isoformat()})

    hold_queue = purge_stale_held(hold_queue)
    if hold_queue and not triggered:
        lines.append("\n<b>📌 HELD FROM YESTERDAY</b>")
        for item in hold_queue:
            lines.append(f"• {item['headline']}")

    send_telegram("\n".join(lines), chat_id=chat_id)
    save_hold_queue(purge_stale_held(new_held))
    log.info("Brief sent.")

def check_urgent_alerts():
    all_articles = fetch_all_news()
    flat = [a for arts in all_articles.values() for a in arts]
    if not flat:
        return
    input_text = "\n".join([f"- {a.get('title','')} | {a.get('description','')}" for a in flat])
    try:
        resp = requests.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-sonnet-4-20250514", "max_tokens": 2000,
                  "system": "Detect only truly urgent market/HR/labour news crossing hard thresholds. Output Category, Line1, Line2 or exactly NONE.",
                  "messages": [{"role": "user", "content": input_text}]},
            timeout=20)
        if resp.ok:
            result = resp.json()["content"][0]["text"].strip()
            if result and result != "NONE":
                send_telegram(f"⚡ <b>URGENT ALERT</b>\n{result}")
    except Exception as e:
        log.error(f"Urgent check error: {e}")

# ── MAIN ───────────────────────────────────────────────────────

if __name__ == "__main__":
    scheduler = BackgroundScheduler(timezone=IST)
    scheduler.add_job(morning_brief_job, "cron", hour=7, minute=0)
    scheduler.add_job(check_urgent_alerts, "cron", hour="9,10,11,12,13,14,15", minute=30, day_of_week="mon-fri")
    scheduler.start()
    log.info("ARIA + News Agent starting...")
    bot.infinity_polling()
