"""Watch the MyCompPrep competition calendar for SA and VIC shows.

Runs in GitHub Actions. Each new SA/VIC listing is reported by opening a
GitHub issue labelled "show-alert"; GitHub then emails the repo owner.
Issues double as memory: every reported listing carries a hidden key in the
issue body, so it is never reported twice.

Every SA/VIC listing is also kept in data/shows.json (date, name, venue,
state, federation, when first seen, whether still listed) for the dashboard.

Environment:
  GITHUB_TOKEN, GITHUB_REPOSITORY  set by GitHub Actions
  DRY_RUN=1                        print what would be reported, open no issues
  CALENDAR_FILE=path               read HTML from a file instead of the website
  SHOWS_FILE=path                  where to keep the show data (default data/shows.json)
"""

import datetime
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
STATE_CELL_RE = re.compile(r"^(SA|VIC|NSW|QLD|WA|TAS|ACT|NT)$")
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
DATE_RE = re.compile(
    # "04 Oct", "17/18 Apr" (first day of a range), "October 4"
    r"\b(?:(\d{1,2})(?:st|nd|rd|th)?(?:\s*[/&-]\s*\d{1,2}(?:st|nd|rd|th)?)?\s+(%s)[a-z]*"
    r"|(%s)[a-z]*\s+(\d{1,2}))\b" % (("|".join(MONTHS),) * 2),
    re.IGNORECASE,
)


class RowParser(HTMLParser):
    """Collects table rows and list items.

    Each table cell is kept as a list of text parts, split wherever a tag
    starts or ends inside it (so a name and venue in separate elements stay
    apart).
    """

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
            self._cell = [[]]
        elif self._cell is not None:
            self._cell.append([])
        if tag == "li":
            self._item = []

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self._skip = max(0, self._skip - 1)
        elif tag in ("td", "th") and self._cell is not None:
            parts = [clean(" ".join(p)) for p in self._cell]
            self._row.append([p for p in parts if p])
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
        if self._cell is not None:
            self._cell.append([])

    def handle_data(self, data):
        if self._skip:
            return
        if self._cell is not None:
            self._cell[-1].append(data)
        if self._item is not None:
            self._item.append(data)


def clean(text):
    return re.sub(r"\s+", " ", text).strip()


def is_sa_or_vic(text):
    return bool(ABBREV_RE.search(text) or NAME_RE.search(text))


def extract_listings(html):
    """Return (all listings, SA/VIC listings).

    Each listing is (display text, cells), where cells is a list of each
    table cell's text parts (empty for the list-layout fallback).
    """
    parser = RowParser()
    parser.feed(html)
    listings = []
    # Table rows are the expected layout; header rows (all <th>) are skipped
    # naturally because they won't mention a state.
    for row in parser.rows:
        # Drop empty cells and the site's "Register →" button text.
        cells = [c for c in row if c and not " ".join(c).lower().startswith("register")]
        if len(row) >= 2 and cells:
            listings.append((" | ".join(" ".join(c) for c in cells), cells))
    if not listings:
        # Fallback if the site switches to a list layout. Short items are
        # navigation links, not shows.
        listings = [(i, []) for i in parser.items if len(i) > 25 and re.search(r"\d", i)]
    seen, matches = set(), []
    for text, cells in listings:
        if is_sa_or_vic(text) and text not in seen:
            seen.add(text)
            matches.append((text, cells))
    return listings, matches


def parse_date(text, today):
    """Turn "04 Oct" or "October 4" into a date. The calendar omits years and
    lists upcoming shows, so take the first match no more than 60 days ago."""
    m = DATE_RE.search(text)
    if not m:
        return None
    day = int(m.group(1) or m.group(4))
    month = MONTHS[(m.group(2) or m.group(3))[:3].lower()]
    options = []
    for year in (today.year - 1, today.year, today.year + 1):
        try:
            options.append(datetime.date(year, month, day))
        except ValueError:
            pass
    recent = today - datetime.timedelta(days=60)
    return next((d for d in options if d >= recent), None)


def describe(text, cells, today):
    """Pull date, name, venue, state and federation out of one listing."""
    show = {"key": key_for(text), "listing": text, "date": None, "date_label": "",
            "name": text, "venue": "", "state": "", "federation": ""}
    rest = []
    for parts in cells:
        joined = " ".join(parts)
        if show["date"] is None and DATE_RE.search(joined) and len(joined) <= 20:
            d = parse_date(joined, today)
            show["date"], show["date_label"] = d.isoformat() if d else None, joined
        elif not show["state"] and STATE_CELL_RE.match(joined):
            show["state"] = joined
        else:
            rest.append(parts)
    if rest:
        # The live site puts name and venue in one cell as separate parts;
        # otherwise the first remaining cell is the name and the longest
        # other one the venue.
        main = next((p for p in rest if len(p) > 1), rest[0])
        show["name"], show["venue"] = main[0], ", ".join(main[1:])
        others = [" ".join(p) for p in rest if p is not main]
        show["federation"] = next((o for o in others if len(o) <= 12), "")
        if not show["venue"]:
            show["venue"] = max((o for o in others if len(o) > 12), key=len, default="")
    if show["date"] is None:
        d = parse_date(text, today)
        show["date"] = d.isoformat() if d else None
    if not show["state"]:
        show["state"] = "SA" if re.search(r"\bSA\b|South Australia|Adelaide", text, re.I) else "VIC"
    return show


def load_shows_file(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"shows": []}


def save_shows_file(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def note_failed_check(path, now, message):
    """Record a failed check so the dashboard can say the data is stale."""
    data = load_shows_file(path)
    data["last_error"] = {"at": now.isoformat(timespec="seconds"), "message": message}
    save_shows_file(path, data)


def update_shows_file(path, matches, now):
    """Merge today's SA/VIC listings into the show data file."""
    data = load_shows_file(path)
    today = now.date()
    stamp = now.isoformat(timespec="seconds")
    by_key = {s["key"]: s for s in data.get("shows", [])}
    current = set()
    for text, cells in matches:
        show = describe(text, cells, today)
        current.add(show["key"])
        old = by_key.get(show["key"], {})
        show["first_seen"] = old.get("first_seen", today.isoformat())
        show["listed"] = True
        by_key[show["key"]] = show
    for key, show in by_key.items():
        if key not in current:
            show["listed"] = False
    data = {
        "checked_at": stamp,
        "source": CALENDAR_URL,
        "shows": sorted(by_key.values(), key=lambda s: (s["date"] or "9999", s["name"])),
    }
    save_shows_file(path, data)


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

    now = datetime.datetime.now(datetime.timezone.utc)
    shows_file = os.environ.get("SHOWS_FILE", "data/shows.json")

    def fail(message):
        report_error(gh, message)
        if not dry_run:
            note_failed_check(shows_file, now, message)
        return 1

    try:
        html = fetch_calendar()
    except Exception as e:  # network errors, HTTP 403/5xx, timeouts
        return fail(f"Could not download the page: {e}")

    listings, matches = extract_listings(html)
    print(f"Found {len(listings)} listings, {len(matches)} in SA or VIC.")
    if not listings:
        title = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
        print(f"Page was {len(html)} characters, {html.count('<tr')} table rows, "
              f"title {clean(title.group(1)) if title else 'missing'!r}")
        return fail("The page loaded but no show listings were found in it.")

    if dry_run:
        for text, cells in matches:
            print(f"  [{key_for(text)}] {text}")
            print(f"      {json.dumps(describe(text, cells, now.date()), ensure_ascii=False)}")
        return 0

    update_shows_file(shows_file, matches, now)
    matches = [text for text, _ in matches]

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
