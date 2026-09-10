"""Detect which boards changed, using one request per company.

A real fetch pages through every board and costs ~8 minutes, which is why the
radar could only refresh a few times a day. But noticing that a board changed
is far cheaper than reading it: every platform reports a total, and one request
gets it. ~320 companies check in well under a minute.

So the loop becomes: probe everything cheaply, and pay for a full fetch only on
the boards that actually moved. Most runs find nothing and cost one minute.

    python scripts/watch.py                 # -> data/signatures.json, prints changed
    python scripts/watch.py --print-only    # detect without recording

Exit code 0 when something changed, 1 when nothing did, so a workflow can skip
the expensive half with `if:`.
"""
import argparse
import json
import pathlib
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from common import session

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
TIMEOUT = 20


def _count(r, path):
    """Read a total out of a JSON response, tolerating every shape."""
    if r is None or r.status_code != 200:
        return None
    try:
        body = r.json()
    except ValueError:
        return None
    if path == "len_jobs":
        return len(body.get("jobs") or [])
    if path == "len_list":
        return len(body) if isinstance(body, list) else None
    return body.get(path)


def signature(c):
    """A cheap number that moves when the board's postings move."""
    s = session()
    ats = c.get("ats")
    try:
        if ats == "workday":
            r = s.post(c["api"], timeout=TIMEOUT,
                       json={"appliedFacets": {}, "limit": 1, "offset": 0,
                             "searchText": "intern"})
            return _count(r, "total")
        if ats == "greenhouse":
            r = s.get(f"https://boards-api.greenhouse.io/v1/boards/"
                      f"{c['token']}/jobs?content=false", timeout=TIMEOUT)
            return _count(r, "len_jobs")
        if ats == "lever":
            r = s.get(f"https://api.lever.co/v0/postings/{c['token']}?mode=json",
                      timeout=TIMEOUT)
            return _count(r, "len_list")
        if ats == "ashby":
            r = s.get(f"https://api.ashbyhq.com/posting-api/job-board/{c['token']}",
                      timeout=TIMEOUT)
            return _count(r, "len_jobs")
        if ats == "smartrecruiters":
            r = s.get(f"https://api.smartrecruiters.com/v1/companies/"
                      f"{c['token']}/postings?limit=1", timeout=TIMEOUT)
            return _count(r, "totalFound")
        if ats == "custom":
            return _custom_signature(c, s)
    except Exception:
        return None
    return None


def _custom_signature(c, s):
    board = c.get("board")
    if board == "amazon":
        r = s.get("https://www.amazon.jobs/en/search.json",
                  params={"base_query": "intern", "result_limit": 1},
                  timeout=TIMEOUT)
        return _count(r, "hits")
    if board == "apple":
        # Apple ships totals inside the page's hydration blob, so reuse the
        # same parser rather than inventing a second one.
        from custom_boards import APPLE_INTERN_TEAM, _hydration
        d = _hydration(s, "https://jobs.apple.com/en-us/search"
                          f"?team={APPLE_INTERN_TEAM}&sort=newest")
        if not d:
            return None
        return ((d.get("loaderData") or {}).get("search") or {}).get("totalRecords")
    if board == "eightfold":
        r = s.get(f"{c['host']}/api/apply/v2/jobs",
                  params={"domain": c["domain"], "query": "intern",
                          "start": 0, "num": 1}, timeout=TIMEOUT)
        return _count(r, "count")
    if board == "oracle_orc":
        r = s.get(f"{c['host']}/hcmRestApi/resources/latest/"
                  "recruitingCEJobRequisitions",
                  params={"onlyData": "true",
                          "expand": "requisitionList.secondaryLocations",
                          "finder": f"findReqs;siteNumber={c.get('site', 'CX_1')},"
                                    "limit=1,keyword=intern"},
                  headers={"Accept": "application/json"}, timeout=TIMEOUT)
        if r is None or r.status_code != 200:
            return None
        try:
            return (r.json().get("items") or [{}])[0].get("TotalJobsCount")
        except (ValueError, IndexError):
            return None
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--print-only", action="store_true",
                    help="report changes without updating signatures.json")
    ap.add_argument("--workers", type=int, default=16)
    args = ap.parse_args()

    ats = json.loads((DATA / "ats.json").read_text(encoding="utf-8"))
    live = [c for c in ats if c.get("ats") not in (None, "unknown")
            and c.get("crawl_allowed")]

    sig_path = DATA / "signatures.json"
    old = {}
    if sig_path.exists():
        old = json.loads(sig_path.read_text(encoding="utf-8")).get("signatures", {})

    new, unreachable = {}, []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(signature, c): c for c in live}
        for f in as_completed(futs):
            c = futs[f]
            try:
                v = f.result()
            except Exception:
                v = None
            if v is None:
                unreachable.append(c["name"])
            else:
                new[c["name"]] = v

    # A board that fails to answer is not a change; keeping its old signature
    # stops a flaky endpoint from triggering a full fetch every single run.
    changed = sorted(n for n, v in new.items() if old.get(n) != v)
    first_run = not old

    print(f"probed {len(live)} boards, {len(unreachable)} unreachable, "
          f"{len(changed)} changed", file=sys.stderr)
    for n in changed[:40]:
        print(f"  {n}: {old.get(n)} -> {new[n]}", file=sys.stderr)

    if not args.print_only:
        merged = {**old, **new}
        sig_path.write_text(json.dumps(
            {"signatures": merged, "unreachable": sorted(unreachable)},
            indent=1, sort_keys=True), encoding="utf-8")

    # The names go to stdout so a workflow can pass them straight to
    # fetch_listings --companies.
    if changed and not first_run:
        print(",".join(changed))
    elif first_run:
        print("__ALL__")

    sys.exit(0 if (changed or first_run) else 1)


if __name__ == "__main__":
    main()
