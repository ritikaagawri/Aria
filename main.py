import os
import json
import time
import logging
import requests
import threading
import xml.etree.ElementTree as ET
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

IST           = ZoneInfo("Asia/Kolkata")
SENT_IDS_FILE = "sent_ids.json"

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
   - Real-time news briefs fetched from live RSS feeds
   - Morning brief auto-sent at 7 AM IST daily
   - Use /news for on-demand brief
   - Every brief ends with NUCL IMPACT section
   - Urgent alerts during market hours

3. PERSONAL ASSISTANCE
   - Retirement and financial planning
   - Investment research and portfolio questions
   - Any research, drafting, analysis Ritika needs

YOUR STYLE:
- Direct, practical, no corporate fluff
- Plain language, not formal HR-speak
- Flag compliance risks clearly
- Never guess on legal matters
- No markdown — no **, no ##, no ---
- Use HTML only: <b> for bold"""

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
        .replace("### ", "")
        .replace("## ", "")
        .replace("# ", "")
        .replace("---", "")
        .replace("***", ""))

def send_message(text, chat_id):
    if not text or not text.strip():
        return
    text = clean_text(text)
    url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
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
# RSS FEEDS
# ─────────────────────────────────────────────

RSS_FEEDS = {
    "GLOBAL": [
        "https://feeds.reuters.com/reuters/topNews",
        "https://feeds.reuters.com/reuters/worldNews",
        "https://feeds.bbci.co.uk/news/world/rss.xml",
    ],
    "INDIA": [
        "https://economictimes.indiatimes.com/news/economy/rssfeeds/1415012842.cms",
        "https://www.business-standard.com/rss/economy-policy-10206.rss",
        "https://www.livemint.com/rss/economy",
        "https://feeds.feedburner.com/ndtvprofit-latest",
    ],
    "MARKETS": [
        "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
        "https://www.moneycontrol.com/rss/MCtopnews.xml",
    ],
    "RETAIL_APPAREL": [
        "https://economictimes.indiatimes.com/industry/cons-products/fashion-/-cosmetics-/-jewellery/rssfeeds/13357270.cms",
        "https://www.fibre2fashion.com/rss/news.xml",
    ],
    "LABOUR_COMPLIANCE": [
        "https://economictimes.indiatimes.com/news/economy/policy/rssfeeds/1052732854.cms",
        "https://www.business-standard.com/rss/economy-policy-10206.rss",
    ],
    "HR_WORKFORCE": [
        "https://economictimes.indiatimes.com/jobs/rssfeeds/107115.cms",
    ],
    "HR_TECH": [
        "https://hr.economictimes.indiatimes.com/rss/topstories",
    ],
}

def fetch_rss(url):
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; ARIA-Bot/1.0)"}
        resp = requests.get(url, headers=headers, timeout=12)
        if not resp.ok:
            log.warning(f"RSS failed [{url}]: {resp.status_code}")
            return []

        root     = ET.fromstring(resp.content)
        articles = []

        for item in root.iter("item"):
            title = item.findtext("title", "").strip()
            link  = item.findtext("link", "").strip()
            desc  = item.findtext("description", "").strip()
            pub   = item.findtext("pubDate", "").strip()

            if not title or not link:
                continue

            pub_dt = None
            for fmt in [
                "%a, %d %b %Y %H:%M:%S %z",
                "%a, %d %b %Y %H:%M:%S GMT",
                "%Y-%m-%dT%H:%M:%S%z",
            ]:
                try:
                    pub_dt = datetime.strptime(pub, fmt)
                    break
                except Exception:
                    continue

            articles.append({
                "title":       title,
                "url":         link,
                "description": desc[:150] if desc else "",
                "publishedAt": pub_dt.isoformat() if pub_dt else "",
                "pub_dt":      pub_dt,
            })

        return articles

    except Exception as e:
        log.error(f"RSS parse error [{url}]: {e}")
        return []

def fetch_all_rss():
    sent_ids     = set(load_json(SENT_IDS_FILE, []))
    all_articles = {}
    cutoff       = datetime.now(IST).replace(tzinfo=None) - timedelta(hours=24)

    for section, urls in RSS_FEEDS.items():
        articles    = []
        seen_titles = set()

        for url in urls:
            for a in fetch_rss(url):
                uid   = a["url"]
                title = a["title"]

                if uid in sent_ids or title in seen_titles:
                    continue

                if a["pub_dt"]:
                    pub_naive = a["pub_dt"].replace(tzinfo=None)
                    if pub_naive < cutoff:
                        continue

                articles.append(a)
                sent_ids.add(uid)
                seen_titles.add(title)

            time.sleep(0.3)

        all_articles[section] = articles
        log.info(f"{section}: {len(articles)} fresh articles")

    save_json(SENT_IDS_FILE, list(sent_ids)[-1000:])
    return all_articles

# ─────────────────────────────────────────────
# CLASSIFY VIA CLAUDE
# ─────────────────────────────────────────────

CLASSIFY_PROMPT = """You are a news classifier for Ritika Gawri, Head of HR at Numero Uno Clothing Limited (NUCL), an Indian apparel manufacturer and retailer with factories in Haryana and Uttarakhand and retail stores pan-India.

You will receive real news articles fetched live from RSS feeds right now.

For each article classify as URGENT, IMPORTANT, or DISCARD.
For URGENT and IMPORTANT write one bullet — max 15 words, fact-first, no source name, no asterisks, no markdown.

After all sections add NUCL_IMPACT: pick 3-5 items that directly affect NUCL costs, compliance, workforce, or retail. Format each as:
[what happened] — [specific implication for NUCL in one line]

Output ONLY valid JSON, no markdown, no code blocks:
{
  "GLOBAL": [{"headline": "...", "class": "URGENT|IMPORTANT|DISCARD"}],
  "INDIA": [...],
  "MARKETS": [...],
  "RETAIL_APPAREL": [...],
  "LABOUR_COMPLIANCE": [...],
  "HR_WORKFORCE": [...],
  "HR_TECH": [...],
  "NUCL_IMPACT": [{"headline": "..."}]
}"""

SECTION_LABELS = {
    "GLOBAL":            "🌍 GLOBAL",
    "INDIA":             "🇮🇳 INDIA ECONOMY",
    "MARKETS":           "📈 MARKETS",
    "RETAIL_APPAREL":    "🏭 RETAIL & APPAREL",
    "LABOUR_COMPLIANCE": "⚖️ LABOUR & COMPLIANCE",
    "HR_WORKFORCE":      "👥 HR & WORKFORCE",
    "HR_TECH":           "🤖 HR TECH",
}

SECTION_CAPS = {
    "GLOBAL": 10, "INDIA": 10,
    "MARKETS": 5, "RETAIL_APPAREL": 5,
    "LABOUR_COMPLIANCE": 5, "HR_WORKFORCE": 3, "HR_TECH": 3,
}

def classify_articles(all_articles):
    input_data = {
        section: [
            {
                "title":       a["title"],
                "description": a["description"][:150],
                "publishedAt": a["publishedAt"],
            }
            for a in articles[:15]
        ]
        for section, articles in all_articles.items()
    }

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":         ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json"
            },
            json={
                "model":      "claude-sonnet-4-20250514",
                "max_tokens": 4000,
                "system":     CLASSIFY_PROMPT,
                "messages":   [{"role": "user", "content": json.dumps(input_data)}]
            },
            timeout=240
        )
        if resp.ok:
            text = resp.json()["content"][0]["text"].strip()
            text = text.replace("```json", "").replace("```", "").strip()
            return json.loads(text)
        else:
            log.error(f"Claude classify error: {resp.status_code} {resp.text[:300]}")
    except Exception as e:
        log.error(f"Classify exception: {e}")
    return {}

# ─────────────────────────────────────────────
# BUILD AND SEND BRIEF
# ─────────────────────────────────────────────

def build_and_send_brief(chat_id, triggered=False):
    log.info(f"Running {'on-demand' if triggered else 'scheduled'} news brief...")

    all_articles = fetch_all_rss()
    total        = sum(len(v) for v in all_articles.values())
    log.info(f"Total fresh articles: {total}")

    if total == 0:
        send_message("No fresh news found right now. Try again shortly.", chat_id)
        return

    classified = classify_articles(all_articles)
    if not classified:
        send_message("News fetched but classification failed. Try again shortly.", chat_id)
        return

    now_str     = datetime.now(IST).strftime("%d %b %Y, %I:%M %p")
    label       = "On-Demand Brief" if triggered else "Morning Brief"
    lines       = [f"<b>📰 {label} — {now_str} IST</b>\n"]
    has_content = False

    for section, sec_label in SECTION_LABELS.items():
        items   = classified.get(section, [])
        bullets = [
            i["headline"] for i in items
            if i.get("class") in ("URGENT", "IMPORTANT") and i.get("headline")
        ]
        cap = SECTION_CAPS.get(section, 5)

        if bullets:
            has_content = True
            lines.append(f"\n<b>{sec_label}</b>")
            for b in bullets[:cap]:
                lines.append(f"• {b}")

    nucl_items = classified.get("NUCL_IMPACT", [])
    if nucl_items:
        has_content = True
        lines.append("\n<b>🎯 NUCL IMPACT</b>")
        for item in nucl_items:
            if item.get("headline"):
                lines.append(f"• {item['headline']}")

    if not has_content:
        lines.append("Nothing significant to report right now.")

    send_message("\n".join(lines), chat_id)
    log.info("Brief sent successfully.")

def morning_brief_job():
    build_and_send_brief(chat_id=TELEGRAM_CHAT_ID, triggered=False)

# ─────────────────────────────────────────────
# URGENT ALERTS
# ─────────────────────────────────────────────

URGENT_PROMPT = """You are an urgent news detector for Ritika Gawri at Numero Uno Clothing Limited (NUCL), an Indian apparel company.

Review these real news articles and identify ONLY stories crossing hard thresholds:
- Nifty or Sensex move greater than 1.5% intraday
- RBI unscheduled announcement
- War escalation, major attack, new sanctions
- Brent crude move greater than 3% in a session
- Supreme Court ruling on labour or employment
- EPFO or ESIC rate change or major circular
- US Fed surprise action
- Major country default or financial crisis
- India textile, garment, or GST sudden change

For each qualifying story:
Category: [one word]
Line1: [what happened, max 15 words]
Line2: [why it matters for NUCL or India HR/markets, max 15 words]

If NOTHING qualifies output exactly: NONE"""

def check_urgent_alerts():
    log.info("Checking urgent alerts...")
    all_articles = fetch_all_rss()
    flat         = [a for arts in all_articles.values() for a in arts]
    if not flat:
        return

    input_text = "\n".join([
        f"- {a['title']} | {a['description']} | {a['publishedAt']}"
        for a in flat[:60]
    ])

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":         ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json"
            },
            json={
                "model":      "claude-sonnet-4-20250514",
                "max_tokens": 800,
                "system":     URGENT_PROMPT,
                "messages":   [{"role": "user", "content": input_text}]
            },
            timeout=60
        )
        if resp.ok:
            result = resp.json()["content"][0]["text"].strip()
            if result and result != "NONE":
                category = result.split("Category:")[-1].split("Line1:")[0].strip()
                lines    = [f"⚡ <b>URGENT — {category}</b>"]
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
# SINGLE MESSAGE HANDLER
# ─────────────────────────────────────────────

@bot.message_handler(func=lambda m: m.text is not None)
def handle_all(message):
    text    = message.text.strip()
    chat_id = message.chat.id

    log.info(f"Received from {chat_id}: {text[:60]}")

    if text.startswith("/news"):
        send_message("Fetching live news from RSS feeds... give me a moment.", chat_id)
        threading.Thread(
            target=build_and_send_brief,
            kwargs={"chat_id": chat_id, "triggered": True},
            daemon=True
        ).start()
        return

    if text.startswith("/clear"):
        conversation_history[chat_id] = []
        send_message("Conversation cleared. Fresh start!", chat_id)
        return

    if text.startswith("/start"):
        send_message(
            "Hi Ritika! ARIA is online.\n\n"
            "<b>Commands:</b>\n"
            "/news — fetch live news brief\n"
            "/clear — clear conversation history\n"
            "/help — show this menu\n\n"
            "Or just type anything — I am here.",
            chat_id
        )
        return

    if text.startswith("/help"):
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
            chat_id
        )
        return

    if text.startswith("/"):
        return

    # Regular message — send to Claude
    add_to_history(chat_id, "user", text)

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

    scheduler.add_job(morning_brief_job, "cron", hour=7, minute=0)

    scheduler.add_job(check_urgent_alerts, "cron",
                      hour="9,10,11,12,13,14,15", minute=30,
                      day_of_week="mon-fri")
    scheduler.add_job(check_urgent_alerts, "cron",
                      hour=9, minute=0, day_of_week="mon-fri")
    scheduler.add_job(check_urgent_alerts, "cron",
                      hour=15, minute=55, day_of_week="mon-fri")

    scheduler.start()
    log.info("ARIA is online. Morning brief 7 AM IST. Urgent checks 9 AM-4 PM IST.")
    bot.infinity_polling()
