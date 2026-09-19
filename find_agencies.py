#!/usr/bin/env python3
"""
find_agencies.py - daily discovery of small marketing-agency LinkedIn company pages.

Feeds the "agency-candidates-<date>.csv" file that the Apps Script verifier reads.
Output columns: agency,linkedin_company,country

Sources (all free, no API keys):
  1. search    - metasearch (ddgs) for  site:linkedin.com/company "<niche>" <city>
  2. overpass  - OpenStreetMap businesses tagged as advertising/marketing agencies,
                 then their website is fetched and the LinkedIn link read from the HTML
  3. websites  - an optional CSV of websites you already have (e.g. a Google Maps
                 export): same website -> LinkedIn extraction

LinkedIn itself is never fetched. Company URLs come from public search results and
from links that agencies publish on their own sites.

Usage:
  python find_agencies.py --out candidates                 # all sources, today's rotation
  python find_agencies.py --sources search --queries 60
  python find_agencies.py --sources websites --websites-csv maps_export.csv
  python find_agencies.py --dry-run                        # print, write nothing
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import datetime as dt
import os
import random
import re
import sys
import time
from typing import Iterable
from urllib.parse import unquote

import requests

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"

# Countries in priority order. ISO2 -> cities used in search queries.
CITIES: dict[str, list[str]] = {
    "US": ["New York", "Los Angeles", "Chicago", "Austin", "Dallas", "Miami", "Atlanta",
           "Denver", "Seattle", "Boston", "Phoenix", "San Diego", "Houston", "Nashville",
           "Charlotte", "Tampa", "Portland", "Minneapolis", "Philadelphia", "Las Vegas"],
    "AE": ["Dubai", "Abu Dhabi", "Sharjah", "Ajman", "Ras Al Khaimah"],
    "GB": ["London", "Manchester", "Birmingham", "Leeds", "Bristol", "Glasgow", "Edinburgh"],
    "CA": ["Toronto", "Vancouver", "Montreal", "Calgary", "Ottawa", "Edmonton"],
    "AU": ["Sydney", "Melbourne", "Brisbane", "Perth", "Adelaide", "Gold Coast"],
    "SA": ["Riyadh", "Jeddah", "Dammam", "Khobar"],
    "QA": ["Doha"],
    "KW": ["Kuwait City"],
    "BH": ["Manama"],
    "OM": ["Muscat"],
    "IE": ["Dublin", "Cork"],
    "SG": ["Singapore"],
    "NZ": ["Auckland", "Wellington"],
    "ZA": ["Johannesburg", "Cape Town", "Durban"],
    "DE": ["Berlin", "Munich", "Hamburg", "Frankfurt"],
    "NL": ["Amsterdam", "Rotterdam", "Utrecht"],
    "IN": ["Mumbai", "Bangalore", "Delhi", "Pune", "Hyderabad"],
}

# How many search queries each country gets per day, relative to the others.
WEIGHT = {"US": 6, "AE": 5, "GB": 3, "CA": 3, "AU": 3, "SA": 3, "QA": 1, "KW": 1,
          "BH": 1, "OM": 1, "IE": 1, "SG": 1, "NZ": 1, "ZA": 1, "DE": 1, "NL": 1, "IN": 1}

NICHES = [
    "PPC agency", "performance marketing agency", "lead generation agency",
    "paid social agency", "Google Ads agency", "digital marketing agency",
    "demand generation agency", "paid media agency", "growth marketing agency",
]

# A candidate must look like an agency.
AGENCY_WORDS = re.compile(
    r"\b(agency|agencia|agence|marketing|media|advertis\w*|ads|ppc|sem|seo|creative|"
    r"growth|leads?|lead-?gen\w*|performance|digital|branding|promo\w*|studio)\b", re.I)

# ... and must not look like one of these.
BLOCK_NAMES = re.compile(
    r"\b(wpp|ogilvy|publicis|dentsu|omnicom|havas|accenture|deloitte|pwc|kpmg|ey|mckinsey|"
    r"mccann|leo burnett|saatchi|bbdo|ddb|grey group|vml|wunderman|isobar|mindshare|groupm|"
    r"zenith|starcom|initiative|essence|iprospect|merkle|razorfish|edelman|weber shandwick|"
    r"ipg|interpublic|google|meta|facebook|linkedin|microsoft|amazon|adobe|salesforce|hubspot|"
    r"semrush|ahrefs|similarweb|university|college|school|institute|academy|bootcamp|"
    r"conference|summit|awards|association|federation|chamber|magazine|podcast|newsletter|"
    r"jobs?|careers?|recruit\w*|staffing|freelanc\w*|marketplace|directory|template)\b", re.I)

# Software/SaaS rather than a services agency.
BLOCK_SAAS = re.compile(
    r"\b(software|saas|platform|app|apps|crm|erp|api|sdk|cloud|hosting|analytics tool|"
    r"tool|tools|technolog\w*|labs?|systems?|solutions? inc)\b", re.I)

# LinkedIn slugs that are never a company page we want.
BAD_SLUG = re.compile(
    r"^(company|showcase|school|jobs|feed|login|signup|posts|pulse|learning|products|"
    r"groups|events|in)$", re.I)

LI_COMPANY = re.compile(r"linkedin\.com/(?:company|showcase)/([A-Za-z0-9\-_%\.]+)", re.I)

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

OVERPASS_QUERY = """
[out:json][timeout:90];
area["ISO3166-1"="{iso}"][admin_level=2]->.a;
(
  nwr["office"="advertising_agency"](area.a);
  nwr["office"="marketing"](area.a);
  nwr["shop"="advertising_agency"](area.a);
  nwr["office"="consulting"]["consulting"="marketing"](area.a);
);
out tags center {limit};
"""


# --------------------------------------------------------------------------- #
# Pure helpers (unit-tested)
# --------------------------------------------------------------------------- #

def clean_linkedin(url: str) -> str:
    """Return https://www.linkedin.com/company/<slug> or '' if it is not a usable company URL."""
    if not url:
        return ""
    u = unquote(str(url))
    m = LI_COMPANY.search(u)
    if not m:
        return ""
    slug = m.group(1).strip().strip(".").lower()
    slug = slug.split("?")[0].split("#")[0].rstrip("/")
    if not slug or BAD_SLUG.match(slug) or len(slug) < 2:
        return ""
    return "https://www.linkedin.com/company/" + slug


def slug_of(linkedin_url: str) -> str:
    m = LI_COMPANY.search(linkedin_url or "")
    return m.group(1).lower() if m else ""


def clean_name(title: str) -> str:
    """Turn a search-result title into a company name."""
    t = re.sub(r"\s*\|\s*LinkedIn\s*$", "", str(title or ""), flags=re.I)
    t = re.sub(r"\s*[-–|:]\s*(LinkedIn|Jobs|Life|People|About|Posts|Overview)\s*$", "", t, flags=re.I)
    # "Acme Media | 1,234 followers on LinkedIn"  ->  "Acme Media"
    t = re.split(r"\s*\|\s*\d", t)[0]
    t = re.sub(r"\s+", " ", t).strip(" -–|·•,")
    return t.strip()


def looks_like_agency(name: str, slug: str = "") -> bool:
    """Heuristic filter: agency-ish, not a giant, not a SaaS tool."""
    blob = f"{name} {slug.replace('-', ' ')}"
    if not name or len(name) < 3:
        return False
    if BLOCK_NAMES.search(blob):
        return False
    if BLOCK_SAAS.search(blob) and not re.search(r"\bagency\b", blob, re.I):
        return False
    return bool(AGENCY_WORDS.search(blob))


def daily_queries(day: int, total: int) -> list[tuple[str, str]]:
    """
    (query, country) pairs for one day. The offset moves with the day number so the
    same city/niche combinations are not repeated every morning.
    """
    combos: list[tuple[str, str]] = []
    for iso, cities in CITIES.items():
        for city in cities:
            for niche in NICHES:
                combos.append((f'site:linkedin.com/company "{niche}" {city}', iso))
    # Stable shuffle, then weighted round-robin so priority countries get more slots.
    random.Random(1234).shuffle(combos)
    by_country: dict[str, list[tuple[str, str]]] = {}
    for q, iso in combos:
        by_country.setdefault(iso, []).append((q, iso))

    out: list[tuple[str, str]] = []
    order = [iso for iso in CITIES for _ in range(WEIGHT.get(iso, 1))]
    i = 0
    while len(out) < total and order:
        iso = order[i % len(order)]
        pool = by_country.get(iso) or []
        if pool:
            out.append(pool[(day * 7 + i) % len(pool)])
        i += 1
        if i > total * 20:
            break
    # de-duplicate while preserving order
    seen, uniq = set(), []
    for q, iso in out:
        if q not in seen:
            seen.add(q)
            uniq.append((q, iso))
    return uniq


def extract_linkedin_from_html(html: str) -> str:
    """First usable LinkedIn company URL found in a page's HTML."""
    for m in LI_COMPANY.finditer(html or ""):
        url = clean_linkedin(m.group(0))
        if url:
            return url
    return ""


def merge(rows: Iterable[dict], seen_slugs: set[str]) -> list[dict]:
    """Drop rows without a LinkedIn slug, rows already seen, and repeats inside the batch."""
    out, batch = [], set()
    for r in rows:
        s = slug_of(r.get("linkedin_company", ""))
        if not s or s in seen_slugs or s in batch:
            continue
        if not looks_like_agency(r.get("agency", ""), s):
            continue
        batch.add(s)
        out.append({"agency": r["agency"], "linkedin_company": r["linkedin_company"],
                    "country": (r.get("country") or "").upper()[:2]})
    return out


# --------------------------------------------------------------------------- #
# Source 1: metasearch
# --------------------------------------------------------------------------- #

def from_search(queries: list[tuple[str, str]], per_query: int, pause: float, log=print) -> list[dict]:
    try:
        from ddgs import DDGS
    except ImportError:
        log("ddgs is not installed - skipping the search source (pip install ddgs)")
        return []

    rows: list[dict] = []
    for n, (q, iso) in enumerate(queries, 1):
        try:
            with DDGS() as d:
                hits = list(d.text(q, max_results=per_query))
        except Exception as e:                                    # blocked, rate limited, offline
            log(f"  search {n}/{len(queries)} failed ({type(e).__name__}) - {q}")
            time.sleep(pause * 3)
            continue
        got = 0
        for h in hits:
            url = clean_linkedin(h.get("href") or h.get("url") or "")
            name = clean_name(h.get("title") or "")
            if url and name:
                rows.append({"agency": name, "linkedin_company": url, "country": iso})
                got += 1
        log(f"  search {n}/{len(queries)} +{got:<2} {q}")
        time.sleep(pause)
    return rows


# --------------------------------------------------------------------------- #
# Source 2: OpenStreetMap -> agency website -> LinkedIn link
# --------------------------------------------------------------------------- #

def from_overpass(countries: list[str], limit: int, log=print) -> list[dict]:
    found: list[dict] = []
    for iso in countries:
        body = OVERPASS_QUERY.format(iso=iso, limit=limit)
        data = None
        for ep in OVERPASS_ENDPOINTS:
            try:
                r = requests.post(ep, data={"data": body}, timeout=120,
                                  headers={"User-Agent": UA})
                if r.status_code == 200:
                    data = r.json()
                    break
                log(f"  overpass {iso}: HTTP {r.status_code} from {ep}")
            except Exception as e:
                log(f"  overpass {iso}: {type(e).__name__} from {ep}")
        if not data:
            continue
        n = 0
        for el in data.get("elements", []):
            t = el.get("tags", {})
            name = (t.get("name") or "").strip()
            site = (t.get("website") or t.get("contact:website") or "").strip()
            if not name or not site:
                continue
            if not site.startswith("http"):
                site = "https://" + site
            found.append({"agency": name, "website": site, "country": iso})
            n += 1
        log(f"  overpass {iso}: {n} businesses with a website")
        time.sleep(2)
    return found


def linkedin_from_websites(items: list[dict], workers: int, log=print) -> list[dict]:
    """Fetch each website's homepage and read the LinkedIn company link out of the HTML."""
    def one(it: dict) -> dict | None:
        for path in ("", "/contact", "/about"):
            try:
                r = requests.get(it["website"].rstrip("/") + path, timeout=15,
                                 headers={"User-Agent": UA}, allow_redirects=True)
                if r.status_code != 200 or "text/html" not in r.headers.get("content-type", ""):
                    continue
                url = extract_linkedin_from_html(r.text[:400_000])
                if url:
                    return {"agency": it["agency"], "linkedin_company": url,
                            "country": it.get("country", "")}
            except Exception:
                pass
        return None

    out: list[dict] = []
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        for i, res in enumerate(ex.map(one, items), 1):
            if res:
                out.append(res)
            if i % 25 == 0:
                log(f"  websites: {i}/{len(items)} checked, {len(out)} LinkedIn links")
    log(f"  websites: {len(items)} checked, {len(out)} LinkedIn links")
    return out


def read_websites_csv(path: str, log=print) -> list[dict]:
    """A CSV with at least a website column (e.g. a Google Maps export)."""
    items: list[dict] = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            low = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            site = low.get("website") or low.get("url") or low.get("site") or ""
            name = low.get("agency") or low.get("name") or low.get("title") or ""
            iso = (low.get("country") or "").upper()[:2]
            if site and name:
                if not site.startswith("http"):
                    site = "https://" + site
                items.append({"agency": name, "website": site, "country": iso})
    log(f"  websites csv: {len(items)} rows with a website")
    return items


# --------------------------------------------------------------------------- #
# Seen list + output
# --------------------------------------------------------------------------- #

def load_seen(path: str) -> set[str]:
    if not os.path.exists(path):
        return set()
    with open(path, newline="", encoding="utf-8") as f:
        return {(r.get("slug") or "").strip().lower() for r in csv.DictReader(f) if r.get("slug")}


def save_seen(path: str, slugs: Iterable[str], today: str) -> None:
    new = sorted(set(slugs))
    exists = os.path.exists(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow(["slug", "first_seen"])
        for s in new:
            w.writerow([s, today])


def write_csv(path: str, rows: list[dict]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["agency", "linkedin_company", "country"])
        w.writeheader()
        w.writerows(rows)


# --------------------------------------------------------------------------- #

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--sources", default="search,overpass",
                   help="comma list: search, overpass, websites (default: search,overpass)")
    p.add_argument("--out", default="candidates", help="output directory")
    p.add_argument("--seen", default="candidates/seen.csv", help="file of slugs already emitted")
    p.add_argument("--queries", type=int, default=40, help="search queries per run")
    p.add_argument("--per-query", type=int, default=15, help="results per search query")
    p.add_argument("--pause", type=float, default=2.5, help="seconds between searches")
    p.add_argument("--overpass-countries", default="AE,SA,QA,KW,BH,OM,IE,NZ",
                   help="ISO2 list for OpenStreetMap (small countries work best)")
    p.add_argument("--overpass-limit", type=int, default=400, help="max businesses per country")
    p.add_argument("--websites-csv", default="", help="CSV of websites for the websites source")
    p.add_argument("--workers", type=int, default=12, help="parallel website fetches")
    p.add_argument("--dry-run", action="store_true", help="print results, write nothing")
    a = p.parse_args(argv)

    today = dt.date.today()
    stamp = today.isoformat()
    sources = [s.strip() for s in a.sources.split(",") if s.strip()]
    seen = load_seen(a.seen)
    print(f"find_agencies {stamp} | sources={sources} | {len(seen)} slugs already seen")

    raw: list[dict] = []

    if "search" in sources:
        qs = daily_queries(today.toordinal(), a.queries)
        print(f"search: {len(qs)} queries")
        raw += from_search(qs, a.per_query, a.pause)

    site_items: list[dict] = []
    if "overpass" in sources:
        isos = [c.strip().upper() for c in a.overpass_countries.split(",") if c.strip()]
        print(f"overpass: {isos}")
        site_items += from_overpass(isos, a.overpass_limit)
    if "websites" in sources and a.websites_csv:
        site_items += read_websites_csv(a.websites_csv)
    if site_items:
        print(f"websites: resolving LinkedIn for {len(site_items)} sites")
        raw += linkedin_from_websites(site_items, a.workers)

    rows = merge(raw, seen)
    print(f"\n{len(raw)} raw -> {len(rows)} new candidates")

    if a.dry_run:
        for r in rows[:40]:
            print(f"  {r['country']}  {r['agency'][:44]:<44} {r['linkedin_company']}")
        if len(rows) > 40:
            print(f"  ... and {len(rows) - 40} more")
        return 0

    if not rows:
        print("nothing new - no file written")
        return 0

    out = os.path.join(a.out, f"agency-candidates-{stamp}.csv")
    write_csv(out, rows)
    save_seen(a.seen, (slug_of(r["linkedin_company"]) for r in rows), stamp)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
