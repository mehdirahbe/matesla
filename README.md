# matesla

Python/Django website using the Tesla API to connect to your marvellous Tesla car.

Useful files:

1. Procedures calling the Tesla API (extend here if something is missing):  
   `matesla/TeslaConnect.py`
2. Views that return the HTML shown in the browser:  
   `matesla/views.py`
3. URL routes (path, view, and reverse name used from templates):  
   `matesla/urls.py`
4. Car status page template:  
   `matesla/templates/matesla/carstatus.html`
5. Base layout and shared CSS hooks:  
   `templates/base.html`

How to:

1. To display extra live fields on the status page, adapt `_carstatus_body.html`
   (and `PreparestatusDictionary` if the value is computed).
2. Change look: adapt CSS under `static/` / templates.

Vehicle commands (lock, climate, charge start, …) were removed: modern cars
require Tesla’s Vehicle Command Protocol; the official app covers remote
control. matesla focuses on status, history, maps, and stats.

Todo:

1. Improve look of AddTeslaAccount form
2. Allow to set EPA range
3. Allow to change password / recover features if password is lost
4. Add more languages. If you are a native speaker, please don't hesitate
   to add a new language. No need to be a programmer to do that — ask for a
   prepared text file and translate it.

Install on Linux (Ubuntu / Debian-like)
---------------------------------------

One script: venv, dependencies, database, static files, systemd service at boot,
**capture cron** (history every minute), and a menu/desktop shortcut that opens
the browser.

**Prerequisites** (once per machine):

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip curl
# optional (weekly DB backups): sudo apt install -y zstd
```

**Install** (from the matesla folder — clone or unzip):

```bash
cd /path/to/matesla
./scripts/install-linux.sh
```

Then open **http://127.0.0.1:8001** (or use the **MaTesla** menu / desktop icon).

What the install enables for history:

| Piece | Role |
|-------|------|
| systemd / gunicorn | App always listening on `127.0.0.1:8001` |
| user crontab (every minute) | `curl` → `/matesla/internal/capture` (adaptive Fleet spacing inside the app) |
| log | `/tmp/matesla-capture.log` |

Without the capture cron, status works but **graphs stay empty**.

Options:

| Flag | Effect |
|------|--------|
| `--no-service` | Skip systemd (venv + migrate only; start manually) |
| `--no-cron` | Skip history capture crontab (not recommended) |
| `--with-backup` | Also install the weekly SQLite backup timer |
| `--no-desktop` | Do not create menu / desktop launcher |

Useful commands after install:

```bash
systemctl status matesla-gunicorn.service
sudo systemctl stop matesla-gunicorn.service     # free port for runserver
sudo systemctl start matesla-gunicorn.service
journalctl -u matesla-gunicorn.service -f
crontab -l
tail -f /tmp/matesla-capture.log
./scripts/install_capture_cron.sh                # re-add capture cron alone
./scripts/uninstall_capture_cron.sh
./config/uninstall_gunicorn_service.sh           # remove service completely
```

The systemd unit is **generated for your user and install path**. Re-run
`./config/install_gunicorn_service.sh` after moving the project directory.

Tesla Fleet credentials (`.env` or in-app setup) still require a Tesla Developer
account — matesla cannot skip that step.

For developers (manual run, no install script)
----------------------------------------------

Python 3.12+, Django 5.2 LTS:

1. `python3 -m venv .venv && source .venv/bin/activate`
2. `pip install -U pip && pip install -r requirements.txt`
3. Optional: Tesla Fleet credentials in a `.env` file (see `settings.py`)
4. `python manage.py migrate`   # SQLite by default (`db.sqlite3`)
5. `python manage.py createsuperuser`  # optional
6. `python manage.py test`
7. `python manage.py collectstatic --noinput`   # needed when DEBUG is off
8. `python manage.py runserver 127.0.0.1:8001`  
   or systemd only: `./config/install_gunicorn_service.sh`

`DEBUG` is **off by default** (no Django debug toolbar). To re-enable:

```bash
export DJANGO_DEBUG=1
python manage.py runserver 127.0.0.1:8001
```

Access MaTesla on your phone via Tailscale (HTTPS)
--------------------------------------------------

Goal: phone and laptop on the same [Tailscale](https://tailscale.com) tailnet,
with MaTesla reachable over HTTPS without opening the home router.

Default ports used in the examples below:

| HTTPS (Tailscale Serve) | Local backend | App |
|-------------------------|---------------|-----|
| **8443** | `http://127.0.0.1:8001` | MaTesla |

You can use port **443** instead if nothing else on the machine already uses
Tailscale Serve on 443.

### 1. Run MaTesla locally

Prefer `./scripts/install-linux.sh` (systemd on `127.0.0.1:8001`).  
**`Restart=no`**: if you `stop` or kill the service to free the port for
`runserver`, it stays down until the next boot (or `systemctl start`).

```bash
# already installed via install-linux.sh, or:
./config/install_gunicorn_service.sh
systemctl status matesla-gunicorn.service
```

Manual dev server:

```bash
cd /path/to/matesla
source .venv/bin/activate
python manage.py collectstatic --noinput
# sudo systemctl stop matesla-gunicorn.service   # if daemon holds the port
python manage.py runserver 127.0.0.1:8001
```

### Database backups (full history, compressed)

Minute-level history (TeslaFi + capture) is irreplaceable — backups are **full
SQLite snapshots**, not thinned exports. Archives live **next to the live DB**
in `db-backups/` (gitignored). **MaTesla never uploads backups**; copy those
files yourself to USB / cloud storage.

| | |
|--|--|
| Format | `sqlite3.backup` then **zstd -3** (typically ~8× smaller) |
| Cadence | weekly (timer) or manual; skip if last archive &lt; 6 days (unless `FORCE=1`) |
| Retention | **4** newest `matesla-db-*.sqlite3.zst` only |

```bash
# one-shot
./scripts/backup_db.sh
FORCE=1 ./scripts/backup_db.sh   # ignore 6-day gap

# automatic (recommended): user crontab — e.g. Sunday 03:30
# 30 3 * * 0 /path/to/matesla/scripts/backup_db.sh >> /tmp/matesla-db-backup.log 2>&1
crontab -l | grep backup_db

# alternative: systemd timer (optional, needs sudo + zstd)
# ./config/install_db_backup_timer.sh
# or: ./scripts/install-linux.sh --with-backup
```

Restore example:

```bash
zstd -d db-backups/matesla-db-YYYY-MM-DD.sqlite3.zst -o /tmp/matesla-restore.sqlite3
# stop web first, then replace live DB carefully
sudo systemctl stop matesla-gunicorn.service
cp db.sqlite3 "db.sqlite3.before-restore-$(date +%F)"
cp /tmp/matesla-restore.sqlite3 db.sqlite3
sudo systemctl start matesla-gunicorn.service
```

### 2. Configure Tailscale Serve

Prefer making your user the Tailscale operator once:

```bash
sudo tailscale set --operator=$USER
```

Then either run the helper:

```bash
./scripts/tailscale-serve-matesla.sh
```

Or manually:

```bash
# MaTesla on HTTPS 8443 → local 8001
tailscale serve --bg --yes --https=8443 http://127.0.0.1:8001

tailscale serve status
```

Open on the phone (Tailscale app connected, **Use Tailscale DNS** / MagicDNS on):

```text
https://<your-machine>.<tailnet>.ts.net:8443/
```

Use a language-prefixed path if you prefer (e.g. `/fr/`, `/en/`, …).

### Read-only on Tailscale (HTTPS remote Host)

**Write is only allowed when the HTTP `Host` is local** (`127.0.0.1` /
`localhost`). Access via the MagicDNS name (`*.ts.net`) is **read-only** and
**does not require a MaTesla login**:

- Status, personal stats, day map, vehicle switcher: **yes**
  (anonymous viewers use the household owner that holds the Tesla token)
- Tesla account / OAuth, signup, admin: **no** (UI hidden; direct URLs return **404**)
- Vehicle remote commands are **not supported** (use the official Tesla app;
  Fleet Vehicle Command Protocol is out of scope for matesla)
- Local `http://127.0.0.1:8001` still needs **login** for setup / full access

Override if needed:

```bash
export MATESLA_WRITABLE_HOSTS=127.0.0.1,localhost
# Optional if several Django users exist:
export MATESLA_OWNER_USERNAME=you@example.com
```

### 3. ACL: allow port 8443

If the site works on the laptop via MagicDNS but **not** on the phone, check
Tailscale **Access controls**. A rule that only allows port 443 (e.g.
`"ip": ["443"]`) will block 8443 on other devices. Allow both ports if you
also use 443, for example:

```json
{
  "src": ["*"],
  "dst": ["*"],
  "ip": ["443", "8443"]
}
```

(Adapt to your ACL format / node names if you use stricter `dst`.)

Ping between phone and laptop can still work when only 443 is allowed (ICMP);
HTTPS on 8443 will not.

### 4. Django hosts / CSRF

`mysite/settings.py` may need your MagicDNS host for HTTPS. If Django rejects
the Host header or CSRF fails, set:

```bash
export DJANGO_ALLOWED_HOSTS=your-host.tailnet.ts.net
export DJANGO_CSRF_TRUSTED_ORIGINS=https://your-host.tailnet.ts.net:8443
```

### 5. Reset / stop Serve

```bash
# Remove only MaTesla HTTPS binding on 8443
tailscale serve --https=8443 off

# Or wipe all Serve config on this node
# tailscale serve reset
```

TeslaFi-style history (online cars only, never wakes)
------------------------------------------------------

When a vehicle is already **online**, capture full `vehicle_data` into
`TeslaCarDataSnapshot` (graphs / personal stats). Offline/asleep cars are
skipped. The app **never** sends Fleet API wake commands (use the official
Tesla app if you need to wake a car).

### Adaptive spacing (cron can stay every minute)

Cron may hit the endpoint every minute; **Fleet is only called when due**.
Spacing uses Europe/Brussels wall clock + last known activity
(`TeslaVehicle.state` + latest snapshot: speed / shift / charging):

| Context | Interval |
|---------|----------|
| Driving (day or night) | 2 min |
| DC / Supercharger (day or night) | 2 min |
| Night 22:00–06:00, not drive/DC | 30 min (incl. AC wall charge) |
| Day, user present / dog / camp / climate on | 2 min |
| Day, Sentry only | 5 min |
| Day, online but no cabin/sentry signal | 5 min |
| Day, AC charging | 15 min |
| Day, asleep / offline | 5 min |

If no vehicle is due for a user, **no** `/vehicles` list call is made.
Each vehicle stores `last_polled_at` after a real poll attempt.
JSON stats include `skipped_wait` when the policy defers the car.

The latest snapshot only chooses the **next wait** (AC → 15 min day, etc.).
Flags from a snapshot older than **20 minutes** are ignored, and list
`asleep` wins over an old “Charging” flag. List `offline` still triggers
`vehicle_data` (Fleet offline is unreliable); only explicit `asleep` skips it.

### Adaptive poll spacing (Fleet cost)

`matesla/capture.py` still uses a **reactive** baseline (drive/DC 2 min,
AC 15 min day, night idle 30 min, day idle 5 min). On top of that, `matesla/poll_habits.py`
may **set idle/asleep** spacing for the current weekday+hour to:

| Habit class | Idle interval | Effect vs baseline |
|-------------|---------------|--------------------|
| **busy** | **5 min** | denser than night 30 (e.g. night driver) |
| **moderate** | **15 min** | sparser than day 5; denser than night 30 |
| **quiet** | **30 min** | sparser than day 5 |

When a habit applies it **replaces** the idle baseline (not `max()`), so reliable
busy nights *do* poll more often. Live drive/charge/cabin never use habits.

Conditions: last ~12 weeks only, ≥4 reference weeks, no regime break
(school ↔ holidays ↔ trips).  
Diagnose: `python manage.py ShowPollHabits --force`

### Why not `manage.py` from cron?

SQLite does not like two Django processes writing at once (web on port 8001
**plus** a second `python manage.py …`). Prefer:

1. **One** web process only, bound to localhost, e.g.  
   `gunicorn mysite.wsgi --bind 127.0.0.1:8001 --workers 1`  
   (or `runserver 8001` for dev)
2. Cron that only runs **curl** against that process (capture runs *inside*
   the web process → same DB connection)

Manual one-shot when the site is **stopped** is still fine:

```bash
python manage.py TakeTeslaCarDataSnapshot
```

### Capture HTTP endpoint

| | |
|--|--|
| URL | `http://127.0.0.1:8001/matesla/internal/capture` |
| Methods | GET or POST |
| Auth | none (intended for **localhost-only**; do not expose this port to the internet) |
| Response | JSON, e.g. `{"saved":1,"skipped_offline":0,"skipped_wait":2,"skipped_error":0,"token_error":0,"fleet_limit":0}` |

`saved` = snapshotted; `skipped_offline` = list said not online; `skipped_wait` = deferred by adaptive interval (no Fleet call for that car).

Quick test in a browser or shell (site must be running):

```bash
curl -fsS http://127.0.0.1:8001/matesla/internal/capture
```

### Schedule with cron (Linux “Task Scheduler”)

`./scripts/install-linux.sh` installs this automatically. To add or refresh
only the capture line:

```bash
./scripts/install_capture_cron.sh
# remove: ./scripts/uninstall_capture_cron.sh
```

Cron jobs run as **your user**, whether or not you are logged into a graphical
session, as long as the machine is on and the `cron` service is running.

Manual equivalent (every minute + timestamped log):

```cron
* * * * * { date -Iseconds; curl -fsS http://127.0.0.1:8001/matesla/internal/capture; echo; } >> /tmp/matesla-capture.log 2>&1
```

Check:

```bash
crontab -l
tail -f /tmp/matesla-capture.log
```

#### Cron field order

```text
minute  hour  day-of-month  month  day-of-week  command
*       *     *             *      *            run every minute
*/5     *     *             *      *            every 5 minutes
0       *     *             *      *            every hour at :00
```

#### What the log line does

| Part | Role |
|------|------|
| `date -Iseconds` | timestamp before each run (curl alone has no time) |
| `curl -fsS …` | call the capture endpoint |
| `echo` | blank line between runs |
| `>> /tmp/matesla-capture.log` | append stdout to the log file |
| `2>&1` | also append errors (e.g. site down) |

Example when the site is **down** (dev, not started):

```text
2026-07-25T20:15:00+02:00
curl: (7) Failed to connect to 127.0.0.1 port 8001 … Couldn't connect to server
```

Example when the site is **up**:

```text
2026-07-25T20:16:00+02:00
{"saved":1,"skipped_offline":2,"skipped_error":0,"token_error":0}
```

Logging to `/tmp` is optional (handy; may be cleared on reboot). Without
`>> … 2>&1`, cron still runs the job but you usually see nothing.

To remove the job later: `crontab -e` and delete the line.

#### Requirements for a successful capture

- Machine powered on; `cron` service active
- Web app listening on `127.0.0.1:8001`
- Tesla account linked (OAuth) and partner register done for the Fleet region

TeslaFi history import (monthly CSV)
------------------------------------

To backfill graphs from [TeslaFi monthly exports](https://teslafi.com/export2.php):

1. **Download** month CSVs: `scripts/download_teslafi_exports.py`  
   (cookie from Chrome recommended if the TeslaFi account has **2FA**)
2. **Import** into `TeslaCarDataSnapshot`:  
   `python manage.py ImportTeslaFiCSV path/to/MYYYY.csv --tz Europe/Brussels`

Full guide (auth, 2FA, long ranges, gaps, batch import):  
**[docs/teslafi-import.md](docs/teslafi-import.md)**
