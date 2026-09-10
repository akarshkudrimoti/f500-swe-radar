"""Render README.md from data/ats.json + data/listings.json."""
import datetime as dt
import json
import pathlib
from urllib.parse import quote_plus

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

ATS_LABEL = {"workday": "Workday", "greenhouse": "Greenhouse", "lever": "Lever",
             "ashby": "Ashby", "smartrecruiters": "SmartRecruiters",
             "custom": "Own site", "unknown": "—"}


def rk(c):
    """Fortune rank, or an em dash for the non-F500 companies."""
    return str(c.get("rank")) if c.get("rank") else "—"


def esc(s):
    return str(s).replace("|", "\\|").replace("\n", " ").strip()


def age(days):
    if days is None:
        return "—"
    if days == 0:
        return "today"
    if days == 1:
        return "1d"
    if days < 30:
        return f"{days}d"
    return f"{days // 30}mo"


def main():
    ats = json.loads((DATA / "ats.json").read_text(encoding="utf-8"))
    blob = json.loads((DATA / "listings.json").read_text(encoding="utf-8"))
    rows = blob["listings"]

    by_ats = {}
    for c in ats:
        k = c.get("ats", "unknown")
        by_ats[k] = by_ats.get(k, 0) + 1
    live = sum(1 for c in ats if c.get("ats") != "unknown" and c.get("crawl_allowed"))
    optout = sum(1 for c in ats if c.get("ats") != "unknown" and not c.get("crawl_allowed"))
    gen = blob["generated_utc"].replace("T", " ").replace("+00:00", " UTC")

    L = []
    A = L.append
    A("# Fortune 500 SWE Internship Radar")
    A("")
    A("Software-engineering **internship** postings (Summer 2027 cycle), pulled "
      "straight from the applicant tracking systems of the companies on the "
      "[2026 Fortune 500](https://fortune.com/ranking/fortune500/2026/).")
    A("")
    n_us = sum(1 for r in rows if r.get("is_us") is True)
    A(f"**{len(rows)} open postings** ({n_us} in the US) across "
      f"**{len({r['company'] for r in rows})} companies** · "
      f"last refreshed {gen}")
    A("")
    A(f"Coverage: {live} of 500 companies have a machine-readable feed we query. "
      f"{optout} more were identified but their `robots.txt` disallows crawling, so "
      f"they are listed as links only. {by_ats.get('unknown', 0)} are still unresolved "
      "— see [Coverage](#coverage).")
    A("")
    A("---")
    A("")
    def table(section):
        if not section:
            A("_Nothing open right now._")
            return
        A("| # | Company | Role | Location | Age | |")
        A("|---|---------|------|----------|-----|-|")
        for r in section:
            A(f"| {rk(r)} | **{esc(r['company'])}** | {esc(r['title'])} "
              f"| {esc(r['location'])} | {age(r['age_days'])} "
              f"| [Apply]({r['url']}) |")

    us = [r for r in rows if r.get("is_us") is True]
    intl = [r for r in rows if r.get("is_us") is False]
    unstated = [r for r in rows if r.get("is_us") is None]

    A("## United States")
    A("")
    table(us)
    A("")

    if intl or unstated:
        A("<details>")
        A(f"<summary>International ({len(intl)}) and unstated location "
          f"({len(unstated)})</summary>")
        A("")
        A("### International")
        A("")
        table(intl)
        A("")
        A("### Location not stated")
        A("")
        A("_Usually a multi-site requisition; open the posting to see where._")
        A("")
        table(unstated)
        A("")
        A("</details>")
        A("")
    A("## Coverage")
    A("")
    A("| Source | Companies |")
    A("|--------|-----------|")
    for k, v in sorted(by_ats.items(), key=lambda kv: -kv[1]):
        A(f"| {ATS_LABEL.get(k, k)} | {v} |")
    A("")
    A("<details>")
    A("<summary>All 500 companies and where their jobs live</summary>")
    A("")
    A("| # | Company | Sector | ATS | Careers |")
    A("|---|---------|--------|-----|---------|")
    for c in ats:
        url = c.get("careers_url")
        if url:
            link = f"[open]({url})"
        else:
            # No board resolved: a search link keeps the directory complete for
            # all 500 rather than leaving a dead cell.
            q = quote_plus(f"{c['name']} software engineering internship careers")
            link = f"[search](https://www.google.com/search?q={q})"
        flag = "" if c.get("crawl_allowed") or c.get("ats") == "unknown" else " ¹"
        A(f"| {rk(c)} | {esc(c['name'])} | {esc(c['sector'])} "
          f"| {ATS_LABEL.get(c.get('ats', 'unknown'), c.get('ats'))}{flag} | {link} |")
    A("")
    A("</details>")
    A("")
    A("¹ `robots.txt` disallows crawling this career site, so postings are not "
      "collected — the link goes to their board instead.")
    A("")
    A("## How it works")
    A("")
    A("```")
    A("scripts/discover_ats.py   which ATS does each company run?  -> data/ats.json")
    A("scripts/fetch_listings.py pull + filter current postings    -> data/listings.json")
    A("scripts/build_readme.py   render this file")
    A("```")
    A("")
    A("**Discovery.** Most of the Fortune 500 runs Workday, which names its career "
      "site in `robots.txt` (`Sitemap:`/`Allow:`, or `Disallow:` for the ones that "
      "opt out). One request per Workday pod resolves tenant *and* site; companies "
      "that miss are then probed against the public Greenhouse, Lever, Ashby and "
      "SmartRecruiters board APIs.")
    A("")
    A("**Filtering.** `scripts/classify.py` decides what counts as a SWE internship "
      "from the job title: an internship signal, a software signal, no competing "
      "discipline, and either no year in the title or the target year. Adjust "
      "`TARGET_YEAR` there when the cycle rolls over.")
    A("")
    A("It is tuned for precision over recall, because the company list is mostly "
      "industrial: a bare \"Engineering Intern\" is petroleum or mechanical far more "
      "often than software, so bare `engineering` and `technology` are not treated as "
      "software signals. That also drops genuine \"Information Technology Intern\" "
      "roles — widen `SOFTWARE` in `classify.py` if you want them.")
    A("")
    A("**Manners.** Career sites that disallow crawling are never fetched. Requests "
      "are pooled, retried only on transient errors, and identify themselves.")
    A("")
    A("**Corrections.** A company probed to the wrong board, or one that needs a "
      "hand-written entry, goes in `data/overrides.json` — those always win over "
      "discovery.")
    A("")
    A("---")
    A("")
    A(f"Company list: Fortune 500, 2026 edition. Generated {dt.date.today()}.")

    (ROOT / "README.md").write_text("\n".join(L), encoding="utf-8")
    print(f"README.md: {len(rows)} postings, {len(ats)} companies")


if __name__ == "__main__":
    main()
