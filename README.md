# BEST TRADING BOT — Indian Market Intraday Signal Engine

A disciplined, explainable **decision-support** engine for intraday trading on
NSE stocks. It scans a liquid universe (Nifty 50/100), combines **technical**
confluence with a **fundamental** quality gate and **news sentiment**, and
produces ranked trade ideas with concrete **entry, stop-loss, two targets, and
position size** — sized to a fixed % of your capital so risk is controlled.

> **Read this first.** This tool does **not** place trades and is **not**
> financial advice. It generates ideas from *free, delayed* data so you can make
> faster, less emotional decisions — you review and execute every trade through
> your own broker. No bot beats hedge funds or guarantees returns. SEBI's own
> studies show the large majority of individual intraday traders lose money.
> Treat this as a structured assistant, paper-trade it first, and only ever risk
> capital you can afford to lose.

---

## What it actually does

1. **Pulls intraday OHLCV** (5-min default) for each stock from Yahoo Finance,
   with a jugaad-data/NSE fallback. All free, no API key.
2. **Computes indicators** — EMA stack, VWAP, RSI, MACD, ATR, Supertrend,
   Bollinger, ADX, volume — vectorized in pandas.
3. **Scores each stock 0–100** by combining 8 independent factors into a
   weighted composite, and checks the **higher-timeframe (15-min) trend** agrees.
4. **Filters for quality** using Screener.in fundamentals (ROE, debt/equity) so
   it avoids junk, and nudges confidence with **Google News sentiment**.
5. **Builds a trade plan** — ATR-based stop, two reward:risk targets, and a
   share quantity sized so a stop-out loses only your configured % (e.g. 1%).
6. **Ranks and reports** the best setups to console + saved JSON/text, and can
   run a **live loop** during market hours (09:15–15:30 IST).

## Why this design (the honest edge)

Your edge as an individual is **not** speed or data — funds win there. It's
**discipline**: fixed risk per trade, volatility-based stops, only trading when
multiple signals agree and a real trend exists, and stopping for the day after a
loss limit. This bot enforces exactly that. The math is standard and transparent
so you can audit and tune every decision in `config.yaml`.

## Setup

```bash
pip install -r requirements.txt
```

## Usage

```bash
python run.py once              # one scan now → prints + saves a report
python run.py live              # continuous scans during market hours
python run.py symbol RELIANCE   # deep-dive a single stock
python run.py backtest          # backtest whole universe over ~55 days
python run.py backtest RELIANCE INFY   # backtest specific symbols
python run.py email             # scan once and email the momentum report
python run.py auto              # scan + email every 2h during market hours
python run.py paper             # log picks as paper trades + show live track record
python run.py web               # launch the local web dashboard (http://127.0.0.1:5000)
python run.py eod               # email the end-of-day paper P&L (run ~15:45 IST)
```

### Strategy modes: intraday vs swing

Set `mode` in `config.yaml`:

- `mode: swing` *(default)* - **multi-day momentum.** Scans DAILY candles for the
  strongest movers (stacked uptrend + high 20-day momentum + fresh breakout +
  volume surge) across large/mid/small caps, holds days-to-weeks, and targets
  **+8% (T1) / +16% (T2)** with a -5% stop. Uses daily data, which is far more
  reliable than the intraday feed. This is where the bigger moves live.
- `mode: intraday` - same-day momentum on 5-min candles, smaller targets, needs
  live data during market hours.

Tune the swing thresholds under the `swing:` block (targets, stop, momentum
filter, max hold days). Lower `swing.min_score` for more picks, raise it to be
stricter. **Reality check:** wider targets mean bigger wins *and* bigger losing
trades; no setting produces 10%/day. Backtest and paper-trade before trusting it.

## Backtesting (validate before you risk money)

The backtester replays ~55 days of historical intraday candles bar-by-bar
through the **exact** live signal logic. It has **no lookahead bias** (signals
fire on a bar's close, entries fill on the next bar's open), assumes
**worst-case fills** (if a bar hits both stop and target, the stop counts),
and charges **round-trip costs**. It reports win rate, expectancy (in INR and
R-multiples), profit factor, total return, and max drawdown, plus a plain-English
verdict on whether the configuration is worth trading. Results save to
`reports/backtest_*.json` and `.txt`.

> Run a backtest and read the verdict **before** trading any configuration.
> A negative expectancy means that setup loses money on past data — don't trade it.

Edit **`config.yaml`** to set your capital, risk per trade, universe, interval,
and thresholds. Start with `risk_per_trade_pct: 1.0` and paper-trade.

## Email reports (momentum watchlist every 2 hours)

The bot can email you a momentum watchlist on a schedule. Each pick is laid out
in the order you asked for:

1. **Why look at it** - the momentum reasons (EMA stack, VWAP, Supertrend, MACD,
   volume, RSI) that fired.
2. **Potential upside** - Target 1 and Target 2 with the % move and reward:risk,
   and where to book partial vs. trail.
3. **Risks & exit** - the hard stop (price + % + rupees at risk), what
   invalidates the idea, negative-news flags, and the intraday square-off rule.

### One-time setup (Gmail App Password)

Gmail blocks normal-password logins from scripts, so you create a free
**App Password** (requires 2-Step Verification on your Google account):

1. Turn on 2-Step Verification: https://myaccount.google.com/security
2. Create an App Password: https://myaccount.google.com/apppasswords
   (pick "Mail" / "Other") - you'll get a 16-character code.
3. Set two environment variables so the code is never stored in the repo:

   **Windows (PowerShell), permanent:**
   ```powershell
   setx GMAIL_USER "arnav.t.srivastava@gmail.com"
   setx GMAIL_APP_PASSWORD "your16charapppassword"
   ```
   Close and reopen the terminal afterwards so the variables load.

4. In `config.yaml`, set `email.enabled: true`.

If the env vars are missing, the bot runs in **dry-run**: it writes the email to
`reports/email_preview_*.html` instead of sending, so you can preview safely.
Send a test with `python run.py email`.

### Running it every 2 hours

**Simplest** - keep one terminal open during market hours:
```bash
python run.py auto      # scans + emails every 2h (config: email.every_hours)
```

**Hands-off (Windows Task Scheduler)** - run `python run.py email` on a
2-hour trigger, 09:30-15:30 on weekdays:
1. Open *Task Scheduler* -> *Create Task*.
2. Triggers -> New -> Daily, **Repeat task every 2 hours** for a duration of
   "8 hours", active on weekdays.
3. Actions -> New -> Program: `python`, Arguments: `run.py email`,
   Start in: your project folder path.
4. (Optional) Settings -> only run when network is available.

## Automate everything (set-and-forget)

Double-click **`tasks\install_schedule.bat`** once. It creates two Windows
scheduled tasks (uses the Gmail creds you set with `setx`):

- **Momentum email every 2 hours**, 09:30 / 11:30 / 13:30 / 15:30, Mon-Fri.
- **End-of-day P&L email at 16:00**, Mon-Fri - shows, for each of the day's
  picks, what **Rs 10,000 in EACH** would have made/lost marked to the close,
  the total, and your **goal % vs actual**.

To stop them, double-click `tasks\uninstall_schedule.bat`. Logs go to
`reports\scheduler.log`. (Set the per-trade amount and goal in
`config.yaml` -> `simulation:`.)

## Paper trading (a real track record, zero money at risk)

Every pick the bot surfaces is logged to `reports/paper_trades.csv` as an OPEN
paper trade. On each later run the tracker pulls real intraday candles and
closes any trade that hit its target, its stop, or the end-of-day square-off -
just like the backtester, but **forward in time on live data**. Over days this
builds an honest record of how the strategy would actually have done.

```bash
python run.py paper     # scan, log new picks, resolve old ones, print track record
```

It runs automatically inside `email` and `auto` too, and a one-line summary
(win rate, expectancy, net P&L, open count) is printed at the top of every
email. Inspect or chart `reports/paper_trades.csv` any time.

> Use paper trading for at least a few weeks. If the live track record is not
> clearly positive after costs, do not move to real money - tune the strategy.

## Web dashboard

Prefer a website over the terminal? Launch the built-in dashboard:

```bash
python run.py web
```

Then open **http://127.0.0.1:5000** in your browser. The dashboard shows:

- **Momentum picks** as cards - each with *why to look at it*, *potential upside*
  (targets + %), and *risks & exit*, colour-coded long/short.
- **Paper track record** stat tiles (win rate, expectancy, net P&L) and an
  **equity-curve chart** of your closed paper trades.
- A **Scan now** button that runs a fresh scan on demand; the page auto-refreshes
  every few seconds.

It runs entirely on your own computer (localhost) - nothing is published to the
internet, and it reaches the live market data directly. To expose it on your
home network, run with host `0.0.0.0` (edit `src/web/app.py`) - but only do that
on a trusted network.

## End-of-day P&L email ("what would Rs 10,000 have done today")

After the close, the bot can email you a plain-English P&L: it resolves the
day's paper picks against real intraday data, splits a hypothetical
**Rs 10,000** (set by `simulation.hypothetical_capital` in `config.yaml`) equally
across them, and tells you how much you would have **gained or lost** - with a
per-pick breakdown.

```bash
python run.py eod        # builds + emails the EOD P&L (uses the same Gmail setup)
```

Every momentum pick also carries a Rs-amount projection inline: "On Rs 10,000:
~N shares -> +Rs X at T1, +Rs Y at T2", and the matching stop-out loss.

**Schedule it (Windows Task Scheduler)** - one daily trigger at **15:45 IST**,
weekdays, Program `python`, Arguments `run.py eod`, Start in your project folder.

## Universe (large + mid + small cap)

`config.yaml` -> `universe.preset` controls what gets scanned:

- `nifty50` / `nifty100` - large caps only
- `midcap` - ~50 liquid mid caps
- `smallcap` - ~45 liquid small caps
- `broad` *(default)* - all of the above (~195 names across every market cap)
- `custom` - your own list in `universe.custom_symbols`

A bigger universe finds more setups but each scan makes more data calls, so it's
slower and more likely to hit Yahoo's rate limits. If scans feel slow, switch to
`nifty100` or `midcap`.

## Project layout

```
config.yaml            all tunable settings
run.py                 entry point (once / live / symbol)
src/
  config.py            config loader + validation
  universe.py          Nifty 50 / 100 symbol lists
  utils.py             IST clock, market-hours logic, logging
  data/
    market_data.py     intraday OHLCV (yfinance + NSE fallback)
    fundamentals.py    Screener.in quality metrics (cached daily)
    sentiment.py       Google News headline sentiment
  analysis/
    indicators.py      all technical indicators
    signals.py         composite scoring + trend alignment
  risk/
    position_sizing.py ATR stops, targets, sizing, daily loss guard
  backtest.py          walk-forward backtester (no lookahead)
  scanner.py           runs the pipeline across the universe, ranks
  reporting.py         console + JSON/text reports
  notify/
    email_report.py    HTML/text momentum email + Gmail SMTP send
  paper/
    tracker.py         paper-trade ledger + live track record
  web/
    app.py             Flask dashboard server
    templates/index.html   single-page dashboard UI
```

## Tuning ideas (next steps)

- **Backtest is built in** (`python run.py backtest`). Use it to measure win
  rate and expectancy on your universe before trading, and after every tweak.
- Add **opening-range breakout** and **gap** strategies for the first 30 min.
- Swap free data for a **broker API** (Zerodha Kite, Upstox) for real-time
  ticks and cleaner fills once you're confident.

## Limitations (read them)

- Yahoo intraday data is delayed and occasionally gappy; treat levels as
  approximate. Cross-check on your broker before acting.
- Scraping Screener.in / Google News is best-effort and "fails open" — if a
  source is down, the bot continues without that input.
- The strategy is **unvalidated until you backtest it**. Run `python run.py
  backtest` first; do not deploy real capital until you've backtested AND
  paper-traded it. Free intraday history is limited to ~60 days on Yahoo.

---

*Decision-support software. Not investment advice. Markets carry risk of loss.*
