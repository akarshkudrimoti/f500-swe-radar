"""Enumerate real ATS board tokens from the Common Crawl URL index.

Guessing a board address from a company name is what produced Chanel for
Charter Communications. Common Crawl already knows which boards exist: its URL
index is queryable by prefix, so asking it for every `boards.greenhouse.io/*`
it has ever seen turns guessing into a lookup.

Nothing is crawled here -- this reads an index of crawls that already happened.

    python scripts/cc_tokens.py --list-crawls
    python scripts/cc_tokens.py --crawl CC-MAIN-2026-34 --platform greenhouse
    python scripts/cc_tokens.py --crawl CC-MAIN-2026-34            # all platforms
"""
import argparse
import json
import pathlib
import re
import sys
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
INDEX = "https://index.commoncrawl.org"
UA = ("f500-swe-radar/1.0 (ATS token enumeration from the public CC index; "
      "contact via repo issues)")

# url pattern to ask the index for, and how to read a token out of a hit.
PLATFORMS = {
    "greenhouse": {
        "patterns": ["boards.greenhouse.io/*", "job-boards.greenhouse.io/*"],
        "token": re.compile(r"^https?://(?:job-)?boards\.greenhouse\.io/([^/?#]+)", re.I),
    },
    "lever": {
        "patterns": ["jobs.lever.co/*"],
        "token": re.compile(r"^https?://jobs\.lever\.co/([^/?#]+)", re.I),
    },
    "ashby": {
        "patterns": ["jobs.ashbyhq.com/*"],
        "token": re.compile(r"^https?://jobs\.ashbyhq\.com/([^/?#]+)", re.I),
    },
    "smartrecruiters": {
        "patterns": ["jobs.smartrecruiters.com/*"],
        "token": re.compile(r"^https?://jobs\.smartrecruiters\.com/([^/?#]+)", re.I),
    },
    "workable": {
        "patterns": ["apply.workable.com/*"],
        "token": re.compile(r"^https?://apply\.workable\.com/([^/?#]+)", re.I),
    },
    "workday": {
        # Every tenant is its own subdomain, so the wildcard goes on the host.
        "patterns": ["*.myworkdayjobs.com/*"],
        "token": re.compile(r"^https?://([a-z0-9-]+)\.wd(\d+)\.myworkdayjobs\.com/"
                            r"(?:[a-z]{2}-[A-Z]{2}/)?([^/?#]+)", re.I),
    },
}

# Paths that are the platform's own furniture, not a customer's board.
LOCALE = re.compile(r"^[a-z]{2}([-_][A-Za-z]{2})?$")

NOT_A_TOKEN = {
    "embed", "assets", "static", "favicon.ico", "robots.txt", "sitemap.xml",
    "css", "js", "images", "img", "api", "v1", "search", "jobs", "login",
    "signup", "privacy", "terms", "en-us", "en", "companies", "boards",
}


def session():
    s = requests.Session()
    s.headers["User-Agent"] = UA
    retry = Retry(total=5, backoff_factor=1.5,
                  status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=frozenset(["GET"]))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


def num_pages(s, crawl, pattern):
    r = s.get(f"{INDEX}/{crawl}-index",
              params={"url": pattern, "output": "json", "showNumPages": "true"},
              timeout=120)
    if r.status_code != 200:
        return 0
    try:
        return int(r.json().get("pages", 0))
    except ValueError:
        return 0


def fetch_page(s, crawl, pattern, page):
    """One index page of CDX records, as dicts. Returns [] on a miss."""
    for attempt in range(4):
        try:
            r = s.get(f"{INDEX}/{crawl}-index",
                      params={"url": pattern, "output": "json", "page": page},
                      timeout=180)
        except requests.RequestException:
            time.sleep(2 + 3 * attempt)
            continue
        if r.status_code == 404:
            return []
        if r.status_code != 200:
            time.sleep(2 + 3 * attempt)
            continue
        out = []
        for line in r.text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
        return out
    return []


def harvest(s, crawl, name, spec, max_pages, sleep):
    found = {}
    for pattern in spec["patterns"]:
        pages = num_pages(s, crawl, pattern)
        if not pages:
            print(f"  {name}: {pattern} -> no index pages", file=sys.stderr)
            continue
        take = min(pages, max_pages) if max_pages else pages
        print(f"  {name}: {pattern} -> {pages} pages, reading {take}",
              file=sys.stderr, flush=True)
        for page in range(take):
            recs = fetch_page(s, crawl, pattern, page)
            for rec in recs:
                url = rec.get("url") or ""
                m = spec["token"].match(url)
                if not m:
                    continue
                if name == "workday":
                    tenant, pod, site = m.group(1), m.group(2), m.group(3)
                    if site.lower() in NOT_A_TOKEN or LOCALE.match(site):
                        continue
                    key = f"{tenant}|wd{pod}|{site}"
                    found[key] = found.get(key, 0) + 1
                else:
                    tok = m.group(1)
                    if tok.lower() in NOT_A_TOKEN or len(tok) < 2:
                        continue
                    found[tok] = found.get(tok, 0) + 1
            if sleep:
                time.sleep(sleep)
            print(f"    page {page + 1}/{take}: {len(found)} distinct so far",
                  file=sys.stderr, flush=True)
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--crawl", default="CC-MAIN-2026-34")
    ap.add_argument("--platform", help="one of " + ", ".join(PLATFORMS))
    ap.add_argument("--max-pages", type=int, default=0, help="0 = all pages")
    ap.add_argument("--sleep", type=float, default=0.5,
                    help="pause between index pages; the index is a free service")
    ap.add_argument("--list-crawls", action="store_true")
    args = ap.parse_args()

    s = session()
    if args.list_crawls:
        for c in s.get(f"{INDEX}/collinfo.json", timeout=60).json()[:12]:
            print(f"{c['id']:22} {c['name']}")
        return

    todo = ({args.platform: PLATFORMS[args.platform]} if args.platform
            else PLATFORMS)
    out_path = DATA / "cc_tokens.json"
    store = {}
    if out_path.exists():
        store = json.loads(out_path.read_text(encoding="utf-8"))

    for name, spec in todo.items():
        print(f"{name}:", file=sys.stderr, flush=True)
        found = harvest(s, args.crawl, name, spec, args.max_pages, args.sleep)
        merged = store.get(name, {})
        for k, v in found.items():
            merged[k] = merged.get(k, 0) + v
        store[name] = merged
        out_path.write_text(json.dumps(store, indent=1, sort_keys=True),
                            encoding="utf-8")
        print(f"  {name}: {len(merged)} distinct tokens stored",
              file=sys.stderr, flush=True)

    print("\ntotals:", file=sys.stderr)
    for k, v in store.items():
        print(f"  {k:16} {len(v)}", file=sys.stderr)


if __name__ == "__main__":
    main()
