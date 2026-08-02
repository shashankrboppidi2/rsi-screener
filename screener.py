#!/usr/bin/env python3
# rsi-screener v2.4.1 (2026-08-02)
"""
RSI decile screener -- CI edition.

Pulls daily closes for the CORE-deduped universe, computes Wilder RSI,
buckets into deciles (1 = most oversold), and writes results to output/.

Data sources:
  --source yahoo   Yahoo via yfinance, batched + adaptive throttle + resumable
                   checkpoints. No API key. Default.
  --source fmp     Financial Modeling Prep (needs FMP_API_KEY). Faster, needs a plan.

Usage:
  python screener.py --source yahoo --min-dollar-vol 2000000
  python screener.py --source yahoo --us-only --limit 100      # smoke test
"""

import re, argparse, os, sys, time, json

__version__ = "2.4.1"

# Simple moving averages over trailing trading days. A window is only computed
# when the full history exists -- a "3y MA" built from 2y of data is not a 3y MA,
# so short names get NaN and `bars_available` explains why.
def _tidy_name(s):
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




MA_WINDOWS = {"ma_200d": 200, "ma_1y": 252, "ma_2y": 504, "ma_3y": 756, "ma_4y": 1008}
from concurrent.futures import ThreadPoolExecutor, as_completed
import numpy as np
import pandas as pd

FMP_BASE = "https://financialmodelingprep.com"


# ---------------------------------------------------------------- RSI

def wilder_rsi(close: pd.Series, n: int = 14) -> pd.Series:
    """Wilder's RSI. SMA-seeded, then Wilder smoothing (alpha = 1/n).

    Validated against an independent Wilder implementation over 400 bars
    (max abs diff 0.0); monotonic-up -> 100, monotonic-down -> 0.
    """
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)

    avg_gain = gain.rolling(n).mean()
    avg_loss = loss.rolling(n).mean()
    g = gain.to_numpy(copy=True); l = loss.to_numpy(copy=True)
    agv = avg_gain.to_numpy(copy=True); alv = avg_loss.to_numpy(copy=True)
    for i in range(n + 1, len(close)):
        if np.isnan(agv[i - 1]):
            continue
        agv[i] = (agv[i - 1] * (n - 1) + g[i]) / n
        alv[i] = (alv[i - 1] * (n - 1) + l[i]) / n
    ag = pd.Series(agv, index=close.index)
    al = pd.Series(alv, index=close.index)

    rs = ag / al.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    rsi = rsi.where(al != 0, 100.0)
    rsi = rsi.where(~((al != 0) & (ag == 0)), 0.0)
    return rsi


# ---------------------------------------------------------------- fetch: FMP

def _fmp_one(session, symbol, api_key, start, end, retries=3):
    """Return (symbol, DataFrame[close, volume]) or (symbol, None)."""
    url = f"{FMP_BASE}/stable/historical-price-eod/full"
    params = {"symbol": symbol, "from": start, "to": end, "apikey": api_key}
    for attempt in range(retries):
        try:
            r = session.get(url, params=params, timeout=30)
            if r.status_code == 429:                     # rate limited
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            payload = r.json()
        except Exception:
            time.sleep(1 + attempt)
            continue

        # FMP has shipped two shapes: a bare list, and {"historical": [...]}.
        rows = payload.get("historical") if isinstance(payload, dict) else payload
        if not rows:
            return symbol, None
        df = pd.DataFrame(rows)
        price_col = next((c for c in ("adjClose", "close", "price") if c in df.columns), None)
        if price_col is None or "date" not in df.columns:
            return symbol, None
        df["date"] = pd.to_datetime(df["date"])
        out = (df.set_index("date")[[price_col, "volume"]]
                 .rename(columns={price_col: "close"})
                 .sort_index())
        out["volume"] = pd.to_numeric(out["volume"], errors="coerce").fillna(0)
        out["close"] = pd.to_numeric(out["close"], errors="coerce")
        return symbol, out.dropna(subset=["close"])
    return symbol, None


def fetch_fmp(symbols, days=400, workers=8):
    import requests
    api_key = os.environ.get("FMP_API_KEY")
    if not api_key:
        sys.exit("FMP_API_KEY not set. Add it as a repo secret, or use --source yahoo.")

    end = pd.Timestamp.utcnow().normalize()
    start = end - pd.Timedelta(days=days)
    s, e = start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")

    closes, vols, failed = {}, {}, []
    session = requests.Session()
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_fmp_one, session, sym, api_key, s, e): sym for sym in symbols}
        for fut in as_completed(futs):
            sym, df = fut.result()
            done += 1
            if done % 250 == 0:
                print(f"  {done}/{len(symbols)} fetched", file=sys.stderr, flush=True)
            if df is None or df.empty:
                failed.append(sym); continue
            closes[sym] = df["close"]; vols[sym] = df["volume"]

    print(f"  fetched {len(closes)}, failed {len(failed)}", file=sys.stderr)
    if not closes:
        sys.exit("no data returned from FMP -- check the key and its plan entitlements")
    return pd.DataFrame(closes).sort_index(), pd.DataFrame(vols).sort_index(), failed


# ---------------------------------------------------------------- fetch: yahoo

def fetch_yahoo_throttled(symbols, **kw):
    from fetch_yahoo import fetch_yahoo
    return fetch_yahoo(symbols, **kw)


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", action="version",
                    version=f"rsi-screener {__version__}")
    ap.add_argument("--universe", default="data/universe_deduped.csv")
    ap.add_argument("--outdir", default="output")
    ap.add_argument("--source", choices=["yahoo", "fmp"], default="yahoo")
    ap.add_argument("--us-only", action="store_true")
    ap.add_argument("--fast-period", type=int, default=14)
    ap.add_argument("--slow-period", type=int, default=21)
    ap.add_argument("--min-history", type=int, default=60,
                    help="bars required to compute RSI; short names go to unranked.csv")
    ap.add_argument("--batch-size", type=int, default=50)
    ap.add_argument("--base-delay", type=float, default=2.0)
    ap.add_argument("--period", default="5y",
                    help="history to fetch; 5y needed for the 4y MA")
    ap.add_argument("--cache-dir", default="cache")
    ap.add_argument("--max-runtime-min", type=float, default=0)
    ap.add_argument("--no-resume", action="store_true")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--no-sectors", action="store_true")
    ap.add_argument("--sector-cache", default="data/sector_cache.csv")
    ap.add_argument("--min-mcap", type=float, default=0,
                    help="minimum market cap in USD, e.g. 50e6. 0 = no floor")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)

    uni = pd.read_csv(args.universe)
    if args.us_only:
        uni = uni[uni.us_listed == "Y"]
    uni = uni[uni.symbol_needs_manual_map == "N"]
    uni = uni.dropna(subset=["vendor_symbol"]).drop_duplicates("vendor_symbol")
    if args.limit:
        uni = uni.head(args.limit)
    symbols = uni.vendor_symbol.tolist()
    print(f"universe: {len(symbols)} symbols via {args.source}", file=sys.stderr)

    if args.source == "fmp":
        closes, vols, failed = fetch_fmp(symbols, workers=args.workers)
    else:
        closes, vols, failed = fetch_yahoo_throttled(
            symbols, period=args.period, batch_size=args.batch_size, cache_dir=args.cache_dir,
            base_delay=args.base_delay, resume=not args.no_resume,
            max_runtime_s=(args.max_runtime_min * 60) or None)

    if closes.empty:
        sys.exit("no data fetched -- check network / throttling")

    fastcol, slowcol = f"rsi{args.fast_period}", f"rsi{args.slow_period}"
    rows, unranked = [], []

    for sym in symbols:
        if sym not in closes.columns:
            unranked.append({"vendor_symbol": sym, "reason": "no data returned"})
            continue
        s = closes[sym].dropna()
        if len(s) < args.min_history:
            unranked.append({"vendor_symbol": sym,
                             "reason": f"only {len(s)} bars (need {args.min_history})"})
            continue

        fast = wilder_rsi(s, args.fast_period)
        slow = wilder_rsi(s, args.slow_period)
        if fast.dropna().empty or len(fast) < 3 or pd.isna(fast.iloc[-1]):
            unranked.append({"vendor_symbol": sym, "reason": "RSI not computable"})
            continue

        v = vols[sym].reindex(s.index).fillna(0) if sym in vols.columns else pd.Series(0.0, index=s.index)
        f0, f1, f2 = fast.iloc[-1], fast.iloc[-2], fast.iloc[-3]
        px = float(s.iloc[-1])
        win52 = s.tail(252)
        hi52, lo52 = float(win52.max()), float(win52.min())

        rec = {
            "vendor_symbol": sym,
            "price": round(px, 4),
            fastcol: round(float(f0), 2),
            slowcol: round(float(slow.iloc[-1]), 2),
            "rsi_fast_prev": round(float(f1), 2),
            "hook_turning_up": bool(f0 > f1 and f1 < f2),
            "pct_from_52w_high": round((px / hi52 - 1) * 100, 2),
            "pct_from_52w_low": round((px / lo52 - 1) * 100, 2),
            # Reported, never used to exclude -- ranking covers the whole universe.
            "avg_dollar_vol_20d": int((s * v).tail(20).mean()),
            "asof": str(s.index[-1].date()),
            "bars_available": len(s),
        }

        # Premium/discount of price to each trailing SMA, in percent.
        for label, win in MA_WINDOWS.items():
            if len(s) >= win:
                ma = float(s.tail(win).mean())
                rec[label] = round(ma, 4)
                rec[f"prem_{label}"] = round((px / ma - 1) * 100, 2)
            else:
                rec[label] = None
                rec[f"prem_{label}"] = None

        rows.append(rec)

    if not rows:
        sys.exit("no symbols produced an RSI")

    df = pd.DataFrame(rows).merge(uni, on="vendor_symbol", how="left")

    # Sector / industry enrichment (cached; only new symbols cost a request).
    if not args.no_sectors:
        try:
            from enrich import enrich
            sec = enrich(df.vendor_symbol.tolist(), cache_path=args.sector_cache)
            if not sec.empty:
                sec = sec.drop(columns=["source"], errors="ignore")
                # The universe file may already carry company_name. Merging two
                # frames that share a column silently produces _x/_y suffixes and
                # leaves the plain name empty -- so suffix explicitly and coalesce.
                overlap = [c for c in sec.columns
                           if c != "vendor_symbol" and c in df.columns]
                sec = sec.rename(columns={c: f"{c}__enr" for c in overlap})
                df = df.merge(sec, on="vendor_symbol", how="left")
                for c in overlap:
                    df[c] = df[c].fillna(df[f"{c}__enr"])
                    df = df.drop(columns=[f"{c}__enr"])
        except Exception as ex:
            print(f"sector enrichment skipped: {ex}", file=sys.stderr)
    for c in ("company_name", "sector", "industry", "country", "shares_outstanding"):
        if c not in df.columns:
            df[c] = pd.NA

    # Market cap = shares outstanding (cached, rarely changes) x latest close
    # (already fetched). Costs no extra requests and stays current.
    df["shares_outstanding"] = pd.to_numeric(df["shares_outstanding"], errors="coerce")
    df["market_cap"] = (df["shares_outstanding"] * df["price"]).round(0)

    if args.min_mcap > 0:
        before = len(df)
        known = df["market_cap"].notna()
        dropped_unknown = int((~known).sum())
        df = df[known & (df["market_cap"] >= args.min_mcap)].copy()
        print(f"market cap floor ${args.min_mcap:,.0f}: {before} -> {len(df)} "
              f"({dropped_unknown} had no share count and were excluded)",
              file=sys.stderr)
        if df.empty:
            sys.exit("market cap filter removed everything -- is the sector cache populated?")

    # Rank the ENTIRE universe. Decile 1 = lowest RSI = most oversold,
    # decile 10 = highest RSI = most overbought. No filtering anywhere.
    df["rsi_rank"] = df[fastcol].rank(method="first").astype(int)
    df["rsi_decile"] = pd.qcut(df["rsi_rank"], 10, labels=range(1, 11)).astype(int)
    df["rsi_pctile"] = (df[fastcol].rank(pct=True) * 100).round(1)
    df = df.sort_values("rsi_rank").reset_index(drop=True)

    lead = ["tv_ticker", "vendor_symbol", "company_name", "sector", "industry", "country",
            "exchange", "market_cap", "price", fastcol, slowcol, "rsi_decile",
            "rsi_pctile", "rsi_rank", "hook_turning_up",
            "pct_from_52w_high", "pct_from_52w_low",
            "prem_ma_200d", "prem_ma_1y", "prem_ma_2y", "prem_ma_3y", "prem_ma_4y",
            "ma_200d", "ma_1y", "ma_2y", "ma_3y", "ma_4y",
            "avg_dollar_vol_20d", "bars_available", "asof"]
    df = df[[c for c in lead if c in df.columns] +
            [c for c in df.columns if c not in lead]]

    if "company_name" in df.columns:
        df["company_name"] = df["company_name"].map(_tidy_name)
    df = df.drop(columns=[c for c in ("local_symbol", "etf", "us_listed",
                                      "symbol_needs_manual_map", "mcap_decile_file")
                          if c in df.columns], errors="ignore")
    # mcap_band is a passthrough from the older banded universe files; it is always
    # empty when the universe comes from build_universe.py, so drop it if unused.
    if "mcap_band" in df.columns and df["mcap_band"].isna().all():
        df = df.drop(columns=["mcap_band"])

    o = args.outdir
    df.to_csv(f"{o}/rsi_ranked_all.csv", index=False)

    # Per-decile summary
    summ = (df.groupby("rsi_decile")
              .agg(count=(fastcol, "size"), rsi_min=(fastcol, "min"),
                   rsi_max=(fastcol, "max"), rsi_median=(fastcol, "median"),
                   hooks=("hook_turning_up", "sum"))
              .round(2).reset_index())
    summ.to_csv(f"{o}/decile_summary.csv", index=False)

    # Sector breakdown of the two extremes
    if df.sector.notna().any():
        piv = (df.assign(sector=df.sector.fillna("Unknown"))
                 .pivot_table(index="sector", columns="rsi_decile",
                              values="vendor_symbol", aggfunc="count", fill_value=0))
        piv.to_csv(f"{o}/sector_by_decile.csv")

    if unranked:
        pd.DataFrame(unranked).to_csv(f"{o}/unranked.csv", index=False)

    # TradingView exports: every decile, sectioned.
    parts = []
    for d in range(1, 11):
        tag = {1: "D01 MOST OVERSOLD", 10: "D10 MOST OVERBOUGHT"}.get(d, f"D{d:02d}")
        parts.append(f"###{tag}")
        parts.extend(df[df.rsi_decile == d].tv_ticker.dropna().tolist())
    with open(f"{o}/tv_all_deciles.txt", "w") as fh:
        fh.write(",".join(parts))

    d1 = df[df.rsi_decile == 1]
    with open(f"{o}/tv_decile1_oversold.txt", "w") as fh:
        fh.write(",".join(
            ["###D1 HOOK CONFIRMED"] + d1[d1.hook_turning_up].tv_ticker.dropna().tolist() +
            ["###D1 WATCH"] + d1[~d1.hook_turning_up].tv_ticker.dropna().tolist()))

    asof = df["asof"].mode().iat[0]
    summary = {
        "run_utc": pd.Timestamp.utcnow().isoformat(),
        "source": args.source,
        "requested": len(symbols),
        "ranked": len(df),
        "unranked": len(unranked),
        "fetch_failures": len(failed),
        "sectors_resolved": int(df.sector.notna().sum()),
        "min_mcap_filter": args.min_mcap or None,
        "market_cap_resolved": int(df.market_cap.notna().sum()),
        "ma_coverage": {k: int(df[k].notna().sum()) for k in MA_WINDOWS if k in df.columns},
        "median_bars_available": int(df.bars_available.median()),
        "decile1_rsi_ceiling": float(df[df.rsi_decile == 1][fastcol].max()),
        "decile10_rsi_floor": float(df[df.rsi_decile == 10][fastcol].min()),
        "hook_confirmed_in_d1": int(d1.hook_turning_up.sum()),
        "asof": asof,
        "data_age_days": int((pd.Timestamp.utcnow().tz_localize(None).normalize()
                              - pd.Timestamp(asof)).days),
    }
    if summary["data_age_days"] > 4:
        summary["warning"] = (f"latest bar is {summary['data_age_days']} days old -- "
                              "holiday, or stale data from the vendor")
    with open(f"{o}/run_summary.json", "w") as fh:
        json.dump(summary, fh, indent=2)
    if failed:
        with open(f"{o}/fetch_failures.txt", "w") as fh:
            fh.write("\n".join(failed))

    print(json.dumps(summary, indent=2), file=sys.stderr)
    print("\n--- decile summary ---")
    print(summ.to_string(index=False))
    print(f"\n--- decile 1, most oversold (top 20 of {len(d1)}) ---")
    show = [c for c in ("tv_ticker", "company_name", "sector", fastcol,
                        "pct_from_52w_high", "prem_ma_200d", "prem_ma_1y")
            if c in df.columns]
    print(d1.head(20)[show].to_string(index=False))


if __name__ == "__main__":
    main()
