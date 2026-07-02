# Go fully cloud — laptop OFF, everything still runs

Once set up, GitHub runs the bot on its servers on schedule:

| IST time | What happens |
|---|---|
| 09:40 | Locks the day's top picks + pre-market email |
| 10:00 / 12:00 / 14:00 | Momentum scan + watchlist email |
| 16:00 | End-of-day P&L, auto-tune, history snapshot + email |

Every run also **publishes the dashboard as a website** you can open from
your phone: `https://<your-username>.github.io/<repo-name>/`

Your laptop can be off. Emails still arrive. Dashboard stays current.

---

## One-time setup (~10 minutes)

### 1. Create the GitHub repo
- Sign in at github.com → **New repository**
- Name it e.g. `trading-bot`
- **Public** (required for the free dashboard website — see note below)
- Don't add a README (we're pushing an existing folder)

> **Public vs private:** GitHub Pages (the free website) needs a public repo
> on the free plan. Public = anyone could read the code and paper-trade
> history. Your Gmail password is NOT in the repo (it's in encrypted
> Secrets, and `gmail_login.txt` is gitignored). If you want it private,
> GitHub Pro (~$4/mo) enables Pages on private repos.

### 2. Push this folder
Open PowerShell in this folder and run:

```
git init
git add .
git commit -m "trading bot"
git branch -M main
git remote add origin https://github.com/<your-username>/<repo-name>.git
git push -u origin main
```

(If git asks who you are: `git config --global user.email "you@email.com"`
and `git config --global user.name "Your Name"` first.)

### 3. Add the email secrets
Repo → **Settings → Secrets and variables → Actions → New repository secret**:
- `GMAIL_USER` = your Gmail address
- `GMAIL_APP_PASSWORD` = the 16-character app password (same as gmail_login.txt)

### 4. Enable the website
Repo → **Settings → Pages**:
- Source: **Deploy from a branch**
- Branch: **main**, folder: **/docs** → Save

### 5. Test it
Repo → **Actions** tab → pick **premarket-lock** → **Run workflow**.
Two minutes later:
- you get the email,
- `https://<your-username>.github.io/<repo-name>/` shows the live dashboard.

---

## Day-to-day
Nothing. It just runs. Check the website or your inbox.

**Heads-up:** the cloud state (paper book, history) lives in the GitHub repo.
Your local copy won't include cloud trades unless you `git pull`. Treat the
website as the source of truth once cloud mode is on, and avoid running
premarket/eod locally on the same day (double-locking is prevented, but
state can drift between the two copies).
