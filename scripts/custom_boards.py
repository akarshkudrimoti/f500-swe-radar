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
import re

from classify import classify
from common import session

TIMEOUT = 25


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


BOARDS = {"amazon": amazon}
