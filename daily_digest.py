"""
Daily Digest Bot
-----------------
Sends each recipient a personalized Telegram message: weather (today +
tomorrow) for a shared location, plus news headlines picked per-recipient
by source and category - delivered at a time set per recipient.

EVERYTHING YOU MAINTAIN DAY-TO-DAY LIVES IN YOUR GOOGLE SHEET, NOT HERE.
  - "Recipients" tab: Name | ChatID | Time  -> who gets a message, and when.
  - "NewsPlan" tab: Source | category | <one column per recipient name>
    -> how many headlines each person gets per category. 0 = skip it.
Add a recipient: add a row in "Recipients", then a matching column in
"NewsPlan". Add/remove a category: add/remove a row in "NewsPlan" (only
using categories already defined in NEWS_SOURCES below). Change a time or
a headline count: just edit that cell in Sheets. No code changes, ever,
for any of that.

Setup (one-time, technical):
  1. pip install requests feedparser
  2. Set environment variable (or GitHub Actions secret):
       TELEGRAM_BOT_TOKEN   - token from @BotFather
  3. Make sure the two SHEET_CSV_URLS below point at your sheet's tabs
     (already done for this setup).
  4. The GitHub Actions workflow runs this script every 15 minutes. Each
     run re-reads the sheet, checks the current time in Zurich, and only
     sends a message to recipients whose "Time" matches the current
     15-minute slot - so everyone gets their message at their own chosen
     time without a separate schedule per person.
"""

import os
import csv
import io
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import feedparser

# ======================================================================
# GOOGLE SHEET SOURCE (the only two links that ever need to change)
# ======================================================================
# These point at your sheet's two tabs, exported as plain CSV. If you ever
# recreate the sheet, replace these two URLs with the new tab links,
# changing "/edit?gid=..." to "/export?format=csv&gid=...".

SHEET_ID = "1nqe0sPAcu3SPPa9C07ArXqWbYCx1NhPKwfBFQ0y0PuM"
RECIPIENTS_CSV_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid=2057126119"
NEWS_PLAN_CSV_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid=0"

# ======================================================================
# WEATHER LOCATION (shared by everyone - edit here if you ever move)
# ======================================================================

WEATHER_LOCATION = {
    "name": "Dietikon",
    "latitude": 47.4047,
    "longitude": 8.4006,
}

LOCAL_TIMEZONE = ZoneInfo("Europe/Zurich")

# ======================================================================
# NEWS SOURCES - technical definition of every available category.
# This is the one part of the whole system that still lives in code,
# because it involves real RSS feed URLs. You won't need to touch this
# unless you want to add a brand-new news source/category beyond what's
# already offered in your NewsPlan sheet.
# ======================================================================

NEWS_SOURCES = {
    "20 Minuten": {
        "homepage": "https://www.20min.ch",
        "categories": {
            "Top Stories": {"feed_url": "https://partner-feeds.20min.ch/rss/20minuten"},
            "Schweiz":     {"feed_url": "https://partner-feeds.20min.ch/rss/20minuten/schweiz"},
            "Sport":       {"feed_url": "https://partner-feeds.20min.ch/rss/20minuten/sport"},
            "Wirtschaft":  {"feed_url": "https://partner-feeds.20min.ch/rss/20minuten/wirtschaft"},
            "Regionen":    {"feed_url": "https://partner-feeds.20min.ch/rss/20minuten/regionen"},
            "Lifestyle":   {"feed_url": "https://partner-feeds.20min.ch/rss/20minuten/lifestyle"},
            "Ausland":     {"feed_url": "https://partner-feeds.20min.ch/rss/20minuten/ausland"},
            "People":      {"feed_url": "https://partner-feeds.20min.ch/rss/20minuten/people"},
            "Good Vibes":  {"feed_url": "https://partner-feeds.20min.ch/rss/20minuten/good-vibes"},
        },
    },
    "Πρώτο Θέμα": {
        "homepage": "https://www.protothema.gr",
        "main_feed_url": "https://www.protothema.gr/rss",
        "categories": {
            "Ελλάδα":      {"filter_tag": "Ελλάδα"},
            "Κόσμος":      {"filter_tag": "Κόσμος"},
            "Πολιτική":    {"filter_tag": "Πολιτική"},
            "Οικονομία":   {"filter_tag": "Οικονομία"},
            "Sports":      {"filter_tag": "Sports"},
            "Gala":        {"filter_tag": "Gala"},
            "Αυτοκίνητο":  {"filter_tag": "Αυτοκίνητο"},
            "People":      {"filter_tag": "People"},
            "Πολιτισμός":  {"filter_tag": "Πολιτισμός"},
            "Τεχνολογία":  {"filter_tag": "Τεχνολογία"},
            "Περιβάλλον":  {"filter_tag": "Περιβάλλον"},
            "Υγεία + Ζωή": {"filter_tag": "Υγεία + Ζωή"},
            "Life Style":  {"filter_tag": "Life Style"},
        },
    },
}

# ======================================================================
# READ CONFIG LIVE FROM GOOGLE SHEETS
# ======================================================================

def _fetch_csv_rows(url):
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    # Google's CSV export doesn't always declare UTF-8 in its headers, which
    # can silently corrupt non-Latin text (Greek, etc.) if we trust
    # requests' auto-detected encoding. Force UTF-8 explicitly.
    resp.encoding = "utf-8"
    reader = csv.reader(io.StringIO(resp.text))
    return [row for row in reader if any(cell.strip() for cell in row)]


def load_recipients():
    """Reads the 'Recipients' tab: Name | ChatID | Time"""
    rows = _fetch_csv_rows(RECIPIENTS_CSV_URL)
    header, data_rows = rows[0], rows[1:]
    recipients = []
    for row in data_rows:
        name, chat_id, send_time = (row + ["", "", ""])[:3]
        name, chat_id, send_time = name.strip(), chat_id.strip(), send_time.strip()
        if not name or not chat_id:
            continue
        # Normalize times like "7:00" or "7:5" to "07:00" / "07:05"
        if ":" in send_time:
            h, m = send_time.split(":", 1)
            send_time = f"{int(h):02d}:{int(m):02d}"
        recipients.append({"name": name, "chat_id": chat_id, "send_time": send_time})
    return recipients


def load_news_plan(recipient_names):
    """Reads the 'NewsPlan' tab: Source | category | <one column per recipient>"""
    rows = _fetch_csv_rows(NEWS_PLAN_CSV_URL)
    header, data_rows = rows[0], rows[1:]
    # header looks like: ["Source", "category", "marios", "wife", ...]
    recipient_columns = header[2:]

    plan = []
    for row in data_rows:
        row = row + [""] * (len(header) - len(row))
        source_name, category_name = row[0].strip(), row[1].strip()
        if not source_name or not category_name:
            continue
        counts = {}
        for col_name, raw_value in zip(recipient_columns, row[2:]):
            raw_value = raw_value.strip()
            try:
                count = int(raw_value) if raw_value else 0
            except ValueError:
                count = 0
            # Match the sheet's recipient column name to the actual
            # recipient name case-insensitively, so "marios" in the sheet
            # matches "Marios" in the Recipients tab.
            for name in recipient_names:
                if name.lower() == col_name.strip().lower():
                    counts[name] = count
                    break
        plan.append((source_name, category_name, counts))
    return plan


# ======================================================================
# WEATHER
# ======================================================================

WEATHER_CODES = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Depositing rime fog",
    51: "Light drizzle", 53: "Moderate drizzle", 55: "Dense drizzle",
    61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
    71: "Slight snow", 73: "Moderate snow", 75: "Heavy snow",
    80: "Rain showers", 81: "Moderate rain showers", 82: "Violent rain showers",
    95: "Thunderstorm", 96: "Thunderstorm with hail", 99: "Thunderstorm with heavy hail",
}

_weather_cache = None


def get_weather_section():
    global _weather_cache
    if _weather_cache is not None:
        return _weather_cache

    loc = WEATHER_LOCATION
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={loc['latitude']}&longitude={loc['longitude']}"
        "&daily=weathercode,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
        "&timezone=auto"
    )
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    data = resp.json()["daily"]

    def day_line(label, i):
        code = data["weathercode"][i]
        tmax = data["temperature_2m_max"][i]
        tmin = data["temperature_2m_min"][i]
        rain_chance = data["precipitation_probability_max"][i]
        description = WEATHER_CODES.get(code, "Unknown conditions")
        return (
            f"<b>{label}:</b> {description}, {tmin:.0f}°C–{tmax:.0f}°C, "
            f"☔ {rain_chance}%"
        )

    lines = [f"📍 {loc['name']}", day_line("Today", 0), day_line("Tomorrow", 1)]
    _weather_cache = "\n".join(lines)
    return _weather_cache


# ======================================================================
# NEWS
# ======================================================================

_feed_cache = {}


def _get_parsed_feed(feed_url):
    if feed_url not in _feed_cache:
        _feed_cache[feed_url] = feedparser.parse(feed_url)
    return _feed_cache[feed_url]


def _headlines_for_row(source_name, category_name, limit):
    if limit <= 0:
        return []

    source_cfg = NEWS_SOURCES.get(source_name)
    if not source_cfg:
        print(f"  ⚠️  Unknown source in sheet: {source_name!r} (check spelling matches NEWS_SOURCES)")
        return []
    category_cfg = source_cfg["categories"].get(category_name)
    if not category_cfg:
        print(f"  ⚠️  Unknown category in sheet: {source_name!r} / {category_name!r}")
        return []

    if "feed_url" in category_cfg:
        parsed = _get_parsed_feed(category_cfg["feed_url"])
        if getattr(parsed, "bozo", False):
            print(f"  ⚠️  Feed error for {source_name}/{category_name}: {parsed.get('bozo_exception')}")
        entries = parsed.entries[:limit]
        if not entries:
            print(f"  ⚠️  No entries returned for {source_name}/{category_name} ({category_cfg['feed_url']})")
        return [e.get("title", "").strip() for e in entries if e.get("title")]

    elif "filter_tag" in category_cfg:
        parsed = _get_parsed_feed(source_cfg["main_feed_url"])
        wanted_tag = category_cfg["filter_tag"]
        matches = []
        for entry in parsed.entries:
            tags = [t.get("term", "") for t in entry.get("tags", [])]
            if wanted_tag in tags:
                title = entry.get("title", "").strip()
                if title:
                    matches.append(title)
            if len(matches) >= limit:
                break
        if not matches:
            print(f"  ⚠️  No entries tagged {wanted_tag!r} found in {source_name}'s main feed")
        return matches

    return []


def get_news_section_for_recipient(recipient_name, news_plan):
    by_source = {}
    for source_name, category_name, counts in news_plan:
        limit = counts.get(recipient_name, 0)
        if limit <= 0:
            continue
        by_source.setdefault(source_name, []).append((category_name, limit))

    sections = []
    for source_name, category_picks in by_source.items():
        source_cfg = NEWS_SOURCES.get(source_name, {})
        source_block_lines = []
        for category_name, limit in category_picks:
            try:
                titles = _headlines_for_row(source_name, category_name, limit)
            except Exception as e:
                source_block_lines.append(f"<i>{category_name}</i>: couldn't fetch ({e})")
                continue
            if not titles:
                continue
            bullet_lines = "\n".join(f"  • {t.rstrip('.')}." for t in titles)
            source_block_lines.append(f"<i>{category_name}</i>\n{bullet_lines}")

        if not source_block_lines:
            continue

        homepage = source_cfg.get("homepage", "")
        link_line = f"\n🔗 {homepage}" if homepage else ""
        body = "\n\n".join(source_block_lines)
        sections.append(f"📰 <b>{source_name}</b>\n{body}{link_line}")

    return "\n\n".join(sections) if sections else "(no categories selected)"


# ======================================================================
# TELEGRAM
# ======================================================================

def send_telegram_message(chat_id, text):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    resp = requests.post(url, data=payload, timeout=15)
    if not resp.ok:
        print(f"Failed to send to {chat_id}: {resp.text}")
    else:
        print(f"Sent to {chat_id}")


# ======================================================================
# SEND-TIME MATCHING
# ======================================================================

def _current_zurich_slot():
    now = datetime.now(LOCAL_TIMEZONE)
    rounded_minute = (now.minute // 15) * 15
    return now.replace(minute=rounded_minute).strftime("%H:%M")


def _should_send_now(recipient, current_slot, force_all):
    return force_all or recipient["send_time"] == current_slot


# ======================================================================
# MAIN
# ======================================================================

def build_message_for_recipient(recipient, news_plan):
    today_str = datetime.now(LOCAL_TIMEZONE).strftime("%A, %d %B %Y")
    weather_section = get_weather_section()
    news_section = get_news_section_for_recipient(recipient["name"], news_plan)

    return (
        f"<b>☀️ Daily Digest — {today_str}</b>\n\n"
        f"<b>Weather</b>\n{weather_section}\n\n"
        f"<b>News</b>\n{news_section}"
    )


if __name__ == "__main__":
    force_all = os.environ.get("FORCE_SEND_ALL") == "1"

    recipients = load_recipients()
    recipient_names = [r["name"] for r in recipients]
    news_plan = load_news_plan(recipient_names)

    current_slot = _current_zurich_slot()
    print(f"Loaded {len(recipients)} recipients, {len(news_plan)} news-plan rows.")
    print(f"Current Zurich time slot: {current_slot} (force_all={force_all})")

    for recipient in recipients:
        if not _should_send_now(recipient, current_slot, force_all):
            print(f"Skipping {recipient['name']}: scheduled for {recipient['send_time']}, not {current_slot}")
            continue
        message = build_message_for_recipient(recipient, news_plan)
        print(f"--- Sending to {recipient['name']} ({recipient['chat_id']}) ---")
        send_telegram_message(recipient["chat_id"], message)
