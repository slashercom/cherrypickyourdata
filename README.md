# cherrypickyourdata.com — S&P 500 rolling returns

A self-updating static page showing S&P 500 rolling annualized returns across ten
horizons (6 months → 25 years), in both nominal and inflation-adjusted terms.

## How it works

| File | Purpose |
|---|---|
| `index.html` | The published page. **Generated — don't hand-edit it.** |
| `template.html` | The actual source of the page. Edit this for design/copy changes. |
| `build_site.py` | Fetches data, computes rolling returns, renders `template.html` → `index.html`. |
| `data/monthly.csv` | Cached raw data (price + CPI). Fallback if the source is unreachable. |
| `.github/workflows/update-data.yml` | Daily job that reruns the build and commits any changes. |

Data comes from Robert Shiller's *Irrational Exuberance* dataset
(`ie_data.xls`), which carries monthly S&P 500 prices **and** matching CPI back
to 1871 — so the nominal and real series come from one file and stay
consistent. It's refreshed roughly monthly.

## One-time setup

### 1. Create the repo

```bash
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/USERNAME/REPO.git
git push -u origin main
```

The repo must be **public** for free GitHub Pages + unlimited Actions minutes.

### 2. Turn on Pages

Repo → **Settings → Pages** → Source: *Deploy from a branch* → Branch: `main`, folder `/ (root)` → Save.

Your site goes live at `https://USERNAME.github.io/REPO/` within a minute or two.

> Publishing **from a branch** (not a custom Actions deploy workflow) matters here:
> it's what makes the `CNAME` file work for the custom domain.

### 3. Point the domain

At your DNS provider for `cherrypickyourdata.com`:

| Type | Name | Value |
|---|---|---|
| CNAME | `www` | `USERNAME.github.io` |
| A | `@` | `185.199.108.153` |
| A | `@` | `185.199.109.153` |
| A | `@` | `185.199.110.153` |
| A | `@` | `185.199.111.153` |

The `www` CNAME is what serves the site. The four apex A records make the
bare `cherrypickyourdata.com` work too (GitHub redirects it to `www`).

Then in **Settings → Pages → Custom domain**, enter `www.cherrypickyourdata.com`
and save. Once DNS resolves, tick **Enforce HTTPS** (the certificate can take
up to ~24h to issue; if it gets stuck, remove and re-add the custom domain to
force reissue).

### 4. Allow the workflow to push

Repo → **Settings → Actions → General → Workflow permissions** →
select **Read and write permissions** → Save.

Without this the daily job builds fine but can't commit the result.

### 5. Test it

Actions tab → *Update rolling-returns data* → **Run workflow**. It should
finish green and either commit an update or report "No new data".

## Updating by hand

```bash
pip install -r requirements.txt
python build_site.py              # fetch fresh data + rebuild
python build_site.py --offline    # rebuild from cache (e.g. after design edits)
```

Then commit `index.html` (and `data/monthly.csv` if data changed).

## Changing the design

Edit `template.html`, run `python build_site.py --offline`, and commit both
files. The template uses four placeholders that the build script fills in:
`__DATA_JSON__`, `__WINDOWS_JSON__`, `__FIRST_DATE__`, `__LAST_DATE__`,
plus `__LATEST_MONTH__` and `__BUILD_DATE__` for the footer stamp.

## If the data source ever moves

Shiller's file has lived at `econ.yale.edu/~shiller/data/ie_data.xls` for years,
but it's also mirrored via [shillerdata.com](https://shillerdata.com/). If the
daily job starts failing, update `SHILLER_URL` in `build_site.py`. The build
falls back to the cached CSV, so the live site keeps working (just with stale
data) rather than breaking.

## Notes

- Prices are **price-only** — they exclude dividends, so returns understate
  total return by roughly 2%/yr historically.
- Monthly resolution, so figures differ slightly from daily-close calculations.
- Chart.js loads from jsDelivr; the page needs internet access to render.
