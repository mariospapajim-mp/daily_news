"""
Daily Digest Bot
-----------------
Sends each recipient a personalized Telegram message: weather (today +
tomorrow) for a shared location, plus news headlines picked per-recipient
by source and category - and delivered at a time you set per recipient.

Setup:
  1. pip install requests feedparser
  2. Set environment variable (or GitHub Actions secret):
       TELEGRAM_BOT_TOKEN   - token from @BotFather
  3. Edit the CONFIG TABLES below - this is the only part you should need
     to touch day-to-day.
  4. The GitHub Actions workflow runs this script every 15 minutes. Each
     run checks the current time in Zurich and only sends messages to
     recipients whose "send_time" matches the current 15-minute slot, so
     everyone gets their message at their own chosen time without you
     needing a separate schedule per person.

HOW THE CONFIG WORKS
---------------------
1. WEATHER_LOCATION - one shared weather location for everyone.
2. NEWS_SOURCES - defines every available (source, category) combination
   and the RSS feed URL / tag that supplies it. Add new sources/categories
   here once; the plan table below then offers them to every recipient.
3. RECIPIENTS - one row per person: their name, Telegram chat ID, and what
   time they should receive their message (24h "HH:MM", Zurich time).
4. NEWS_PLAN - THE MAIN TABLE TO MAINTAIN. One row per (source, category);
   one column per recipient. Just set a number = how many headlines that
   person gets from that row. 0 means they don't get that category.
   Adding a new recipient or category never requires copying lines -
   just add one row (category) or one key in every row's dict (recipient).
"""

import os
from datetime import datetime, date
from zoneinfo import ZoneInfo

import requests
import feedparser

# ======================================================================
# 1. WEATHER LOCATION (shared by everyone)
# ======================================================================

WEATHER_LOCATION = {
    "name": "Dietikon",
    "latitude": 47.4047,
    "longitude": 8.4006,
}

LOCAL_TIMEZONE = ZoneInfo("Europe/Zurich")

# ======================================================================
# 2. NEWS SOURCES - every available (source, category) combination
# ======================================================================
#
# Two kinds of entries:
#   "feed_url"    -> a dedicated RSS feed for that exact category
#                     (used for 20 Minuten, which publishes one feed per
#                     section).
#   "filter_tag"  -> pulls from the source's single main feed, but only
#                     keeps headlines whose RSS <category> tag matches
#                     this value (used for Πρώτο Θέμα, which tags each
#                     headline instead of offering per-section feeds).
#
# To add a new category: add one row here with the right feed_url or
# filter_tag, then add a matching row in NEWS_PLAN below - it will
# automatically be offered to every recipient (starting at 0 headlines
# until you set a number for them).

NEWS_SOURCES = {
    "20 Minuten": {
        "homepage": "https://www.20min.ch",
        "categories": {
            "Top Stories": {"feed_url": "https://partner-feeds.20min.ch/rss/20minuten"},
            "Schweiz":     {"feed_url": "https://partner-feeds.20min.ch/rss/20minuten/schweiz"},
            "Sport":       {"feed_url": "https://partner-feeds.20min.ch/rss/20minuten/sport"},
        },
    },
    "Πρώτο Θέμα": {
        "homepage": "https://www.protothema.gr",
        "main_feed_url": "https://www.protothema.gr/rss",  # shared by all filter_tag categories below
        "categories": {
            "Ελλάδα":    {"filter_tag": "Ελλάδα"},
            "Κόσμος":    {"filter_tag": "Κόσμος"},
            "Πολιτική":  {"filter_tag": "Πολιτική"},
            "Οικονομία": {"filter_tag": "Οικονομία"},
            "Sports":    {"filter_tag": "Sports"},
            "Gala":      {"filter_tag": "Gala"},  # lifestyle / celebrity
        },
    },
}

# ======================================================================
# 3. RECIPIENTS - one row per person
# ======================================================================
# "send_time" is 24h "HH:MM" in Zurich local time. The script runs every
# 15 minutes, so times should ideally land on a quarter-hour (e.g. 07:15,
# 07:30) - if not, it'll send on the next run at or after that time.

RECIPIENTS = [
    {"name": "Marios", "chat_id": "685566804",  "send_time": "07:15"},
    {"name": "Wife",   "chat_id": "8581702180", "send_time": "07:15"},
]

# ======================================================================
# 4. NEWS PLAN - THE MAIN TABLE TO MAINTAIN DAY-TO-DAY
# ======================================================================
# One row per (Source, Category). For each recipient, set a number: how
# many headlines they get from that row. Use 0 to skip it for them.
#
# To add a recipient: add them to RECIPIENTS above, then add their name
# as a new key (with a number) in every row's dict below.
# To add a category: add one row here (and the matching entry in
# NEWS_SOURCES above).

NEWS_PLAN = [
    # Source          Category       {Recipient: headline_count}
    ("20 Minuten",   "Top Stories",  {"Marios": 5, "Wife": 5}),
    ("20 Minuten",   "Schweiz",      {"Marios": 0, "Wife": 0}),
    ("20 Minuten",   "Sport",        {"Marios": 3, "Wife": 0}),
    ("Πρώτο Θέμα",   "Ελλάδα",       {"Marios": 4, "Wife": 4}),
    ("Πρώτο Θέμα",   "Κόσμος",       {"Marios": 0, "Wife": 0}),
    ("Πρώτο Θέμα",   "Πολιτική",     {"Marios": 0, "Wife": 0}),
    ("Πρώτο Θέμα",   "Οικονομία",    {"Marios": 0, "Wife": 0}),
    ("Πρώτο Θέμα",   "Sports",       {"Marios": 3, "Wife": 0}),
    ("Πρώτο Θέμα",   "Gala",         {"Marios": 0, "Wife": 4}),
]

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
    """Returns up to `limit` headline titles for one (source, category) row."""
    if limit <= 0:
        return []

    source_cfg = NEWS_SOURCES[source_name]
    category_cfg = source_cfg["categories"][category_name]

    if "feed_url" in category_cfg:
        parsed = _get_parsed_feed(category_cfg["feed_url"])
        entries = parsed.entries[:limit]
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
        return matches

    return []


def get_news_section_for_recipient(recipient_name):
    """Builds the news section text for one recipient, from NEWS_PLAN."""
    by_source = {}
    for source_name, category_name, counts in NEWS_PLAN:
        limit = counts.get(recipient_name, 0)
        if limit <= 0:
            continue
        by_source.setdefault(source_name, []).append((category_name, limit))

    sections = []
    for source_name, category_picks in by_source.items():
        source_cfg = NEWS_SOURCES[source_name]
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
    """
    Returns the current time in Zurich, rounded DOWN to the nearest
    15-minute mark, as an "HH:MM" string. This is compared against each
    recipient's send_time so a run every 15 minutes catches everyone.
    """
    now = datetime.now(LOCAL_TIMEZONE)
    rounded_minute = (now.minute // 15) * 15
    return now.replace(minute=rounded_minute).strftime("%H:%M")


def _should_send_now(recipient, current_slot, force_all):
    return force_all or recipient["send_time"] == current_slot


# ======================================================================
# MAIN
# ======================================================================

def build_message_for_recipient(recipient):
    today_str = datetime.now(LOCAL_TIMEZONE).strftime("%A, %d %B %Y")
    weather_section = get_weather_section()
    news_section = get_news_section_for_recipient(recipient["name"])

    return (
        f"<b>☀️ Daily Digest — {today_str}</b>\n\n"
        f"<b>Weather</b>\n{weather_section}\n\n"
        f"<b>News</b>\n{news_section}"
    )


if __name__ == "__main__":
    # Set FORCE_SEND_ALL=1 as an env var to bypass the time check and send
    # to everyone immediately - useful for manual testing.
    force_all = os.environ.get("FORCE_SEND_ALL") == "1"
    current_slot = _current_zurich_slot()
    print(f"Current Zurich time slot: {current_slot} (force_all={force_all})")

    for recipient in RECIPIENTS:
        if not _should_send_now(recipient, current_slot, force_all):
            print(f"Skipping {recipient['name']}: scheduled for {recipient['send_time']}, not {current_slot}")
            continue
        message = build_message_for_recipient(recipient)
        print(f"--- Sending to {recipient['name']} ({recipient['chat_id']}) ---")
        send_telegram_message(recipient["chat_id"], message)
