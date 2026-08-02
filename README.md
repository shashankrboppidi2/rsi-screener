# RSI Decile Screener — v2.4.1

**2026-07-31.** Supersedes any earlier download. See `CHANGELOG.md` for what changed.
Check with `python screener.py --version`.


Nightly oversold screen across a 7,417-name global universe, deduped against the CORE
watchlist. Runs on GitHub Actions overnight, commits results to `output/`, and emits a
TradingView-importable watchlist of the most oversold decile.

## What it does

1. Loads `data/universe_deduped.csv` (7,417 tickers; 6,818 with resolvable symbols).
2. Pulls ~2y of daily closes, batched and throttled.
3. Computes **Wilder RSI(14)** ("two-week") and **RSI(21)** ("one-month").
4. **Ranks the entire universe** into 10 deciles — decile 1 = lowest RSI = most oversold,
   decile 10 = highest = most overbought. Nothing is filtered out.
5. Attaches sector, industry, company name, and country.
6. Flags `hook_turning_up`: RSI today > yesterday **and** yesterday < the day before —
   the same shape as the QQQ LEAP entry trigger. Separates "turning" from "still falling."

## Weekly run on GitHub Actions

Push this repo, then **Settings > Actions > General > Workflow permissions > Read and write**.
The job runs Saturdays at 08:00 UTC and needs no local setup or API key.

Outputs land in `output/`, a dated copy goes to `history/`, and the pick table renders in the
Actions run summary. Trigger manually from the Actions tab to override trend weight, RSI
ceiling, market-cap floor, or picks per sector.

## Two universes

**A. The supplied 7,417-name global list** (`data/universe_deduped.csv`) — deduped against CORE.

**B. All US-listed stocks above a market-cap floor** — built fresh from NASDAQ Trader's public
symbol files. No API key, no subscription:

```bash
python build_universe.py --out data/us_universe.csv
python screener.py --universe data/us_universe.csv --min-mcap 50e6
```

`build_universe.py` pulls `nasdaqlisted.txt` and `otherlisted.txt` — the official daily listing
files — and strips test issues, ETFs, warrants, units, rights and preferred series. Share-class
dots become dashes for Yahoo (`BRK.B` -> `BRK-B`). Pass `--keep-etfs` to retain funds.

### How market cap is computed without a data subscription

Shares outstanding is cached alongside sector (it changes rarely). Market cap is then recomputed
each run as `shares_outstanding x latest_close` from prices already fetched — so it stays current
and costs zero extra requests per day.

The floor is applied **before** ranking, so deciles describe only the universe you asked for.
Names with no available share count are excluded and counted in the log rather than silently kept.

## Repairing enrichment gaps

`yfinance`'s `.info` endpoint is throttled far harder than the batched `download()`.
A full run typically resolves only 30-40% of sectors, with gaps spread evenly rather
than stopping at a point.

```bash
python patch_enrichment.py --csv output\rsi_ranked_all.csv --names-only   # seconds
python patch_enrichment.py --csv output\rsi_ranked_all.csv                # slow tier too
```

Company names come from NASDAQ Trader's listing files — two requests, complete coverage
of US listings, no rate limit. Sector and industry still need a per-symbol lookup; that
tier uses 3 workers, 1s base delay, four retries with backoff, and checkpoints
continuously, so Ctrl+C and resume is safe.

## Setup

1. Push these files to a repo.
2. **Settings → Actions → General → Workflow permissions** → *Read and write permissions*.
3. **Actions** tab → *RSI Decile Screener* → **Run workflow**.

No API key needed for the default Yahoo path. `FMP_API_KEY` is optional (`--source fmp`).

## Schedule

`0 2 * * 2-6` — 02:00 UTC, Tuesday through Saturday.

That's ~21:00–22:00 ET on the *previous* weekday, so each run lands a few hours after a
Mon–Fri US close. The UTC day sits one ahead of the trading day it covers, which is why the
cron is `2-6` and not `1-5`. GitHub's scheduler is best-effort and drifts under load.

## Rate throttling

Yahoo publishes no rate limit; it simply starts returning empty frames and 429s. Shared CI
egress IPs reach that point faster than a home connection. The fetcher handles it four ways:

- **Batching.** 50 symbols per request, not one. 6,818 names becomes ~137 requests, not 6,818.
- **Adaptive sleep.** Doubles on failure (2 → 4 → 8 → 16 … capped at 120s), decays 15% per
  success. Jittered so retries don't synchronise.
- **Checkpoints.** Every batch is pickled to `cache/` the moment it lands. A crash, a timeout,
  or a cancelled run never re-fetches what it already has.
- **Sweep.** Batches that fail all retries are re-attempted at the end in groups of 10, slower.

Tested against a mock at a 25% failure rate: all 500 symbols recovered, and a resume run made
zero network calls.

## Runtime

**GitHub-hosted runners hard-stop at 6 hours** — "no time constraint" isn't quite on offer.
The job is capped at 350 minutes with a 300-minute fetch budget, so it always reaches the
commit step with whatever it collected. If the budget runs out, checkpoints persist and a
rerun that day resumes mid-fetch.

In practice the full 6,818-name universe is ~137 requests, so expect well under an hour unless
Yahoo throttles hard. The 5-hour budget is headroom, not an estimate.

If you genuinely need unlimited runtime, a self-hosted runner removes the cap.

## Outputs (in `output/`)

| File | Contents |
|---|---|
| `rsi_ranked_all.csv` | **Every symbol**, ranked, with decile, percentile, sector, industry |
| `decile_summary.csv` | Count, RSI min/max/median and hook count per decile |
| `sector_by_decile.csv` | Sector × decile crosstab — where the pain is concentrated |
| `tv_all_deciles.txt` | TradingView import, all 10 deciles as sections |
| `tv_decile1_oversold.txt` | Decile 1 only, split HOOK CONFIRMED / WATCH |
| `unranked.csv` | Symbols with a reason they couldn't be ranked |
| `run_summary.json` | Counts, decile bounds, as-of date, staleness warning |
| `fetch_failures.txt` | Symbols that returned no data |

### Ranking, not screening

Liquidity (`avg_dollar_vol_20d`) is reported as a column and **never used to exclude**. Every
symbol that produces a valid RSI gets ranked. Sort or filter in Excel afterwards if you want to.

### Sector / industry

Classification is static, so it's cached to `data/sector_cache.csv` and committed back. The
first run pays the full cost; every run after only fetches genuinely new symbols — verified at
120 calls on a cold cache, 0 on a warm one, 2 when two new symbols appeared.

FMP's bulk screener covers thousands of rows per call when `FMP_API_KEY` is set and the plan
allows it. Otherwise it falls back to per-symbol `yfinance.Ticker.get_info()`, which is slow
but keyless. `--no-sectors` skips it entirely.

Also uploaded as a build artifact (90-day retention) on every run, including failures.

## A note on `pandas_ta`

`ta.rsi()` is Wilder's RSI, but it seeds with a plain EWM rather than an SMA of the first
`n` changes. Measured against a properly seeded implementation, it needs **~120 bars to
converge within 0.01**, with differences up to 30 RSI points early in the series.

It doesn't matter with a year of history. It matters a lot if you pull four months and read
`.iloc[-1]` — near an RSI 30 threshold, or at a decile boundary, that gap changes answers.
This repo uses its own seeded Wilder function, validated to 1e-10 against an independent
implementation, so the history length stops being a correctness question.

Also worth knowing: `yf.download()` now defaults to `auto_adjust=True` and
`multi_level_index=True`, so `df['Close']` returns a DataFrame rather than a Series even for a
single ticker. Code written against older yfinance breaks quietly here.

## Known limitations

**599 tickers excluded** for unresolvable symbols — KRX needs `.KS` (KOSPI) vs `.KQ` (KOSDAQ),
EURONEXT needs `.PA`/`.AS`/`.BR`/`.LS` by venue. Neither is inferable from the code alone.
Flagged `symbol_needs_manual_map=Y`. Fixing KRX alone recovers 471 names.

**Low RSI is not a buy signal.** Sorting 7,417 names by RSI surfaces the genuinely broken
first, because they belong there. Pair `hook_turning_up` with `pct_below_252d_high` and look
at the chart before acting.

**Non-US names may not be tradable** from your broker. Add an exchange filter if you don't
want decile 1 filling with names you can't buy.

**TradingView has no bulk alert import.** Watchlists import from the `.txt` fine, but alerts
are per-symbol via the UI. One Pine script scanning the watchlist beats hundreds of clicks.

## Local run

```bash
pip install -r requirements.txt
python screener.py --source yahoo --us-only --limit 100     # smoke test
python screener.py --source yahoo --min-dollar-vol 2000000  # full universe
```

`--limit` caps symbols, `--no-resume` ignores checkpoints, `--max-runtime-min` sets a budget.
