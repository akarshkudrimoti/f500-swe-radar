"""Work out which applicant tracking system each Fortune 500 company runs.

Probing order is cheapest-and-most-likely first. Workday dominates the Fortune
500, and it gives itself away in robots.txt: the Sitemap/Allow lines name the
career site exactly, so one request per pod resolves tenant *and* site.

Results are cached in data/ats.json and merged on re-run, so a partial run is
never wasted and manual entries in data/overrides.json always win.

    python scripts/discover_ats.py [--only "NVIDIA,Target"] [--recheck-unknown]
"""
import argparse
import json
import pathlib
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import re

from common import load_companies, normalize, rank_key, session, tokens

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

# Ordered by how many Fortune 500 tenants sit on each pod.
PODS = [5, 1, 3, 12, 10, 2, 103, 101, 105, 8, 6, 501, 502, 4, 11]
TIMEOUT = 8


def _json(r):
    try:
        return r.json()
    except ValueError:
        return None


# --- individual ATS probes -------------------------------------------------
# Each returns a dict on a confirmed hit, or None. A hit must prove there are
# real postings behind it: a 200 on an empty board is a false positive.

# Workday paths that are plumbing, never a career site.
NOT_A_SITE = {"refreshfacet", "talentcommunity", "static", "wday", "assets", "images"}


def parse_robots(text):
    """-> (candidate site names, {site: crawl_allowed}) for the wildcard agent.

    Tenants advertise their site name in robots.txt one way or another: NVIDIA
    via Sitemap/Allow, Lowe's via Disallow. Both name the site; only one of them
    consents to being crawled, and we record which.
    """
    allow, disallow = set(), set()
    for line in text.splitlines():
        line = line.strip()
        low = line.lower()
        bucket = None
        if low.startswith("allow:"):
            bucket = allow
        elif low.startswith("disallow:"):
            bucket = disallow
        elif low.startswith("sitemap:"):
            parts = line.split(":", 1)[1].strip().split("/")
            if len(parts) > 3 and parts[3] and parts[3].lower() not in NOT_A_SITE:
                allow.add(parts[3])
            continue
        if bucket is None:
            continue
        p = line.split(":", 1)[1].strip().strip("/")
        if p and "/" not in p and p.lower() not in NOT_A_SITE:
            bucket.add(p)

    sites = list(allow | disallow)
    # Prefer the external-facing site when a tenant exposes several.
    sites.sort(key=lambda x: (("external" not in x.lower()), ("career" not in x.lower())))
    return sites, {s: (s in allow) for s in sites}


def _pod_robots(slug, pod):
    """-> (pod, robots text) if this tenant lives on this pod, else None."""
    try:
        r = session().get(f"https://{slug}.wd{pod}.myworkdayjobs.com/robots.txt",
                          timeout=TIMEOUT)
    except Exception:
        return None
    return (pod, r.text) if r.status_code == 200 else None


# One shared, bounded pool for pod probes. A pool per call spawned ~150 threads
# at once, which throttled every request including the ones that would have hit.
PROBE_POOL = ThreadPoolExecutor(max_workers=24, thread_name_prefix="probe")


def try_workday(slug):
    # A tenant sits on exactly one pod and the rest answer with an error, so the
    # pods are asked together rather than in sequence.
    hits = []
    for f in as_completed([PROBE_POOL.submit(_pod_robots, slug, p) for p in PODS]):
        try:
            got = f.result()
        except Exception:
            got = None
        if got:
            hits.append(got)
    hits.sort(key=lambda h: PODS.index(h[0]))

    s = session()
    for pod, robots in hits:
        host = f"https://{slug}.wd{pod}.myworkdayjobs.com"
        sites, allowed = parse_robots(robots)

        for site in sites[:4]:
            url = f"{host}/wday/cxs/{slug}/{site}/jobs"
            try:
                jr = s.post(url, json={"appliedFacets": {}, "limit": 1, "offset": 0,
                                       "searchText": ""}, timeout=TIMEOUT)
            except Exception:
                continue
            body = _json(jr) if jr.status_code == 200 else None
            if body and body.get("total", 0) > 0:
                return {"ats": "workday", "tenant": slug, "pod": pod, "site": site,
                        "api": url, "careers_url": f"{host}/{site}",
                        "total_open": body["total"],
                        "crawl_allowed": allowed.get(site, False)}
    return None


def try_greenhouse(slug):
    try:
        r = session().get(
            f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=false",
            timeout=TIMEOUT)
    except Exception:
        return None
    b = _json(r) if r.status_code == 200 else None
    if b and b.get("jobs"):
        return {"ats": "greenhouse", "token": slug, "crawl_allowed": True,
                "api": f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
                "careers_url": f"https://boards.greenhouse.io/{slug}",
                "total_open": len(b["jobs"])}
    return None


def try_lever(slug):
    try:
        r = session().get(f"https://api.lever.co/v0/postings/{slug}?mode=json",
                          timeout=TIMEOUT)
    except Exception:
        return None
    b = _json(r) if r.status_code == 200 else None
    if isinstance(b, list) and b:
        return {"ats": "lever", "token": slug, "crawl_allowed": True,
                "api": f"https://api.lever.co/v0/postings/{slug}?mode=json",
                "careers_url": f"https://jobs.lever.co/{slug}", "total_open": len(b)}
    return None


def try_ashby(slug):
    try:
        r = session().get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
                          timeout=TIMEOUT)
    except Exception:
        return None
    b = _json(r) if r.status_code == 200 else None
    if b and b.get("jobs"):
        return {"ats": "ashby", "token": slug, "crawl_allowed": True,
                "api": f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
                "careers_url": f"https://jobs.ashbyhq.com/{slug}",
                "total_open": len(b["jobs"])}
    return None


def try_smartrecruiters(slug):
    try:
        r = session().get(
            f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=1",
            timeout=TIMEOUT)
    except Exception:
        return None
    b = _json(r) if r.status_code == 200 else None
    if b and b.get("totalFound", 0) > 0:
        return {"ats": "smartrecruiters", "token": slug, "crawl_allowed": True,
                "api": f"https://api.smartrecruiters.com/v1/companies/{slug}/postings",
                "careers_url": f"https://jobs.smartrecruiters.com/{slug}",
                "total_open": b["totalFound"]}
    return None


PROBES = (try_workday, try_greenhouse, try_lever, try_ashby, try_smartrecruiters)


def corroborated(name, hit):
    """Does this board actually belong to this company?

    The only independent evidence a board offers is the career-site name --
    'WellsFargoJobsTargeted' proves it, 'ChanelCareers' disproves it, and a
    generic 'External' proves nothing. The non-Workday boards are addressed by
    the guessed token itself, so they offer no evidence at all.
    """
    site = re.sub(r"[^a-z0-9]", "", (hit.get("site") or "").lower())
    if not site:
        return False
    return any(t in site for t in tokens(name))


def resolve(company):
    # Past the first few, extra slug guesses cost far more than they resolve.
    for slug, strong in normalize(company["name"])[:5]:
        for probe in PROBES:
            hit = probe(slug)
            if not hit:
                continue
            if not strong and not corroborated(company["name"], hit):
                # A weak slug landed on somebody else's board. Keep looking.
                continue
            return {**company, **hit, "slug": slug, "match": "strong" if strong
                    else "corroborated"}
    return {**company, "ats": "unknown"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="comma-separated company names to (re)probe")
    ap.add_argument("--recheck-unknown", action="store_true",
                    help="re-probe companies previously left unresolved")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    companies = load_companies(DATA)
    cache_path = DATA / "ats.json"
    cache = {}
    if cache_path.exists():
        cache = {c["name"]: c for c in json.loads(cache_path.read_text(encoding="utf-8"))}

    overrides = {}
    ov_path = DATA / "overrides.json"
    if ov_path.exists():
        overrides = {o["name"]: o for o in json.loads(ov_path.read_text(encoding="utf-8"))}

    if args.only:
        wanted = {n.strip().lower() for n in args.only.split(",")}
        todo = [c for c in companies if c["name"].lower() in wanted]
    else:
        todo = [c for c in companies
                if c["name"] not in cache
                or (args.recheck_unknown and cache[c["name"]].get("ats") == "unknown")]
        todo = [c for c in todo if c["name"] not in overrides]

    print(f"{len(todo)} to probe, {len(cache)} cached, {len(overrides)} overridden",
          file=sys.stderr, flush=True)

    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(resolve, c): c for c in todo}
        for f in as_completed(futs):
            try:
                res = f.result()
            except Exception as e:
                res = {**futs[f], "ats": "unknown", "error": str(e)}
            cache[res["name"]] = res
            done += 1
            if done % 10 == 0:  # checkpoint: a killed run keeps what it found
                cache_path.write_text(encoding="utf-8", data=json.dumps(
                    sorted(cache.values(), key=rank_key), indent=1))
            if res["ats"] != "unknown":
                print(f"  [{done}/{len(todo)}] {res['name']}: {res['ats']} "
                      f"({res.get('total_open')} open)", file=sys.stderr, flush=True)
            elif done % 25 == 0:
                print(f"  [{done}/{len(todo)}] ...", file=sys.stderr, flush=True)

    for name, o in overrides.items():
        cache[name] = {**cache.get(name, {}), **o}

    rows = sorted(cache.values(), key=rank_key)
    cache_path.write_text(encoding="utf-8", data=json.dumps(rows, indent=1))

    resolved = sum(1 for r in rows if r.get("ats") != "unknown")
    by = {}
    for r in rows:
        by[r.get("ats", "unknown")] = by.get(r.get("ats", "unknown"), 0) + 1
    print(f"\nresolved {resolved}/{len(rows)}", file=sys.stderr)
    for k, v in sorted(by.items(), key=lambda kv: -kv[1]):
        print(f"  {k:16} {v}", file=sys.stderr)


if __name__ == "__main__":
    main()
