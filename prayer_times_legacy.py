#!/usr/bin/env python3
"""Fetch prayer times from the Jordanian Awqaf website and save as JSON."""

import argparse
import json
import re
import sys
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


def get_session_and_viewstate(session: requests.Session) -> tuple[dict, list[str]]:
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

    # Extract available cities from the dropdown
    cities = []
    select = soup.find("select", {"id": re.compile(r"DropCompany", re.I)})
    if select:
        for opt in select.find_all("option"):
            val = opt.get("value", "")
            if val and val != "الرجاء الاختيار":
                cities.append(val)

    return fields, cities


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
                entry[english_key] = cell_value
            else:
                entry[f"col_{idx}"] = cell_value

        results.append(entry)

    return results


def fetch_prayer_times(
    from_date: str, to_date: str, city: str = "اربد", list_cities: bool = False
) -> list[dict]:
    """Main function: fetch and return parsed prayer times."""
    session = requests.Session()

    print("Initializing session and fetching viewstate...")
    hidden_fields, available_cities = get_session_and_viewstate(session)
    print(f"Session established. Cookies: {list(session.cookies.keys())}")

    if list_cities:
        print("\nAvailable cities/regions:")
        for c in available_cities:
            print(f"  - {c}")
        return []

    if available_cities and city not in available_cities:
        # Try partial match (e.g., "عمان" matches "عمان، البلقاء، الزرقاء، مادبا")
        matches = [c for c in available_cities if city in c]
        if len(matches) == 1:
            print(f"City '{city}' matched to '{matches[0]}'")
            city = matches[0]
        elif len(matches) > 1:
            print(f"Error: '{city}' matches multiple regions:", file=sys.stderr)
            for m in matches:
                print(f"  - {m}", file=sys.stderr)
            print("Please specify the full region name.", file=sys.stderr)
            sys.exit(1)
        else:
            print(f"Error: City '{city}' not found. Use --list-cities to see options.",
                  file=sys.stderr)
            sys.exit(1)

    post_data = build_post_data(hidden_fields, city, from_date, to_date)

    headers = {**HEADERS, **AJAX_HEADERS}
    print(f"Fetching prayer times for {city} from {from_date} to {to_date}...")
    resp = session.post(BASE_URL, data=post_data, headers=headers)
    resp.raise_for_status()

    html_content = extract_html_from_ajax(resp.text)
    prayer_times = parse_prayer_times(html_content)

    if not prayer_times:
        print("Warning: No prayer times found in response. The page structure may have changed.",
              file=sys.stderr)
        print("Response preview (first 2000 chars):", file=sys.stderr)
        print(resp.text[:2000], file=sys.stderr)

    return prayer_times


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
        default="prayer_times.json",
        help="Output JSON file path (default: prayer_times.json)",
    )
    parser.add_argument(
        "--list-cities",
        action="store_true",
        help="List available cities/regions and exit",
    )

    args = parser.parse_args()

    prayer_times = fetch_prayer_times(
        args.from_date, args.to_date, args.city, args.list_cities
    )

    if args.list_cities:
        return

    output = {
        "city": args.city,
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
