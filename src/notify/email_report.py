"""
Email reporting: momentum watchlist + end-of-day paper P&L, to your inbox.

Momentum email - each pick laid out as:
  1. WHY to look at it   2. UPSIDE (targets, %, and Rs-amount projection)
  3. RISKS (stop, Rs at risk, what invalidates it)

EOD email - "how much would Rs X have made or lost today" based on the day's
resolved paper trades.

Gmail SMTP over SSL. Creds from env vars (never stored in the repo):
    GMAIL_USER             sender gmail address
    GMAIL_APP_PASSWORD     16-char Google App Password
If creds are missing -> DRY RUN: writes an .html preview to reports/ instead.
"""
from __future__ import annotations

import os
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from ..utils import now_ist, get_logger
from ..reporting import DISCLAIMER, _report_dir

log = get_logger()


def _pct(a: float, b: float) -> float:
    return 0.0 if b == 0 else round((a - b) / b * 100, 2)


def budget_scenario(p, direction: str, capital: float, leverage: float = 1.0) -> dict:
    """What `capital` rupees would do on this single idea. Returns shares
    affordable and projected P&L at stop / T1 / T2 (no leverage by default)."""
    lev = max(1.0, float(leverage or 1))
    shares = int((capital * lev) // p.entry) if p.entry > 0 else 0
    sign = 1 if direction == "LONG" else -1
    return {
        "capital": round(capital), "leverage": lev, "shares": shares,
        "pnl_t1": round((p.target1 - p.entry) * shares * sign),
        "pnl_t2": round((p.target2 - p.entry) * shares * sign),
        "pnl_stop": round((p.stop_loss - p.entry) * shares * sign),
    }


def _enrich(r: dict, hypo_capital: float = 10000, leverage: float = 1.0) -> dict:
    s, p = r["signal"], r["plan"]
    long = s.direction == "LONG"
    up1 = abs(_pct(p.target1, p.entry))
    up2 = abs(_pct(p.target2, p.entry))
    risk_pct = abs(_pct(p.stop_loss, p.entry))
    b = budget_scenario(p, s.direction, hypo_capital, leverage)
    cap = b["capital"]

    upside = [
        f"Target 1: Rs {p.target1:.2f}  (+{up1:.2f}% / {p.reward_risk_t1}R) - book ~50% here",
        f"Target 2: Rs {p.target2:.2f}  (+{up2:.2f}% / {p.reward_risk_t2}R) - trail the rest",
        f"On Rs {cap:,}: ~{b['shares']} shares -> +Rs {b['pnl_t1']:,} at T1, "
        f"+Rs {b['pnl_t2']:,} at T2",
    ]
    risks = [
        f"Hard stop: Rs {p.stop_loss:.2f}  (-{risk_pct:.2f}%) - exit immediately if hit; "
        f"only Rs {p.rupees_at_risk:.0f} at risk on {p.quantity} shares",
        f"On Rs {cap:,}: a stop-out loses about Rs {abs(b['pnl_stop']):,}",
        f"Idea invalidates if price {'closes back below VWAP/breakout' if long else 'reclaims VWAP'} "
        f"or the {'up' if long else 'down'}trend breaks",
        ("Swing trade: hold days-to-weeks; exit if it closes back below the "
         "breakout / 20-EMA" if r.get("mode") == "swing"
         else "Intraday only: square off by ~15:20 IST even if neither stop nor target is hit"),
    ]
    if s.sentiment <= -0.2:
        risks.insert(1, f"News sentiment is negative ({s.sentiment:+.2f}) - headline risk")
    return {
        "symbol": s.symbol, "direction": s.direction, "score": s.score,
        "entry": p.entry, "why": s.reasons[:6], "upside": upside, "risks": risks,
        "fundamentals": r.get("fundamentals", {}), "sentiment": s.sentiment,
        "budget": b,
    }


# --------------------------------------------------------------------------- #
#  Momentum watchlist email
# --------------------------------------------------------------------------- #
def build_html(results: list, cfg: dict, track_line: str = "") -> str:
    ts = now_ist().strftime("%d %b %Y, %H:%M IST")
    capital = cfg["account"]["capital"]
    hypo = cfg.get("simulation", {}).get("hypothetical_capital", 10000)
    lev = cfg["account"].get("intraday_leverage", 1)
    track = (f'<div style="margin-top:6px;font-size:12px;background:#16213e;color:#fff;'
             f'padding:6px 10px;border-radius:6px;display:inline-block">{track_line}</div>'
             if track_line else "")
    head = (
        '<div style="font-family:Segoe UI,Arial,sans-serif;max-width:680px;margin:auto;color:#1a1a2e">'
        '<div style="background:#0f3460;color:#fff;padding:18px 22px;border-radius:10px 10px 0 0">'
        '<h2 style="margin:0">Momentum Watchlist</h2>'
        f'<div style="opacity:.85;font-size:13px">{ts} &nbsp;|&nbsp; capital Rs {capital:,} '
        f'&nbsp;|&nbsp; projections on Rs {hypo:,}</div>{track}</div>'
        '<div style="padding:8px 22px;background:#f7f7fb;border-radius:0 0 10px 10px">')

    if not results:
        body = ('<p style="padding:24px 0;color:#555">No momentum setups cleared the '
                'filters this scan. No trade is a valid decision - capital preserved.</p>')
        return head + body + _footer()

    cards = []
    for i, r in enumerate(results, 1):
        e = _enrich(r, hypo, lev)
        color = "#1b7d3f" if e["direction"] == "LONG" else "#b3261e"
        why = "".join(f"<li>{w}</li>" for w in e["why"])
        ups = "".join(f"<li>{u}</li>" for u in e["upside"])
        rsk = "".join(f"<li>{x}</li>" for x in e["risks"])
        fund = e["fundamentals"]
        fstr = (" | " + ", ".join(f"{k}={v}" for k, v in fund.items())) if fund else ""
        cards.append(
            '<div style="background:#fff;border:1px solid #e3e3ee;border-radius:10px;margin:14px 0;padding:16px">'
            '<div style="display:flex;justify-content:space-between;align-items:center">'
            f'<div style="font-size:18px;font-weight:700">#{i} &nbsp;{e["symbol"]}'
            f'<span style="background:{color};color:#fff;font-size:12px;padding:2px 8px;border-radius:6px;margin-left:6px">{e["direction"]}</span></div>'
            f'<div style="font-size:13px;color:#555">score <b>{e["score"]:.0f}/100</b> &nbsp;|&nbsp; sentiment {e["sentiment"]:+.2f}</div></div>'
            f'<div style="font-size:13px;color:#333;margin:6px 0 10px">Suggested entry near <b>Rs {e["entry"]:.2f}</b>{fstr}</div>'
            f'<div style="font-size:13px"><b style="color:#0f3460">Why look at it</b><ul style="margin:4px 0 10px">{why}</ul></div>'
            f'<div style="font-size:13px"><b style="color:#1b7d3f">Potential upside</b><ul style="margin:4px 0 10px">{ups}</ul></div>'
            f'<div style="font-size:13px"><b style="color:#b3261e">Risks &amp; exit</b><ul style="margin:4px 0 4px">{rsk}</ul></div>'
            '</div>')
    return head + "".join(cards) + _footer()


def _footer() -> str:
    return (f'<p style="font-size:11px;color:#888;line-height:1.5;margin-top:16px">{DISCLAIMER}<br>'
            'Generated automatically from free, delayed data. Verify every level on your '
            'broker before acting. This is not investment advice.</p></div></div>')


def build_text(results: list, cfg: dict, track_line: str = "") -> str:
    hypo = cfg.get("simulation", {}).get("hypothetical_capital", 10000)
    lev = cfg["account"].get("intraday_leverage", 1)
    lines = [f"MOMENTUM WATCHLIST - {now_ist().strftime('%d %b %Y %H:%M IST')}", ""]
    if track_line:
        lines += [track_line, ""]
    if not results:
        lines.append("No momentum setups cleared the filters this scan.")
    for i, r in enumerate(results, 1):
        e = _enrich(r, hypo, lev)
        lines += [f"#{i} {e['symbol']} [{e['direction']}]  score {e['score']:.0f}/100  "
                  f"entry ~Rs {e['entry']:.2f}",
                  "  WHY: " + "; ".join(e["why"]),
                  "  UPSIDE:"] + [f"    - {u}" for u in e["upside"]] + \
                 ["  RISKS:"] + [f"    - {x}" for x in e["risks"]] + [""]
    lines += ["", DISCLAIMER]
    return "\n".join(lines)


def send_report(results: list, cfg: dict, track_line: str = "") -> str:
    subject = (cfg.get("email", {}).get("subject_prefix", "Momentum Watchlist") +
               f" - {now_ist().strftime('%H:%M IST')} ({len(results)} picks)")
    html = build_html(results, cfg, track_line)
    text = build_text(results, cfg, track_line)
    return _deliver(cfg, subject, html, text, len(results))


# --------------------------------------------------------------------------- #
#  End-of-day paper P&L email
# --------------------------------------------------------------------------- #
def build_eod_html(cfg: dict, d: dict) -> str:
    ts = now_ist().strftime("%d %b %Y")
    cap = d["capital_per_trade"]
    pos = d["total_pnl"] >= 0
    color = "#1b7d3f" if pos else "#b3261e"
    verb = "GAINED" if pos else "LOST"
    head = (
        '<div style="font-family:Segoe UI,Arial,sans-serif;max-width:680px;margin:auto;color:#1a1a2e">'
        f'<div style="background:#0f3460;color:#fff;padding:18px 22px;border-radius:10px 10px 0 0">'
        f'<h2 style="margin:0">End-of-Day P&amp;L</h2>'
        f'<div style="opacity:.85;font-size:13px">{ts} &nbsp;|&nbsp; Rs {cap:,} in EACH pick</div></div>'
        '<div style="padding:16px 22px;background:#f7f7fb;border-radius:0 0 10px 10px">')

    if d["n"] == 0:
        body = ('<p style="color:#555">No picks were logged today, so there is nothing '
                'to mark. Run a scan during/after market hours first.</p>')
        return head + body + _eod_footer()

    goal = d.get("goal_pct", 10)
    peak = d.get("peak_pct", d["pct"])
    headline = (
        f'<div style="background:#fff;border:1px solid #e3e3ee;border-radius:10px;padding:18px;text-align:center">'
        f'<div style="font-size:13px;color:#555">Rs {cap:,} in each of today\'s {d["n"]} pick(s) '
        f'(Rs {d["invested"]:,} total) would have</div>'
        f'<div style="font-size:30px;font-weight:800;color:{color};margin:6px 0">{verb} Rs {abs(d["total_pnl"]):,.0f}</div>'
        f'<div style="font-size:14px;color:{color}">{d["pct"]:+.2f}% at close &nbsp;|&nbsp; '
        f'peak {peak:+.2f}% during the day &nbsp;|&nbsp; goal {goal}%</div></div>')
    review = d.get("review", "")
    review_block = (
        f'<div style="background:#eef3ff;border:1px solid #cdd9f5;border-radius:10px;padding:14px 16px;margin-top:14px">'
        f'<div style="font-size:13px;font-weight:700;color:#0f3460;margin-bottom:4px">What the bot learned today</div>'
        f'<div style="font-size:13px;color:#333;line-height:1.5">{review}</div></div>') if review else ""

    rows = "".join(
        f'<tr><td style="padding:7px 8px;border-top:1px solid #eee">{x["symbol"]}</td>'
        f'<td style="padding:7px 8px;border-top:1px solid #eee">{x["direction"]}</td>'
        f'<td style="padding:7px 8px;border-top:1px solid #eee;text-align:right">{x["entry"]:.2f}</td>'
        f'<td style="padding:7px 8px;border-top:1px solid #eee;text-align:right">{x["current"]:.2f}</td>'
        f'<td style="padding:7px 8px;border-top:1px solid #eee;text-align:right">{x["move_pct"]:+.2f}%</td>'
        f'<td style="padding:7px 8px;border-top:1px solid #eee;text-align:right;color:{"#1b7d3f" if x["pnl"]>=0 else "#b3261e"}">'
        f'Rs {x["pnl"]:+,.0f}</td></tr>'
        for x in d["rows"])
    table = (
        '<div style="background:#fff;border:1px solid #e3e3ee;border-radius:10px;padding:8px 14px;margin-top:14px">'
        '<div style="font-size:14px;font-weight:700;margin:6px 0">Per-pick (Rs %s each)</div>' % f"{cap:,}" +
        '<table style="width:100%;border-collapse:collapse;font-size:13px">'
        '<tr style="color:#777;font-size:11px;text-transform:uppercase">'
        '<td style="padding:4px 8px">Symbol</td><td style="padding:4px 8px">Dir</td>'
        '<td style="padding:4px 8px;text-align:right">Entry</td><td style="padding:4px 8px;text-align:right">Close</td>'
        '<td style="padding:4px 8px;text-align:right">Move</td><td style="padding:4px 8px;text-align:right">P&amp;L</td></tr>'
        f'{rows}</table></div>')
    return head + headline + review_block + table + _eod_footer()


def _eod_footer() -> str:
    return (f'<p style="font-size:11px;color:#888;line-height:1.5;margin-top:16px">{DISCLAIMER}<br>'
            'Paper result on free/delayed data, marked to the latest close. Not real trades, '
            'not investment advice. A 10% daily goal is aspirational - most days will be far less.</p></div></div>')


def build_eod_text(cfg: dict, d: dict) -> str:
    if d["n"] == 0:
        return (f"END-OF-DAY P&L - {now_ist().strftime('%d %b %Y')}\n\n"
                "No picks logged today. Nothing to mark.\n\n" + DISCLAIMER)
    verb = "GAINED" if d["total_pnl"] >= 0 else "LOST"
    cap = d["capital_per_trade"]
    lines = [f"END-OF-DAY P&L - {now_ist().strftime('%d %b %Y')}", "",
             f"Rs {cap:,} in each of {d['n']} pick(s) (Rs {d['invested']:,} total) would have",
             f"{verb} Rs {abs(d['total_pnl']):,.0f}  ({d['pct']:+.2f}% at close, "
             f"peak {d.get('peak_pct', d['pct']):+.2f}%; goal {d.get('goal_pct',10)}%).",
             ""]
    if d.get("review"):
        lines += ["WHAT THE BOT LEARNED TODAY:", "  " + d["review"], ""]
    lines += ["Per-pick:"]
    for x in d["rows"]:
        lines.append(f"  {x['symbol']:<12} {x['direction']:<5} entry {x['entry']:>9.2f} "
                     f"close {x['current']:>9.2f}  {x['move_pct']:+6.2f}%  Rs {x['pnl']:+,.0f}")
    lines += ["", DISCLAIMER]
    return "\n".join(lines)


def send_eod(cfg: dict, tune: dict = None) -> str:
    from ..paper import tracker
    from .. import learn
    d = tracker.marktomarket(cfg)
    peak = tracker.get_peak(cfg).get("peak_pct")
    d["peak_pct"] = peak if peak is not None else d["pct"]
    d["review"] = learn.summarize(tune) if tune else ""
    verb = "+" if d["total_pnl"] >= 0 else "-"
    subject = (f"EOD P&L {now_ist().strftime('%d %b')}: {verb}Rs {abs(d['total_pnl']):,.0f} "
               f"({d['pct']:+.1f}%, peak {d['peak_pct']:+.1f}%) on Rs {d['capital_per_trade']:,}/trade")
    html = build_eod_html(cfg, d)
    text = build_eod_text(cfg, d)
    return _deliver(cfg, subject, html, text, d["n"])


# --------------------------------------------------------------------------- #
#  Shared delivery (Gmail SMTP, with dry-run fallback)
# --------------------------------------------------------------------------- #
def _load_file_creds():
    """Read Gmail login from gmail_login.txt in the project root (line1 = email,
    line2 = 16-char app password). Robust for scheduled tasks where env vars
    may not be present. Returns (user, password) or ("","")."""
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    path = os.path.join(root, "gmail_login.txt")
    if not os.path.exists(path):
        return "", ""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = [ln.strip() for ln in fh if ln.strip() and not ln.strip().startswith("#")]
        if len(lines) >= 2:
            return lines[0], lines[1].replace(" ", "")  # strip spaces from app pw
    except Exception:
        pass
    return "", ""


def _deliver(cfg: dict, subject: str, html: str, text: str, n: int) -> str:
    ecfg = cfg.get("email", {})
    if not ecfg.get("enabled", False):
        log.info("Email disabled in config; skipping send.")
        return "disabled"
    recipient = ecfg.get("recipient")
    user = os.environ.get(ecfg.get("user_env", "GMAIL_USER"), "")
    pw = os.environ.get(ecfg.get("password_env", "GMAIL_APP_PASSWORD"), "")
    if not (user and pw):
        fu, fp = _load_file_creds()
        user = user or fu
        pw = pw or fp

    if not (user and pw):
        path = os.path.join(_report_dir(cfg),
                            f"email_preview_{now_ist().strftime('%Y%m%d_%H%M%S')}.html")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(html)
        log.warning("No GMAIL creds in env -> DRY RUN. Preview written: %s", path)
        return path

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = recipient
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))
    ctx = ssl.create_default_context()
    host = ecfg.get("smtp_host", "smtp.gmail.com")
    port = int(ecfg.get("smtp_port", 465))
    with smtplib.SMTP_SSL(host, port, context=ctx) as server:
        server.login(user, pw)
        server.sendmail(user, [recipient], msg.as_string())
    log.info("Email sent to %s.", recipient)
    return "sent"


# --------------------------------------------------------------------------- #
#  Pre-market email: top picks for the day + explicit EXIT PLAN
# --------------------------------------------------------------------------- #
def _exit_plan(r, e, cfg):
    p = r["plan"]
    long = e["direction"] == "LONG"
    swing = r.get("mode") == "swing"
    hold = cfg.get("swing", {}).get("max_hold_days", 15)
    return [
        f"Stop-loss (hard exit): Rs {p.stop_loss:.2f} - leave the moment it trades here.",
        f"Target 1: Rs {p.target1:.2f} - book ~50% and move stop to entry (risk-free).",
        f"Target 2: Rs {p.target2:.2f} - trail the rest; exit fully here.",
        (f"Time exit: close after ~{hold} trading days if neither target hits."
         if swing else "Time exit: square off by ~15:20 IST same day."),
        f"Invalidation: exit early if price {'closes back below the breakout / 20-EMA' if long else 'reclaims the breakdown level'}.",
    ]


def build_premarket_html(results, cfg):
    ts = now_ist().strftime("%d %b %Y")
    hypo = cfg.get("simulation", {}).get("hypothetical_capital", 10000)
    lev = cfg["account"].get("intraday_leverage", 1)
    head = (
        '<div style="font-family:Segoe UI,Arial,sans-serif;max-width:680px;margin:auto;color:#1a1a2e">'
        '<div style="background:#0f3460;color:#fff;padding:18px 22px;border-radius:10px 10px 0 0">'
        '<h2 style="margin:0">Pre-Market Top Picks</h2>'
        f'<div style="opacity:.85;font-size:13px">{ts} &nbsp;|&nbsp; momentum + news ranked &nbsp;|&nbsp; '
        f'projections on Rs {hypo:,}</div></div>'
        '<div style="padding:10px 22px;background:#f7f7fb;border-radius:0 0 10px 10px">')
    if not results:
        return head + '<p style="color:#555">No high-quality setups this morning.</p>' + _footer()
    cards = []
    for i, r in enumerate(results, 1):
        e = _enrich(r, hypo, lev)
        color = "#1b7d3f" if e["direction"] == "LONG" else "#b3261e"
        why = "".join(f"<li>{w}</li>" for w in e["why"][:4])
        ex = "".join(f"<li>{x}</li>" for x in _exit_plan(r, e, cfg))
        b = e["budget"]
        cards.append(
            '<div style="background:#fff;border:1px solid #e3e3ee;border-radius:10px;margin:12px 0;padding:16px">'
            f'<div style="font-size:18px;font-weight:700">#{i} &nbsp;{e["symbol"]}'
            f'<span style="background:{color};color:#fff;font-size:12px;padding:2px 8px;border-radius:6px;margin-left:6px">{e["direction"]}</span>'
            f'<span style="float:right;font-size:13px;color:#555">score {e["score"]:.0f}/100 | news {e["sentiment"]:+.2f}</span></div>'
            f'<div style="font-size:13px;color:#333;margin:6px 0 8px">Entry near <b>Rs {e["entry"]:.2f}</b> &nbsp;|&nbsp; '
            f'Rs {b["capital"]:,} buys ~{b["shares"]} sh (+Rs {b["pnl_t1"]:,} at T1 / +Rs {b["pnl_t2"]:,} at T2)</div>'
            f'<div style="font-size:13px"><b style="color:#0f3460">Why</b><ul style="margin:4px 0 8px">{why}</ul></div>'
            f'<div style="font-size:13px"><b style="color:#b3261e">EXIT PLAN (when to get out)</b><ul style="margin:4px 0 2px">{ex}</ul></div>'
            '</div>')
    return head + "".join(cards) + _footer()


def build_premarket_text(results, cfg):
    hypo = cfg.get("simulation", {}).get("hypothetical_capital", 10000)
    lev = cfg["account"].get("intraday_leverage", 1)
    lines = [f"PRE-MARKET TOP PICKS - {now_ist().strftime('%d %b %Y')}", ""]
    if not results:
        lines.append("No high-quality setups this morning.")
    for i, r in enumerate(results, 1):
        e = _enrich(r, hypo, lev)
        lines += [f"#{i} {e['symbol']} [{e['direction']}]  score {e['score']:.0f}  news {e['sentiment']:+.2f}",
                  f"   Entry ~Rs {e['entry']:.2f}",
                  "   WHY: " + "; ".join(e["why"][:4]),
                  "   EXIT PLAN:"] + [f"     - {x}" for x in _exit_plan(r, e, cfg)] + [""]
    lines += ["", DISCLAIMER]
    return "\n".join(lines)


def send_premarket(results, cfg):
    subject = f"Pre-Market Top {len(results)} - {now_ist().strftime('%d %b')} (with exit plans)"
    html = build_premarket_html(results, cfg)
    text = build_premarket_text(results, cfg)
    return _deliver(cfg, subject, html, text, len(results))


def send_test(cfg: dict) -> str:
    """Instant tiny email to verify delivery (no scanning)."""
    html = ('<div style="font-family:Segoe UI,Arial,sans-serif">'
            '<h2 style="color:#0f3460">Trading bot email test</h2>'
            '<p>If you can read this, your Gmail sending works. The 8:45 / 2-hourly / '
            '4 PM emails will land here automatically.</p></div>')
    text = "Trading bot email test - if you can read this, email works."
    return _deliver(cfg, "Trading bot - email test", html, text, 1)
