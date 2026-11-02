# Earnings-Alerts

# Sales-growth screener

Checks SEC EDGAR every hour for new 10-Q and 10-K filings from US-listed companies,
screens them for rapid sales growth, and emails you the matches.

## What it checks

A company is flagged when its latest quarter clears the **growth bar** and shows
at least one **confirming signal**, with no **quality warnings**.

| Check | Rule (default) | Source |
|---|---|---|
| Growth bar | YoY revenue growth ≥ 10% (≥ $2.5B/quarter revenue), ≥ 20% (≥ $250M), ≥ 30% (smaller); minimum $25M/quarter | SEC |
| Signal: acceleration | YoY growth up 3+ pp from prior quarter | SEC |
| Signal: contracted revenue | RPO (or deferred revenue) growing faster than revenue | SEC |
| Signal: gross margin | Up 1+ pp year over year | SEC |
| Signal: revenue surprise | Revenue beat consensus by 3%+ | FMP |
| Warning: gross margin | Down 2+ pp year over year | SEC |
| Warning: receivables | Days sales outstanding up more than 15% | SEC |
| Separate alert: estimate revisions | Next-quarter consensus revenue up 3%+, checked daily for 120 days after a company is flagged | FMP |

All thresholds are at the top of `screener.py`.

**Not covered:** guidance raises (they're in press-release text, not structured data),
backlog and book-to-bill (reported inconsistently), banks and insurers (they don't
report a standard revenue line), and foreign filers (20-F/6-K).

**Timing:** companies usually announce earnings a few days before filing the 10-Q,
so alerts arrive after the initial earnings-day move.

## Setup

### 1. Gather three credentials
- **SEC identity:** SEC requires a name and email in every request, e.g. `Jane Doe jane@example.com`.
- **Gmail app password:** Turn on 2-Step Verification for the Google account, then create an
  app password at myaccount.google.com → Security → App passwords. Use the 16-character code,
  not your regular password. (Other email providers work too; set `SMTP_HOST`/`SMTP_PORT`.)
- **Financial Modeling Prep API key:** Sign up at financialmodelingprep.com. Make sure your plan
  includes the *Earnings* and *Analyst Estimates* endpoints. Without a key the screener still
  runs, minus the surprise and revision checks.

### 2. Create the repository
1. Create a new GitHub repository. **Public** is recommended: Actions minutes are unlimited for
   public repos, while private repos get 2,000 free minutes a month and this job can use most of
   that during earnings season. Your credentials stay private either way (they're stored as
   secrets), and the only file the job writes is `state.json`, a list of filing numbers and tickers.
2. Upload `screener.py`, `requirements.txt`, and `.github/workflows/screener.yml`, keeping that
   folder path exactly.

### 3. Add secrets
In the repository, go to **Settings → Secrets and variables → Actions → New repository secret**
and add:

| Name | Value |
|---|---|
| `SEC_USER_AGENT` | `Your Name you@example.com` |
| `EMAIL_USER` | Gmail address that sends the alerts |
| `EMAIL_APP_PASSWORD` | The 16-character app password |
| `EMAIL_TO` | Where alerts go (optional; defaults to `EMAIL_USER`; comma-separate several) |
| `FMP_API_KEY` | Your FMP key |

### 4. Turn it on
Open the **Actions** tab, enable workflows if prompted, select **Sales growth screener**, and click
**Run workflow** to test it. After that it runs every hour on its own. You'll get an email only
when something matches. GitHub emails you if a run fails.

## Testing locally

```bash
pip install requests
export SEC_USER_AGENT="Your Name you@example.com"
export FMP_API_KEY=...
python screener.py --test NVDA      # screen one company, no email, no state changes
DRY_RUN=1 python screener.py        # full run, prints matches instead of emailing
```

## Notes
- The first run picks up filings from the last 48 hours. Each run screens up to 150 filings;
  any extras wait for the next run.
- If SEC hasn't posted a filing's financial data yet, the screener retries for up to 6 hours.
- GitHub pauses scheduled workflows in repos with no activity for 60 days. The hourly state
  commits normally prevent that, but if it ever stops, re-enable it from the Actions tab.
