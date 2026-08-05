#!/usr/bin/env python3
"""
Rebuild the S&P 500 rolling-returns page from the latest available data.

Data source: Robert Shiller's "Irrational Exuberance" dataset (ie_data.xls),
which carries monthly S&P 500 prices AND the matching CPI back to 1871 --
so both the nominal and the inflation-adjusted series come from one file and
stay internally consistent. Shiller refreshes it roughly monthly.

Usage:
    python build_site.py            # fetch fresh data, write index.html
    python build_site.py --offline  # rebuild from cached data/monthly.csv

If the download fails, the script falls back to the cached CSV so a transient
outage at the source never publishes a broken page.
"""

import argparse
import datetime as dt
import io
import json
import os
import sys
import urllib.request

import pandas as pd

SHILLER_URL = "http://www.econ.yale.edu/~shiller/data/ie_data.xls"
CACHE_CSV = os.path.join("data", "monthly.csv")
TEMPLATE = "template.html"
OUTPUT = "index.html"

# Start the series in 1950; earlier data exists but the modern index is the
# interesting part and it keeps the payload small.
START_YEAR = 1950

WINDOWS = [
    {"id": "6-months", "label": "6 Months", "short": "6M", "months": 6},
    {"id": "1-year", "label": "1 Year", "short": "1Y", "months": 12},
    {"id": "2-year", "label": "2 Years", "short": "2Y", "months": 24},
    {"id": "3-year", "label": "3 Years", "short": "3Y", "months": 36},
    {"id": "5-year", "label": "5 Years", "short": "5Y", "months": 60},
    {"id": "7-year", "label": "7 Years", "short": "7Y", "months": 84},
    {"id": "10-year", "label": "10 Years", "short": "10Y", "months": 120},
    {"id": "15-year", "label": "15 Years", "short": "15Y", "months": 180},
    {"id": "20-year", "label": "20 Years", "short": "20Y", "months": 240},
    {"id": "25-year", "label": "25 Years", "short": "25Y", "months": 300},
]


def parse_shiller_date(value):
    """Shiller encodes dates as YYYY.MM floats, where .1 means October."""
    text = f"{float(value):.2f}"
    year, frac = text.split(".")
    month = int(frac)
    if month < 1 or month > 12:
        raise ValueError(f"bad month in {value!r}")
    return dt.date(int(year), month, 1)


def download_shiller():
    print(f"Downloading {SHILLER_URL} ...")
    req = urllib.request.Request(
        SHILLER_URL, headers={"User-Agent": "rolling-returns-site/1.0"}
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        blob = resp.read()
    print(f"  got {len(blob):,} bytes")

    last_err = None
    for engine in ("xlrd", "openpyxl", None):
        try:
            kwargs = {"sheet_name": "Data", "skiprows": 7}
            if engine:
                kwargs["engine"] = engine
            df = pd.read_excel(io.BytesIO(blob), **kwargs)
            break
        except Exception as exc:  # noqa: BLE001 - want to try every engine
            last_err = exc
            df = None
    if df is None:
        raise RuntimeError(f"could not parse workbook: {last_err}")

    df.columns = [str(c).strip() for c in df.columns]
    date_col = df.columns[0]
    price_col = next(c for c in df.columns if c == "P" or c.startswith("P."))
    cpi_col = next(c for c in df.columns if c.upper().startswith("CPI"))

    rows = []
    for _, r in df.iterrows():
        try:
            d = parse_shiller_date(r[date_col])
        except Exception:
            continue  # footer / notes rows
        price = pd.to_numeric(r[price_col], errors="coerce")
        cpi = pd.to_numeric(r[cpi_col], errors="coerce")
        if pd.isna(price):
            continue
        rows.append({"date": d, "price": float(price),
                     "cpi": float(cpi) if not pd.isna(cpi) else None})

    out = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
    if len(out) < 500:
        raise RuntimeError(f"suspiciously few rows parsed ({len(out)})")
    print(f"  parsed {len(out):,} monthly rows, "
          f"{out['date'].iloc[0]} to {out['date'].iloc[-1]}")
    return out


def load_cache():
    print(f"Loading cached data from {CACHE_CSV}")
    df = pd.read_csv(CACHE_CSV, parse_dates=["date"])
    df["date"] = df["date"].dt.date
    return df


def save_cache(df):
    os.makedirs(os.path.dirname(CACHE_CSV), exist_ok=True)
    df.to_csv(CACHE_CSV, index=False)
    print(f"Cached raw data -> {CACHE_CSV}")


def rolling_series(monthly):
    """monthly: pd.Series indexed by month-start. Returns {window_id: [pts]}."""
    out = {}
    for w in WINDOWS:
        wm = w["months"]
        ratio = monthly / monthly.shift(wm)
        years = wm / 12.0
        ann = (ratio.pow(1.0 / years) - 1.0) * 100.0
        ann = ann.dropna()
        out[w["id"]] = [
            {"d": idx.strftime("%Y-%m"), "v": round(float(v), 2)}
            for idx, v in ann.items()
        ]
    return out


def as_points(series):
    return [
        {"d": idx.strftime("%Y-%m"), "v": round(float(v), 2)}
        for idx, v in series.items()
    ]


def build(df):
    df = df[pd.to_datetime(df["date"]).dt.year >= START_YEAR].copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date")

    nominal = df["price"].resample("MS").last().ffill()

    # Real prices in today's dollars: deflate by CPI, rebase to latest CPI.
    cpi = df["cpi"].resample("MS").last().ffill()
    have_cpi = cpi.dropna()
    if have_cpi.empty:
        raise RuntimeError("no CPI data available for the real series")
    latest_cpi = have_cpi.iloc[-1]
    real = (nominal / cpi * latest_cpi).dropna()

    payload = {
        "series": rolling_series(nominal),
        "series_real": rolling_series(real),
        "level": as_points(nominal),
        "level_real": as_points(real),
    }

    latest = payload["level"][-1]
    print(f"  latest month: {latest['d']}  index {latest['v']:,.2f}")
    print(f"  10y nominal {payload['series']['10-year'][-1]['v']:+.2f}%  "
          f"real {payload['series_real']['10-year'][-1]['v']:+.2f}%")

    tpl = open(TEMPLATE, encoding="utf-8").read()
    html = (
        tpl.replace("__DATA_JSON__", json.dumps(payload, separators=(",", ":")))
        .replace("__WINDOWS_JSON__", json.dumps(WINDOWS))
        .replace("__FIRST_DATE__", payload["level"][0]["d"])
        .replace("__LAST_DATE__", latest["d"])
        .replace("__LATEST_MONTH__", latest["d"])
        .replace("__BUILD_DATE__", dt.date.today().isoformat())
    )
    with open(OUTPUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Wrote {OUTPUT} ({len(html):,} bytes)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true",
                    help="skip the download, rebuild from the cached CSV")
    args = ap.parse_args()

    df = None
    if not args.offline:
        try:
            df = download_shiller()
            save_cache(df)
        except Exception as exc:  # noqa: BLE001
            print(f"WARNING: fetch failed ({exc})", file=sys.stderr)

    if df is None:
        if not os.path.exists(CACHE_CSV):
            print("ERROR: no fresh data and no cache to fall back on",
                  file=sys.stderr)
            return 1
        df = load_cache()

    build(df)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
