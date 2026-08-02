# rsi-screener v2.4.1 (2026-08-02)
"""
Throttled, resumable Yahoo Finance fetcher.

Yahoo has no published rate limit; it just starts returning empty frames and 429s
when it decides you're noisy. Shared CI egress IPs get there faster. So:

  * batch symbols per request (yfinance accepts a list) instead of one call each
  * adaptive sleep -- back off hard on failure, ease off slowly on success
  * checkpoint every batch to disk, so a crash or timeout never re-fetches
  * sweep failed batches at the end with smaller batches and longer waits
"""

import os, time, random, hashlib, sys
import pandas as pd


class AdaptiveThrottle:
    """Multiplicative-increase / gentle-decrease sleep controller."""

    def __init__(self, base=2.0, floor=1.0, ceiling=120.0):
        self.delay = base
        self.floor = floor
        self.ceiling = ceiling
        self.consecutive_failures = 0

    def success(self):
        self.consecutive_failures = 0
        self.delay = max(self.floor, self.delay * 0.85)

    def failure(self):
        self.consecutive_failures += 1
        self.delay = min(self.ceiling, max(self.floor, self.delay) * 2.0)

    def wait(self):
        # Jitter so retries don't synchronise into a thundering herd.
        time.sleep(self.delay * random.uniform(0.8, 1.2))


def _batch_key(symbols, period):
    h = hashlib.md5(("|".join(sorted(symbols)) + period).encode()).hexdigest()[:16]
    return f"batch_{h}.pkl"


def _download(symbols, period, tries=3, throttle=None):
    """One batched yfinance call. Returns (closes_df, volumes_df) or (None, None)."""
    import yfinance as yf
    for attempt in range(tries):
        try:
            d = yf.download(
                symbols, period=period, interval="1d",
                auto_adjust=True, progress=False,
                group_by="column", threads=False,   # we do our own pacing
            )
            if d is None or d.empty:
                raise ValueError("empty frame")

            if isinstance(d.columns, pd.MultiIndex):
                closes, vols = d["Close"], d["Volume"]
            else:
                # Single surviving symbol collapses the MultiIndex.
                closes = d[["Close"]].rename(columns={"Close": symbols[0]})
                vols = d[["Volume"]].rename(columns={"Volume": symbols[0]})

            closes = closes.dropna(axis=1, how="all")
            if closes.empty:
                raise ValueError("all columns empty")
            if throttle:
                throttle.success()
            return closes, vols.reindex(columns=closes.columns)

        except Exception as ex:
            if throttle:
                throttle.failure()
            print(f"    attempt {attempt + 1}/{tries} failed: {ex}", file=sys.stderr)
            if attempt < tries - 1 and throttle:
                throttle.wait()
    return None, None


def fetch_yahoo(symbols, period="5y", batch_size=50, cache_dir="cache",
                base_delay=2.0, max_runtime_s=None, resume=True):
    """Fetch daily closes + volumes for `symbols`. Returns (closes, volumes, failed)."""
    os.makedirs(cache_dir, exist_ok=True)
    throttle = AdaptiveThrottle(base=base_delay)
    started = time.time()

    batches = [symbols[i:i + batch_size] for i in range(0, len(symbols), batch_size)]
    frames, failed_batches, stopped_early = [], [], False

    for n, chunk in enumerate(batches, 1):
        path = os.path.join(cache_dir, _batch_key(chunk, period))

        if resume and os.path.exists(path):
            try:
                frames.append(pd.read_pickle(path))
                continue                       # already have it; no request, no sleep
            except Exception:
                os.remove(path)                # corrupt checkpoint, refetch

        if max_runtime_s and (time.time() - started) > max_runtime_s:
            print(f"  runtime budget hit at batch {n}/{len(batches)}; "
                  f"checkpoints kept, rerun to resume", file=sys.stderr)
            stopped_early = True
            break

        print(f"  batch {n}/{len(batches)} ({len(chunk)} symbols, "
              f"delay {throttle.delay:.1f}s)", file=sys.stderr, flush=True)
        closes, vols = _download(chunk, period, throttle=throttle)

        if closes is None:
            failed_batches.append(chunk)
        else:
            wide = pd.concat({"close": closes, "volume": vols}, axis=1)
            wide.to_pickle(path)
            frames.append(wide)

        throttle.wait()

    # Sweep: retry failures in smaller pieces, slower.
    if failed_batches and not stopped_early:
        retry = [s for chunk in failed_batches for s in chunk]
        print(f"  sweeping {len(retry)} symbols from {len(failed_batches)} "
              f"failed batches", file=sys.stderr)
        throttle.delay = max(throttle.delay, 5.0)
        small = [retry[i:i + 10] for i in range(0, len(retry), 10)]
        failed_batches = []
        for n, chunk in enumerate(small, 1):
            print(f"    sweep {n}/{len(small)}", file=sys.stderr, flush=True)
            closes, vols = _download(chunk, period, tries=2, throttle=throttle)
            if closes is None:
                failed_batches.append(chunk)
            else:
                wide = pd.concat({"close": closes, "volume": vols}, axis=1)
                wide.to_pickle(os.path.join(cache_dir, _batch_key(chunk, period)))
                frames.append(wide)
            throttle.wait()

    if not frames:
        return pd.DataFrame(), pd.DataFrame(), symbols

    allw = pd.concat(frames, axis=1).sort_index()
    closes = allw["close"]
    vols = allw["volume"]
    closes = closes.loc[:, ~closes.columns.duplicated()]
    vols = vols.loc[:, ~vols.columns.duplicated()]

    got = set(closes.columns)
    failed = [s for s in symbols if s not in got]
    return closes, vols, failed
