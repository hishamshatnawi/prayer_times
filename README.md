# Prayer Times (Jordan Awqaf)

Fetch daily prayer times from the [Jordanian Awqaf website](https://www.awqaf.gov.jo/ar/Pages/PrayerTime) and schedule Home Assistant scenes via [AppDaemon](https://appdaemon.readthedocs.io/).

## Contents

| File | Purpose |
|------|---------|
| `prayer_times_homeassistant_used.py` | **AppDaemon app** — fetch, cache JSON, schedule `scene_on` / `scene_off` windows |
| `prayer_times_homeassistant.py` | Shared fetch/parse logic (library-style) |
| `prayer_times.py` | Standalone CLI to fetch prayer times and save JSON |
| `apps.yaml` | Example AppDaemon configuration |
| `test.py` | Early AppDaemon prototype |

## Requirements

```bash
pip install -r requirements.txt
```

For the AppDaemon app, install the same dependencies in your Home Assistant AppDaemon environment.

## Standalone CLI

```bash
python prayer_times.py --city "اربد" --days 7 -o prayer_times.json
```

## Home Assistant / AppDaemon

1. Copy `prayer_times_homeassistant_used.py` to `/config/appdaemon/apps/`.
2. Add the `prayer_times` block from `apps.yaml` to `/config/appdaemon/apps/apps.yaml`.
3. Create Home Assistant scenes `scene.prayer_on` and `scene.prayer_off` (or change the names in config).
4. Restart AppDaemon.

See the module docstring in `prayer_times_homeassistant_used.py` for all configuration options (`test_mode`, `pre_fajr_minutes`, `schedule_from_file_only`, etc.).

## License

MIT
