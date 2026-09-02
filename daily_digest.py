"""
Daily Digest Bot
-----------------
Fetches the daily weather forecast + a news summary from chosen RSS feeds,
and sends it as a Telegram message to one or more chat IDs.

Setup:
  1. pip install requests feedparser
  2. Set environment variables (or GitHub Actions secrets):
       TELEGRAM_BOT_TOKEN   - token from @BotFather
       TELEGRAM_CHAT_IDS    - comma-separated chat IDs, e.g. "111111,222222"
  3. Edit the CONFIG section below (location + news feeds).
  4. Run: python daily_digest.py
"""

import os
import requests
import feedparser
from datetime import datetime

# ============ CONFIG ============

# Location for weather
LOCATION_NAME = "Dietikon"
LATITUDE = 47.4047
LONGITUDE = 8.4006

# RSS feeds for news
NEWS_FEEDS = {
    "20 Minuten": "https://partner-feeds.20min.ch/rss/20minuten",
    "Πρώτο Θέμα": "https://www.protothema.gr/rss",
    # "iefimerida.gr": skipped - no confirmed RSS feed URL found
}

# Main homepage link shown under each source's headlines
NEWS_SITE_URLS = {
    "20 Minuten": "https://www.20min.ch",
    "Πρώτο Θέμα": "https://www.protothema.gr",
}

ARTICLES_PER_FEED = 8

# ============ WEATHER ============

WEATHER_CODES = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Depositing rime fog",
    51: "Light drizzle", 53: "Moderate drizzle", 55: "Dense drizzle",
    61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
    71: "Slight snow", 73: "Moderate snow", 75: "Heavy snow",
    80: "Rain showers", 81: "Moderate rain showers", 82: "Violent rain showers",
    95: "Thunderstorm", 96: "Thunderstorm with hail", 99: "Thunderstorm with heavy hail",
}


def get_weather():
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={LATITUDE}&longitude={LONGITUDE}"
        "&daily=weathercode,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
        "&timezone=auto"
    )
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    data = resp.json()["daily"]

    code = data["weathercode"][0]
    tmax = data["temperature_2m_max"][0]
    tmin = data["temperature_2m_min"][0]
    rain_chance = data["precipitation_probability_max"][0]
    description = WEATHER_CODES.get(code, "Unknown conditions")

    return (
        f"📍 {LOCATION_NAME}: {description}\n"
        f"🌡️ {tmin:.0f}°C – {tmax:.0f}°C   ☔ {rain_chance}% chance of rain"
    )


# ============ NEWS ============

def get_news():
    """
    Builds one readable paragraph per source by stringing together today's
    headlines. This is NOT true AI summarization - it's a reformatted digest
    of the headlines themselves, joined into flowing sentences.
    """
    sections = []
    for source_name, feed_url in NEWS_FEEDS.items():
        try:
            parsed = feedparser.parse(feed_url)
            entries = parsed.entries[:ARTICLES_PER_FEED]
            if not entries:
                continue
            titles = [entry.get("title", "").strip() for entry in entries]
            titles = [t.rstrip(".") for t in titles if t]
            # One headline per line, as a bullet list, for easier reading
            bullet_lines = "\n".join(f"• {t}." for t in titles)
            site_url = NEWS_SITE_URLS.get(source_name, "")
            link_line = f"\n🔗 {site_url}" if site_url else ""
            sections.append(f"📰 <b>{source_name}</b>\n{bullet_lines}{link_line}")
        except Exception as e:
            sections.append(f"📰 <b>{source_name}</b>\n(couldn't fetch: {e})")
    return "\n\n".join(sections)


# ============ TELEGRAM ============

def send_telegram_message(text):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_ids = os.environ["TELEGRAM_CHAT_IDS"].split(",")

    for chat_id in chat_ids:
        chat_id = chat_id.strip()
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


# ============ MAIN ============

def build_message():
    today = datetime.now().strftime("%A, %d %B %Y")
    weather_section = get_weather()
    news_section = get_news()

    return (
        f"<b>☀️ Daily Digest — {today}</b>\n\n"
        f"<b>Weather</b>\n{weather_section}\n\n"
        f"<b>News</b>\n{news_section}"
    )


if __name__ == "__main__":
    message = build_message()
    print(message)  # useful for local testing
    send_telegram_message(message)
