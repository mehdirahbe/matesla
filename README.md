# matesla
Python/django website using tesla api to connect to your marvellous Tesla car

Useful files:
1) Procedures calling tesla api, if you need something not yet available, it there that you have to adapt code
https://github.com/mehdirahbe/matesla/blob/master/matesla/TeslaConnect.py

2) Views which return the HTML content displayed in browser.
https://github.com/mehdirahbe/matesla/blob/master/matesla/views.py

3) List all URL available. Contain the URL, the view to use and a short name used to reference it from HTML
https://github.com/mehdirahbe/matesla/blob/master/matesla/urls.py

4) The HTML with car status page
https://github.com/mehdirahbe/matesla/blob/master/matesla/templates/matesla/carstatus.html

5) The base of all HTML rendering, it contains the formatting in the form of CSS
https://github.com/mehdirahbe/matesla/blob/master/templates/base.html

How to:
1) To add a new link, you have to adapt urls.py, views.py (to serve it) and carstatus.html (to display the link). See for example sentry or door lock.

2) If you want to display an additional information on the car, you probably only have to adapt carstatus.html, except if it is a computed value (such as battery degradattion) where you will also have to adapt views.py to compute the value and put it in the context passed to rendering.

3) Change look: adapt CSS in base.html

Todo:
1) Improve look of AddTeslaAccount form https://afternoon-scrubland-61531.herokuapp.com/fr/matesla/AddTeslaAccount
2) Allow to set EPA range
3) Allow to control overheat
4) When doing a command, avoid a page refresh
5) Allow to change PW+add feature in case of lost PW
6) Add more languages. If you are a native speaker, please don't hesitate
to add a new language. No need to be a programmer to do that, just ask me to prepare
and I will prepare a text file to just translate.

For developers, how to run site locally (Python 3.12+, Django 5.2 LTS):
1) python3 -m venv .venv && source .venv/bin/activate
2) pip install -U pip && pip install -r requirements.txt
3) Optional: copy Tesla Fleet credentials into a `.env` file (see settings.py)
4) python manage.py migrate   # SQLite by default (db.sqlite3); set DATABASE_URL for Postgres
5) python manage.py createsuperuser  # optional
6) python manage.py test
7) python manage.py runserver 8001

TeslaFi-style history (online cars only, never wakes)
------------------------------------------------------

When a vehicle is already **online**, capture full `vehicle_data` into
`TeslaCarDataSnapshot` (graphs / personal stats). Offline/asleep cars are
skipped. The app **never** sends Fleet API wake commands (use the official
Tesla app if you need to wake a car).

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
| Response | JSON, e.g. `{"saved":1,"skipped_offline":2,"skipped_error":0,"token_error":0}` |

`saved` = vehicles snapshotted this run; `skipped_offline` = not online (no data call).

Quick test in a browser or shell (site must be running):

```bash
curl -fsS http://127.0.0.1:8001/matesla/internal/capture
```

### Schedule with cron (Linux “Task Scheduler”)

Cron jobs created with `crontab -e` run as **your user**, whether or not you
are logged into a graphical session, as long as the machine is on and the
`cron` service is running.

1. Open your crontab:

   ```bash
   crontab -e
   ```

   (Often opens **nano**.)

2. Add **one line** at the end (every minute + timestamped log):

   ```cron
   * * * * * { date -Iseconds; curl -fsS http://127.0.0.1:8001/matesla/internal/capture; echo; } >> /tmp/matesla-capture.log 2>&1
   ```

3. Save and quit nano:
   - **Ctrl+O**, then **Enter** (confirm the temp path cron gives you — often under `/tmp/crontab.…`; that is normal)
   - **Ctrl+X** to exit  
   Cron then installs that file as your real crontab.

4. Check:

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

Import historique TeslaFi (CSV mensuels)
----------------------------------------

To backfill graphs from [TeslaFi monthly exports](https://teslafi.com/export2.php):

1. **Download** month CSVs: `scripts/download_teslafi_exports.py`  
   (cookie from Chrome recommended if the TeslaFi account has **2FA**)
2. **Import** into `TeslaCarDataSnapshot`:  
   `python manage.py ImportTeslaFiCSV path/to/MYYYY.csv --tz Europe/Brussels`

Full guide (auth, 2FA, long ranges, gaps, batch import):  
**[docs/teslafi-import.md](docs/teslafi-import.md)**

