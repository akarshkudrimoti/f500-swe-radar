"""Pull current SWE internship postings from every company with a live feed.

Only companies whose ATS was resolved AND whose robots.txt does not disallow
crawling are queried; the rest stay in the radar as directory links.

    python scripts/fetch_listings.py [--limit 50] [--company "Nvidia"]
"""
import argparse
import datetime as dt
import json
import pathlib
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from classify import classify, is_us
from common import session
from custom_boards import BOARDS

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
TIMEOUT = 20
TODAY = dt.date.today()

# Workday has no "is this an internship" facet we can rely on across tenants,
# and searchText is a loose OR match -- "software intern" returns everything
# matching *either* word. So we cast one wide net per term, page all the way
# through it, and let classify.py do the actual filtering.
WORKDAY_TERMS = ("intern", "co-op")
PAGE = 20                    # Workday ignores limit > 20 and returns nothing
WORKDAY_MAX_RESULTS = 800    # ~40 pages; no tenant has more real internships


def _age_days(value):
    """Normalise the many shapes of 'when was this posted' into whole days."""
    if value is None:
        return None
    if isinstance(value, (int, float)):  # epoch millis (Lever, Ashby)
        try:
            ms = float(value)
            d = dt.datetime.utcfromtimestamp(ms / 1000 if ms > 1e11 else ms).date()
            return max((TODAY - d).days, 0)
        except (ValueError, OSError, OverflowError):
            return None
    s = str(value)
    m = re.search(r"(\d+)\+?\s*(day|week|month|hour|minute)", s, re.I)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        return {"minute": 0, "hour": 0, "day": n, "week": n * 7, "month": n * 30}[unit]
    if re.search(r"today|just posted", s, re.I):
        return 0
    if re.search(r"yesterday", s, re.I):
        return 1
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return max((TODAY - dt.date(*map(int, m.groups()))).days, 0)
        except ValueError:
            return None
    return None


def _row(company, title, location, url, posted):
    loc = (location or "").strip()
    return {"company": company["name"], "rank": company["rank"],
            "sector": company["sector"], "title": title.strip(),
            "location": loc or "Not stated", "is_us": is_us(loc),
            "url": url, "age_days": _age_days(posted), "ats": company["ats"]}


# --- per-ATS fetchers ------------------------------------------------------

def from_workday(c):
    s, out, seen = session(), [], set()
    host = f"https://{c['tenant']}.wd{c['pod']}.myworkdayjobs.com"
    for term in WORKDAY_TERMS:
        offset, total = 0, None
        while offset < WORKDAY_MAX_RESULTS:
            try:
                r = s.post(c["api"], timeout=TIMEOUT,
                           json={"appliedFacets": {}, "limit": PAGE,
                                 "offset": offset, "searchText": term})
                body = r.json() if r.status_code == 200 else None
            except Exception:
                break
            if not body:
                break
            if total is None:
                total = body.get("total") or 0
            posts = body.get("jobPostings") or []
            for p in posts:
                path = p.get("externalPath") or ""
                title = p.get("title") or ""
                if not path or path in seen:
                    continue
                seen.add(path)
                if not classify(title)["keep"]:
                    continue
                out.append(_row(c, title, p.get("locationsText"),
                                f"{host}/{c['site']}{path}", p.get("postedOn")))
            offset += PAGE
            # Some tenants report total only on the first page, then send 0.
            if len(posts) < PAGE or (total and offset >= total):
                break
    return out


def from_greenhouse(c):
    r = session().get(f"https://boards-api.greenhouse.io/v1/boards/"
                      f"{c['token']}/jobs?content=false", timeout=TIMEOUT)
    return [_row(c, j["title"], (j.get("location") or {}).get("name"),
                 j.get("absolute_url"), j.get("updated_at"))
            for j in (r.json().get("jobs") or []) if classify(j["title"])["keep"]]


def from_lever(c):
    r = session().get(f"https://api.lever.co/v0/postings/{c['token']}?mode=json",
                      timeout=TIMEOUT)
    return [_row(c, j["text"], (j.get("categories") or {}).get("location"),
                 j.get("hostedUrl"), j.get("createdAt"))
            for j in r.json() if classify(j.get("text", ""))["keep"]]


def from_ashby(c):
    r = session().get(f"https://api.ashbyhq.com/posting-api/job-board/{c['token']}",
                      timeout=TIMEOUT)
    return [_row(c, j["title"], j.get("location"), j.get("jobUrl"),
                 j.get("publishedAt"))
            for j in (r.json().get("jobs") or []) if classify(j["title"])["keep"]]


def from_smartrecruiters(c):
    s, out, offset = session(), [], 0
    while offset < 400:
        r = s.get(f"https://api.smartrecruiters.com/v1/companies/{c['token']}"
                  f"/postings?limit=100&offset={offset}", timeout=TIMEOUT)
        if r.status_code != 200:
            break
        body = r.json()
        for j in body.get("content") or []:
            if not classify(j.get("name", ""))["keep"]:
                continue
            loc = j.get("location") or {}
            out.append(_row(c, j["name"],
                            ", ".join(x for x in (loc.get("city"), loc.get("country")) if x),
                            f"https://jobs.smartrecruiters.com/{c['token']}/{j['id']}",
                            j.get("releasedDate")))
        offset += 100
        if offset >= body.get("totalFound", 0):
            break
    return out


def from_custom(c):
    """Dispatch to a hand-written board in custom_boards.BOARDS."""
    board = BOARDS.get(c.get("board"))
    if board is None:
        return []
    return board(c, _row)


FETCHERS = {"workday": from_workday, "greenhouse": from_greenhouse,
            "lever": from_lever, "ashby": from_ashby,
            "smartrecruiters": from_smartrecruiters, "custom": from_custom}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--company", help="fetch a single company by name")
    ap.add_argument("--limit", type=int, help="stop after N companies (smoke test)")
    ap.add_argument("--workers", type=int, default=10)
    args = ap.parse_args()

    ats = json.loads((DATA / "ats.json").read_text(encoding="utf-8"))
    live = [c for c in ats if c.get("ats") in FETCHERS and c.get("crawl_allowed")]
    if args.company:
        live = [c for c in live if c["name"].lower() == args.company.lower()]
    if args.limit:
        live = live[:args.limit]

    print(f"querying {len(live)} live feeds", file=sys.stderr, flush=True)

    rows, failed, done = [], [], 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(FETCHERS[c["ats"]], c): c for c in live}
        for f in as_completed(futs):
            c = futs[f]
            done += 1
            try:
                got = f.result()
            except Exception as e:
                failed.append({"company": c["name"], "error": str(e)[:120]})
                continue
            rows.extend(got)
            if got:
                print(f"  [{done}/{len(live)}] {c['name']}: {len(got)}",
                      file=sys.stderr, flush=True)

    # Newest first; unknown ages sink to the bottom rather than jumping the queue.
    rows.sort(key=lambda r: (r["age_days"] is None, r["age_days"] or 0, r["rank"]))

    (DATA / "listings.json").write_text(encoding="utf-8", data=json.dumps(
        {"generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
         "companies_queried": len(live), "companies_failed": failed,
         "count": len(rows), "listings": rows}, indent=1))

    print(f"\n{len(rows)} postings from "
          f"{len({r['company'] for r in rows})} companies; {len(failed)} feeds errored",
          file=sys.stderr)


if __name__ == "__main__":
    main()
