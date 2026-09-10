"""Resolve companies against the Common Crawl token set instead of guessing.

Blind probing asks "does a board exist at the slug I invented?", which is how
'cc' reached Chanel. This asks the opposite and much safer question: of the
boards that demonstrably exist, which one belongs to this company?

That also reaches companies whose board is named nothing like them -- Charter
Communications hires under Spectrum -- because the career-site name is searched
too, not just the tenant.

Every proposal is verified against the live board before it is written, and the
same corroboration rule as discovery applies: a match on a weak, generic or
short token has to be backed by name evidence.

    python scripts/match_tokens.py --dry-run
    python scripts/match_tokens.py            # verify and write into ats.json
"""
import argparse
import json
import pathlib
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from common import (GENERIC_FIRST, SUFFIXES, load_companies, normalize,
                    rank_key)
from discover_ats import (try_ashby, try_greenhouse, try_lever,
                          try_smartrecruiters, _json, session, TIMEOUT)

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

SIMPLE = {"greenhouse": try_greenhouse, "lever": try_lever, "ashby": try_ashby,
          "smartrecruiters": try_smartrecruiters}


# Words that name an industry, not a company. Matching on these is what
# proposed FormEnergy for Valero and check-technologies for Dell.
INDUSTRY = {
    "energy", "technology", "technologies", "insurance", "market", "markets",
    "machine", "machines", "food", "foods", "system", "systems", "solution",
    "solutions", "service", "services", "holding", "holdings", "industries",
    "express", "line", "lines", "motor", "motors", "electric", "electronics",
    "health", "healthcare", "financial", "finance", "bank", "banking",
    "capital", "partner", "partners", "brand", "brands", "store", "stores",
    "farm", "performance", "dynamics", "labs", "digital", "media", "network",
    "networks", "communications", "resources", "materials", "products",
    "enterprises", "company", "corporation", "science", "sciences",
    "pharmaceutical", "pharmaceuticals", "petroleum", "airline", "airlines",
    "auto", "automotive", "retail", "restaurant", "hotels", "resorts",
    "entertainment", "studios", "telecom", "mobile", "wireless", "data",
    "cloud", "software", "hardware", "semiconductor", "semiconductors",
    "engineering", "construction", "transport", "transportation", "logistics",
    "freight", "shipping", "supply", "management", "consulting", "advisors",
    "advisory", "trust", "mutual", "life", "group", "worldwide", "global",
}

# What may legitimately trail a company name in a board address.
CAREER_TAIL = re.compile(
    r"^(careers?|jobs?|hiring|talent|external|externalcareersite|careersite|"
    r"recruiting|opportunities|joinus|work|workday|corporate|global|inc|llc|"
    r"ltd|group|holdings?|inc|us|usa|uk|com|site|portal|main|new|v2|\d+)*$")


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def name_words(company_name):
    """-> (full_name, [distinctive single words]).

    The full concatenated name is self-evidently this company. A single word
    is not: 'charter' is also Charter Manufacturing, 'fidelity' is also
    Fidelity Investments, 'berkshire' is also Berkshire Group. Single words are
    returned separately so the caller can demand extra evidence for them.
    """
    # Derived here rather than from tokens(), which already appends the
    # concatenated name -- joining its output produced 'kraftheinzkraftheinz'.
    n = re.sub(r"[^a-z0-9]+", " ", company_name.lower().replace("&", "and"))
    core = [w for w in n.split() if w and w not in SUFFIXES]
    full = "".join(core)
    singles = [w for w in core
               if len(w) >= 5 and w not in INDUSTRY and w not in GENERIC_FIRST]
    return (full if len(full) >= 5 else None), singles


def owns(hay, words):
    """True when `hay` is a company word plus, at most, careers-page furniture.

    Prefix-anchored and tail-constrained on purpose: 'disneycareer' is Disney,
    'LillyPulitzer' is not Eli Lilly, and 'advocateslawcareers' is not Tesla
    even though it contains the letters.
    """
    for w in words:
        if hay.startswith(w) and CAREER_TAIL.match(hay[len(w):]):
            return w
    return None


def candidates(company, store):
    """-> [(platform, token_key, why)] worth verifying for this company."""
    name = company["name"]
    full, singles = name_words(name)
    if not full:
        return []
    strong = {norm(s) for s, is_strong in normalize(name) if is_strong}
    out = []

    for platform, entries in store.items():
        for key in entries:
            if platform == "workday":
                tenant, pod, site = key.split("|")
                hay, ten = norm(site), norm(tenant)
                if owns(hay, [full]):
                    out.append((platform, key, f"site '{site}' is the full name"))
                elif ten in strong:
                    out.append((platform, key, f"tenant '{tenant}' is the full name"))
                elif (w := owns(hay, singles)) and ten == w:
                    # A single word only counts when the tenant is exactly that
                    # word too: 'disney' hosting 'disneycareer' is Disney,
                    # 'chartermfg' hosting 'Charter_Careers' is not Charter
                    # Communications.
                    out.append((platform, key, f"site and tenant both '{w}'"))
            elif owns(norm(key), [full]):
                # Off-Workday boards are addressed by the token alone, so there
                # is no second signal -- only a full-name match is safe.
                out.append((platform, key, f"token '{key}' is the full name"))

    # Longest evidence first, so a full-name match is verified before a
    # single-word one.
    out.sort(key=lambda c: -len(c[2]))
    return out


def verify(company, platform, key):
    """Hit the live board; return an ats.json record or None."""
    if platform in SIMPLE:
        hit = SIMPLE[platform](key)
        return {**company, **hit, "slug": key, "match": "commoncrawl"} if hit else None

    tenant, pod, site = key.split("|")
    pod = pod[2:]
    host = f"https://{tenant}.wd{pod}.myworkdayjobs.com"
    url = f"{host}/wday/cxs/{tenant}/{site}/jobs"
    try:
        r = session().post(url, json={"appliedFacets": {}, "limit": 1, "offset": 0,
                                      "searchText": ""}, timeout=TIMEOUT)
    except Exception:
        return None
    body = _json(r) if r.status_code == 200 else None
    if not body or not body.get("total"):
        return None

    allowed = True
    try:
        rb = session().get(f"{host}/robots.txt", timeout=TIMEOUT)
        if rb.status_code == 200:
            low = rb.text.lower()
            allowed = f"allow: /{site.lower()}/" in low
    except Exception:
        allowed = False

    return {**company, "ats": "workday", "tenant": tenant, "pod": int(pod),
            "site": site, "api": url, "careers_url": f"{host}/{site}",
            "total_open": body["total"], "crawl_allowed": allowed,
            "slug": tenant, "match": "commoncrawl"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="show proposals without touching the live boards")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    store = json.loads((DATA / "cc_tokens.json").read_text(encoding="utf-8"))
    cache = {c["name"]: c for c in
             json.loads((DATA / "ats.json").read_text(encoding="utf-8"))}
    overrides = {o["name"] for o in
                 json.loads((DATA / "overrides.json").read_text(encoding="utf-8"))}

    unresolved = [c for c in load_companies(DATA)
                  if cache.get(c["name"], {}).get("ats", "unknown") == "unknown"
                  and c["name"] not in overrides]
    print(f"{len(unresolved)} unresolved companies against "
          f"{sum(len(v) for v in store.values())} enumerated tokens",
          file=sys.stderr)

    proposals = [(c, cands) for c in unresolved if (cands := candidates(c, store))]
    print(f"{len(proposals)} have at least one candidate", file=sys.stderr)

    if args.dry_run:
        for c, cands in proposals[:60]:
            for p, k, why in cands[:3]:
                print(f"  {c['name'][:32]:34} {p:10} {k[:44]:46} {why}")
        return

    def work(c, cands):
        for platform, key, _ in cands[:6]:
            rec = verify(c, platform, key)
            if rec:
                return rec
        return None

    found = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(work, c, cands): c for c, cands in proposals}
        for f in as_completed(futs):
            try:
                rec = f.result()
            except Exception:
                rec = None
            if rec:
                cache[rec["name"]] = rec
                found += 1
                print(f"  + {rec['name']}: {rec['ats']} "
                      f"({rec.get('total_open')} open)", file=sys.stderr, flush=True)

    (DATA / "ats.json").write_text(
        json.dumps(sorted(cache.values(), key=rank_key), indent=1), encoding="utf-8")
    print(f"\nresolved {found} more companies from the Common Crawl index",
          file=sys.stderr)


if __name__ == "__main__":
    main()
