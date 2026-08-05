# Partner register without a paid domain (GitHub Pages)

Tesla requires a public HTTPS domain for *partner register*.
**GitHub Pages** provides one for free: `https://YOUR_USERNAME.github.io`.

Replace `YOUR_USERNAME` below with your GitHub username.

## 1. Keys (generate locally if needed)

```
tesla_keys/private-key.pem   # SECRET — never publish
tesla_keys/public-key.pem    # publish this one
tesla_keys/github-pages/.well-known/appspecific/com.tesla.3p.public-key.pem
```

URL Tesla will fetch:

```
https://YOUR_USERNAME.github.io/.well-known/appspecific/com.tesla.3p.public-key.pem
```

## 2. Create the GitHub Pages site (user site)

```bash
# Repo name must be EXACTLY: <username>.github.io
gh repo create YOUR_USERNAME.github.io --public --source=tesla_keys/github-pages --push
```

Or manually:

1. Create a public repository `YOUR_USERNAME.github.io` on GitHub
2. Push the contents of `tesla_keys/github-pages/` (including the `.well-known/…` tree)
3. Settings → Pages → Branch `main` / root
4. Wait 1–2 minutes, then open the URL above in a browser (the PEM should display)

## 3. Tesla developer dashboard

Edit allowed origins, **in addition to** `http://localhost:8001`:

| Field | Value |
|--------|--------|
| Allowed Origin | `https://YOUR_USERNAME.github.io` |
| Redirect (unchanged) | `http://localhost:8001/oauth/callback` |

## 4. In matesla

1. Partner domain: `YOUR_USERNAME.github.io` (no `https://`)
2. Save
3. **Verify public key online**
4. **Register partner** (for your Fleet region)
5. **Resync vehicles**

## Notes

- Do **not** put the word “tesla” in a custom domain name
- The private key must stay only on your machine (`tesla_keys/`, gitignored)
- If your GitHub username changes, update the repo name and domain accordingly
