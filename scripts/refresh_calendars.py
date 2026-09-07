#!/usr/bin/env python3
"""
Fetches each property's Airbnb iCal export and writes a compact JSON file
of blocked (booked/unavailable) date ranges to data/calendars/<slug>.json.

Reads the AIRBNB_ICS_URLS environment variable -- a JSON object mapping
property slug -> ics export URL, e.g.:

  {"property2": "https://www.airbnb.com/calendar/ical/XXXX.ics?t=YYYY"}

The URLs themselves contain a private access token and are kept out of
the repository entirely -- they live only in the repo's
"AIRBNB_ICS_URLS" Actions secret. This script (and the workflow that
runs it) never prints the URLs to logs.
"""
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone

OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "calendars")


def unfold(text):
    """RFC5545 line unfolding: a line starting with a space/tab continues the previous line."""
    lines = text.replace("\r\n", "\n").split("\n")
    out = []
    for line in lines:
        if line.startswith(" ") or line.startswith("\t"):
            if out:
                out[-1] += line[1:]
        else:
            out.append(line)
    return out


def parse_events(ics_text):
    """Extract (start, end) date pairs (YYYY-MM-DD strings, end exclusive) from VEVENT blocks."""
    events = []
    in_event = False
    start = end = None
    for line in unfold(ics_text):
        if line == "BEGIN:VEVENT":
            in_event = True
            start = end = None
        elif line == "END:VEVENT":
            if start and end:
                events.append({"start": start, "end": end})
            in_event = False
        elif in_event:
            m = re.match(r"DTSTART(;[^:]*)?:(\d{8})", line)
            if m:
                d = m.group(2)
                start = f"{d[0:4]}-{d[4:6]}-{d[6:8]}"
            m = re.match(r"DTEND(;[^:]*)?:(\d{8})", line)
            if m:
                d = m.group(2)
                end = f"{d[0:4]}-{d[4:6]}-{d[6:8]}"
    return events


def merge_ranges(ranges):
    """Merge/sort overlapping or touching [start, end) date ranges."""

    def to_ord(s):
        return datetime.strptime(s, "%Y-%m-%d").toordinal()

    spans = sorted(
        ((to_ord(r["start"]), to_ord(r["end"])) for r in ranges),
        key=lambda x: x[0],
    )
    merged = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return [
        {
            "start": datetime.fromordinal(s).strftime("%Y-%m-%d"),
            "end": datetime.fromordinal(e).strftime("%Y-%m-%d"),
        }
        for s, e in merged
    ]


def fetch_ics(url):
    req = urllib.request.Request(url, headers={"User-Agent": "RentifyCalendarSync/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def main():
    raw = os.environ.get("AIRBNB_ICS_URLS", "").strip()
    if not raw:
        print("AIRBNB_ICS_URLS secret is not set (or empty) -- nothing to refresh.")
        return 0

    try:
        urls = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"AIRBNB_ICS_URLS is not valid JSON: {e}", file=sys.stderr)
        return 1

    os.makedirs(OUT_DIR, exist_ok=True)
    any_changed = False
    had_error = False

    for slug, url in urls.items():
        try:
            ics_text = fetch_ics(url)
        except Exception as e:
            # Never print the URL itself (it contains a private token).
            print(f"[{slug}] fetch failed: {e}", file=sys.stderr)
            had_error = True
            continue

        events = parse_events(ics_text)
        blocked = merge_ranges(events) if events else []

        out_path = os.path.join(OUT_DIR, f"{slug}.json")
        payload = {
            "property": slug,
            "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "blocked": blocked,
        }

        existing = None
        if os.path.exists(out_path):
            with open(out_path) as f:
                try:
                    existing = json.load(f)
                except json.JSONDecodeError:
                    existing = None

        if existing and existing.get("blocked") == blocked:
            # Nothing actually changed -- keep the previous timestamp so the
            # file (and therefore `git diff`) stays byte-identical and the
            # workflow doesn't create a no-op commit every run.
            payload["updated"] = existing.get("updated", payload["updated"])
        else:
            any_changed = True

        with open(out_path, "w") as f:
            json.dump(payload, f, indent=2)
            f.write("\n")

        print(f"[{slug}] {len(blocked)} blocked range(s) -> {out_path}")

    gh_output = os.environ.get("GITHUB_OUTPUT")
    if gh_output:
        with open(gh_output, "a") as f:
            f.write(f"changed={'true' if any_changed else 'false'}\n")

    return 1 if had_error and not any_changed else 0


if __name__ == "__main__":
    sys.exit(main())
