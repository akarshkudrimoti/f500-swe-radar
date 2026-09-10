"""Hand-written fetchers for companies that run their own career site.

The biggest employers on the list -- Amazon, Walmart, Apple, Alphabet -- are
exactly the ones that don't use an off-the-shelf ATS, so automatic discovery
can never reach them. Each entry here is a deliberate, checked exception:
confirm the site's robots.txt permits it before adding one.

Register a fetcher in BOARDS, then point a data/overrides.json entry at it:

    {"name": "Amazon", "ats": "custom", "board": "amazon",
     "crawl_allowed": true, "careers_url": "https://www.amazon.jobs/en/search"}
"""
import datetime as dt
import json
import re

from classify import classify
from common import session

TIMEOUT = 25
APPLE_MAX_PAGES = 15                        # 20 per page; the team holds ~70
APPLE_INTERN_TEAM = "internships-STDNT-INTRN"  # Apple's own internships team
EIGHTFOLD_MAX = 500
ORACLE_MAX = 500


def _epoch_date(v):
    """Eightfold stamps t_create in epoch seconds."""
    if not v:
        return None
    try:
        return dt.datetime.utcfromtimestamp(float(v)).date().isoformat()
    except (ValueError, OSError, OverflowError):
        return None


def _amazon_date(s):
    """'September  9, 2026' -> ISO date string the shared age parser understands."""
    if not s:
        return None
    try:
        return dt.datetime.strptime(re.sub(r"\s+", " ", s.strip()),
                                    "%B %d, %Y").date().isoformat()
    except ValueError:
        return None


def amazon(c, row):
    """amazon.jobs search.json. robots.txt only disallows /internal paths."""
    out, offset = [], 0
    while offset < 500:
        r = session().get(
            "https://www.amazon.jobs/en/search.json",
            params={"base_query": "intern", "result_limit": 100,
                    "offset": offset, "sort": "recent"}, timeout=TIMEOUT)
        if r.status_code != 200:
            break
        body = r.json()
        jobs = body.get("jobs") or []
        for j in jobs:
            title = j.get("title") or ""
            if not classify(title)["keep"]:
                continue
            out.append(row(c, title,
                           j.get("normalized_location") or j.get("location"),
                           "https://www.amazon.jobs" + (j.get("job_path") or ""),
                           _amazon_date(j.get("posted_date"))))
        offset += 100
        if len(jobs) < 100 or offset >= body.get("hits", 0):
            break
    return out


def _hydration(session_, url):
    """Pull window.__staticRouterHydrationData out of an Apple jobs page.

    The search page is server-rendered and ships its results as a JS string
    literal holding JSON. Scanning to the literal's closing quote and parsing
    twice gets the real objects -- no regex-scraping of the markup.
    """
    t = session_.get(url, timeout=TIMEOUT).text
    m = re.search(r"__staticRouterHydrationData\s*=\s*JSON\.parse\(", t)
    if not m:
        return None
    start = t.index("JSON.parse(", m.start()) + len("JSON.parse(")
    if start >= len(t) or t[start] != '"':
        return None
    i, esc = start + 1, False
    while i < len(t):
        c = t[i]
        if esc:
            esc = False
        elif c == "\\":
            esc = True
        elif c == '"':
            break
        i += 1
    try:
        return json.loads(json.loads(t[start:i + 1]))
    except ValueError:
        return None


def _apple_location(job):
    locs = job.get("locations") or []
    if not locs:
        return ""
    parts = [locs[0].get(k) for k in ("city", "stateProvince", "countryName")]
    out = ", ".join(p for p in parts if p) or (locs[0].get("name") or "")
    return out + (f" (+{len(locs) - 1} more)" if len(locs) > 1 else "")


def apple(c, row):
    """jobs.apple.com search. That host serves no robots.txt, so nothing is
    disallowed there; the page is public and server-rendered.

    Apple files every internship under one team, which beats a free-text
    search: `search=intern` loose-matches ~1900 records of which almost none
    are internships, while the team filter returns them exactly.
    """
    s, out, seen = session(), [], set()
    for page in range(1, APPLE_MAX_PAGES + 1):
        d = _hydration(s, "https://jobs.apple.com/en-us/search"
                          f"?team={APPLE_INTERN_TEAM}&sort=newest&page={page}")
        if not d:
            break
        sr = (d.get("loaderData") or {}).get("search") or {}
        jobs = sr.get("searchResults") or []
        for j in jobs:
            pid = j.get("positionId")
            title = j.get("postingTitle") or ""
            if not pid or pid in seen:
                continue
            seen.add(pid)
            if not classify(title)["keep"]:
                continue
            out.append(row(c, title, _apple_location(j),
                           f"https://jobs.apple.com/en-us/details/{pid}/"
                           f"{j.get('transformedPostingTitle', '')}",
                           j.get("postDateInGMT")))
        if len(jobs) < 20 or page * 20 >= sr.get("totalRecords", 0):
            break
    return out


def eightfold(c, row):
    """Eightfold career hubs ({host}/api/apply/v2/jobs).

    Netflix's robots.txt names `/api/apply` in its Allow list, so this endpoint
    is explicitly sanctioned rather than merely undefended. Check the same
    before pointing a new company here -- Qualcomm runs the identical template
    but answers "Not authorized for PCSX", and is link-only for that reason.
    """
    s, out, start = session(), [], 0
    host, domain = c["host"], c["domain"]
    while start < EIGHTFOLD_MAX:
        r = s.get(f"{host}/api/apply/v2/jobs",
                  params={"domain": domain, "query": "intern",
                          "start": start, "num": 50}, timeout=TIMEOUT)
        if r.status_code != 200:
            break
        body = r.json()
        positions = body.get("positions") or []
        for p in positions:
            title = p.get("name") or ""
            if not classify(title)["keep"]:
                continue
            loc = (p.get("location") or "").replace(",", ", ")
            out.append(row(c, title, loc,
                           p.get("canonicalPositionUrl")
                           or f"{host}/careers/job/{p.get('id')}",
                           _epoch_date(p.get("t_create"))))
        start += 50
        if len(positions) < 50 or start >= body.get("count", 0):
            break
    return out


def oracle_orc(c, row):
    """Oracle Cloud Recruiting (`/hcmRestApi/.../recruitingCEJobRequisitions`).

    The requisition list only comes back when `expand` is supplied; without it
    the response carries facets and no jobs at all.
    """
    s, out, offset = session(), [], 0
    host, site = c["host"], c.get("site", "CX_1")
    while offset < ORACLE_MAX:
        r = s.get(f"{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions",
                  params={"onlyData": "true",
                          "expand": "requisitionList.secondaryLocations",
                          "finder": f"findReqs;siteNumber={site},limit=50,"
                                    f"offset={offset},keyword=intern"},
                  headers={"Accept": "application/json"}, timeout=TIMEOUT)
        if r.status_code != 200:
            break
        item = (r.json().get("items") or [{}])[0]
        reqs = item.get("requisitionList") or []
        for q in reqs:
            title = q.get("Title") or ""
            if not classify(title)["keep"]:
                continue
            out.append(row(c, title, q.get("PrimaryLocation"),
                           f"{host}/hcmUI/CandidateExperience/en/sites/"
                           f"{c.get('ui_site', 'careers')}/job/{q.get('Id')}",
                           q.get("PostedDate")))
        offset += 50
        if len(reqs) < 50 or offset >= item.get("TotalJobsCount", 0):
            break
    return out


BOARDS = {"amazon": amazon, "apple": apple, "eightfold": eightfold,
          "oracle_orc": oracle_orc}
