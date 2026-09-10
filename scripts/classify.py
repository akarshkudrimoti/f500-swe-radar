"""Decide whether a job title is a software-engineering internship.

Kept separate from fetching so the rules can be tested and tuned on their own:
    python scripts/classify.py "2027 Summer Intern - Backend Engineer"
"""
import re
import sys

TARGET_YEAR = 2027

# Plurals matter: "NVIDIA 2027 Internships: ..." is how a whole programme is
# titled, and `internship\b` cannot match it. Spelled out rather than `intern\w*`
# so "internal" and "international" don't sneak in.
INTERN = re.compile(
    r"\b(interns?|internships?|co-?ops?|apprentices?(hips?)?|"
    r"summer analysts?|industrial placements?)\b", re.I)

# Some employers only signal it through the programme name.
INTERN_SOFT = re.compile(r"\b(university|student|campus|early talent)\b", re.I)

SOFTWARE = re.compile(
    r"\b(software|swe|developer|programming|programmer|computer science|"
    r"back ?end|front ?end|full ?stack|web dev|mobile|ios|android|"
    r"machine learning|ml|ai|artificial intelligence|data engineer|"
    r"data scien\w*|platform|infrastructure|cloud|devops|sre|"
    r"site reliability|embedded|firmware|systems? engineer|"
    r"security engineer|application engineer|research engineer|"
    # Deliberately no bare "engineering"/"technology": across a list this
    # industrial they match petroleum and mechanical interns far more often
    # than software ones. "Software Engineering" still matches on "software".
    r"quality engineer|qa engineer|test engineer)\b", re.I)

# Disciplines that share the word "engineer" but are not this list.
NOT_SOFTWARE = re.compile(
    r"\b(mechanical|civil|chemical|petroleum|structural|electrical power|"
    r"aerospace|manufacturing|industrial engineer|process engineer|"
    r"field engineer|sales engineer|nurse|nursing|clinical|pharmac\w*|"
    r"marketing|merchandis\w*|accounting|audit|tax|human resources|"
    r"supply chain|logistics\w*|store|retail associate|driver|technician|"
    r"paralegal|legal|communications|public relations|recruit\w*|"
    # Chip work sits next to software in the same listings and is not this list.
    r"asic|rtl|vlsi|silicon|circuit design|design for test|physical design|"
    r"hardware engineer\w*|hardware development|hardware design)\b", re.I)

YEAR = re.compile(r"\b(20\d{2})\b")


def classify(title: str) -> dict:
    """-> {'is_intern', 'is_software', 'year', 'keep'}"""
    t = title or ""
    is_intern = bool(INTERN.search(t)) or (
        bool(INTERN_SOFT.search(t)) and bool(re.search(r"\b(grad|program)\b", t, re.I)))
    is_software = bool(SOFTWARE.search(t)) and not NOT_SOFTWARE.search(t)

    years = [int(y) for y in YEAR.findall(t)]
    year = years[0] if years else None
    # A stated year that isn't the target cycle means a stale or future posting.
    year_ok = year is None or year == TARGET_YEAR

    return {"is_intern": is_intern, "is_software": is_software, "year": year,
            "keep": is_intern and is_software and year_ok}


if __name__ == "__main__":
    for arg in sys.argv[1:]:
        print(f"{arg!r} -> {classify(arg)}")


# --- location ---------------------------------------------------------------
# Workday tenants write locations every which way ("USA, CA, Santa Clara",
# "United States of America", "China, Shanghai"), so match on tokens, not shape.

STATES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
    "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
    "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina",
    "south dakota", "tennessee", "texas", "utah", "vermont", "virginia",
    "washington", "west virginia", "wisconsin", "wyoming",
    "district of columbia", "puerto rico",
}
ABBR = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC", "PR",
}
US_WORDS = re.compile(r"\b(usa|u\.s\.a?|united states|us remote|remote[, ]+us)\b", re.I)


def is_us(location: str):
    """True / False / None when the posting doesn't say."""
    if not location:
        return None
    loc = location.strip()
    if re.fullmatch(r"\d+\s+locations?", loc, re.I):
        return None  # Workday collapses multi-site reqs to a bare count
    if US_WORDS.search(loc):
        return True
    low = loc.lower()
    if any(st in low for st in STATES):
        return True
    parts = [p.strip() for p in re.split(r"[,/|]", loc)]
    if any(p.upper() in ABBR for p in parts):
        return True
    return False
