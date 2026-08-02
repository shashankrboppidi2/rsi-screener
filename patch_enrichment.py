#!/usr/bin/env python3
# rsi-screener v2.4.1 (2026-08-02)
"""
Patch company_name / sector / industry into an existing rsi_ranked_all.csv.

Why this exists: yfinance's .info endpoint is rate-limited far more aggressively
than the batched download() call. A 3,700-symbol run typically resolves only
30-40% of names, and the failures are spread evenly rather than stopping at a
point -- so the gaps are throttling, not a timeout.

Two tiers:

  1. company_name comes from NASDAQ Trader's listing files. Two HTTP requests,
     100% coverage of US listings, no rate limit. This alone fills the name
     column completely.

  2. sector / industry still need a per-symbol lookup. This does it with heavy
     retries, few workers, and long jittered sleeps, checkpointing continuously
     so you can stop and resume freely.

Usage:
  python patch_enrichment.py --csv output/rsi_ranked_all.csv --names-only
  python patch_enrichment.py --csv output/rsi_ranked_all.csv
  python patch_enrichment.py --csv output/rsi_ranked_all.csv --workers 2 --delay 1.5
"""

import re, argparse, io, os, sys, time, random
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd

NASDAQ = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
CACHE = "data/sector_cache.csv"


# ---------------------------------------------------------------- tier 1: names

def fetch_listing_names():
    """All US listed security names in two requests. No key, no rate limit."""
    import requests
    frames = []
    for url, symcol in ((NASDAQ, "Symbol"), (OTHER, "ACT Symbol")):
        try:
            r = requests.get(url, timeout=60,
                             headers={"User-Agent": "Mozilla/5.0 (enrichment)"})
            r.raise_for_status()
        except Exception as ex:
            print(f"  {url.rsplit('/', 1)[-1]} failed: {ex}", file=sys.stderr)
            continue
        text = "\n".join(ln for ln in r.text.splitlines()
                         if ln.strip() and not ln.lower().startswith("file creation"))
        df = pd.read_csv(io.StringIO(text), sep="|", dtype=str)
        df.columns = [c.strip() for c in df.columns]
        if symcol not in df.columns:
            continue
        out = pd.DataFrame({
            "vendor_symbol": df[symcol].str.strip().str.replace(".", "-", regex=False),
            "listing_name": df["Security Name"].str.strip(),
        })
        frames.append(out)
        print(f"  {url.rsplit('/', 1)[-1]}: {len(out)} names", file=sys.stderr)

    if not frames:
        return pd.DataFrame(columns=["vendor_symbol", "listing_name"])
    return pd.concat(frames, ignore_index=True).drop_duplicates("vendor_symbol")


def tidy_name(s):
    """Strip exchange boilerplate: 'Acme Corp - Common Stock' -> 'Acme Corp'.

    Case-insensitive and whitespace-tolerant: the listing files are inconsistent
    ('Common stock', 'Common Stock', doubled spaces, trailing blanks).
    """
    if not isinstance(s, str):
        return s
    s = re.sub(r"\s+", " ", s).strip()
    pat = (r"\s*[-,]?\s*(?:class\s+[a-z]\s+)?"
           r"(?:common\s+stock|common\s+shares?|ordinary\s+shares?|"
           r"American\s+Depositary\s+Shares?)\s*$")
    prev = None
    while prev != s:                      # handles stacked suffixes
        prev = s
        s = re.sub(pat, "", s, flags=re.I).strip()
    return s.strip().rstrip(",-").strip()




# ---------------------------------------------------------------- tier 2: sectors

def _one(sym, tries=4, base=1.0):
    import yfinance as yf
    for attempt in range(tries):
        try:
            info = yf.Ticker(sym).get_info()
            if info and (info.get("sector") or info.get("longName")):
                return {
                    "vendor_symbol": sym,
                    "company_name": info.get("longName") or info.get("shortName"),
                    "sector": info.get("sector"),
                    "industry": info.get("industry"),
                    "country": info.get("country"),
                    "shares_outstanding": (info.get("sharesOutstanding")
                                           or info.get("impliedSharesOutstanding")),
                    "source": "yfinance",
                }
        except Exception:
            pass
        # Exponential backoff with jitter -- this is the bit the old code lacked.
        time.sleep(base * (2 ** attempt) * random.uniform(0.7, 1.3))
    return None


def fetch_sectors(symbols, cache_path=CACHE, workers=3, delay=1.0, flush=100):
    cache = (pd.read_csv(cache_path) if os.path.exists(cache_path)
             else pd.DataFrame(columns=["vendor_symbol"]))
    known = set(cache.vendor_symbol) if len(cache) else set()
    todo = [s for s in symbols if s not in known]
    print(f"sector cache: {len(known)} known, {len(todo)} to fetch", file=sys.stderr)
    if not todo:
        return cache

    got, done = [], 0
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(_one, s, base=delay): s for s in todo}
            for fut in as_completed(futs):
                rec = fut.result()
                done += 1
                if rec:
                    got.append(rec)
                if done % flush == 0:
                    hit = len(got) / done * 100
                    print(f"  {done}/{len(todo)}  resolved {len(got)} ({hit:.0f}%)",
                          file=sys.stderr, flush=True)
                    cache = _flush(cache, got, cache_path); got = []
                time.sleep(delay * random.uniform(0.5, 1.5) / max(workers, 1))
    except KeyboardInterrupt:
        print("\ninterrupted -- flushing what we have", file=sys.stderr)

    return _flush(cache, got, cache_path)


def _flush(cache, got, path):
    if got:
        cache = pd.concat([cache, pd.DataFrame(got)], ignore_index=True)
        cache = cache.drop_duplicates("vendor_symbol")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    cache.to_csv(path, index=False)
    return cache


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="output/rsi_ranked_all.csv")
    ap.add_argument("--out", default=None, help="defaults to overwriting --csv")
    ap.add_argument("--cache", default=CACHE)
    ap.add_argument("--names-only", action="store_true",
                    help="bulk names only; skip the slow per-symbol sector lookup")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--delay", type=float, default=1.0)
    args = ap.parse_args()

    df = pd.read_csv(args.csv)
    out_path = args.out or args.csv
    before = {c: df[c].notna().sum() for c in ("company_name", "sector", "industry")
              if c in df.columns}
    print(f"loaded {len(df)} rows from {args.csv}", file=sys.stderr)

    # --- tier 1 ---
    print("\nfetching listing names (bulk)...", file=sys.stderr)
    names = fetch_listing_names()
    if not names.empty:
        names["listing_name"] = names["listing_name"].map(tidy_name)
        df = df.merge(names, on="vendor_symbol", how="left")
        if "company_name" not in df.columns:
            df["company_name"] = pd.NA
        df["company_name"] = df["company_name"].fillna(df["listing_name"])
        df = df.drop(columns=["listing_name"])

    # --- tier 2 ---
    if not args.names_only:
        need = df[df["sector"].isna()]["vendor_symbol"].dropna().tolist() \
            if "sector" in df.columns else df["vendor_symbol"].tolist()
        if need:
            print(f"\nfetching sector/industry for {len(need)} symbols "
                  f"({args.workers} workers, {args.delay}s base delay)...", file=sys.stderr)
            cache = fetch_sectors(need, args.cache, args.workers, args.delay)
            cols = [c for c in ("vendor_symbol", "sector", "industry", "country",
                                "shares_outstanding") if c in cache.columns]
            add = cache[cols].rename(columns={c: f"{c}__new" for c in cols
                                              if c != "vendor_symbol"})
            df = df.merge(add, on="vendor_symbol", how="left")
            for c in ("sector", "industry", "country", "shares_outstanding"):
                if f"{c}__new" in df.columns:
                    if c not in df.columns:
                        df[c] = pd.NA
                    df[c] = df[c].fillna(df[f"{c}__new"])
                    df = df.drop(columns=[f"{c}__new"])

    df.to_csv(out_path, index=False)

    print(f"\nwrote {out_path}", file=sys.stderr)
    print(f"{'column':16s} {'before':>8s} {'after':>8s} {'of':>8s}")
    for c in ("company_name", "sector", "industry"):
        if c in df.columns:
            print(f"{c:16s} {before.get(c, 0):8d} {df[c].notna().sum():8d} {len(df):8d}")


if __name__ == "__main__":
    main()
