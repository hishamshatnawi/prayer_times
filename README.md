# Prayer Times (Jordan Awqaf)

Fetch daily prayer times from the [Jordanian Awqaf website](https://www.awqaf.gov.jo/ar/Pages/PrayerTime) and schedule Home Assistant scenes via [AppDaemon](https://appdaemon.readthedocs.io/).

## Files

| File | Purpose |
|------|---------|
| `prayer_times.py` | **AppDaemon app** — fetch, cache JSON, schedule `scene_on` / `scene_off` windows |
| `prayer_times_cli.py` | **CLI** — same fetch/parse logic as the app; for manual runs and testing |
| `prayer_times_legacy.py` | Legacy CLI (older scraper; missing DropCompany fixes and 24h time normalization) |
| `apps.yaml` | Example AppDaemon configuration |
| `test.py` | Early AppDaemon prototype |

## CLI vs AppDaemon app

Both `prayer_times_cli.py` and `prayer_times.py` share the same Awqaf scraping core: session/viewstate handling, DropCompany city resolution, HTML parsing, and 24-hour prayer-time normalization. They produce the same JSON shape (`city`, `from_date`, `to_date`, `count`, `prayer_times`).

| | `prayer_times_cli.py` | `prayer_times.py` |
|---|---|---|
| **Runs in** | Your terminal / dev machine | Home Assistant via AppDaemon |
| **Entry point** | `python prayer_times_cli.py …` | `apps.yaml` → `module: prayer_times` |
| **Primary job** | Fetch times and write JSON | Fetch times, cache JSON, **schedule HA scenes** |
| **Scene automation** | No | Yes — `scene_on` / `scene_off` around each prayer |
| **Scheduling** | One-shot when you run it | Startup + daily at 00:05 |
| **Config** | CLI flags (`--city`, `--output`, …) | `apps.yaml` (`before_minutes`, `test_mode`, …) |
| **On fetch failure** | Prints warning; may write empty JSON | Falls back to cached `prayer_times.json` |
| **Extra features** | `--list-cities` | `pre_fajr_minutes`, `test_mode`, `schedule_from_file_only`, `prayer_keys` |

**Use the CLI** to list regions, debug scraping, or generate JSON offline.

**Use the AppDaemon app** in production on Home Assistant to keep times updated and trigger scenes automatically.

## Requirements

```bash
pip install -r requirements.txt
```

For the AppDaemon app, install the same dependencies in your Home Assistant AppDaemon environment.

## Standalone CLI

```bash
python prayer_times_cli.py --list-cities
python prayer_times_cli.py --city "اربد" --output prayer_times.json
python prayer_times_cli.py --city 2 --from-date 2026/06/23 --to-date 2026/06/30 \
  --output /config/appdaemon/apps/prayer_times.json
```

Default output path matches the AppDaemon cache: `/config/appdaemon/apps/prayer_times.json`.

## Home Assistant / AppDaemon

1. Copy `prayer_times.py` to `/config/appdaemon/apps/`.
2. Add the `prayer_times` block from `apps.yaml` to `/config/appdaemon/apps/apps.yaml`.
3. Create Home Assistant scenes `scene.prayer_on` and `scene.prayer_off` (or change the names in config).
4. Restart AppDaemon.

See the module docstring in `prayer_times.py` for all configuration options (`test_mode`, `pre_fajr_minutes`, `schedule_from_file_only`, etc.).

## License

MIT
