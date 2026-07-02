"""Download the real datasets (idempotent, cached under data/).

- Docs corpus: selected pages of the public GitLab Handbook (CC BY-SA 4.0).
  Citations point at the live handbook URLs.
- Analytics: UCI "Online Retail" (CC BY 4.0) — real transactions of a UK online
  retailer, Dec 2010 – Dec 2011, ~540k rows.
"""

import io
import json
import zipfile
from pathlib import Path

import httpx

DATA = Path(__file__).resolve().parent.parent / "data"
CORPUS = DATA / "corpus"

RAW = "https://gitlab.com/gitlab-com/content-sites/handbook/-/raw/main/{path}"
LIVE = "https://handbook.gitlab.com/{path}/"

# repo path (under content/, without .md) — slug derived from it
HANDBOOK_PAGES = [
    "handbook/people-group/time-off-and-absence/_index",
    "handbook/people-group/time-off-and-absence/time-off-types",
    "handbook/finance/spending-company-money",
    "handbook/security/policies_and_standards/password-standard",
    "handbook/security/security-and-technology-policies/access-management-policy",
    "handbook/security/security-and-technology-policies/audit-logging-policy",
    "handbook/people-group/general-onboarding/_index",
    "handbook/engineering/infrastructure-platforms/incident-management/_index",
]

UCI_ZIP = "https://archive.ics.uci.edu/static/public/352/online+retail.zip"


def frontmatter_title(text: str) -> str | None:
    if not text.startswith("---"):
        return None
    for line in text.split("---", 2)[1].splitlines():
        if line.strip().startswith("title:"):
            return line.split(":", 1)[1].strip().strip("\"'")
    return None


def fetch_corpus(client: httpx.Client) -> None:
    CORPUS.mkdir(parents=True, exist_ok=True)
    sources: dict[str, dict] = {}
    for page in HANDBOOK_PAGES:
        slug = page.removeprefix("handbook/").removesuffix("/_index").replace("/", "--") + ".md"
        target = CORPUS / slug
        live_url = LIVE.format(path=page.removesuffix("/_index"))
        if not target.exists():
            r = client.get(RAW.format(path=f"content/{page}.md"), follow_redirects=True)
            r.raise_for_status()
            target.write_text(r.text)
            print(f"fetched {slug} ({len(r.text)} chars)")
        sources[slug] = {
            "title": frontmatter_title(target.read_text()) or slug,
            "url": live_url,
        }
    (CORPUS / "sources.json").write_text(json.dumps(sources, indent=2))


def fetch_retail(client: httpx.Client) -> None:
    xlsx = DATA / "online_retail.xlsx"
    if xlsx.exists():
        return
    print("downloading UCI Online Retail (~24 MB)...")
    r = client.get(UCI_ZIP, follow_redirects=True)
    r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        name = next(n for n in z.namelist() if n.endswith(".xlsx"))
        xlsx.write_bytes(z.read(name))
    print(f"wrote {xlsx} ({xlsx.stat().st_size // 1024} KiB)")


def main() -> None:
    DATA.mkdir(exist_ok=True)
    with httpx.Client(timeout=120) as client:
        fetch_corpus(client)
        fetch_retail(client)
    print("data ready")


if __name__ == "__main__":
    main()
