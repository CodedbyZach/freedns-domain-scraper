#!/usr/bin/env python3
"""
FreeDNS shared domain registry scraper.

Collects every domain listed at https://freedns.afraid.org/domain/registry/
and writes three files next to this script:

  domains.txt  every domain, one per line, sorted
  domains.csv  domain, status, hosts, domain_id, owner, owner_id, created
  changes.md   domains added and removed on each run, newest first

The two domain files are only rewritten when domains were added or removed.
A run with no additions or removals just logs "No changes" in changes.md.

If the scrape looks incomplete (a page keeps failing, far fewer domains than
the registry reports, or a big drop since the last run) the script exits with
an error and writes nothing, so a bad run never overwrites good data.

Usage:
  python scraper.py                 scrape and update the files
  python scraper.py --dry-run       scrape and print the domains, write nothing
  python scraper.py --max-pages 3   quick test (implies --dry-run)
  python scraper.py --force         save even if the safety checks fail
"""

from __future__ import annotations

import argparse
import csv
import io
import logging
import math
import os
import random
import re
import sys
import time
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

REGISTRY_URL = "https://freedns.afraid.org/domain/registry/"
# Registry sort modes: 1 name, 2 status/age, 3 owner, 4 age, 5 popularity, 6 length.
# Sorting by name keeps the order stable while we page through it.
SORT_BY_NAME = 1

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://freedns.afraid.org/",
}

REQUEST_TIMEOUT = (15, 45)               # connect, read (seconds)
RETRY_WAITS = [5, 15, 30, 60, 120, 180]  # pauses between attempts: 7 attempts per page
PAGE_DELAY = 2.0                         # pause between pages (seconds)
MAX_PAGES = 2000                         # hard stop if pagination misbehaves
MIN_COMPLETENESS = 0.97                  # need 97% of the count the registry reports
MAX_DROP = 0.10                          # refuse to save if the count fell more than 10%

ROOT = Path(__file__).resolve().parent
DOMAINS_TXT = "domains.txt"
DOMAINS_CSV = "domains.csv"
CHANGES_MD = "changes.md"
CSV_FIELDS = ["domain", "status", "hosts", "domain_id", "owner", "owner_id", "created"]
ENTRIES_MARKER = "<!-- entries -->"
CHANGES_HEADER = (
    "# FreeDNS registry changes\n\n"
    "Domains added to or removed from the "
    "[FreeDNS shared registry](https://freedns.afraid.org/domain/registry/) "
    "on each run, newest first.\n\n"
    f"{ENTRIES_MARKER}\n"
)

DOMAIN_RE = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)
SHOWING_RE = re.compile(r"Showing\s+([\d,]+)\s*-\s*([\d,]+)\s+of\s+([\d,]+)", re.I)
PAGE_OF_RE = re.compile(r"\bPage\s+(?:[\d,]+\s+)?of\s+([\d,]+)", re.I)
HOSTS_RE = re.compile(r"\(\s*([\d,]+)\s+hosts?\s+in\s+use\s*\)", re.I)
CREATED_RE = re.compile(r"\((\d{1,2})/(\d{1,2})/(\d{4})\)")
DOMAIN_ID_RE = re.compile(r"domain_id=(\d+)")
USER_ID_RE = re.compile(r"user_id=(\d+)")

log = logging.getLogger("freedns")


class ScrapeError(Exception):
    """The scrape can't be trusted, so nothing gets written."""


class EmptyPage(ScrapeError):
    """The server answered, but the page had no domain rows."""


class StyleFailed(ScrapeError):
    """A URL style doesn't work: page 1 failed, or later pages came back as page 1."""


# ---------------------------------------------------------------- parsing

def to_int(text: str) -> int:
    return int(text.replace(",", ""))


def normalize_domain(text: str) -> str:
    """Lowercase a domain, convert unicode names to punycode, reject non-domains."""
    text = text.strip().lower().rstrip(".")
    if not text:
        return ""
    if not text.isascii():
        try:
            text = text.encode("idna").decode("ascii")
        except UnicodeError:
            return ""
    return text if DOMAIN_RE.match(text) else ""


def parse_row(tr) -> dict | None:
    """One registry table row: domain cell, status, owner, age."""
    cells = tr.find_all("td")
    if len(cells) < 4:
        return None
    domain_cell, status_cell, owner_cell, age_cell = cells[:4]

    # The domain is the first link in the cell; its href carries the FreeDNS domain id.
    domain, domain_id = "", ""
    for link in domain_cell.find_all("a"):
        domain = normalize_domain(link.get_text(strip=True))
        if domain:
            match = DOMAIN_ID_RE.search(link.get("href", ""))
            domain_id = match.group(1) if match else ""
            break
    if not domain:
        words = domain_cell.get_text(" ", strip=True).split()
        domain = normalize_domain(words[0]) if words else ""
    if not domain:
        return None

    hosts = HOSTS_RE.search(domain_cell.get_text(" ", strip=True))

    owner_link = owner_cell.find("a")
    owner = (owner_link or owner_cell).get_text(" ", strip=True)
    owner_id = USER_ID_RE.search(owner_link.get("href", "")) if owner_link else None

    # Age cell looks like "6782 days ago (04/02/2008)" (month/day/year).
    created = ""
    match = CREATED_RE.search(age_cell.get_text(" ", strip=True))
    if match:
        month, day, year = (int(group) for group in match.groups())
        try:
            created = date(year, month, day).isoformat()
        except ValueError:
            created = "/".join(match.groups())

    return {
        "domain": domain,
        "status": status_cell.get_text(" ", strip=True).lower(),
        "hosts": to_int(hosts.group(1)) if hosts else "",
        "domain_id": domain_id,
        "owner": owner,
        "owner_id": owner_id.group(1) if owner_id else "",
        "created": created,
    }


def parse_page(html: str | bytes) -> dict:
    """Pull the domain rows and the 'Showing X-Y of Z' / 'Page N of M' counters."""
    soup = BeautifulSoup(html, "html.parser")
    text = " ".join(soup.get_text(" ").split())
    info = {"rows": [], "skipped": 0, "start": None, "end": None, "total": None, "pages": None}

    showing = SHOWING_RE.search(text)
    if showing:
        info["start"], info["end"], info["total"] = (to_int(g) for g in showing.groups())
    page_of = PAGE_OF_RE.search(text)
    if page_of:
        info["pages"] = to_int(page_of.group(1))

    for tr in soup.find_all("tr", class_=["trl", "trd"]):
        row = parse_row(tr)
        if row:
            info["rows"].append(row)
        else:
            info["skipped"] += 1
    return info


def page_count(info: dict, per_page: int) -> int | None:
    if info["total"] is not None and per_page:
        return max(1, math.ceil(info["total"] / per_page))
    return info["pages"]


# ---------------------------------------------------------------- fetching

def page_url(page: int, style: str) -> str:
    if style == "query":
        return f"{REGISTRY_URL}?page={page}&sort={SORT_BY_NAME}"
    return REGISTRY_URL if page == 1 else f"{REGISTRY_URL}page-{page}.html"


def retry_after(response: requests.Response) -> float:
    value = response.headers.get("Retry-After", "").strip()
    return min(float(value), 300.0) if value.isdigit() else 0.0


def fetch_page(session: requests.Session, url: str, min_rows: int = 1) -> dict:
    """Download and parse one page, retrying until it looks like a real registry page.

    min_rows=0 accepts a page with no rows as long as it still shows the registry
    counters (only used for the last page, which can empty out mid-scrape).
    """
    attempts = len(RETRY_WAITS) + 1
    problem, empty = "", False
    for attempt in range(1, attempts + 1):
        server_wait, empty = 0.0, False
        try:
            response = session.get(url, timeout=REQUEST_TIMEOUT)
            if response.status_code == 200:
                info = parse_page(response.content)  # bytes: let BeautifulSoup detect the encoding
                rows = len(info["rows"])
                if rows >= min_rows and (rows or info["total"] is not None):
                    return info
                if rows:
                    problem = f"only {rows} domain rows"
                else:
                    problem, empty = "no domain rows", True
            else:
                problem = f"HTTP {response.status_code}"
                server_wait = retry_after(response)
        except requests.RequestException as exc:
            problem = f"{type(exc).__name__}: {exc}"
        if attempt == attempts:
            break
        wait = max(server_wait, RETRY_WAITS[attempt - 1])
        log.warning("%s: %s (attempt %d/%d), retrying in %.0fs",
                    url, problem, attempt, attempts, wait)
        time.sleep(wait)
    error = EmptyPage if empty else ScrapeError
    raise error(f"{url}: {problem} after {attempts} attempts")


def crawl(session: requests.Session, style: str, delay: float,
          max_pages: int | None) -> tuple[list[dict], int | None]:
    """Walk every registry page using one URL style. Returns (rows, reported total)."""
    try:
        first = fetch_page(session, page_url(1, style))
    except ScrapeError as exc:
        raise StyleFailed(f"page 1 failed with {style} URLs ({exc})") from exc
    if first["start"] not in (None, 1):
        raise StyleFailed(f"page 1 starts at domain {first['start']:,} instead of 1 ({style} URLs)")
    if first["start"] is not None:
        per_page = first["end"] - first["start"] + 1
    else:
        per_page = len(first["rows"])
    pages = page_count(first, per_page)
    reported = first["total"]
    first_domains = [row["domain"] for row in first["rows"]]

    if pages is None:
        log.warning("No page counter found, crawling until a page comes back empty")
    else:
        log.info("Registry reports %s domains on %d pages (%s URLs)",
                 f"{reported:,}" if reported is not None else "?", pages, style)
    log.info("page 1/%s: %d domains", pages or "?", len(first["rows"]))

    rows = list(first["rows"])
    previous_domains = first_domains
    page = 1
    while True:
        if max_pages and page >= max_pages:
            break
        if pages is not None and page >= pages:
            break
        if page >= MAX_PAGES:
            raise ScrapeError(f"stopped after {MAX_PAGES} pages, pagination looks broken")
        page += 1
        time.sleep(delay * random.uniform(0.75, 1.25))

        is_last = pages is not None and page == pages
        min_rows = 0 if is_last else (max(1, per_page // 2) if pages is not None else 1)
        try:
            info = fetch_page(session, page_url(page, style), min_rows)
        except EmptyPage:
            if pages is None:
                break  # no counter to go by, so an empty page is the end
            raise

        domains = [row["domain"] for row in info["rows"]]
        expected_start = (page - 1) * per_page + 1
        if domains and info["start"] is not None and info["start"] != expected_start:
            raise StyleFailed(f"page {page} starts at domain {info['start']:,} instead of "
                              f"{expected_start:,} ({style} URLs)")
        if domains and domains == first_domains:
            raise StyleFailed(f"asked for page {page} but got page 1 back ({style} URLs)")
        if domains and domains == previous_domains:
            if pages is None:
                break  # server repeats the last page past the end
            raise StyleFailed(f"page {page} is a repeat of page {page - 1} ({style} URLs)")
        if info["skipped"]:
            log.warning("page %d: %d rows had no readable domain", page, info["skipped"])

        rows.extend(info["rows"])
        previous_domains = domains
        latest = page_count(info, per_page)
        if latest is not None:
            if pages is not None and latest != pages:
                log.info("Page count changed from %d to %d during the scrape", pages, latest)
            pages = latest
        if info["total"] is not None:
            reported = info["total"]
        log.info("page %d/%s: %d domains", page, pages or "?", len(domains))
        if not domains:
            break
    return rows, reported


def scrape_registry(delay: float = PAGE_DELAY,
                    max_pages: int | None = None) -> tuple[list[dict], int | None]:
    """Scrape the whole registry.

    Returns (domains, reported_total). domains is a list of dicts with the
    CSV_FIELDS keys, unique by domain and sorted by name. Tries ?page=N&sort=1
    URLs first and falls back to page-N.html if those misbehave.
    """
    failures: list[str] = []
    best: tuple[list[dict], int | None] | None = None
    with requests.Session() as session:
        session.headers.update(HEADERS)
        for style in ("query", "static"):
            try:
                rows, reported = crawl(session, style, delay, max_pages)
            except StyleFailed as exc:
                failures.append(str(exc))
                log.warning("%s", exc)
                continue
            unique: dict[str, dict] = {}
            for row in rows:
                unique.setdefault(row["domain"], row)
            domains = sorted(unique.values(), key=lambda row: row["domain"])
            if max_pages or not reported or len(domains) >= reported * MIN_COMPLETENESS:
                return domains, reported
            log.warning("%s URLs only gave %s of %s domains", style,
                        f"{len(domains):,}", f"{reported:,}")
            if best is None or len(domains) > len(best[0]):
                best = (domains, reported)
    if best:
        return best  # incomplete: main() refuses to save it unless --force
    raise ScrapeError("; ".join(failures))


# ---------------------------------------------------------------- output

def read_previous(out_dir: Path) -> tuple[set[str] | None, dict[str, str]]:
    """Domains from the last run (None on the first run) plus their statuses."""
    txt = out_dir / DOMAINS_TXT
    if not txt.exists():
        return None, {}
    names = {line.strip() for line in txt.read_text(encoding="utf-8").splitlines() if line.strip()}
    statuses: dict[str, str] = {}
    csv_path = out_dir / DOMAINS_CSV
    if csv_path.exists():
        with csv_path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                statuses[row.get("domain", "")] = row.get("status", "")
    return names or None, statuses


def check_results(count: int, reported: int | None, previous_count: int) -> None:
    if reported and count < reported * MIN_COMPLETENESS:
        raise ScrapeError(
            f"only got {count:,} of the {reported:,} domains the registry reports"
        )
    if previous_count and count < previous_count * (1 - MAX_DROP):
        raise ScrapeError(
            f"domain count fell from {previous_count:,} to {count:,} "
            f"(more than {MAX_DROP:.0%}), rerun with --force if that is real"
        )


def write_text(path: Path, text: str) -> None:
    """Write via a temp file so an interrupted run never leaves a half-written file."""
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    os.replace(tmp, path)


def csv_text(domains: list[dict]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(domains)
    return buffer.getvalue()


def build_entry(now: datetime, domains: list[dict], added: set[str], removed: set[str],
                new_status: dict[str, str], old_status: dict[str, str], first_run: bool) -> str:
    counts = Counter(row["status"] for row in domains)
    breakdown = ", ".join(f"{n:,} {status or 'unknown'}" for status, n in counts.most_common())
    lines = [f"## {now:%Y-%m-%d %H:%M} UTC", ""]
    if first_run:
        lines.append(f"Initial snapshot: {len(domains):,} domains ({breakdown}).")
        return "\n".join(lines) + "\n"
    lines += [f"{len(domains):,} domains ({breakdown}). "
              f"Added {len(added):,}, removed {len(removed):,}.", ""]
    for title, names, statuses in (("Added", added, new_status), ("Removed", removed, old_status)):
        if not names:
            continue
        lines += [f"### {title} ({len(names):,})", ""]
        for name in sorted(names):
            status = statuses.get(name)
            lines.append(f"- {name} ({status})" if status else f"- {name}")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def prepend_entry(path: Path, entry: str) -> None:
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    if ENTRIES_MARKER in old:
        head, rest = old.split(ENTRIES_MARKER, 1)
    else:
        head, rest = CHANGES_HEADER.split(ENTRIES_MARKER, 1)[0], "\n" + old
    rest = rest.strip("\n")
    write_text(path, head + ENTRIES_MARKER + "\n\n" + entry + ("\n" + rest + "\n" if rest else ""))


def report_to_github(entry: str, message: str, total: int, added: int, removed: int) -> None:
    """Job summary and step outputs when running inside GitHub Actions."""
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write("## FreeDNS registry, " + entry.removeprefix("## ") + "\n")
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"message={message}\ntotal={total}\nadded={added}\nremoved={removed}\n")


# ---------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scrape the FreeDNS shared domain registry.")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the domains instead of writing files")
    parser.add_argument("--max-pages", type=int, metavar="N",
                        help="only scrape the first N pages (implies --dry-run)")
    parser.add_argument("--force", action="store_true",
                        help="save even if the completeness or drop checks fail")
    parser.add_argument("--delay", type=float, default=PAGE_DELAY, metavar="SECONDS",
                        help=f"pause between pages (default {PAGE_DELAY})")
    parser.add_argument("--out", type=Path, default=ROOT, metavar="DIR",
                        help="folder for the output files (default: next to this script)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S", stream=sys.stderr)

    dry_run = args.dry_run or args.max_pages is not None
    try:
        domains, reported = scrape_registry(args.delay, args.max_pages)
        previous, old_status = read_previous(args.out)
        if not dry_run and not args.force:
            check_results(len(domains), reported, len(previous or ()))
    except ScrapeError as exc:
        log.error("%s. Nothing was written.", exc)
        return 1

    if dry_run:
        for row in domains:
            print(row["domain"])
        log.info("Got %d domains (dry run, nothing written)", len(domains))
        return 0

    args.out.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    current = {row["domain"] for row in domains}
    new_status = {row["domain"]: row["status"] for row in domains}
    first_run = previous is None
    added = set() if first_run else current - previous
    removed = set() if first_run else previous - current

    changed = first_run or bool(added or removed)
    if changed:
        entry = build_entry(now, domains, added, removed, new_status, old_status, first_run)
    else:
        entry = f"## {now:%Y-%m-%d %H:%M} UTC\n\nNo changes ({len(domains):,} domains).\n"
    # changes.md first and domains.txt last: if a run dies midway, the next run
    # repeats the diff instead of losing it.
    prepend_entry(args.out / CHANGES_MD, entry)
    if changed:
        # The domain files are only rewritten when domains were added or removed.
        write_text(args.out / DOMAINS_CSV, csv_text(domains))
        write_text(args.out / DOMAINS_TXT, "".join(row["domain"] + "\n" for row in domains))

    if first_run:
        message = f"Initial domain snapshot: {len(domains):,} domains"
    elif changed:
        message = f"Update domains: {len(domains):,} total, +{len(added)} -{len(removed)}"
    else:
        message = f"No changes: {len(domains):,} domains"
    log.info(message)
    report_to_github(entry, message, len(domains), len(added), len(removed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
