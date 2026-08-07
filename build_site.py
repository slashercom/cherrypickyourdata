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
SITE_URL = "https://www.cherrypickyourdata.com"
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


def fetch_yahoo_monthly():
    """
    Fallback price source: ^GSPC daily closes from Yahoo, averaged to monthly.

    Shiller's P column is the monthly *average* of daily closes, not the
    month-end close, so we average here too -- otherwise the fresh months
    would sit on a slightly different basis than the historical ones.
    """
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC"
           "?range=max&interval=1d")
    print(f"Fetching prices from Yahoo Finance ...")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=90) as resp:
        blob = json.loads(resp.read().decode("utf-8"))

    result = blob["chart"]["result"][0]
    stamps = result["timestamp"]
    closes = result["indicators"]["quote"][0]["close"]
    recs = [
        (dt.datetime.utcfromtimestamp(t).date(), c)
        for t, c in zip(stamps, closes) if c is not None
    ]
    s = pd.Series([c for _, c in recs],
                  index=pd.DatetimeIndex([d for d, _ in recs]))
    monthly = s.resample("MS").mean()
    print(f"  got {len(monthly):,} months, through {monthly.index[-1].date()}")
    return monthly


def fetch_fred_cpi():
    """Fallback CPI source: CPIAUCSL from FRED (no API key needed)."""
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=CPIAUCSL"
    print("Fetching CPI from FRED ...")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=90) as resp:
        raw = resp.read().decode("utf-8")
    df = pd.read_csv(io.StringIO(raw))
    date_col, val_col = df.columns[0], df.columns[1]
    df[date_col] = pd.to_datetime(df[date_col])
    df[val_col] = pd.to_numeric(df[val_col], errors="coerce")
    s = df.dropna(subset=[val_col]).set_index(date_col)[val_col].resample("MS").last()
    print(f"  got {len(s):,} months, through {s.index[-1].date()}")
    return s


def fetch_yahoo_fred():
    """Combine the two fallback sources into the standard frame."""
    price = fetch_yahoo_monthly()
    cpi = fetch_fred_cpi()
    idx = price.index.union(cpi.index)
    out = pd.DataFrame({
        "date": [d.date() for d in idx],
        "price": price.reindex(idx).values,
        "cpi": cpi.reindex(idx).values,
    }).dropna(subset=["price"]).reset_index(drop=True)
    return out


def latest_month(df):
    return max(df["date"])


def merge_with_cache(cache_df, new_df):
    """
    Extend the cached history with any genuinely newer months.

    History is never rewritten -- we only append months past the cache's end.
    That keeps the long series stable and means a source switch can't silently
    restate 70 years of numbers.

    CPI is rebased onto the cache's scale first, since different sources use
    different index bases (Shiller vs FRED's 1982-84=100). Without this the
    inflation-adjusted series would jump at the join.
    """
    c = cache_df.set_index("date").sort_index()
    n = new_df.set_index("date").sort_index()

    common = c.index.intersection(n.index)
    if len(common) < 12:
        raise RuntimeError(
            f"only {len(common)} overlapping months with the cache - "
            "refusing to splice series that may not be comparable"
        )

    both_cpi = [d for d in common
                if pd.notna(c.loc[d, "cpi"]) and pd.notna(n.loc[d, "cpi"])]
    if both_cpi:
        factor = float(pd.Series(
            [c.loc[d, "cpi"] / n.loc[d, "cpi"] for d in both_cpi]).median())
        n["cpi"] = n["cpi"] * factor
        print(f"  rebased incoming CPI by x{factor:.5f} to match cache")

    price_ratio = float(pd.Series(
        [c.loc[d, "price"] / n.loc[d, "price"] for d in common]).median())
    if abs(price_ratio - 1.0) > 0.02:
        print(f"  WARNING: price levels differ by {abs(price_ratio-1)*100:.1f}% "
              "across the overlap - sources may use different conventions",
              file=sys.stderr)

    cutoff = c.index.max()
    appended = n.loc[[d for d in n.index if d > cutoff]]
    if appended.empty:
        return cache_df, 0
    merged = pd.concat([c, appended]).sort_index().reset_index()
    return merged, len(appended)


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


FAQ = [
    ("What is a rolling return?",
     "An ordinary return measures one fixed start date to one fixed end date. A rolling "
     "return recalculates that measurement at every point in time, showing what a given "
     "holding period would have returned depending on when you started."),
    ("Why do longer horizons look calmer?",
     "Partly because good and bad years offset each other, and partly because annualizing "
     "compresses extremes: a severe one-year crash becomes a couple of percentage points "
     "per year when spread across a 25-year window."),
    ("Do these figures include dividends?",
     "No. These are price returns only. Dividends have historically added roughly two "
     "percentage points per year, so total-return figures would be meaningfully higher."),
    ("What does the inflation-adjusted toggle change?",
     "It swaps the price series for a CPI-deflated one, expressing returns in constant "
     "purchasing power rather than nominal dollars."),
    ("Where does the data come from?",
     "Robert Shiller's long-run dataset, which carries monthly S&P 500 prices and matching "
     "CPI back to 1871. The page checks for new data daily and rebuilds when a new month "
     "is published."),
    ("Is this investment advice?",
     "No. It is a historical data visualization, not a forecast, and does not account for "
     "individual taxes, fees, or circumstances."),
]


def build_jsonld(latest_month, first_month):
    """Dataset + FAQ structured data. Both are formats Google understands."""
    graph = [
        {
            "@type": "WebSite",
            "@id": f"{SITE_URL}/#website",
            "url": f"{SITE_URL}/",
            "name": "Cherry Pick Your Data",
            "description": "Interactive explorations of financial and market data.",
        },
        {
            "@type": "Dataset",
            "@id": f"{SITE_URL}/#dataset",
            "name": "S&P 500 Rolling Annualized Returns, 1950-present",
            "description": (
                "Rolling annualized total price returns for the S&P 500 index across ten "
                "holding-period horizons (6 months to 25 years), in both nominal and "
                "inflation-adjusted (CPI-deflated) terms, at monthly resolution."
            ),
            "url": f"{SITE_URL}/",
            "keywords": [
                "S&P 500", "rolling returns", "annualized returns",
                "inflation-adjusted returns", "stock market history",
                "historical returns", "CPI", "real returns",
            ],
            "temporalCoverage": f"{first_month}/{latest_month}",
            "license": "https://creativecommons.org/licenses/by/4.0/",
            "isAccessibleForFree": True,
            "creator": {"@type": "Organization", "name": "Cherry Pick Your Data"},
            "distribution": [{
                "@type": "DataDownload",
                "encodingFormat": "text/csv",
                "contentUrl": f"{SITE_URL}/data/monthly.csv",
            }],
            "variableMeasured": [
                {"@type": "PropertyValue", "name": "Nominal annualized return",
                 "unitText": "percent per year"},
                {"@type": "PropertyValue", "name": "Inflation-adjusted annualized return",
                 "unitText": "percent per year"},
            ],
        },
        {
            "@type": "FAQPage",
            "@id": f"{SITE_URL}/#faq",
            "mainEntity": [
                {"@type": "Question", "name": q,
                 "acceptedAnswer": {"@type": "Answer", "text": a}}
                for q, a in FAQ
            ],
        },
    ]
    return json.dumps({"@context": "https://schema.org", "@graph": graph},
                      separators=(",", ":"))


def write_seo_files():
    today = dt.date.today().isoformat()
    with open("sitemap.xml", "w", encoding="utf-8") as f:
        f.write(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            f"  <url>\n    <loc>{SITE_URL}/</loc>\n"
            f"    <lastmod>{today}</lastmod>\n"
            "    <changefreq>monthly</changefreq>\n"
            "    <priority>1.0</priority>\n  </url>\n"
            "</urlset>\n"
        )
    with open("robots.txt", "w", encoding="utf-8") as f:
        f.write(f"User-agent: *\nAllow: /\n\nSitemap: {SITE_URL}/sitemap.xml\n")
    print("Wrote sitemap.xml and robots.txt")


def write_favicon():
    """Tiny sparkline mark, drawn as SVG so it stays crisp at any size."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        '<rect width="64" height="64" rx="12" fill="#15171C"/>'
        '<polyline points="10,44 20,38 27,46 34,26 41,32 48,14 54,20" '
        'fill="none" stroke="#52ABA6" stroke-width="5" '
        'stroke-linecap="round" stroke-linejoin="round"/>'
        '<circle cx="54" cy="20" r="5" fill="#E4BE45"/>'
        "</svg>"
    )
    with open("favicon.svg", "w", encoding="utf-8") as f:
        f.write(svg)
    print("Wrote favicon.svg")


def write_og_image(payload):
    """1200x630 social preview card, generated from the live data."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    INK, PARCH, DIM = "#15171C", "#EAE6DA", "#A9A69C"
    TEAL, AMBER, ROSE = "#52ABA6", "#E4BE45", "#D2695D"

    fig = plt.figure(figsize=(12, 6.3), dpi=100)
    fig.patch.set_facecolor(INK)
    ax = fig.add_axes([0.06, 0.10, 0.88, 0.46])
    ax.set_facecolor(INK)

    pts = payload["series"]["10-year"]
    xs = [dt.date(int(p["d"][:4]), int(p["d"][5:7]), 1) for p in pts]
    ys = [p["v"] for p in pts]
    ax.plot(xs, ys, color=TEAL, linewidth=2.4)
    ax.fill_between(xs, ys, 0, where=[y >= 0 for y in ys],
                    color=TEAL, alpha=0.16, interpolate=True)
    ax.fill_between(xs, ys, 0, where=[y < 0 for y in ys],
                    color=ROSE, alpha=0.20, interpolate=True)
    ax.axhline(0, color=PARCH, alpha=0.35, linewidth=1, linestyle="--")

    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color("#33363F")
    ax.tick_params(colors=DIM, labelsize=11)
    ax.grid(color="#FFFFFF", alpha=0.06)
    ax.set_ylabel("% per year", color=DIM, fontsize=11)

    fig.text(0.06, 0.86, "S&P 500 Rolling Returns",
             color=PARCH, fontsize=40, fontweight="semibold", va="top")
    fig.text(0.06, 0.735, "Ten horizons, 6 months to 25 years  ·  nominal & inflation-adjusted",
             color=AMBER, fontsize=17, va="top")
    latest = payload["level"][-1]["d"]
    fig.text(0.06, 0.665, f"Monthly data 1950–{latest}  ·  updated automatically",
             color=DIM, fontsize=13.5, va="top")
    fig.text(0.94, 0.045, "cherrypickyourdata.com",
             color=DIM, fontsize=13, ha="right")
    fig.add_artist(Rectangle((0, 0.972), 1, 0.028, color=AMBER,
                             transform=fig.transFigure, zorder=5))

    fig.savefig("og-image.png", facecolor=INK)
    plt.close(fig)
    print("Wrote og-image.png (1200x630)")


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
    first_month = payload["level"][0]["d"]
    html = (
        tpl.replace("__DATA_JSON__", json.dumps(payload, separators=(",", ":")))
        .replace("__WINDOWS_JSON__", json.dumps(WINDOWS))
        .replace("__JSONLD__", build_jsonld(latest["d"], first_month))
        .replace("__SITE_URL__", SITE_URL)
        .replace("__FIRST_DATE__", first_month)
        .replace("__LAST_DATE__", latest["d"])
        .replace("__LATEST_MONTH__", latest["d"])
        .replace("__BUILD_DATE__", dt.date.today().isoformat())
    )
    with open(OUTPUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Wrote {OUTPUT} ({len(html):,} bytes)")

    write_seo_files()
    write_favicon()
    try:
        write_og_image(payload)
    except Exception as exc:  # noqa: BLE001 - image is nice-to-have, not critical
        print(f"WARNING: could not render og-image.png ({exc})", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true",
                    help="skip downloads, rebuild from the cached CSV")
    ap.add_argument("--force", action="store_true",
                    help="publish even if no source is newer than the cache")
    args = ap.parse_args()

    if not os.path.exists(CACHE_CSV):
        print(f"ERROR: {CACHE_CSV} is missing - it is the baseline history",
              file=sys.stderr)
        return 1
    cache = load_cache()
    cache_latest = latest_month(cache)
    print(f"Cache currently ends {cache_latest}")

    if args.offline:
        build(cache)
        return 0

    # Try each source; keep the first one that is actually newer than the cache.
    sources = [("Shiller (Yale)", download_shiller),
               ("Yahoo Finance + FRED", fetch_yahoo_fred)]

    merged, added = cache, 0
    for name, fetch in sources:
        try:
            fresh = fetch()
        except Exception as exc:  # noqa: BLE001
            print(f"  {name} failed: {exc}", file=sys.stderr)
            continue

        fresh_latest = latest_month(fresh)
        if fresh_latest <= cache_latest:
            print(f"  {name} is not newer (ends {fresh_latest}) - "
                  "ignoring it and trying the next source", file=sys.stderr)
            continue

        try:
            merged, added = merge_with_cache(cache, fresh)
        except Exception as exc:  # noqa: BLE001
            print(f"  {name} could not be merged: {exc}", file=sys.stderr)
            continue

        print(f"  {name}: added {added} new month(s), now ending "
              f"{latest_month(merged)}")
        break

    if added == 0 and not args.force:
        print("No source had newer data than the cache. "
              "Leaving the published page untouched.")
        return 0

    # Only now is it safe to persist -- the data has been proven to move forward.
    save_cache(merged)
    build(merged)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
