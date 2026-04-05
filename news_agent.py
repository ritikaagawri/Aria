"""
News Agent for Telegram
=======================
Morning Brief at 7:00 AM IST + Urgent Alerts during market hours
Hold Queue: important but non-urgent news held for next morning
"""

import os
import json
import time
import logging
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from apscheduler.schedulers.blocking import BlockingScheduler

# ─────────────────────────────────────────────
# CONFIG — set these as Railway Variables
# ─────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]
NEWS_API_KEY       = os.environ["NEWS_API_KEY"]
ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]

IST = ZoneInfo("Asia/Kolkata")
HOLD_QUEUE_FILE = "hold_queue.json"
SENT_IDS_FILE   = "sent_ids.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# HOLD QUEUE — persist across runs
# ─────────────────────────────────────────────

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
    """Discard held stories older than 36 hours."""
    cutoff = datetime.now(IST) - timedelta(hours=36)
    fresh = [s for s in queue if datetime.fromisoformat(s["held_at"]) > cutoff]
    return fresh


# ─────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────

def send_telegram(text):
    if not text or not text.strip():
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
    for chunk in chunks:
        resp = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": chunk,
            "parse_mode": "HTML"
        })
        if not resp.ok:
            log.error(f"Telegram error: {resp.text}")
        time.sleep(0.5)


# ─────────────────────────────────────────────
# NEWS FETCH
# ─────────────────────────────────────────────

SEARCH_QUERIES = {
    "GLOBAL": [
        "geopolitics war sanctions",
        "US economy inflation Federal Reserve",
        "oil price crude gold commodities",
        "China economy trade policy",
        "Europe economy politics",
    ],
    "INDIA": [
        "India economy RBI policy rupee",
        "India government policy regulation",
        "India business FDI merger acquisition",
        "India trade export import tariff",
        "India infrastructure investment",
    ],
    "RETAIL_APPAREL": [
        "India fashion retail apparel textile",
        "India GST garments textile policy",
        "India retail consumer ecommerce",
    ],
    "LABOUR_COMPLIANCE": [
        "India labour code EPFO ESIC",
        "India labour welfare fund minimum wage",
        "India employment law court ruling gratuity",
        "India HR compliance shop establishment contract labour",
    ],
    "HR_WORKFORCE": [
        "India hiring layoffs workforce gig economy",
        "India unemployment wage workforce data",
    ],
    "HR_TECH": [
        "HR technology AI hiring payroll HRMS India",
        "Darwinbox greytHR Keka HR tech India funding",
        "Workday SAP HR technology update",
    ],
    "MARKET": [
        "Nifty Sensex stock market India",
        "FII DII India stock market flows",
        "India market global cues",
    ],
}

def fetch_news_for_query(query, page_size=10):
    url = "https://newsapi.org/v2/everything"
    params = {
        "q": query,
        "language": "en",
        "sortBy": "publishedAt",
        "pageSize": page_size,
        "apiKey": NEWS_API_KEY,
        "from": (datetime.now(IST) - timedelta(hours=20)).strftime("%Y-%m-%dT%H:%M:%S"),
    }
    try:
        resp = requests.get(url, params=params, timeout=10)
        if resp.ok:
            return resp.json().get("articles", [])
    except Exception as e:
        log.error(f"News fetch error for '{query}': {e}")
    return []

def fetch_all_news():
    sent_ids = load_sent_ids()
    all_articles = {}
    for section, queries in SEARCH_QUERIES.items():
        articles = []
        for q in queries:
            for a in fetch_news_for_query(q):
                uid = a.get("url", "")
                if uid and uid not in sent_ids:
                    articles.append(a)
                    sent_ids.add(uid)
        all_articles[section] = articles
        time.sleep(0.3)
    save_sent_ids(sent_ids)
    return all_articles


# ─────────────────────────────────────────────
# CLAUDE CLASSIFICATION + SUMMARISATION
# ─────────────────────────────────────────────

CLASSIFY_SYSTEM = """
You are a strict news classifier and summariser for a senior HR professional in India.

You will receive a list of news articles grouped by section.
For EACH article, you must:
1. Classify it as one of: URGENT | IMPORTANT | DISCARD
2. If URGENT or IMPORTANT, write a ONE-LINE bullet (max 15 words). Fact first. No source name. No opinion.

Classification rules:
- URGENT: Market-moving or time-sensitive. Crosses these thresholds:
  * Nifty/Sensex move >1.5% intraday
  * RBI unscheduled announcement
  * War escalation, major attack, sanctions
  * Brent crude >3% move in a session
  * Budget amendment, ordinance, Supreme Court ruling on labour
  * EPFO/ESIC rate change or major circular
  * Labour Welfare Fund new state notification
  * HR tech major outage, data breach, Indian acquisition
  * US Fed surprise move, major country default
- IMPORTANT: Relevant and material but no immediate action needed. Hold for next morning.
- DISCARD: Celebrity, sports, crime, speculation, opinion, repetition, sponsored content.

Output ONLY valid JSON. No markdown. No explanation. Format:
{
  "GLOBAL": [{"headline": "...", "class": "URGENT|IMPORTANT|DISCARD"}],
  "INDIA": [...],
  "RETAIL_APPAREL": [...],
  "LABOUR_COMPLIANCE": [...],
  "HR_WORKFORCE": [...],
  "HR_TECH": [...],
  "MARKET": [...]
}
"""

def classify_and_summarise(all_articles):
    input_data = {}
    for section, articles in all_articles.items():
        input_data[section] = [
            {"title": a.get("title",""), "description": a.get("description","")}
            for a in articles
        ]

    payload = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 4000,
        "system": CLASSIFY_SYSTEM,
        "messages": [{"role": "user", "content": json.dumps(input_data)}]
    }
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json=payload,
            timeout=30
        )
        if resp.ok:
            text = resp.json()["content"][0]["text"]
            text = text.strip().lstrip("```json").rstrip("```").strip()
            return json.loads(text)
    except Exception as e:
        log.error(f"Claude classification error: {e}")
    return {}


# ─────────────────────────────────────────────
# MORNING BRIEF
# ─────────────────────────────────────────────

SECTION_LABELS = {
    "GLOBAL":            "🌍 GLOBAL",
    "INDIA":             "🇮🇳 INDIA",
    "RETAIL_APPAREL":    "🏭 RETAIL & APPAREL (India)",
    "LABOUR_COMPLIANCE": "⚖️ LABOUR LAW & HR COMPLIANCE",
    "HR_WORKFORCE":      "👥 HR & WORKFORCE",
    "HR_TECH":           "🤖 HR TECH",
    "MARKET":            "📈 MARKET WATCH",
}

# Global & India get 8-10 bullets, all other sections get 3
SECTION_CAPS = {
    "GLOBAL":            10,
    "INDIA":             10,
    "RETAIL_APPAREL":    3,
    "LABOUR_COMPLIANCE": 3,
    "HR_WORKFORCE":      3,
    "HR_TECH":           3,
    "MARKET":            3,
}

def build_morning_brief(classified, held_queue):
    now_str = datetime.now(IST).strftime("%d %b %Y")
    lines = [f"<b>📰 Morning Brief — {now_str}</b>\n"]

    for section, label in SECTION_LABELS.items():
        items = classified.get(section, [])
        bullets = [i["headline"] for i in items if i.get("class") in ("URGENT", "IMPORTANT")]
        cap = SECTION_CAPS.get(section, 3)
        if bullets:
            lines.append(f"\n<b>{label}</b>")
            for b in bullets[:cap]:
                lines.append(f"• {b}")

    # Held stories from yesterday
    held_queue = purge_stale_held(held_queue)
    if held_queue:
        lines.append("\n<b>📌 HELD FROM YESTERDAY</b>")
        for item in held_queue:
            lines.append(f"• {item['headline']}")

    return "\n".join(lines)

def morning_brief_job():
    log.info("Running morning brief...")
    all_articles = fetch_all_news()
    classified = classify_and_summarise(all_articles)

    hold_queue = load_hold_queue()

    new_held = []
    for section, items in classified.items():
        for item in items:
            if item.get("class") == "IMPORTANT":
                new_held.append({
                    "headline": item["headline"],
                    "section": section,
                    "held_at": datetime.now(IST).isoformat()
                })

    message = build_morning_brief(classified, hold_queue)
    send_telegram(message)
    log.info("Morning brief sent.")

    hold_queue = purge_stale_held(new_held)
    save_hold_queue(hold_queue)


# ─────────────────────────────────────────────
# URGENT ALERT CHECK
# ─────────────────────────────────────────────

URGENT_SYSTEM = """
You are a strict urgent news detector.
You will receive recent news headlines and descriptions.
Identify ONLY stories that cross these hard thresholds:
- Nifty/Sensex move >1.5% intraday
- RBI unscheduled announcement
- War escalation, major attack, sanctions
- Brent crude >3% single session move
- Budget amendment, ordinance, Supreme Court labour ruling
- EPFO/ESIC rate change or major circular
- Labour Welfare Fund new state notification
- HR tech major outage, data breach, Indian HR tech acquisition
- US Fed surprise, major country default/crisis
- India textile/apparel export duty or GST change

For each qualifying story output:
Category: [one word category]
Line1: [what happened — one line, max 15 words]
Line2: [why it matters to India/markets/HR — one line, max 15 words]

If NOTHING qualifies, output exactly: NONE
No markdown. No preamble.
"""

def check_urgent_alerts():
    all_articles = fetch_all_news()
    flat_articles = []
    for articles in all_articles.values():
        flat_articles.extend(articles)

    if not flat_articles:
        return

    input_text = "\n".join([
        f"- {a.get('title','')} | {a.get('description','')}"
        for a in flat_articles
    ])

    payload = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 2000,
        "system": URGENT_SYSTEM,
        "messages": [{"role": "user", "content": input_text}]
    }
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json=payload,
            timeout=20
        )
        if resp.ok:
            result = resp.json()["content"][0]["text"].strip()
            if result == "NONE" or not result:
                log.info("No urgent news found.")
                return

            message = f"⚡ <b>URGENT — {result.split('Category:')[-1].split('Line1:')[0].strip()}</b>\n"
            for line in result.split("\n"):
                if line.startswith("Line1:") or line.startswith("Line2:"):
                    message += f"• {line.split(':', 1)[-1].strip()}\n"

            send_telegram(message)
            log.info("Urgent alert sent.")

    except Exception as e:
        log.error(f"Urgent check error: {e}")


# ─────────────────────────────────────────────
# SCHEDULER
# ─────────────────────────────────────────────

def main():
    scheduler = BlockingScheduler(timezone=IST)

    # Morning brief at 7:00 AM IST daily
    scheduler.add_job(morning_brief_job, "cron", hour=7, minute=0)

    # Urgent alert check every 90 minutes, market hours only (9 AM – 4 PM IST)
    scheduler.add_job(
        check_urgent_alerts,
        "cron",
        hour="9,10,11,12,13,14,15",
        minute=30,
        day_of_week="mon-fri"
    )
    scheduler.add_job(check_urgent_alerts, "cron", hour=9,  minute=0,  day_of_week="mon-fri")
    scheduler.add_job(check_urgent_alerts, "cron", hour=15, minute=55, day_of_week="mon-fri")

    log.info("Scheduler started. Morning brief at 7 AM IST. Urgent checks 9 AM–4 PM IST.")
    scheduler.start()

if __name__ == "__main__":
    main()
