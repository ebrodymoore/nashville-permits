#!/usr/bin/env python3
"""
Nashville Building Residential - New Permits CLI

Fetches permits entered on a given date (default: yesterday) and looks up
contractor contact info via web search. No AI API required.

Usage:
    python nashville_permits.py                  # yesterday's permits
    python nashville_permits.py --date 2026-03-09
    python nashville_permits.py --no-lookup      # skip contact search
    python nashville_permits.py --json           # JSON output
"""

import sys
import re
import json
import argparse
import time
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

try:
    import requests
    from bs4 import BeautifulSoup
    from ddgs import DDGS
except ImportError:
    print("Missing dependencies. Run:  pip install requests beautifulsoup4 ddgs", file=sys.stderr)
    sys.exit(1)

ARCGIS_URL = (
    "https://services2.arcgis.com/HdTo6HJqh92wn4D8/arcgis/rest"
    "/services/Building_Permit_Applications_Feature_Layer_view/FeatureServer/0/query"
)

NASHVILLE_TZ = ZoneInfo("America/Chicago")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# ── ArcGIS helpers ──────────────────────────────────────────────────────────

def fetch_permits(start: date, end: date) -> list[dict]:
    """Return all 'Building Residential - New' permits entered in [start, end] inclusive."""
    end_exclusive = end + timedelta(days=1)
    # ArcGIS requires TIMESTAMP literal format; dates are stored in local Nashville time
    subtypes = (
        "Single Family Residence",
        "Multifamily, Townhome",
        "Multifamily, Tri-Plex, Quad, Apartments",
    )
    subtype_filter = " OR ".join(
        f"Permit_Subtype_Description='{s}'" for s in subtypes
    )
    where = (
        f"Permit_Type_Description='Building Residential - New' "
        f"AND ({subtype_filter}) "
        f"AND Date_Entered >= TIMESTAMP '{start} 00:00:00' "
        f"AND Date_Entered < TIMESTAMP '{end_exclusive} 00:00:00'"
    )
    params = {
        "where": where,
        "outFields": "Permit__,Address,City,ZIP,Contact,Purpose,Date_Entered,Date_Issued",
        "f": "json",
        "resultRecordCount": 1000,
        "orderByFields": "Date_Entered ASC",
    }
    resp = requests.get(ARCGIS_URL, params=params, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"ArcGIS API error: {data['error']['message']}")
    return [f["attributes"] for f in data.get("features", [])]


def _fmt_date(ms) -> str:
    if not ms:
        return "N/A"
    return datetime.fromtimestamp(ms / 1000, tz=NASHVILLE_TZ).strftime("%Y-%m-%d")


# ── Contact info lookup ──────────────────────────────────────────────────────

_PHONE_RE = re.compile(r"\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}")
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_NOISE_DOMAINS = {"duckduckgo.com", "w3.org", "schema.org", "example.com", "bing.com",
                  "google.com", "facebook.com"}

# URLs from these domains are almost never useful for contractor contact info
_SKIP_URL_DOMAINS = {
    "wikipedia.org", "paypal.com", "icloud.com", "apple.com", "irs.gov",
    "dol.gov", "sba.gov", "zillow.com", "realtor.com", "trulia.com",
    "indeed.com", "linkedin.com", "twitter.com", "instagram.com",
    "temu.com", "ssa.gov", "reddit.com", "youtube.com", "amazon.com",
}


def _dedup_phones(phones: list[str]) -> list[str]:
    seen, out = set(), []
    for p in phones:
        digits = re.sub(r"\D", "", p)
        if digits not in seen:
            seen.add(digits)
            out.append(p)
    return out[:5]


def _dedup_emails(emails: list[str]) -> list[str]:
    seen, out = set(), []
    for e in emails:
        domain = e.split("@")[-1].lower()
        if domain not in _NOISE_DOMAINS and e.lower() not in seen:
            seen.add(e.lower())
            out.append(e)
    return out[:5]


def _extract_from_page(url: str) -> tuple[list[str], list[str]]:
    """Fetch a URL and return (phones, emails) found in its text."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=10, allow_redirects=True)
        r.raise_for_status()
        text = BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True)
        return _dedup_phones(_PHONE_RE.findall(text)), _dedup_emails(_EMAIL_RE.findall(text))
    except Exception:
        return [], []


def search_contact_info(company: str) -> dict:
    """
    Search for company contact info using DuckDuckGo (ddgs library, no API key).
    Fetches the top search result pages to extract phone and email.
    Returns dict with keys: phones, emails, websites.
    """
    query = f"{company} Nashville TN phone contact"
    phones, emails, websites = [], [], []

    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=5))
    except Exception as exc:
        return {"phones": [], "emails": [], "websites": [], "error": str(exc)}

    for r in results:
        url = r.get("href", "")
        if url and not any(d in url for d in _NOISE_DOMAINS | _SKIP_URL_DOMAINS):
            websites.append(url)

    # Fetch pages to find phone/email — try up to 4, collect from all
    for url in websites[:4]:
        p, e = _extract_from_page(url)
        phones.extend(x for x in p if x not in phones)
        emails.extend(x for x in e if x not in emails)
        time.sleep(0.3)

    return {
        "phones":   _dedup_phones(phones),
        "emails":   _dedup_emails(emails),
        "websites": websites[:3],
    }


# ── Output ──────────────────────────────────────────────────────────────────

def print_permit(r: dict, idx: int, total: int) -> None:
    sep = "─" * 70
    ci  = r.get("contact_info", {})
    print(sep)
    print(f"  Permit #{idx} of {total}:  {r['permit']}")
    print(sep)
    print(f"  Address:       {r['address']}")
    print(f"  Contact:       {r['contact']}")
    # Split on period followed by space or end-of-string, not on decimals (e.g. "2.5 bath")
    first_sentence = (re.split(r'\.(?:\s|$)', r['purpose'])[0] + '.').strip() if r['purpose'] else ''
    print(f"  Purpose:       {first_sentence}")
    print(f"  Date Entered:  {r['date_entered']}")
    if r['date_issued'] != 'N/A':
        print(f"  Date Issued:   {r['date_issued']}")
    if ci:
        if ci.get("phones"):
            print(f"  Phone(s):      {', '.join(ci['phones'])}")
        if ci.get("emails"):
            print(f"  Email(s):      {', '.join(ci['emails'])}")
        if ci.get("websites"):
            print(f"  Website(s):    {', '.join(ci['websites'])}")
        if not ci.get("phones") and not ci.get("emails") and not ci.get("websites"):
            print("  Contact info:  (none found via web search)")
    print()


# ── Main ────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch Nashville Building Residential - New permits and look up contact info."
    )
    parser.add_argument(
        "--date",
        metavar="YYYY-MM-DD",
        help="Single date to fetch (default: yesterday)",
    )
    parser.add_argument(
        "--start",
        metavar="YYYY-MM-DD",
        help="Start of date range (use with --end)",
    )
    parser.add_argument(
        "--end",
        metavar="YYYY-MM-DD",
        help="End of date range, inclusive (use with --start)",
    )
    parser.add_argument(
        "--no-lookup",
        action="store_true",
        help="Skip web contact lookup (faster)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="output_json",
        help="Output raw JSON instead of formatted text",
    )
    args = parser.parse_args()

    # Resolve date range
    def _parse(s: str, label: str) -> date:
        try:
            return datetime.strptime(s, "%Y-%m-%d").date()
        except ValueError:
            print(f"ERROR: {label} must be YYYY-MM-DD", file=sys.stderr)
            sys.exit(1)

    yesterday = (datetime.now(tz=NASHVILLE_TZ) - timedelta(days=1)).date()

    if args.start or args.end:
        if not (args.start and args.end):
            print("ERROR: --start and --end must be used together", file=sys.stderr)
            sys.exit(1)
        start_date = _parse(args.start, "--start")
        end_date   = _parse(args.end,   "--end")
        if end_date < start_date:
            print("ERROR: --end must be on or after --start", file=sys.stderr)
            sys.exit(1)
    elif args.date:
        start_date = end_date = _parse(args.date, "--date")
    else:
        start_date = end_date = yesterday

    label = str(start_date) if start_date == end_date else f"{start_date} to {end_date}"
    print(f"[*] Querying Nashville permits for {label}...", file=sys.stderr)
    permits = fetch_permits(start_date, end_date)

    if not permits:
        print(f"No 'Building Residential - New' permits found for {target}.")
        return

    print(f"[*] Found {len(permits)} permit(s).", file=sys.stderr)

    # De-duplicate company lookups
    contact_cache: dict[str, dict] = {}

    results = []
    for i, p in enumerate(permits):
        company = (p.get("Contact") or "").strip()
        addr_parts = filter(None, [
            p.get("Address", "").strip(),
            p.get("City", "Nashville").strip(),
            "TN",
            p.get("ZIP", "").strip(),
        ])
        entry = {
            "permit":       p.get("Permit__", ""),
            "address":      " ".join(addr_parts),
            "contact":      company,
            "purpose":      (p.get("Purpose") or "").strip(),
            "date_entered": _fmt_date(p.get("Date_Entered")),
            "date_issued":  _fmt_date(p.get("Date_Issued")),
            "contact_info": {},
        }

        if not args.no_lookup and company:
            if company not in contact_cache:
                print(
                    f"  [{i+1}/{len(permits)}] Looking up: {company}...",
                    file=sys.stderr,
                )
                contact_cache[company] = search_contact_info(company)
                time.sleep(1.2)  # polite delay between requests
            entry["contact_info"] = contact_cache[company]

        results.append(entry)

    if args.output_json:
        print(json.dumps(results, indent=2))
        return

    # Pretty print
    print()
    print(f"Nashville Building Residential – New Permits  |  {label}")
    print(f"{'=' * 70}\n")
    for i, r in enumerate(results, 1):
        print_permit(r, i, len(results))


if __name__ == "__main__":
    main()
