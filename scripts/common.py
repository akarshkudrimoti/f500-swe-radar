"""Shared helpers: slug generation and a polite HTTP session."""
import re
import threading

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

UA = "f500-swe-radar/1.0 (+https://github.com/; job-board aggregator; contact via repo issues)"

# Corporate noise that never appears in an ATS tenant slug.
SUFFIXES = {
    "inc", "incorporated", "corp", "corporation", "co", "company", "companies",
    "group", "holdings", "holding", "international", "industries", "enterprises",
    "plc", "ltd", "llc", "lp", "nv", "sa", "ag", "the", "and", "of",
}

# First words that identify an industry or a flavour of incorporation rather
# than a company: 'american' alone reaches American Express, American Airlines
# and American Electric Power indiscriminately. Their bare first word is never
# trustworthy on its own.
GENERIC_FIRST = {
    "american", "general", "united", "national", "first", "global", "standard",
    "union", "liberty", "international", "pacific", "atlantic", "capital",
    "premier", "alliance", "consolidated", "continental", "universal",
    "republic", "new", "old", "best", "air", "super", "world", "east", "west",
    "north", "south", "central", "public", "service", "services", "advanced",
}

_local = threading.local()


def session() -> requests.Session:
    """One pooled session per thread; retries only on transient server errors."""
    s = getattr(_local, "s", None)
    if s is None:
        s = requests.Session()
        retry = Retry(total=2, backoff_factor=0.4, status_forcelist=(429, 502, 503, 504),
                      allowed_methods=frozenset(["GET", "POST"]))
        ad = HTTPAdapter(max_retries=retry, pool_connections=32, pool_maxsize=32)
        s.mount("https://", ad)
        s.headers["User-Agent"] = UA
        _local.s = s
    return s


def tokens(name: str) -> list[str]:
    """Words from a company name distinctive enough to identify it in a site name.

    Generic words are excluded for the same reason they make weak slugs: the
    site 'globalpartnerscareers' contains 'global', which would otherwise
    "prove" it belongs to Global Payments.
    """
    n = re.sub(r"[^a-z0-9]+", " ", name.lower().replace("&", "and")).strip()
    core = [w for w in n.split() if w and w not in SUFFIXES]
    out = [w for w in core if len(w) >= 3 and w not in GENERIC_FIRST]
    if len(core) > 1:
        out.append("".join(core))
    return out


def normalize(name: str) -> list[tuple[str, bool]]:
    """Company name -> ordered (slug, is_strong) candidates, likeliest first.

    A *strong* slug is built from the whole name, so a hit on it is self-
    evidently the right company. A *weak* slug -- the bare first word, or an
    initialism -- collides constantly ('td' reaches TD Bank, not TD Synnex;
    'cc' reaches Chanel, not Charter Communications), so hits on those have to
    be corroborated before they are believed.
    """
    n = name.lower()
    n = n.replace("&", "and").replace("+", "plus")
    n = re.sub(r"[.'’,()]", "", n)
    n = re.sub(r"[^a-z0-9]+", " ", n).strip()
    words = [w for w in n.split() if w]
    core = [w for w in words if w not in SUFFIXES] or words

    out = []
    seen = set()

    def add(t, strong):
        t = t.strip("-")
        if t and t not in seen and 2 <= len(t) <= 40:
            seen.add(t)
            out.append((t, strong))

    multi = len(core) > 1
    add("".join(core), True)
    add("-".join(core), True)
    add("".join(words), True)
    # A single-word company IS its first word. For a multi-word name the first
    # word only stands alone when it is distinctive: 'cisco' and 'micron'
    # identify a company, 'td' and 'american' identify nothing.
    if core:
        first = core[0]
        add(first, not multi or (len(first) >= 4 and first not in GENERIC_FIRST))
    if multi:
        add("".join(core[:2]), False)
        add("".join(w[0] for w in core), False)  # initialisms: 'ibm', 'ups'
    return out
