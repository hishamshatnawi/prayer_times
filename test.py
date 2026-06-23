import appdaemon.plugins.hass.hassapi as hass
import json
from datetime import date, timedelta
import requests
from bs4 import BeautifulSoup
import re

BASE_URL = "https://www.awqaf.gov.jo/ar/Pages/PrayerTime"

class PrayerTimes(hass.Hass):

    def initialize(self):
        self.log("PrayerTimes app started")

        # Run once at startup
        self.run_in(self.update_prayer_times, 5)

        # Run every day at 00:05
        self.run_daily(self.update_prayer_times, "00:05:00")

    def update_prayer_times(self, kwargs):
        try:
            today = date.today()
            from_date = today.strftime("%Y/%m/%d")
            to_date = (today + timedelta(days=7)).strftime("%Y/%m/%d")
            city = "اربد"

            data = self.fetch_prayer_times(from_date, to_date, city)

            output = {
                "city": city,
                "from_date": from_date,
                "to_date": to_date,
                "count": len(data),
                "prayer_times": data,
            }

            path = "/config/appdaemon/apps/prayer_times.json"

            with open(path, "w", encoding="utf-8") as f:
                json.dump(output, f, ensure_ascii=False, indent=2)

            self.log(f"Saved {len(data)} entries to {path}")

        except Exception as e:
            self.log(f"Error: {e}", level="ERROR")

    def fetch_prayer_times(self, from_date, to_date, city):
        session = requests.Session()

        resp = session.get(BASE_URL)
        soup = BeautifulSoup(resp.text, "html.parser")

        fields = {}
        for name in ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION"):
            tag = soup.find("input", {"name": name})
            fields[name] = tag["value"] if tag else ""

        data = dict(fields)
        data["ctl00$ScriptManagerSiteMaster"] = (
            "ctl00$MainContent$UpdatePanel1|ctl00$MainContent$btn_search"
        )
        data["ctl00$MainContent$DropCompany"] = city
        data["ctl00$MainContent$txtFromDate"] = from_date
        data["ctl00$MainContent$txtToDate"] = to_date
        data["ctl00$MainContent$btn_search"] = "ابحث"
        data["__ASYNCPOST"] = "true"

        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
        }

        resp = session.post(BASE_URL, data=data, headers=headers)

        html = resp.text
        soup = BeautifulSoup(html, "html.parser")

        table = soup.find("table")
        if not table:
            return []

        rows = table.find_all("tr")
        headers = [th.get_text(strip=True) for th in rows[0].find_all(["th", "td"])]

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
            if not cells:
                continue

            entry = {}
            for i, val in enumerate(cells):
                key = column_map.get(headers[i], headers[i])
                entry[key] = val.replace("ص", "").replace("م", "").strip()

            results.append(entry)

        return results
