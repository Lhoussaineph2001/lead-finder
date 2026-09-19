# Agency discovery pipeline

Finds LinkedIn company pages of small marketing agencies and commits them as a daily
CSV. The Apps Script verifier (`lead_finder_v7.gs`) then checks which of them are
actually running Google ads.

## Try it locally first

```bash
pip install -r requirements.txt

# print results, write nothing
python find_agencies.py --sources search --queries 8 --dry-run

# real run: writes candidates/agency-candidates-<today>.csv
python find_agencies.py --sources search,overpass --queries 40
```

Useful flags: `--queries` (searches per run), `--per-query` (results each),
`--pause` (seconds between searches, raise it if you get blocked),
`--overpass-countries AE,SA,QA`, `--websites-csv maps_export.csv`.

## Run it daily on GitHub Actions (free)

1. Create a **public** GitHub repo, e.g. `lead-finder`.
2. Copy in `find_agencies.py`, `requirements.txt` and `.github/workflows/daily.yml`.
3. Settings → Actions → General → Workflow permissions → **Read and write**.
4. Actions tab → *Find agencies* → **Run workflow** to test it once.

It runs at 04:00 UTC (05:00 Morocco) and commits `candidates/agency-candidates-<date>.csv`.

## Connect it to the sheet

In `lead_finder_v7.gs`, set:

```js
githubRepo: 'YOUR-USERNAME/lead-finder',
```

`runDaily()` then pulls new CSVs straight from the repo — no Drive step. Files
dropped in Drive still work too, so you can keep using both.

## Sources

| Source | What it gives | Notes |
|---|---|---|
| `search` | LinkedIn company URL + name directly | Rotates city/niche daily so runs differ |
| `overpass` | OpenStreetMap agencies → their website → LinkedIn link in the HTML | Free, no key; patchy coverage, best for small countries |
| `websites` | Same extraction from a CSV you supply | For a Google Maps export or any list of sites |

LinkedIn is never fetched. URLs come from public search results and from links
agencies publish on their own websites.

## Duplicates

`candidates/seen.csv` records every slug already emitted, so the same agency is not
sent twice. Delete that file to start over.

## If a run returns nothing

The search backends rate-limit. Raise `--pause` to 5, lower `--queries`, or rerun
later. A failed query is logged and skipped, never fatal.
