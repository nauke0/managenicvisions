"""Watch the MyCompPrep competition calendar for SA and VIC shows.

Runs in GitHub Actions. Each new SA/VIC listing is reported by opening a
GitHub issue labelled "show-alert"; GitHub then emails the repo owner.
Issues double as memory: every reported listing carries a hidden key in the
issue body, so it is never reported twice.

Environment:
  GITHUB_TOKEN, GITHUB_REPOSITORY  set by GitHub Actions
  DRY_RUN=1                        print what would be reported, open no issues
  CALENDAR_FILE=path               read HTML from a file instead of the website
"""

import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from html.parser import HTMLParser

CALENDAR_URL = "https://mycompprep.com/competition-calendar/"
ALERT_LABEL = "show-alert"
ERROR_LABEL = "show-monitor-error"
KEY_RE = re.compile(r"<!-- show-key: ([0-9a-f]{16}) -->")

# A listing counts as SA or VIC if it names the state or a city in it. The
# abbreviations stay case-sensitive so words like "sa" in prose don't match.
ABBREV_RE = re.compile(r"\b(SA|VIC)\b")
NAME_RE = re.compile(r"\b(South Australia|Victoria|Victorian|Adelaide|Melbourne)\b", re.IGNORECASE)


class RowParser(HTMLParser):
    """Collects table rows (as lists of cell text) and list items."""

    def __init__(self):
        super().__init__()
        self.rows, self.items = [], []
        self._row = self._cell = self._item = None
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self._skip += 1
        elif tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
        elif tag == "li":
            self._item = []

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self._skip = max(0, self._skip - 1)
        elif tag in ("td", "th") and self._cell is not None:
            self._row.append(clean(" ".join(self._cell)))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if any(self._row):
                self.rows.append(self._row)
            self._row = None
        elif tag == "li" and self._item is not None:
            text = clean(" ".join(self._item))
            if text:
                self.items.append(text)
            self._item = None

    def handle_data(self, data):
        if self._skip:
            return
        if self._cell is not None:
            self._cell.append(data)
        if self._item is not None:
            self._item.append(data)


def clean(text):
    return re.sub(r"\s+", " ", text).strip()


def is_sa_or_vic(text):
    return bool(ABBREV_RE.search(text) or NAME_RE.search(text))


def extract_listings(html):
    """Return (all listings, SA/VIC listings) as display strings."""
    parser = RowParser()
    parser.feed(html)
    # Table rows are the expected layout; header rows (all <th>) are skipped
    # naturally because they won't mention a state.
    listings = [" | ".join(c for c in row if c) for row in parser.rows if len(row) >= 2]
    if not listings:
        # Fallback if the site switches to a list layout. Short items are
        # navigation links, not shows.
        listings = [i for i in parser.items if len(i) > 25 and re.search(r"\d", i)]
    matches = list(dict.fromkeys(l for l in listings if is_sa_or_vic(l)))
    return listings, matches


def key_for(listing):
    return hashlib.sha256(listing.lower().encode()).hexdigest()[:16]


def fetch_calendar():
    path = os.environ.get("CALENDAR_FILE")
    if path:
        with open(path, encoding="utf-8") as f:
            return f.read()
    req = urllib.request.Request(
        CALENDAR_URL,
        headers={"User-Agent": "Mozilla/5.0 (show monitor for NicVisions Photography)"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8", errors="replace")


class GitHub:
    def __init__(self):
        self.repo = os.environ["GITHUB_REPOSITORY"]
        self.token = os.environ["GITHUB_TOKEN"]

    def call(self, method, path, body=None):
        req = urllib.request.Request(
            f"https://api.github.com/repos/{self.repo}{path}",
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read() or "null")

    def ensure_label(self, name, color, description):
        try:
            self.call("POST", "/labels", {"name": name, "color": color, "description": description})
        except urllib.error.HTTPError as e:
            if e.code != 422:  # 422 = label already exists
                raise

    def issues(self, label, state):
        page, out = 1, []
        while True:
            batch = self.call("GET", f"/issues?labels={label}&state={state}&per_page=100&page={page}")
            out += batch
            if len(batch) < 100:
                return out
            page += 1

    def open_issue(self, title, body, label):
        issue = self.call("POST", "/issues", {"title": title, "body": body, "labels": [label]})
        print(f"Opened issue #{issue['number']}: {title}")


def report_error(gh, message):
    print(f"ERROR: {message}", file=sys.stderr)
    if gh is None:
        return
    gh.ensure_label(ERROR_LABEL, "d73a4a", "The show monitor needs attention")
    if gh.issues(ERROR_LABEL, "open"):
        print("An error issue is already open; not opening another.")
        return
    gh.open_issue(
        "Show monitor couldn't read the competition calendar",
        f"The daily check of {CALENDAR_URL} failed:\n\n> {message}\n\n"
        "The website may be down, blocking the check, or have changed its layout. "
        "Close this issue once it's sorted; a new one opens if it keeps failing.",
        ERROR_LABEL,
    )


def listing_lines(listings):
    return "\n".join(f"- {l} <!-- show-key: {key_for(l)} -->" for l in listings)


def main():
    dry_run = os.environ.get("DRY_RUN") == "1"
    gh = None if dry_run else GitHub()

    try:
        html = fetch_calendar()
    except Exception as e:  # network errors, HTTP 403/5xx, timeouts
        report_error(gh, f"Could not download the page: {e}")
        return 1

    listings, matches = extract_listings(html)
    print(f"Found {len(listings)} listings, {len(matches)} in SA or VIC.")
    if not listings:
        report_error(gh, "The page loaded but no show listings were found in it.")
        return 1

    if dry_run:
        for m in matches:
            print(f"  [{key_for(m)}] {m}")
        return 0

    gh.ensure_label(ALERT_LABEL, "c5a28a", "New SA or VIC show on the competition calendar")
    past = gh.issues(ALERT_LABEL, "all")
    seen = {k for issue in past for k in KEY_RE.findall(issue.get("body") or "")}
    new = [m for m in matches if key_for(m) not in seen]

    if not new:
        print("Nothing new.")
        return 0

    if not past:
        title = f"Show monitor is on: {len(new)} SA/VIC shows currently listed"
        intro = (
            "The show monitor is now checking the competition calendar every day. "
            "These SA and VIC shows are already listed. From now on you'll get an "
            "email like this only when a new one appears.\n\n"
        )
    else:
        title = f"New SA/VIC show{'s' if len(new) > 1 else ''} listed ({len(new)})"
        intro = "New or updated listings on the competition calendar:\n\n"

    gh.open_issue(
        title,
        f"{intro}{listing_lines(new)}\n\nSource: {CALENDAR_URL}\n\n"
        "Always confirm dates with the federation before planning around them.",
        ALERT_LABEL,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
