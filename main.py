"""
BMS Ticket Checker — CI/Headless mode for GitHub Actions.
Runs once, checks all configured watches, emails on changes.
State is persisted via a JSON artifact.

Configure via environment variables or edit the CONFIG below.
"""

import os
import re
import sys
import json
from html import escape
from datetime import datetime
from dataclasses import dataclass, field
from urllib.parse import urlparse
import requests

# ──────────────────────────────────────────────────────────────────────
# CONFIGURATION — edit these or set via env vars
# ──────────────────────────────────────────────────────────────────────
CONFIG = {
    "url": os.getenv(
        "BMS_URL",
        "https://in.bookmyshow.com/movies/chennai/dhurandhar-the-revenge/buytickets/ET00478890"
    ),
    "dates": os.getenv("BMS_DATES", ""),          # comma-separated YYYYMMDD, empty = from URL
    "theatre": os.getenv("BMS_THEATRE", ""),       # substring filter, empty = all
    "time_period": os.getenv("BMS_TIME", ""),      # e.g. "evening,night", empty = all
}

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
RESEND_TO_EMAIL = os.getenv("RESEND_TO_EMAIL", "")
RESEND_FROM_EMAIL = os.getenv("RESEND_FROM_EMAIL", "aviiciii@resend.dev")

STATE_FILE = "bms_state.json"

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
    "bengaluru":  ("BANG",   "bengaluru",  "12.972", "77.594", "tdr"),
    "bangalore":  ("BANG",   "bengaluru",  "12.972", "77.594", "tdr"),
    "hyderabad":  ("HYD",    "hyderabad",  "17.385", "78.487", "tep"),
    "kolkata":    ("KOLK",   "kolkata",    "22.573", "88.364", "tun"),
    "pune":       ("PUNE",   "pune",       "18.520", "73.856", "te2"),
    "kochi":      ("KOCH",   "kochi",      "9.932",  "76.267", "t9z"),
}


# ─────────────────────────────────────���────────────────────────────────
# DATA
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
    categories: list[CatInfo] = field(default_factory=list)
    event_code: str = ""
    language: str = ""
    format: str = ""

@dataclass
class DateInfo:
    date_code: str
    status: str


# ──────────────────────────────────────────────────────────────────────
# URL PARSER + REGION RESOLVER
# ──────────────────────────────────────────────────────────────────────
def parse_bms_url(url):
    path = urlparse(url).path.strip("/")
    parts = path.split("/")
    result = {"event_code": None, "date_code": None, "region_slug": None}
    for p in parts:
        if re.match(r"^ET\d{8,}$", p):
            result["event_code"] = p
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
    return (key.upper()[:6], key, "0", "0", "")


# ──────────────────────────────────────────────────────────────────────
# BMS API
# ──────────────────────────────────────────────────────────────────────
API_URL = (
    "https://in.bookmyshow.com/api/movies-data/v4/"
    "showtimes-by-event/primary-dynamic"
)


def fetch_bms(event_code, date_code, region_code, region_slug,
              lat, lon, geohash):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/145.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": (
            f"https://in.bookmyshow.com/movies/"
            f"{region_slug}/buytickets/{event_code}/"
        ),
        "sec-ch-ua": '"Chromium";v="145", "Not:A-Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "x-app-code": "WEB",
        "x-region-code": region_code,
        "x-region-slug": region_slug,
        "x-geohash": geohash,
        "x-latitude": lat,
        "x-longitude": lon,
        "x-location-selection": "manual",
        "x-lsid": "",
    }
    params = {
        "eventCode": event_code,
        "dateCode": date_code or "",
        "isDesktop": "true",
        "regionCode": region_code,
        "xLocationShared": "false",
        "memberId": "", "lsId": "", "subCode": "",
        "lat": lat, "lon": lon,
    }
    try:
        resp = requests.get(API_URL, headers=headers,
                            params=params, timeout=15)
        if resp.status_code == 200:
            return resp.json()
        print(f"  HTTP {resp.status_code}")
    except requests.RequestException as e:
        print(f"  Request failed: {e}")
    return None


# ──────────────────────────────────────────────────────────────────────
# PARSERS
# ──────────────────────────────────────────────────────────────────────
def parse_movie_info(data):
    info = {"name": "Unknown Movie", "language": "", "format": ""}
    for w in data.get("data", {}).get("topStickyWidgets", []):
        if w.get("type") == "horizontal-text-list":
            for item in w.get("data", []):
                for row in item.get("leftText", {}).get("data", []):
                    for c in row.get("components", []):
                        if "•" in c.get("text", ""):
                            language, _, movie_format = c["text"].partition("•")
                            info["language"] = language.strip()
                            info["format"] = movie_format.strip()
    bs = data.get("data", {}).get("bottomSheetData", {})
    for w in bs.get("format-selector", {}).get("widgets", []):
        if w.get("type") == "vertical-text-list":
            for d in w.get("data", []):
                if d.get("styleId") == "bottomsheet-subtitle":
                    info["name"] = d.get("text", info["name"])
    return info


def parse_variant_codes(data):
    """Read event IDs only from this movie's language/format selector."""
    sheets = data.get("data", {}).get("bottomSheetData", {})
    codes = []

    def visit(value):
        if isinstance(value, dict):
            for key, child in value.items():
                visit(key)
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, str):
            # Selector actions can contain an ID, a URL, or encoded route data.
            for code in re.findall(r"\bET\d{8,}\b", value):
                if code not in codes:
                    codes.append(code)

    for name in ("format-selector", "language-selector"):
        visit(sheets.get(name, {}))
    return codes


def parse_dates(data):
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
                ))
    return dates


def parse_shows(data, event_code="", date_code=""):
    shows = []
    movie_info = parse_movie_info(data)
    selected_date = next(
        (d.date_code for d in parse_dates(data) if d.status == "BOOKABLE"),
        "",
    )
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
                    show_date = str(
                        sa.get("showDateCode", "")
                        or sa.get("dateCode", "")
                    ).strip()
                    if not show_date and re.match(
                            r"^\d{8}", sa.get("cutOffDateTime", "")):
                        show_date = sa["cutOffDateTime"][:8]

                    show = ShowInfo(
                        venue_code=vcode,
                        venue_name=vname,
                        session_id=sa.get("sessionId", ""),
                        date_code=show_date or date_code or selected_date,
                        time=st.get("title", ""),
                        time_code=sa.get("showTimeCode", ""),
                        screen_attr=(st.get("screenAttr", "")
                                     or sa.get("attributes", "")),
                        event_code=event_code,
                        language=movie_info["language"],
                        format=movie_info["format"],
                    )
                    for cat in sa.get("categories", []):
                        ca = str(cat.get("availStatus", ""))
                        lbl, _ = AVAIL_STATUS_MAP.get(ca, ("UNKNOWN", ""))
                        show.categories.append(CatInfo(
                            name=cat.get("priceDesc", ""),
                            price=cat.get("curPrice", "0"),
                            status=ca,
                        ))
                    shows.append(show)
    return shows


# ──────────────────────────────────────────────────────────────────────
# FILTERING
# ──────────────────────────────────────────────────────────────────────
def filter_shows(shows, theatre_filter, time_periods, date_codes):
    result = []
    kws = [k.strip().lower() for k in theatre_filter.split(",")
           if k.strip()] if theatre_filter else []
    periods = [p.strip().lower() for p in time_periods.split(",")
               if p.strip()] if time_periods else []
    dates_set = set(d.strip() for d in date_codes.split(",")
                    if d.strip()) if date_codes else set()

    for s in shows:
        # Theatre filter
        if kws:
            name_lower = s.venue_name.lower()
            if not any(k in name_lower for k in kws):
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
    """Build a comparable state dict."""
    show_state = {}
    for s in shows:
        for c in s.categories:
            key = (
                f"{s.event_code}|{s.venue_code}|{s.session_id}|"
                f"{s.date_code}|{c.name}"
            )
            show_state[key] = {
                "venue": s.venue_name,
                "time": s.time,
                "date": s.date_code,
                "cat": c.name,
                "price": c.price,
                "status": c.status,
                "event_code": s.event_code,
                "language": s.language,
                "format": s.format,
            }

    date_state = {
        f"{event_code}|{d.date_code}": d.status
        for event_code, d in dates
    }

    return {"shows": show_state, "dates": date_state}


def migrate_state(state, event_code):
    """Attribute the previous single-event state to the configured URL."""
    return {
        section: {
            key if re.match(r"^ET\d{8,}\|", key)
            else f"{event_code}|{key}": value
            for key, value in state.get(section, {}).items()
        }
        for section in ("shows", "dates")
    }


def variant_label(language, movie_format):
    return " / ".join(value for value in (language, movie_format) if value)


def show_label(show):
    label = variant_label(show.language, show.format) or show.event_code
    if show.screen_attr:
        label = " / ".join(value for value in (label, show.screen_attr) if value)
    return f" [{label}]" if label else ""


def detect_changes(old_state, new_state):
    changes = []

    # New dates opening
    old_dates = old_state.get("dates", {})
    new_dates = new_state.get("dates", {})
    for dc, status in new_dates.items():
        old_status = old_dates.get(dc)
        if (old_status == "NOT_OPEN"
                and status in ("BOOKABLE", "AVAILABLE")):
            event_code, _, date_code = dc.partition("|")
            changes.append(
                f"📅 NEW DATE OPENED: {date_code} [{event_code}]"
            )

    old_shows = old_state.get("shows", {})
    new_shows = new_state.get("shows", {})

    # New showtimes
    for key in set(new_shows) - set(old_shows):
        s = new_shows[key]
        label = variant_label(s.get("language", ""), s.get("format", ""))
        label = label or s.get("event_code", "")
        changes.append(
            f"🆕 NEW: {s['venue']} {s['time']} [{s['date']}] "
            f"[{label}] — {s['cat']} ₹{s['price']}"
        )

    # Sold out → available
    for key, new_s in new_shows.items():
        old_s = old_shows.get(key)
        if old_s and old_s["status"] == "0" and new_s["status"] != "0":
            lbl, ico = AVAIL_STATUS_MAP.get(
                new_s["status"], ("UNKNOWN", "⚪")
            )
            changes.append(
                f"{ico} BACK: {new_s['venue']} {new_s['time']} "
                f"[{new_s['date']}] "
                f"[{variant_label(new_s.get('language', ''), new_s.get('format', '')) or new_s.get('event_code', '')}] "
                f"— {new_s['cat']} → {lbl}"
            )

    return changes


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

    now_str = datetime.now().strftime("%d %b %Y, %I:%M %p")
    movie_name = movie_info.get("name", "Movie")

    # Build changes HTML
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

    # Build shows section grouped by venue
    venue_groups = {}
    for s in shows:
        venue_groups.setdefault(s.venue_name, []).append(s)

    shows_html = ""
    for vname, vshows in venue_groups.items():
        show_rows = ""
        for s in vshows:
            cats = " | ".join(
                f"{escape(c.name)} Rs.{escape(c.price)} ({_cat_status_label(c.status)})"
                for c in s.categories
            )
            fmt = escape(show_label(s))
            show_rows += (
                f'<tr>'
                f'<td style="padding:5px 8px;border-bottom:1px solid #ddd;'
                f'font-size:13px;vertical-align:top;">'
                f'{escape(s.date_code)} {escape(s.time)}{fmt}</td>'
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
                           font-weight:bold;">Date / Time / Language / Format</th>
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

    # Build plain-text version with full show details
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
            plain_lines.append(
                f"  {s.date_code} {s.time}{show_label(s)} - {cats}"
            )
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
            sys.exit(1)
    except requests.RequestException as e:
        print(f"  ❌ Email failed: {e}")
        sys.exit(1)


# ──────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────
def main():
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now_str}] BMS Ticket Checker — CI mode")

    # Parse config
    parsed = parse_bms_url(CONFIG["url"])
    event_code = parsed["event_code"]
    region_slug = parsed["region_slug"]
    url_date = parsed.get("date_code", "")

    if not event_code or not region_slug:
        print("  ❌ Invalid BMS_URL. Could not extract event/region.")
        sys.exit(1)

    region_code, region_slug_r, lat, lon, geohash = resolve_region(
        region_slug
    )

    # Determine dates to check
    raw_dates = CONFIG["dates"].strip()
    if raw_dates:
        date_list = [d.strip() for d in raw_dates.split(",") if d.strip()]
    elif url_date:
        date_list = [url_date]
    else:
        date_list = [""]

    print(f"  Event: {event_code}  Region: {region_code}  "
          f"Dates: {date_list}")

    # Each language/format can have its own event ID and date availability.
    all_shows = []
    all_dates = []
    movie_info = None
    event_codes = [event_code]
    checked = set()
    seen_shows = set()

    for variant_code in event_codes:
        for dc in dict.fromkeys(date_list):
            if (variant_code, dc) in checked:
                continue
            checked.add((variant_code, dc))
            data = fetch_bms(variant_code, dc, region_code,
                             region_slug_r, lat, lon, geohash)
            if not isinstance(data, dict) or not isinstance(data.get("data"), dict):
                print(
                    f"  ❌ Could not check {variant_code} "
                    f"for date {dc or '(default)'}. State left unchanged."
                )
                sys.exit(1)

            info = parse_movie_info(data)
            if movie_info is None:
                movie_info = info

            for code in parse_variant_codes(data):
                if code not in event_codes:
                    event_codes.append(code)

            all_dates.extend(
                (variant_code, d) for d in parse_dates(data)
            )
            shows = parse_shows(data, variant_code, dc)
            print(
                f"  🎬 {info['name']} "
                f"[{variant_label(info['language'], info['format']) or variant_code}]"
                f"  Date: {dc or '(default)'}  Shows: {len(shows)}"
            )
            for show in shows:
                key = (
                    show.event_code, show.venue_code,
                    show.session_id, show.date_code,
                )
                if key not in seen_shows:
                    seen_shows.add(key)
                    all_shows.append(show)

    print(f"  Checked {len(event_codes)} language/format event(s).")
    if len(event_codes) == 1:
        print(
            "  ⚠️  No alternate event IDs found in the format selector; "
            "only the configured event could be checked."
        )

    # Apply filters
    filtered = filter_shows(
        all_shows,
        CONFIG["theatre"],
        CONFIG["time_period"],
        CONFIG["dates"],
    )
    print(f"  📊 {len(filtered)} showtime(s) after filters")

    # Build state & detect changes
    new_state = build_state(filtered, all_dates)
    old_state = migrate_state(load_state(), event_code)

    changes = []
    if old_state["shows"] or old_state["dates"]:
        changes = detect_changes(old_state, new_state)

    if changes:
        print(f"\n  ⚡ {len(changes)} change(s) detected:")
        for c in changes:
            print(f"     {c}")
        send_email(
            f"BMS Alert: {movie_info['name']} - {len(changes)} change(s)",
            changes, filtered, movie_info,
        )
    else:
        print("  ✅ No changes since last check.")

    save_state(new_state)

    # Print current status
    print(f"\n  Current status ({len(filtered)} shows):")
    for s in filtered:
        cats = ", ".join(
            f"{c.name}=₹{c.price}({AVAIL_STATUS_MAP.get(c.status, ('?',''))[0]})"
            for c in s.categories
        )
        fmt = show_label(s)
        print(f"    {s.venue_name} — {s.time}{fmt} [{s.date_code}] — {cats}")

    print("\n  Done.")


if __name__ == "__main__":
    main()
