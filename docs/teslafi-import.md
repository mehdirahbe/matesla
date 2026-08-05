# TeslaFi history import → matesla

Two complementary tools:

| Step | Tool | Role |
|------|------|------|
| 1. Export | `scripts/download_teslafi_exports.py` | Downloads monthly CSVs from [TeslaFi Export](https://teslafi.com/export2.php) |
| 2. Import | `python manage.py ImportTeslaFiCSV` | Loads CSVs into `TeslaCarDataSnapshot` |

TeslaFi timestamps are in the **account’s local time** (often Europe/Brussels);
the import converts them to **UTC**.  
Deduplication: **same VIN + same minute** → merge / update the existing row.

> Never commit a cookie, password, or 2FA code.

---

## Prerequisites

- TeslaFi account with data for the **correct car** (multi-vehicle: select the active car before export)
- matesla environment (venv + `requirements.txt`)
- Migrations applied (`python manage.py migrate`)

---

## 1. Download the CSVs

Standalone script (Python stdlib only):

```bash
cd /path/to/matesla
source .venv/bin/activate   # optional for download alone
python scripts/download_teslafi_exports.py --help
```

### Authentication

#### A. Chrome cookie (recommended with **2FA**)

1. Sign in at https://teslafi.com (complete 2FA in the browser)
2. Select the **correct vehicle** if the account has several
3. Open https://teslafi.com/export2.php
4. **F12 → Network → reload (F5) → click `export2.php`**
5. **Headers → Request Headers → Cookie**: copy the **entire** line (not only `PHPSESSID` from the Application tab)

```bash
export TESLAFI_COOKIE='PHPSESSID=…; other=…'

python scripts/download_teslafi_exports.py --cookie-only \
  --from 2019-02 --to 2025-06 \
  --out ~/Downloads/teslafi-car1 \
  --skip-existing \
  --sleep 1.5
```

#### B. Username / password login (+ interactive 2FA)

```bash
python scripts/download_teslafi_exports.py \
  --from 2025-05 --to 2026-07 \
  --out ~/Downloads/teslafi-car2 \
  --skip-existing
```

The script prompts for email/username, password, then the **2FA code** if TeslaFi asks for it.

Optional environment variables: `TESLAFI_USER`, `TESLAFI_PASSWORD`, `TESLAFI_TOTP`, `TESLAFI_COOKIE`.

### Useful options

| Option | Description |
|--------|-------------|
| `--from YYYY-MM` / `--to YYYY-MM` | Inclusive range |
| `--out DIR` | Output directory (files `MYYYY.csv`, e.g. `72026.csv` = July 2026) |
| `--skip-existing` | Resume after a session drop without re-downloading |
| `--exclude YYYY-MM` | Skip a month (repeatable) |
| `--sleep SEC` | Pause between months (default 1.5 s) |
| `--cookie-only` | No login; cookie only |
| `--debug` | Keep failure HTML under `DIR/debug/` |

### Gaps in history

A month with no TeslaFi data produces a nearly empty CSV (header only). That is normal; import can skip tiny files (size &lt; ~5 KB).

### Session lost mid-run

Over a long range (several years), the cookie may expire:

1. Sign in again / re-copy the cookie
2. Re-run **the same command** with `--skip-existing`

---

## 2. Import into matesla

For **each** non-empty file:

```bash
cd /path/to/matesla
source .venv/bin/activate

python manage.py ImportTeslaFiCSV \
  ~/Downloads/teslafi-car1/52021.csv \
  --tz Europe/Brussels
```

### Options

| Option | Description |
|--------|-------------|
| `csv_path` | Path to the TeslaFi monthly CSV |
| `--tz` | Timezone of TeslaFi `Date` columns (default `Europe/Brussels`) → stored as UTC |
| `--vin` | Force VIN (otherwise read from each row) |
| `--dry-run` | Count creates / merges without writing |

### Batch import

```bash
for f in ~/Downloads/teslafi-car1/*.csv; do
  # skip empty months (header only ~2 KB)
  [ "$(wc -c < "$f")" -lt 5000 ] && echo "skip tiny $f" && continue
  python manage.py ImportTeslaFiCSV "$f" --tz Europe/Brussels
done
```

Tip: one directory per car (`teslafi-car1`, `teslafi-car2`) so exports are not mixed.

### Quick check

```bash
python manage.py shell -c "
from matesla.models.TeslaCarDataSnapshot import TeslaCarDataSnapshot
from django.db.models import Min, Max, Count
vin = 'YOUR_VIN_HERE'
qs = TeslaCarDataSnapshot.objects.filter(vin=vin)
print(qs.count(), qs.aggregate(Min('Date'), Max('Date')))
"
```

In the UI: select the vehicle, period **1 year** / **2 years** / **5 years** (default **1 month** only shows recent history).

---

## Data behaviour

- **`battery_level`** and most metrics are **float** (TeslaFi precision kept)
- TeslaFi fields missing from older live capture were added to the model **and** to `SaveSnapshot` (Fleet)
- Per-minute merge: if a live capture and a TeslaFi row fall in the same minute, TeslaFi fills / updates the row
- **`est_battery_range`** is sometimes stuck on TeslaFi’s side (e.g. constant for months) while `battery_level` / `battery_range` move — prefer `battery_range` in charts if needed

---

## Example ranges

**Recent history only** (one car):

```bash
python scripts/download_teslafi_exports.py --cookie-only \
  --from 2025-05 --to 2026-06 \
  --out ~/Downloads/teslafi \
  --skip-existing
```

**Long range** (2FA + possible gaps):

```bash
python scripts/download_teslafi_exports.py --cookie-only \
  --from 2019-02 --to 2025-06 \
  --out ~/Downloads/teslafi-car1 \
  --skip-existing --sleep 1.5
```

---

## Related files

- `scripts/download_teslafi_exports.py` — TeslaFi HTTP export
- `matesla/management/commands/ImportTeslaFiCSV.py` — Django import
- `matesla/models/TeslaCarDataSnapshot.py` — schema + apply/merge
- `matesla/migrations/0037_teslafi_fields_and_floats.py` — float + fields migration
