# Partner register sans domaine payant (GitHub Pages)

Tesla exige un domaine HTTPS public pour le *partner register*.
**GitHub Pages** fournit gratuitement `https://TON_PSEUDO.github.io`.

Ton compte GitHub détecté côté machine : **mehdirahbe**  
→ domaine : **`mehdirahbe.github.io`**

## 1. Clés (déjà générées localement)

```
tesla_keys/private-key.pem   # SECRET — ne jamais publier
tesla_keys/public-key.pem    # à publier
tesla_keys/github-pages/.well-known/appspecific/com.tesla.3p.public-key.pem
```

URL finale attendue par Tesla :

```
https://mehdirahbe.github.io/.well-known/appspecific/com.tesla.3p.public-key.pem
```

## 2. Créer le site GitHub Pages (user site)

```bash
# Repo nommé EXACTEMENT : <pseudo>.github.io
gh repo create mehdirahbe.github.io --public --source=tesla_keys/github-pages --push
```

Ou à la main :

1. Créer le dépôt public `mehdirahbe.github.io` sur GitHub
2. Y pousser le contenu de `tesla_keys/github-pages/` (avec le dossier `.well-known/…`)
3. Settings → Pages → Branch `main` / root
4. Attendre 1–2 min, vérifier l’URL ci-dessus dans le navigateur (le PEM s’affiche)

## 3. Dashboard Tesla (MyRobotCar)

Éditer les origines autorisées, **en plus** de `http://localhost:8001` :

| Champ | Valeur |
|--------|--------|
| Allowed Origin | `https://mehdirahbe.github.io` |
| Redirect (inchangé) | `http://localhost:8001/oauth/callback` |

## 4. Dans matesla

1. Domaine partner : `mehdirahbe.github.io` (sans `https://`)
2. Enregistrer
3. **Vérifier la clé en ligne**
4. **Register partner (EU)**
5. **Resync véhicules**

## Notes

- Ne mets **pas** le mot « tesla » dans un nom de domaine custom
- La clé privée reste uniquement sur ton PC (`tesla_keys/`, gitignored)
- Si le pseudo GitHub change, adapte le nom du repo et le domaine
