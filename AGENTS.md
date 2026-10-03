# AGENTS.md

Instructions for coding agents in this repository. Facts below were checked against the code and the installed unit `/etc/systemd/system/matesla-gunicorn.service`. If something here disagrees with `matesla/capture.py` or `mysite/settings.py`, trust those files. The README spacing table lags the code (sentry, dog/camp, habit classes).

## What this project is

matesla is a personal Django site (project package `mysite`, settings module `mysite.settings`) that reads a household’s cars through the Tesla Fleet API. It shows live status, minute-level history, day maps, drives, charge curves, and personal stats.

It does not send vehicle commands (lock, climate, charge start, `wake_up`). Those were removed; the official Tesla app covers remote control, and Vehicle Command Protocol is out of scope. `matesla/TeslaConnect.py` still has a comment that commands are “only on explicit user action”; there is no command route. Do not add `wake_up` or command endpoints.

Fleet API is pay-per-use. This install stays inside the free credit. Auth and token refresh are not billed. `vehicle_data` and `/vehicles` are billed. Call them only where the code already does.

## Layout

Django apps in `INSTALLED_APPS`:

| App | Role |
|-----|------|
| `matesla` | Fleet client, capture, snapshots, OAuth, charge-cost math, geocoding |
| `personalstats` | Stats, day map, drives, DC curves, charge-cost pages |
| `accounts` | Signup (`accounts/urls.py` → `signup/`) |
| `carimage` | Cached Tesla paint/wheel image (`carimage/urls.py`, name `CarImageFromTesla`) |

Files that matter:

- `matesla/TeslaConnect.py` — Fleet HTTP, `GetVehicles`, `ParamsConnectedTesla` (status page `vehicle_data`). Endpoints string `VEHICLE_DATA_ENDPOINTS` must keep `location_data` or GPS is often missing.
- `matesla/TeslaOAuth.py`, `matesla/TeslaPartner.py`, `matesla/TeslaState.py` — token refresh, partner register, parsed state.
- `matesla/capture.py` — adaptive poll policy and `/internal/capture`. Source of truth for intervals.
- `matesla/poll_habits.py`, `matesla/poll_diagnostics.py` — idle habit classes and the Polling details page.
- `matesla/charge_cost.py`, `matesla/elia_dayahead.py`, `matesla/tesla_charging_history.py` — €/kWh rules, Elia day-ahead, Supercharger invoices.
- `matesla/geo_enrich.py`, `matesla/models/AddressFromLatLong.py` — elevation and reverse-geocode cache.
- `matesla/models/` — `TeslaToken`, `TeslaCarDataSnapshot`, `TeslaCarInfo`, `ChargeCost`, `FleetApiCall`, `VinHash`.
- `matesla/views.py`, `matesla/urls.py` — HTML views. Landing `home` redirects to the day map and must stay database-only.
- `personalstats/urls.py`, `personalstats/views.py`, `personalstats/charge_pages.py` — stats routes. `ChargeCosts` / `ChargeCostsSetup` live in `charge_pages.py`.
- `mysite/urls.py` — root URLconf. Language-prefixed UI via `i18n_patterns`. Two paths are outside that prefix (see URLs).
- `mysite/settings.py` — `DEBUG`, hosts, CSRF, languages, SQLite, Fleet and geocoder env.
- `mysite/writable_access.py`, `mysite/middleware.py` (`ReadOnlyRemoteMiddleware`) — Tailscale read-only.
- `mysite/wsgi.py` — `mysite.wsgi:application` (gunicorn target).
- `matesla/apps.py` — on connect, SQLite `journal_mode=WAL`, `busy_timeout=30000`.
- `matesla/sqlite_guard.py` — `heavy_snapshot_read()` serializes big snapshot scans on the threaded worker.
- `config/matesla-gunicorn.service.in` — unit template. Installed copy is the systemd path above.
- `scripts/install_capture_cron.sh`, `scripts/install_elia_cron.sh`, `scripts/tailscale-serve-matesla.sh`.

`TIME_ZONE` in settings is `UTC`. Capture, habits, charge cost, and day map use `Europe/Brussels` on purpose. Do not switch those clocks to UTC.

## How to run tests

Project virtualenv is `.venv`. Settings module is `mysite.settings` (`manage.py`).

```bash
cd /home/mehdi/PycharmProjects/matesla
.venv/bin/python manage.py test
```

One module:

```bash
.venv/bin/python manage.py test matesla.tests_capture_lock
.venv/bin/python manage.py test personalstats.tests
```

One class or method: `matesla.tests_osm_tiles.OsmTileSettingsTests` or the full `path.Class.method` label.

Django `TestCase` uses `DATABASES["default"]["TEST"]["NAME"]` = `test_matesla.sqlite3`, never `db.sqlite3`. `personalstats/test_factories.py` `assert_not_production_database()` aborts if the connection points at the live history database. Do not override the test database name to `db.sqlite3`. Do not import `test_factories` from capture or TeslaFi import.

`matesla.tests_i18n.TranslationCoverageTests` fails on empty or fuzzy `msgstr` in any non-English language, and expects `fr`, `es`, `de`, `nl`, `nb`.

## How the site runs

Installed unit `matesla-gunicorn.service` (user `mehdi`, working directory this repo, `EnvironmentFile=-…/matesla/.env`):

```text
.venv/bin/gunicorn mysite.wsgi:application --bind 127.0.0.1:8001 --reload --timeout 200 --workers 1 --threads 4
```

`Restart=no`.

SQLite is the database (`db.sqlite3` unless `DATABASE_URL` is set). Keep `--workers 1`. A second worker is a second writer and will lock the database. Threads exist so the UI can answer while capture waits on Fleet. Capture is serialized with `matesla.capture._capture_lock`. `sqlite_guard.heavy_snapshot_read` exists because several stats queries at once blow up on the ~500k-row snapshot table. Leave both locks in place.

Restart after `.env`, template, `.mo`, or static changes:

```bash
sudo systemctl restart matesla-gunicorn.service
```

`--reload` watches Python files only. `.env` is read when the process starts. With `DEBUG` off, Django caches templates, so HTML edits need that restart too. `DEBUG` off uses WhiteNoise `CompressedStaticFilesStorage` (not Manifest: Manifest breaks on `leaflet.js` `sourceMappingURL`). After static edits run `collectstatic --noinput`, then restart.

`runserver` only when the service is stopped. It binds the same port:

```bash
sudo systemctl stop matesla-gunicorn.service
.venv/bin/python manage.py runserver 127.0.0.1:8001
```

Because `Restart=no`, a stop stays down until `sudo systemctl start matesla-gunicorn.service` or the next boot. Start the service again when you are done so capture cron has something to hit. Do not run `runserver` on another port and retarget cron or Tailscale to match.

Do not run `manage.py TakeTeslaCarDataSnapshot` (or any other management command that writes snapshots) while gunicorn is up. That is a second process on the same SQLite file. Cron must stay a `curl` into the web process. A one-shot management command is fine only when the service is stopped.

## URLs and ports

- Local site: `http://127.0.0.1:8001/`
- This machine’s MagicDNS name, from `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS` in `mysite/settings.py`: `mehdi-thinkbook-13s-g2-itl.taila97662.ts.net`
- Tailscale HTTPS for matesla: `https://mehdi-thinkbook-13s-g2-itl.taila97662.ts.net:8443/` → `http://127.0.0.1:8001` (`scripts/tailscale-serve-matesla.sh`, default `MATESLA_TS_HTTPS_PORT=8443`)
- Language prefix on UI routes (`i18n_patterns`): `/en/`, `/fr/`, `/es/`, `/de/`, `/nl/`, `/nb/`. Example day map: `/en/personalstats/DayMap/<hashedVin>/`.

PicturesDjango owns port 8000 and Tailscale Serve on 443 on this machine. Never bind matesla there, never point `tailscale serve` at 443 or at port 8000, and never change PicturesDjango to free them. The README mentions 443 only as a generic alternative when it is free. It is not free here.

These two paths are outside `i18n_patterns` in `mysite/urls.py`. Do not add a language prefix:

- `http://127.0.0.1:8001/matesla/internal/capture` — cron capture, no auth, `csrf_exempt`. Localhost only. Do not expose port 8001 to the internet.
- `http://localhost:8001/oauth/callback` — Tesla OAuth redirect (`TESLA_REDIRECT_URI`). It must match the developer console exactly, including `localhost` and the missing language prefix.

`SECURE_SSL_REDIRECT` stays off unless `DJANGO_SECURE_SSL_REDIRECT=1`. A forced HTTPS redirect breaks the HTTP capture cron.

## Read-only Tailscale vs local HTTP

`ReadOnlyRemoteMiddleware` treats the request as writable only when the `Host` (without port) is in `WRITABLE_HOSTS`. Default from `MATESLA_WRITABLE_HOSTS` is `127.0.0.1,localhost`. The MagicDNS name is read-only.

On a read-only host:

- URLs not in `READONLY_ALLOWED_URL_NAMES` return 404, including admin, signup, and Tesla account / OAuth screens.
- POST/PUT/PATCH/DELETE return 403 unless the url name is in `READONLY_SAFE_POST_URL_NAMES` (`login`, `logout`, `select_vehicle`, `set_distance_unit`, password reset/change).
- Anonymous viewers are the household owner (`writable_access.get_site_owner_user`: `MATESLA_OWNER_USER_ID`, else `MATESLA_OWNER_USERNAME`, else the single `TeslaToken` user). Several token owners need `MATESLA_OWNER_USER_ID`.
- Local `http://127.0.0.1:8001` still requires login for setup. Anonymous local users are not silently mapped to the owner.

`personalstats` `ChargeCostsSetup` is not in the read-only allow list. Tariff edits stay on local HTTP. `PersoChargeCosts` is allowed remotely (read). A new mutating view must be added to the allow list only if remote users should reach it, and to the safe-POST list only if remote POST is intended. Default is 404.

## Capture, and not waking cars

Cron hits `GET` or `POST /matesla/internal/capture` every minute. Fleet is called only when a car is due. If none are due, there is no `/vehicles` list call. A capture already holding `_capture_lock` returns HTTP 200 with `skipped_already_running: true`. On `EXCEEDED_LIMIT` the run stops instead of retrying.

Never send Fleet `wake_up`. List state `asleep` skips `vehicle_data`, except the drive-seal window: if the latest sample is still driving and younger than `DRIVE_SEAL_MAX_AGE_MIN` (30), capture still tries `vehicle_data` once so the day map can place the real park point. List state `offline` is not sleep. Fleet “offline” is often wrong; capture still calls `vehicle_data`, and spacing treats it as `online_idle`.

Do not tighten intervals “to be fresher” without a reason. Current baseline in `capture.py` (minutes), civil clock `Europe/Brussels`, night 22:00 inclusive to 06:00 exclusive:

| Kind | Day | Night |
|------|-----|-------|
| Driving, ETA ≤ 3 min, or Supercharger-like destination with ETA ≤ 12 min | 1 | 1 |
| Driving, ETA ≤ 12, missing/non-positive ETA, or speed < 20 mph | 2 | 2 |
| Driving, ETA ≤ 40 / above 40 | 5 / 10 | 5 / 10 |
| DC (`INTERVAL_DC_CHARGE_MIN`) | 1 | 1 |
| AC wall | 15 | 30 |
| Cabin (user present or climate on, not dog/camp) | 2 | 30 |
| Dog or camp (`IsDogOrCampingModes`) | 15 | 30 |
| Sentry only | 10 | 30 |
| Online idle, or list `offline` | 5 | 30 |
| List `asleep` | 5 | 30 |

AC wall connectors `MCSingleWireCAN` and `ACSingleWireCAN` (also `ac`, `ac_single`, `ac_three`) are not DC. Classifying any non-empty `fast_charger_type` as DC polled cars every minute and burned Fleet credit. DC types are `combo`, `ccs`, `tesla`, `supercharger`, `chademo`, or `fast_charger_present`, with the power heuristic at `DC_POWER_KW_MIN` (12 kW).

A snapshot older than `ACTIVITY_SNAP_MAX_AGE_MIN` (20 minutes, and always above the 15 minute AC interval) must not choose the next interval. A stale “Charging” flag must not keep the AC cadence after the car sleeps. List `asleep` wins over that flag.

Long park with no drive and no charge raises an idle floor: ≥24 h → 10 min, ≥48 h → 15, ≥72 h → 30. Applied as `max(interval, floor)`.

`poll_habits.py` may replace the idle/asleep/sentry baseline for the current weekday+hour (not `max` with the baseline). It does not apply to `LIVE_ACTIVITY_KINDS` (`driving`, `dc_charge`, `ac_charge`, `cabin`, `dogcamp`). Sentry is idle for this purpose. Trusted model classes: `busy` 5 min, `busy_near` 10 min (±1 h around a busy core), `quiet` 30 min, `moderate` 15 min by day only (night moderate leaves the 30 min baseline). Window is about 12 weeks, at least 2 reference weeks, and a regime break (school / holidays / trip) drops trust. The README’s three-class table is stale; follow `poll_habits.py`. Diagnose with `python manage.py ShowPollHabits --force` or personal stats Polling details.

Each capture tick also runs `geo_enrich.geo_enrichment_tick` even when no car was due. That path must not call Fleet. After a Supercharge ends, capture may call charging-history for the invoice (`tesla_charging_history.py`). Day map reads the cache only. Do not HTTP-fetch `/api/1/dx/charging/history` from the day-map request.

`last_polled_at` is updated after a real poll attempt. Spacing uses that, not “every cron tick”.

## Charge cost

`matesla/charge_cost.py` prices a session in this order: Tesla Supercharger invoice, home geofence for that vehicle and local date, work geofence, Supercharger site rate, other-chargers rate, otherwise unpriced. Do not invent euros for the unpriced case.

Home dynamic price is Elia spot €/MWh / 1000 plus the surcharge in cents, integrated per MTU. Civil dates and night windows use `Europe/Brussels`. Elia is filled by the 20:00 cron `manage.py FetchEliaDayAhead` (`scripts/install_elia_cron.sh`), not by opening the Charges page. Cached days are skipped.

Place clusters in `personalstats/charge_pages.py` round GPS to 0.001° (~100 m) and then merge nearby cells so one driveway is one place.

## Maps and geocoding

Raster tiles must stay `https://tile.openstreetmap.org/{z}/{x}/{y}.png` (`OSM_TILE_URL`). No `{s}` letter subdomains. `matesla/tests_osm_tiles.py` fails if a template contains `{s}.tile.openstreetmap.org` or if `SECURE_REFERRER_POLICY` is not `strict-origin-when-cross-origin`. Django’s `same-origin` default strips `Referer` on the tile host; OSMF requires it. `no-referrer` is also wrong. Keep the English licence line in `mysite/context_processors.py` (`osm_tiles`): “© OpenStreetMap contributors”.

Reverse geocode: Geoapify when `GEOAPIFY_API_KEY` or `GEOAPIFY_KEY` is set, otherwise public Nominatim (`AddressFromLatLong.active_geocoder`). Cache is a ~11 m grid (4 decimal degrees) in `AddressFromLatLong`. Empty address is allowed (elevation-only rows). Daily caps and minimum intervals are in settings (`GEOAPIFY_*`, `NOMINATIM_*`). The quota table is still named `NominatimDailyQuota`; it counts Geoapify too. Backfill must use the backfill purpose so it does not eat the interactive budget. Do not reverse-geocode every drive sample; `geo_enrich` only fills high-value grids (parked / endpoints), default 1 address per tick on Nominatim and 5 on Geoapify. Prefer driveable roads over footways (`_CAR_ROAD_KEYS` vs `_PEDESTRIAN_KEYS`).

Changing the Geoapify key requires a gunicorn restart. Free-tier Geoapify needs the visible “Powered by Geoapify” footer (`geocoder_attribution`). Elevation comes from Open-Meteo, not Fleet.

Optional outbound proxy for Fleet only: `HTTPS_PROXY` via `matesla/GetProxyToUse.py`. Leave it unset on this machine unless Tesla blocks the egress IP.

## Settings, secrets, translations

`DEBUG` is on only when `DJANGO_DEBUG` is `1`, `true`, or `yes`. Do not hard-code `DEBUG = True`. The debug toolbar is installed only in that case. Default `SECRET_KEY` in settings is a local fallback; do not rotate it casually (`saltSeed` is mixed into legacy VIN hashes).

Never commit:

- `.env` (Tesla client id/secret, Geoapify, `DJANGO_SECRET_KEY`, SendGrid)
- `tesla_keys/` (`private-key.pem` and the partner public key)
- `db.sqlite3`, `*.sqlite3`, WAL/SHM sidecars, `db-backups/`

`.mo` files are intentionally not gitignored (see `.gitignore`). After editing strings, compile them and include the `.mo` updates.

Languages in `LANGUAGES`: `en` (source msgid), `fr`, `es`, `de`, `nl`, `nb`. New user-facing strings go through `gettext` / `{% trans %}` / `{% blocktrans %}`. Then:

```bash
.venv/bin/python manage.py makemessages -a --no-wrap
# fill empty msgstr in locale/*/LC_MESSAGES/django.po
.venv/bin/python manage.py check_translations
.venv/bin/python manage.py compilemessages
```

Then restart gunicorn. A new language means a `LANGUAGES` entry, a `locale/<code>/LC_MESSAGES/django.po`, compiled `.mo`, and the `tests_i18n` list. Ops log lines inside `capture.py` are French on purpose so `/tmp/matesla-capture.log` stays readable. Do not translate those as a drive-by.

## Naming and comments

Use full words in new Python names, URL names, and template variables. Avoid cryptic abbreviations. Short domain tokens that already exist are fine: `vin`, `soc`, `eta`, `kwh`, `dc`, `ac`, `osm`.

Comments in English, and only to say why a constraint exists (billing, SQLite, OSMF tile policy, stale Fleet flags). Do not comment what the next line obviously does.

## Git

Do not commit or push unless the user explicitly asked. The user wants the site checked in a browser (local `http://127.0.0.1:8001`, and the Tailscale host if the change is visible there) before a commit. Tailscale cannot save settings; use local HTTP for anything that writes.

Do not stage `.env`, `tesla_keys/`, or any sqlite file. Do not retarget ports 8000 or 443.
