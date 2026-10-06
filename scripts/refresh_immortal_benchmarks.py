#!/usr/bin/env python3
"""Build local Immortal-Divine Position-1 minute benchmarks from OpenDota.

Designed to be safe against OpenDota's anonymous rate limits:
- retries HTTP 429/5xx with Retry-After/exponential backoff
- throttles requests using returned rate-limit headers
- caches public-match pages, hero constants, and parsed match details
- writes a resumable state file after every accepted match
- never destroys the previous benchmark unless a new non-empty one is built

Environment variables:
  IMMORTAL_BENCHMARK_PLAYERS=250     target Position-1 player samples
  IMMORTAL_BENCHMARK_MAX_PAGES=250   maximum /publicMatches pages to scan
  IMMORTAL_DIVINE_MIN_RANK=75         minimum public-match avg_rank_tier
  IMMORTAL_DIVINE_MAX_RANK=80         maximum public-match avg_rank_tier
  OPEN_DOTA_MIN_INTERVAL=1.10         minimum seconds between API requests
  OPEN_DOTA_API_KEY=...               optional OpenDota API key
  DOTA_PATCH=7.41f

Position-1 classification:
  OpenDota parsed player position_est == 1 (maximum one estimated Pos-1 per team).
  Individual rank_tier is not used as a hard filter; lobby avg_rank_tier 75-80 defines the cohort.
"""
import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATCH = os.getenv("DOTA_PATCH", "7.41f")
OUT = ROOT / "knowledge" / f"immortal_pos1_{PATCH.replace('.', '_')}.json"
CACHE = ROOT / ".cache" / "opendota_immortal_benchmarks"
DETAIL_CACHE = CACHE / "matches"
PAGE_CACHE = CACHE / "public_pages"
STATE_FILE = CACHE / f"state_{PATCH.replace('.', '_')}.json"
CONSTANTS_CACHE = CACHE / "heroes.json"

TARGET_PLAYERS = int(os.getenv("IMMORTAL_BENCHMARK_PLAYERS", "250"))
MAX_PAGES = int(os.getenv("IMMORTAL_BENCHMARK_MAX_PAGES", "250"))
MIN_INTERVAL = float(os.getenv("OPEN_DOTA_MIN_INTERVAL", "1.10"))
MAX_RETRIES = int(os.getenv("OPEN_DOTA_MAX_RETRIES", "8"))
RANK_MIN = int(os.getenv("IMMORTAL_DIVINE_MIN_RANK", "75"))
RANK_MAX = int(os.getenv("IMMORTAL_DIVINE_MAX_RANK", "80"))
COHORT_ID = f"immortal_divine_avg_rank_{RANK_MIN}_{RANK_MAX}_position_est_1_v2"
MINUTES = list(range(5, 46, 5))
API = "https://api.opendota.com/api"
KEY = os.getenv("OPEN_DOTA_API_KEY")

CACHE.mkdir(parents=True, exist_ok=True)
DETAIL_CACHE.mkdir(parents=True, exist_ok=True)
PAGE_CACHE.mkdir(parents=True, exist_ok=True)

_last_request_at = 0.0


def _sleep_before_request():
    global _last_request_at
    wait = MIN_INTERVAL - (time.monotonic() - _last_request_at)
    if wait > 0:
        time.sleep(wait)


def _header_int(headers, name):
    try:
        return int(headers.get(name))
    except (TypeError, ValueError):
        return None


def get(path, params=None):
    """GET JSON with rate-limit-aware retry/backoff."""
    global _last_request_at
    params = dict(params or {})
    if KEY:
        params["api_key"] = KEY
    url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")

    for attempt in range(MAX_RETRIES + 1):
        _sleep_before_request()
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "dota-ai-assistant/1.2",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                _last_request_at = time.monotonic()
                remaining_min = _header_int(r.headers, "x-rate-limit-remaining-minute")
                remaining_day = _header_int(r.headers, "x-rate-limit-remaining-day")
                data = json.load(r)

                # If the minute bucket is nearly exhausted, proactively wait.
                if remaining_min is not None and remaining_min <= 3:
                    print(
                        f"Rate limit nearly exhausted: {remaining_min}/min remaining, "
                        f"{remaining_day if remaining_day is not None else '?'} daily remaining. "
                        "Cooling down 62s...",
                        flush=True,
                    )
                    time.sleep(62)
                return data, remaining_min, remaining_day

        except urllib.error.HTTPError as e:
            _last_request_at = time.monotonic()
            if e.code not in (429, 500, 502, 503, 504) or attempt >= MAX_RETRIES:
                raise

            retry_after = e.headers.get("Retry-After") if e.headers else None
            try:
                wait = float(retry_after)
            except (TypeError, ValueError):
                # 429 should normally wait for a fresh minute bucket; server errors can retry sooner.
                wait = 62.0 if e.code == 429 else min(30.0, 2 ** attempt)
            wait += random.uniform(0.1, 0.8)
            print(f"OpenDota HTTP {e.code}. Retrying in {wait:.1f}s (attempt {attempt + 1}/{MAX_RETRIES})...", flush=True)
            time.sleep(wait)

        except (urllib.error.URLError, TimeoutError) as e:
            _last_request_at = time.monotonic()
            if attempt >= MAX_RETRIES:
                raise
            wait = min(30.0, 2 ** attempt) + random.uniform(0.1, 0.8)
            print(f"Network error: {e}. Retrying in {wait:.1f}s...", flush=True)
            time.sleep(wait)

    raise RuntimeError(f"Failed to fetch {url}")


def cached_json(path, fetcher):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8")), True
        except Exception:
            pass
    data = fetcher()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(path)
    return data, False


def q(values, p):
    values = sorted(values)
    if not values:
        return None
    x = (len(values) - 1) * p
    lo = int(x)
    hi = min(lo + 1, len(values) - 1)
    f = x - lo
    return round(values[lo] * (1 - f) + values[hi] * f)


def is_position_one(player):
    """Use OpenDota's estimated farm-priority position, not lane_role.

    lane_role describes lane assignment and can label multiple players on one
    team as role 1. position_est is the parsed estimate of positions 1..5.
    """
    try:
        return int(player.get("position_est")) == 1
    except (TypeError, ValueError):
        return False


def series_at(arr, minute):
    if not isinstance(arr, list) or len(arr) <= minute:
        return None
    return arr[minute]


def add_player(rows, p):
    gold_t, xp_t, lh_t = p.get("gold_t"), p.get("xp_t"), p.get("lh_t")
    if not all(isinstance(x, list) for x in (gold_t, xp_t, lh_t)):
        return False
    added = False
    for minute in MINUTES:
        gold = series_at(gold_t, minute)
        xp = series_at(xp_t, minute)
        lh = series_at(lh_t, minute)
        if gold is not None:
            rows[minute]["gpm"].append(gold / max(1, minute))
            added = True
        if xp is not None:
            rows[minute]["xpm"].append(xp / max(1, minute))
            added = True
        if lh is not None:
            rows[minute]["last_hits"].append(lh)
            added = True
    return added


def pack(rows, sample_size, min_metric_n=5):
    out = {"sample_size": sample_size, "minutes": {}}
    for minute, metrics in rows.items():
        packed = {}
        for metric, vals in metrics.items():
            if len(vals) >= min_metric_n:
                packed[metric] = {
                    "p25": q(vals, .25),
                    "p50": q(vals, .50),
                    "p75": q(vals, .75),
                    "n": len(vals),
                }
        if packed:
            out["minutes"][str(minute)] = packed
    return out


def rows_to_plain(rows):
    return {
        str(minute): {metric: vals for metric, vals in metrics.items()}
        for minute, metrics in rows.items()
    }


def plain_to_rows(data):
    rows = defaultdict(lambda: defaultdict(list))
    for minute, metrics in (data or {}).items():
        for metric, vals in (metrics or {}).items():
            rows[int(minute)][metric].extend(vals or [])
    return rows


def save_state(less_than, pages_scanned, seen, accepted_players, matches_sampled,
               hero_counts, general_rows, hero_rows):
    payload = {
        "patch": PATCH,
        "cohort_id": COHORT_ID,
        "rank_min": RANK_MIN,
        "rank_max": RANK_MAX,
        "less_than": less_than,
        "pages_scanned": pages_scanned,
        "seen_match_ids": sorted(seen),
        "accepted_players": accepted_players,
        "matches_sampled": matches_sampled,
        "hero_counts": dict(hero_counts),
        "general_rows": rows_to_plain(general_rows),
        "hero_rows": {hero: rows_to_plain(rows) for hero, rows in hero_rows.items()},
        "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(STATE_FILE)


def load_state():
    if not STATE_FILE.exists():
        return None
    try:
        s = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if s.get("patch") != PATCH:
            return None
        # A changed cohort definition must not resume an incompatible cursor/state.
        if s.get("cohort_id") != COHORT_ID:
            print("Benchmark cohort changed; starting a fresh sampling state (page/detail caches remain reusable).", flush=True)
            return None
        return s
    except Exception:
        return None


def fetch_constants():
    data, _, _ = get("/constants/heroes")
    return data


heroes_const, constants_cached = cached_json(CONSTANTS_CACHE, fetch_constants)
hero_names = {int(k): v["localized_name"] for k, v in heroes_const.items()}
print(f"Hero constants: {'cache' if constants_cached else 'downloaded'} ({len(hero_names)} heroes)", flush=True)

state = load_state()
if state:
    less_than = state.get("less_than")
    pages_scanned = int(state.get("pages_scanned", 0))
    seen = set(state.get("seen_match_ids") or [])
    accepted_players = int(state.get("accepted_players", 0))
    matches_sampled = int(state.get("matches_sampled", 0))
    hero_counts = defaultdict(int, state.get("hero_counts") or {})
    general_rows = plain_to_rows(state.get("general_rows"))
    hero_rows = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for hero, raw_rows in (state.get("hero_rows") or {}).items():
        hero_rows[hero] = plain_to_rows(raw_rows)
    print(
        f"Resuming: {accepted_players}/{TARGET_PLAYERS} Pos-1 samples, "
        f"{matches_sampled} accepted matches, {pages_scanned} pages already scanned.",
        flush=True,
    )
else:
    hero_rows = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    hero_counts = defaultdict(int)
    general_rows = defaultdict(lambda: defaultdict(list))
    seen = set()
    less_than = None
    accepted_players = 0
    matches_sampled = 0
    pages_scanned = 0

try:
    while accepted_players < TARGET_PLAYERS and pages_scanned < MAX_PAGES:
        params = {"mmr_descending": 1}
        if less_than:
            params["less_than_match_id"] = less_than

        # Cache pages by pagination cursor so a restart does not spend another request.
        page_key = str(less_than or "latest")
        page_file = PAGE_CACHE / f"{page_key}.json"

        def fetch_page():
            data, rem_min, rem_day = get("/publicMatches", params)
            print(
                f"OpenDota quota after page request: minute={rem_min if rem_min is not None else '?'}, "
                f"day={rem_day if rem_day is not None else '?'}",
                flush=True,
            )
            return data

        matches, page_cached = cached_json(page_file, fetch_page)
        if not matches:
            print("No more public matches returned.", flush=True)
            break

        pages_scanned += 1
        less_than = min(m["match_id"] for m in matches if m.get("match_id"))
        immortal_divine_candidates = [
            m for m in matches
            if RANK_MIN <= (m.get("avg_rank_tier") or 0) <= RANK_MAX
            and m.get("match_id") not in seen
        ]
        print(
            f"Page {pages_scanned}/{MAX_PAGES} ({'cache' if page_cached else 'API'}): "
            f"{len(matches)} matches, {len(immortal_divine_candidates)} new Immortal-Divine candidates "
            f"(avg rank {RANK_MIN}-{RANK_MAX}) | "
            f"samples {accepted_players}/{TARGET_PLAYERS}",
            flush=True,
        )

        for m in immortal_divine_candidates:
            if accepted_players >= TARGET_PLAYERS:
                break
            match_id = int(m["match_id"])
            seen.add(match_id)
            detail_file = DETAIL_CACHE / f"{match_id}.json"

            def fetch_detail(mid=match_id):
                data, rem_min, rem_day = get(f"/matches/{mid}")
                print(
                    f"  Match {mid}: downloaded | quota minute={rem_min if rem_min is not None else '?'}, "
                    f"day={rem_day if rem_day is not None else '?'}",
                    flush=True,
                )
                return data

            try:
                detail, detail_cached = cached_json(detail_file, fetch_detail)
            except Exception as e:
                print(f"  Match {match_id}: skipped after retries ({e})", flush=True)
                save_state(less_than, pages_scanned, seen, accepted_players, matches_sampled,
                           hero_counts, general_rows, hero_rows)
                continue

            used_match = False
            accepted_here = []
            for p in detail.get("players") or []:
                if not is_position_one(p):
                    continue
                hero = hero_names.get(p.get("hero_id"))
                if not hero:
                    continue
                if not add_player(general_rows, p):
                    continue
                add_player(hero_rows[hero], p)
                hero_counts[hero] += 1
                accepted_players += 1
                accepted_here.append(hero)
                used_match = True
                if accepted_players >= TARGET_PLAYERS:
                    break

            if used_match:
                matches_sampled += 1
            print(
                f"  Match {match_id}: {'cache' if detail_cached else 'API'}; "
                f"Pos-1 accepted={accepted_here or 0}; total={accepted_players}/{TARGET_PLAYERS}",
                flush=True,
            )
            save_state(less_than, pages_scanned, seen, accepted_players, matches_sampled,
                       hero_counts, general_rows, hero_rows)

        save_state(less_than, pages_scanned, seen, accepted_players, matches_sampled,
                   hero_counts, general_rows, hero_rows)

except KeyboardInterrupt:
    print("\nInterrupted. Progress has been saved; rerun the script to resume.", flush=True)

out = {
    "patch": PATCH,
    "role": "Position 1",
    "cohort": "Immortal-Divine Level Position 1",
    "rank_filter": f"public match {RANK_MIN} <= avg_rank_tier <= {RANK_MAX} (Immortal-Divine candidate lobby); parsed player position_est == 1",
    "position_classifier": "OpenDota parsed player position_est == 1; rank_tier retained as source metadata when available",
    "rank_min": RANK_MIN,
    "rank_max": RANK_MAX,
    "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    "source": "OpenDota publicMatches + parsed match time series",
    "matches_sampled": matches_sampled,
    "position1_players_sampled": accepted_players,
    "pages_scanned": pages_scanned,
    "target_players": TARGET_PLAYERS,
    "complete": accepted_players >= TARGET_PLAYERS,
    "all_position_1": pack(general_rows, accepted_players),
    "heroes": {},
}
for hero, rows in hero_rows.items():
    out["heroes"][hero] = pack(rows, hero_counts[hero])

# Never replace a previously useful benchmark with an empty failed run.
if accepted_players > 0:
    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, indent=2), encoding="utf-8")
    tmp.replace(OUT)
    print(
        f"Wrote {OUT}: {accepted_players} Immortal-Divine Position-1 players from "
        f"{matches_sampled} matches; {len(out['heroes'])} heroes; complete={out['complete']}.",
        flush=True,
    )
else:
    print(
        "No qualifying Position-1 samples were collected, so the existing benchmark file was left unchanged.\n"
        f"Scanned {pages_scanned} pages. Re-run to resume, or increase IMMORTAL_BENCHMARK_MAX_PAGES.",
        flush=True,
    )
