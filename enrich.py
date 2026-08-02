# rsi-screener v2.4.1 (2026-08-02)
"""
Sector / industry enrichment.

Classification is effectively static, so it's cached to disk permanently and only
new symbols cost a request. First full run is slow; every run after is nearly free.

Sources, in order of preference:
  1. FMP bulk screener  -- thousands of rows per call, needs FMP_API_KEY + plan access
  2. yfinance .info     -- one call per symbol, threaded and throttled, no key
"""

import os, sys, time, random
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd

CACHE = "data/sector_cache.csv"
COLS = ["vendor_symbol", "company_name", "sector", "industry", "country",
        "shares_outstanding", "source"]


def load_cache(path=CACHE):
    if os.path.exists(path):
        try:
            return pd.read_csv(path).drop_duplicates("vendor_symbol")
        except Exception:
            pass
    return pd.DataFrame(columns=COLS)


def save_cache(df, path=CACHE):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    df.drop_duplicates("vendor_symbol").sort_values("vendor_symbol").to_csv(path, index=False)


# ---------------------------------------------------------------- FMP bulk

def from_fmp(exchanges=("NASDAQ", "NYSE", "AMEX"), page_limit=1000, max_pages=20):
    """Bulk pull via FMP's company screener. Few calls, thousands of rows."""
    import requests
    key = os.environ.get("FMP_API_KEY")
    if not key:
        return pd.DataFrame(columns=COLS)

    rows = []
    for ex in exchanges:
        for page in range(max_pages):
            try:
                r = requests.get(
                    "https://financialmodelingprep.com/stable/company-screener",
                    params={"exchange": ex, "limit": page_limit,
                            "page": page, "apikey": key}, timeout=30)
                if r.status_code != 200:
                    print(f"  FMP {ex} page {page}: HTTP {r.status_code}", file=sys.stderr)
                    break
                batch = r.json()
            except Exception as ex_:
                print(f"  FMP {ex} page {page} failed: {ex_}", file=sys.stderr)
                break
            if not batch:
                break
            rows.extend(batch)
            if len(batch) < page_limit:
                break
            time.sleep(0.3)

    if not rows:
        return pd.DataFrame(columns=COLS)

    df = pd.DataFrame(rows)
    out = pd.DataFrame({
        "vendor_symbol": df.get("symbol"),
        "company_name": df.get("companyName"),
        "sector": df.get("sector"),
        "industry": df.get("industry"),
        "country": df.get("country"),
        "shares_outstanding": pd.NA,
        "source": "fmp",
    })
    return out.dropna(subset=["vendor_symbol"]).drop_duplicates("vendor_symbol")


# ---------------------------------------------------------------- yfinance

def _yf_one(sym, tries=4, base=1.0):
    """Retry with exponential backoff -- .info is throttled hard, and a single
    unretried rejection used to mean a permanent gap in the output."""
    import yfinance as yf
    import time as _t, random as _r
    for attempt in range(tries):
        try:
            info = yf.Ticker(sym).get_info()
            if info and (info.get("sector") or info.get("longName")):
                break
        except Exception:
            info = None
        _t.sleep(base * (2 ** attempt) * _r.uniform(0.7, 1.3))
    else:
        return None
    try:
        if not info:
            return None
        shares = (info.get("sharesOutstanding")
                  or info.get("impliedSharesOutstanding"))
        return {
            "vendor_symbol": sym,
            "company_name": info.get("longName") or info.get("shortName"),
            "sector": info.get("sector"),
            "industry": info.get("industry"),
            "country": info.get("country"),
            # Cached because it changes rarely. Market cap is recomputed each run
            # as shares * latest close, so it stays current for free.
            "shares_outstanding": shares,
            "source": "yfinance",
        }
    except Exception:
        return None


def from_yfinance(symbols, workers=3, delay=1.0, cache_path=CACHE, flush_every=200):
    """Per-symbol .info pull. Slow but keyless. Flushes to cache as it goes."""
    got, done = [], 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_yf_one, s): s for s in symbols}
        for fut in as_completed(futs):
            rec = fut.result()
            done += 1
            if rec:
                got.append(rec)
            if done % flush_every == 0:
                print(f"  enriched {done}/{len(symbols)}", file=sys.stderr, flush=True)
                # Flush early so a timeout doesn't discard the work.
                merged = pd.concat([load_cache(cache_path), pd.DataFrame(got)],
                                   ignore_index=True)
                save_cache(merged, cache_path)
            time.sleep(delay * random.uniform(0.7, 1.3) / max(workers, 1))
    return pd.DataFrame(got, columns=COLS) if got else pd.DataFrame(columns=COLS)


# ---------------------------------------------------------------- entry point

def enrich(symbols, cache_path=CACHE, use_fmp=True, use_yf=True, yf_workers=4):
    """Return a DataFrame of COLS covering as many of `symbols` as possible."""
    cache = load_cache(cache_path)
    have = set(cache.vendor_symbol) if not cache.empty else set()
    missing = [s for s in symbols if s not in have]
    print(f"sector cache: {len(have)} known, {len(missing)} to fetch", file=sys.stderr)

    if missing and use_fmp and os.environ.get("FMP_API_KEY"):
        print("  trying FMP bulk screener...", file=sys.stderr)
        bulk = from_fmp()
        if not bulk.empty:
            cache = pd.concat([cache, bulk], ignore_index=True).drop_duplicates("vendor_symbol")
            save_cache(cache, cache_path)
            have = set(cache.vendor_symbol)
            missing = [s for s in symbols if s not in have]
            print(f"  FMP covered {len(bulk)} symbols; {len(missing)} still missing",
                  file=sys.stderr)

    if missing and use_yf:
        print(f"  falling back to yfinance for {len(missing)}...", file=sys.stderr)
        got = from_yfinance(missing, workers=yf_workers, cache_path=cache_path)
        if not got.empty:
            cache = pd.concat([cache, got], ignore_index=True).drop_duplicates("vendor_symbol")
            save_cache(cache, cache_path)

    return cache[cache.vendor_symbol.isin(symbols)].copy()
