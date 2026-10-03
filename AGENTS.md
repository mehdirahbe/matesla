# AGENTS.md

Instructions for coding agents and for humans opening a PR. Facts below were
checked against the code and the installed unit
`/etc/systemd/system/matesla-gunicorn.service`. If something here disagrees
with `matesla/capture.py`, `matesla/charge_cost.py`, or `mysite/settings.py`,
trust those files. The README spacing table lags the code (sentry, dog/camp,
habit classes).

This file is the durable “why”. Intervals, rule order, and constants live in
the modules named below — copy them from there, do not freeze a second copy
here that will rot.

## What this project is

matesla is a personal Django site (project package `mysite`, settings module
`mysite.settings`) that reads a household’s cars through the Tesla Fleet API.
It shows live status, minute-level history, day maps, drives, charge curves,
charge costs, and personal stats.

It is a **generic** app: models and pricing have no hard-coded VIN, address, or
tariff. The owner tags home/work and enters rates. Do not special-case a car
or a house in code.

It does not send vehicle commands (lock, climate, charge start, `wake_up`).
Those were removed; the official Tesla app covers remote control, and Vehicle
Command Protocol is out of scope. `matesla/TeslaConnect.py` still has a comment
that commands are “only on explicit user action”; there is no command route.
Do not add `wake_up` or command endpoints.

Fleet API is pay-per-use. This install stays inside the free credit. Auth and
token refresh are not billed. `vehicle_data` and `/vehicles` are billed. Call
them only where the code already does. Do not add Fleet Telemetry (streaming);
capture is snapshot polling on purpose.

## How to change this repo

- User-facing copy and git commits are **English**. Capture cron lines in
  `capture.py` stay **French** so `/tmp/matesla-capture.log` is readable.
- Do not start a new product feature unless the user asked for it.
- Do not commit or push unless the user explicitly asked.
- Prefer **404** over 500 or an empty 200 for a bad `hashedVin` URL.
- Honesty on money and distance: a missing value is blank (`—`), not a fake 0.
  True zero (parked, free work, campsite already charged that day) is 0.
- Day map is the **default landing tab**. It must stay a cheap first paint:
  local DB only for Elia and Tesla invoices. HTTP for those belongs to capture,
  the 20:00 Elia cron, or opening Charges.
- Never write production `db.sqlite3` except a schema migrate the user asked
  for. Tests use `test_matesla.sqlite3`.
- UI changes (layout, routing, client state, rendered data): verify in the
  browser at `http://127.0.0.1:8001` the way a user would. Tailscale is
  read-only for settings; use local HTTP to write.
- After templates, translations (`.mo`), or hashed static files: restart
  gunicorn yourself (`sudo systemctl restart matesla-gunicorn.service`).
  NOPASSWD on this machine is only that command and the PicturesDjango twin.

Before a PR, read the locked non-goals at the bottom. “Make DayMap fetch
prices”, “poll AC like DC”, “add `{s}` tile subdomains”, and “second gunicorn
worker” have all been tried or rejected.

## Layout

Django apps in `INSTALLED_APPS`:

| App | Role |
|-----|------|
| `matesla` | Fleet client, capture, snapshots, OAuth, charge-cost math, geocoding |
| `personalstats` | Stats, day map, Where, drives, DC curves, charge-cost pages |
| `accounts` | Signup (`accounts/urls.py` → `signup/`) |
| `carimage` | Cached Tesla paint/wheel image (`carimage/urls.py`, name `CarImageFromTesla`) |

Files that matter:

- `matesla/TeslaConnect.py` — Fleet HTTP, `GetVehicles`, `ParamsConnectedTesla` (status page `vehicle_data`). Endpoints string `VEHICLE_DATA_ENDPOINTS` must keep `location_data` or GPS is often missing.
- `matesla/TeslaOAuth.py`, `matesla/TeslaPartner.py`, `matesla/TeslaState.py` — token refresh, partner register, parsed state.
- `matesla/capture.py` — adaptive poll policy and `/internal/capture`. Source of truth for intervals.
- `matesla/poll_habits.py`, `matesla/poll_diagnostics.py` — idle habit classes and the Polling details page.
- `matesla/charge_cost.py`, `matesla/elia_dayahead.py`, `matesla/tesla_charging_history.py` — €/kWh rules, Elia day-ahead, Supercharger invoices.
- `matesla/place_search.py`, `personalstats/place_search.py` — Where-did-I-go (GPS bbox, not address ILIKE).
- `matesla/units.py` — miles in the DB, km/mi only at display (cookie `matesla_distance_unit`).
- `matesla/soc_refine.py`, `matesla/BatteryDegradation.py`, `matesla/epa_catalog.py` — raw SoC stays as stored; degradation is rule-of-three vs EPA.
- `matesla/graphstyle.py`, `matesla/degradation_graphs.py`, `personalstats/dc_charge.py`, `personalstats/stats_bundle.py` — PNG graphs, DC curves, Stats first paint.
- `matesla/geo_enrich.py`, `matesla/models/AddressFromLatLong.py` — elevation and reverse/forward-geocode cache.
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

`TIME_ZONE` in settings is `UTC`. Capture, habits, charge cost, day map, and
Where use `Europe/Brussels` on purpose. Do not switch those clocks to UTC.

Two different “night” windows exist on purpose. Capture quiet hours are 22:00
inclusive → 06:00 exclusive (Fleet spend). Tariff night defaults to 22:00–07:00
(Belgian retail night). Do not unify them as a drive-by.

## How to run tests

Project virtualenv is `.venv`. Settings module is `mysite.settings` (`manage.py`).

```bash
cd /home/mehdi/PycharmProjects/matesla
.venv/bin/python manage.py test --keepdb
```

`--keepdb` is the normal local flag: Django `TEST NAME` is
`test_matesla.sqlite3`, and recreating it every run is slow. It is never
`db.sqlite3`. `personalstats/test_factories.py`
`assert_not_production_database()` aborts if the connection points at the live
history database. Do not override the test database name to `db.sqlite3`. Do
not import `test_factories` from capture or TeslaFi import.

One module:

```bash
.venv/bin/python manage.py test matesla.tests_capture_lock --keepdb
.venv/bin/python manage.py test personalstats.tests --keepdb
```

One class or method: `matesla.tests_osm_tiles.OsmTileSettingsTests` or the
full `path.Class.method` label.

`matesla.tests_i18n.TranslationCoverageTests` fails on empty or fuzzy `msgstr`
in any non-English language, and expects `fr`, `es`, `de`, `nl`, `nb`.

Hashed-VIN URL matrix: `personalstats/tests.py` `PERSONAL_HASHED_VIN_ROUTES`
must stay in sync with `personalstats/urls.py`. A new `hashedVin` route that
is missing there will 200 on a typo instead of 404.

## How the site runs

Installed unit `matesla-gunicorn.service` (user `mehdi`, working directory this repo, `EnvironmentFile=-…/matesla/.env`):

```text
.venv/bin/gunicorn mysite.wsgi:application --bind 127.0.0.1:8001 --reload --timeout 200 --workers 1 --threads 4
```

`Restart=no`.

SQLite is the database (`db.sqlite3` unless `DATABASE_URL` is set). Keep
`--workers 1`. A second worker is a second writer and will lock the database.
Threads exist so the UI can answer while capture waits on Fleet. Capture is
serialized with `matesla.capture._capture_lock`. `sqlite_guard.heavy_snapshot_read`
exists because several stats queries at once blow up on the ~500k-row snapshot
table. Leave both locks in place.

PNG graphs use in-process `LocMemCache` (`GRAPH_PNG_CACHE_SECONDS` = 300 in
`personalstats/views.py`). One worker is also why that cache is shared. Matplotlib
is serialized with `graphstyle.exclusive_mpl` (`--threads 4` would otherwise
interleave Stats thumbs).

Restart after `.env`, template, `.mo`, or static changes:

```bash
sudo systemctl restart matesla-gunicorn.service
```

`--reload` watches Python files only. `.env` is read when the process starts.
With `DEBUG` off, Django caches templates, so HTML edits need that restart too.
`DEBUG` off uses WhiteNoise `CompressedStaticFilesStorage` (not Manifest:
Manifest breaks on `leaflet.js` `sourceMappingURL`). After static edits run
`collectstatic --noinput`, then restart.

`runserver` only when the service is stopped. It binds the same port:

```bash
sudo systemctl stop matesla-gunicorn.service
.venv/bin/python manage.py runserver 127.0.0.1:8001
```

Because `Restart=no`, a stop stays down until `sudo systemctl start
matesla-gunicorn.service` or the next boot. Start the service again when you
are done so capture cron has something to hit. Do not run `runserver` on
another port and retarget cron or Tailscale to match.

Do not run `manage.py TakeTeslaCarDataSnapshot` (or any other management
command that writes snapshots) while gunicorn is up. That is a second process
on the same SQLite file. Cron must stay a `curl` into the web process. A
one-shot management command is fine only when the service is stopped.
`FetchEliaDayAhead` is the exception that already runs from crontab against
the live DB; do not add a second writer beside it.

## URLs and ports

- Local site: `http://127.0.0.1:8001/`
- This machine’s MagicDNS name, from `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS` in `mysite/settings.py`: `mehdi-thinkbook-13s-g2-itl.taila97662.ts.net`
- Tailscale HTTPS for matesla: `https://mehdi-thinkbook-13s-g2-itl.taila97662.ts.net:8443/` → `http://127.0.0.1:8001` (`scripts/tailscale-serve-matesla.sh`, default `MATESLA_TS_HTTPS_PORT=8443`)
- Language prefix on UI routes (`i18n_patterns`): `/en/`, `/fr/`, `/es/`, `/de/`, `/nl/`, `/nb/`. Example day map: `/en/personalstats/DayMap/<hashedVin>/`.

PicturesDjango owns port 8000 and Tailscale Serve on 443 on this machine.
Never bind matesla there, never point `tailscale serve` at 443 or at port 8000,
and never change PicturesDjango to free them. The README mentions 443 only as
a generic alternative when it is free. It is not free here.

These two paths are outside `i18n_patterns` in `mysite/urls.py`. Do not add a
language prefix:

- `http://127.0.0.1:8001/matesla/internal/capture` — cron capture, no auth, `csrf_exempt`. Localhost only. Do not expose port 8001 to the internet.
- `http://localhost:8001/oauth/callback` — Tesla OAuth redirect (`TESLA_REDIRECT_URI`). It must match the developer console exactly, including `localhost` and the missing language prefix.

`SECURE_SSL_REDIRECT` stays off unless `DJANGO_SECURE_SSL_REDIRECT=1`. A forced
HTTPS redirect breaks the HTTP capture cron.

## Read-only Tailscale vs local HTTP

`ReadOnlyRemoteMiddleware` treats the request as writable only when the `Host`
(without port) is in `WRITABLE_HOSTS`. Default from `MATESLA_WRITABLE_HOSTS` is
`127.0.0.1,localhost`. The MagicDNS name is read-only.

On a read-only host:

- URLs not in `READONLY_ALLOWED_URL_NAMES` return 404, including admin, signup, and Tesla account / OAuth screens.
- POST/PUT/PATCH/DELETE return 403 unless the url name is in `READONLY_SAFE_POST_URL_NAMES` (`login`, `logout`, `select_vehicle`, `set_distance_unit`, password reset/change).
- Anonymous viewers are the household owner (`writable_access.get_site_owner_user`: `MATESLA_OWNER_USER_ID`, else `MATESLA_OWNER_USERNAME`, else the single `TeslaToken` user). Several token owners need `MATESLA_OWNER_USER_ID`.
- Local `http://127.0.0.1:8001` still requires login for setup. Anonymous local users are not silently mapped to the owner.

`personalstats` `ChargeCostsSetup` is not in the read-only allow list. Tariff
edits stay on local HTTP. `PersoChargeCosts` is allowed remotely (read). A new
mutating view must be added to the allow list only if remote users should reach
it, and to the safe-POST list only if remote POST is intended. Default is 404.

## hashedVin URLs

Public graph URLs never contain a raw VIN. `HashTheVin` is sha224(vin +
`SECRET_KEY`). Do not rotate `SECRET_KEY` casually (`saltSeed` is mixed into
legacy hashes).

`IsValidHash` is format-only (`[a-z0-9.]`). A one-character typo in a real
digest still passes. `IsKnownHashedVin` is the second gate: unknown tokens
**404**, a known car with no rows yet is a legitimate empty page. Pair both on
every new `hashedVin` route (`_unknown_hashed_vin_response`). Do not render an
empty Stats/DayMap/Charges page for a random hash — that looked like a 200
success and leaked that the URL shape was valid.

## Capture, and not waking cars

Cron hits `GET` or `POST /matesla/internal/capture` every minute. Fleet is
called only when a car is due. If none are due, there is no `/vehicles` list
call. A capture already holding `_capture_lock` returns HTTP 200 with
`skipped_already_running: true`. On `EXCEEDED_LIMIT` the run stops instead of
retrying.

Never send Fleet `wake_up`. List state `asleep` skips `vehicle_data`, except
the drive-seal window: if the latest sample is still driving and younger than
`DRIVE_SEAL_MAX_AGE_MIN` (30), capture still tries `vehicle_data` once so the
day map can place the real park point. List state `offline` is not sleep.
Fleet “offline” is often wrong; capture still calls `vehicle_data`, and
spacing treats it as `online_idle`.

Do not tighten intervals “to be fresher” without a reason. Current baseline in
`capture.py` (minutes), civil clock `Europe/Brussels`, night 22:00 inclusive to
06:00 exclusive:

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

`INTERVAL_DC_CHARGE_MIN` stays **1 minute**. Do not change it to 2. Do not add
an extra-high-SoC poll kind.

AC wall connectors `MCSingleWireCAN` and `ACSingleWireCAN` (also `ac`,
`ac_single`, `ac_three`) are not DC. Classifying any non-empty
`fast_charger_type` as DC polled cars every minute and burned Fleet credit.
DC types are `combo`, `ccs`, `tesla`, `supercharger`, `chademo`, or
`fast_charger_present`, with the power heuristic at `DC_POWER_KW_MIN` (12 kW).

A snapshot older than `ACTIVITY_SNAP_MAX_AGE_MIN` (20 minutes, and always
above the 15 minute AC interval) must not choose the next interval. A stale
“Charging” flag must not keep the AC cadence after the car sleeps. List
`asleep` wins over that flag.

Long park with no drive and no charge raises an idle floor: ≥24 h → 10 min,
≥48 h → 15, ≥72 h → 30. Applied as `max(interval, floor)`.

`poll_habits.py` may replace the idle/asleep/sentry baseline for the current
weekday+hour (not `max` with the baseline). It does not apply to
`LIVE_ACTIVITY_KINDS` (`driving`, `dc_charge`, `ac_charge`, `cabin`,
`dogcamp`). Sentry is idle for this purpose. Trusted model classes: `busy`
5 min, `busy_near` 10 min (±1 h around a busy core), `quiet` 30 min,
`moderate` 15 min by day only (night moderate leaves the 30 min baseline).
Window is about 12 weeks, at least 2 reference weeks, and a regime break
(school / holidays / trip) drops trust. The README’s three-class table is
stale; follow `poll_habits.py`. Diagnose with `python manage.py ShowPollHabits
--force` or personal stats Polling details.

Each capture tick also runs `geo_enrich.geo_enrichment_tick` even when no car
was due. That path must not call Fleet.

After a Supercharge **ends**, capture may call Tesla charging-history
(`maybe_refresh_invoice_after_supercharge`). Tesla publishes the invoice only
once the session is closed. Fetching while still plugged in returns empty and
would start the 10 minute cooldown, so DayMap would keep the fallback €/kWh.
Settle 5 minutes after the last DC sample (covers asleep-still-Charging),
retry every 10 minutes for 24 h, fetch span 2 days, `force=True` on that
narrow window. AC wall is ignored. Day map and the landing request must not
HTTP-fetch `/api/1/dx/charging/history`.

`last_polled_at` is updated after a real poll attempt. Spacing uses that, not
“every cron tick”.

The laptop must be on for cron. User crontab does not catch up missed 20:00
Elia runs; `CRON_LOOKBACK_DAYS` (7) on the next evening run covers a missed
night. Capture does **not** poll Elia (DayMap timeout / Fleet lock).

## Day map first paint

`matesla.views.home` redirects to `PersoDayMap` using only the local DB (active
vehicle / VIN hash). It never calls Fleet `vehicle_data`. Live status stays at
`matesla/status` when the user opts in.

Day map render (`annotate_daymap_charges`):

- Matches Tesla invoices **already in the local cache**. No charging-history HTTP.
- Does **not** call Elia. A hole in the spot cache stays unpriced (`—`) until
  the evening cron or Charges backfill.
- Supercharger site names are filled async (`PersoMatchSupercharger`, ~400 m,
  directory cached 12 h from supercharge.info). Do not block first HTML on
  that directory.

`stats_bundle.py` is the same idea for the Stats grid: one SQL builder, PNGs
encode in parallel, series identical to the solo graph URLs. Graph
optimizations must keep the **same plotted series and style**.

## Charge cost

`matesla/charge_cost.py` prices a session in this order (most specific first):

1. Tesla Supercharger invoice (charging history, or a stored amount)
2. Home geofence for that vehicle and local date (with role period)
3. Work geofence likewise
4. Supercharger site match → user Supercharger **average** €/kWh
5. Other-chargers €/kWh
6. Unpriced — no invented euros

Open-ended periods (`valid_to` null) are valid. Missing `valid_to` is not why
a home dynamic session is unpriced; a hole in the Elia cache is.

Home dynamic: €/kWh = Elia spot €/MWh / 1000 + surcharge_cents / 100,
integrated per MTU (not one price × session kWh). Civil dates use
`Europe/Brussels`. A missing MTU → `COST_UNPRICED` / `COST_PARTIAL`, never a
guessed spot.

Who fetches Elia:

| Path | HTTP Elia? |
|------|------------|
| 20:00 cron `FetchEliaDayAhead` | Yes: today, D+1 (auction ~13:00 Brussels), lookback 7 days |
| Charges page (`price_sessions_starting_in`) | Yes, only if a dynamic tariff overlaps the window, newest first, cap `WEB_MAX_FETCH_DAYS` (14) |
| Saving a dynamic tariff | Yes, `ensure_spots_for_dynamic_period` |
| Day map | **No** |
| Capture / car poll | **No** |

Cached civil days (≥20 MTUs) are skipped. Prefer quarter-hour, fall back to
hourly.

Tesla Supercharger invoice vs fallback rate:

- When an invoice is matched, the displayed € is Tesla’s **billed total**
  (delivered kWh, idle fees). Do not recompute 0.21 × battery-added kWh.
- Session `kwh` is energy **added to the pack** (car). Tesla’s kWh is energy
  **delivered**. A few percent gap is normal; it is not a matesla bug.
- `ChargeCostSettings.supercharger_eur_per_kwh` is a user estimate used only
  when no invoice is in cache (rule `supercharger_rate`). It is not Tesla’s
  site price.

Tesla charging history (`GET /api/1/dx/charging/history`) is chunked to Tesla’s
1-year API limit (`MAX_API_SPAN` 364 days after ±12 h pad). Match onto plug-in
sessions by overlap (20 min pad) or close start (30 min). Tesla sometimes
splits one stop into two invoices; sum them. Charges may backfill one extra
older chunk. Manual sync is on Charge rates (`ChargeCostsSetup`), localhost
only.

Place clusters in `personalstats/charge_pages.py` round GPS to 0.001° (~100 m)
and then merge nearby cells so one driveway is one place. Default geofence
radius is 150 m. Role (home/work) is **per vehicle** and dated
(`VehiclePlaceRole`). Two cars at the same campsite each get the daily pitch
fee (`TARIFF_PER_DAY`: one fee per place per Brussels civil day **per car**).

Charges KPI distance: first→last odometer **inside** the displayed window
(`_period_odometer_delta_miles`). A sample just before the window counts only
if it is ≤ 7 days old (`_ODO_PRIOR_MAX_GAP`). A months-long TeslaFi/offline
hole must not dump all intervening km into the year. One lonely sample,
no in-window samples, or a negative delta → `None` (unknown, `—`), not 0.
Two samples at the same odometer → true 0 (parked). €/100 km only when
distance > 0, there are sessions, and `unpriced_kwh == 0`.

Odometer in the DB is **miles** (Fleet / TeslaFi). Convert only at display
(`matesla.units`, cookie `matesla_distance_unit`, default km).

Source badges on the Charges table (invoice vs estimate vs home…) are
required. Do not invent historical home tariffs for years before the owner
configured places.

## Where-did-I-go

`/personalstats/Where/<hashedVin>/` — place or region + date range → civil days
the car was there, DayMap drill-down, map of matched days.

Uses `TeslaCarDataSnapshot` GPS and the same reverse-geocode stack as the rest
of the app (Geoapify if a key is set, else Nominatim). **Does not** use
`ChargePlace`, address `ILIKE`, or `active_route_destination`.

Forward geocode once, cache key includes `FORWARD_CACHE_VERSION` (`v2`: amenity
supplement so a river/region query is not only the city). Tight bbox for a
city, large bbox for a region (constants in `AddressFromLatLong.py`). Ambiguous
→ 2–3 chips (`c` index). Vehicle switch keeps `q` / `from` / `to`, not `c`.
Empty To → `end_day = start_day`. Geocode fail, empty, or quota: i18n message,
not 500.

## Maps and geocoding

Raster tiles must stay `https://tile.openstreetmap.org/{z}/{x}/{y}.png`
(`OSM_TILE_URL`). No `{s}` letter subdomains. `matesla/tests_osm_tiles.py`
fails if a template contains `{s}.tile.openstreetmap.org` or if
`SECURE_REFERRER_POLICY` is not `strict-origin-when-cross-origin`. Django’s
`same-origin` default strips `Referer` on the tile host; OSMF requires it.
`no-referrer` is also wrong. Keep the English licence line in
`mysite/context_processors.py` (`osm_tiles`): “© OpenStreetMap contributors”.
Carto/Geoapify raster tiles were rejected; do not switch provider unless OSM
blocks this install after that policy fix.

Reverse geocode: Geoapify when `GEOAPIFY_API_KEY` or `GEOAPIFY_KEY` is set,
otherwise public Nominatim (`AddressFromLatLong.active_geocoder`). Cache is a
~11 m grid (4 decimal degrees) in `AddressFromLatLong`. Empty address is
allowed (elevation-only rows). Daily caps and minimum intervals are in
settings (`GEOAPIFY_*`, `NOMINATIM_*`). The quota table is still named
`NominatimDailyQuota`; it counts Geoapify too. Backfill must use the backfill
purpose so it does not eat the interactive budget. Do not reverse-geocode every
drive sample; `geo_enrich` only fills high-value grids (parked / endpoints),
default 1 address per tick on Nominatim and 5 on Geoapify. Prefer driveable
roads over footways (`_CAR_ROAD_KEYS` vs `_PEDESTRIAN_KEYS`).

Changing the Geoapify key requires a gunicorn restart. Free-tier Geoapify needs
the visible “Powered by Geoapify” footer (`geocoder_attribution`). Elevation
comes from Open-Meteo, not Fleet.

Optional outbound proxy for Fleet only: `HTTPS_PROXY` via
`matesla/GetProxyToUse.py`. Leave it unset on this machine unless Tesla blocks
the egress IP.

## Graphs, DC curves, degradation

Store Fleet/TeslaFi SoC as received. Do not rewrite `battery_level` from
`battery_range` (`soc_refine.py`, `RestoreRawSoc`). Degradation is
rule-of-three: implied 100% rated miles = `battery_range / (SoC/100)`, then
vs cached EPA miles (`BatteryDegradation.py`, `epa_catalog.py`). Scatter fits
prefer ≥ 80 % SoC (fallback 75 %).

DC analytics (`personalstats/dc_charge.py`): session peak floor
`DC_SESSION_PEAK_KW_MIN` (40 kW) so destination AC (~22 kW) does not enter the
curves. Drop the Supercharger power ramp (samples before the first peak) for
power-vs-SoC; AC uses plain min/max. Mode `all` includes every DC session;
default is robust (MAD + slow-start gate).

A graph change that “looks nicer” but plots different series or a different
smoothing is a product change. Keep the series.

## Settings, secrets, translations

`DEBUG` is on only when `DJANGO_DEBUG` is `1`, `true`, or `yes`. Do not
hard-code `DEBUG = True`. The debug toolbar is installed only in that case.
Default `SECRET_KEY` in settings is a local fallback; do not rotate it casually
(`saltSeed` is mixed into legacy VIN hashes).

Never commit:

- `.env` (Tesla client id/secret, Geoapify, `DJANGO_SECRET_KEY`, SendGrid)
- `tesla_keys/` (`private-key.pem` and the partner public key)
- `db.sqlite3`, `*.sqlite3`, WAL/SHM sidecars, `db-backups/`

`.mo` files are intentionally not gitignored (see `.gitignore`). After editing
strings, compile them and include the `.mo` updates.

Languages in `LANGUAGES`: `en` (source msgid), `fr`, `es`, `de`, `nl`, `nb`.
New user-facing strings go through `gettext` / `{% trans %}` / `{% blocktrans %}`.
Then:

```bash
.venv/bin/python manage.py makemessages -a --no-wrap
# fill empty msgstr in locale/*/LC_MESSAGES/django.po
.venv/bin/python manage.py check_translations
.venv/bin/python manage.py compilemessages
```

Then restart gunicorn. A new language means a `LANGUAGES` entry, a
`locale/<code>/LC_MESSAGES/django.po`, compiled `.mo`, and the `tests_i18n`
list. Ops log lines inside `capture.py` are French on purpose so
`/tmp/matesla-capture.log` stays readable. Do not translate those as a
drive-by.

Django may warn on migrate dry-run about a `teslacharginginvoice` index rename.
Do not create a new migration for that unless the user asked.

Firmware history is still a page. Live capture stores the current version when
first seen online. Do not “clean up leftover firmware tables” unless asked.

## Naming and comments

Use full words in new Python names, URL names, and template variables. Avoid
cryptic abbreviations. Short domain tokens that already exist are fine: `vin`,
`soc`, `eta`, `kwh`, `dc`, `ac`, `osm`.

Comments in English, and only to say why a constraint exists (billing, SQLite,
OSMF tile policy, stale Fleet flags, honesty). Do not comment what the next
line obviously does.

## Locked non-goals

Do not implement these unless the user explicitly reopens them:

- Fleet Telemetry / streaming (capture stays poll-based)
- Vehicle commands or `wake_up`
- Changing `INTERVAL_DC_CHARGE_MIN` from 1 minute
- Extra-high-SoC poll cadence
- Elia or Tesla charging-history HTTP on DayMap / landing
- Fake euros or fake 0 km for unknown windows
- Inventing historical home/work tariffs
- `{s}.tile.openstreetmap.org`, Carto, or Geoapify raster tiles
- Second gunicorn worker, or `runserver` beside gunicorn on another port
- Binding 8000 or Tailscale 443 (PicturesDjango)
- ChargePlace as a geocoder for Where
- FirmwareHistory leftover schema cleanup
- Recalculating Supercharger € from car kWh when a Tesla invoice exists

## Git

Do not commit or push unless the user explicitly asked. The user wants the
site checked in a browser (local `http://127.0.0.1:8001`, and the Tailscale
host if the change is visible there) before a commit. Tailscale cannot save
settings; use local HTTP for anything that writes.

Do not stage `.env`, `tesla_keys/`, or any sqlite file. Do not retarget ports
8000 or 443.
