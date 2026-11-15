#!/usr/bin/env python3
"""
SIN flight tracker.

Samples Google Flights fares out of Singapore with the free fast-flights
library, keeps a price history in data/prices.csv, builds a dashboard of the
cheapest fares per region (docs/index.html and DASHBOARD.md), and sends
Telegram alerts for deals plus a daily summary.

Designed to run every 8 hours on GitHub Actions (see .github/workflows), but
it runs anywhere with Python 3.11+.

Usage:
    python tracker.py                         search if due, update dashboard, send alerts
    python tracker.py --force                 search now, ignoring the schedule
    python tracker.py --dashboard             rebuild the dashboard from saved data only
    python tracker.py --status                show when it last ran
    python tracker.py --dry-run --limit 10    quick test: print messages, save nothing
"""

from __future__ import annotations

import sys

if sys.version_info < (3, 11):
    sys.exit("This script needs Python 3.11 or newer.")

import argparse
import csv
import html
import json
import os
import random
import re
import ssl
import statistics
import time
import tomllib
import traceback
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fast_flights import FlightQuery, FlightsNotFound, Passengers, create_query, get_flights

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.toml"
SECRETS_PATH = ROOT / "secrets.toml"
DATA_DIR = ROOT / "data"
PRICES_PATH = DATA_DIR / "prices.csv"
STATE_PATH = DATA_DIR / "state.json"
LOG_PATH = DATA_DIR / "tracker.log"
LOCK_PATH = DATA_DIR / ".lock"
DOCS_DIR = ROOT / "docs"
DASHBOARD_HTML = DOCS_DIR / "index.html"
DASHBOARD_MD = ROOT / "DASHBOARD.md"

SGT = timezone(timedelta(hours=8))
EPOCH = datetime.min.replace(tzinfo=timezone.utc)
FIELDS = ["checked_at", "region", "dest", "trip", "depart", "return", "price", "airlines", "stops"]
TELEGRAM_LIMIT = 4000  # Telegram's hard cap is 4096 characters per message
MAX_CONSECUTIVE_ERRORS = 6
LOCK_MAX_AGE_HOURS = 2  # a lock older than this is left over from a crash
LOG_MAX_BYTES = 2_000_000

AIRPORT_NAMES = {
    "SIN": "Singapore",
    "BKK": "Bangkok", "DMK": "Bangkok Don Mueang", "HKT": "Phuket", "CNX": "Chiang Mai",
    "SGN": "Ho Chi Minh City", "HAN": "Hanoi", "DAD": "Da Nang",
    "HKG": "Hong Kong", "TPE": "Taipei",
    "PEK": "Beijing", "PVG": "Shanghai", "CAN": "Guangzhou", "SZX": "Shenzhen",
    "HND": "Tokyo Haneda", "NRT": "Tokyo Narita", "KIX": "Osaka", "FUK": "Fukuoka", "CTS": "Sapporo",
    "ICN": "Seoul", "PUS": "Busan",
    "SYD": "Sydney", "MEL": "Melbourne", "BNE": "Brisbane", "PER": "Perth",
    "AKL": "Auckland", "CHC": "Christchurch",
    "LIS": "Lisbon", "OPO": "Porto", "MAD": "Madrid", "BCN": "Barcelona", "CDG": "Paris", "NCE": "Nice",
    "DUB": "Dublin", "AMS": "Amsterdam", "BRU": "Brussels", "FRA": "Frankfurt", "MUC": "Munich",
    "LUX": "Luxembourg", "FCO": "Rome", "MXP": "Milan", "ATH": "Athens", "ZAG": "Zagreb",
    "DBV": "Dubrovnik", "LJU": "Ljubljana", "VIE": "Vienna", "PRG": "Prague", "BUD": "Budapest",
    "CPH": "Copenhagen", "ARN": "Stockholm", "HEL": "Helsinki", "KEF": "Reykjavik", "OSL": "Oslo",
    "LHR": "London", "IST": "Istanbul",
    "LAX": "Los Angeles", "SFO": "San Francisco", "JFK": "New York", "SEA": "Seattle",
}

try:  # use certifi's certificates if available (fixes HTTPS on python.org builds for macOS)
    import certifi

    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CTX = ssl.create_default_context()

try:  # keep emoji from crashing older Windows consoles (no-op under pythonw)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def log(msg: str) -> None:
    """Print, and append to data/tracker.log (the scheduled runs have no console)."""
    try:
        print(msg, flush=True)
    except Exception:
        pass
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except OSError:
        pass


def trim_log() -> None:
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > LOG_MAX_BYTES:
            tail = LOG_PATH.read_bytes()[-LOG_MAX_BYTES // 2 :]
            LOG_PATH.write_bytes(tail[tail.find(b"\n") + 1 :])
    except OSError:
        pass


# --------------------------------------------------------------------------- data


@dataclass
class Obs:
    """One observed fare: the cheapest result for a route and set of dates."""

    checked_at: datetime
    region: str
    dest: str
    trip: str  # "ow" (one-way) or "rt" (round trip)
    depart: date
    ret: date | None
    price: int
    airlines: str
    stops: int

    @property
    def key(self) -> str:
        return f"{self.dest}|{self.trip}|{self.depart}|{self.ret or ''}"

    def to_row(self) -> dict:
        return {
            "checked_at": self.checked_at.isoformat(),
            "region": self.region,
            "dest": self.dest,
            "trip": self.trip,
            "depart": self.depart.isoformat(),
            "return": self.ret.isoformat() if self.ret else "",
            "price": self.price,
            "airlines": self.airlines,
            "stops": self.stops,
        }

    @classmethod
    def from_row(cls, r: dict) -> "Obs":
        return cls(
            checked_at=datetime.fromisoformat(r["checked_at"]),
            region=r["region"],
            dest=r["dest"],
            trip=r["trip"],
            depart=date.fromisoformat(r["depart"]),
            ret=date.fromisoformat(r["return"]) if r["return"] else None,
            price=int(r["price"]),
            airlines=r["airlines"],
            stops=int(r["stops"]),
        )


@dataclass
class Deal:
    obs: Obs
    reasons: list[str]
    score: float  # price relative to typical (or target); lower is better


TRIP_CODES = {"one-way": "ow", "oneway": "ow", "ow": "ow", "return": "rt", "round-trip": "rt", "rt": "rt"}
TRIP_NAMES = {"ow": "One-way", "rt": "Return"}


def region_trips(region: dict) -> list[str]:
    """Trip types searched for a region, from its `trips` setting (default: return only)."""
    codes = [TRIP_CODES[t.lower()] for t in region.get("trips", ["return"]) if t.lower() in TRIP_CODES]
    return list(dict.fromkeys(codes)) or ["rt"]


def week_key(dest: str, trip: str, d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{dest}|{trip}|{y}-W{w:02d}"


def load_config(path: Path = CONFIG_PATH) -> dict:
    """Load config.toml. Each region inherits any settings from its [groups.<name>] block."""
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    groups = cfg.get("groups", {})
    for rk, region in cfg.get("regions", {}).items():
        group = groups.get(region.get("group", ""), {})
        merged = {k: v for k, v in group.items() if k != "label"}
        merged.update(region)
        merged["group_label"] = group.get("label", "")
        cfg["regions"][rk] = merged
    names = [n for n in cfg["search"].get("exclude_airlines", []) if n.strip()]
    cfg["_exclude_re"] = (
        re.compile(r"\b(?:" + "|".join(re.escape(n.strip()) for n in names) + r")\b", re.IGNORECASE)
        if names
        else None
    )
    return cfg


def is_excluded(cfg: dict, airlines: str) -> bool:
    """True if any airline on the itinerary is on the exclude_airlines list."""
    rx = cfg.get("_exclude_re")
    return bool(rx and rx.search(airlines))


def load_history(path: Path = PRICES_PATH) -> list[Obs]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return [Obs.from_row(r) for r in csv.DictReader(f)]


def save_history(history: list[Obs], now: datetime, keep_days: int, path: Path = PRICES_PATH) -> None:
    cutoff = now - timedelta(days=keep_days)
    rows = sorted((o for o in history if o.checked_at >= cutoff), key=lambda o: o.checked_at)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(o.to_row() for o in rows)
    os.replace(tmp, path)  # atomic, so a crash can't leave a half-written file


def load_state(path: Path = STATE_PATH) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict, path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def acquire_lock() -> bool:
    """Stop two copies running at once (e.g. a manual run during a scheduled one)."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if LOCK_PATH.exists() and time.time() - LOCK_PATH.stat().st_mtime > LOCK_MAX_AGE_HOURS * 3600:
        LOCK_PATH.unlink(missing_ok=True)
    try:
        fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)
    return True


def release_lock() -> None:
    LOCK_PATH.unlink(missing_ok=True)


def get_secret(name: str) -> str:
    """Read from an environment variable first, then from secrets.toml."""
    value = os.environ.get(name, "").strip()
    if value:
        return value
    if SECRETS_PATH.exists():
        with open(SECRETS_PATH, "rb") as f:
            return str(tomllib.load(f).get(name.lower(), "")).strip()
    return ""


# ----------------------------------------------------------------- google flights


def build_query(cfg: dict, dest: str, depart: date, ret: date | None):
    s = cfg["search"]
    legs = [FlightQuery(date=depart.isoformat(), from_airport=s["origin"], to_airport=dest)]
    if ret:
        legs.append(FlightQuery(date=ret.isoformat(), from_airport=dest, to_airport=s["origin"]))
    return create_query(
        flights=legs,
        trip="round-trip" if ret else "one-way",
        seat=s.get("cabin", "economy"),
        passengers=Passengers(adults=int(s.get("adults", 1))),
        currency=s.get("currency", "SGD"),
        language="en",
        max_stops=s.get("max_stops"),
        hide_separate_and_self_transfer=bool(s.get("hide_self_transfer", True)),
    )


def flights_url(query) -> str:
    return "https://www.google.com/travel/flights/search?" + urllib.parse.urlencode(query.params())


def search_cheapest(cfg: dict, dest: str, depart: date, ret: date | None):
    """Return (price, airlines, stops) of the cheapest result, or None if there are no flights.

    For round trips, Google shows the total round-trip price on the outbound list.
    Network or parsing problems raise, so the caller can spot rate-limiting.
    """
    try:
        results = get_flights(build_query(cfg, dest, depart, ret))
    except FlightsNotFound:
        return None
    priced = [
        f for f in results
        if isinstance(f.price, int) and f.price > 0 and not is_excluded(cfg, ", ".join(f.airlines))
    ]
    if not priced:
        return None  # no flights, or only ones on excluded airlines
    best = min(priced, key=lambda f: f.price)
    return best.price, ", ".join(best.airlines), max(len(best.flights) - 1, 0)


# ----------------------------------------------------------------------- planning


def plan_searches(
    cfg: dict, history: list[Obs], checked: dict, now: datetime, today: date, rng: random.Random
) -> list[tuple]:
    """Pick which (destination, dates) to search this run.

    Departure dates are grouped into calendar weeks. Each run explores the weeks
    that have gone longest without a check for that destination (including
    checks that found no flights), so the whole window gets covered over time.
    Optionally, one search per destination re-checks the cheapest dates found recently.
    """
    s = cfg["search"]
    first = today + timedelta(days=int(s["days_ahead_min"]))
    last = today + timedelta(days=int(s["days_ahead_max"]))
    weeks: dict[tuple, list[date]] = defaultdict(list)
    d = first
    while d <= last:
        weeks[d.isocalendar()[:2]].append(d)
        d += timedelta(days=1)

    last_checked: dict[str, datetime] = {k: datetime.fromisoformat(v) for k, v in checked.items()}
    latest: dict[str, Obs] = {}
    recent = now - timedelta(days=7)
    for o in history:
        wk = week_key(o.dest, o.trip, o.depart)
        if o.checked_at > last_checked.get(wk, EPOCH):
            last_checked[wk] = o.checked_at
        if o.checked_at >= recent and first <= o.depart <= last:
            if o.key not in latest or o.checked_at > latest[o.key].checked_at:
                latest[o.key] = o
    best: dict[tuple, Obs] = {}
    for o in latest.values():
        k = (o.dest, o.trip)
        if k not in best or o.price < best[k].price:
            best[k] = o

    plan = []
    for region_key, region in cfg["regions"].items():
        lengths = region.get("trip_lengths") or [7]
        for dest in region["airports"]:
            samples = {
                "ow": int(region.get("one_way_samples", s.get("one_way_samples", 3))),
                "rt": int(region.get("round_trip_samples", s.get("round_trip_samples", 3))),
            }
            for trip in region_trips(region):
                n = samples[trip]
                picks: list[tuple] = []
                if n >= 2 and s.get("recheck_best", True) and (dest, trip) in best:
                    b = best[(dest, trip)]
                    picks.append((b.depart, b.ret))
                order = sorted(
                    weeks,
                    key=lambda w: (last_checked.get(week_key(dest, trip, weeks[w][0]), EPOCH), rng.random()),
                )
                for w in order:
                    if len(picks) >= n:
                        break
                    dep = rng.choice(weeks[w])
                    ret = dep + timedelta(days=rng.choice(lengths)) if trip == "rt" else None
                    if (dep, ret) not in picks:
                        picks.append((dep, ret))
                plan.extend((region_key, dest, trip, dep, ret) for dep, ret in picks)
    rng.shuffle(plan)  # if Google cuts us off midway, coverage is still spread out
    return plan


def run_searches(cfg: dict, plan: list[tuple], now: datetime, search_fn=None, sleep=time.sleep):
    """Run the planned searches. Returns (fares found, searches completed, errors, stopped early)."""
    search_fn = search_fn or search_cheapest
    lo, hi = cfg["search"].get("delay_seconds", [2.0, 5.0])
    found: list[Obs] = []
    completed: list[tuple] = []  # (dest, trip, depart) for searches that got an answer
    errors = consecutive = 0
    for i, (region, dest, trip, dep, ret) in enumerate(plan, 1):
        if i > 1:
            sleep(random.uniform(lo, hi))
        label = f"[{i}/{len(plan)}] {dest} {'RT' if ret else 'OW'} {dep}{' to ' + str(ret) if ret else ''}"
        try:
            result = search_fn(cfg, dest, dep, ret)
        except Exception as e:  # network error, captcha page, layout change...
            errors += 1
            consecutive += 1
            log(f"{label}: ERROR {type(e).__name__}: {e}"[:300])
            if consecutive >= MAX_CONSECUTIVE_ERRORS:
                log("Too many errors in a row; Google may be rate-limiting. Stopping this run early.")
                return found, completed, errors, True
            continue
        consecutive = 0
        completed.append((dest, trip, dep))
        if result is None:
            log(f"{label}: no flights")
            continue
        price, airlines, stops = result
        found.append(Obs(now, region, dest, trip, dep, ret, int(price), airlines, int(stops)))
        log(f"{label}: {price} | {airlines} | {stops} stop(s)")
    return found, completed, errors, False


# ------------------------------------------------------------------ deal detection


def prepare_history(cfg: dict, history: list[Obs], state: dict) -> tuple[list[Obs], int]:
    """Apply the current settings to saved fares.

    Drops fares on airlines now excluded, and files each fare under the region
    its airport belongs to today. If the airline list changed, forgets which
    weeks were already searched, so weeks that only had excluded fares get
    searched again soon.
    """
    airport_region = {a: rk for rk, r in cfg["regions"].items() for a in r["airports"]}
    kept = []
    for o in history:
        if is_excluded(cfg, o.airlines):
            continue
        o.region = airport_region.get(o.dest, o.region)
        kept.append(o)
    signature = "|".join(sorted(n.strip().lower() for n in cfg["search"].get("exclude_airlines", [])))
    if state.get("airline_filter") != signature:
        state["checked"] = {}
        state["airline_filter"] = signature
    return kept, len(history) - len(kept)


def baselines(history: list[Obs], now: datetime, days: int, min_samples: int) -> dict[tuple, float]:
    """Typical (median) price per (destination, trip type) over the last `days` days."""
    cutoff = now - timedelta(days=days)
    latest: dict[str, Obs] = {}
    for o in history:
        if o.checked_at >= cutoff and (o.key not in latest or o.checked_at > latest[o.key].checked_at):
            latest[o.key] = o  # re-checks of the same dates count once
    groups: dict[tuple, list[int]] = defaultdict(list)
    for o in latest.values():
        groups[(o.dest, o.trip)].append(o.price)
    return {k: statistics.median(v) for k, v in groups.items() if len(v) >= min_samples}


def find_deals(cfg: dict, new_obs: list[Obs], base: dict, alerted: dict) -> list[Deal]:
    a = cfg["alerts"]
    drop = a.get("drop_pct", 25) / 100
    redrop = a.get("realert_drop_pct", 5) / 100
    deals = []
    for o in new_obs:
        region = cfg["regions"][o.region]
        target = region.get("one_way_target" if o.trip == "ow" else "round_trip_target", 0) or 0
        typical = base.get((o.dest, o.trip))
        reasons = []
        if typical and o.price <= typical * (1 - drop):
            reasons.append(f"{round((1 - o.price / typical) * 100)}% below the typical {money(cfg, typical)}")
        if target and o.price <= target:
            reasons.append(f"under your {money(cfg, target)} target")
        if not reasons:
            continue
        prev = alerted.get(o.key)
        if prev is not None and o.price > prev * (1 - redrop):
            continue  # already alerted at about this price
        score = o.price / typical if typical else o.price / target
        deals.append(Deal(o, reasons, score))
    deals.sort(key=lambda x: x.score)
    return deals[: int(a.get("max_alerts_per_run", 8))]


# ------------------------------------------------------------------------ messages


def esc(text) -> str:
    return html.escape(str(text), quote=True)


def money(cfg: dict, amount: float) -> str:
    cur = cfg["search"].get("currency", "SGD")
    return f"{'S$' if cur == 'SGD' else cur + ' '}{amount:,.0f}"


def place(cfg: dict, code: str) -> str:
    name = {**AIRPORT_NAMES, **cfg.get("names", {})}.get(code)
    return f"{name} ({code})" if name else code


def fmt_date(d: date, today: date) -> str:
    return f"{d:%a} {d.day} {d:%b}" + (f" {d.year}" if d.year != today.year else "")


def fmt_dates(o: Obs, today: date) -> str:
    if not o.ret:
        return fmt_date(o.depart, today)
    if (o.depart.year, o.depart.month) == (o.ret.year, o.ret.month):
        span = f"{o.depart.day}–{o.ret.day} {o.ret:%b}"
    else:
        span = f"{o.depart.day} {o.depart:%b} – {o.ret.day} {o.ret:%b}"
    if o.ret.year != today.year:
        span += f" {o.ret.year}"
    return f"{span} ({(o.ret - o.depart).days}d)"


def stops_text(n: int) -> str:
    return "direct" if n == 0 else f"{n} stop" + ("s" if n > 1 else "")


def link(cfg: dict, o: Obs, text: str) -> str:
    url = flights_url(build_query(cfg, o.dest, o.depart, o.ret))
    return f'<a href="{esc(url)}">{esc(text)}</a>'


def format_deals(cfg: dict, deals: list[Deal], today: date) -> str:
    origin = cfg["search"]["origin"]
    blocks = []
    for d in deals:
        o = d.obs
        kind = "Round trip" if o.ret else "One-way"
        blocks.append(
            f"🔥 <b>{esc(origin)} → {esc(place(cfg, o.dest))}</b>\n"
            f"{kind} · {fmt_dates(o, today)}\n"
            f"<b>{money(cfg, o.price)}</b> · {esc(o.airlines)} · {stops_text(o.stops)}\n"
            f"<i>{esc(' · '.join(d.reasons))}</i>\n"
            f"{link(cfg, o, 'Open in Google Flights')}"
        )
    return "\n\n".join(blocks)


def region_groups(cfg: dict) -> list[tuple[str, list[str]]]:
    """[(group label, [region keys])] in config order."""
    out: dict[str, list[str]] = {}
    for rk, region in cfg["regions"].items():
        out.setdefault(region.get("group_label", ""), []).append(rk)
    return list(out.items())


def dest_label(cfg: dict, o: Obs) -> str:
    """'Japan, Osaka' or just 'Hong Kong' when the city and region share a name."""
    region = cfg["regions"].get(o.region, {}).get("label", o.region)
    city = {**AIRPORT_NAMES, **cfg.get("names", {})}.get(o.dest, o.dest)
    return region if city == region else f"{region}, {city}"


def format_digest(cfg: dict, history: list[Obs], now: datetime, today: date, base: dict, run_info: str) -> str:
    hours = int(cfg["alerts"].get("digest_lookback_hours", 72))
    cutoff = now - timedelta(hours=hours)
    latest: dict[str, Obs] = {}
    for o in history:
        region = cfg["regions"].get(o.region)
        if not region or o.dest not in region["airports"] or o.trip not in region_trips(region):
            continue
        if o.checked_at >= cutoff and o.depart > today:
            if o.key not in latest or o.checked_at > latest[o.key].checked_at:
                latest[o.key] = o
    best: dict[tuple, Obs] = {}
    for o in latest.values():
        k = (o.region, o.trip)
        if k not in best or o.price < best[k].price:
            best[k] = o

    def line(o: Obs, extra: str = "") -> str:
        return (
            f"{link(cfg, o, money(cfg, o.price))} {esc(dest_label(cfg, o))} · {fmt_dates(o, today)} · "
            f"{esc(o.airlines)}{extra}"
        )

    s = cfg["search"]
    blocks = [
        f"☀️ <b>Cheapest fares from {esc(s['origin'])}</b>\n"
        f"<i>Seen in the last {hours}h · {esc(s.get('cabin', 'economy'))} · tap a price to open it</i>"
    ]
    for label, keys in region_groups(cfg):
        rows = sorted((best[(k, "rt")] for k in keys if (k, "rt") in best), key=lambda o: o.price)
        if rows:
            title = "Return" + (f", {label}" if label else "")
            blocks.append("\n".join([f"<b>{esc(title)}</b>"] + [line(o) for o in rows]))
    one_way = sorted((o for (k, t), o in best.items() if t == "ow"), key=lambda o: o.price)[:5]
    if one_way:
        blocks.append("\n".join(["<b>One-way, cheapest 5</b>"] + [line(o) for o in one_way]))

    drops = sorted(
        ((o.price / base[(o.dest, o.trip)], o) for o in latest.values() if (o.dest, o.trip) in base),
        key=lambda x: x[0],
    )
    drops = [(r, o) for r, o in drops if r <= 0.85][:5]
    if drops:
        lines = ["📉 <b>Furthest below typical</b>"]
        for r, o in drops:
            kind = "return" if o.ret else "one-way"
            lines.append(line(o, f" · {kind}, {round((1 - r) * 100)}% below typical"))
        blocks.append("\n".join(lines))

    if len(blocks) == 1:
        blocks.append("No fares in the last few days yet. They'll appear after the next few runs.")
    url = cfg.get("dashboard", {}).get("url", "").strip()
    if url:
        blocks.append(f'<a href="{esc(url)}">Open the dashboard</a> for the top 3 fares per destination')
    blocks.append(f"<i>{esc(run_info)}</i>")
    return "\n\n".join(blocks)


# ----------------------------------------------------------------------- dashboard

DASHBOARD_CSS = """
:root {
  --paper: #f2f4f8; --surface: #ffffff; --ink: #17223b; --muted: #5b6477; --line: #d9dee8;
  --accent: #5b2e91; --accent-soft: #efe8f8; --deal: #16704e; --deal-soft: #e2f2ea;
  --display: "Barlow Condensed", "Arial Narrow", "Roboto Condensed", system-ui, sans-serif;
  --text: "Barlow", system-ui, -apple-system, "Segoe UI", sans-serif;
  color-scheme: light;
}
@media (prefers-color-scheme: dark) {
  :root {
    --paper: #0f1420; --surface: #161d2c; --ink: #e6e9f1; --muted: #98a1b4; --line: #273146;
    --accent: #c4a7f2; --accent-soft: #2a2242; --deal: #74d3a8; --deal-soft: #15302a;
    color-scheme: dark;
  }
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0; background: var(--paper); color: var(--ink); font: 400 1rem/1.5 var(--text);
  padding: env(safe-area-inset-top, 0px) env(safe-area-inset-right, 0px)
           env(safe-area-inset-bottom, 0px) env(safe-area-inset-left, 0px);
}
.top, nav, main, footer { max-width: 56rem; margin: 0 auto; padding: 0 1.25rem; }
.top { padding-top: 2.5rem; }
h1 { font: 700 clamp(2.75rem, 9vw, 4.5rem)/0.95 var(--display); letter-spacing: -0.01em; margin: 0; }
.updated { color: var(--muted); margin: .6rem 0 0; max-width: 40rem; }
nav { margin-top: 2rem; }
.nav-title { font-weight: 600; margin: 0 0 .75rem; }
.index { list-style: none; margin: 0; padding: 0; display: grid;
         grid-template-columns: repeat(auto-fill, minmax(9rem, 1fr)); gap: .5rem; }
.index a { display: flex; flex-direction: column; height: 100%; padding: .6rem .75rem;
           border: 1px solid var(--line); border-radius: .5rem; background: var(--surface);
           color: inherit; text-decoration: none; }
.index a:hover { border-color: var(--accent); }
.chip-name { font-weight: 500; }
.chip-price { font: 700 1.4rem/1.1 var(--display); color: var(--accent); margin-top: .1rem; }
.chip-kind { font-size: .8rem; color: var(--muted); }
.group-title { font-size: .9rem; color: var(--muted); margin: 1.1rem 0 .45rem; }
.nav-title + .group-title { margin-top: 0; }
section { margin-top: 3rem; scroll-margin-top: 1rem; }
h2 { font: 700 2rem/1.1 var(--display); margin: 0; padding-bottom: .35rem; border-bottom: 3px solid var(--ink); }
h3 { font-weight: 600; font-size: 1rem; color: var(--muted); margin: 1.25rem 0 0; }
.fares { list-style: none; margin: 0; padding: 0; }
.fares li + li { border-top: 1px solid var(--line); }
.fare { display: grid; grid-template-columns: 6.25rem 1fr auto; gap: 1rem; align-items: center;
        padding: .9rem .25rem; color: inherit; text-decoration: none; }
.fare:hover .price { text-decoration: underline; text-underline-offset: .15em; }
.code { font: 700 2.4rem/1 var(--display); }
.info { display: flex; flex-direction: column; min-width: 0; }
.city { font-weight: 600; }
.when, .how { color: var(--muted); font-size: .92rem; }
.nights { white-space: nowrap; margin-left: .35rem; padding: 0 .4rem; border-radius: .25rem;
          background: var(--accent-soft); color: var(--ink); font-size: .8rem; }
.cost { display: flex; flex-direction: column; align-items: flex-end; text-align: right; }
.price { font: 700 1.9rem/1 var(--display); color: var(--accent); }
.deal { margin-top: .3rem; padding: .05rem .45rem; border-radius: .25rem; background: var(--deal-soft);
        color: var(--deal); font-size: .8rem; font-weight: 600; white-space: nowrap; }
.age { margin-top: .25rem; color: var(--muted); font-size: .78rem; white-space: nowrap; }
.empty { color: var(--muted); margin: .75rem 0; }
details { margin-top: .5rem; }
summary { cursor: pointer; color: var(--accent); font-weight: 500; padding: .35rem 0; }
.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: .92rem; }
th { text-align: left; font-weight: 600; color: var(--muted); }
th, td { padding: .45rem .75rem .45rem 0; border-bottom: 1px solid var(--line); white-space: nowrap; }
.num { text-align: right; padding-right: 0; }
td a { color: var(--accent); font-weight: 600; }
.muted { color: var(--muted); }
footer { margin: 3.5rem auto 2.5rem; color: var(--muted); font-size: .88rem; }
a:focus-visible { outline: 3px solid var(--accent); outline-offset: 2px; border-radius: .25rem; }
@media (max-width: 34rem) {
  .fare { grid-template-columns: 3.6rem 1fr auto; gap: .7rem; }
  .code { font-size: 1.55rem; }
  .price { font-size: 1.5rem; }
}
"""


def ago(now: datetime, t: datetime) -> str:
    h = (now - t).total_seconds() / 3600
    if h < 1:
        return "just now"
    return f"{int(h)}h ago" if h < 48 else f"{int(h // 24)}d ago"


def long_dates(o: Obs, today: date) -> tuple[str, str]:
    """('Sat 9 Jan – Sat 16 Jan 2027', '7 nights') for returns, ('Sat 9 Jan 2027', '') for one-ways."""
    if not o.ret:
        return fmt_date(o.depart, today), ""
    dep = f"{o.depart:%a} {o.depart.day} {o.depart:%b}"
    if o.depart.year != o.ret.year:
        dep += f" {o.depart.year}"
    nights = (o.ret - o.depart).days
    return f"{dep} – {fmt_date(o.ret, today)}", f"{nights} night{'s' if nights != 1 else ''}"


def vs_typical(o: Obs, base: dict) -> int | None:
    typical = base.get((o.dest, o.trip))
    if not typical:
        return None
    pct = round((1 - o.price / typical) * 100)
    return pct if pct >= 5 else None


def dashboard_data(cfg: dict, history: list[Obs], now: datetime, today: date) -> dict:
    """Top fares per region and trip type, from fares seen in the last `lookback_days` days."""
    d = cfg.get("dashboard", {})
    top_n, days = int(d.get("top_n", 3)), int(d.get("lookback_days", 7))
    cutoff = now - timedelta(days=days)
    latest: dict[str, Obs] = {}
    for o in history:
        if o.checked_at >= cutoff and o.depart > today:
            if o.key not in latest or o.checked_at > latest[o.key].checked_at:
                latest[o.key] = o  # a later re-check of the same dates replaces the older price
    regions = []
    for rk, region in cfg["regions"].items():
        airports = set(region["airports"])
        groups = []
        for trip in region_trips(region):
            fares = sorted(
                (o for o in latest.values() if o.region == rk and o.trip == trip and o.dest in airports),
                key=lambda o: (o.price, o.depart),
            )
            by_city: dict[str, Obs] = {}
            for o in fares:
                by_city.setdefault(o.dest, o)
            groups.append({"trip": trip, "top": fares[:top_n], "by_city": list(by_city.values())})
        regions.append({"key": rk, "label": region.get("label", rk), "group": region.get("group_label", ""), "groups": groups})
    return {"regions": regions, "lookback_days": days}


def _index_groups(data: dict) -> list[tuple[str, list[tuple]]]:
    """[(group label, entries)], where each entry is (region, trip group shown, cheapest fare or None),
    cheapest first within each group. Groups keep their config order."""
    grouped: dict[str, list[tuple]] = {}
    for r in data["regions"]:
        g = next((g for g in r["groups"] if g["trip"] == "rt"), r["groups"][0])
        grouped.setdefault(r["group"], []).append((r, g, g["top"][0] if g["top"] else None))
    for entries in grouped.values():
        entries.sort(key=lambda x: x[2].price if x[2] else float("inf"))
    return list(grouped.items())


def render_html(cfg: dict, data: dict, now: datetime, today: date, base: dict) -> str:
    s = cfg["search"]
    origin, days = esc(s["origin"]), data["lookback_days"]
    local = now.astimezone(SGT)
    names = {**AIRPORT_NAMES, **cfg.get("names", {})}

    def url(o: Obs) -> str:
        return esc(flights_url(build_query(cfg, o.dest, o.depart, o.ret)))

    index_groups = _index_groups(data)
    nav = []
    for group_label, entries in index_groups:
        chips = []
        for r, g, best in entries:
            inner = (
                f'<span class="chip-price">{money(cfg, best.price)}</span>'
                f'<span class="chip-kind">{TRIP_NAMES[g["trip"]].lower()}, {esc(names.get(best.dest, best.dest))}</span>'
                if best
                else '<span class="chip-kind">no fares yet</span>'
            )
            chips.append(f'<li><a href="#{esc(r["key"])}"><span class="chip-name">{esc(r["label"])}</span>{inner}</a></li>')
        if group_label and len(index_groups) > 1:
            nav.append(f'<p class="group-title">{esc(group_label)}</p>')
        nav.append(f'<ul class="index">{"".join(chips)}</ul>')

    sections = []
    for r in data["regions"]:
        parts = [f'<section id="{esc(r["key"])}"><h2>{esc(r["label"])}</h2>']
        multi = len(r["groups"]) > 1
        for g in r["groups"]:
            kind = TRIP_NAMES[g["trip"]]
            if multi:
                parts.append(f"<h3>{kind}</h3>")
            if not g["top"]:
                parts.append(
                    f'<p class="empty">No {kind.lower()} fares seen in the last {days} days yet. '
                    "They show up after the next few runs.</p>"
                )
                continue
            rows = []
            for o in g["top"]:
                when, nights = long_dates(o, today)
                pct = vs_typical(o, base)
                rows.append(
                    f'<li><a class="fare" href="{url(o)}" target="_blank" rel="noopener">'
                    f'<span class="code">{esc(o.dest)}</span>'
                    f'<span class="info"><span class="city">{esc(names.get(o.dest, o.dest))}</span>'
                    f'<span class="when">{esc(when)}'
                    + (f'<span class="nights">{nights}</span>' if nights else "")
                    + f'</span><span class="how">{esc(o.airlines)}, {stops_text(o.stops)}</span></span>'
                    f'<span class="cost"><span class="price">{money(cfg, o.price)}</span>'
                    + (f'<span class="deal">{pct}% below typical</span>' if pct else "")
                    + f'<span class="age">checked {ago(now, o.checked_at)}</span></span></a></li>'
                )
            parts.append(f'<ol class="fares">{"".join(rows)}</ol>')
            if len(g["by_city"]) > 1:
                trs = "".join(
                    f'<tr><td>{esc(names.get(o.dest, o.dest))} <span class="muted">{esc(o.dest)}</span></td>'
                    f"<td>{esc(long_dates(o, today)[0])}</td>"
                    f'<td class="num"><a href="{url(o)}" target="_blank" rel="noopener">{money(cfg, o.price)}</a></td></tr>'
                    for o in g["by_city"]
                )
                parts.append(
                    f'<details><summary>Cheapest in each city ({len(g["by_city"])})</summary>'
                    '<div class="table-wrap"><table><thead><tr><th scope="col">City</th>'
                    '<th scope="col">Dates</th><th scope="col" class="num">Price</th></tr></thead>'
                    f"<tbody>{trs}</tbody></table></div></details>"
                )
        parts.append("</section>")
        sections.append("".join(parts))

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Cheap fares from {origin}</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>✈️</text></svg>">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Barlow:wght@400;500;600&family=Barlow+Condensed:wght@600;700&display=swap" rel="stylesheet">
<style>{DASHBOARD_CSS}</style>
</head>
<body>
<header class="top">
<h1>Departing {origin}</h1>
<p class="updated">Updated {local:%a} {local.day} {local:%b %Y}, {local:%H:%M} Singapore time. Refreshes every 8 hours.</p>
</header>
<nav aria-label="Regions, cheapest first">
<p class="nav-title">Where it’s cheapest right now</p>
{"".join(nav)}
</nav>
<main>{"".join(sections)}</main>
<footer><p>Fares from Google Flights for {s.get("adults", 1)} adult in {esc(s.get("cabin", "economy"))}, seen in the last {days} days. Tap a fare to open it. Budget airlines may not include bags, so confirm the price before booking.</p></footer>
</body>
</html>
"""


def render_md(cfg: dict, data: dict, now: datetime, today: date, base: dict) -> str:
    s = cfg["search"]
    local = now.astimezone(SGT)
    names = {**AIRPORT_NAMES, **cfg.get("names", {})}

    def cell(text) -> str:
        return str(text).replace("|", "\\|")

    def url(o: Obs) -> str:
        return flights_url(build_query(cfg, o.dest, o.depart, o.ret))

    out = [
        f"# Cheap fares from {s['origin']}",
        "",
        f"Updated {local:%a} {local.day} {local:%b %Y}, {local:%H:%M} Singapore time. Refreshes every 8 hours. "
        f"Fares for {s.get('adults', 1)} adult in {s.get('cabin', 'economy')}, seen in the last "
        f"{data['lookback_days']} days. Click a price to open it in Google Flights.",
        "",
    ]
    for group_label, entries in _index_groups(data):
        out += [
            f"**{group_label or 'Cheapest by destination'}:** "
            + " · ".join(
                f"[{r['label']}](#{r['label'].lower().replace(' ', '-')}) "
                + (f"{money(cfg, best.price)} {TRIP_NAMES[g['trip']].lower()}" if best else "no fares yet")
                for r, g, best in entries
            ),
            "",
        ]
    for r in data["regions"]:
        out += [f"## {r['label']}", ""]
        multi = len(r["groups"]) > 1
        for g in r["groups"]:
            if multi:
                out += [f"### {TRIP_NAMES[g['trip']]}", ""]
            if not g["top"]:
                out += [f"_No fares seen in the last {data['lookback_days']} days yet._", ""]
                continue
            out += ["| | Price | Destination | Dates | Airline | vs typical | Checked |", "|---|---|---|---|---|---|---|"]
            for i, o in enumerate(g["top"], 1):
                when, nights = long_dates(o, today)
                pct = vs_typical(o, base)
                out.append(
                    f"| {i} | [**{money(cfg, o.price)}**]({url(o)}) | {cell(names.get(o.dest, o.dest))} ({o.dest}) | "
                    f"{cell(when)}{f' ({nights})' if nights else ''} | {cell(o.airlines)}, {stops_text(o.stops)} | "
                    f"{f'{pct}% below' if pct else ''} | {ago(now, o.checked_at)} |"
                )
            out.append("")
            if len(g["by_city"]) > 1:
                out += ["<details><summary>Cheapest in each city</summary>", "", "| City | Dates | Price |", "|---|---|---|"]
                out += [
                    f"| {cell(names.get(o.dest, o.dest))} ({o.dest}) | {cell(long_dates(o, today)[0])} | "
                    f"[{money(cfg, o.price)}]({url(o)}) |"
                    for o in g["by_city"]
                ]
                out += ["", "</details>", ""]
    return "\n".join(out) + "\n"


def write_dashboards(cfg: dict, history: list[Obs], now: datetime, today: date, base: dict) -> None:
    data = dashboard_data(cfg, history, now, today)
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    (DOCS_DIR / ".nojekyll").write_text("", encoding="utf-8")  # serve the page as-is on GitHub Pages
    DASHBOARD_HTML.write_text(render_html(cfg, data, now, today, base), encoding="utf-8")
    DASHBOARD_MD.write_text(render_md(cfg, data, now, today, base), encoding="utf-8")


# ------------------------------------------------------------------------ telegram


def telegram_api(method: str, payload: dict | None = None) -> dict:
    token = get_secret("TELEGRAM_BOT_TOKEN")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise RuntimeError(f"Telegram {method} failed ({e.code}): {detail}") from None


def visible_len(text: str) -> int:
    """Length as Telegram counts it: without HTML tags (so link addresses don't count)."""
    return len(html.unescape(re.sub(r"<[^>]+>", "", text)))


def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    chunks, cur = [], ""
    for block in text.split("\n\n"):
        candidate = f"{cur}\n\n{block}" if cur else block
        if visible_len(candidate) <= limit:
            cur = candidate
            continue
        if cur:
            chunks.append(cur)
        while visible_len(block) > limit:  # a single oversized block: split on line breaks
            cut = block.rfind("\n", 0, limit)
            cut = cut if cut > 0 else limit
            chunks.append(block[:cut])
            block = block[cut:].lstrip("\n")
        cur = block
    if cur:
        chunks.append(cur)
    return chunks


def send_message(text: str, dry_run: bool = False) -> bool:
    """Send to Telegram. Returns True if sent; prints instead when dry-running or not configured."""
    chat = get_secret("TELEGRAM_CHAT_ID")
    if dry_run or not get_secret("TELEGRAM_BOT_TOKEN") or not chat:
        log("\n" + "=" * 18 + " Telegram message (not sent) " + "=" * 18 + "\n" + text + "\n")
        return False
    for chunk in split_message(text):
        telegram_api(
            "sendMessage",
            {"chat_id": chat, "text": chunk, "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}},
        )
    return True


def recent_chats() -> dict:
    chats = {}
    for u in telegram_api("getUpdates").get("result", []):
        msg = u.get("message") or u.get("channel_post") or {}
        c = msg.get("chat")
        if c:
            chats[c["id"]] = c.get("username") or c.get("title") or c.get("first_name") or ""
    return chats


def setup_telegram() -> int:
    print("Telegram setup\n")
    print("1. In Telegram, open a chat with @BotFather, send /newbot and follow the prompts.")
    token = input("2. Paste the token BotFather gives you, then press Enter: ").strip()
    if not token:
        print("No token entered. Run this again when you have it.")
        return 1
    os.environ["TELEGRAM_BOT_TOKEN"] = token
    try:
        bot = telegram_api("getMe")["result"]["username"]
    except Exception as e:
        print(f"That token didn't work: {e}")
        return 1
    print(f"   Found your bot: @{bot}")
    input(f"3. In Telegram, open a chat with @{bot}, send it any message (e.g. hi), then press Enter here...")
    chats = {}
    for _ in range(3):
        chats = recent_chats()
        if chats:
            break
        time.sleep(3)
    if not chats:
        print("   I couldn't see your message yet. Send the bot a message and run this setup again.")
        return 1
    chat_id = list(chats)[-1]
    SECRETS_PATH.write_text(
        "# Telegram credentials for the flight tracker. Keep this file private.\n"
        f'telegram_bot_token = "{token}"\n'
        f'telegram_chat_id = "{chat_id}"\n',
        encoding="utf-8",
    )
    os.environ["TELEGRAM_CHAT_ID"] = str(chat_id)
    send_message("✅ Flight tracker connected. Deal alerts and a daily summary will arrive here.")
    print(f"   Saved to {SECRETS_PATH.name} and sent you a test message. Done.")
    return 0


# ---------------------------------------------------------------------------- main


def hours_since(iso: str | None, now: datetime) -> float:
    return (now - datetime.fromisoformat(iso)).total_seconds() / 3600 if iso else float("inf")


def show_status() -> int:
    cfg = load_config()
    state = load_state()
    now = datetime.now(timezone.utc)
    gap = float(cfg.get("schedule", {}).get("min_hours_between_runs", 7))
    last = state.get("last_run")
    print(f"Last search run:  {state.get('last_run_summary', 'never')}")
    if last:
        due = datetime.fromisoformat(last) + timedelta(hours=gap)
        when = "at the next hourly check" if due <= now else f"after {due.astimezone(SGT):%a %d %b %H:%M} SGT"
        print(f"Next search:      {when}")
    print(f"Last daily summary: {state.get('last_digest', 'never')}")
    print(f"Fares in history: {len(load_history())}")
    print(f"Telegram set up:  {'yes' if get_secret('TELEGRAM_BOT_TOKEN') and get_secret('TELEGRAM_CHAT_ID') else 'no'}")
    print(f"Log file:         {LOG_PATH}")
    return 0


def run(args, now: datetime | None = None) -> int:
    if not acquire_lock():
        print("Another copy of the tracker is running; skipping this check.")
        return 0
    try:
        return _run(args, now)
    finally:
        release_lock()


def _run(args, now: datetime | None) -> int:
    cfg = load_config()
    a = cfg["alerts"]
    now = now or datetime.now(timezone.utc).replace(microsecond=0)
    local = now.astimezone(SGT)
    today = local.date()
    this_week = week_key("", "", today).split("|")[2]

    state = load_state()
    alerted = {k: v for k, v in state.get("alerted", {}).items() if date.fromisoformat(k.split("|")[2]) >= today}
    checked = {k: v for k, v in state.get("checked", {}).items() if k.split("|")[2] >= this_week}
    state["alerted"], state["checked"] = alerted, checked

    gap = float(cfg.get("schedule", {}).get("min_hours_between_runs", 7))
    search_due = args.force or args.dry_run or hours_since(state.get("last_run"), now) >= gap
    digest_due = args.force_digest or (
        state.get("last_digest") != today.isoformat() and local.hour >= int(a.get("digest_hour_sgt", 8))
    )
    if not search_due and not digest_due:
        print(f"Nothing due yet (last search {hours_since(state.get('last_run'), now):.1f}h ago).")
        return 0

    history, dropped = prepare_history(cfg, load_history(), state)
    checked = state["checked"]  # may have been reset by prepare_history
    if dropped:
        log(f"Removed {dropped} saved fares on excluded airlines.")
    base = baselines(history, now, int(a.get("baseline_days", 30)), int(a.get("min_samples", 15)))
    telegram_ok = True

    def notify(text: str) -> bool:
        nonlocal telegram_ok
        try:
            return send_message(text, args.dry_run)
        except Exception as e:
            telegram_ok = False
            log(f"Could not send Telegram message: {e}")
            return False

    all_failed = False
    if search_due:
        trim_log()
        plan = plan_searches(cfg, history, checked, now, today, random.Random())
        if args.limit:
            plan = plan[: args.limit]
        log(f"\n=== {local:%Y-%m-%d %H:%M} SGT · {len(plan)} searches planned · {len(history)} fares in history ===")
        sleep = (lambda _s: None) if args.no_delay else time.sleep
        found, completed, errors, stopped = run_searches(cfg, plan, now, sleep=sleep)
        log(f"Done: {len(found)} fares found, {errors} errors" + (" (stopped early)" if stopped else ""))
        history.extend(found)
        for dest, trip, dep in completed:
            checked[week_key(dest, trip, dep)] = now.isoformat()

        deals = find_deals(cfg, found, base, alerted)
        if deals and notify(format_deals(cfg, deals, today)):
            for d in deals:
                alerted[d.obs.key] = d.obs.price

        all_failed = bool(plan) and not found and errors > 0
        if (stopped or all_failed) and state.get("last_warning") != today.isoformat():
            warning = (
                f"⚠️ Flight tracker: Google stopped answering after {len(found)} fares ({errors} errors). "
                "It'll try again next run. If this keeps happening, lower the samples "
                "in config.toml or remove a few airports."
            )
            if notify(warning):
                state["last_warning"] = today.isoformat()

        state["last_run"] = now.isoformat()
        state["last_run_summary"] = f"{local:%a %d %b %H:%M} SGT, {len(found)} fares found, {errors} errors"

    if digest_due and history:
        run_info = f"Last search: {state.get('last_run_summary', 'none yet')}"
        if notify(format_digest(cfg, history, now, today, base, run_info)):
            state["last_digest"] = today.isoformat()

    if args.dry_run:
        log("Dry run: nothing saved.")
    else:
        if search_due:
            save_history(history, now, int(a.get("history_days", 60)))
            write_dashboards(cfg, history, now, today, base)
        save_state(state)
    return 1 if (all_failed or not telegram_ok) else 0


def main() -> int:
    p = argparse.ArgumentParser(description="Cheap flight tracker from Singapore (Google Flights + Telegram).")
    p.add_argument("--force", action="store_true", help="search now, ignoring the schedule")
    p.add_argument("--status", action="store_true", help="show when it last ran and exit")
    p.add_argument("--setup-telegram", action="store_true", help="connect your Telegram bot and exit")
    p.add_argument("--test-telegram", action="store_true", help="send a test message and exit")
    p.add_argument("--dry-run", action="store_true", help="print messages instead of sending; save nothing")
    p.add_argument("--limit", type=int, default=0, help="only run the first N searches (for testing)")
    p.add_argument("--no-delay", action="store_true", help="skip the pause between searches")
    p.add_argument("--force-digest", action="store_true", help="send the daily summary now")
    p.add_argument("--dashboard", action="store_true", help="rebuild the dashboard from saved data and exit")
    args = p.parse_args()

    if args.setup_telegram:
        return setup_telegram()
    if args.status:
        return show_status()
    if args.dashboard:
        cfg = load_config()
        now = datetime.now(timezone.utc).replace(microsecond=0)
        history, _ = prepare_history(cfg, load_history(), {})
        a = cfg["alerts"]
        base = baselines(history, now, int(a.get("baseline_days", 30)), int(a.get("min_samples", 15)))
        write_dashboards(cfg, history, now, now.astimezone(SGT).date(), base)
        print(f"Dashboard written to {DASHBOARD_HTML.relative_to(ROOT)} and {DASHBOARD_MD.name}")
        return 0
    if args.test_telegram:
        ok = send_message("✅ Flight tracker test message.")
        print("Sent." if ok else "Not sent: run  python tracker.py --setup-telegram  first.")
        return 0 if ok else 1
    try:
        return run(args)
    except Exception:
        log("Crashed:\n" + traceback.format_exc())  # scheduled runs have no console, so keep it in the log
        return 1


if __name__ == "__main__":
    sys.exit(main())
