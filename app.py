import json
import math
import os
import re
import random
import shutil
import subprocess
import tempfile
import threading
import time
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from openai import OpenAI

load_dotenv()

app = Flask(__name__)
BUILD_ID = "v44.4-map-wave-indicators-2026-10-06"
BASE_DIR = Path(__file__).resolve().parent

STRUCTURE_TRACE_DIR = BASE_DIR / "logs"
STRUCTURE_TRACE_FILE = STRUCTURE_TRACE_DIR / "structure_gsi_transitions.jsonl"
STRUCTURE_TRACE_ENABLED = os.getenv("STRUCTURE_TRACE_ENABLED", "1").lower() in {"1", "true", "yes", "on"}
_structure_trace_lock = threading.Lock()


MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-luna")
AI_INTERVAL_PATTERN = [int(x.strip()) for x in os.getenv("AI_INTERVAL_PATTERN", "60").split(",") if x.strip()] or [60]
AI_INTERVAL = AI_INTERVAL_PATTERN[0]  # compatibility/display only
PORT = int(os.getenv("PORT", "5050"))
VOICE_ENABLED = os.getenv("VOICE_ENABLED", "1").lower() in {"1", "true", "yes", "on"}
KOKORO_BIN = os.getenv("KOKORO_BIN", "kokoro-tts")
KOKORO_MODEL = os.getenv("KOKORO_MODEL", "models/kokoro/kokoro-v1.0.onnx")
KOKORO_VOICES = os.getenv("KOKORO_VOICES", "models/kokoro/voices-v1.0.bin")
KOKORO_VOICE = os.getenv("KOKORO_VOICE", "am_michael")
VOICE_VOLUME = float(os.getenv("VOICE_VOLUME", "3.0"))
VOICE_SPEED = float(os.getenv("VOICE_SPEED", "1.25"))
CURRENT_PATCH = os.getenv("DOTA_PATCH", "7.41f")
KNOWLEDGE_FILE = os.getenv("KNOWLEDGE_FILE", f"knowledge/patch_{CURRENT_PATCH.replace('.', '_')}.json")
META_PROFILE_FILE = os.getenv("META_PROFILE_FILE", "knowledge/meta_carry_profiles.json")
IMMORTAL_BENCHMARK_FILE = os.getenv("IMMORTAL_BENCHMARK_FILE", f"knowledge/immortal_pos1_{CURRENT_PATCH.replace(chr(46), chr(95))}.json")
PROMINENT_CARRY_POOL = ["Sven", "Ursa", "Phantom Assassin", "Juggernaut"]
USD_TO_IDR = float(os.getenv("USD_TO_IDR", "17924"))
LUNA_INPUT_USD_PER_M = 0.20
LUNA_OUTPUT_USD_PER_M = 1.20

_lock = threading.Lock()
_voice_lock = threading.Lock()
_state = {
    "connected": False,
    "last_gsi_at": None,
    "observation": {},
    "advice": None,
    "pregame_strategy": None,
    "meta_strategy": None,
    "live_win_probability": {"probability": None, "factors": [], "updated_at": None, "history": []},
    "performance_report": None,
    "performance_report_status": "waiting",
    "performance_report_error": None,
    "pregame_status": "waiting",
    "pregame_error": None,
    "pregame_signature": None,
    "pregame_candidate_signature": None,
    "pregame_candidate_since": 0.0,
    "pregame_last_spoken_signature": None,
    "draft": {"allies": [], "enemies": [], "source": "waiting_for_gsi"},
    "manual_draft": {"allies": [], "enemies": []},
    "known_gsi_draft": {"allies": [], "enemies": []},
    "immortal_pace": {"available": False, "status": "Benchmark not loaded"},
    "strategic_context": {},
    "cockpit": {},
    "analysis_schedule": {"pattern_seconds": AI_INTERVAL_PATTERN, "next_interval_seconds": AI_INTERVAL_PATTERN[0]},
    "ai_status": "idle",
    "ai_error": None,
    "last_ai_at": None,
    "model": MODEL,
    "voice_enabled": VOICE_ENABLED,
    "voice_status": "idle",
    "knowledge": {"patch": CURRENT_PATCH, "loaded": False, "entries": 0, "relevant_entries": 0, "error": None},
    "voice_error": None,
    "last_spoken": None,
    "usage": {
        "ai_calls": 0,
        "tts_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
                "llm_cost_usd": 0.0,
        "tts_cost_usd": 0.0,
        "voice_mode": "LOCAL / OFFLINE",
        "total_cost_usd": 0.0,
        "total_cost_idr": 0.0,
        "usd_to_idr": USD_TO_IDR,
    },
    "history": [],
    "match_key": None,
    "session_phase": "waiting",
    "last_gsi_age_seconds": None,
    "manual_enemy_structures": {},
}
_last_analysis_monotonic = 0.0
_analysis_interval_index = 0
_analysis_running = False
_pregame_running = False
_postmatch_report_running = False
_gsi_snapshot = {}
_structure_registry = {"radiant": {}, "dire": {}}
_minimap_hero_registry = {}
_gsi_packet_seq = 0

# Zero-API event coach state. These notifications are derived locally from GSI
# deltas and spoken with local Kokoro; they never call OpenAI.
_event_coach = {
    "initialized": False,
    "kills": 0,
    "deaths": 0,
    "level": 0,
    "items": set(),
    "buyback_cooldown": 0,
    "last_kill_monotonic": None,
    "multikill_count": 0,
    "last_death_phrase": None,
    "last_single_kill_phrase": None,
}
SINGLE_KILL_COACH_PHRASES = (
    "Nice kill.",
    "Good kill.",
    "Well played.",
    "Good one.",
    "Clean.",
    "Nice.",
    "Good job.",
    "Keep going.",
    "That's good.",
    "Well done.",
    "Good execution.",
    "Nice catch.",
)
DEATH_COACH_PHRASES = (
    "Reset. Next timing.",
    "Stay focused. Reset.",
    "No problem. Don't let one death become two.",
    "Reset and farm.",
    "Stay calm.",
    "Next play.",
    "Forget it. Reset.",
    "Don't chain deaths.",
    "Farm your next timing.",
    "Reset the map.",
    "Play the next minute.",
    "Stay disciplined.",
)

def _pick_nonrepeating_phrase(options, last_phrase):
    choices = [x for x in options if x != last_phrase]
    return random.choice(choices or list(options))


MAJOR_ITEM_KEYWORDS = (
    "black_king_bar", "manta", "butterfly", "satanic", "skadi", "abyssal",
    "daedalus", "silver_edge", "monkey_king_bar", "mjollnir", "bloodthorn",
    "nullifier", "disperser", "swift_blink", "overwhelming_blink", "arcane_blink",
    "assault", "heart", "refresher", "aghanims_scepter",
)

SYSTEM_PROMPT = """You are a real-time Dota 2 Position 1 carry coach for an IMMORTAL-LEVEL solo-queue player.
Assume the player already understands last hitting, denying, creep aggro/equilibrium, pulls, standard farming patterns, common hero abilities/items, basic objectives, and basic carry positioning. Do not waste output explaining fundamentals unless the current state makes one immediately relevant.

Coach at Immortal decision depth. Prioritize lane matchup optimization, wave-state manipulation, map geometry and safe/unsafe farm, enemy-location implications from ONLY observed information, farming opportunity cost, adaptive itemization, item/level timing versus enemy timings, power-spike synchronization, fight selection, target priority, cooldown/spell interactions when observable, buyback/Roshan/high-ground decisions, when NOT to join the team, risk-adjusted resource acquisition, converting enemy map movements into farm/objectives, and the current win condition.

Use ONLY information in the supplied observation, pre-game plan, and supplied PATCH KNOWLEDGE. Patch knowledge contains authoritative Valve facts for the configured current patch and overrides conflicting older pretrained Dota knowledge. Do not mention patch facts unless they materially affect the decision. Never invent enemy positions, cooldowns, objectives, wards, or hidden information. Specific simple advice is better than generic sophisticated-sounding advice.

The SAME 60-second live analysis call must update four things together: Recommendation, Win Probability, In-game Carry Plan, and Strategy Support. The carry plan is an IN-GAME plan, not a pre-game draft summary. Re-evaluate it from the current observable match state, including game phase, score, structures, player level/farm trajectory, current items and progression, readiness, recent events, visible draft/matchups, and patch knowledge. Never invent hidden enemy state.
WIN_PROBABILITY must be recomputed as a fresh snapshot estimate of the player's TEAM chance to win from the CURRENT observable match state on every live analysis cycle. Do not anchor to, smooth toward, or preserve the previous percentage merely for continuity. The current context can justify a large change between cycles. `player_team` and `score_from_player_perspective` explicitly identify which side is the player's team. Do not confuse Radiant/Dire score ordering.
STRUCTURE STATE is authoritative and MUST be incorporated into WIN_PROBABILITY, WIN_FACTORS, Recommendation, and the in-game carry plan. Own-team structure status comes from GSI; enemy structure status comes from manual dashboard input. Never say no structures are destroyed when structure_state reports destroyed towers or barracks.
LANE WAVE PRESSURE is a coarse CURRENT-GSI-only map-pressure signal derived from visible lane creeps. Incorporate it when strategically relevant to WIN_PROBABILITY and decisions: pushed waves can create farm, map pressure, tower/high-ground windows, or expose a side of the map. Treat confidence conservatively and NEVER use wave state to invent hidden enemy hero locations.
WIN_PROBABILITY must estimate the player's TEAM chance to win from the CURRENT observable match state. Use all strategically relevant supplied real-time evidence when available: current team score and score differential, game time, player level and any observable level comparison, GPM/XPM and Immortal pace trajectory, current items and item progression/power spikes, deaths/KDA, HP/mana/readiness, buyback state, destroyed/damaged towers and barracks, recent events, draft matchup, and accumulated strategy support. Weight evidence by game phase and strategic importance rather than mechanically averaging statistics. Never infer unavailable enemy levels, net worth, hidden items, fog positions, wards, Roshan state, or cooldowns. If important information is missing, make the estimate more conservative and reflect that uncertainty in CONFIDENCE.
This percentage is an AI strategic estimate, NOT an official or statistically calibrated Dota win probability. Keep factors concise and non-redundant, naming the 1-3 observations that most moved the estimate this cycle.

Return exactly fifteen fields:
ACTION: <specific best next action>
WHY: <one concise sentence, 12-22 words, containing only the decisive Immortal-level reason grounded in observation>
WATCH: <one concise sentence, 8-18 words, containing only the highest-value condition/timing/threat to monitor>
CONFIDENCE: <LOW|MEDIUM|HIGH>
WIN_PROBABILITY: <integer 0-100>%
WIN_FACTORS: <1-3 concise current-match factors, comma-separated>
WIN_CONDITION: <one concise current in-game carry win condition>
LANE: <current lane/wave plan; if laning is over, say the relevant wave-management plan instead>
ITEMS: <current item progression and conditional next purchase>
FARM: <current farming pattern, safest resources, and areas to avoid>
TEAMFIGHT: <current fight-entry rule and key commitment condition>
TARGETS: <current target-selection priority>
THREATS: <current 2-3 highest-value observable/draft threats>
META_NEW: <0-3 concise genuinely NEW strategy-support insights, separated by semicolons. Format each CATEGORY: insight. Allowed CATEGORY: HERO, ITEMS, MATCHUP, TEAMFIGHT, FARM, NEUTRAL. Do not repeat or paraphrase ACCUMULATED STRATEGY SUPPORT. If nothing new, write NONE.>
VOICE: <adaptive spoken coaching. If urgent, 5-18 words. Otherwise 25-50 words, ordered as immediate action -> location/pattern -> reason/threat -> next objective. Do not read the dashboard verbatim.>
Use the supplied DERIVED STRATEGIC CONTEXT as the primary compressed match model. Integrate ECONOMY, MAP/STRUCTURES, POWER/READINESS, and OBJECTIVE state rather than commenting on isolated statistics.
Use IMMORTAL PACE TRAJECTORY gradually: compare current pace with earlier checkpoints and mention it only when it materially changes the decision. Winning the game, urgent objectives, survival, and timing windows outrank benchmark optimization. Never chase GPM merely to improve a benchmark. Never invent a benchmark.
Enemy minimap coordinates may be used ONLY when `visible_enemy_positions` contains them. Those rows come from the CURRENT GSI minimap packet and are treated as currently observable evidence. Never infer a current enemy location from remembered/accumulated/display-only coordinates or from a missing hero.
If information is insufficient, explicitly say so and choose the best conservative high-level carry action.
"""


PREGAME_PROMPT = """You are an IMMORTAL-LEVEL Dota 2 Position 1 strategy coach for a solo-queue carry player. Assume strong mastery of fundamentals; focus on matchup-specific lane optimization, adaptive itemization, farming geometry, timing windows, fight entry, target priority, and win conditions.
Build an initial carry plan using ONLY the currently supplied draft information and supplied PATCH KNOWLEDGE. This is only the initial fallback; the live 60-second coach will continuously replace it with an in-game plan once the match is analyzable. Patch knowledge is authoritative for the configured current patch and overrides conflicting older pretrained Dota knowledge. Do not invent heroes, lanes, items, or enemy capabilities that are not supported by the supplied draft.
The player is the carry hero marked player_hero. Consider ally synergy, enemy threats, lane pressure, dispels, silences, roots, burst, physical damage, saves, initiation, and likely fight shape.
The player prominently plays Sven, Ursa, Phantom Assassin, and Juggernaut. During hero selection, compare these four against the VISIBLE draft only. If the player hero is already selected, evaluate that hero rather than pretending another pick is possible.
Return exactly these eight lines, concise but specific:
WIN_CONDITION: <one-sentence carry win condition>
LANE: <lane plan, trading posture, creep-equilibrium priority>
ITEMS: <starting/early/core item direction and conditional adaptation>
FARM: <post-lane farming pattern and dangerous areas>
TEAMFIGHT: <when/how to enter fights and what to avoid>
TARGETS: <target-selection principle based on draft>
THREATS: <2-3 most important enemy threats and why>
META: <2-4 concise NEW strategy-support insights only. Format each as CATEGORY: insight and separate insights with semicolons. Allowed categories: HERO, ITEMS, MATCHUP, TEAMFIGHT, FARM, NEUTRAL. Do not repeat or paraphrase information already present in EXISTING STRATEGY SUPPORT. Each insight must add a distinct actionable fact. If nothing genuinely new is supported by the updated draft, return META: NONE.>
If the draft is incomplete, explicitly qualify the plan instead of guessing missing heroes.
"""

def resolve_repo_path(value):
    path = Path(value).expanduser()
    return path if path.is_absolute() else BASE_DIR / path


def load_patch_knowledge():
    path = resolve_repo_path(KNOWLEDGE_FILE)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        entries = data.get("entries", [])
        with _lock:
            _state["knowledge"].update({"patch": data.get("patch", CURRENT_PATCH), "loaded": True, "entries": len(entries), "error": None})
        return data
    except Exception as exc:
        with _lock:
            _state["knowledge"].update({"loaded": False, "entries": 0, "error": str(exc)})
        return {"patch": CURRENT_PATCH, "entries": []}


def norm_key(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def relevant_patch_knowledge(obs, limit=10):
    kb = load_patch_knowledge()
    hero_names = {norm_key(obs.get("hero", {}).get("name"))}
    draft = obs.get("draft") or {}
    hero_names.update(norm_key(x) for x in draft.get("allies", []))
    hero_names.update(norm_key(x) for x in draft.get("enemies", []))
    hero_names.discard("")
    item_names = {norm_key(x.get("name")) for x in (obs.get("items") or []) if isinstance(x, dict)}
    selected=[]
    for entry in kb.get("entries", []):
        subject=norm_key(entry.get("subject"))
        if entry.get("category") == "hero" and subject in hero_names:
            selected.append(entry)
        elif entry.get("category") == "item" and subject in item_names:
            selected.append(entry)
        elif entry.get("always_relevant"):
            selected.append(entry)
    selected=selected[:limit]
    with _lock:
        _state["knowledge"]["relevant_entries"] = len(selected)
    return {"patch": kb.get("patch", CURRENT_PATCH), "source": kb.get("source"), "entries": selected}


def load_immortal_benchmarks():
    try:
        return json.loads(resolve_repo_path(IMMORTAL_BENCHMARK_FILE).read_text(encoding="utf-8"))
    except Exception:
        return {"patch": CURRENT_PATCH, "role": "Position 1", "heroes": {}}


def percentile_from_quartiles(value, q):
    """Approximate percentile from P25/P50/P75. Tails are deliberately conservative."""
    if value is None or not q:
        return None
    pts = [(25, q.get("p25")), (50, q.get("p50")), (75, q.get("p75"))]
    if any(v is None for _, v in pts):
        return None
    p25, p50, p75 = [float(v) for _, v in pts]
    value = float(value)
    if value <= p25:
        span = max(1.0, p50 - p25)
        return max(5, round(25 - (p25 - value) / span * 20))
    if value <= p50:
        return round(25 + (value - p25) / max(1.0, p50 - p25) * 25)
    if value <= p75:
        return round(50 + (value - p50) / max(1.0, p75 - p50) * 25)
    span = max(1.0, p75 - p50)
    return min(95, round(75 + (value - p75) / span * 20))


def economy_status_from_percentile(percentile):
    """Canonical Position-1 economy band used by benchmark and Strategic State."""
    if percentile is None:
        return "Insufficient metrics"
    if percentile >= 80:
        return "VERY AHEAD"
    if percentile >= 60:
        return "AHEAD"
    if percentile >= 40:
        return "ON PACE"
    if percentile >= 20:
        return "BEHIND"
    return "VERY BEHIND"


def immortal_pace(obs):
    hero = obs.get("hero", {}).get("name")
    seconds = obs.get("game", {}).get("clock_time")
    if not hero or seconds is None:
        return {"available": False, "status": "Waiting for match data", "hero": hero, "phase": "waiting"}
    if seconds < 300:
        return {
            "available": False,
            "status": "EARLY GAME · BUILDING BASELINE",
            "detail": "Full Immortal–Divine comparison starts at 5:00",
            "hero": hero,
            "phase": "early_game",
        }

    kb = load_immortal_benchmarks()
    heroes = kb.get("heroes") or {}
    hero_data = heroes.get(hero)
    general_data = kb.get("all_position_1") or kb.get("general")

    # Prefer a sufficiently sampled same-hero cohort. Otherwise use the broad
    # Immortal Position-1 cohort. Older benchmark files without all_position_1
    # remain readable, but cannot provide a fabricated fallback.
    min_hero_n = int(os.getenv("IMMORTAL_HERO_MIN_SAMPLE", "20"))
    cohort = None
    cohort_label = None
    benchmark_data = None
    if hero_data and (hero_data.get("sample_size") or 0) >= min_hero_n and hero_data.get("minutes"):
        benchmark_data = hero_data
        cohort = "same_hero"
        cohort_label = f"Immortal–Divine {hero} Position 1"
    elif general_data and general_data.get("minutes"):
        benchmark_data = general_data
        cohort = "all_position_1"
        cohort_label = "Immortal–Divine Position 1 baseline"
    elif hero_data and hero_data.get("minutes"):
        # Better than no benchmark at all when using a legacy database, but label
        # the small sample explicitly rather than pretending it is robust.
        benchmark_data = hero_data
        cohort = "same_hero_small_sample"
        cohort_label = f"Immortal–Divine {hero} Position 1 · small sample"
    else:
        return {
            "available": False,
            "status": "BENCHMARK DATABASE NOT POPULATED",
            "detail": "Run scripts/refresh_immortal_benchmarks.py before the match.",
            "hero": hero,
            "phase": "missing_database",
            "cohort": None,
        }

    minute = max(5, int(seconds // 60))
    available_minutes = sorted(int(x) for x in benchmark_data.get("minutes", {}).keys())
    if not available_minutes:
        return {"available": False, "status": "Benchmark has no minute checkpoints", "hero": hero, "cohort": cohort}
    nearest = min(available_minutes, key=lambda x: abs(x-minute))
    ref = benchmark_data["minutes"][str(nearest)]
    p = obs.get("player", {})
    metrics = {}
    for key, label in (("gpm", "GPM"), ("xpm", "XPM"), ("last_hits", "Last hits")):
        if key in ref and p.get(key) is not None:
            metrics[key] = {
                "label": label, "player": p.get(key),
                "p25": ref[key].get("p25"), "p50": ref[key].get("p50"), "p75": ref[key].get("p75"),
                "percentile": percentile_from_quartiles(p.get(key), ref[key]),
            }
    ps = [m["percentile"] for m in metrics.values() if m.get("percentile") is not None]
    overall = round(sum(ps)/len(ps)) if ps else None
    status = economy_status_from_percentile(overall)
    fallback_note = None
    if cohort == "all_position_1":
        fallback_note = f"{hero}-specific sample unavailable/small; using all Immortal–Divine Position-1 carries."

    # Send the P50 checkpoint curve to the dashboard. The player's trajectory is
    # recorded locally from GSI every ~5 seconds in _state["history"], so these
    # charts require no extra OpenAI/OpenDota calls during a match.
    benchmark_series = []
    for m in available_minutes:
        minute_ref = benchmark_data.get("minutes", {}).get(str(m), {})
        benchmark_series.append({
            "minute": m,
            "gpm": (minute_ref.get("gpm") or {}).get("p50"),
            "last_hits": (minute_ref.get("last_hits") or {}).get("p50"),
            "xpm": (minute_ref.get("xpm") or {}).get("p50"),
        })

    return {
        "available": bool(metrics), "status": status, "hero": hero, "minute": nearest,
        "game_minute": round(seconds / 60.0, 2),
        "sample_size": benchmark_data.get("sample_size"), "updated_at": kb.get("updated_at"), "source": kb.get("source"),
        "overall_percentile": overall, "metrics": metrics, "cohort": cohort, "cohort_label": cohort_label,
        "benchmark_series": benchmark_series,
        "fallback_note": fallback_note, "phase": "benchmark",
    }


def load_meta_profiles():
    try:
        return json.loads(resolve_repo_path(META_PROFILE_FILE).read_text(encoding="utf-8"))
    except Exception:
        return {"patch": CURRENT_PATCH, "heroes": {}}

def extract_meta_strategy(strategy):
    """Dashboard meta is strategist output, never a dump of the hidden knowledge base."""
    if not strategy:
        return None
    match = re.search(r"(?mi)^META:\s*(.+)$", strategy)
    return match.group(1).strip() if match else None

def pct(current, maximum):
    if current is None or not maximum:
        return None
    return round(100 * current / maximum)


def parse_items(items):
    out = []
    for slot, item in (items or {}).items():
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not name or name == "empty":
            continue
        out.append({
            "slot": slot,
            "name": name.replace("item_", "").replace("_", " ").title(),
            "raw_name": name,
            "cooldown": item.get("cooldown"),
            "charges": item.get("charges"),
            "can_cast": item.get("can_cast"),
        })
    return out


def parse_abilities(abilities):
    out = []
    for slot, ability in (abilities or {}).items():
        if not isinstance(ability, dict):
            continue
        name = ability.get("name")
        if not name:
            continue
        out.append({
            "slot": slot,
            "name": name.replace("_", " ").title(),
            "raw_name": name,
            "level": ability.get("level"),
            "cooldown": ability.get("cooldown"),
            "can_cast": ability.get("can_cast"),
            "ultimate": ability.get("ultimate"),
        })
    return out


def _deep_merge_state(dst, src):
    """Recursively merge a GSI delta into a persistent current-state snapshot."""
    if not isinstance(src, dict):
        return deepcopy(src)
    if not isinstance(dst, dict):
        dst = {}
    for key, value in src.items():
        if key in {"previously", "added"}:
            continue
        if isinstance(value, dict):
            dst[key] = _deep_merge_state(dst.get(key, {}), value)
        else:
            dst[key] = deepcopy(value)
    return dst


def _normalize_building_team(value):
    x = str(value or "").lower()
    if "radiant" in x or x in {"2", "good", "goodguys"}:
        return "radiant"
    if "dire" in x or x in {"3", "bad", "badguys"}:
        return "dire"
    return None


def _building_rows(branch):
    """Yield explicit building rows from one GSI branch.

    This intentionally reads only the branch supplied by the caller. It does
    not interpret ordinary omission as destruction.
    """
    buildings = (branch or {}).get("buildings") if isinstance(branch, dict) else None
    if not isinstance(buildings, dict):
        return {}
    out = {}
    for raw_team, rows in buildings.items():
        team = _normalize_building_team(raw_team)
        if not team or not isinstance(rows, dict):
            continue
        for name, state in rows.items():
            if isinstance(state, dict):
                out[(team, str(name))] = deepcopy(state)
    return out


def capture_structure_gsi_transition(packet):
    """Persist only packets that contain explicit building evidence.

    This diagnostic trace lets us validate the exact live-client semantics of
    current/added/previously around tower and barracks transitions without
    logging unrelated GSI payloads. It is local-only and contains no API key.
    """
    if not STRUCTURE_TRACE_ENABLED or not isinstance(packet, dict):
        return
    current = _building_rows(packet)
    added = _building_rows(packet.get("added") or {})
    previous = _building_rows(packet.get("previously") or {})
    if not (current or added or previous):
        return
    game = packet.get("map") if isinstance(packet.get("map"), dict) else {}
    record = {
        "captured_at": datetime.now().isoformat(timespec="milliseconds"),
        "game_time": game.get("game_time"),
        "clock_time": game.get("clock_time"),
        "game_state": game.get("game_state"),
        "current_buildings": packet.get("buildings") if isinstance(packet.get("buildings"), dict) else {},
        "added_buildings": ((packet.get("added") or {}).get("buildings")
                            if isinstance(packet.get("added"), dict) and isinstance((packet.get("added") or {}).get("buildings"), dict) else {}),
        "previously_buildings": ((packet.get("previously") or {}).get("buildings")
                                 if isinstance(packet.get("previously"), dict) and isinstance((packet.get("previously") or {}).get("buildings"), dict) else {}),
    }
    try:
        STRUCTURE_TRACE_DIR.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        with _structure_trace_lock:
            with STRUCTURE_TRACE_FILE.open("a", encoding="utf-8") as fh:
                fh.write(line)
    except Exception:
        # Diagnostics must never interrupt live GSI ingestion.
        pass



def _structure_trace_summary(rows):
    """Summarize the full captured structure trace without team assumptions."""
    teams_ever = set()
    names_by_team = {}
    transitions_by_team = {}
    health_changes = []
    removals = []
    last_health = {}

    for row in rows:
        if not isinstance(row, dict):
            continue
        branches = {
            "current": row.get("current_buildings") or {},
            "added": row.get("added_buildings") or {},
            "previously": row.get("previously_buildings") or {},
        }
        normalized = {}
        for branch_name, buildings in branches.items():
            normalized[branch_name] = {}
            if not isinstance(buildings, dict):
                continue
            for raw_team, structures in buildings.items():
                team = _normalize_building_team(raw_team) or str(raw_team)
                teams_ever.add(team)
                if not isinstance(structures, dict):
                    continue
                normalized[branch_name][team] = structures
                names_by_team.setdefault(team, set()).update(map(str, structures.keys()))

        current = normalized["current"]
        previous = normalized["previously"]
        added = normalized["added"]
        for team, structures in current.items():
            for name, state in structures.items():
                if not isinstance(state, dict) or state.get("health") is None:
                    continue
                hp = state.get("health")
                key = (team, str(name))
                old_hp = last_health.get(key)
                if old_hp is not None and hp != old_hp:
                    transitions_by_team[team] = transitions_by_team.get(team, 0) + 1
                    health_changes.append({
                        "game_time": row.get("game_time"), "clock_time": row.get("clock_time"),
                        "team": team, "structure": str(name), "from": old_hp, "to": hp
                    })
                last_health[key] = hp

        replacements = set()
        for source in (current, added):
            for team, structures in source.items():
                replacements.update((team, str(name)) for name in structures)
        for team, structures in previous.items():
            for name, state in structures.items():
                key = (team, str(name))
                if key not in replacements:
                    removals.append({
                        "game_time": row.get("game_time"), "clock_time": row.get("clock_time"),
                        "team": team, "structure": str(name),
                        "previous_health": state.get("health") if isinstance(state, dict) else None
                    })

    return {
        "packets_analyzed": len(rows),
        "teams_ever_seen": sorted(teams_ever),
        "structures_ever_seen": {k: sorted(v) for k, v in sorted(names_by_team.items())},
        "health_change_count_by_team": dict(sorted(transitions_by_team.items())),
        "health_changes": health_changes[-100:],
        "explicit_removals": removals[-100:],
        "symmetry_result": (
            "BOTH_RADIANT_AND_DIRE_OBSERVED" if {"radiant", "dire"}.issubset(teams_ever)
            else "ONLY_RADIANT_OBSERVED" if teams_ever == {"radiant"}
            else "ONLY_DIRE_OBSERVED" if teams_ever == {"dire"}
            else "INSUFFICIENT_DATA"
        ),
    }


def _read_structure_trace_all():
    rows = []
    try:
        if STRUCTURE_TRACE_FILE.exists():
            with _structure_trace_lock:
                lines = STRUCTURE_TRACE_FILE.read_text(encoding="utf-8").splitlines()
            for line in lines:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    except Exception:
        pass
    return rows

def update_structure_registry(packet):
    """Apply explicit GSI building deltas to an absolute Radiant/Dire registry.

    Normal packet omission means NO CHANGE. `previously` is used only as a
    deletion signal: if GSI says a building path changed by putting it in
    `previously`, but supplies no replacement for that same path in the current
    or `added` branch, the building was removed from the current layout and is
    persisted as destroyed. This prevents a dead tower from reverting to the
    dashboard's match-start display default.
    """
    global _structure_registry
    capture_structure_gsi_transition(packet)
    current = _building_rows(packet)
    added = _building_rows(packet.get("added") or {}) if isinstance(packet, dict) else {}
    previous = _building_rows(packet.get("previously") or {}) if isinstance(packet, dict) else {}
    replacements = dict(added)
    replacements.update(current)

    with _lock:
        for (team, name), delta in replacements.items():
            old = deepcopy(_structure_registry.setdefault(team, {}).get(name, {}))
            old.update(delta)
            hp = old.get("health")
            if hp is not None:
                old["health"] = hp
            _structure_registry[team][name] = old

        # A path appearing in `previously` is known to have changed. If GSI
        # supplies no current replacement for that exact building, treat that
        # explicit removal as destruction. Mere absence from a packet is never
        # enough to destroy a structure.
        for (team, name), old_state in previous.items():
            if (team, name) in replacements:
                continue
            known = deepcopy(_structure_registry.setdefault(team, {}).get(name, {}))
            if not known:
                known.update(old_state)
            known["health"] = 0
            if known.get("max_health") is None and old_state.get("max_health") is not None:
                known["max_health"] = old_state.get("max_health")
            known["destroyed_via_gsi_removal"] = True
            _structure_registry[team][name] = known

        return deepcopy(_structure_registry)


def accumulate_gsi_packet(packet):
    """Reconstruct a stable GSI snapshot across incremental/delta POSTs.

    Valve can expose newly-created nested fields under `added`, while normal
    top-level sections may vary between packets. `previously` contains OLD
    values and is not merged as current state. Buildings additionally use a
    persistent registry so explicit GSI removals remain destroyed.
    """
    global _gsi_snapshot
    structures = update_structure_registry(packet)
    with _lock:
        snap = deepcopy(_gsi_snapshot)
        added = packet.get("added")
        if isinstance(added, dict):
            snap = _deep_merge_state(snap, added)
        snap = _deep_merge_state(snap, packet)
        # The structure registry is authoritative for building persistence.
        # It is absolute Radiant/Dire state and never ally/enemy-relative.
        snap["buildings"] = deepcopy(structures)
        _gsi_snapshot = snap
        return deepcopy(snap)


def update_minimap_hero_registry(packet, player_hero=None):
    """Track minimap hero objects by object id, with recency.

    We only consume CURRENT and ADDED rows. `previously` contains old values and
    is never promoted to current identity. Object-id reuse overwrites the old row.
    """
    global _gsi_packet_seq, _minimap_hero_registry
    with _lock:
        _gsi_packet_seq += 1
        seq = _gsi_packet_seq
        branches = []
        if isinstance(packet.get("added"), dict):
            branches.append(packet["added"])
        branches.append(packet)
        for branch in branches:
            mm = branch.get("minimap") or {}
            if not isinstance(mm, dict):
                continue
            for object_id, delta in mm.items():
                if not isinstance(delta, dict):
                    continue
                row = deepcopy(_minimap_hero_registry.get(object_id, {}).get("row", {}))
                row.update(deepcopy(delta))
                raw = next((str(row.get(f)) for f in ("name", "unitname")
                            if row.get(f) and str(row.get(f)).startswith("npc_dota_hero_")), None)
                # If an object is reused for a non-hero, remove its stale hero identity.
                explicit_name = delta.get("name") or delta.get("unitname")
                if explicit_name and not str(explicit_name).startswith("npc_dota_hero_"):
                    _minimap_hero_registry.pop(object_id, None)
                    continue
                if raw:
                    _minimap_hero_registry[object_id] = {"row": row, "last_seen_seq": seq}
        return seq


def current_registry_draft(data, player_hero=None, manual=None):
    """Build the current roster from the five most-recent unique heroes per team."""
    player = data.get("player") or {}
    raw_team = str(player.get("team_name") or player.get("team") or "").lower()
    player_team = 2 if raw_team in {"radiant", "2"} else 3 if raw_team in {"dire", "3"} else None
    candidates = {"allies": [], "enemies": []}
    with _lock:
        entries = [(oid, deepcopy(v)) for oid, v in _minimap_hero_registry.items()]
    for oid, entry in entries:
        row = entry.get("row") or {}
        raw = next((str(row.get(f)) for f in ("name", "unitname")
                    if row.get(f) and str(row.get(f)).startswith("npc_dota_hero_")), None)
        if not raw:
            continue
        hero = clean_hero_name(raw)
        team = row.get("team")
        try: team = int(team) if team is not None else None
        except (TypeError, ValueError): team = None
        image = str(row.get("image") or "").lower()
        if hero == clean_hero_name(player_hero) or "herocircle_self" in image:
            side = "allies"
            if player_team is None and team in (2, 3): player_team = team
        elif player_team is not None and team in (2, 3):
            side = "allies" if team == player_team else "enemies"
        elif "enemyicon" in image:
            side = "enemies"
        elif "herocircle" in image:
            side = "allies"
        else:
            continue
        candidates[side].append((entry.get("last_seen_seq", 0), hero, oid))

    result = {"allies": [], "enemies": []}
    for side in ("allies", "enemies"):
        for _, hero, _ in sorted(candidates[side], reverse=True):
            if hero and hero not in result[side]:
                result[side].append(hero)
            if len(result[side]) == 5:
                break
        result[side].reverse()  # stable-ish display order while retaining recent-five selection
    if player_hero:
        ph = clean_hero_name(player_hero)
        if ph and ph not in result["allies"]:
            result["allies"] = ([ph] + result["allies"])[:5]
    manual = manual or {}
    for side in ("allies", "enemies"):
        for hero in manual.get(side, []):
            hero = clean_hero_name(hero)
            if hero and hero not in result[side]: result[side].append(hero)
        result[side] = result[side][:5]
    result["source"] = "hybrid" if (manual.get("allies") or manual.get("enemies")) else ("gsi" if result["allies"] or result["enemies"] else "waiting_for_gsi")
    return result


def extract_packet_hero_identities(packet, player_hero=None):
    """Harvest hero identities from current/added/previously minimap branches.

    Identity is safe to remember once GSI exposed it. Coordinates are ignored.
    This also bootstraps correctly when the assistant starts mid-match.
    """
    player = packet.get("player") or {}
    raw_team = str(player.get("team_name") or player.get("team") or "").lower()
    player_team = 2 if raw_team in {"radiant", "2"} else 3 if raw_team in {"dire", "3"} else None
    out = {"allies": [], "enemies": []}

    def add(side, raw):
        name = clean_hero_name(raw)
        if name and name not in out[side]:
            out[side].append(name)

    branches = [packet]
    for meta in ("added", "previously"):
        if isinstance(packet.get(meta), dict):
            branches.append(packet[meta])
    for branch in branches:
        mm = branch.get("minimap") or {}
        if not isinstance(mm, dict):
            continue
        for row in mm.values():
            if not isinstance(row, dict):
                continue
            raw = next((str(row.get(f)) for f in ("name", "unitname")
                        if row.get(f) and str(row.get(f)).startswith("npc_dota_hero_")), None)
            if not raw:
                continue
            team = row.get("team")
            try: team = int(team) if team is not None else None
            except (TypeError, ValueError): team = None
            image = str(row.get("image") or "").lower()
            hero = clean_hero_name(raw)
            if hero == clean_hero_name(player_hero) or "herocircle_self" in image:
                add("allies", raw)
            elif player_team is not None and team in (2, 3):
                add("allies" if team == player_team else "enemies", raw)
            elif "enemyicon" in image:
                add("enemies", raw)
            elif "herocircle" in image:
                add("allies", raw)
    return out


HERO_NAME_ALIASES = {
    "Necrolyte": "Necrophos",
    "Vengefulspirit": "Vengeful Spirit",
    "Nevermore": "Shadow Fiend",
    "Skeleton King": "Wraith King",
    "Windrunner": "Windranger",
    "Obsidian Destroyer": "Outworld Destroyer",
    "Furion": "Nature's Prophet",
    "Rattletrap": "Clockwerk",
    "Magnataur": "Magnus",
    "Shredder": "Timbersaw",
    "Zuus": "Zeus",
    "Wisp": "Io",
    "Doom Bringer": "Doom",
    "Life Stealer": "Lifestealer",
    "Queenofpain": "Queen of Pain",
    "Treant": "Treant Protector",
    "Centaur": "Centaur Warrunner",
    "Abyssal Underlord": "Underlord",
}

def clean_hero_name(name):
    if not name:
        return None
    cleaned = str(name).replace("npc_dota_hero_", "").replace("_", " ").title()
    return HERO_NAME_ALIASES.get(cleaned, cleaned)


def extract_draft(data, player_hero=None, manual=None):
    """Extract hero identities directly from the CURRENT GSI minimap snapshot.

    Team 2 = Radiant, team 3 = Dire. We determine the player's team from
    player.team_name, then classify every minimap row whose name/unitname is
    npc_dota_hero_*. This intentionally does not inspect `previously`/`added`.
    """
    allies, enemies = [], []
    player = data.get("player") or {}
    raw_team = str(player.get("team_name") or player.get("team") or "").lower()
    player_team = 2 if raw_team in {"radiant", "2"} else 3 if raw_team in {"dire", "3"} else None

    def add(bucket, raw_name):
        name = clean_hero_name(raw_name)
        if name and name not in bucket:
            bucket.append(name)

    minimap = data.get("minimap") or {}
    if isinstance(minimap, dict):
        for row in minimap.values():
            if not isinstance(row, dict):
                continue
            raw_name = None
            for field in ("name", "unitname"):
                value = row.get(field)
                if value and str(value).startswith("npc_dota_hero_"):
                    raw_name = str(value)
                    break
            if not raw_name:
                continue

            team = row.get("team")
            try:
                team = int(team) if team is not None else None
            except (TypeError, ValueError):
                team = None
            image = str(row.get("image") or "").lower()

            # Self is always allied and can also recover our team.
            if "herocircle_self" in image or clean_hero_name(raw_name) == clean_hero_name(player_hero):
                add(allies, raw_name)
                if player_team is None and team in (2, 3):
                    player_team = team
                continue

            if player_team is not None and team in (2, 3):
                add(allies if team == player_team else enemies, raw_name)
            elif "enemyicon" in image:
                add(enemies, raw_name)
            elif "herocircle" in image:
                add(allies, raw_name)

    # Own hero can exist before minimap hero rows appear.
    if player_hero:
        add(allies, player_hero)

    manual = manual or {}
    for name in manual.get("allies", []):
        add(allies, name)
    for name in manual.get("enemies", []):
        add(enemies, name)

    source = "hybrid" if (manual.get("allies") or manual.get("enemies")) else ("gsi" if allies or enemies else "waiting_for_gsi")
    return {"allies": allies[:5], "enemies": enemies[:5], "source": source}


def summarize_structures(buildings):
    """Compact tower/rax state. Missing enemy structures are never inferred."""
    out = {}
    for team, rows in (buildings or {}).items():
        if not isinstance(rows, dict):
            continue
        team_out = {}
        for name, st in rows.items():
            if not isinstance(st, dict):
                continue
            hp, mx = st.get("health"), st.get("max_health")
            team_out[name] = {"health": hp, "max_health": mx, "alive": (hp or 0) > 0 if hp is not None else None,
                              "health_percent": pct(hp, mx)}
        out[team] = team_out
    return out


def summarize_minimap(minimap, player_team, player_hero):
    """Hero identities are useful; enemy coordinates stay quarantined until fog behavior is validated."""
    def norm_team(v):
        x = str(v or "").lower()
        if x in {"2", "radiant"}: return "radiant"
        if x in {"3", "dire"}: return "dire"
        return x or None
    pt = norm_team(player_team)
    # Minimap can contain multiple object IDs for the same hero over time.  Keep
    # exactly one current display marker per unique hero, capped at five/team.
    by_side = {"allies": {}, "enemies": {}}
    for row in (minimap or {}).values():
        if not isinstance(row, dict):
            continue
        raw = next((v for v in (row.get("name"), row.get("unitname"))
                    if v and "npc_dota_hero_" in str(v)), "")
        if not raw:
            continue
        hero_name = clean_hero_name(raw)
        team = norm_team(row.get("team"))
        if not hero_name or not pt or team not in {"radiant", "dire"}:
            continue
        side = "allies" if team == pt else "enemies"
        rec = {"hero": hero_name, "image": row.get("image"), "team": team}
        pos = {"x": row.get("xpos"), "y": row.get("ypos")}
        if side == "allies":
            rec["position"] = pos
        else:
            # Enemy coordinates are exposed ONLY for dashboard rendering.
            # Strategic/GPT context continues to receive enemy identities only.
            rec["display_position"] = pos
        by_side[side][hero_name] = rec

    allies = list(by_side["allies"].values())[:5]
    enemies = list(by_side["enemies"].values())[:5]
    return {"allies": allies, "enemies": enemies,
            "enemy_positions_policy": "display_only_visibility_unverified_do_not_use"}



def current_visible_enemy_positions(packet, player_team, player_hero=None):
    """Return enemy hero positions evidenced by the CURRENT GSI minimap packet only.

    This deliberately does not read the accumulated GSI snapshot, the minimap hero
    registry, `added`, or `previously`. A position is eligible for strategic reasoning
    only when the current packet itself exposes that enemy hero with finite xpos/ypos.
    This is conservative: it may omit usable positions, but it never promotes a stale
    remembered coordinate into AI context.
    """
    def norm_team(v):
        x = str(v or "").lower()
        if x in {"2", "radiant"}: return "radiant"
        if x in {"3", "dire"}: return "dire"
        return None

    pt = norm_team(player_team)
    if pt is None:
        return []
    out = {}
    minimap = packet.get("minimap") or {}
    if not isinstance(minimap, dict):
        return []
    for row in minimap.values():
        if not isinstance(row, dict):
            continue
        raw = next((v for v in (row.get("name"), row.get("unitname"))
                    if v and str(v).startswith("npc_dota_hero_")), None)
        if not raw:
            continue
        team = norm_team(row.get("team"))
        if team is None or team == pt:
            continue
        hero = clean_hero_name(raw)
        if hero == clean_hero_name(player_hero):
            continue
        x, y = row.get("xpos"), row.get("ypos")
        try:
            x, y = float(x), float(y)
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(x) and math.isfinite(y)):
            continue
        out[hero] = {"hero": hero, "team": team, "position": {"x": x, "y": y},
                     "visibility": "current_gsi_minimap"}
    return list(out.values())[:5]

def current_lane_wave_pressure(packet, player_team):
    """Derive compact lane-wave pressure from CURRENT GSI minimap creeps only.

    This is deliberately current-packet-only: no accumulated/previous creep positions are
    used. Lane classification uses map geometry (y-x) and pressure uses progression along
    the Radiant->Dire diagonal (x+y). It is a coarse strategic signal, not exact lane state.
    """
    def norm_team(v):
        x = str(v or "").lower()
        if x in {"2", "radiant"}: return "radiant"
        if x in {"3", "dire"}: return "dire"
        return None

    pt = norm_team(player_team)
    lanes = {k: {"radiant": [], "dire": []} for k in ("top", "mid", "bot")}
    minimap = packet.get("minimap") or {}
    if not isinstance(minimap, dict):
        return {"source": "current_gsi_minimap", "lanes": {}, "confidence": "LOW"}

    for row in minimap.values():
        if not isinstance(row, dict):
            continue
        unit = str(row.get("unitname") or "")
        image = str(row.get("image") or "")
        # Lane creeps only. Exclude neutrals, summons, heroes, couriers and buildings.
        if not ("npc_dota_creep_goodguys_" in unit or "npc_dota_creep_badguys_" in unit or
                "npc_dota_goodguys_siege" in unit or "npc_dota_badguys_siege" in unit):
            continue
        team = norm_team(row.get("team"))
        if team not in {"radiant", "dire"}:
            continue
        try:
            x, y = float(row.get("xpos")), float(row.get("ypos"))
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(x) and math.isfinite(y)) or (x == 0 and y == 0):
            continue
        delta = y - x
        lane = "top" if delta > 1800 else "bot" if delta < -1800 else "mid"
        progress = (x + y) / 2.0  # negative=Radiant side, positive=Dire side
        lanes[lane][team].append(progress)

    def territory(v):
        if v <= -3000: return "RADIANT_DEEP"
        if v <= -900: return "RADIANT_SIDE"
        if v < 900: return "RIVER_CENTER"
        if v < 3000: return "DIRE_SIDE"
        return "DIRE_DEEP"

    out = {}
    for lane, sides in lanes.items():
        r, d = sides["radiant"], sides["dire"]
        # Leading creeps: Radiant advances toward larger progress; Dire toward smaller.
        rf = max(r) if r else None
        df = min(d) if d else None
        if rf is not None and df is not None:
            center = (rf + df) / 2.0
            confidence = "HIGH" if len(r) >= 2 and len(d) >= 2 else "MEDIUM"
        elif rf is not None:
            center, confidence = rf, "LOW"
        elif df is not None:
            center, confidence = df, "LOW"
        else:
            continue
        area = territory(center)
        if pt == "radiant":
            player_view = {
                "RADIANT_DEEP": "PUSHED_DEEP_INTO_OUR_SIDE",
                "RADIANT_SIDE": "PUSHED_INTO_OUR_SIDE",
                "RIVER_CENTER": "NEAR_CENTER",
                "DIRE_SIDE": "PUSHED_INTO_ENEMY_SIDE",
                "DIRE_DEEP": "PUSHED_DEEP_INTO_ENEMY_SIDE",
            }[area]
        elif pt == "dire":
            player_view = {
                "RADIANT_DEEP": "PUSHED_DEEP_INTO_ENEMY_SIDE",
                "RADIANT_SIDE": "PUSHED_INTO_ENEMY_SIDE",
                "RIVER_CENTER": "NEAR_CENTER",
                "DIRE_SIDE": "PUSHED_INTO_OUR_SIDE",
                "DIRE_DEEP": "PUSHED_DEEP_INTO_OUR_SIDE",
            }[area]
        else:
            player_view = area
        out[lane] = {
            "state": player_view,
            "absolute_territory": area,
            "confidence": confidence,
            "radiant_creeps_seen": len(r),
            "dire_creeps_seen": len(d),
        }
    return {
        "source": "current_gsi_minimap_only",
        "lanes": out,
        "rule": "Coarse current wave-pressure signal only; do not infer hidden heroes or future wave position.",
    }


def build_observation(data):
    game_map = data.get("map", {})
    player = data.get("player", {})
    hero = data.get("hero", {})

    hero_name = clean_hero_name(hero.get("name"))
    with _lock:
        manual = deepcopy(_state.get("manual_draft") or {})
    draft = extract_draft(data, hero_name, manual)

    return {
        "game": {
            "state": game_map.get("game_state"),
            "game_time": game_map.get("game_time"),
            "clock_time": game_map.get("clock_time"),
            "radiant_score": game_map.get("radiant_score"),
            "dire_score": game_map.get("dire_score"),
            "daytime": game_map.get("daytime"),
            "paused": game_map.get("paused"),
            "win_team": game_map.get("win_team"),
        },
        "hero": {
            "name": hero_name,
            "level": hero.get("level"),
            "alive": hero.get("alive"),
            "health": hero.get("health"),
            "max_health": hero.get("max_health"),
            "health_percent": hero.get("health_percent") or pct(hero.get("health"), hero.get("max_health")),
            "mana": hero.get("mana"),
            "max_mana": hero.get("max_mana"),
            "mana_percent": hero.get("mana_percent") or pct(hero.get("mana"), hero.get("max_mana")),
            "respawn_seconds": hero.get("respawn_seconds"),
            "buyback_cost": hero.get("buyback_cost"),
            "buyback_cooldown": hero.get("buyback_cooldown"),
            "position": {"x": hero.get("xpos"), "y": hero.get("ypos")},
            "statuses": {k: hero.get(k) for k in ("silenced", "stunned", "disarmed", "magicimmune", "hexed", "muted", "break", "smoked", "has_debuff")},
            "aghanims_scepter": hero.get("aghanims_scepter"),
            "aghanims_shard": hero.get("aghanims_shard"),
        },
        "player": {
            "activity": player.get("activity"),
            "kills": player.get("kills"),
            "deaths": player.get("deaths"),
            "assists": player.get("assists"),
            "gold": player.get("gold"),
            "last_hits": player.get("last_hits"),
            "denies": player.get("denies"),
            "gpm": player.get("gpm"),
            "xpm": player.get("xpm"),
            "gold_reliable": player.get("gold_reliable"),
            "gold_unreliable": player.get("gold_unreliable"),
            "gold_from_hero_kills": player.get("gold_from_hero_kills"),
            "gold_from_creep_kills": player.get("gold_from_creep_kills"),
            "gold_from_income": player.get("gold_from_income"),
            "gold_from_shared": player.get("gold_from_shared"),
            "team_name": player.get("team_name"),
        },
        "items": parse_items(data.get("items", {})),
        "abilities": parse_abilities(data.get("abilities", {})),
        "draft": draft,
        "structures": summarize_structures(data.get("buildings") or {}),
        "map_context": summarize_minimap(data.get("minimap") or {}, player.get("team_name"), hero_name),
        "events": (data.get("events") or []) if isinstance(data.get("events"), list) else [],
    }


def readiness_context(obs):
    abilities = []
    for a in obs.get("abilities") or []:
        if a.get("level", 0) and (a.get("ultimate") or not a.get("can_cast") or (a.get("cooldown") or 0) > 0):
            abilities.append({k: a.get(k) for k in ("name", "level", "cooldown", "can_cast", "ultimate")})
    items = [{k: i.get(k) for k in ("name", "cooldown", "charges", "can_cast")} for i in obs.get("items") or []]
    return {"key_abilities": abilities, "items": items, "alive": obs.get("hero", {}).get("alive"),
            "hp_percent": obs.get("hero", {}).get("health_percent"), "mana_percent": obs.get("hero", {}).get("mana_percent"),
            "buyback_cost": obs.get("hero", {}).get("buyback_cost"), "buyback_cooldown": obs.get("hero", {}).get("buyback_cooldown")}


def pace_trajectory(current, history):
    points = [x.get("pace") for x in history if x.get("pace") and x["pace"].get("overall_percentile") is not None]
    if current.get("available") and current.get("overall_percentile") is not None:
        points.append({"minute": current.get("minute"), "overall_percentile": current.get("overall_percentile")})
    # Deduplicate benchmark minutes, keeping latest observation.
    by_min = {p.get("minute"): p for p in points if p.get("minute") is not None}
    pts = [by_min[k] for k in sorted(by_min)][-4:]
    trend = "INSUFFICIENT_HISTORY"
    if len(pts) >= 2:
        d = pts[-1]["overall_percentile"] - pts[0]["overall_percentile"]
        trend = "IMPROVING" if d >= 8 else "DECLINING" if d <= -8 else "STABLE"
    # Never retain the full `current` dict here. The caller attaches this
    # trajectory back onto `pace`, so storing `current` itself would create
    # pace -> trajectory -> current -> pace and Flask jsonify() would fail
    # with "ValueError: Circular reference detected".
    current_point = None
    if current.get("available") and current.get("overall_percentile") is not None:
        current_point = {
            "minute": current.get("minute"),
            "overall_percentile": current.get("overall_percentile"),
            "status": current.get("status"),
        }
    return {"current": current_point, "trend": trend, "checkpoints": pts}


def strategic_context(obs, pace, history):
    structures = obs.get("structures") or {}
    destroyed = {}
    for team, rows in structures.items():
        destroyed[team] = [name for name, st in rows.items() if st.get("alive") is False]
    return {
        "economy": {**{k: obs.get("player", {}).get(k) for k in ("gpm", "xpm", "last_hits", "denies", "gold", "gold_from_creep_kills", "gold_from_hero_kills")},
                    "immortal_trajectory": pace_trajectory(pace, history)},
        "map_and_structures": {"self_position": obs.get("hero", {}).get("position"), "allies": obs.get("map_context", {}).get("allies", []),
                               "enemy_heroes": [x.get("hero") for x in obs.get("map_context", {}).get("enemies", [])],
                               "visible_enemy_positions": obs.get("map_context", {}).get("visible_enemy_positions", []),
                               "enemy_positions_policy": "USE ONLY visible_enemy_positions; current GSI minimap evidence only; never use remembered/display-only enemy coordinates",
                               "structure_state": ai_structure_state(obs),
                               "structure_rule": "Authoritative: own team from GSI, enemy team from manual dashboard. MUST affect win probability and strategy.",
                               "lane_wave_pressure": obs.get("lane_wave_pressure") or {},
                               "wave_rule": "Current GSI minimap lane creeps only; use as coarse map-pressure/farm/objective context, never as hidden hero evidence."},
        "power_and_readiness": readiness_context(obs),
        "score_and_time": obs.get("game"),
        "recent_events": obs.get("events", []),
    }



def compact_event(event):
    """Turn GSI generic_event payloads into a safe compact timeline row."""
    if not isinstance(event, dict):
        return None
    row = {"game_time": event.get("game_time"), "event_type": event.get("event_type")}
    data = event.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (json.JSONDecodeError, TypeError):
            data = {"raw": data[:160]}
    if isinstance(data, dict):
        row["type"] = data.get("type")
        row["playerid1"] = data.get("playerid1")
        row["playerid2"] = data.get("playerid2")
        row["value"] = data.get("value")
    return row


def recent_compact_events(events, limit=5):
    """Return the newest compact GSI events in chronological order.

    Do not assume the incoming GSI event array is perfectly ordered. The
    frontend reverses this five-row list so the newest event is shown first.
    """
    rows = []
    for idx, event in enumerate(events or []):
        row = compact_event(event)
        if not row:
            continue
        gt = row.get("game_time")
        timed = isinstance(gt, (int, float))
        rows.append((timed, gt if timed else float("-inf"), idx, row))
    rows.sort(key=lambda x: (x[0], x[1], x[2]))
    return [x[3] for x in rows[-max(0, int(limit)):]]


def objective_rows(structures, player_team=None, manual_enemy=None):
    """Team-aware dashboard structure board.

    Radiant/Dire remain absolute teams.  The player's own side may expose
    structure health through GSI `buildings`, so the dashboard shows HP there.
    The opposing side intentionally exposes only public ALIVE/DESTROYED status;
    enemy HP is never shown or inferred.  Every structure starts alive because
    that is known at match start, then explicit GSI evidence may overwrite it.

    IMPORTANT: display defaults are UI state only. Strategic reasoning continues
    to use raw GSI-derived `structures`, so an unobserved enemy structure is not
    treated as evidence by the AI.
    """
    def norm_team(value):
        x = str(value or "").lower()
        if "radiant" in x or x == "2": return "radiant"
        if "dire" in x or x == "3": return "dire"
        return None

    own_team = norm_team(player_team)

    def standing(team):
        return {"alive": True, "health": None, "max_health": None,
                "health_percent": None, "display_default": True,
                "info_mode": "health" if team == own_team else "status"}

    teams = {
        team: {
            lane: {"t1": standing(team), "t2": standing(team), "t3": standing(team),
                   "melee": standing(team), "range": standing(team)}
            for lane in ("top", "mid", "bot")
        }
        for team in ("radiant", "dire")
    }

    for raw_team, rows in (structures or {}).items():
        team = norm_team(raw_team)
        if team not in teams or not isinstance(rows, dict):
            continue
        lanes = teams[team]

        # Live-client evidence from V38 showed that the exposed building branch
        # behaves as a full list of structures that still exist: a destroyed
        # tower reaches health=0, then is removed from the branch.  Therefore,
        # once we have an exposed snapshot for the PLAYER'S OWN absolute team,
        # an expected tower/rax missing from that snapshot must stay DESTROYED
        # on the dashboard.  This also makes a mid-match app start reconstruct
        # already-destroyed own structures correctly instead of reverting them
        # to the match-start ALIVE display default.
        if team == own_team and rows:
            present = {str(name).lower() for name in rows}
            for lane in ("top", "mid", "bot"):
                expected = {
                    "t1": f"tower1_{lane}", "t2": f"tower2_{lane}",
                    "t3": f"tower3_{lane}", "melee": f"rax_melee_{lane}",
                    "range": f"rax_range_{lane}",
                }
                for slot, token in expected.items():
                    if not any(token in name for name in present):
                        lanes[lane][slot] = {
                            "alive": False, "health": 0, "max_health": None,
                            "health_percent": 0, "display_default": False,
                            "info_mode": "health", "inferred_from_exposed_snapshot_absence": True,
                        }

        for name, st in rows.items():
            if not isinstance(st, dict):
                continue
            shown = dict(st)
            shown["display_default"] = False
            shown["info_mode"] = "health" if team == own_team else "status"
            # Never leak/infer opponent structure HP into the dashboard.  If
            # future GSI versions expose an opponent row, retain only its
            # public alive/destroyed state.
            if team != own_team:
                shown["health"] = None
                shown["max_health"] = None
                shown["health_percent"] = None
            lname = str(name).lower()
            for lane in lanes:
                if f"tower1_{lane}" in lname: lanes[lane]["t1"] = shown
                elif f"tower2_{lane}" in lname: lanes[lane]["t2"] = shown
                elif f"tower3_{lane}" in lname: lanes[lane]["t3"] = shown
                elif f"rax_melee_{lane}" in lname: lanes[lane]["melee"] = shown
                elif f"rax_range_{lane}" in lname: lanes[lane]["range"] = shown

    # Manual opponent status is explicit user-provided public game knowledge.
    # It affects only the opponent status board; it never fabricates HP.
    enemy_team = "dire" if own_team == "radiant" else "radiant" if own_team == "dire" else None
    manual_enemy = manual_enemy or {}
    if enemy_team and isinstance(manual_enemy, dict):
        for lane in ("top", "mid", "bot"):
            row = manual_enemy.get(lane) or {}
            for slot in ("t1", "t2", "t3"):
                if slot in row:
                    alive = bool(row[slot])
                    teams[enemy_team][lane][slot] = {"alive": alive, "health": None, "max_health": None,
                        "health_percent": None, "display_default": False, "info_mode": "status", "manual": True}
            if "rax_alive" in row:
                n = max(0, min(2, int(row.get("rax_alive", 2))))
                teams[enemy_team][lane]["manual_rax_alive"] = n
    return {"player_team": own_team, "teams": teams}



def ai_structure_state(obs):
    """Authoritative AI structure summary: own=GSI, enemy=manual dashboard."""
    p = obs.get("player") or {}
    own = str(p.get("team_name") or "").lower()
    if own not in {"radiant", "dire"}:
        own = None
    enemy = "dire" if own == "radiant" else "radiant" if own == "dire" else None
    board = objective_rows(obs.get("structures") or {}, own, obs.get("manual_enemy_structures") or {})
    teams = board.get("teams") or {}
    def summarize(team):
        lanes = teams.get(team) or {}
        towers = {}
        for slot in ("t1", "t2", "t3"):
            vals = [((lanes.get(l) or {}).get(slot) or {}).get("alive") for l in ("top", "mid", "bot")]
            towers[slot] = {"alive": sum(v is not False for v in vals),
                            "destroyed": sum(v is False for v in vals), "total": 3}
        rax_alive = 0
        for lane in ("top", "mid", "bot"):
            row = lanes.get(lane) or {}
            if team == enemy and row.get("manual_rax_alive") is not None:
                rax_alive += max(0, min(2, int(row["manual_rax_alive"])))
            else:
                rax_alive += sum(((row.get(k) or {}).get("alive") is not False) for k in ("melee", "range"))
        return {"towers": towers, "rax": {"alive": rax_alive, "destroyed": 6-rax_alive, "total": 6}}
    out = {"player_team": own, "enemy_team": enemy,
           "policy": "Own structures = GSI. Enemy structures = manual dashboard input. Never infer enemy structures from minimap."}
    if own: out[own] = {"source": "gsi", **summarize(own)}
    if enemy: out[enemy] = {"source": "manual_user_input", **summarize(enemy)}
    return out

def power_state(obs):
    h = obs.get("hero") or {}
    abilities = obs.get("abilities") or []
    items = obs.get("items") or []
    ultimate = next((a for a in abilities if a.get("ultimate") and (a.get("level") or 0) > 0), None)
    active_abilities = [a for a in abilities if (a.get("level") or 0) > 0 and not a.get("ultimate") and (a.get("cooldown") or 0) > 0]
    key_items = [i for i in items if i.get("raw_name") in {"item_black_king_bar","item_blink","item_swift_blink","item_overwhelming_blink","item_arcane_blink","item_satanic","item_manta","item_abyssal_blade"}]
    tp = next((i for i in items if i.get("raw_name") in {"item_tpscroll","item_travel_boots","item_travel_boots_2"}), None)
    hp = h.get("health_percent")
    mana = h.get("mana_percent")
    key_items_ready = all((i.get("cooldown") or 0) <= 0 for i in key_items)
    ult_ready = bool(ultimate and ultimate.get("can_cast"))
    # Five-level local readiness scale. This is intentionally independent from economy.
    if h.get("alive") is False or (hp is not None and hp < 20):
        fight = "VERY WEAK"
    elif hp is not None and hp < 45:
        fight = "WEAK"
    elif ult_ready and key_items_ready and (hp is None or hp >= 80) and (mana is None or mana >= 60):
        fight = "VERY STRONG"
    elif (ult_ready or not ultimate) and key_items_ready and (hp is None or hp >= 60):
        fight = "STRONG"
    else:
        fight = "PARTIAL"
    return {
        "fight_state": fight, "hp_percent": hp, "mana_percent": h.get("mana_percent"),
        "ultimate": ultimate, "cooldowns": active_abilities[:3], "key_items": key_items[:4], "tp": tp,
        "alive": h.get("alive"), "respawn_seconds": h.get("respawn_seconds"),
        "buyback_cost": h.get("buyback_cost"), "buyback_cooldown": h.get("buyback_cooldown"),
    }


def _structure_objective(obs):
    """Return a specific structure objective only when GSI state supports it."""
    player = obs.get("player") or {}
    team = str(player.get("team_name") or player.get("team") or "").lower()
    enemy = "dire" if ("radiant" in team or team == "2") else "radiant" if ("dire" in team or team == "3") else None
    rows = (obs.get("structures") or {}).get(enemy, {}) if enemy else {}
    if not isinstance(rows, dict) or not rows:
        return None

    def states(kind):
        out = []
        for name, st in rows.items():
            if kind in str(name).lower() and isinstance(st, dict):
                out.append(st.get("alive"))
        return out

    t2, t3 = states("tower2_"), states("tower3_")
    rax = states("rax_")
    # All enemy T2 explicitly gone + at least one T3 explicitly alive => high-ground window.
    if t2 and all(x is False for x in t2) and any(x is True for x in t3):
        return "HIGH GROUND"
    # A T3 explicitly down while an enemy barracks is explicitly alive => barracks is the next structure target.
    if any(x is False for x in t3) and any(x is True for x in rax):
        return "BARRACKS"
    return "PUSH TOWER"


def cockpit_state(obs, pace):
    g, p = obs.get("game") or {}, obs.get("player") or {}
    allies = obs.get("map_context", {}).get("allies", [])
    trajectory = pace.get("trajectory") or {}
    percentile = pace.get("overall_percentile")
    economy = economy_status_from_percentile(percentile) if percentile is not None else "BENCHMARK PENDING"
    power = power_state(obs)
    # Position confidence remains conservative; current-visible enemy positions are
    # separately supplied to the AI strategic context.
    map_state = "POSITION DATA LIMITED"

    # Priority is a coarse local objective signal, not an instruction that overrides GPT.
    # Only use objectives that can be justified from current local state.
    if power["fight_state"] == "VERY WEAK":
        priority = "RESET / REGEN"
    elif economy == "VERY BEHIND":
        priority = "OBJECTIVE (NEXT ITEM)"
    elif power["fight_state"] == "VERY STRONG":
        priority = f"OBJECTIVE ({_structure_objective(obs) or 'PUSH TOWER'})"
    elif power["fight_state"] == "STRONG":
        priority = "FIGHT / POWER SPIKE"
    elif economy == "BEHIND":
        priority = "OBJECTIVE (NEXT ITEM)"
    else:
        priority = "FARM / TIMING"
    return {
        "strategic_state": {
            "economy": economy, "economy_trend": trajectory.get("trend"),
            "power": power.get("fight_state"), "map": map_state, "priority": priority,
            "score": f"{g.get('radiant_score', 0)}–{g.get('dire_score', 0)}",
        },
        "power": power,
        "objectives": objective_rows(obs.get("structures") or {}, p.get("team_name"), obs.get("manual_enemy_structures") or {}),
        "map": {"allies": allies, "enemies": obs.get("map_context", {}).get("enemies", []),
                "daytime": g.get("daytime"),
                "wave_pressure": obs.get("lane_wave_pressure") or {},
                "enemy_positions_policy": obs.get("map_context", {}).get("enemy_positions_policy")},
        "events": recent_compact_events(obs.get("events") or [], 5),
    }

def is_analyzable(obs):
    return bool(
        obs.get("hero", {}).get("name")
        and obs.get("game", {}).get("state") == "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS"
    )


def ai_payload(obs):
    """Build a compact strategic payload for OpenAI instead of sending raw GSI-derived state.

    The dashboard still keeps the richer local observation. The model receives only
    fields that can materially change a Position-1 decision. Enemy positions are included
    only when the CURRENT GSI minimap packet exposes them as visible_enemy_positions.
    """
    g = obs.get("game") or {}
    h = obs.get("hero") or {}
    p = obs.get("player") or {}
    draft = obs.get("draft") or {}

    items = []
    for item in obs.get("items") or []:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        row = {"name": item.get("name")}
        for key in ("cooldown", "charges", "can_cast"):
            value = item.get(key)
            if value not in (None, 0, False, ""):
                row[key] = value
        items.append(row)

    abilities = []
    for ability in obs.get("abilities") or []:
        if not isinstance(ability, dict) or not ability.get("name") or not ability.get("level"):
            continue
        # Normal ready basic spells add little strategic value. Keep ultimates and
        # abilities that are currently unavailable/on cooldown.
        if not (ability.get("ultimate") or (ability.get("cooldown") or 0) > 0 or ability.get("can_cast") is False):
            continue
        abilities.append({k: ability.get(k) for k in ("name", "level", "cooldown", "can_cast", "ultimate") if ability.get(k) is not None})

    structures = {}
    for team, rows in (obs.get("structures") or {}).items():
        changed = {}
        for name, st in (rows or {}).items():
            if not isinstance(st, dict):
                continue
            hp = st.get("health_percent")
            if st.get("alive") is False or (hp is not None and hp < 100):
                changed[name] = {k: st.get(k) for k in ("alive", "health_percent") if st.get(k) is not None}
        if changed:
            structures[team] = changed

    events = recent_compact_events(obs.get("events") or [], 5)
    return {
        "time": {k: g.get(k) for k in ("clock_time", "radiant_score", "dire_score", "daytime", "paused") if g.get(k) is not None},
        "player_team": p.get("team_name"),
        "score_from_player_perspective": {
            "own": g.get("radiant_score") if str(p.get("team_name") or "").lower() == "radiant" else g.get("dire_score") if str(p.get("team_name") or "").lower() == "dire" else None,
            "enemy": g.get("dire_score") if str(p.get("team_name") or "").lower() == "radiant" else g.get("radiant_score") if str(p.get("team_name") or "").lower() == "dire" else None,
        },
        "hero": {k: h.get(k) for k in ("name", "level", "alive", "health_percent", "mana_percent", "respawn_seconds", "buyback_cost", "buyback_cooldown", "aghanims_scepter", "aghanims_shard") if h.get(k) is not None},
        "carry": {k: p.get(k) for k in ("kills", "deaths", "assists", "gold", "last_hits", "denies", "gpm", "xpm") if p.get(k) is not None},
        "items": items,
        "key_abilities": abilities,
        "draft": {"allies": draft.get("allies", []), "enemies": draft.get("enemies", [])},
        "visible_enemy_positions": (obs.get("map_context") or {}).get("visible_enemy_positions", []),
        "enemy_position_rule": "Only these current-GSI-visible positions may be used for location/safety/rotation reasoning; absence does not imply location.",
        "changed_structures_gsi_detail": structures,
        "structure_state": ai_structure_state(obs),
        "structure_rule": "Use structure_state for win probability: own=GSI, enemy=manual dashboard input.",
        "lane_wave_pressure": obs.get("lane_wave_pressure") or {},
        "lane_wave_rule": "Derived only from lane creeps present in the CURRENT GSI minimap packet. Use it for current map pressure, safe-farm opportunity, objective/high-ground setup and win probability; do not infer hidden hero locations from it.",
        "recent_events": events,
    }



def usage_value(obj, *names):
    if obj is None:
        return 0
    for name in names:
        value = getattr(obj, name, None)
        if value is None and isinstance(obj, dict):
            value = obj.get(name)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                pass
    return 0


def refresh_costs_locked():
    u = _state["usage"]
    u["llm_cost_usd"] = (u["input_tokens"] / 1_000_000 * LUNA_INPUT_USD_PER_M) + (u["output_tokens"] / 1_000_000 * LUNA_OUTPUT_USD_PER_M)
    u["tts_cost_usd"] = 0.0  # Local Kokoro TTS has no API charge.
    u["total_cost_usd"] = u["llm_cost_usd"] + u["tts_cost_usd"]
    u["total_cost_idr"] = u["total_cost_usd"] * USD_TO_IDR


def record_history_locked(obs):
    g, h, p = obs.get("game", {}), obs.get("hero", {}), obs.get("player", {})
    t = g.get("clock_time")
    if t is None or t < 0:
        return
    row = {
        "time": t, "gpm": p.get("gpm") or 0, "xpm": p.get("xpm") or 0,
        "gold": p.get("gold") or 0, "last_hits": p.get("last_hits") or 0,
        "hp": h.get("health_percent") or 0, "mana": h.get("mana_percent") or 0,
    }
    hist = _state["history"]
    if not hist or t - hist[-1]["time"] >= 5:
        hist.append(row)
        if len(hist) > 720:
            del hist[:-720]


def _item_key(item):
    return str((item or {}).get("name") or "").lower().replace("item_", "")


def _major_item_label(key):
    if not key:
        return None
    normalized = key.replace("item_", "")
    if not any(token in normalized for token in MAJOR_ITEM_KEYWORDS):
        return None
    aliases = {
        "black_king_bar": "BKB", "monkey_king_bar": "MKB", "aghanims_scepter": "Aghanim's Scepter",
        "silver_edge": "Silver Edge", "swift_blink": "Swift Blink", "overwhelming_blink": "Overwhelming Blink",
        "arcane_blink": "Arcane Blink", "eye_of_skadi": "Skadi",
    }
    return aliases.get(normalized, normalized.replace("_", " ").title())


def zero_api_event_messages(previous_obs, obs):
    """Return short local-coach messages from observable self-state deltas only."""
    global _event_coach
    p = obs.get("player") or {}
    h = obs.get("hero") or {}
    game = obs.get("game") or {}
    if game.get("state") != "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS":
        return []

    kills = int(p.get("kills") or 0)
    deaths = int(p.get("deaths") or 0)
    level = int(h.get("level") or 0)
    item_keys = {_item_key(x) for x in (obs.get("items") or []) if _item_key(x)}
    bb_cd = int(h.get("buyback_cooldown") or 0)

    if not _event_coach["initialized"]:
        _event_coach.update({"initialized": True, "kills": kills, "deaths": deaths, "level": level,
                             "items": item_keys, "buyback_cooldown": bb_cd,
                             "last_kill_monotonic": None, "multikill_count": 0})
        return []

    messages = []
    kill_delta = max(0, kills - int(_event_coach.get("kills") or 0))
    death_delta = max(0, deaths - int(_event_coach.get("deaths") or 0))
    now = time.monotonic()

    if death_delta:
        phrase = _pick_nonrepeating_phrase(DEATH_COACH_PHRASES, _event_coach.get("last_death_phrase"))
        messages.append(phrase)
        _event_coach["last_death_phrase"] = phrase
        _event_coach["multikill_count"] = 0
        _event_coach["last_kill_monotonic"] = None
    elif kill_delta:
        last = _event_coach.get("last_kill_monotonic")
        if last is not None and now - last <= 18:
            _event_coach["multikill_count"] = min(5, int(_event_coach.get("multikill_count") or 1) + kill_delta)
        else:
            _event_coach["multikill_count"] = kill_delta
        _event_coach["last_kill_monotonic"] = now
        multi = _event_coach["multikill_count"]
        if multi >= 2:
            messages.append({2: "Double kill. Keep the tempo.", 3: "Triple kill. Convert it.",
                             4: "Ultra kill. Stay sharp.", 5: "Rampage. Convert the map."}.get(multi, "Rampage. Convert the map."))
        else:
            phrase = _pick_nonrepeating_phrase(SINGLE_KILL_COACH_PHRASES, _event_coach.get("last_single_kill_phrase"))
            messages.append(phrase)
            _event_coach["last_single_kill_phrase"] = phrase

    old_level = int(_event_coach.get("level") or 0)
    for milestone in (6, 12, 18):
        if old_level < milestone <= level:
            messages.append(f"Level {milestone}. Power spike ready.")

    new_items = item_keys - set(_event_coach.get("items") or set())
    for key in sorted(new_items):
        label = _major_item_label(key)
        if label:
            messages.append(f"{label} complete. Use the timing.")
            break  # one concise item notification per GSI transition

    old_bb = int(_event_coach.get("buyback_cooldown") or 0)
    if old_bb <= 0 < bb_cd:
        messages.append("Buyback used. Protect this life.")

    _event_coach.update({"kills": kills, "deaths": deaths, "level": level,
                         "items": item_keys, "buyback_cooldown": bb_cd})
    return messages[:2]


def reset_match_locked(match_key):
    global _gsi_snapshot, _structure_registry, _minimap_hero_registry, _gsi_packet_seq, _event_coach
    _gsi_snapshot = {}
    _structure_registry = {"radiant": {}, "dire": {}}
    _minimap_hero_registry = {}
    _gsi_packet_seq = 0
    _event_coach = {"initialized": False, "kills": 0, "deaths": 0, "level": 0, "items": set(), "buyback_cooldown": 0, "last_kill_monotonic": None, "multikill_count": 0, "last_death_phrase": None, "last_single_kill_phrase": None}
    _state["match_key"] = match_key
    _state["session_phase"] = "waiting"
    _state["last_gsi_age_seconds"] = None
    _state["history"] = []
    _state["pregame_strategy"] = None
    _state["meta_strategy"] = None
    _state["live_win_probability"] = {"probability": None, "factors": [], "updated_at": None, "history": []}
    _state["performance_report"] = None
    _state["performance_report_status"] = "waiting"
    _state["performance_report_error"] = None
    _state["pregame_status"] = "waiting"
    _state["pregame_error"] = None
    _state["pregame_signature"] = None
    _state["pregame_candidate_signature"] = None
    _state["pregame_candidate_since"] = 0.0
    _state["pregame_last_spoken_signature"] = None
    _state["draft"] = {"allies": [], "enemies": [], "source": "waiting_for_gsi"}
    _state["manual_draft"] = {"allies": [], "enemies": []}
    _state["manual_enemy_structures"] = {}
    _state["known_gsi_draft"] = {"allies": [], "enemies": []}
    _state["immortal_pace"] = {"available": False, "status": "Benchmark not loaded"}
    _state["strategic_context"] = {}
    _state["cockpit"] = {}
    _state["analysis_schedule"] = {"pattern_seconds": AI_INTERVAL_PATTERN, "next_interval_seconds": AI_INTERVAL_PATTERN[0]}
    _state["usage"] = {
        "ai_calls": 0, "tts_calls": 0, "input_tokens": 0, "output_tokens": 0,
        "llm_cost_usd": 0.0,
        "tts_cost_usd": 0.0, "voice_mode": "LOCAL / OFFLINE", "total_cost_usd": 0.0, "total_cost_idr": 0.0,
        "usd_to_idr": USD_TO_IDR,
    }


def extract_voice(advice):
    match = re.search(r"^VOICE:\s*(.+)$", advice or "", flags=re.MULTILINE | re.IGNORECASE)
    return match.group(1).strip().strip('"') if match else None


def play_audio_file(path):
    """Play AI speech at the configured speed and volume."""
    ffplay = shutil.which("ffplay")
    if not ffplay:
        raise RuntimeError("ffplay is required for AI voice playback.")

    subprocess.run(
        [
            ffplay,
            "-nodisp",
            "-autoexit",
            "-loglevel", "quiet",
            "-af", f"volume={VOICE_VOLUME}",
            path,
        ],
        check=True,
    )


def speak_async(text):
    """Generate speech locally with Kokoro, then play through ffplay. No TTS API call."""
    if not VOICE_ENABLED or not text:
        return

    def worker():
        text_path = audio_path = None
        try:
            with _voice_lock:
                with _lock:
                    _state["voice_status"] = "generating_local"
                    _state["voice_error"] = None
                kokoro = shutil.which(KOKORO_BIN) if os.path.sep not in KOKORO_BIN else KOKORO_BIN
                if not kokoro or not os.path.exists(kokoro):
                    raise RuntimeError("kokoro-tts not found in the active environment")
                model_path = resolve_repo_path(KOKORO_MODEL)
                voices_path = resolve_repo_path(KOKORO_VOICES)
                if not model_path.exists() or not voices_path.exists():
                    raise RuntimeError(f"Kokoro model files missing: {model_path} / {voices_path}")
                with tempfile.NamedTemporaryFile(suffix=".txt", mode="w", encoding="utf-8", delete=False) as tf:
                    tf.write(text)
                    text_path = tf.name
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as af:
                    audio_path = af.name
                cmd = [kokoro, text_path, audio_path, "--voice", KOKORO_VOICE, "--speed", str(VOICE_SPEED), "--lang", "en-us", "--model", str(model_path), "--voices", str(voices_path)]
                proc = subprocess.run(cmd, text=True, capture_output=True)
                if proc.returncode != 0:
                    raise RuntimeError("Kokoro TTS failed: " + (proc.stderr.strip() or proc.stdout.strip() or "unknown error"))
                with _lock:
                    _state["usage"]["tts_calls"] += 1
                    refresh_costs_locked()
                    _state["voice_status"] = "playing"
                play_audio_file(audio_path)
                with _lock:
                    _state["voice_status"] = "ready"
                    _state["last_spoken"] = text
        except Exception as exc:
            with _lock:
                _state["voice_status"] = "error"
                _state["voice_error"] = str(exc)
        finally:
            for path in (text_path, audio_path):
                if path:
                    try: os.unlink(path)
                    except OSError: pass

    threading.Thread(target=worker, daemon=True).start()

def pregame_signature(draft, player_hero):
    return json.dumps({"player": player_hero, "allies": draft.get("allies", []), "enemies": draft.get("enemies", [])}, sort_keys=True)


def extract_pregame_voice(strategy):
    """Read the first pre-game analysis fields verbatim, with fixed spoken labels."""
    fields = {}
    for key in ("WIN_CONDITION", "LANE", "ITEMS", "FARM"):
        match = re.search(rf"(?mi)^{key}:\s*(.+)$", strategy or "")
        if match:
            fields[key] = match.group(1).strip()
    if len(fields) != 4:
        return None
    return (
        "Hi, I'm your Dota AI Assistant. "
        f"Your Winning Condition: {fields['WIN_CONDITION']} "
        f"Laning: {fields['LANE']} "
        f"Items: {fields['ITEMS']} "
        f"Farming: {fields['FARM']} "
        "Good luck, have fun!"
    )


def analyze_pregame_async(force=False):
    """Event-driven pre-game plan. Wait briefly for a stable draft, then generate once per signature."""
    global _pregame_running
    with _lock:
        if _pregame_running:
            return False
        obs = deepcopy(_state["observation"])
        draft = deepcopy(obs.get("draft") or {})
        hero = obs.get("hero", {}).get("name")
        if not hero or len(draft.get("allies", [])) + len(draft.get("enemies", [])) < 1:
            _state["pregame_status"] = "waiting"
            return False
        sig = pregame_signature(draft, hero)
        now = time.monotonic()
        if not force:
            if sig == _state.get("pregame_signature"):
                return False
            if sig != _state.get("pregame_candidate_signature"):
                _state["pregame_candidate_signature"] = sig
                _state["pregame_candidate_since"] = now
                _state["pregame_status"] = "draft_stabilizing"
                return False
            if now - float(_state.get("pregame_candidate_since") or 0.0) < 2.0:
                return False
        _pregame_running = True
        _state["pregame_status"] = "thinking"
        _state["pregame_error"] = None

    def worker():
        global _pregame_running
        try:
            client = OpenAI()
            payload = {"player_hero": hero, "allies": draft.get("allies", []), "enemies": draft.get("enemies", [])}
            patch_context = relevant_patch_knowledge(obs)
            with _lock:
                existing_support = list(_state.get("meta_strategy") or [])
            response = client.responses.create(
                model=MODEL,
                instructions=PREGAME_PROMPT,
                input="DRAFT:\n" + json.dumps(payload)
                    + "\n\nPATCH KNOWLEDGE:\n" + json.dumps(patch_context, ensure_ascii=False)
                    + "\n\nEXISTING STRATEGY SUPPORT (do not repeat or paraphrase these; return only genuinely new insights):\n"
                    + json.dumps(existing_support, ensure_ascii=False),
                store=False,
            )
            strategy = (response.output_text or "").strip()
            usage = getattr(response, "usage", None)
            with _lock:
                _state["usage"]["ai_calls"] += 1
                _state["usage"]["input_tokens"] += usage_value(usage, "input_tokens")
                _state["usage"]["output_tokens"] += usage_value(usage, "output_tokens")
                refresh_costs_locked()
                _state["pregame_strategy"] = strategy
                new_meta = extract_meta_strategy(strategy)
                if new_meta and new_meta.upper() != "NONE":
                    existing = list(_state.get("meta_strategy") or [])
                    # The model is instructed to emit only novel insights; this exact-key guard
                    # prevents accidental duplicate chips without deleting prior support.
                    seen = {re.sub(r"\s+", " ", x).strip().casefold() for x in existing}
                    for insight in [x.strip() for x in new_meta.split(";") if x.strip()]:
                        key = re.sub(r"\s+", " ", insight).strip().casefold()
                        if key not in seen:
                            existing.append(insight)
                            seen.add(key)
                    _state["meta_strategy"] = existing
                _state["pregame_signature"] = sig
                _state["pregame_candidate_signature"] = sig
                _state["pregame_status"] = "ready"
                # Opening briefing is spoken only once per match: the first successful
                # pre-game analysis. Later draft refreshes may update the panel silently.
                should_speak = _state.get("pregame_last_spoken_signature") is None
                if should_speak:
                    _state["pregame_last_spoken_signature"] = sig
            pregame_voice = extract_pregame_voice(strategy)
            if should_speak and pregame_voice:
                speak_async(pregame_voice)
        except Exception as exc:
            with _lock:
                _state["pregame_status"] = "error"
                _state["pregame_error"] = str(exc)
        finally:
            with _lock:
                _pregame_running = False
    threading.Thread(target=worker, daemon=True).start()
    return True


def extract_live_strategy(text):
    """Extract the in-game carry plan and novel support from the 60-second live call."""
    fields = ["WIN_CONDITION", "LANE", "ITEMS", "FARM", "TEAMFIGHT", "TARGETS", "THREATS"]
    values = {}
    for key in fields:
        m = re.search(rf"(?mi)^{key}:\s*(.+)$", text or "")
        if m:
            values[key] = m.group(1).strip()
    strategy = "\n".join(f"{k}: {values[k]}" for k in fields if k in values) if values else None
    mm = re.search(r"(?mi)^META_NEW:\s*(.+)$", text or "")
    meta = mm.group(1).strip() if mm else None
    return strategy, meta


def extract_live_win_probability(text):
    """Parse the win estimate emitted by the same live-analysis call as VOICE."""
    pm = re.search(r"(?mi)^WIN_PROBABILITY:\s*(\d{1,3})\s*%?\s*$", text or "")
    fm = re.search(r"(?mi)^WIN_FACTORS:\s*(.+)$", text or "")
    if not pm:
        return None
    probability = max(0, min(100, int(pm.group(1))))
    raw = fm.group(1).strip() if fm else ""
    factors = [x.strip(" -•\t") for x in re.split(r"\s*[,;]\s*", raw) if x.strip()][:3]
    return {"probability": probability, "factors": factors}


def analyze_async(force=False):
    global _analysis_running, _last_analysis_monotonic, _analysis_interval_index
    with _lock:
        if _analysis_running:
            return False
        obs = deepcopy(_state["observation"])
        pace_snapshot = deepcopy(_state.get("immortal_pace") or {})
        history_snapshot = deepcopy(_state.get("history") or [])
        context_snapshot = strategic_context(obs, pace_snapshot, history_snapshot)
        if not is_analyzable(obs):
            return False
        current_interval = AI_INTERVAL_PATTERN[_analysis_interval_index % len(AI_INTERVAL_PATTERN)]
        if not force and time.monotonic() - _last_analysis_monotonic < current_interval:
            return False
        _analysis_running = True
        _state["ai_status"] = "thinking"
        _state["ai_error"] = None
        # Recommendation, win probability, in-game plan and support share this cycle.
        _state["pregame_status"] = "thinking"

    def worker():
        global _analysis_running, _last_analysis_monotonic, _analysis_interval_index
        try:
            if not os.getenv("OPENAI_API_KEY"):
                raise RuntimeError("OPENAI_API_KEY is not configured")
            client = OpenAI()
            response = client.responses.create(
                model=MODEL,
                instructions=SYSTEM_PROMPT,
                input="CURRENT/PRIOR IN-GAME CARRY PLAN (refresh it from live state below):\n" + str(_state.get("pregame_strategy") or "Not available") + "\n\nACCUMULATED STRATEGY SUPPORT (retain; add only genuinely new non-redundant insights via META_NEW):\n" + json.dumps(_state.get("meta_strategy") or [], ensure_ascii=False) + "\n\nPATCH KNOWLEDGE (authoritative when relevant):\n" + json.dumps(relevant_patch_knowledge(obs), ensure_ascii=False) + "\n\nDERIVED STRATEGIC CONTEXT (primary reasoning input):\n" + json.dumps(context_snapshot, ensure_ascii=False) + "\n\nCURRENT DOTA OBSERVATION (supporting facts):\n" + json.dumps(ai_payload(obs), ensure_ascii=False),
                store=False,
            )
            advice = (response.output_text or "").strip()
            voice_text = extract_voice(advice)
            win_estimate = extract_live_win_probability(advice)
            live_strategy, live_meta = extract_live_strategy(advice)
            resp_usage = getattr(response, "usage", None)
            win_threshold_voice = None
            with _lock:
                _state["usage"]["ai_calls"] += 1
                _state["usage"]["input_tokens"] += usage_value(resp_usage, "input_tokens")
                _state["usage"]["output_tokens"] += usage_value(resp_usage, "output_tokens")
                refresh_costs_locked()
                _state["advice"] = advice or "No recommendation returned."
                now_iso = datetime.now().isoformat(timespec="seconds")
                # The in-game strategist is refreshed by this exact same 60-second call.
                if live_strategy:
                    _state["pregame_strategy"] = live_strategy
                    _state["pregame_status"] = "ready"
                    _state["pregame_error"] = None
                if live_meta and live_meta.upper() != "NONE":
                    existing = list(_state.get("meta_strategy") or [])
                    seen = {re.sub(r"\s+", " ", x).strip().casefold() for x in existing}
                    for insight in [x.strip() for x in live_meta.split(";") if x.strip()]:
                        key = re.sub(r"\s+", " ", insight).strip().casefold()
                        if key not in seen:
                            existing.append(insight)
                            seen.add(key)
                    _state["meta_strategy"] = existing
                if win_estimate:
                    previous = _state.get("live_win_probability") or {}
                    previous_probability = previous.get("probability")
                    new_probability = win_estimate["probability"]
                    if previous_probability is not None:
                        if previous_probability <= 50 < new_probability:
                            win_threshold_voice = "Win probability above fifty percent."
                        elif previous_probability >= 50 > new_probability:
                            win_threshold_voice = "Win probability below fifty percent."
                    history = list(previous.get("history") or [])
                    game_clock = (obs.get("game") or {}).get("clock_time")
                    history.append({"game_time": game_clock, "probability": win_estimate["probability"], "updated_at": now_iso})
                    _state["live_win_probability"] = {
                        "probability": win_estimate["probability"],
                        "factors": win_estimate["factors"],
                        "updated_at": now_iso,
                        "history": history[-20:],
                        "source": "same_live_ai_analysis_as_voice",
                        "calibrated": False,
                    }
                _state["last_ai_at"] = now_iso
                _state["ai_status"] = "ready"
                _state["ai_error"] = None
                _last_analysis_monotonic = time.monotonic()
                _analysis_interval_index = (_analysis_interval_index + 1) % len(AI_INTERVAL_PATTERN)
                _state["analysis_schedule"] = {"pattern_seconds": AI_INTERVAL_PATTERN, "next_interval_seconds": AI_INTERVAL_PATTERN[_analysis_interval_index]}
            # A 50% threshold crossing is a short local notification. It uses the
            # already-computed AI estimate but makes no additional API call.
            if win_threshold_voice:
                speak_async(win_threshold_voice)
            # Speak only when the short voice instruction changed, avoiding repeated narration.
            with _lock:
                previous_spoken = _state.get("last_spoken")
            if voice_text and voice_text != previous_spoken:
                speak_async(voice_text)
        except Exception as exc:
            with _lock:
                _state["ai_status"] = "error"
                _state["ai_error"] = str(exc)
                if _state.get("pregame_status") == "thinking":
                    _state["pregame_status"] = "error"
                    _state["pregame_error"] = str(exc)
        finally:
            with _lock:
                _analysis_running = False

    threading.Thread(target=worker, daemon=True).start()
    return True


POSTMATCH_PROMPT = """You are an IMMORTAL-LEVEL Dota 2 Position 1 post-match coach.
Create a compact lesson-learned report using ONLY the supplied accumulated match evidence. Do not invent enemy net worth, hidden items, wards, fog positions, or events. Compare farming trajectory with the supplied Immortal-Divine Position-1 benchmark only where supported. Focus on decisions and repeatable lessons, not generic encouragement.

Return exactly five single-line fields:
OVERALL: <1 concise assessment of the carry performance and match arc>
WHAT_WENT_WELL: <1-2 specific strengths supported by match evidence>
MAIN_MISTAKE: <the single highest-value mistake or missed opportunity; say Insufficient evidence if unsupported>
KEY_LESSON: <one transferable Position-1 lesson from this match>
NEXT_MATCH_FOCUS: <one concrete behavior to practice next match>
"""

def postmatch_payload(snapshot):
    obs = snapshot.get("observation") or {}
    hist = snapshot.get("history") or []
    # Downsample local telemetry to roughly one checkpoint per minute to keep the one-time report cheap.
    sampled, last_bucket = [], None
    for row in hist:
        t = int(row.get("time") or 0)
        bucket = max(0, t // 60)
        if bucket != last_bucket:
            sampled.append({k: row.get(k) for k in ("time","gpm","xpm","last_hits","gold","pace") if row.get(k) is not None})
            last_bucket = bucket
    return {
        "final_state": ai_payload(obs),
        "farm_trajectory": sampled[-60:],
        "immortal_divine_pace": snapshot.get("immortal_pace") or {},
        "win_probability_history": (snapshot.get("live_win_probability") or {}).get("history", []),
        "recent_events": (snapshot.get("cockpit") or {}).get("events", [])[-20:],
        "final_strategic_context": snapshot.get("strategic_context") or {},
        "final_carry_plan": snapshot.get("pregame_strategy"),
        "strategy_support": snapshot.get("meta_strategy") or [],
    }

def generate_postmatch_report_async():
    global _postmatch_report_running
    with _lock:
        if _postmatch_report_running or _state.get("performance_report_status") in {"thinking", "ready"}:
            return False
        if not _state.get("observation"):
            return False
        _postmatch_report_running = True
        _state["performance_report_status"] = "thinking"
        _state["performance_report_error"] = None
        snapshot = deepcopy(_state)
    def worker():
        global _postmatch_report_running
        try:
            if not os.getenv("OPENAI_API_KEY"):
                raise RuntimeError("OPENAI_API_KEY is not configured")
            response = OpenAI().responses.create(
                model=MODEL, instructions=POSTMATCH_PROMPT,
                input="POST-MATCH EVIDENCE:\n" + json.dumps(postmatch_payload(snapshot), ensure_ascii=False),
                store=False,
            )
            report = (response.output_text or "").strip()
            usage = getattr(response, "usage", None)
            with _lock:
                _state["usage"]["ai_calls"] += 1
                _state["usage"]["input_tokens"] += usage_value(usage, "input_tokens")
                _state["usage"]["output_tokens"] += usage_value(usage, "output_tokens")
                refresh_costs_locked()
                _state["performance_report"] = report
                _state["performance_report_status"] = "ready"
                _state["performance_report_error"] = None
        except Exception as exc:
            with _lock:
                _state["performance_report_status"] = "error"
                _state["performance_report_error"] = str(exc)
        finally:
            with _lock:
                _postmatch_report_running = False
    threading.Thread(target=worker, daemon=True).start()
    return True


@app.get("/api/debug/structure-transitions")
def debug_structure_transitions():
    limit = max(1, min(int(request.args.get("limit", 50)), 500))
    rows = []
    try:
        if STRUCTURE_TRACE_FILE.exists():
            with _structure_trace_lock:
                lines = STRUCTURE_TRACE_FILE.read_text(encoding="utf-8").splitlines()[-limit:]
            for line in lines:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    except Exception:
        pass
    with _lock:
        registry = deepcopy(_structure_registry)
    return jsonify({
        "build_id": BUILD_ID,
        "trace_enabled": STRUCTURE_TRACE_ENABLED,
        "trace_file": str(STRUCTURE_TRACE_FILE),
        "count": len(rows),
        "recent": rows,
        "structure_registry": registry,
    })



@app.get("/api/debug/structure-experiment")
def debug_structure_experiment():
    rows = _read_structure_trace_all()
    return jsonify({
        "build_id": BUILD_ID,
        "trace_file": str(STRUCTURE_TRACE_FILE),
        **_structure_trace_summary(rows),
        "instructions": [
            "Damage at least one Radiant tower.",
            "Damage at least one Dire tower.",
            "Preferably destroy one tower on each side.",
            "Then refresh this endpoint; BOTH_RADIANT_AND_DIRE_OBSERVED proves both absolute teams appeared in raw captured GSI."
        ],
    })


@app.post("/api/debug/structure-experiment/reset")
def reset_structure_experiment():
    try:
        STRUCTURE_TRACE_DIR.mkdir(parents=True, exist_ok=True)
        with _structure_trace_lock:
            STRUCTURE_TRACE_FILE.write_text("", encoding="utf-8")
        return jsonify({"ok": True, "build_id": BUILD_ID, "trace_file": str(STRUCTURE_TRACE_FILE)})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

@app.get("/api/debug/draft")
def debug_draft():
    with _lock:
        return jsonify({
            "build_id": BUILD_ID,
            "draft": deepcopy(_state.get("draft") or {}),
            "known_gsi_draft": deepcopy(_state.get("known_gsi_draft") or {}),
            "observation_draft": deepcopy((_state.get("observation") or {}).get("draft") or {}),
            "current_minimap_objects": len((_gsi_snapshot.get("minimap") or {})),
            "current_minimap_heroes": list(dict.fromkeys([
                clean_hero_name(next((str(r.get(f)) for f in ("name", "unitname")
                                      if r.get(f) and str(r.get(f)).startswith("npc_dota_hero_")), None))
                for r in (_gsi_snapshot.get("minimap") or {}).values() if isinstance(r, dict)
                if any(r.get(f) and str(r.get(f)).startswith("npc_dota_hero_") for f in ("name", "unitname"))
            ])),
            "registry_heroes": [
                {"object_id": oid, "hero": clean_hero_name(next((str(v["row"].get(f)) for f in ("name", "unitname") if v.get("row", {}).get(f) and str(v["row"].get(f)).startswith("npc_dota_hero_")), None)), "team": v.get("row", {}).get("team"), "image": v.get("row", {}).get("image"), "last_seen_seq": v.get("last_seen_seq")}
                for oid, v in sorted(_minimap_hero_registry.items(), key=lambda kv: kv[1].get("last_seen_seq", 0), reverse=True)
            ],
        })


@app.route("/", methods=["GET", "POST"])
def index_or_gsi():
    if request.method == "POST":
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "Expected JSON"}), 400
        with _lock:
            _state["last_raw_gsi"] = deepcopy(data)
            _state["last_gsi_received_at"] = datetime.now().isoformat(timespec="milliseconds")
        # Reconstruct the stable state first; do not treat every POST as a full snapshot.
        full_data = accumulate_gsi_packet(data)
        obs = build_observation(full_data)
        # Strategic enemy locations must come from the CURRENT packet, never the
        # accumulated snapshot. This prevents stale/fogged coordinates from entering GPT.
        visible_enemies = current_visible_enemy_positions(
            data, (obs.get("player") or {}).get("team_name"), (obs.get("hero") or {}).get("name")
        )
        obs.setdefault("map_context", {})["visible_enemy_positions"] = visible_enemies
        # Lane-wave pressure is also CURRENT-PACKET ONLY. Never use accumulated creep
        # coordinates, because removed/moved creeps would otherwise create stale pressure.
        obs["lane_wave_pressure"] = current_lane_wave_pressure(
            data, (obs.get("player") or {}).get("team_name")
        )
        # Dashboard enemy markers are also CURRENT-PACKET ONLY. Never render
        # enemy coordinates reconstructed from the accumulated minimap snapshot:
        # once an enemy is absent from the current GSI minimap evidence, its map
        # marker disappears instead of lingering at a last-known position.
        obs["map_context"]["enemies"] = [
            {"hero": row.get("hero"), "team": row.get("team"),
             "display_position": deepcopy(row.get("position") or {})}
            for row in visible_enemies
        ]
        obs["map_context"]["enemy_positions_policy"] = "current_visible_only_no_stale_markers"
        with _lock:
            previous_obs_for_events = deepcopy(_state.get("observation") or {})
            event_voice_messages = zero_api_event_messages(previous_obs_for_events, obs)
        update_minimap_hero_registry(data, obs.get("hero", {}).get("name"))
        with _lock:
            manual = deepcopy(_state.get("manual_draft") or {})
        merged = current_registry_draft(full_data, obs.get("hero", {}).get("name"), manual)
        obs["draft"] = merged
        # Compatibility field: no longer a monotonic cross-session memory.
        with _lock:
            _state["known_gsi_draft"] = {"allies": list(merged["allies"]), "enemies": list(merged["enemies"])}
        with _lock:
            obs["manual_enemy_structures"] = deepcopy(_state.get("manual_enemy_structures") or {})
        pace = immortal_pace(obs)
        with _lock:
            history_before = deepcopy(_state.get("history") or [])
        trajectory = pace_trajectory(pace, history_before)
        pace["trajectory"] = trajectory
        context = strategic_context(obs, pace, history_before)
        cockpit = cockpit_state(obs, pace)
        with _lock:
            new_time = obs.get("game", {}).get("clock_time")
            old_time = _state.get("observation", {}).get("game", {}).get("clock_time")
            if new_time is not None and new_time < 0 and old_time is not None and old_time > 60:
                reset_match_locked(datetime.now().isoformat(timespec="seconds"))
            _state["connected"] = True
            _state["last_gsi_at"] = datetime.now().isoformat(timespec="seconds")
            _state["last_gsi_age_seconds"] = 0
            game_state = (obs.get("game") or {}).get("state")
            if game_state == "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS":
                _state["session_phase"] = "live"
            elif game_state in {"DOTA_GAMERULES_STATE_POST_GAME", "DOTA_GAMERULES_STATE_DISCONNECT"}:
                # Only an explicit terminal Dota game state is allowed to mark the
                # match as post-match. A stale/missing GSI connection may freeze
                # the UI for review, but must never imply that the match ended.
                _state["session_phase"] = "frozen"
            elif _state.get("observation"):
                _state["session_phase"] = "connected"
            _state["observation"] = obs
            _state["draft"] = deepcopy(obs.get("draft") or {})
            _state["immortal_pace"] = pace
            _state["strategic_context"] = context
            _state["cockpit"] = cockpit
            record_history_locked(obs)
            if _state["history"]:
                _state["history"][-1]["pace"] = {"minute": pace.get("minute"), "overall_percentile": pace.get("overall_percentile")} if pace.get("available") else None
        # Generate the one-time report only from explicit terminal game-state
        # evidence received from Dota, never merely because GSI became stale.
        if game_state in {"DOTA_GAMERULES_STATE_POST_GAME", "DOTA_GAMERULES_STATE_DISCONNECT"}:
            generate_postmatch_report_async()

        # Zero-API fixed-rule coach: immediate local Kokoro notifications.
        for event_voice in event_voice_messages:
            speak_async(event_voice)

        # Build the initial carry plan only in Dota's explicit PRE_GAME state.
        # HERO_SELECTION, STRATEGY_TIME, and TEAM_SHOWCASE must never trigger
        # the pre-game OpenAI call or opening voice briefing. The briefing is
        # spoken by analyze_pregame_async only after the first plan succeeds.
        # Once GAME_IN_PROGRESS begins, analyze_async owns the 60-second live cycle.
        game = obs.get("game") or {}
        game_state = game.get("state")
        if game_state == "DOTA_GAMERULES_STATE_PRE_GAME":
            analyze_pregame_async(force=False)
        analyze_async(force=False)
        return jsonify({"ok": True})
    return render_template("index.html", model=MODEL, interval="60")


@app.get("/api/state")
def api_state():
    with _lock:
        snapshot = deepcopy(_state)
    # A stale GSI connection must not erase the last valid match snapshot.
    # The browser keeps rendering that snapshot in a read-only/frozen state so
    # the player can review the match after Dota stops sending GSI packets.
    if snapshot["last_gsi_at"]:
        try:
            age = max(0.0, (datetime.now() - datetime.fromisoformat(snapshot["last_gsi_at"])).total_seconds())
            snapshot["last_gsi_age_seconds"] = int(age)
            snapshot["connected"] = age < 10
            has_match_data = bool(snapshot.get("observation"))
            if not snapshot["connected"] and has_match_data:
                snapshot["session_phase"] = "frozen"
            elif snapshot["connected"] and snapshot.get("session_phase") == "waiting":
                snapshot["session_phase"] = "connected"
        except ValueError:
            pass
    elif snapshot.get("observation"):
        snapshot["session_phase"] = "frozen"
    game_time = snapshot.get("observation", {}).get("game", {}).get("clock_time") or 0
    spent = snapshot.get("usage", {}).get("total_cost_idr", 0.0)
    if game_time > 120 and spent > 0:
        snapshot["usage"]["projected_45m_idr"] = spent / game_time * 2700
        snapshot["usage"]["projected_45m_usd"] = snapshot["usage"]["projected_45m_idr"] / USD_TO_IDR
    else:
        snapshot["usage"]["projected_45m_idr"] = 0.0
        snapshot["usage"]["projected_45m_usd"] = 0.0
    return jsonify(snapshot)


@app.post("/api/draft")
def api_draft():
    body = request.get_json(silent=True) or {}
    def clean_list(value):
        if isinstance(value, str): value = value.split(",")
        return [clean_hero_name(x.strip()) for x in (value or []) if isinstance(x, str) and x.strip()][:5]
    manual = {"allies": clean_list(body.get("allies")), "enemies": clean_list(body.get("enemies"))}
    with _lock:
        _state["manual_draft"] = manual
        obs = deepcopy(_state.get("observation") or {})
        hero = obs.get("hero", {}).get("name")
        # Merge manual entries with the latest already-extracted GSI draft without inventing data.
        current = deepcopy(_state.get("draft") or {})
        allies = list(dict.fromkeys((current.get("allies") or []) + manual["allies"]))[:5]
        enemies = list(dict.fromkeys((current.get("enemies") or []) + manual["enemies"]))[:5]
        if hero and hero not in allies: allies.insert(0, hero)
        obs["draft"] = {"allies": allies[:5], "enemies": enemies[:5], "source": "hybrid"}
        _state["observation"] = obs
        _state["draft"] = deepcopy(obs["draft"])
    return jsonify({"ok": True, "draft": obs["draft"]})


@app.post("/api/manual-enemy-structure")
def api_manual_enemy_structure():
    """Set public opponent tower/rax status manually when GSI does not expose it."""
    body = request.get_json(silent=True) or {}
    lane = str(body.get("lane") or "").lower()
    slot = str(body.get("slot") or "").lower()
    if lane not in {"top", "mid", "bot"} or slot not in {"t1", "t2", "t3", "rax"}:
        return jsonify({"ok": False, "error": "invalid lane/slot"}), 400
    with _lock:
        manual = deepcopy(_state.get("manual_enemy_structures") or {})
        row = manual.setdefault(lane, {})
        if slot == "rax":
            row["rax_alive"] = max(0, min(2, int(body.get("alive_count", 2))))
        else:
            row[slot] = bool(body.get("alive"))
        _state["manual_enemy_structures"] = manual
        obs = deepcopy(_state.get("observation") or {})
        obs["manual_enemy_structures"] = deepcopy(manual)
        _state["observation"] = obs
        pace = deepcopy(_state.get("immortal_pace") or {})
        history = deepcopy(_state.get("history") or [])
        _state["strategic_context"] = strategic_context(obs, pace, history)
        _state["cockpit"] = cockpit_state(obs, pace)
    return jsonify({"ok": True, "manual_enemy_structures": manual})


@app.post("/api/pregame")
def api_pregame():
    started = analyze_pregame_async(force=True)
    return jsonify({"ok": started, "status": "started" if started else "busy_or_draft_incomplete"})


@app.post("/api/analyze")
def api_analyze():
    started = analyze_async(force=True)
    return jsonify({"ok": started, "status": "started" if started else "busy_or_no_live_game"})


@app.get("/health")
def health():
    return jsonify({"ok": True, "model": MODEL, "voice_enabled": VOICE_ENABLED, "voice_mode": "local_kokoro", "kokoro_voice": KOKORO_VOICE, "patch": CURRENT_PATCH, "knowledge": _state.get("knowledge")})



@app.get("/api/gsi-information")
def api_gsi_information():
    """Read-only latest raw GSI packet plus the dashboard's current derived observation."""
    with _lock:
        current_packet = deepcopy(_state.get("last_raw_gsi") or {})
        received_at = _state.get("last_gsi_received_at")
        observation = deepcopy(_state.get("observation") or {})
        session_phase = _state.get("session_phase")
    return jsonify({
        "build_id": BUILD_ID,
        "received_at": received_at,
        "session_phase": session_phase,
        "current_packet": current_packet,
        "dashboard_observation": observation,
        "visibility_note": "Use current_packet for current enemy minimap visibility; never infer current enemy coordinates from historical/accumulated state."
    })



if __name__ == "__main__":
    print(f"Dota AI dashboard: http://127.0.0.1:{PORT}")
    print(f"Mobile/LAN: http://<THIS-PC-IP>:{PORT}")
    app.run(host="0.0.0.0", port=PORT, threaded=True, debug=False)
