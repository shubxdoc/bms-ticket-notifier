"""
BMS Ticket Checker — CI/Headless mode for GitHub Actions.
Runs once, checks all configured watches, notifies on changes.
State is persisted via a JSON file committed back to the repo.

Features:
  * Multi-movie support via comma-separated URLs in BMS_URL.
  * Auto-format discovery per movie with URL #fragment filtering
    (e.g. ...ET00493076#3d+2d or ...ET00493076#hindi 3d+hindi 2d).
  * Theatre + Screen filtering in BMS_THEATRE (e.g. "ALLU,Prasads,AAA:LED").
  * Notifications fire ONLY when discovered formats have active showtimes at
    your filtered BMS_THEATRE venues.
  * curl_cffi browser TLS impersonation (Safari 17 / Chrome) + session reuse.
  * merge_state: failed fetches keep their previous state (no false NEW alerts).
"""

import os
import re
import sys
import json
import time
from html import escape
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from urllib.parse import urlparse, unquote

try:
    from curl_cffi import requests as cffi_requests
    HAS_CFFI = True
except ImportError:
    cffi_requests = None
    HAS_CFFI = False

import requests

# ──────────────────────────────────────────────────────────────────────
# CONFIGURATION — edit these or set via env vars
# ──────────────────────────────────────────────────────────────────────
CONFIG = {
    "url": os.getenv(
        "BMS_URL",
        "https://in.bookmyshow.com/movies/hyderabad/ramayana/buytickets/ET00493076#3d+2d"
    ),
    "dates": os.getenv("BMS_DATES", ""),          # comma-separated YYYYMMDD, empty = from URL
    "theatre": os.getenv("BMS_THEATRE", ""),       # e.g. "ALLU,Prasads,AAA:LED"
    "time_period": os.getenv("BMS_TIME", ""),      # e.g. "evening,night", empty = all
}

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
RESEND_TO_EMAIL = os.getenv("RESEND_TO_EMAIL", "")
RESEND_FROM_EMAIL = os.getenv("RESEND_FROM_EMAIL", "aviiciii@resend.dev")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
PROXY_URL = os.getenv("PROXY_URL", "").strip()

STATE_FILE = "bms_state.json"

IST = timezone(timedelta(hours=5, minutes=30))

# ──────────────────────────────────────────────────────────────────────
# CONSTANTS
# ──────────────────────────────────────────────────────────────────────
AVAIL_STATUS_MAP = {
    "0": ("SOLD OUT",    "🔴"),
    "1": ("ALMOST FULL", "🟡"),
    "2": ("FILLING FAST","🟠"),
    "3": ("AVAILABLE",   "🟢"),
}

DATE_STYLE_MAP = {
    "date-selected": "BOOKABLE",
    "date-disabled": "NOT_OPEN",
    "date-default":  "AVAILABLE",
}

TIME_PERIODS = {
    "morning":   (600, 1200),
    "afternoon": (1200, 1600),
    "evening":   (1600, 1900),
    "night":     (1900, 2400),
}

REGION_MAP = {
    "chennai":    ("CHEN",   "chennai",    "13.056", "80.206", "tf3"),
    "mumbai":     ("MUMBAI", "mumbai",     "19.076", "72.878", "te7"),
    "delhi-ncr":  ("NCR",    "delhi-ncr",  "28.613", "77.209", "ttn"),
    "delhi":      ("NCR",    "delhi-ncr",  "28.613", "77.209", "ttn"),
    "national-capital-region-ncr": ("NCR", "national-capital-region-ncr", "28.613", "77.209", "ttn"),
    "bengaluru":  ("BANG",   "bengaluru",  "12.972", "77.594", "tdr"),
    "bangalore":  ("BANG",   "bengaluru",  "12.972", "77.594", "tdr"),
    "hyderabad":  ("HYD",    "hyderabad",  "17.385", "78.487", "tep"),
    "kolkata":    ("KOLK",   "kolkata",    "22.573", "88.364", "tun"),
    "pune":       ("PUNE",   "pune",       "18.520", "73.856", "te2"),
    "kochi":      ("KOCH",   "kochi",      "9.932",  "76.267", "t9z"),
}


# ──────────────────────────────────────────────────────────────────────
# DATA STRUCTURES
# ──────────────────────────────────────────────────────────────────────
@dataclass
class CatInfo:
    name: str
    price: str
    status: str

@dataclass
class ShowInfo:
    venue_code: str
    venue_name: str
    session_id: str
    date_code: str
    time: str
    time_code: str
    screen_attr: str
    movie_name: str = ""
    lang_format: str = ""
    categories: list[CatInfo] = field(default_factory=list)

@dataclass
class DateInfo:
    date_code: str
    status: str
    movie_key: str = ""
    movie_name: str = ""


# ──────────────────────────────────────────────────────────────────────
# URL PARSER (WITH #FORMAT FRAGMENT) + REGION RESOLVER
# ──────────────────────────────────────────────────────────────────────
def parse_bms_url(url):
    """
    Parse BMS URL and optional #format fragment.
    Example:
      https://in.bookmyshow.com/movies/hyderabad/ramayana/buytickets/ET00493076#hindi 3d+2d
      -> event_code='ET00493076', region_slug='hyderabad',
         format_specs=['hindi 3d', '2d'], clean_url='https://...'
    """
    parsed = urlparse(url.strip())
    path = parsed.path.strip("/")
    parts = path.split("/")
    fragment = unquote(parsed.fragment or "").strip()

    format_specs = [
        s.strip().lower()
        for s in re.split(r"[+|/;]+", fragment)
        if s.strip()
    ]

    clean_url = parsed._replace(fragment="").geturl()
    result = {
        "event_code": None,
        "date_code": None,
        "region_slug": None,
        "format_specs": format_specs,
        "clean_url": clean_url,
    }
    for p in parts:
        if re.match(r"^ET\d{8,}$", p, re.I):
            result["event_code"] = p.upper()
        elif re.match(r"^\d{8}$", p):
            result["date_code"] = p
    if "movies" in parts:
        idx = parts.index("movies")
        if idx + 1 < len(parts):
            result["region_slug"] = parts[idx + 1]
    return result


def resolve_region(slug):
    key = (slug or "").lower().strip()
    if key in REGION_MAP:
        return REGION_MAP[key]
    return (key.upper()[:6], key, "17.385", "78.487", "tep")


# ──────────────────────────────────────────────────────────────────────
# BMS API & SESSION MANAGEMENT
# ──────────────────────────────────────────────────────────────────────
API_URL = (
    "https://in.bookmyshow.com/api/movies-data/v4/"
    "showtimes-by-event/primary-dynamic"
)

BROWSER_PROFILES = [
    {
        "impersonate": "safari17_0",
        "headers": {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                "Version/17.0 Safari/605.1.15"
            ),
        },
    },
    {
        "impersonate": "chrome120",
        "headers": {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "sec-ch-ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
        },
    },
    {
        "impersonate": "chrome124",
        "headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        },
    },
]

MAX_ATTEMPTS = 3
RETRY_DELAYS = (2.5, 4.5)
INTER_DATE_DELAY = 2.0
RETRY_BUDGET_SECONDS = 120
RETRYABLE_STATUS = {403, 429, 500, 502, 503, 504}
BLOCK_ALERT_AFTER = 3

RUN_STARTED = time.monotonic()


def create_session(profile_idx=0):
    profile = BROWSER_PROFILES[profile_idx % len(BROWSER_PROFILES)]
    if HAS_CFFI:
        s = cffi_requests.Session(impersonate=profile["impersonate"])
    else:
        s = requests.Session()

    if PROXY_URL:
        s.proxies = {"http": PROXY_URL, "https": PROXY_URL}

    s.headers.update({
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "x-app-code": "WEB",
        "x-location-selection": "manual",
        "x-lsid": "",
        **profile["headers"],
    })
    return s


def fetch_bms(event_code, date_code, region_code, region_slug,
              lat, lon, geohash, session=None, start_profile=0):
    active_session = session or create_session(start_profile)
    active_profile = start_profile

    params = {
        "eventCode": event_code,
        "dateCode": date_code or "",
        "isDesktop": "true",
        "regionCode": region_code,
        "xLocationShared": "false",
        "memberId": "", "lsId": "", "subCode": "",
        "lat": lat, "lon": lon,
    }

    for attempt in range(MAX_ATTEMPTS):
        if attempt > 0:
            active_profile = (start_profile + attempt) % len(BROWSER_PROFILES)
            active_session = create_session(active_profile)

        headers = {
            "Referer": (
                f"https://in.bookmyshow.com/movies/"
                f"{region_slug}/buytickets/{event_code}/"
            ),
            "x-region-code": region_code,
            "x-region-slug": region_slug,
            "x-geohash": geohash,
            "x-latitude": lat,
            "x-longitude": lon,
        }
        reason = None
        try:
            resp = active_session.get(
                API_URL, headers=headers, params=params, timeout=15
            )
            if resp.status_code == 200:
                try:
                    return resp.json(), active_session, active_profile
                except ValueError:
                    reason = "response was not JSON"
            else:
                reason = f"HTTP {resp.status_code}"
                if resp.status_code not in RETRYABLE_STATUS:
                    print(f"  {reason}")
                    return None, active_session, active_profile
        except Exception as e:
            reason = f"Request failed: {e}"

        last_attempt = attempt == MAX_ATTEMPTS - 1
        out_of_time = time.monotonic() - RUN_STARTED > RETRY_BUDGET_SECONDS
        if last_attempt or out_of_time:
            print(f"  {reason}")
            return None, active_session, active_profile
        print(f"  {reason} — retrying ({attempt + 2}/{MAX_ATTEMPTS})")
        time.sleep(RETRY_DELAYS[attempt])
    return None, active_session, active_profile


# ──────────────────────────────────────────────────────────────────────
# PARSERS & AUTO-FORMAT DISCOVERY
# ──────────────────────────────────────────────────────────────────────
def parse_movie_info(data):
    info = {"name": "Unknown Movie", "language": ""}
    header_title = data.get("data", {}).get("header", {}).get("title", {})
    if header_title.get("text"):
        info["name"] = header_title["text"]

    for w in data.get("data", {}).get("topStickyWidgets", []):
        if w.get("type") == "horizontal-text-list":
            for item in w.get("data", []):
                for row in item.get("leftText", {}).get("data", []):
                    for c in row.get("components", []):
                        if "•" in c.get("text", ""):
                            info["language"] = c["text"].strip()
    bs = data.get("data", {}).get("bottomSheetData", {})
    for w in bs.get("format-selector", {}).get("widgets", []):
        if w.get("type") == "vertical-text-list":
            for d in w.get("data", []):
                if d.get("styleId") == "bottomsheet-subtitle":
                    info["name"] = d.get("text", info["name"])
    return info


def extract_available_formats(data):
    """
    Extract all formats and languages linked to this movie from BMS's format-selector.
    Returns list of dicts: {'language', 'format', 'event_code', 'display'}.
    """
    formats = []
    seen = set()
    fs = data.get("data", {}).get("bottomSheetData", {}).get("format-selector", {})
    for w in fs.get("widgets", []):
        if w.get("type") == "chip-list":
            lang = w.get("text", "").strip()
            for chip in w.get("data", []):
                fmt_title = chip.get("title", "").strip()
                addl = chip.get("cta", {}).get("additionalData", {})
                ecode = (addl.get("eventCode") or addl.get("refEventCode") or "").upper()
                if ecode and ecode not in seen:
                    seen.add(ecode)
                    display = f"{lang} • {fmt_title}".strip(" •")
                    formats.append({
                        "language": lang,
                        "format": fmt_title,
                        "event_code": ecode,
                        "display": display,
                    })
    return formats


def match_target_formats(available_formats, base_event_code, base_lang_fmt, format_specs):
    """
    Filter discovered formats using the URL's #fragment specs.
    - Each spec in format_specs (e.g. 'hindi 3d' or '2d') is split into word tokens.
    - A format matches a spec if every token in that spec appears as a word in
      '<language> <format>' (so '3d' matches 'Hindi • 3D' and 'IMAX 3D', while
      '2d' matches 'Hindi • 2D').
    - If format_specs is empty (no #fragment in URL) or 'all', all discovered
      formats for the movie are checked.
    """
    fallback = [{
        "event_code": base_event_code,
        "language": "",
        "format": "",
        "display": base_lang_fmt or "Default",
    }]
    if not available_formats:
        return fallback

    if not format_specs or "all" in format_specs:
        return available_formats

    matched = []
    seen = set()
    for spec in format_specs:
        tokens = [t for t in re.split(r"[\s•,-]+", spec.lower()) if t]
        if not tokens:
            continue
        for f in available_formats:
            haystack_words = set(
                re.split(r"[\s•,-]+", f"{f['language']} {f['format']}".lower())
            )
            haystack_str = f"{f['language']} {f['format']}".lower()
            if all(tok in haystack_words or tok in haystack_str for tok in tokens):
                if f["event_code"] not in seen:
                    seen.add(f["event_code"])
                    matched.append(f)

    if not matched:
        print(f"  ⚠️  No formats matched #{ '+'.join(format_specs) }; "
              f"falling back to base event {base_event_code}")
        for f in available_formats:
            if f["event_code"] == base_event_code:
                return [f]
        return fallback

    return matched


def parse_dates(data, movie_key="", movie_name=""):
    dates = []
    for w in data.get("data", {}).get("topStickyWidgets", []):
        if w.get("type") != "horizontal-block-list":
            continue
        for item in w.get("data", []):
            texts = item.get("data", [])
            if len(texts) >= 3:
                style = item.get("styleId", "")
                dates.append(DateInfo(
                    date_code=item.get("id", ""),
                    status=DATE_STYLE_MAP.get(style, "UNKNOWN"),
                    movie_key=movie_key,
                    movie_name=movie_name,
                ))
    return dates


def parse_shows(data, movie_name="", lang_format=""):
    shows = []
    for w in data.get("data", {}).get("showtimeWidgets", []):
        if w.get("type") != "groupList":
            continue
        for g in w.get("data", []):
            if g.get("type") != "venueGroup":
                continue
            for card in g.get("data", []):
                if card.get("type") != "venue-card":
                    continue
                addl = card.get("additionalData", {})
                vname = addl.get("venueName", "Unknown")
                vcode = addl.get("venueCode", "")

                for st in card.get("showtimes", []):
                    sa = st.get("additionalData", {})
                    date_code = str(
                        sa.get("showDateCode", "")
                        or sa.get("dateCode", "")
                    ).strip()
                    if not date_code and re.match(
                            r"^\d{8}", sa.get("cutOffDateTime", "")):
                        date_code = sa["cutOffDateTime"][:8]

                    raw_screen = (st.get("screenAttr", "")
                                  or sa.get("attributes", ""))
                    if lang_format and raw_screen:
                        display_attr = f"{raw_screen} · {lang_format}"
                    elif lang_format:
                        display_attr = lang_format
                    else:
                        display_attr = raw_screen

                    show = ShowInfo(
                        venue_code=vcode,
                        venue_name=vname,
                        session_id=sa.get("sessionId", ""),
                        date_code=date_code,
                        time=st.get("title", ""),
                        time_code=str(sa.get("showTimeCode", "")),
                        screen_attr=display_attr,
                        movie_name=movie_name,
                        lang_format=lang_format,
                    )
                    for cat in sa.get("categories", []):
                        ca = str(cat.get("availStatus", ""))
                        show.categories.append(CatInfo(
                            name=cat.get("priceDesc", ""),
                            price=cat.get("curPrice", "0"),
                            status=ca,
                        ))
                    shows.append(show)
    return shows


def dedupe_shows(shows):
    unique = {}
    for s in shows:
        unique[(s.venue_code, s.session_id, s.date_code, s.time)] = s
    return list(unique.values())


# ──────────────────────────────────────────────────────────────────────
# FILTERING (Supports "Theatre" and "Theatre:Screen" rules)
# ──────────────────────────────────────────────────────────────────────
def parse_theatre_rules(theatre_filter):
    """
    Parse 'ALLU,Prasads,AAA:LED' into a list of (venue_kw, screen_kw) tuples.
    - 'Prasads' -> ('prasads', None) matches any screen at Prasads
    - 'AAA:LED' -> ('aaa', 'led') matches AAA only when screen_attr contains 'led'
    """
    rules = []
    if not theatre_filter:
        return rules
    for item in theatre_filter.split(","):
        item = item.strip()
        if not item:
            continue
        if ":" in item:
            v_kw, s_kw = item.split(":", 1)
            rules.append((v_kw.strip().lower(), s_kw.strip().lower()))
        else:
            rules.append((item.lower(), None))
    return rules


def filter_shows(shows, theatre_filter, time_periods, date_codes):
    result = []
    rules = parse_theatre_rules(theatre_filter)
    periods = [p.strip().lower() for p in time_periods.split(",")
               if p.strip()] if time_periods else []
    dates_set = set(d.strip() for d in date_codes.split(",")
                    if d.strip()) if date_codes else set()

    for s in shows:
        # Theatre + optional Screen filter
        if rules:
            name_lower = s.venue_name.lower()
            screen_lower = (s.screen_attr or "").lower()
            matched_venue = False
            for v_kw, s_kw in rules:
                if v_kw in name_lower:
                    if not s_kw or s_kw in screen_lower:
                        matched_venue = True
                        break
            if not matched_venue:
                continue

        # Date filter
        if dates_set and s.date_code and s.date_code not in dates_set:
            continue

        # Time period filter
        if periods:
            try:
                tc = int(s.time_code)
            except ValueError:
                tc = 0
            matched = False
            for p in periods:
                if p in TIME_PERIODS:
                    lo, hi = TIME_PERIODS[p]
                    if lo <= tc < hi:
                        matched = True
                        break
            if not matched:
                continue

        result.append(s)
    return result


# ──────────────────────────────────────────────────────────────────────
# STATE (for change detection between runs)
# ──────────────────────────────────────────────────────────────────────
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def build_state(shows, dates):
    show_state = {}
    for s in shows:
        for c in s.categories:
            key = f"{s.venue_code}|{s.session_id}|{s.date_code}|{c.name}"
            show_state[key] = {
                "venue": s.venue_name,
                "time": s.time,
                "date": s.date_code,
                "cat": c.name,
                "price": c.price,
                "status": c.status,
                "movie": s.movie_name,
                "screen": s.screen_attr,
            }

    date_state = {}
    for d in dates:
        d_key = f"{d.movie_key}|{d.date_code}" if d.movie_key else d.date_code
        date_state[d_key] = d.status

    return {"shows": show_state, "dates": date_state}


def merge_state(old_state, fetched_state):
    return {
        "shows": {
            **old_state.get("shows", {}),
            **fetched_state.get("shows", {}),
        },
        "dates": {
            **old_state.get("dates", {}),
            **fetched_state.get("dates", {}),
        },
        "fail_streak": 0,
    }


def _format_show_alert(prefix, s, suffix=""):
    movie_lbl = f"[{s['movie']}] " if s.get("movie") else ""
    screen_lbl = f" ({s['screen']})" if s.get("screen") else ""
    return (
        f"{prefix}: {movie_lbl}{s['venue']} {s['time']}{screen_lbl} "
        f"[{s['date']}] — {s['cat']} ₹{s['price']}{suffix}"
    )


def detect_changes(old_state, new_state):
    changes = []

    old_dates = old_state.get("dates", {})
    new_dates = new_state.get("dates", {})
    for d_key, status in new_dates.items():
        old_status = old_dates.get(d_key)
        # Also check legacy un-prefixed date key for smooth transition
        plain_dc = d_key.split("|")[-1]
        if old_status is None and plain_dc in old_dates:
            old_status = old_dates.get(plain_dc)

        if (old_status == "NOT_OPEN"
                and status in ("BOOKABLE", "AVAILABLE")):
            changes.append(f"📅 NEW DATE OPENED: {plain_dc}")

    old_shows = old_state.get("shows", {})
    new_shows = new_state.get("shows", {})

    for key in sorted(set(new_shows) - set(old_shows)):
        s = new_shows[key]
        changes.append(_format_show_alert("🆕 NEW", s))

    for key, new_s in new_shows.items():
        old_s = old_shows.get(key)
        if old_s and old_s["status"] == "0" and new_s["status"] != "0":
            lbl, ico = AVAIL_STATUS_MAP.get(
                new_s["status"], ("UNKNOWN", "⚪")
            )
            changes.append(
                _format_show_alert(f"{ico} BACK", new_s, suffix=f" → {lbl}")
            )

    return changes


# ──────────────────────────────────────────────────────────────────────
# TELEGRAM NOTIFICATION
# ──────────────────────────────────────────────────────────────────────
def _pretty_date(date_code):
    try:
        return datetime.strptime(date_code, "%Y%m%d").strftime("%d %b")
    except ValueError:
        return date_code


def _time_key(t):
    try:
        return datetime.strptime(t.strip(), "%I:%M %p")
    except ValueError:
        return datetime.max


def send_telegram(subject, changes, shows, movie_info):
    token = TELEGRAM_BOT_TOKEN.strip()
    chat_id = TELEGRAM_CHAT_ID.strip()
    if not token or not chat_id:
        print("  ⚠️  Skipping Telegram — TELEGRAM_BOT_TOKEN or "
              "TELEGRAM_CHAT_ID not set.")
        return

    now_str = datetime.now(IST).strftime("%d %b, %I:%M %p IST")
    lines = [f"🎬 {subject}", now_str, "", "Changes:"]
    lines.extend(changes)

    grouped = {}
    for s in shows:
        key = (s.movie_name, s.venue_name, s.date_code, s.screen_attr)
        grouped.setdefault(key, set()).add(s.time)
    if grouped:
        lines.extend(["", "Current shows:"])
        for (mname, venue, dc, scr), times in sorted(
            grouped.items(),
            key=lambda kv: (kv[0][0], kv[0][2], kv[0][1], kv[0][3]),
        ):
            m_prefix = f"[{mname}] " if mname else ""
            scr_lbl = f" ({scr})" if scr else ""
            lines.append(
                f"{m_prefix}{venue}{scr_lbl} · {_pretty_date(dc)}: "
                f"{', '.join(sorted(times, key=_time_key))}"
            )

    first_url = parse_bms_url(CONFIG["url"].split(",")[0])["clean_url"]
    lines.extend(["", f"Book: {first_url}"])
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3990] + "\n…"

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        if resp.status_code == 200:
            print("  ✅ Telegram message sent")
        else:
            print(f"  ❌ Telegram {resp.status_code}: {resp.text}")
    except requests.RequestException as e:
        print(f"  ❌ Telegram failed: {e}")


# ──────────────────────────────────────────────────────────────────────
# EMAIL NOTIFICATION (Resend)
# ──────────────────────────────────────────────────────────────────────
def _cat_status_label(status):
    return AVAIL_STATUS_MAP.get(status, ("UNKNOWN", ""))[0]


def send_email(subject, changes, shows, movie_info):
    api_key = RESEND_API_KEY.strip()
    to = RESEND_TO_EMAIL.strip()
    frm = RESEND_FROM_EMAIL.strip() or "onboarding@resend.dev"

    if not api_key or not to:
        print("  ⚠️  Skipping email — RESEND_API_KEY or RESEND_TO_EMAIL not set.")
        return

    now_str = datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST")
    movie_name = movie_info.get("name", "Movie")

    changes_html = ""
    if changes:
        rows = "".join(
            f'<li style="padding:3px 0;font-size:14px;">{escape(c)}</li>'
            for c in changes
        )
        changes_html = f"""
        <h3 style="margin:0 0 8px 0;font-size:15px;font-weight:bold;color:#333;">
            Changes Detected
        </h3>
        <ul style="margin:0 0 20px 0;padding-left:20px;line-height:1.6;color:#333;">
            {rows}
        </ul>"""

    venue_groups = {}
    for s in shows:
        header_label = f"{s.movie_name} — {s.venue_name}" if s.movie_name else s.venue_name
        venue_groups.setdefault(header_label, []).append(s)

    shows_html = ""
    for vname, vshows in venue_groups.items():
        show_rows = ""
        for s in vshows:
            cats = " | ".join(
                f"{escape(c.name)} Rs.{escape(c.price)} ({_cat_status_label(c.status)})"
                for c in s.categories
            )
            fmt = f" [{escape(s.screen_attr)}]" if s.screen_attr else ""
            show_rows += (
                f'<tr>'
                f'<td style="padding:5px 8px;border-bottom:1px solid #ddd;'
                f'font-size:13px;vertical-align:top;">'
                f'{escape(_pretty_date(s.date_code))} {escape(s.time)}{fmt}</td>'
                f'<td style="padding:5px 8px;border-bottom:1px solid #ddd;'
                f'font-size:13px;vertical-align:top;">'
                f'{cats}</td>'
                f'</tr>'
            )

        shows_html += f"""
        <p style="margin:14px 0 4px 0;font-size:14px;font-weight:bold;color:#333;">
            {escape(vname)}
        </p>
        <table style="width:100%;border-collapse:collapse;font-size:13px;">
            <tr style="background:#f5f5f5;">
                <th style="padding:5px 8px;text-align:left;border-bottom:1px solid #ddd;
                           font-weight:bold;">Date / Time</th>
                <th style="padding:5px 8px;text-align:left;border-bottom:1px solid #ddd;
                           font-weight:bold;">Categories</th>
            </tr>
            {show_rows}
        </table>"""

    html = f"""<!doctype html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:24px;font-family:Arial,Helvetica,sans-serif;
             font-size:14px;color:#333;background:#fff;">
    <h2 style="margin:0 0 4px 0;font-size:18px;color:#111;">
        BMS Alert: {escape(movie_name)}
    </h2>
    <p style="margin:0 0 20px 0;font-size:13px;color:#666;">
        {escape(now_str)}
    </p>
    <hr style="border:none;border-top:1px solid #ddd;margin:0 0 20px 0;">
    {changes_html}
    <h3 style="margin:0 0 8px 0;font-size:15px;font-weight:bold;color:#333;">
        Current Showtimes
    </h3>
    {shows_html}
    <p style="margin:24px 0 0 0;font-size:12px;color:#999;">
        This is an automated alert from BMS Ticket Notifier.
    </p>
</body>
</html>"""

    plain_lines = [subject, "", f"Checked at: {now_str}", ""]
    if changes:
        plain_lines.append("Changes Detected:")
        plain_lines.extend(f"  - {c}" for c in changes)
        plain_lines.append("")
    plain_lines.append("Current Showtimes:")
    for vname, vshows in venue_groups.items():
        plain_lines.append(f"\n{vname}")
        for s in vshows:
            cats = " | ".join(
                f"{c.name} Rs.{c.price} ({_cat_status_label(c.status)})"
                for c in s.categories
            )
            fmt = f" [{s.screen_attr}]" if s.screen_attr else ""
            plain_lines.append(
                f"  {_pretty_date(s.date_code)} {s.time}{fmt} - {cats}")
    plain_lines.extend(["", "This is an automated alert from BMS Ticket Notifier."])
    plain = "\n".join(plain_lines)

    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": frm, "to": [to],
                "subject": subject,
                "text": plain, "html": html,
            },
            timeout=15,
        )
        if resp.status_code in (200, 201):
            print(f"  ✅ Email sent to {to}")
        else:
            print(f"  ❌ Resend {resp.status_code}: {resp.text}")
    except requests.RequestException as e:
        print(f"  ❌ Email failed: {e}")


def send_notice(subject, message):
    info = {"name": "BMS Ticket Checker"}
    send_telegram(subject, [message], [], info)
    send_email(subject, [message], [], info)


# ──────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────
def main():
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    engine = "curl_cffi (Browser TLS)" if HAS_CFFI else "requests (standard)"
    print(f"[{now_str}] BMS Ticket Checker — CI mode [{engine}]")

    raw_urls = [u.strip() for u in CONFIG["url"].split(",") if u.strip()]
    if not raw_urls:
        print("  ❌ Invalid BMS_URL. No URL provided.")
        sys.exit(1)

    raw_dates = CONFIG["dates"].strip()
    old_state = load_state()
    prev_streak = int(old_state.get("fail_streak", 0) or 0)

    current_profile = 0
    session = create_session(current_profile)

    all_shows = []
    all_dates = []
    tracked_movie_names = []
    total_requests = 0
    total_failed = 0

    for u_idx, url in enumerate(raw_urls):
        parsed = parse_bms_url(url)
        base_event_code = parsed["event_code"]
        region_slug = parsed["region_slug"]
        url_date = parsed.get("date_code", "")
        format_specs = parsed["format_specs"]

        if not base_event_code or not region_slug:
            print(f"  ❌ Skipping invalid URL: {url}")
            continue

        region_code, region_slug_r, lat, lon, geohash = resolve_region(region_slug)

        if raw_dates:
            date_list = [d.strip() for d in raw_dates.split(",") if d.strip()]
        elif url_date:
            date_list = [url_date]
        else:
            date_list = [""]

        spec_label = f"#{ '+'.join(format_specs) }" if format_specs else "(all formats)"
        print(f"\n🔎 Base Event: {base_event_code} {spec_label}  "
              f"Region: {region_code}  Dates: {date_list}")

        # Step 1: Discover movie info and all available formats using the first date
        first_dc = date_list[0]
        if total_requests > 0:
            time.sleep(INTER_DATE_DELAY)
        total_requests += 1

        init_data, session, current_profile = fetch_bms(
            base_event_code, first_dc, region_code, region_slug_r,
            lat, lon, geohash,
            session=session,
            start_profile=current_profile,
        )
        if not init_data:
            print(f"  ⚠️  Initial discovery failed for {base_event_code}")
            total_failed += 1
            continue

        movie_info = parse_movie_info(init_data)
        m_name = movie_info["name"]
        if m_name != "Unknown Movie" and m_name not in tracked_movie_names:
            tracked_movie_names.append(m_name)

        available_formats = extract_available_formats(init_data)
        if available_formats:
            print(f"  🎥 {m_name} — Available formats on BMS: "
                  f"{', '.join(f['display'] for f in available_formats)}")
        else:
            print(f"  🎥 {m_name}  {movie_info['language']}")

        target_formats = match_target_formats(
            available_formats,
            base_event_code,
            movie_info["language"],
            format_specs,
        )
        print(f"  🎯 Tracking {len(target_formats)} format(s): "
              f"{', '.join(f'{f[\"display\"]} ({f[\"event_code\"]})' for f in target_formats)}")

        # Step 2: Fetch each matched format across the requested dates
        for tf in target_formats:
            f_code = tf["event_code"]
            f_display = tf["display"]
            open_dates_for_fmt = set()
            failed_for_fmt = []

            for dc in date_list:
                # Reuse the initial discovery response if it matches (f_code, first_dc)
                if f_code == base_event_code and dc == first_dc:
                    data = init_data
                else:
                    if open_dates_for_fmt and dc and dc not in open_dates_for_fmt:
                        print(f"  ⏳ [{f_display}] Date {dc} is not open on BMS yet — skipping")
                        continue

                    time.sleep(INTER_DATE_DELAY)
                    total_requests += 1
                    data, session, current_profile = fetch_bms(
                        f_code, dc, region_code, region_slug_r,
                        lat, lon, geohash,
                        session=session,
                        start_profile=current_profile,
                    )

                if not data:
                    print(f"  ⚠️  [{f_display}] No data for date {dc or '(default)'}")
                    failed_for_fmt.append(dc)
                    total_failed += 1
                    continue

                fetched_dates = parse_dates(data, movie_key=base_event_code, movie_name=m_name)
                all_dates.extend(fetched_dates)
                for d in fetched_dates:
                    if d.status in ("BOOKABLE", "AVAILABLE"):
                        open_dates_for_fmt.add(d.date_code)

                all_shows.extend(parse_shows(data, movie_name=m_name, lang_format=f_display))

            if failed_for_fmt:
                print(f"  ⚠️  [{f_display}] {len(failed_for_fmt)} of {len(date_list)} date(s) "
                      f"failed: {', '.join(d or '(default)' for d in failed_for_fmt)} "
                      "(previous state kept)")

    if total_requests > 0 and total_failed == total_requests:
        streak = prev_streak + 1
        print(f"\n  ❌ Every request failed ({streak} run(s) in a row). "
              "BookMyShow is refusing requests from this runner.")
        save_state({**old_state, "fail_streak": streak})
        if streak == BLOCK_ALERT_AFTER:
            send_notice(
                "BMS Alert: requests are being blocked",
                f"BookMyShow has refused every request for {streak} runs in "
                "a row. No alerts can be sent until it recovers. A quiet "
                "log does NOT mean nothing changed.",
            )
        sys.exit(0)

    if prev_streak >= BLOCK_ALERT_AFTER:
        send_notice(
            "BMS Alert: requests are working again",
            f"BookMyShow is answering again after {prev_streak} blocked "
            "runs in a row. Shows added during the gap will show up below "
            "as NEW.",
        )

    if not all_shows:
        print("\n  ❌ No showtimes found.")
        if prev_streak:
            save_state({**old_state, "fail_streak": 0})
        sys.exit(0)

    # Deduplicate and strictly filter by BMS_THEATRE (including Theatre:Screen rules)
    # BEFORE building state or detecting changes.
    all_shows = dedupe_shows(all_shows)
    filtered = filter_shows(
        all_shows,
        CONFIG["theatre"],
        CONFIG["time_period"],
        CONFIG["dates"],
    )
    print(f"\n  📊 {len(filtered)} showtime(s) matched your BMS_THEATRE & time filters")

    # Only report NEW DATE OPENED for dates that actually have shows at your filtered theatres
    active_dates_at_theatres = {s.date_code for s in filtered}
    filtered_dates = [
        d for d in all_dates
        if d.status == "NOT_OPEN" or d.date_code in active_dates_at_theatres
    ]

    has_baseline = bool(old_state.get("shows") or old_state.get("dates"))
    new_state = merge_state(old_state, build_state(filtered, filtered_dates))

    changes = []
    if has_baseline:
        changes = detect_changes(old_state, new_state)

    save_state(new_state)

    title_movies = ", ".join(tracked_movie_names) if tracked_movie_names else "Movies"
    summary_info = {"name": title_movies}

    if changes:
        print(f"\n  ⚡ {len(changes)} change(s) detected:")
        for c in changes:
            print(f"     {c}")
        subject = f"BMS Alert: {title_movies} - {len(changes)} change(s)"
        send_telegram(subject, changes, filtered, summary_info)
        send_email(subject, changes, filtered, summary_info)
    else:
        print("  ✅ No changes since last check.")

    print(f"\n  Current status ({len(filtered)} shows):")
    for s in filtered:
        cats = ", ".join(
            f"{c.name}=₹{c.price}({AVAIL_STATUS_MAP.get(c.status, ('?',''))[0]})"
            for c in s.categories
        )
        fmt = f"|{s.screen_attr}" if s.screen_attr else ""
        m_lbl = f"[{s.movie_name}] " if len(tracked_movie_names) > 1 else ""
        print(f"    {m_lbl}{s.venue_name} — {s.time}{fmt} [{s.date_code}] — {cats}")

    print("\n  Done.")


if __name__ == "__main__":
    main()
