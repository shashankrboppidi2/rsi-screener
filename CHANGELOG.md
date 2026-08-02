# Changelog

## v2.4.1 — 2026-08-02

Pre-push dry run over a realistic 5,135-name universe. Two cosmetic bugs found and fixed.

### Fixed
- **Name tidying missed lowercase variants.** The listing files contain
  `"Common stock"` (lowercase s) as well as `"Common Stock"`, plus doubled spaces
  and trailing blanks. The old exact-suffix list left boilerplate on 144 of 5,088
  names. Now a case-insensitive, whitespace-normalised regex that loops to handle
  stacked suffixes (`"- Class A Common stock"`). Applied in all three scripts.
- **`mcap_band` column was 100% empty** and still written to the CSV — it is a
  passthrough from the older banded universe files. Dropped when unused, along
  with `mcap_decile_file`.

### Confirmed by the dry run
- 32 columns, no `_x`/`_y`/`__enr` merge debris, no empty passthroughs.
- MA nulls are expected and correct: 2.8% lack a 200d MA, 11.9% lack 3y/4y —
  these are recent listings, and `bars_available` explains each one.
- Downstream ranker unaffected: 33 picks across 11 sectors, funds excluded.


## v2.4.0 — 2026-07-31

### Added
- **`rank_opportunities.py`** — scores oversold names within each sector on two
  percentile-ranked factors: how depressed RSI is, and price vs the 4-year average.
  `--trend-weight` is the key dial (0 = deep value, 0.5 = balanced, 1 = momentum).
  Excludes closed-end funds by name; `Trust` is deliberately not matched so REITs survive.
- **`.github/workflows/weekly-screen.yml`** — full pipeline weekly on GitHub's runners.
  Builds the universe, screens, patches names, ranks by sector, archives a dated copy
  to `history/`, commits, and renders the pick table into the run summary.

### Changed
- Replaced the nightly `screener.yml` with the weekly workflow. Saturday 08:00 UTC.
- The stale-checkpoint sweep now uses a 3-day window (was 20h) to suit a weekly cadence.

### Verified
- Ranker reproduces the analysis run: 591 candidates from 4,047, 33 picks across 11 sectors.
- All three workflow input paths exercised: default (33 picks), deep value at
  `--max-rsi 25 --trend-weight 0` (12), momentum at `--trend-weight 1` (22).


## v2.3.1 — 2026-07-31

### Fixed
- **`company_name` came out empty on every row.** When the universe file already
  carried a `company_name` column (as `build_universe.py` output does) and the sector
  cache carried one too, the merge silently produced `company_name_x` / `company_name_y`.
  The follow-up "create the column if absent" step then saw no plain `company_name`
  and created an empty one. Overlapping columns are now suffixed explicitly and
  coalesced. Same bug would have affected `sector`, `industry` and `country` for any
  universe file supplying them.
- Listing boilerplate is now stripped on output: `"Watsco, Inc. Common Stock "` ->
  `"Watsco, Inc."`.
- Universe passthrough columns (`local_symbol`, `etf`, `us_listed`,
  `symbol_needs_manual_map`) are dropped from the result rather than trailing along.

### Note
The v2.1 retry fix worked: a real run resolved **4,030 of 4,047 sectors (99.6%)**,
against 38% before.


## v2.3.0 — 2026-07-31

Audit of a real 5,615-row `us_universe.csv` build. Warrants, units, rights and
preferreds were correctly stripped and symbols were clean, but three categories of
non-operating company slipped through — the exchange ETF flag doesn't catch them.

### Added
- **`--exclude funds spacs debt`** on `build_universe.py`.
  - `funds` — 274 closed-end funds (abrdn, Virtus, AllianceBernstein income funds).
    Matched on `Fund`, deliberately **not** `Trust`: REITs use "Trust" constantly, and
    a blunt filter would have wrongly dropped 137 of them.
  - `spacs` — 203 pre-deal shells. They sit near $10 with almost no volatility, so
    their RSI is close to meaningless.
  - `debt` — 3 exchange-listed debentures and baby bonds.
- **Name tidying in `build_universe.py`.** 4,122 of 5,615 names still carried
  exchange boilerplate; `tidy_name` was only in `patch_enrichment.py`.
  `"Alcoa Corporation Common Stock "` -> `"Alcoa Corporation"`.

### Verified against the real file
- Exclusions: 5,615 -> 5,135, with all 137 REITs preserved.
- Symbols clean: 0 dots, 0 `$`, 24 share-class dashes (correct for Yahoo),
  no 5-char W/U/R endings, no duplicates.


## v2.2.0 — 2026-07-31

### Added
- **52-week high/low distance** — `pct_from_52w_high` (renamed from
  `pct_below_252d_high`) and a new `pct_from_52w_low`.
- **Moving-average premiums** — `prem_ma_200d`, `prem_ma_1y`, `prem_ma_2y`,
  `prem_ma_3y`, `prem_ma_4y`, expressed as percent above/below each trailing SMA.
  The raw `ma_*` levels are included too.
- `bars_available` per symbol, and `ma_coverage` in `run_summary.json`.
- `--period` flag to control how much history is fetched.

### Changed
- **Fetch period 2y -> 5y.** A 3-year MA needs 756 trading bars and a 4-year needs
  ~1,008; the old 2-year pull (~504) would have left both columns entirely empty.
- A window is only computed when the full history exists. Short-history names get
  NaN rather than, say, a "4-year MA" quietly built from 18 months of data.

### Verified
- Premium arithmetic reconciles against recomputed values to 0.02pp across all
  five windows.
- `pct_from_52w_high` max = 0.0, `pct_from_52w_low` min = 0.0 — correct bounds.
- Mixed-history test: 200 names all resolved 200d/1y; only the 140 with full
  history resolved 2y/3y/4y; all 60 short names correctly NaN.


## v2.1.0 — 2026-07-31

Fixes enrichment coverage. A real 3,727-name run resolved only 38% of company
names, with failures spread evenly across the alphabet — i.e. throttling, not a
timeout.

### Added
- **`patch_enrichment.py`** — repairs an existing `rsi_ranked_all.csv` in place.
  Pulls company names in bulk from NASDAQ Trader (two requests, 100% coverage of
  US listings, no rate limit), then optionally fills sector/industry per symbol.
  `--names-only` skips the slow tier entirely.

### Fixed
- **`enrich.py` had no retry.** One throttled `.info` rejection meant a permanent
  gap. Now retries four times with exponential backoff and jitter.
- Default `.info` concurrency lowered from 4 workers / 0.35s to 3 / 1.0s. The old
  settings were fast enough to trigger throttling on almost two-thirds of calls.

### Verified
- Bulk name fill on the real 3,727-row output: 1,402 → 3,727 resolved, 0 missing.


## v2.0.0 — 2026-07-31

The current version. If you have an older download, this replaces it.

### Added
- **`build_universe.py`** — builds a US-listed universe from NASDAQ Trader's public
  listing files (`nasdaqlisted.txt`, `otherlisted.txt`). No API key. Strips test issues,
  ETFs, warrants, units, rights and preferred series; maps `BRK.B` → `BRK-B` for Yahoo.
- **`--min-mcap`** — market-cap floor, e.g. `--min-mcap 50e6`. Computed as
  `shares_outstanding × latest_close`, so it costs no extra requests per run and stays
  current. Applied *before* ranking, so deciles describe only the filtered universe.
- **`enrich.py`** — sector, industry, country, company name and shares outstanding,
  cached to `data/sector_cache.csv`. Only genuinely new symbols cost a request.
- **`fetch_yahoo.py`** — batched, adaptively throttled, resumable Yahoo fetcher.
- **`--sector-cache`** — the cache path was previously hardcoded.
- `--version`, `VERSION`, and this changelog.

### Changed
- **Ranks the full universe instead of screening it.** Every symbol with a valid RSI
  gets a decile. Liquidity is reported, never used to exclude. Names that genuinely
  can't be ranked go to `unranked.csv` with a stated reason.
- **Yahoo is the default source; FMP is optional.** FMP paths remain but are untested
  against a live key.
- Schedule moved to `0 2 * * 2-6` (overnight, Tue–Sat UTC, covering Mon–Fri US closes).
- Outputs reorganised: `rsi_ranked_all.csv`, `decile_summary.csv`, `sector_by_decile.csv`,
  `tv_all_deciles.txt`.

### Fixed
- `df.asof` collided with pandas' `DataFrame.asof()` method and crashed at the summary
  step — after a full fetch had already completed.
- Wilder RSI seeding hit a read-only numpy view.
- Checkpoints switched from parquet to pickle to drop the `pyarrow` dependency.
- Regex capture-group warning in the listing-file name filter.

### Verified
- RSI matches an independent Wilder implementation to 1e-10 over 400 bars; monotonic-up
  returns exactly 100, monotonic-down exactly 0.
- Throttle recovered 500/500 symbols at a simulated 25% failure rate; resume made zero
  network calls.
- Sector cache: 120 calls cold, 0 warm, 2 when two new symbols appeared.
- Market-cap floor: 300 → 214 names at $50M, minimum in output $50,744,406.

### Known gaps
- `pandas_ta`'s RSI needs ~120 bars to converge on true Wilder (up to 30 RSI points of
  divergence early in a series). This repo uses its own seeded implementation.
- 599 tickers in `universe_deduped.csv` have unresolvable symbols (KRX `.KS`/`.KQ`,
  EURONEXT `.PA`/`.AS`/`.BR`/`.LS`). Flagged `symbol_needs_manual_map=Y`.
- FMP code paths are present but that subscription has lapsed — untested.

---

## v1.x — 2026-07-30

Initial build. FMP-first, screened to decile 1 rather than ranking everything, no sector
data, no market-cap filter, fixed 7,417-name universe only.
