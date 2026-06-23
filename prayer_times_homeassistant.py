#!/usr/bin/env python3
"""Fetch prayer times from the Jordanian Awqaf website and save as JSON."""

import argparse
import json
import re
import sys
import unicodedata
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.awqaf.gov.jo/ar/Pages/PrayerTime"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:148.0) Gecko/20100101 Firefox/148.0",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9,ar-JO;q=0.8",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Origin": "https://www.awqaf.gov.jo",
    "Referer": BASE_URL,
    "DNT": "1",
    "Connection": "keep-alive",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}

AJAX_HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "X-MicrosoftAjax": "Delta=true",
    "Cache-Control": "no-cache",
    "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
}

PRAYER_CLOCK_KEYS = frozenset(
    {"fajr", "sunrise", "dhuhr", "asr", "maghrib", "isha"}
)

_AFTERNOON_SALAH_NO_MARKER_PM = frozenset({"asr", "maghrib", "isha"})

_PRAYER_CLOCK_CELL_RE = re.compile(r"(\d{1,2}):(\d{2})")


def normalize_awqaf_prayer_clock(raw: str, english_key: str | None = None) -> str:
    """Return 24-hour ``H:MM`` from an Awqaf table time cell.

    Marker-based: ``صباح`` / ``مساء``, or lone trailing ``ص`` / ``م``.
    Examples: ``7:13 مساءً`` → ``19:13``; ``4:40 صباحاً`` → ``4:40``.

    Without markers: ``asr`` / ``maghrib`` / ``isha`` hours 1–11 get ``+12`` (site omits
    ``مساء``). ``dhuhr``: hour ``1`` → ``+12`` (1 PM → 13:MM); ``11`` / ``12`` unchanged.
    Hours 13+ are never shifted. ``fajr`` / ``sunrise``: no such heuristic.
    """
    text = (raw or "").strip()
    if not text:
        return ""

    m = _PRAYER_CLOCK_CELL_RE.search(text)
    if not m:
        return text

    h = int(m.group(1))
    mi = int(m.group(2))
    s = unicodedata.normalize("NFKC", text)

    has_am = "صباح" in s
    has_pm = "مساء" in s

    if not has_am and not has_pm:
        tail = s[m.end() :].strip()
        if tail:
            if re.fullmatch(r"\u0645[\s\u064b-\u0652\u061f.,،؟؛:]*", tail):
                has_pm = True
            elif re.fullmatch(r"\u0635[\s\u064b-\u0652\u061f.,،؟؛:]*", tail):
                has_am = True

    if has_am and has_pm:
        pass
    elif h >= 13:
        pass
    elif has_pm:
        if 1 <= h <= 11:
            h += 12
    elif has_am:
        if h == 12:
            h = 0
    elif english_key in _AFTERNOON_SALAH_NO_MARKER_PM and 1 <= h <= 11:
        h += 12
    elif english_key == "dhuhr" and h == 1:
        h += 12

    return f"{h}:{mi:02d}"


def drop_company_post_string(raw_value: str, label_text: str) -> str | None:
    """Option ``value`` to POST; ``EnableEventValidation`` rejects label text when values are numeric."""
    v = (raw_value or "").strip()
    t = (label_text or "").strip()
    if t == "الرجاء الاختيار" or v == "الرجاء الاختيار":
        return None
    if not v and not t:
        return None
    if v == "0" and "اختيار" in t:
        return None
    return v or t


def _collect_options_from_select(select) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    if not select:
        return out
    for opt in select.find_all("option"):
        raw_val = (opt.get("value") or "").strip()
        label = opt.get_text(strip=True)
        post = drop_company_post_string(raw_val, label)
        if post is None:
            continue
        out.append((post, label or post, raw_val))
    return out


def find_drop_company_select(soup: BeautifulSoup):
    sel = soup.find("select", id=re.compile(r"DropCompany", re.I))
    if sel:
        return sel
    for candidate in soup.find_all("select"):
        nm = candidate.get("name") or ""
        idi = candidate.get("id") or ""
        if "DropCompany" in nm or "DropCompany" in idi:
            return candidate
    return None


def region_options_from_html(html_text: str) -> list[tuple[str, str, str]]:
    soup = BeautifulSoup(html_text, "html.parser")
    region_options = _collect_options_from_select(find_drop_company_select(soup))
    if region_options:
        return region_options
    block = re.search(
        r"<select[^>]*DropCompany[^>]*>.*?</select>",
        html_text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if block:
        return _collect_options_from_select(
            BeautifulSoup(block.group(0), "html.parser").find("select")
        )
    return []


def get_session_and_viewstate(
    session: requests.Session,
) -> tuple[dict, list[tuple[str, str, str]]]:
    """GET the page to obtain fresh viewstate, event validation, cookies, and city list."""
    resp = session.get(BASE_URL, headers={
        "User-Agent": HEADERS["User-Agent"],
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": HEADERS["Accept-Language"],
    })
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    fields = {}
    for name in ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION",
                  "__EVENTTARGET", "__EVENTARGUMENT"):
        tag = soup.find("input", {"name": name})
        fields[name] = tag["value"] if tag else ""

    for inp in soup.find_all("input", {"type": "hidden"}):
        name = inp.get("name", "")
        if name and name not in fields:
            fields[name] = inp.get("value", "")

    region_options = region_options_from_html(resp.text)

    return fields, region_options


def build_post_data(hidden_fields: dict, city: str, from_date: str, to_date: str) -> dict:
    """Build the POST form data combining hidden fields with user parameters."""
    data = dict(hidden_fields)

    data["ctl00$ScriptManagerSiteMaster"] = (
        "ctl00$MainContent$UpdatePanel1|ctl00$MainContent$btn_search"
    )
    data["ctl00$MainContent$DropCompany"] = city
    data["ctl00$MainContent$txtFromDate"] = from_date
    data["ctl00$MainContent$txtToDate"] = to_date
    data["ctl00$MainContent$btn_search"] = "ابحث"
    data["__ASYNCPOST"] = "true"

    # Ensure search boxes are present (empty)
    data.setdefault("ctl00$txtSearch", "")
    data.setdefault("ctl00$txtSearch1", "")

    return data


def extract_html_from_ajax(response_text: str) -> str:
    """Extract the HTML content from an ASP.NET AJAX UpdatePanel response.

    The response format is pipe-delimited:  length|type|id|content|
    We look for the 'updatePanel' type to get the HTML.
    """
    parts = response_text.split("|")
    i = 0
    while i < len(parts) - 3:
        try:
            length = int(parts[i])
        except ValueError:
            i += 1
            continue
        part_type = parts[i + 1]
        part_id = parts[i + 2]
        content = parts[i + 3]
        if part_type == "updatePanel":
            return content
        i += 4
    # Fallback: return the whole response if no updatePanel found
    return response_text


def parse_prayer_times(html: str) -> list[dict]:
    """Parse the prayer times table from the HTML response."""
    soup = BeautifulSoup(html, "html.parser")

    # Look for the results table/grid
    table = (
        soup.find("table", {"id": re.compile(r"grd_Result|GridView", re.I)})
        or soup.find("table", class_=re.compile(r"grid|table|result", re.I))
        or soup.find("table")
    )

    if not table:
        return []

    rows = table.find_all("tr")
    if len(rows) < 2:
        return []

    # Extract header names from the first row
    header_row = rows[0]
    headers = [th.get_text(strip=True) for th in header_row.find_all(["th", "td"])]

    # Known column mapping (Arabic -> English)
    column_map = {
        "اليوم": "day",
        "التاريخ": "date",
        "الفجر": "fajr",
        "الشروق": "sunrise",
        "الظهر": "dhuhr",
        "العصر": "asr",
        "المغرب": "maghrib",
        "العشاء": "isha",
    }

    results = []
    for row in rows[1:]:
        cells = [td.get_text(strip=True) for td in row.find_all("td")]
        if not cells or len(cells) < 2:
            continue

        entry = {}
        for idx, cell_value in enumerate(cells):
            if idx < len(headers):
                arabic_header = headers[idx]
                english_key = column_map.get(arabic_header, arabic_header)
                if english_key in PRAYER_CLOCK_KEYS:
                    entry[english_key] = normalize_awqaf_prayer_clock(
                        cell_value, english_key
                    )
                else:
                    entry[english_key] = cell_value.strip()
            else:
                entry[f"col_{idx}"] = cell_value

        results.append(entry)

    return results


def resolve_drop_company(
    city: str, region_options: list[tuple[str, str, str]]
) -> tuple[str, str]:
    """Return (DropCompany POST string, display label)."""
    if not region_options:
        print(
            "Warning: No DropCompany options in HTML. "
            "POSTing --city as given; use a numeric region id from the site if this fails.",
            file=sys.stderr,
        )
        return city, city

    for post, label, raw in region_options:
        if city == post or city == label:
            return post, label
        if raw.isdigit() and city == raw:
            return post, label

    matches = [
        (post, label)
        for post, label, raw in region_options
        if city in post or city in label
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        print(f"Error: '{city}' matches multiple regions:", file=sys.stderr)
        for _, lbl in matches:
            print(f"  - {lbl}", file=sys.stderr)
        sys.exit(1)

    print(
        f"Error: City '{city}' not found. Use --list-cities. "
        f"Sample: {[(p, l) for p, l, _ in region_options[:5]]}",
        file=sys.stderr,
    )
    sys.exit(1)


def fetch_prayer_times(
    from_date: str, to_date: str, city: str = "اربد", list_cities: bool = False
) -> tuple[list[dict], str]:
    """Fetch and return (parsed rows, resolved region label). ``list_cities`` → ``([], \"\")``."""
    session = requests.Session()

    print("Initializing session and fetching viewstate...")
    hidden_fields, region_options = get_session_and_viewstate(session)
    print(f"Session established. Cookies: {list(session.cookies.keys())}")

    if list_cities:
        print("\nAvailable cities/regions (label / numeric value_attr / POST field):")
        for post, label, raw in region_options:
            print(f"  - {label}  |  value={raw!r}  →  POST DropCompany={post!r}")
        return [], ""

    post_company, display_label = resolve_drop_company(city, region_options)

    post_data = build_post_data(hidden_fields, post_company, from_date, to_date)

    headers = {**HEADERS, **AJAX_HEADERS}
    print(f"Fetching prayer times for {display_label} from {from_date} to {to_date}...")
    resp = session.post(BASE_URL, data=post_data, headers=headers)
    resp.raise_for_status()

    html_content = extract_html_from_ajax(resp.text)
    prayer_times = parse_prayer_times(html_content)

    if not prayer_times:
        print("Warning: No prayer times found in response. The page structure may have changed.",
              file=sys.stderr)
        print("Response preview (first 2000 chars):", file=sys.stderr)
        print(resp.text[:2000], file=sys.stderr)

    return prayer_times, display_label


def validate_date(date_str: str) -> str:
    """Validate that the date string is in YYYY/MM/DD format."""
    try:
        parts = date_str.split("/")
        if len(parts) != 3:
            raise ValueError
        date(int(parts[0]), int(parts[1]), int(parts[2]))
    except (ValueError, IndexError):
        raise argparse.ArgumentTypeError(
            f"Invalid date format: '{date_str}'. Expected YYYY/MM/DD"
        )
    return date_str


def main():
    today = date.today()
    default_from = today.strftime("%Y/%m/%d")
    default_to = (today + timedelta(days=7)).strftime("%Y/%m/%d")

    parser = argparse.ArgumentParser(
        description="Fetch prayer times from the Jordanian Awqaf website."
    )
    parser.add_argument(
        "--from-date",
        type=validate_date,
        default=default_from,
        help=f"Start date in YYYY/MM/DD format (default: {default_from})",
    )
    parser.add_argument(
        "--to-date",
        type=validate_date,
        default=default_to,
        help=f"End date in YYYY/MM/DD format (default: {default_to})",
    )
    parser.add_argument(
        "--city",
        default="اربد",
        help="City/region name in Arabic (default: اربد)",
    )
    parser.add_argument(
        "--output",
        default="/config/appdaemon/apps/prayer_times.json",
        help="Output JSON file path (default: prayer_times.json)",
    )
    parser.add_argument(
        "--list-cities",
        action="store_true",
        help="List available cities/regions and exit",
    )

    args = parser.parse_args()

    prayer_times, resolved_city = fetch_prayer_times(
        args.from_date, args.to_date, args.city, args.list_cities
    )

    if args.list_cities:
        return

    output = {
        "city": resolved_city,
        "from_date": args.from_date,
        "to_date": args.to_date,
        "count": len(prayer_times),
        "prayer_times": prayer_times,
    }

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"Saved {len(prayer_times)} entries to {args.output}")

    if prayer_times:
        print(f"\nPreview (first entry):")
        for key, val in prayer_times[0].items():
            print(f"  {key}: {val}")


if __name__ == "__main__":
    main()
