"""AppDaemon app: fetch Jordan Awqaf prayer times (aligned with prayer_times_cli.py).

Configure in ``/config/appdaemon/apps/apps.yaml`` (UTF-8)::

    prayer_times:
      module: prayer_times
      class: PrayerTimes
      city: "اربد"
      scene_on: scene.prayer_on
      scene_off: scene.prayer_off
      pre_fajr_minutes: 15
      before_minutes: 2
      after_minutes: 5
      fajr_after_minutes: 6
      test_mode: false
      test_scene_on_in_seconds: 30
      test_scene_off_after_on_seconds: 10
      schedule_from_file_only: false

``city``: Arabic label, numeric DropCompany id (e.g. ``2`` for Irbid), or a substring of the region name.

``scene_on`` / ``scene_off``: Home Assistant scenes to run at the start and end of each prayer
window. Both are activated with ``scene.turn_on`` (``call_service("scene/turn_on", ...)``); the
``scene_off`` scene should capture your “switch off” device state. Defaults: ``scene.prayer_on``,
``scene.prayer_off``. Set either to empty string to skip scheduling.

Default prayer columns: ``fajr``, ``dhuhr``, ``asr``, ``maghrib``, ``isha`` (not ``sunrise``).
Optional ``prayer_keys`` list overrides which columns drive the schedule.

If the live fetch fails or returns no rows, the app reuses ``/config/appdaemon/apps/prayer_times.json``
when it still contains valid data.

``schedule_from_file_only`` (default ``false``): skip Awqaf fetch; load ONLY from ``prayer_times.json``
and reschedule (file is never overwritten). Use for hand-edited JSON tests; disable for production.

``test_mode`` (default ``false``): on the **startup** run only, schedule one-shot ``scene_on``
then ``scene_off``. ``test_scene_on_in_seconds`` (default ``90``); ``test_scene_off_after_on_seconds``
(default ``10``) is seconds after ``scene_on`` until ``scene_off``. Turn ``test_mode`` off after verifying.

``after_minutes`` (default ``5``): minutes after each non-Fajr anchor (and pre-Fajr) until
``scene_off``. ``fajr_after_minutes`` (default ``6``): same for Fajr only. ``before_minutes``
is shared by all windows.

``pre_fajr_minutes`` (default ``0``): when positive, adds an extra window anchored at Fajr minus
that many minutes (same ``before_minutes`` / ``after_minutes`` as other non-Fajr prayers). Use
``15`` for a typical tahajjud / imsak-style lead; ``0`` disables.

If you omit ``city``, default is Irbid via Unicode escapes (ASCII-safe ``.py`` file).
"""

import json
import os
import re
import unicodedata
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import appdaemon.plugins.hass.hassapi as hass
import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.awqaf.gov.jo/ar/Pages/PrayerTime"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:148.0) Gecko/20100101 Firefox/148.0",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9,ar-JO;q=0.8",
    "Accept-Encoding": "gzip, deflate",
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

PRAYER_TIMES_PATH = "/config/appdaemon/apps/prayer_times.json"

DEFAULT_PRAYER_KEYS = ["fajr", "dhuhr", "asr", "maghrib", "isha"]

PRAYER_CLOCK_KEYS = frozenset(
    {"fajr", "sunrise", "dhuhr", "asr", "maghrib", "isha"}
)

# Cells often omit ``مساء``; afternoon salawat shown as ``4:15`` meant as PM.
_AFTERNOON_SALAH_NO_MARKER_PM = frozenset({"asr", "maghrib", "isha"})

PRAYER_LOG_LABELS = {
    "fajr": "Fajr (الفجر)",
    "sunrise": "Sunrise (الشروق)",
    "dhuhr": "Dhuhr (الظهر)",
    "asr": "Asr (العصر)",
    "maghrib": "Maghrib (المغرب)",
    "isha": "Isha (العشاء)",
}

_PRAYER_CLOCK_CELL_RE = re.compile(r"(\d{1,2}):(\d{2})")

# ``ctl00$MainContent$DropCompany`` — ASP.NET ``name``/``id`` vary; match substring.


def drop_company_post_string(raw_value, label_text):
    """Option ``value`` to POST for ``DropCompany`` (what a real browser sends).

    Awqaf uses numeric ``<option value="2">اربد</option>``; ASP.NET
    ``EnableEventValidation`` rejects posted values that are not registered
    option values — posting Arabic text fails with “Invalid postback…”.
    """
    v = (raw_value or "").strip()
    t = (label_text or "").strip()
    if t == "الرجاء الاختيار" or v == "الرجاء الاختيار":
        return None
    if not v and not t:
        return None
    if v == "0" and "اختيار" in t:
        return None
    return v or t


def _collect_options_from_select(select):
    out = []
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


def find_drop_company_select(soup):
    sel = soup.find("select", id=re.compile(r"DropCompany", re.I))
    if sel:
        return sel
    for candidate in soup.find_all("select"):
        nm = candidate.get("name") or ""
        idi = candidate.get("id") or ""
        if "DropCompany" in nm or "DropCompany" in idi:
            return candidate
    return None


def region_options_from_html(html_text):
    """Extract region tuples when the server includes the dropdown in HTML."""
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
        region_options = _collect_options_from_select(
            BeautifulSoup(block.group(0), "html.parser").find("select")
        )
    return region_options


def get_session_and_viewstate(session):
    """GET the page for viewstate, validation, cookies, and city list."""
    resp = session.get(
        BASE_URL,
        headers={
            "User-Agent": HEADERS["User-Agent"],
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": HEADERS["Accept-Language"],
        },
    )
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    fields = {}
    for name in (
        "__VIEWSTATE",
        "__VIEWSTATEGENERATOR",
        "__EVENTVALIDATION",
        "__EVENTTARGET",
        "__EVENTARGUMENT",
    ):
        tag = soup.find("input", {"name": name})
        fields[name] = tag["value"] if tag else ""

    for inp in soup.find_all("input", {"type": "hidden"}):
        name = inp.get("name", "")
        if name and name not in fields:
            fields[name] = inp.get("value", "")

    # (post_field, label, raw_value_attr) for each region option
    region_options = region_options_from_html(resp.text)

    return fields, region_options


def build_post_data(hidden_fields, city, from_date, to_date):
    data = dict(hidden_fields)
    data["ctl00$ScriptManagerSiteMaster"] = (
        "ctl00$MainContent$UpdatePanel1|ctl00$MainContent$btn_search"
    )
    data["ctl00$MainContent$DropCompany"] = city
    data["ctl00$MainContent$txtFromDate"] = from_date
    data["ctl00$MainContent$txtToDate"] = to_date
    data["ctl00$MainContent$btn_search"] = "ابحث"
    data["__ASYNCPOST"] = "true"
    data.setdefault("ctl00$txtSearch", "")
    data.setdefault("ctl00$txtSearch1", "")
    return data


def extract_html_from_ajax(response_text):
    """Pull UpdatePanel HTML from ASP.NET AJAX pipe response."""
    parts = response_text.split("|")
    i = 0
    while i < len(parts) - 3:
        try:
            int(parts[i])
        except ValueError:
            i += 1
            continue
        part_type = parts[i + 1]
        content = parts[i + 3]
        if part_type == "updatePanel":
            return content
        i += 4
    return response_text


def normalize_awqaf_prayer_clock(raw, english_key=None):
    """Return 24-hour ``H:MM`` from an Awqaf table time cell.

    Marker-based: ``صباح`` / ``مساء``, or lone trailing ``ص`` / ``م``.
    Examples: ``7:13 مساءً`` → ``19:13``; ``4:40 صباحاً`` → ``4:40``.

    Without markers: ``asr`` / ``maghrib`` / ``isha`` hours 1–11 get ``+12`` (site omits
    ``مساء``). ``dhuhr``: hour ``1`` → ``+12`` (1 PM → 13:MM); ``11`` / ``12`` unchanged
    (morning/noon corridor). Hours 13+ are never shifted. ``fajr`` / ``sunrise``: no such
    heuristic.
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


def parse_prayer_times(html):
    soup = BeautifulSoup(html, "html.parser")
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

    header_row = rows[0]
    headers_txt = [
        th.get_text(strip=True) for th in header_row.find_all(["th", "td"])
    ]

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
            if idx < len(headers_txt):
                arabic_header = headers_txt[idx]
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


def _yaml_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return bool(value)


class PrayerTimes(hass.Hass):

    def initialize(self):
        # Default \u0627\u0631\u0628\u062f = اربد (Irbid); override with ``city:`` in apps.yaml (UTF-8).
        self.city = (self.args.get("city") or None) or "\u0627\u0631\u0628\u062f"
        self.scene_on = (self.args.get("scene_on") or "scene.prayer_on").strip()
        self.scene_off = (self.args.get("scene_off") or "scene.prayer_off").strip()
        self.before_minutes = int(self.args.get("before_minutes", 2))
        self.after_minutes = int(self.args.get("after_minutes", 5))
        self.fajr_after_minutes = int(self.args.get("fajr_after_minutes", 6))
        pk = self.args.get("prayer_keys")
        self.prayer_keys = pk if isinstance(pk, list) else list(DEFAULT_PRAYER_KEYS)
        self._switch_handles = []
        self.schedule_from_file_only = _yaml_bool(self.args.get("schedule_from_file_only"))
        self.test_mode = _yaml_bool(self.args.get("test_mode"))
        self.test_scene_on_in_seconds = int(self.args.get("test_scene_on_in_seconds", 90))
        self.test_scene_off_after_on_seconds = int(
            self.args.get("test_scene_off_after_on_seconds", 10)
        )
        self.pre_fajr_minutes = int(self.args.get("pre_fajr_minutes", 0))
        self.log("PrayerTimes app started")
        self.run_in(self.update_prayer_times, 5, trigger="startup")
        self.run_daily(self.update_prayer_times, "00:05:00")

    def _after_minutes_for(self, prayer_key):
        """Minutes after the anchor until ``scene_off``; Fajr can differ from other prayers."""
        if prayer_key == "fajr":
            return self.fajr_after_minutes
        return self.after_minutes

    def update_prayer_times(self, kwargs=None):
        if kwargs is None:
            kwargs = {}
        try:
            today = date.today()
            from_date = today.strftime("%Y/%m/%d")
            to_date = (today + timedelta(days=7)).strftime("%Y/%m/%d")

            if self.schedule_from_file_only:
                self.log(
                    "schedule_from_file_only: skipping Awqaf fetch; using local JSON only.",
                    level="WARNING",
                )
                cached = self._load_cached_prayer_file()
                if not cached or not cached.get("prayer_times"):
                    self.log(
                        f"No usable data in {PRAYER_TIMES_PATH} (file-only mode).",
                        level="ERROR",
                    )
                    return
                prayer_data = cached["prayer_times"]
            else:
                fresh = None
                region_used = self.city
                try:
                    fresh, region_used = self.fetch_prayer_times(
                        from_date, to_date, self.city
                    )
                except Exception as e:
                    self.log(f"Fetch failed: {e}", level="WARNING")
                    fresh = None

                if fresh:
                    output = {
                        "city": region_used,
                        "from_date": from_date,
                        "to_date": to_date,
                        "count": len(fresh),
                        "prayer_times": fresh,
                    }
                    os.makedirs(os.path.dirname(PRAYER_TIMES_PATH), exist_ok=True)
                    with open(PRAYER_TIMES_PATH, "w", encoding="utf-8") as f:
                        json.dump(output, f, ensure_ascii=False, indent=2)
                    self.log(f"Saved {len(fresh)} entries to {PRAYER_TIMES_PATH}")
                    prayer_data = fresh
                else:
                    cached = self._load_cached_prayer_file()
                    if not cached or not cached.get("prayer_times"):
                        self.log(
                            "No prayer times available (fetch failed or empty, "
                            "and no usable cached file).",
                            level="ERROR",
                        )
                        return
                    self.log(
                        "Using cached prayer times from "
                        f"{PRAYER_TIMES_PATH} (fetch failed or returned no rows).",
                        level="WARNING",
                    )
                    prayer_data = cached["prayer_times"]

            self._schedule_switch_jobs(prayer_data)

            if self.test_mode and kwargs.get("trigger") == "startup":
                self._schedule_test_mode_once()

        except Exception as e:
            self.log(f"Error: {e}", level="ERROR")

    def _load_cached_prayer_file(self):
        try:
            with open(PRAYER_TIMES_PATH, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return None
        rows = data.get("prayer_times")
        if not isinstance(rows, list) or len(rows) == 0:
            return None
        return data

    def _resolve_tz(self):
        try:
            name = self.get_timezone()
            if name:
                return ZoneInfo(str(name))
        except (TypeError, ValueError, KeyError, OSError):
            pass
        return ZoneInfo("Asia/Amman")

    @staticmethod
    def _parse_hhmm(text):
        m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", (text or "").strip())
        if not m:
            return None
        return int(m.group(1)), int(m.group(2))

    def _combine_prayer_datetime(self, date_str, time_str, tz):
        """``date_str`` is DD/MM/YYYY from the scraper; ``time_str`` is HH:MM."""
        parts = (date_str or "").strip().split("/")
        if len(parts) != 3:
            return None
        try:
            d, mon, y = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            return None
        hm = self._parse_hhmm(time_str)
        if hm is None:
            return None
        h, mi = hm
        return datetime(y, mon, d, h, mi, 0, 0, tzinfo=tz)

    def _cancel_switch_schedules(self):
        for h in self._switch_handles:
            try:
                self.cancel_timer(h)
            except (TypeError, ValueError):
                pass
        self._switch_handles = []

    def _schedule_scene_window(self, on_dt, off_dt, now, prayer_log_label, warn_label=None):
        """Schedule ``scene_on`` / ``scene_off`` for one anchor window; return count of ``run_at`` timers added."""
        w = warn_label if warn_label is not None else str(prayer_log_label)
        n = 0
        if off_dt <= now:
            return 0

        if on_dt <= now < off_dt:
            self.log(
                f"Prayer trigger: {prayer_log_label} — scene_on ({self.scene_on}) "
                f"(inside prayer window; immediate)",
                level="INFO",
            )
            self.call_service("scene/turn_on", entity_id=self.scene_on)
        elif on_dt > now:
            try:
                h_on = self.run_at(
                    self._prayer_scene_on,
                    on_dt,
                    prayer_name=prayer_log_label,
                    phase="scene_on",
                )
                self._switch_handles.append(h_on)
                n += 1
            except Exception as e:
                self.log(
                    f"Could not schedule ON for {w} at {on_dt}: {e}",
                    level="WARNING",
                )

        if off_dt > now:
            try:
                h_off = self.run_at(
                    self._prayer_scene_off,
                    off_dt,
                    prayer_name=prayer_log_label,
                    phase="scene_off",
                )
                self._switch_handles.append(h_off)
                n += 1
            except Exception as e:
                self.log(
                    f"Could not schedule OFF for {w} at {off_dt}: {e}",
                    level="WARNING",
                )
        return n

    def _schedule_switch_jobs(self, prayer_times_list):
        self._cancel_switch_schedules()
        if not self.scene_on or not self.scene_off:
            return

        tz = self._resolve_tz()
        now = datetime.now(tz)
        scheduled = 0

        for row in prayer_times_list:
            date_str = row.get("date", "")
            for key in self.prayer_keys:
                raw_t = row.get(key)
                if not raw_t:
                    continue
                t_s = str(raw_t).strip()
                prayer_dt = self._combine_prayer_datetime(date_str, t_s, tz)
                if prayer_dt is None:
                    continue

                after = self._after_minutes_for(key)
                on_dt = prayer_dt - timedelta(minutes=self.before_minutes)
                off_dt = prayer_dt + timedelta(minutes=after)
                lbl = PRAYER_LOG_LABELS.get(key, key)
                scheduled += self._schedule_scene_window(
                    on_dt, off_dt, now, lbl, warn_label=key
                )

            if self.pre_fajr_minutes > 0:
                raw_f = row.get("fajr")
                if raw_f:
                    t_f = normalize_awqaf_prayer_clock(str(raw_f).strip(), "fajr")
                    fajr_dt = self._combine_prayer_datetime(date_str, t_f, tz)
                    if fajr_dt is not None:
                        anchor = fajr_dt - timedelta(
                            minutes=self.pre_fajr_minutes
                        )
                        on_pre = anchor - timedelta(
                            minutes=self.before_minutes
                        )
                        off_pre = anchor + timedelta(
                            minutes=self.after_minutes
                        )
                        pl_pre = (
                            f"Pre-Fajr ({self.pre_fajr_minutes} min before Fajr)"
                        )
                        scheduled += self._schedule_scene_window(
                            on_pre,
                            off_pre,
                            now,
                            pl_pre,
                            warn_label="pre_fajr",
                        )

        extra = (
            f"; pre_fajr anchor {self.pre_fajr_minutes} min before Fajr"
            if self.pre_fajr_minutes > 0
            else ""
        )
        self.log(
            f"Scenes {self.scene_on!r} / {self.scene_off!r}: {scheduled} timer(s) for "
            f"{self.before_minutes} min before / "
            f"{self.after_minutes} min after (Fajr {self.fajr_after_minutes} min) "
            f"({', '.join(self.prayer_keys)}){extra}",
        )

    def _prayer_scene_on(self, kwargs):
        pname = kwargs.get("prayer_name", "unknown")
        self.log(
            f"Prayer trigger: {pname} — scene_on ({self.scene_on}) "
            f"(start of prayer window)",
            level="INFO",
        )
        self.call_service("scene/turn_on", entity_id=self.scene_on)

    def _prayer_scene_off(self, kwargs):
        pname = kwargs.get("prayer_name", "unknown")
        self.log(
            f"Prayer trigger: {pname} — scene_off ({self.scene_off}) "
            f"(end of prayer window)",
            level="INFO",
        )
        self.call_service("scene/turn_on", entity_id=self.scene_off)

    def _schedule_test_mode_once(self):
        if not self.scene_on or not self.scene_off:
            self.log("test_mode: scene_on/scene_off not set; skipping test timers.", level="WARNING")
            return

        tz = self._resolve_tz()
        base = datetime.now(tz)
        on_dt = base + timedelta(seconds=self.test_scene_on_in_seconds)
        off_dt = on_dt + timedelta(seconds=self.test_scene_off_after_on_seconds)

        self.log(
            f"test_mode (startup): scene_on at {on_dt.isoformat(timespec='seconds')}, "
            f"scene_off {self.test_scene_off_after_on_seconds}s later — disable test_mode "
            "after verifying.",
            level="WARNING",
        )

        try:
            h_on = self.run_at(
                self._prayer_scene_on,
                on_dt,
                prayer_name="Test mode",
                phase="scene_on",
            )
            self._switch_handles.append(h_on)
        except Exception as e:
            self.log(f"test_mode: could not schedule scene_on: {e}", level="WARNING")

        try:
            h_off = self.run_at(
                self._prayer_scene_off,
                off_dt,
                prayer_name="Test mode",
                phase="scene_off",
            )
            self._switch_handles.append(h_off)
        except Exception as e:
            self.log(f"test_mode: could not schedule scene_off: {e}", level="WARNING")

    def fetch_prayer_times(self, from_date, to_date, city):
        session = requests.Session()
        hidden_fields, region_options = get_session_and_viewstate(session)

        post_company, display_name = self._resolve_drop_company(city, region_options)
        if post_company is None:
            return [], city

        post_data = build_post_data(hidden_fields, post_company, from_date, to_date)
        merged_headers = {**HEADERS, **AJAX_HEADERS}
        resp = session.post(BASE_URL, data=post_data, headers=merged_headers)
        resp.raise_for_status()

        html_content = extract_html_from_ajax(resp.text)
        prayer_times = parse_prayer_times(html_content)

        if not prayer_times:
            preview = resp.text[:2000].replace("\n", " ")
            self.log(f"No prayer times parsed. Response preview: {preview}", level="WARNING")

        return prayer_times, display_name

    def _resolve_drop_company(self, city, region_options):
        """Map user ``city`` to DropCompany POST string (Awqaf-compatible)."""
        if not region_options:
            self.log(
                "Could not scrape DropCompany options. If search fails, set ``city`` to the "
                "numeric option id from the website (e.g. ``2`` for Irbid), not Arabic text.",
                level="WARNING",
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
            post, label = matches[0]
            self.log(f"City {city!r} matched to {label!r} (POST {post!r})")
            return post, label
        if len(matches) > 1:
            self.log(
                f"City {city!r} is ambiguous. Candidates (first 5): "
                f"{[m[1] for m in matches[:5]]!r}",
                level="ERROR",
            )
            return None, city

        sample = [(p, lbl) for p, lbl, _ in region_options[:5]]
        self.log(
            f"City {city!r} not found. Option count={len(region_options)}; "
            f"sample (post, label)={sample!r}",
            level="ERROR",
        )
        return None, city
