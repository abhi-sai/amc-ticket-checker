#!/usr/bin/env python3
"""
AMC Ticket Watcher
------------------
Polls specific AMC showtime seat-picker pages, finds runs of adjacent open
seats within your allowed rows, and pings a Discord webhook when it finds
something matching your criteria.

Setup:
    pip install playwright pyyaml requests
    playwright install chromium

Run:
    python watcher.py --config config.yaml

If seat detection stops working (AMC changed their page), run with --debug
on a single showtime to dump a screenshot + HTML snapshot you can inspect,
then adjust SEAT_ID_RE / find_seats() below to match the new markup.
"""

import argparse
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests
import yaml
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("amc-watcher")

STATE_FILE = Path("watcher_state.json")

# -----------------------------------------------------------------------------
# Seat map extraction
# -----------------------------------------------------------------------------
# Confirmed from live markup: AMC's seat picker uses one hidden checkbox
# `<input>` per seat, with:
#   - name="A18"                              <- the seat id (row + number)
#   - aria-label="Occupied AMC Club Rocker A18"   <- taken
#   - aria-label="AMC Club Rocker A33"            <- open
#   - aria-label="Wheelchair Space A30" / "Wheelchair Companion ..."
# The "Occupied " prefix is the ground-truth status straight from the app,
# rather than something inferred from rendered colors — much less fragile.
# If AMC changes this later, use --debug to re-inspect and update here.

SEAT_ID_RE = re.compile(r"^([A-Za-z]{1,2})(\d+)$")


@dataclass
class Seat:
    row: str
    number: int
    available: bool
    seat_type: str = ""


def find_seats(page) -> list[Seat]:
    seats: list[Seat] = []
    inputs = page.query_selector_all("input[aria-label][name]")
    for inp in inputs:
        name = (inp.get_attribute("name") or "").strip()
        m = SEAT_ID_RE.match(name)
        if not m:
            continue  # not a seat checkbox (some other control on the page)

        row, num = m.group(1).upper(), int(m.group(2))
        aria = (inp.get_attribute("aria-label") or "").strip()
        is_occupied = aria.lower().startswith("occupied")

        seat_type = re.sub(r"^occupied\s+", "", aria, flags=re.IGNORECASE)
        seat_type = re.sub(rf"\s*{re.escape(name)}$", "", seat_type).strip()

        seats.append(Seat(row=row, number=num, available=not is_occupied, seat_type=seat_type))
    return seats


COOKIE_BANNER_SELECTORS = [
    "#onetrust-accept-btn-handler",
    "button[id*='accept' i]",
    "button:has-text('Accept')",
    "button:has-text('I Accept')",
    "button:has-text('Agree')",
]


def dismiss_cookie_banner(page):
    """Best-effort: click through any cookie/consent overlay that might be
    covering the page (these commonly block rendering/interaction)."""
    for sel in COOKIE_BANNER_SELECTORS:
        try:
            btn = page.query_selector(sel)
            if btn and btn.is_visible():
                btn.click(timeout=2000)
                page.wait_for_timeout(500)
                log.info("Dismissed a cookie/consent banner (%s)", sel)
                return
        except Exception:
            continue


def dump_debug(page, debug_tag: str):
    """Always try to leave *something* behind to inspect, even if parts of
    this fail (screenshotting can be flaky on some pages/OSes)."""
    try:
        Path(f"debug_{debug_tag}.html").write_text(page.content(), encoding="utf-8")
        log.info("Wrote debug_%s.html", debug_tag)
    except Exception as e:
        log.error("Could not save HTML: %s", e)

    try:
        page.screenshot(path=f"debug_{debug_tag}.png", full_page=False)
        log.info("Wrote debug_%s.png (viewport)", debug_tag)
    except Exception as e:
        log.warning("Viewport screenshot failed (%s), trying full_page", e)
        try:
            page.screenshot(path=f"debug_{debug_tag}.png", full_page=True)
            log.info("Wrote debug_%s.png (full page)", debug_tag)
        except Exception as e2:
            log.error("Screenshot failed entirely, HTML dump still saved: %s", e2)

    log.info("Page title: %r | Final URL: %s", page.title(), page.url)


def fetch_seat_map(page, url: str, debug_tag: Optional[str] = None) -> list[Seat]:
    page.goto(url, wait_until="domcontentloaded", timeout=45000)

    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except PWTimeout:
        pass  # some pages never go fully idle (polling, analytics, etc.) — fine

    dismiss_cookie_banner(page)

    # Seat maps render client-side, sometimes progressively (e.g. skeleton ->
    # partial -> full). Poll for actual labeled seats to show up rather than
    # trusting a single wait_for_selector against a generic wrapper element.
    seats: list[Seat] = []
    deadline = time.time() + 25
    while time.time() < deadline:
        seats = find_seats(page)
        if seats:
            break
        page.wait_for_timeout(1000)

    if not seats:
        log.warning("No seat elements appeared for %s within timeout", url)

    page.wait_for_timeout(500)  # let any remaining async rendering settle
    seats = find_seats(page) or seats  # pick up any late-arriving seats

    if debug_tag or not seats:
        dump_debug(page, debug_tag or "auto")

    if debug_tag:
        breakdown: dict[tuple[str, bool], int] = {}
        for s in seats:
            key = (s.seat_type or "(unknown type)", s.available)
            breakdown[key] = breakdown.get(key, 0) + 1
        log.info("Seat status breakdown (seat_type, status) -> count:")
        for (stype, available), count in sorted(breakdown.items(), key=lambda kv: -kv[1]):
            log.info("  %-45s %-9s -> %4d", stype, "OPEN" if available else "OCCUPIED", count)
        if not breakdown:
            log.info(
                "  (none found — check debug_%s.html/.png; the seat map may not "
                "have rendered, or the markup structure has changed)", debug_tag or "auto"
            )
        rows_seen = sorted({s.row for s in seats})
        log.info("Rows detected: %s", ", ".join(rows_seen) if rows_seen else "(none)")

    return seats


# -----------------------------------------------------------------------------
# Seat-run logic
# -----------------------------------------------------------------------------

def find_adjacent_runs(seats: list[Seat], allowed_rows: set[str], min_together: int, exclude_seat_types=None):
    """Return {row: [ [n1,n2,...], ... ]} of runs of consecutive available
    seat numbers, length >= min_together, restricted to allowed_rows.

    Seats whose seat_type matches anything in exclude_seat_types (substring,
    case-insensitive — e.g. "wheelchair") are dropped entirely before
    building runs. This both keeps them out of results and correctly treats
    them as a gap, so two regular seats on either side of an (available)
    wheelchair space won't be reported as "together."
    """
    exclude_seat_types = [e.lower() for e in (exclude_seat_types or [])]

    def is_excluded(seat: Seat) -> bool:
        stype = (seat.seat_type or "").lower()
        return any(term in stype for term in exclude_seat_types)

    by_row: dict[str, list[int]] = {}
    for s in seats:
        if s.available and s.row in allowed_rows and not is_excluded(s):
            by_row.setdefault(s.row, []).append(s.number)

    results: dict[str, list[list[int]]] = {}
    for row, nums in by_row.items():
        nums = sorted(set(nums))
        run: list[int] = []
        runs: list[list[int]] = []
        for n in nums:
            if run and n == run[-1] + 1:
                run.append(n)
            else:
                if len(run) >= min_together:
                    runs.append(run)
                run = [n]
        if len(run) >= min_together:
            runs.append(run)
        if runs:
            results[row] = runs
    return results


# -----------------------------------------------------------------------------
# Notifications
# -----------------------------------------------------------------------------

def notify_discord(webhook_url: str, label: str, url: str, runs: dict):
    if not webhook_url:
        log.info("[NOTIFY] %s -> %s", label, runs)
        return

    lines = [f"**{row}**: seats {', '.join(map(str, run))}" for row, group in runs.items() for run in group]
    embed = {
        "title": f"🎬 Seats open — {label}",
        "description": "\n".join(lines),
        "url": url,
        "color": 0x2ECC71,
    }
    try:
        resp = requests.post(webhook_url, json={"embeds": [embed]}, timeout=10)
        resp.raise_for_status()
    except requests.RequestException as e:
        log.error("Discord notification failed: %s", e)


# -----------------------------------------------------------------------------
# State (dedupe / cooldown)
# -----------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def runs_signature(runs: dict) -> str:
    return json.dumps(runs, sort_keys=True)


# -----------------------------------------------------------------------------
# Main loop
# -----------------------------------------------------------------------------

def check_once(page, cfg, state):
    allowed_rows = {r.upper() for r in cfg["allowed_rows"]}
    min_together = cfg["min_seats_together"]
    cooldown_s = cfg.get("notify_cooldown_minutes", 20) * 60
    webhook = cfg.get("discord_webhook_url", "")
    exclude_seat_types = cfg.get("exclude_seat_types", ["wheelchair"])

    for showtime in cfg["showtimes"]:
        label, url = showtime["label"], showtime["url"]
        try:
            seats = fetch_seat_map(page, url)
        except Exception as e:
            log.error("Failed to check '%s': %s", label, e)
            continue

        if not seats:
            log.warning("No seats parsed for '%s' — selectors may need updating (see --debug)", label)
            continue

        runs = find_adjacent_runs(seats, allowed_rows, min_together, exclude_seat_types)
        key = label
        now = time.time()
        last = state.get(key, {})

        if runs:
            sig = runs_signature(runs)
            same_as_last = last.get("sig") == sig
            cooled_down = (now - last.get("ts", 0)) > cooldown_s
            if not same_as_last or cooled_down:
                log.info("MATCH for '%s': %s", label, runs)
                notify_discord(webhook, label, url, runs)
                state[key] = {"sig": sig, "ts": now}
            else:
                log.info("Match for '%s' unchanged, within cooldown — skipping notify", label)
        else:
            log.info("No qualifying seats yet for '%s'", label)
            state.pop(key, None)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--once", action="store_true", help="Run a single check and exit")
    parser.add_argument("--debug", metavar="SHOWTIME_LABEL", help="Dump screenshot+HTML for one showtime and exit")
    parser.add_argument("--headed", action="store_true", help="Show the actual browser window (use with --debug)")
    args = parser.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text())
    env_webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if env_webhook:
        cfg["discord_webhook_url"] = env_webhook
    state = load_state()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headed)
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            )
        )

        if args.debug:
            target = next((s for s in cfg["showtimes"] if s["label"] == args.debug), None)
            if not target:
                log.error("No showtime found with label %r", args.debug)
                return
            fetch_seat_map(page, target["url"], debug_tag="single")
            log.info("Debug dump complete.")
            if args.headed:
                input("Browser window open — inspect it, then press Enter here to close...")
            browser.close()
            return

        try:
            if args.once:
                check_once(page, cfg, state)
                save_state(state)
            else:
                while True:
                    check_once(page, cfg, state)
                    save_state(state)
                    time.sleep(cfg.get("poll_interval_seconds", 90))
        except KeyboardInterrupt:
            log.info("Stopped by user.")
        finally:
            browser.close()


if __name__ == "__main__":
    main()