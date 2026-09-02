"""
Daily Digest Bot
-----------------
Sends each recipient a personalized Telegram message: weather (today +
tomorrow) for a shared location, plus news headlines picked per-recipient by
source, category, and how many headlines per category.

Setup:
  1. pip install requests feedparser
  2. Set environment variables (or GitHub Actions secrets):
       TELEGRAM_BOT_TOKEN   - token from @BotFather
  3. Edit the CONFIG TABLES below - this is the only part you should need
     to touch day-to-day.
  4. Run: python daily_digest.py

HOW THE CONFIG WORKS
---------------------
1. WEATHER_LOCATION - one shared weather location for everyone.
2. NEWS_SOURCES - defines every available (source, category) combination
   and the RSS feed URL / tag that supplies it. Add new sources/categories
   here once; recipients then just pick from this list.
3. RECIPIENTS - one row per person. Each person's "feed" is a list of
   (source, category, how many headlines) picks from NEWS_SOURCES.
   This is the table you edit whenever you want to change what someone
   gets, or add/remove a recipient - no other code changes needed.
"""

import os
import requests
import feedparser
from datetime import datetime

# ======================================================================
# 1. WEATHER LOCATION (shared by everyone)
# ======================================================================

WEATHER_LOCATION = {
    "name": "Dietikon",
    "latitude": 47.4047,
    "longitude": 8.4006,
}

# ======================================================================
# 2. NEWS SOURCES - every available (source, category) combination
# ======================================================================
#
# Two kinds of entries:
#   "feed_url"      -> a dedicated RSS feed for that exact category
#                       (used for 20 Minuten, which publishes one feed
#                       per section).
#   "filter_tag"     -> pulls from the source's single main feed, but only
#                       keeps headlines whose RSS <category> tag matches
#                       this value (used for Πρώτο Θέμα, which tags each
#                       headline instead of offering per-section feeds).
#
# To add a new category for a source that uses "feed_url", just add a new
# row with the matching URL from that site's RSS page. To add a new
# category for a source that uses "filter_tag", check what tag values show
# up in that source's feed and add a row with the exact tag text.

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
# 3. RECIPIENTS - edit this table to control who gets what
# ======================================================================
#
# "picks" is a list of (source, category, headline_limit) tuples.
# - source / category must match NEWS_SOURCES exactly (see table above).
# - headline_limit is how many headlines to show for that pick.
#
# Example: give Marios Swiss news (5 headlines) + Sport (3 headlines) from
# 20 Minuten, and Greek politics (4 headlines) from Πρώτο Θέμα.

RECIPIENTS = [
    {
        "name": "Marios",
        "chat_id": "685566804",
        "picks": [
            ("20 Minuten",  "Top Stories", 5),
            ("20 Minuten",  "Sport",       3),
            ("Πρώτο Θέμα",  "Ελλάδα",      4),
            ("Πρώτο Θέμα",  "Sports",      3),
        ],
    },
    {
        "name": "Wife",
        "chat_id": "8581702180",
        "picks": [
            ("20 Minuten",  "Top Stories", 5),
            ("Πρώτο Θέμα",  "Ελλάδα",      4),
            ("Πρώτο Θέμα",  "Gala",        4),
        ],
    },
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


def get_weather_section():
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
    return "\n".join(lines)


# ======================================================================
# NEWS
# ======================================================================

# Cache so each feed is only downloaded once even if multiple recipients
# or multiple categories use it.
_feed_cache = {}


def _get_parsed_feed(feed_url):
    if feed_url not in _feed_cache:
        _feed_cache[feed_url] = feedparser.parse(feed_url)
    return _feed_cache[feed_url]


def _headlines_for_pick(source_name, category_name, limit):
    """Returns a list of headline titles for one (source, category) pick."""
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


def get_news_section_for_recipient(picks):
    """Builds the news section text for one recipient's list of picks."""
    # Group picks by source so each source only prints its homepage link once.
    by_source = {}
    for source_name, category_name, limit in picks:
        by_source.setdefault(source_name, []).append((category_name, limit))

    sections = []
    for source_name, category_picks in by_source.items():
        source_cfg = NEWS_SOURCES[source_name]
        source_block_lines = []
        for category_name, limit in category_picks:
            try:
                titles = _headlines_for_pick(source_name, category_name, limit)
            except Exception as e:
                source_block_lines.append(f"<i>{category_name}</i>: couldn't fetch ({e})")
                continue
            if not titles:
                continue
            bullet_lines = "\n".join(f"  • {t.rstrip('.')}." for t in titles)
            source_block_lines.append(f"<i>{category_name}</i>\n{bullet_lines}")

        homepage = source_cfg.get("homepage", "")
        link_line = f"\n🔗 {homepage}" if homepage else ""
        body = "\n\n".join(source_block_lines)
        sections.append(f"📰 <b>{source_name}</b>\n{body}{link_line}")

    return "\n\n".join(sections)


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
# MAIN
# ======================================================================

def build_message_for_recipient(recipient):
    today = datetime.now().strftime("%A, %d %B %Y")
    weather_section = get_weather_section()
    news_section = get_news_section_for_recipient(recipient["picks"])

    return (
        f"<b>☀️ Daily Digest — {today}</b>\n\n"
        f"<b>Weather</b>\n{weather_section}\n\n"
        f"<b>News</b>\n{news_section}"
    )


if __name__ == "__main__":
    for recipient in RECIPIENTS:
        message = build_message_for_recipient(recipient)
        print(f"--- Message for {recipient['name']} ({recipient['chat_id']}) ---")
        print(message)
        print()
        send_telegram_message(recipient["chat_id"], message)
