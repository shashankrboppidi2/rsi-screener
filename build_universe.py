#!/usr/bin/env python3
# rsi-screener v2.4.1 (2026-08-02)
"""
Build a US-listed universe from NASDAQ Trader's public symbol files.

No API key, no subscription. These are the official listing files the exchanges
publish daily:

  nasdaqlisted.txt  -- every NASDAQ-listed security
  otherlisted.txt   -- NYSE, NYSE American, NYSE Arca, BATS, IEX

Market cap is NOT in these files. It's computed later in the screener as
shares_outstanding * latest_close, so it costs no extra requests per run.

Usage:
  python build_universe.py --out data/us_universe.csv
  python build_universe.py --keep-etfs
"""

import re, argparse, io, sys
import pandas as pd
import requests

NASDAQ = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"

# otherlisted.txt exchange codes -> TradingView prefix
EXCH = {"A": "AMEX", "N": "NYSE", "P": "AMEX", "Z": "AMEX", "V": "AMEX"}

# Suffixes that mark warrants, units, rights, preferreds, and when-issued lines.
JUNK_SUFFIX = ("W", "U", "R", "P")


def _get(url, timeout=60):
    r = requests.get(url, timeout=timeout,
                     headers={"User-Agent": "Mozilla/5.0 (universe-builder)"})
    r.raise_for_status()
    return r.text


def _strip_footer(text):
    """Both files end with a 'File Creation Time: ...' line."""
    lines = [ln for ln in text.splitlines()
             if ln.strip() and not ln.lower().startswith("file creation time")]
    return "\n".join(lines)


def parse_nasdaq(text):
    df = pd.read_csv(io.StringIO(_strip_footer(text)), sep="|", dtype=str)
    df.columns = [c.strip() for c in df.columns]
    df = df.rename(columns={"Symbol": "symbol", "Security Name": "name",
                            "Test Issue": "test", "ETF": "etf",
                            "Financial Status": "fin_status"})
    df["exchange"] = "NASDAQ"
    return df[["symbol", "name", "exchange", "test", "etf", "fin_status"]]


def parse_other(text):
    df = pd.read_csv(io.StringIO(_strip_footer(text)), sep="|", dtype=str)
    df.columns = [c.strip() for c in df.columns]
    df = df.rename(columns={"ACT Symbol": "symbol", "Security Name": "name",
                            "Test Issue": "test", "ETF": "etf",
                            "Exchange": "exch_code"})
    df["exchange"] = df["exch_code"].map(EXCH).fillna("NYSE")
    df["fin_status"] = pd.NA
    return df[["symbol", "name", "exchange", "test", "etf", "fin_status"]]


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


EXCLUDE = {
    "funds": r"\bfund\b",
    "spacs": r"acquisition corp|acquisition inc|acquisition compan|blank check",
    "debt":  r"debenture|exchange-traded note|subordinated notes?\b",
}


def clean(df, keep_etfs=False, exclude=()):
    n0 = len(df)
    df = df.dropna(subset=["symbol"]).copy()
    df["symbol"] = df["symbol"].str.strip()

    df = df[df["test"].fillna("N").str.upper() != "Y"]
    if not keep_etfs:
        df = df[df["etf"].fillna("N").str.upper() != "Y"]

    # Yahoo uses '-' where the exchanges use '.' for share classes (BRK.B -> BRK-B).
    df["vendor_symbol"] = df["symbol"].str.replace(".", "-", regex=False)

    # Drop derivative lines: 5-letter NASDAQ symbols ending in W/U/R/P are
    # warrants, units, rights and preferreds, not common stock.
    is_junk = (df["symbol"].str.len() == 5) & (df["symbol"].str[-1].isin(JUNK_SUFFIX))
    # '$' marks preferred series on the NYSE side.
    is_junk |= df["symbol"].str.contains(r"[\$]", regex=True, na=False)
    df = df[~is_junk]

    name_junk = r"(?:warrant|unit[s]?\b|right[s]?\b|depositary|preferred|when issued|%)"
    df = df[~df["name"].fillna("").str.lower().str.contains(name_junk, regex=True)]

    for key in exclude:
        pat = EXCLUDE.get(key)
        if not pat:
            continue
        hit = df["name"].fillna("").str.contains(pat, case=False, regex=True)
        print(f"  excluding {hit.sum()} {key}", file=sys.stderr)
        df = df[~hit]

    df["name"] = df["name"].map(tidy_name)
    df["tv_ticker"] = df["exchange"] + ":" + df["symbol"]
    df = df.drop_duplicates("vendor_symbol").sort_values("vendor_symbol")
    print(f"  {n0} raw -> {len(df)} after cleaning", file=sys.stderr)
    return df.reset_index(drop=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/us_universe.csv")
    ap.add_argument("--keep-etfs", action="store_true")
    ap.add_argument("--exclude", nargs="*", default=[],
                    choices=sorted(EXCLUDE), metavar="CATEGORY",
                    help="drop non-operating companies: funds spacs debt")
    args = ap.parse_args()

    print("downloading NASDAQ listings...", file=sys.stderr)
    nd = parse_nasdaq(_get(NASDAQ))
    print(f"  nasdaqlisted.txt: {len(nd)} rows", file=sys.stderr)

    print("downloading NYSE / AMEX / Arca listings...", file=sys.stderr)
    ot = parse_other(_get(OTHER))
    print(f"  otherlisted.txt:  {len(ot)} rows", file=sys.stderr)

    df = clean(pd.concat([nd, ot], ignore_index=True),
           keep_etfs=args.keep_etfs, exclude=args.exclude)

    out = df[["tv_ticker", "vendor_symbol", "symbol", "name", "exchange", "etf"]].copy()
    out = out.rename(columns={"symbol": "local_symbol", "name": "company_name"})
    out["us_listed"] = "Y"
    out["symbol_needs_manual_map"] = "N"      # keeps the screener's schema
    out["mcap_band"] = pd.NA                  # filled by the screener at runtime

    import os
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    out.to_csv(args.out, index=False)

    print(f"\nwrote {len(out)} symbols -> {args.out}", file=sys.stderr)
    print(out.exchange.value_counts().to_string(), file=sys.stderr)


if __name__ == "__main__":
    main()
