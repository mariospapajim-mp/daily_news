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
import json
import urllib.parse
from datetime import datetime, timedelta
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
SOURCES_CSV_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid=651822256"

# ======================================================================
# WEATHER (each recipient now has their own city - see Location column in
# your Recipients sheet tab, geocoded automatically at runtime)
# ======================================================================

DEFAULT_LOCATION_NAME = "Dietikon"  # used if a recipient's Location cell is blank
LOCAL_TIMEZONE = ZoneInfo("Europe/Zurich")

# ======================================================================
# TRANSLATIONS - add a language by adding a new key here (e.g. "de") and
# filling in every field. Recipients pick a language via the "Language"
# column in the Recipients sheet tab (use the 2-letter code: en, el).
# ======================================================================

TRANSLATIONS = {
    "en": {
        "greeting_morning": "Good morning",
        "greeting_afternoon": "Good afternoon",
        "greeting_evening": "Good evening",
        "weather_label": "Weather",
        "news_label": "News",
        "today": "Today",
        "tomorrow": "Tomorrow",
        "no_categories": "(no categories selected)",
        "weekdays": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
        "months": ["January", "February", "March", "April", "May", "June", "July",
                   "August", "September", "October", "November", "December"],
    },
    "el": {
        "greeting_morning": "Καλημέρα",
        "greeting_afternoon": "Καλό απόγευμα",
        "greeting_evening": "Καλησπέρα",
        "weather_label": "Καιρός",
        "news_label": "Νέα",
        "today": "Σήμερα",
        "tomorrow": "Αύριο",
        "no_categories": "(δεν έχουν επιλεγεί κατηγορίες)",
        "weekdays": ["Δευτέρα", "Τρίτη", "Τετάρτη", "Πέμπτη", "Παρασκευή", "Σάββατο", "Κυριακή"],
        "months": ["Ιανουαρίου", "Φεβρουαρίου", "Μαρτίου", "Απριλίου", "Μαΐου", "Ιουνίου",
                   "Ιουλίου", "Αυγούστου", "Σεπτεμβρίου", "Οκτωβρίου", "Νοεμβρίου", "Δεκεμβρίου"],
    },
}

DEFAULT_LANGUAGE = "en"


def _t(language, key):
    """Translation lookup with a safe fallback to English."""
    return TRANSLATIONS.get(language, TRANSLATIONS[DEFAULT_LANGUAGE]).get(
        key, TRANSLATIONS[DEFAULT_LANGUAGE].get(key, key)
    )


def _format_date(now, language):
    strings = TRANSLATIONS.get(language, TRANSLATIONS[DEFAULT_LANGUAGE])
    weekday = strings["weekdays"][now.weekday()]
    month = strings["months"][now.month - 1]
    return f"{weekday}, {now.day} {month} {now.year}"

# ======================================================================
# NEWS SOURCES - now loaded live from your "Sources" sheet tab (see
# load_sources() below). To add a brand-new news source or category from
# now on, just add a row to that tab: Source | Category | FeedURL |
# Homepage. No code changes needed, ever.
# ======================================================================

# Populated at startup from your "Sources" sheet tab - see load_sources()
# and the bottom of this file where it's assigned.
NEWS_SOURCES = {}

# ======================================================================
# READ CONFIG LIVE FROM GOOGLE SHEETS
# ======================================================================

def _fetch_csv_rows(url, max_attempts=3):
    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            # Google's CSV export doesn't always declare UTF-8 in its headers,
            # which can silently corrupt non-Latin text (Greek, etc.) if we
            # trust requests' auto-detected encoding. Force UTF-8 explicitly.
            resp.encoding = "utf-8"
            reader = csv.reader(io.StringIO(resp.text))
            return [row for row in reader if any(cell.strip() for cell in row)]
        except requests.exceptions.RequestException as e:
            last_error = e
            print(f"  ⚠️  Attempt {attempt}/{max_attempts} fetching sheet failed: {e}")
    raise last_error


def load_recipients():
    """Reads the 'Recipients' tab: Name | ChatID | Time | Language | Location"""
    rows = _fetch_csv_rows(RECIPIENTS_CSV_URL)
    header, data_rows = rows[0], rows[1:]
    recipients = []
    for row in data_rows:
        row = row + [""] * (5 - len(row))
        name, chat_id, send_time, language, location = row[:5]
        name, chat_id, send_time = name.strip(), chat_id.strip(), send_time.strip()
        language = language.strip().lower() or DEFAULT_LANGUAGE
        location = location.strip() or DEFAULT_LOCATION_NAME
        if not name or not chat_id:
            continue
        # Normalize times like "7:00" or "7:5" to "07:00" / "07:05"
        if ":" in send_time:
            h, m = send_time.split(":", 1)
            send_time = f"{int(h):02d}:{int(m):02d}"
        if language not in TRANSLATIONS:
            print(f"  ⚠️  {name}: unknown language {language!r} in sheet, falling back to {DEFAULT_LANGUAGE!r}")
            language = DEFAULT_LANGUAGE
        recipients.append({
            "name": name,
            "chat_id": chat_id,
            "send_time": send_time,
            "language": language,
            "location_name": location,
        })
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


def load_sources():
    """
    Reads the 'Sources' tab: Source | Category | FeedURL | Homepage
    Returns a dict shaped like: {source_name: {"homepage": ..., "categories":
    {category_name: {"feed_url": ...}}}} - same shape the rest of the script
    already expects, just built from the sheet instead of hardcoded.
    """
    rows = _fetch_csv_rows(SOURCES_CSV_URL)
    header, data_rows = rows[0], rows[1:]

    sources = {}
    for row in data_rows:
        row = row + [""] * (len(header) - len(row))
        source_name, category_name, feed_url, homepage = (row + ["", "", "", ""])[:4]
        source_name, category_name = source_name.strip(), category_name.strip()
        feed_url, homepage = feed_url.strip(), homepage.strip()
        if not source_name or not category_name or not feed_url:
            continue

        if source_name not in sources:
            sources[source_name] = {"homepage": homepage, "categories": {}}
        elif homepage and not sources[source_name].get("homepage"):
            sources[source_name]["homepage"] = homepage

        sources[source_name]["categories"][category_name] = {"feed_url": feed_url}

    return sources


# ======================================================================
# WEATHER
# ======================================================================

WEATHER_CODES = {
    "en": {
        0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
        45: "Fog", 48: "Depositing rime fog",
        51: "Light drizzle", 53: "Moderate drizzle", 55: "Dense drizzle",
        61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
        71: "Slight snow", 73: "Moderate snow", 75: "Heavy snow",
        80: "Rain showers", 81: "Moderate rain showers", 82: "Violent rain showers",
        95: "Thunderstorm", 96: "Thunderstorm with hail", 99: "Thunderstorm with heavy hail",
    },
    "el": {
        0: "Αίθριος", 1: "Κυρίως αίθριος", 2: "Μερική νέφωση", 3: "Συννεφιά",
        45: "Ομίχλη", 48: "Ομίχλη με πάχνη",
        51: "Ελαφρύ ψιλόβροχο", 53: "Μέτριο ψιλόβροχο", 55: "Πυκνό ψιλόβροχο",
        61: "Ελαφριά βροχή", 63: "Μέτρια βροχή", 65: "Δυνατή βροχή",
        71: "Ελαφριά χιονόπτωση", 73: "Μέτρια χιονόπτωση", 75: "Έντονη χιονόπτωση",
        80: "Μπόρες βροχής", 81: "Μέτριες μπόρες", 82: "Ραγδαίες μπόρες",
        95: "Καταιγίδα", 96: "Καταιγίδα με χαλάζι", 99: "Καταιγίδα με έντονο χαλάζι",
    },
}


def _weather_description(code, language):
    codes = WEATHER_CODES.get(language, WEATHER_CODES[DEFAULT_LANGUAGE])
    fallback = "Unknown conditions" if language == "en" else "Άγνωστες συνθήκες"
    return codes.get(code, fallback)


_geocode_cache = {}
_weather_data_cache = {}


def geocode_location(location_name, language=DEFAULT_LANGUAGE):
    """
    Turns a plain city name (e.g. "Athens") into (name, lat, lon) using
    Open-Meteo's free geocoding API - so recipients just type a city name
    in the sheet, no coordinates needed. Best results come from typing the
    name in English/Latin script, but this also passes the recipient's
    chosen language as a hint to help match local-script names too.
    """
    cache_key = (location_name, language)
    if cache_key in _geocode_cache:
        return _geocode_cache[cache_key]

    url = (
        f"https://geocoding-api.open-meteo.com/v1/search"
        f"?name={urllib.parse.quote(location_name)}&count=1&language={language}"
    )
    resp = requests.get(url, timeout=20)
    resp.raise_for_status()
    results = resp.json().get("results")
    if not results:
        raise ValueError(f"Could not find location {location_name!r} - check spelling in the sheet")

    result = results[0]
    resolved = (result.get("name", location_name), result["latitude"], result["longitude"])
    _geocode_cache[cache_key] = resolved
    return resolved


def get_weather_section(location_name, language):
    resolved_name, lat, lon = geocode_location(location_name, language)

    if (lat, lon) not in _weather_data_cache:
        url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lon}"
            "&daily=weathercode,temperature_2m_max,temperature_2m_min,"
            "precipitation_probability_max,sunrise,sunset"
            "&timezone=auto"
        )
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        _weather_data_cache[(lat, lon)] = resp.json()["daily"]

    data = _weather_data_cache[(lat, lon)]

    def day_line(label, i):
        code = data["weathercode"][i]
        tmax = data["temperature_2m_max"][i]
        tmin = data["temperature_2m_min"][i]
        rain_chance = data["precipitation_probability_max"][i]
        description = _weather_description(code, language)
        return (
            f"<b>{label}:</b> {description}, {tmin:.0f}°C–{tmax:.0f}°C, "
            f"☔ {rain_chance}%"
        )

    def sun_times_line(i):
        # Open-Meteo returns ISO datetimes like "2026-09-03T06:45" (already
        # in the location's local time since we passed timezone=auto).
        sunrise = data["sunrise"][i].split("T")[1]
        sunset = data["sunset"][i].split("T")[1]
        return f"🌅 {sunrise}  🌇 {sunset}"

    lines = [
        f"📍 {resolved_name}",
        day_line(_t(language, "today"), 0),
        sun_times_line(0),
        day_line(_t(language, "tomorrow"), 1),
    ]
    return "\n".join(lines)


# ======================================================================
# NEWS
# ======================================================================

_feed_cache = {}

# Some sites block requests that don't look like they come from a real
# browser (feedparser's default identifies itself plainly as a bot, which
# some servers reject with an HTML error page instead of the real feed).
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}


def _get_parsed_feed(feed_url):
    if feed_url not in _feed_cache:
        try:
            resp = requests.get(feed_url, headers=_BROWSER_HEADERS, timeout=20)
            resp.raise_for_status()
            _feed_cache[feed_url] = feedparser.parse(resp.content)
        except Exception:
            # Fall back to feedparser's own fetching if the manual request
            # fails for some reason, so we still get *a* result to inspect.
            _feed_cache[feed_url] = feedparser.parse(feed_url)
    return _feed_cache[feed_url]


def _google_news_fallback(source_cfg, category_name, search_scope_suffix, limit):
    """
    Last resort when a category's dedicated feed is empty, missing, or the
    slug guess was wrong: ask Google News for the latest matching story on
    this exact site instead, so the category still shows something.
    """
    homepage = source_cfg.get("homepage", "")
    site_domain = homepage.replace("https://", "").replace("http://", "").rstrip("/")
    search_scope = f"site:{site_domain}{search_scope_suffix}"
    query = urllib.parse.quote(search_scope)
    fallback_url = f"https://news.google.com/rss/search?q={query}&hl=el&gl=GR&ceid=GR:el"
    try:
        fallback_parsed = _get_parsed_feed(fallback_url)
        fallback_entries = fallback_parsed.entries[:limit]
        fallback_titles = [e.get("title", "").strip() for e in fallback_entries if e.get("title")]
        if not fallback_titles:
            print(f"  ⚠️  Google News fallback also found nothing for {category_name!r}")
        return fallback_titles
    except Exception as e:
        print(f"  ⚠️  Google News fallback failed for {category_name!r}: {e}")
        return []


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
        feed_url = category_cfg["feed_url"]
        parsed = _get_parsed_feed(feed_url)
        bozo = getattr(parsed, "bozo", False)
        entries = parsed.entries[:limit] if not bozo else []
        titles = [e.get("title", "").strip() for e in entries if e.get("title")]
        if titles:
            return titles

        # Dedicated feed returned nothing (bad slug guess, or genuinely no
        # stories right now) - try Google News restricted to this site's
        # section path as a fallback.
        reason = f"feed error ({parsed.get('bozo_exception')})" if bozo else "empty feed"
        print(f"  ℹ️  {source_name}/{category_name} dedicated feed issue ({reason}) - trying Google News fallback")
        # Derive the section path from the feed URL itself, e.g.
        # https://x.com/car-and-speed/rss -> /car-and-speed
        section_path = feed_url.replace(source_cfg.get("homepage", ""), "").rsplit("/rss", 1)[0]
        return _google_news_fallback(source_cfg, category_name, section_path, limit)

    return []


def get_news_section_for_recipient(recipient_name, news_plan, language):
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

    return "\n\n".join(sections) if sections else _t(language, "no_categories")


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

# ======================================================================
# SEND-TIME MATCHING
# ======================================================================
# GitHub's free scheduled Actions are "best effort" - a run every 15
# minutes can occasionally be delayed or skipped by GitHub itself during
# busy periods, which could cause an exact-minute match to be missed
# entirely. To make this reliable, each recipient gets a CATCH WINDOW
# instead of one exact minute: if the current run happens anytime within
# WINDOW_MINUTES after their send_time, and they haven't already received
# today's message, they'll get it now. A small state file (sent_log.json)
# tracks who's already been sent today so a wide window can't cause
# duplicate messages.

WINDOW_MINUTES = 30
SENT_LOG_PATH = "sent_log.json"


def _load_sent_log():
    if os.path.exists(SENT_LOG_PATH):
        try:
            with open(SENT_LOG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_sent_log(log):
    with open(SENT_LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(log, f, indent=2, ensure_ascii=False)


def _should_send_now(recipient, now, sent_log, force_all):
    if force_all:
        return True

    today_str = now.strftime("%Y-%m-%d")
    if sent_log.get(recipient["name"]) == today_str:
        return False  # already sent today

    hour, minute = map(int, recipient["send_time"].split(":"))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    window_end = target + timedelta(minutes=WINDOW_MINUTES)

    return target <= now <= window_end


# ======================================================================
# MAIN
# ======================================================================

def _greeting_for_hour(hour, language):
    if hour < 12:
        return _t(language, "greeting_morning")
    elif hour < 18:
        return _t(language, "greeting_afternoon")
    else:
        return _t(language, "greeting_evening")


def build_message_for_recipient(recipient, news_plan):
    now = datetime.now(LOCAL_TIMEZONE)
    language = recipient["language"]
    date_str = _format_date(now, language)
    greeting = _greeting_for_hour(now.hour, language)
    weather_section = get_weather_section(recipient["location_name"], language)
    news_section = get_news_section_for_recipient(recipient["name"], news_plan, language)

    return (
        f"<b>☀️ {greeting}, {recipient['name']}!</b>\n"
        f"<i>{date_str}</i>\n\n"
        f"<b>{_t(language, 'weather_label')}</b>\n{weather_section}\n\n"
        f"<b>{_t(language, 'news_label')}</b>\n{news_section}"
    )


if __name__ == "__main__":
    force_all = os.environ.get("FORCE_SEND_ALL") == "1"

    NEWS_SOURCES = load_sources()
    recipients = load_recipients()
    recipient_names = [r["name"] for r in recipients]
    news_plan = load_news_plan(recipient_names)

    now = datetime.now(LOCAL_TIMEZONE)
    sent_log = _load_sent_log()
    print(f"Loaded {len(NEWS_SOURCES)} sources, {len(recipients)} recipients, {len(news_plan)} news-plan rows.")
    print(f"Current Zurich time: {now.strftime('%Y-%m-%d %H:%M')} (force_all={force_all})")
    print(f"Sent log: {sent_log}")

    # Sanity check: every (Source, Category) row in NewsPlan should have a
    # matching row in Sources. Catch mismatches here and print them clearly,
    # rather than letting them silently show up as "missing" categories in
    # someone's message with no obvious explanation.
    mismatches = []
    for source_name, category_name, counts in news_plan:
        if not any(v > 0 for v in counts.values()):
            continue  # nobody's using this row anyway, skip the check
        source_cfg = NEWS_SOURCES.get(source_name)
        if not source_cfg:
            mismatches.append(f"'{source_name}' (in NewsPlan) has no matching Source in your Sources tab")
        elif category_name not in source_cfg["categories"]:
            mismatches.append(f"'{source_name}' / '{category_name}' (in NewsPlan) has no matching row in your Sources tab")
    if mismatches:
        print(f"  ⚠️  {len(mismatches)} NewsPlan/Sources mismatch(es) found:")
        for m in mismatches:
            print(f"     - {m}")
    else:
        print("  ✅ Every active NewsPlan row matches a Sources row.")

    log_changed = False
    today_str = now.strftime("%Y-%m-%d")

    for recipient in recipients:
        if not _should_send_now(recipient, now, sent_log, force_all):
            already_sent = sent_log.get(recipient["name"]) == today_str
            reason = "already sent today" if already_sent else f"window is {recipient['send_time']}-+{WINDOW_MINUTES}min"
            print(f"Skipping {recipient['name']}: {reason}")
            continue
        try:
            message = build_message_for_recipient(recipient, news_plan)
            print(f"--- Sending to {recipient['name']} ({recipient['chat_id']}) ---")
            send_telegram_message(recipient["chat_id"], message)
            sent_log[recipient["name"]] = today_str
            log_changed = True
        except Exception as e:
            # Never let one recipient's failure (bad feed, network hiccup,
            # etc.) stop the whole run - log it and keep going so everyone
            # else still gets their message.
            print(f"  ❌ Failed to build/send message for {recipient['name']}: {e}")

    if log_changed:
        _save_sent_log(sent_log)
        print("Updated sent_log.json")
