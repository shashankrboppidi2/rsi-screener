#!/usr/bin/env python3
# rsi-screener v2.4.1 (2026-08-02)
"""
Rank oversold opportunities within each sector.

Takes rsi_ranked_all.csv and produces a shortlist: liquid, adequately sized names
in the lower RSI deciles, scored on two percentile-ranked factors.

  oversold  = how depressed RSI(14) is right now       (higher = more oversold)
  trend     = price vs its 4-year average               (higher = base more intact)

The trend weight is the important dial. At 0.5 the screen finds multi-year winners
that pulled back -- momentum, not value. Set --trend-weight 0.0 for a pure
deep-value cut, or 1.0 to chase only the strongest long-term trends.

Usage:
  python rank_opportunities.py --csv output/rsi_ranked_all.csv
  python rank_opportunities.py --trend-weight 0 --max-rsi 25    # deep value
"""

import argparse, sys
import pandas as pd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="output/rsi_ranked_all.csv")
    ap.add_argument("--outdir", default="output")
    ap.add_argument("--min-dollar-vol", type=float, default=2e6)
    ap.add_argument("--min-mcap", type=float, default=3e8)
    ap.add_argument("--min-bars", type=int, default=756,
                    help="require this much history so the 4y MA is real")
    ap.add_argument("--max-decile", type=int, default=3)
    ap.add_argument("--max-rsi", type=float, default=None,
                    help="hard RSI ceiling; overrides --max-decile if stricter")
    ap.add_argument("--trend-weight", type=float, default=0.5)
    ap.add_argument("--top-n", type=int, default=3, help="picks per sector")
    ap.add_argument("--group-by", default="sector", choices=["sector", "industry"])
    ap.add_argument("--exclude-funds", action="store_true", default=True)
    ap.add_argument("--include-funds", dest="exclude_funds", action="store_false")
    args = ap.parse_args()

    df = pd.read_csv(args.csv)
    n0 = len(df)

    # Coalesce any leftover merge suffixes so this works on older outputs too.
    for base in ("company_name", "sector", "industry"):
        parts = [f"{base}_y", f"{base}_x"]
        if base not in df.columns or df[base].isna().all():
            for p in parts:
                if p in df.columns:
                    df[base] = df.get(base, pd.NA)
                    df[base] = df[base].fillna(df[p])

    need = {"rsi14", "rsi_decile", "prem_ma_4y", "avg_dollar_vol_20d", args.group_by}
    missing = need - set(df.columns)
    if missing:
        sys.exit(f"input is missing required columns: {sorted(missing)}")

    u = df.copy()
    if "market_cap" in u.columns and u.market_cap.notna().any():
        u = u[u.market_cap.fillna(0) >= args.min_mcap]
    u = u[u.avg_dollar_vol_20d.fillna(0) >= args.min_dollar_vol]
    if "bars_available" in u.columns:
        u = u[u.bars_available.fillna(0) >= args.min_bars]
    if args.exclude_funds and "company_name" in u.columns:
        # Closed-end funds slip past the exchange ETF flag. 'Trust' is deliberately
        # not matched -- REITs use it constantly.
        u = u[~u.company_name.fillna("").str.contains(r"\bfund\b", case=False, regex=True)]

    u = u.dropna(subset=["rsi14", "prem_ma_4y", args.group_by])
    print(f"tradable universe: {len(u)} of {n0}", file=sys.stderr)
    if u.empty:
        sys.exit("nothing survived the filters")

    tw = max(0.0, min(1.0, args.trend_weight))
    u["oversold_pctile"] = ((1 - u.rsi14.rank(pct=True)) * 100).round(1)
    u["trend_pctile"] = (u.prem_ma_4y.rank(pct=True) * 100).round(1)
    u["score"] = ((1 - tw) * u.oversold_pctile + tw * u.trend_pctile).round(1)

    cand = u[u.rsi_decile <= args.max_decile]
    if args.max_rsi is not None:
        cand = cand[cand.rsi14 <= args.max_rsi]
    print(f"oversold candidates: {len(cand)}", file=sys.stderr)
    if cand.empty:
        sys.exit("no candidates -- loosen --max-rsi / --max-decile")

    lead = [c for c in ("tv_ticker", "company_name", "sector", "industry", "market_cap",
                        "price", "rsi14", "rsi_decile", "score", "oversold_pctile",
                        "trend_pctile", "hook_turning_up", "pct_from_52w_high",
                        "prem_ma_200d", "prem_ma_1y", "prem_ma_4y",
                        "avg_dollar_vol_20d", "asof") if c in cand.columns]
    cand = cand[lead + [c for c in cand.columns if c not in lead]]

    cand.sort_values("score", ascending=False).to_csv(
        f"{args.outdir}/oversold_candidates.csv", index=False)

    top = (cand.sort_values("score", ascending=False)
               .groupby(args.group_by, group_keys=False).head(args.top_n)
               .sort_values([args.group_by, "score"], ascending=[True, False]))
    top.to_csv(f"{args.outdir}/top_by_{args.group_by}.csv", index=False)

    # Markdown for the Actions run summary
    lines = [f"Ranked {len(cand)} candidates from {n0} symbols "
             f"(trend weight {tw:.2f}, RSI<= {args.max_rsi or 'decile ' + str(args.max_decile)})",
             "", f"| {args.group_by.title()} | Ticker | Company | RSI | 52wHi | vs 200d | vs 4y | Score |",
             "|---|---|---|---|---|---|---|---|"]
    for r in top.itertuples():
        hook = " *" if getattr(r, "hook_turning_up", False) else ""
        lines.append(
            f"| {getattr(r, args.group_by)} | {r.tv_ticker}{hook} | "
            f"{str(getattr(r, 'company_name', ''))[:28]} | {r.rsi14:.1f} | "
            f"{getattr(r, 'pct_from_52w_high', float('nan')):.0f}% | "
            f"{getattr(r, 'prem_ma_200d', float('nan')):.0f}% | "
            f"{r.prem_ma_4y:.0f}% | {r.score:.0f} |")
    lines += ["", "`*` = RSI turning up (hook confirmed)", "",
              "Screen output, not advice. No fundamentals are considered here."]
    with open(f"{args.outdir}/top_summary.md", "w") as fh:
        fh.write("\n".join(lines))

    tv = []
    for grp, g in top.groupby(args.group_by):
        tv.append("###" + str(grp).upper()[:20])
        tv.extend(g.tv_ticker.dropna().tolist())
    with open(f"{args.outdir}/tv_top_picks.txt", "w") as fh:
        fh.write(",".join(tv))

    print("\n".join(lines[:4] + lines[4:34]))


if __name__ == "__main__":
    main()
