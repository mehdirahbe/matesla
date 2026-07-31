# Import historique TeslaFi → matesla

Deux outils complémentaires :

| Étape | Outil | Rôle |
|--------|--------|------|
| 1. Export | `scripts/download_teslafi_exports.py` | Télécharge les CSV mensuels depuis [TeslaFi Export](https://teslafi.com/export2.php) |
| 2. Import | `python manage.py ImportTeslaFiCSV` | Charge les CSV dans `TeslaCarDataSnapshot` |

Les dates TeslaFi sont en **heure locale du compte** (souvent Europe/Brussels) ; l’import les convertit en **UTC**.  
Déduplication : **même VIN + même minute** → fusion / mise à jour de la ligne existante.

> Ne jamais committer de cookie, mot de passe ou code 2FA.

---

## Prérequis

- Compte TeslaFi avec données pour **la bonne voiture** (multi-véhicules : sélectionner la voiture active avant l’export)
- Environnement matesla (venv + `requirements.txt`)
- Migrations à jour (`python manage.py migrate`)

---

## 1. Télécharger les CSV

Script autonome (stdlib Python uniquement) :

```bash
cd /chemin/vers/matesla
source .venv/bin/activate   # optionnel pour le download seul
python scripts/download_teslafi_exports.py --help
```

### Authentification

#### A. Cookie Chrome (recommandé avec **2FA**)

1. Se connecter sur https://teslafi.com (valider le 2FA dans le navigateur)
2. Choisir le **bon véhicule** si le compte en a plusieurs
3. Ouvrir https://teslafi.com/export2.php
4. **F12 → Network → recharger (F5) → clic sur `export2.php`**
5. **Headers → Request Headers → Cookie** : copier **toute** la ligne (pas seulement `PHPSESSID` dans l’onglet Application)

```bash
export TESLAFI_COOKIE='PHPSESSID=…; autres=…'

python scripts/download_teslafi_exports.py --cookie-only \
  --from 2019-02 --to 2025-06 \
  --out ~/Téléchargements/teslafi-corentin \
  --skip-existing \
  --sleep 1.5
```

#### B. Login username / password (+ 2FA interactif)

```bash
python scripts/download_teslafi_exports.py \
  --from 2025-05 --to 2026-07 \
  --out ~/Téléchargements/teslafi-robotbleu \
  --skip-existing
```

Le script demande email/username, mot de passe, puis le **code 2FA** si TeslaFi l’affiche.

Variables d’environnement optionnelles : `TESLAFI_USER`, `TESLAFI_PASSWORD`, `TESLAFI_TOTP`, `TESLAFI_COOKIE`.

### Options utiles

| Option | Description |
|--------|-------------|
| `--from YYYY-MM` / `--to YYYY-MM` | Plage inclusive |
| `--out DIR` | Dossier de sortie (fichiers `MYYYY.csv`, ex. `72026.csv` = juillet 2026) |
| `--skip-existing` | Reprend après coupure de session sans re-télécharger |
| `--exclude YYYY-MM` | Ignore un mois (répétable) |
| `--sleep SEC` | Pause entre mois (défaut 1.5 s) |
| `--cookie-only` | Pas de login ; uniquement le cookie |
| `--debug` | Garde des HTML d’échec sous `DIR/debug/` |

### Trous dans l’historique

Un mois sans données TeslaFi produit un CSV quasi vide (en-tête seulement). C’est normal ; l’import peut les ignorer (taille &lt; ~5 Ko).

### Session perdue en cours de route

Sur une longue plage (plusieurs années), le cookie peut expirer :

1. Refaire le login / recopier le cookie
2. Relancer **la même commande** avec `--skip-existing`

---

## 2. Importer dans matesla

Pour **chaque** fichier non vide :

```bash
cd /chemin/vers/matesla
source .venv/bin/activate

python manage.py ImportTeslaFiCSV \
  ~/Téléchargements/teslafi-corentin/52021.csv \
  --tz Europe/Brussels
```

### Options

| Option | Description |
|--------|-------------|
| `csv_path` | Chemin du CSV mensuel TeslaFi |
| `--tz` | Fuseau des colonnes `Date` TeslaFi (défaut `Europe/Brussels`) → stocké en UTC |
| `--vin` | Force le VIN (sinon lu dans chaque ligne) |
| `--dry-run` | Compte créations / merges sans écrire |

### Import en lot

```bash
for f in ~/Téléchargements/teslafi-corentin/*.csv; do
  # ignorer les mois vides (en-tête seul ~2 Ko)
  [ "$(wc -c < "$f")" -lt 5000 ] && echo "skip tiny $f" && continue
  python manage.py ImportTeslaFiCSV "$f" --tz Europe/Brussels
done
```

Conseil : un dossier par voiture (`teslafi-robotbleu`, `teslafi-corentin`) pour éviter de mélanger les exports.

### Vérification rapide

```bash
python manage.py shell -c "
from matesla.models.TeslaCarDataSnapshot import TeslaCarDataSnapshot
from django.db.models import Min, Max, Count
vin = '5YJ3E7EB1KF200150'  # Corentin
qs = TeslaCarDataSnapshot.objects.filter(vin=vin)
print(qs.count(), qs.aggregate(Min('Date'), Max('Date')))
"
```

Dans l’UI : sélectionner le véhicule, période **1 an** / **2 ans** / **5 ans** (le défaut « 1 mois » ne montre que le passé récent).

---

## Comportement des données

- **`battery_level`** et la plupart des métriques sont en **float** (précision TeslaFi conservée)
- Champs TeslaFi absents de l’ancienne collecte live ont été ajoutés au modèle **et** à `SaveSnapshot` (Fleet)
- Fusion minute : si une capture live et une ligne TeslaFi tombent dans la même minute, TeslaFi complète / met à jour la ligne
- Champ **`est_battery_range`** parfois figé côté TeslaFi (ex. valeur constante pendant des mois) alors que `battery_level` / `battery_range` varient — préférer `battery_range` dans les graphes si besoin

---

## Exemples déjà utilisés

**RobotBleu** (un mois de test puis historique récent) :

```bash
python scripts/download_teslafi_exports.py --cookie-only \
  --from 2025-05 --to 2026-06 \
  --out ~/Téléchargements/teslafi \
  --skip-existing
```

**Corentin** (longue plage + 2FA + trous) :

```bash
python scripts/download_teslafi_exports.py --cookie-only \
  --from 2019-02 --to 2025-06 \
  --out ~/Téléchargements/teslafi-corentin \
  --skip-existing --sleep 1.5
```

---

## Fichiers concernés

- `scripts/download_teslafi_exports.py` — export HTTP TeslaFi
- `matesla/management/commands/ImportTeslaFiCSV.py` — import Django
- `matesla/models/TeslaCarDataSnapshot.py` — schéma + apply/merge
- `matesla/migrations/0037_teslafi_fields_and_floats.py` — migration float + champs
