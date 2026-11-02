#!/usr/bin/env python3
"""
Sales-growth screener.

Every run (hourly via GitHub Actions):
  1. Reads SEC EDGAR's live feed of new 10-Q and 10-K filings.
  2. Pulls each filer's XBRL financial data from SEC and computes revenue growth,
     acceleraåçtion, gross margin, DSO, and RPO / deferred revenue trends.
  3. For companies that pass the growth bar, checks the revenue surprise
     against analyst consensus (Financial Modeling Prep).
  4. Once a day, checks flagged companies for upward analyst revenue revisions.
  5. Emails you any matches.

Run `python screener.py --test TICKER` to check a single company without
touching state or sending email.
"""

import datetime as dt
import json
import os
import re
import smtplib
import sys
import time
import xml.etree.ElementTree as ET
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

# ---------------------------------------------------------------------------
# Settings (change thresholds here)
# ---------------------------------------------------------------------------

# Minimum year-over-year revenue growth, tiered by quarterly revenue.
# $2.5B a quarter is roughly $10B a year; $250M a quarter is roughly $1B a year.
GROWTH_TIERS = [
    (2_500_000_000, 0.10),  # large companies: 10%+
    (250_000_000, 0.20),    # mid-size: 20%+
    (0, 0.30),              # small: 30%+
]
MIN_QUARTERLY_REVENUE = 25_000_000  # skip very small companies (noisy growth rates)
ACCELERATION_PP = 0.03     # YoY growth must rise 3+ percentage points vs. prior quarter
GROSS_MARGIN_UP_PP = 0.01  # margin up 1+ pp counts as a positive signal
GROSS_MARGIN_DROP_PP = 0.02  # margin down 2+ pp is a warning
DSO_RISE_LIMIT = 0.15      # DSO up more than 15% YoY is a warning
REVENUE_SURPRISE_MIN = 0.03  # beat consensus revenue by 3%+
REVISION_MIN = 0.03        # next-quarter revenue estimate up 3%+
MIN_SIGNALS = 1            # positive signals needed on top of the growth bar
EXCLUDE_ON_WARNINGS = True  # drop companies with margin or receivables warnings
WATCH_DAYS = 120           # how long flagged companies are tracked for revisions

FORMS = ["10-Q", "10-K"]
FEED_MAX_PAGES = 10        # up to 1,000 recent filings per form per run
FEED_LOOKBACK_HOURS = 48
MAX_FILINGS_PER_RUN = 150  # extra filings wait in the queue for the next run
MAX_ATTEMPTS = 6           # hours to wait for SEC to post a filing's XBRL data
STATE_FILE = os.environ.get("STATE_FILE", "state.json")

REVENUE_TAGS = [
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "SalesRevenueNet",
    "SalesRevenueGoodsNet",
]
COST_TAGS = ["CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold"]
DEFERRED_TAGS = ["ContractWithCustomerLiabilityCurrent", "DeferredRevenueCurrent"]

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

SEC_USER_AGENT = os.environ.get("SEC_USER_AGENT", "").strip()
FMP_API_KEY = os.environ.get("FMP_API_KEY", "").strip()
EMAIL_USER = os.environ.get("EMAIL_USER", "").strip()
EMAIL_APP_PASSWORD = os.environ.get("EMAIL_APP_PASSWORD", "").strip()
EMAIL_TO = os.environ.get("EMAIL_TO", "").strip() or EMAIL_USER
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")

ATOM = "{http://www.w3.org/2005/Atom}"
FMP_BASE = "https://financialmodelingprep.com/stable"

_sec = requests.Session()


def log(*args):
    print(*args, flush=True)


# ---------------------------------------------------------------------------
# SEC access
# ---------------------------------------------------------------------------

def sec_get(url):
    time.sleep(0.12)  # SEC allows at most 10 requests per second
    r = _sec.get(url, headers={"User-Agent": SEC_USER_AGENT,
                               "Accept-Encoding": "gzip, deflate"}, timeout=60)
    r.raise_for_status()
    return r


def load_tickers():
    data = sec_get("https://www.sec.gov/files/company_tickers.json").json()
    by_cik, by_ticker = {}, {}
    for row in data.values():
        cik, ticker = int(row["cik_str"]), row["ticker"].upper()
        by_cik.setdefault(cik, ticker)  # first listed share class wins
        by_ticker[ticker] = cik
    return by_cik, by_ticker


def recent_filings(form, seen, cutoff):
    """New filings of one form type from EDGAR's live feed, newest first."""
    found = []
    for page in range(FEED_MAX_PAGES):
        url = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent"
               f"&type={form}&company=&dateb=&owner=include"
               f"&start={page * 100}&count=100&output=atom")
        root = ET.fromstring(sec_get(url).content)
        entries = root.findall(f"{ATOM}entry")
        if not entries:
            break
        stop = False
        for e in entries:
            cat = e.find(f"{ATOM}category")
            if cat is None or cat.get("term") != form:
                continue  # skips amendments like 10-Q/A
            updated = e.findtext(f"{ATOM}updated", "")
            try:
                if dt.datetime.fromisoformat(updated) < cutoff:
                    stop = True
                    continue
            except ValueError:
                pass
            acc = e.findtext(f"{ATOM}id", "").split("accession-number=")[-1]
            title = e.findtext(f"{ATOM}title", "")
            m = re.search(r"\((\d{10})\)", title)
            if not acc or not m:
                continue
            if acc in seen:
                stop = True
                continue
            name = title.split(" - ", 1)[-1].rsplit(" (", 2)[0]
            found.append({"accession": acc, "cik": int(m.group(1)), "name": name,
                          "form": form, "updated": updated})
        if stop:
            break
    return found


def filing_url(cik, acc):
    return (f"https://www.sec.gov/Archives/edgar/data/{cik}/"
            f"{acc.replace('-', '')}/{acc}-index.htm")


# ---------------------------------------------------------------------------
# XBRL helpers
# ---------------------------------------------------------------------------

def d(s):
    return dt.date.fromisoformat(s[:10])


def durations(gaap, tag):
    """{(start, end): (value, filed, accession)}, keeping the latest filing."""
    items = gaap.get(tag, {}).get("units", {}).get("USD", [])
    out = {}
    for f in items:
        if "start" not in f:
            continue
        key = (f["start"], f["end"])
        if key not in out or f["filed"] > out[key][1]:
            out[key] = (f["val"], f["filed"], f.get("accn"))
    return out


def instants(gaap, tag):
    """{date: value} for point-in-time values (balance sheet items)."""
    items = gaap.get(tag, {}).get("units", {}).get("USD", [])
    out = {}
    for f in items:
        if "start" in f:
            continue
        if f["end"] not in out or f["filed"] > out[f["end"]][1]:
            out[f["end"]] = (f["val"], f["filed"])
    return {k: v[0] for k, v in out.items()}


def quarterly(dur):
    """{quarter_end: value}. Fourth quarters are derived as the full year
    minus the three reported quarters, since most companies don't report Q4."""
    quarters, annual = {}, []
    for (s, e), (v, _, _) in dur.items():
        days = (d(e) - d(s)).days
        if 80 <= days <= 100:
            quarters[e] = (s, v)
        elif 350 <= days <= 380:
            annual.append((s, e, v))
    reported = dict(quarters)
    for s, e, v in annual:
        if e in quarters:
            continue
        inside = [qv for qe, (qs, qv) in reported.items() if qs >= s and qe < e]
        if len(inside) == 3:
            quarters[e] = (None, v - sum(inside))
    return {e: v for e, (_, v) in quarters.items()}


def find_key(series, end, days_back, tol=20):
    target = d(end) - dt.timedelta(days=days_back)
    best = None
    for k in series:
        gap = abs((d(k) - target).days)
        if gap <= tol and (best is None or gap < best[0]):
            best = (gap, k)
    return best[1] if best else None


def growth(new, old):
    if new is None or old is None or old <= 0:
        return None
    return new / old - 1


def pct(x):
    return "n/a" if x is None else f"{x * 100:+.1f}%"


def money(x):
    if x is None:
        return "n/a"
    return f"${x / 1e9:.2f}B" if abs(x) >= 1e9 else f"${x / 1e6:.1f}M"


def growth_bar(quarterly_revenue):
    for floor, bar in GROWTH_TIERS:
        if quarterly_revenue >= floor:
            return bar
    return GROWTH_TIERS[-1][1]


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def analyze(facts, accession=None):
    """Returns (result, status). status is 'ok', 'pending' (SEC hasn't posted
    this filing's data yet), or a reason the company can't be screened."""
    gaap = facts.get("facts", {}).get("us-gaap", {})

    best = None
    for tag in REVENUE_TAGS:
        dur = durations(gaap, tag)
        series = quarterly(dur)
        if series:
            latest = max(series)
            if best is None or latest > best[2]:
                best = (tag, series, latest, dur)
    if best is None:
        if accession and not any(
                f.get("accn") == accession
                for t in gaap.values()
                for f in t.get("units", {}).get("USD", [])):
            return None, "pending"
        return None, "no quarterly revenue data"
    tag, rev, latest, dur = best
    if accession and not any(v[2] == accession for v in dur.values()):
        return None, "pending"

    R = rev[latest]
    py_end = find_key(rev, latest, 365)
    yoy = growth(R, rev.get(py_end)) if py_end else None
    if yoy is None:
        return None, "no prior-year quarter to compare"

    def yoy_at(end):
        p = find_key(rev, end, 365)
        return growth(rev[end], rev[p]) if p else None

    prev1 = find_key(rev, latest, 91)
    prev2 = find_key(rev, prev1, 91) if prev1 else None
    yoy1 = yoy_at(prev1) if prev1 else None
    yoy2 = yoy_at(prev2) if prev2 else None

    # Gross margin
    gp = quarterly(durations(gaap, "GrossProfit"))
    if not gp:
        for ctag in COST_TAGS:
            cost = quarterly(durations(gaap, ctag))
            if cost:
                gp = {e: rev[e] - c for e, c in cost.items() if e in rev}
                break
    gm = gp[latest] / R if latest in gp and R > 0 else None
    gm_py = gp[py_end] / rev[py_end] if py_end in gp and rev[py_end] > 0 else None

    # Days sales outstanding
    ar = instants(gaap, "AccountsReceivableNetCurrent")
    ar_now = ar.get(latest)
    ar_py_key = find_key(ar, latest, 365, tol=10)
    dso = ar_now / R * 91 if ar_now and R > 0 else None
    dso_py = (ar[ar_py_key] / rev[py_end] * 91
              if ar_py_key and rev[py_end] > 0 else None)

    # Contracted revenue: RPO first, deferred revenue as a fallback
    def instant_growth(tags):
        for t in tags:
            series = instants(gaap, t)
            if latest in series:
                k = find_key(series, latest, 365, tol=10)
                if k:
                    return t, growth(series[latest], series[k])
        return None, None

    rpo_tag, rpo_g = instant_growth(["RevenueRemainingPerformanceObligation"])
    def_tag, def_g = instant_growth(DEFERRED_TAGS)

    bar = growth_bar(R)
    signals, warnings = [], []

    if yoy1 is not None and yoy - yoy1 >= ACCELERATION_PP:
        if yoy2 is not None and yoy1 > yoy2:
            signals.append(f"Growth accelerating 3 quarters: {pct(yoy2)} → {pct(yoy1)} → {pct(yoy)}")
        else:
            signals.append(f"Growth accelerating: {pct(yoy1)} → {pct(yoy)}")
    if rpo_g is not None and rpo_g > yoy:
        signals.append(f"RPO up {pct(rpo_g)}, faster than revenue")
    elif def_g is not None and def_g > yoy:
        signals.append(f"Deferred revenue up {pct(def_g)}, faster than revenue")
    if gm is not None and gm_py is not None:
        change = gm - gm_py
        if change >= GROSS_MARGIN_UP_PP:
            signals.append(f"Gross margin up {change * 100:.1f} pp to {gm * 100:.1f}%")
        elif -change >= GROSS_MARGIN_DROP_PP:
            warnings.append(f"Gross margin down {-change * 100:.1f} pp to {gm * 100:.1f}%")
    dso_change = growth(dso, dso_py)
    if dso_change is not None and dso_change > DSO_RISE_LIMIT:
        warnings.append(f"DSO up {pct(dso_change)} ({dso_py:.0f} → {dso:.0f} days)")

    return {
        "revenue_tag": tag,
        "period_end": latest,
        "revenue": R,
        "yoy": yoy,
        "yoy_prev": yoy1,
        "growth_bar": bar,
        "passes_growth_bar": R >= MIN_QUARTERLY_REVENUE and yoy >= bar,
        "gross_margin": gm,
        "dso": dso,
        "signals": signals,
        "warnings": warnings,
    }, "ok"


# ---------------------------------------------------------------------------
# Financial Modeling Prep (consensus estimates)
# ---------------------------------------------------------------------------

def fmp_get(path, **params):
    if not FMP_API_KEY:
        return None
    params["apikey"] = FMP_API_KEY
    try:
        r = requests.get(f"{FMP_BASE}/{path}", params=params, timeout=30)
        if r.status_code != 200:
            log(f"  FMP {path} returned {r.status_code}: {r.text[:200]}")
            return None
        return r.json()
    except Exception as exc:  # network errors shouldn't stop the run
        log(f"  FMP {path} failed: {exc}")
        return None


def revenue_surprise(ticker, period_end):
    rows = fmp_get("earnings", symbol=ticker)
    if not isinstance(rows, list):
        return None
    pe = d(period_end)
    for row in sorted(rows, key=lambda r: r.get("date", ""), reverse=True):
        actual, est, date = (row.get("revenueActual"), row.get("revenueEstimated"),
                             row.get("date"))
        if actual and est and date and pe <= d(date) <= pe + dt.timedelta(days=120):
            return actual / est - 1
    return None


def next_quarter_estimate(ticker):
    rows = fmp_get("analyst-estimates", symbol=ticker, period="quarter",
                   page=0, limit=12)
    if not isinstance(rows, list):
        return None
    today = dt.date.today().isoformat()
    future = [r for r in rows if r.get("date", "")[:10] > today and r.get("revenueAvg")]
    if not future:
        return None
    r = min(future, key=lambda r: r["date"])
    return r["date"][:10], r["revenueAvg"]


def add_to_watch(state, ticker, name):
    today = dt.date.today().isoformat()
    w = state["watch"].get(ticker)
    if w:
        w["flagged"] = today
        return
    entry = {"name": name, "flagged": today, "last_checked": today}
    est = next_quarter_estimate(ticker)
    if est:
        entry.update(period=est[0], baseline=est[1], baseline_date=today,
                     alerted=False)
    state["watch"][ticker] = entry


def check_revisions(state):
    """Once a day, look for upward revisions to next-quarter revenue estimates."""
    alerts = []
    if not FMP_API_KEY:
        return alerts
    today = dt.date.today()
    for ticker, w in list(state["watch"].items()):
        if (today - d(w["flagged"])).days > WATCH_DAYS:
            del state["watch"][ticker]
            continue
        if w.get("last_checked") == today.isoformat():
            continue
        w["last_checked"] = today.isoformat()
        est = next_quarter_estimate(ticker)
        if not est:
            continue
        period, value = est
        stale = "baseline_date" not in w or (today - d(w["baseline_date"])).days > 90
        if w.get("period") != period or stale or not w.get("baseline"):
            w.update(period=period, baseline=value,
                     baseline_date=today.isoformat(), alerted=False)
            continue
        change = value / w["baseline"] - 1
        if change >= REVISION_MIN and not w.get("alerted"):
            w["alerted"] = True
            alerts.append({"ticker": ticker, "name": w.get("name", ""),
                           "period": period, "from": w["baseline"], "to": value,
                           "change": change, "since": w["baseline_date"]})
    return alerts


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def build_email(matches, revisions):
    parts = []
    if matches:
        parts.append(f"{len(matches)} new filing{'s' if len(matches) != 1 else ''}")
    if revisions:
        parts.append(f"{len(revisions)} estimate revision{'s' if len(revisions) != 1 else ''}")
    subject = "Sales-growth screener: " + ", ".join(parts)

    html = ["<div style='font-family:Arial,sans-serif;font-size:14px'>"]
    text = []
    if matches:
        html.append("<h3>New filings that meet the criteria</h3>")
        for m in sorted(matches, key=lambda m: (-len(m["signals"]), -m["yoy"])):
            surprise = (f" · Revenue vs. consensus: {pct(m['surprise'])}"
                        if m.get("surprise") is not None else "")
            html.append(
                f"<p><b>{m['ticker']}</b> — {m['name']} ({m['form']}, quarter ended "
                f"{m['period_end']})<br>Revenue {money(m['revenue'])}, "
                f"YoY {pct(m['yoy'])} (bar {m['growth_bar'] * 100:.0f}%), "
                f"prior quarter {pct(m['yoy_prev'])}{surprise}<br>"
                + "".join(f"✔ {s}<br>" for s in m["signals"])
                + "".join(f"⚠ {w}<br>" for w in m["warnings"])
                + f"<a href='{m['url']}'>Filing on SEC EDGAR</a></p>")
            text.append(f"{m['ticker']} {m['name']}: YoY {pct(m['yoy'])}; "
                        + "; ".join(m["signals"] + m["warnings"]) + f" {m['url']}")
    if revisions:
        html.append("<h3>Upward analyst revenue revisions (previously flagged)</h3>")
        for r in revisions:
            line = (f"{r['ticker']} — {r['name']}: estimate for quarter ending "
                    f"{r['period']} up {pct(r['change'])} since {r['since']} "
                    f"({money(r['from'])} → {money(r['to'])})")
            html.append(f"<p>{line}</p>")
            text.append(line)
    html.append("<p style='color:#777;font-size:12px'>Automated screen of SEC "
                "filings. Thresholds are rules of thumb; verify before acting.</p></div>")
    return subject, "\n".join(html), "\n".join(text)


def send_email(subject, html, text):
    if DRY_RUN:
        log(f"\n[DRY RUN] {subject}\n{text}\n")
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, EMAIL_USER, EMAIL_TO
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as s:
        s.login(EMAIL_USER, EMAIL_APP_PASSWORD)
        s.sendmail(EMAIL_USER, [a.strip() for a in EMAIL_TO.split(",")], msg.as_string())
    log(f"Email sent: {subject}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def load_state():
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    for key in ("seen", "queue", "watch"):
        state.setdefault(key, {})
    return state


def save_state(state):
    cutoff = (dt.date.today() - dt.timedelta(days=21)).isoformat()
    state["seen"] = {k: v for k, v in state["seen"].items() if v >= cutoff}
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1, sort_keys=True)


def screen(filing, ticker, facts):
    result, status = analyze(facts, filing["accession"])
    if status != "ok":
        return None, status
    if not result["passes_growth_bar"]:
        return None, f"below growth bar ({pct(result['yoy'])})"
    surprise = revenue_surprise(ticker, result["period_end"])
    result["surprise"] = surprise
    if surprise is not None and surprise >= REVENUE_SURPRISE_MIN:
        result["signals"].append(f"Revenue beat consensus by {pct(surprise)}")
    if EXCLUDE_ON_WARNINGS and result["warnings"]:
        return None, "quality warning: " + "; ".join(result["warnings"])
    if len(result["signals"]) < MIN_SIGNALS:
        return None, f"growth {pct(result['yoy'])} but no confirming signals"
    result.update(ticker=ticker, name=filing["name"], form=filing["form"],
                  url=filing_url(filing["cik"], filing["accession"]))
    return result, "MATCH"


def run():
    state = load_state()
    try:
        now = dt.datetime.now(dt.timezone.utc)
        cutoff = now - dt.timedelta(hours=FEED_LOOKBACK_HOURS)
        for form in FORMS:
            for f in recent_filings(form, state["seen"], cutoff):
                state["seen"][f["accession"]] = now.date().isoformat()
                state["queue"][f["accession"]] = {**f, "attempts": 0}
        log(f"{len(state['queue'])} filings in queue")

        by_cik, _ = load_tickers()
        matches = []
        queue = sorted(state["queue"].values(), key=lambda f: f["updated"])
        for f in queue[:MAX_FILINGS_PER_RUN]:
            acc = f["accession"]
            ticker = by_cik.get(f["cik"])
            if not ticker:
                del state["queue"][acc]
                continue
            try:
                facts = sec_get("https://data.sec.gov/api/xbrl/companyfacts/"
                                f"CIK{f['cik']:010d}.json").json()
            except requests.HTTPError as exc:
                if exc.response is not None and exc.response.status_code == 404:
                    del state["queue"][acc]
                    continue
                raise
            result, status = screen(f, ticker, facts)
            if status == "pending":
                f["attempts"] += 1
                state["queue"][acc] = f
                if f["attempts"] >= MAX_ATTEMPTS:
                    del state["queue"][acc]
                    log(f"  {ticker}: gave up waiting for XBRL data")
                continue
            del state["queue"][acc]
            log(f"  {ticker} {f['form']}: {status}")
            if result:
                matches.append(result)
                add_to_watch(state, ticker, f["name"])

        revisions = check_revisions(state)
        if matches or revisions:
            send_email(*build_email(matches, revisions))
        else:
            log("No matches this run.")
    finally:
        save_state(state)


def test(ticker):
    """Screen one company's latest filing, ignoring state."""
    _, by_ticker = load_tickers()
    cik = by_ticker.get(ticker.upper())
    if not cik:
        sys.exit(f"Ticker {ticker} not found in SEC's ticker list.")
    facts = sec_get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json").json()
    result, status = analyze(facts)
    if status != "ok":
        sys.exit(f"{ticker}: {status}")
    surprise = revenue_surprise(ticker.upper(), result["period_end"])
    log(json.dumps({**result, "revenue_surprise": surprise,
                    "next_quarter_estimate": next_quarter_estimate(ticker.upper())},
                   indent=2, default=str))


if __name__ == "__main__":
    if not SEC_USER_AGENT:
        sys.exit("Set SEC_USER_AGENT, e.g. 'Jane Doe jane@example.com' (SEC requires it).")
    if len(sys.argv) == 3 and sys.argv[1] == "--test":
        test(sys.argv[2])
    else:
        if not DRY_RUN and not (EMAIL_USER and EMAIL_APP_PASSWORD):
            sys.exit("Set EMAIL_USER and EMAIL_APP_PASSWORD, or DRY_RUN=1.")
        run()
