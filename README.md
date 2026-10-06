# DOTA 2 AI Assistant

**Current build:** V44.4\
**Role:** Real-time Position 1 carry coach for high-level solo queue\
**Patch knowledge:** 7.41f\
**Backend:** Python + Flask\
**AI:** OpenAI\
**Game telemetry:** DOTA 2 Game State Integration (GSI)\
**Voice:** Local Kokoro TTS\
**Dashboard:** Desktop + mobile browser

------------------------------------------------------------------------

## 1. What This Project Does

DOTA 2 AI Assistant is a real-time strategic coaching system for a
**Position 1 carry player**. It combines DOTA 2's Game State Integration telemetry with locally
derived strategic state, patch-specific knowledge, an Immortal--Divine
Position 1 benchmark, and an OpenAI model. The result is a live browser
dashboard plus short spoken coaching.

The assistant is designed primarily for **macro decisions**, not
mechanical execution. It tries to answer questions such as:

-   Should I keep farming or join the next fight?
-   Is my carry progression ahead of or behind a high-level pace?
-   What is my current power spike?
-   Which part of the map is currently safer or more valuable?
-   Are our waves applying pressure or coming into us?
-   What objective should matter next?
-   How should the current draft change my win condition?
-   How should the structure state affect the game plan?
-   How favorable is the current observable game state?

The assistant does **not** control DOTA, issue commands, move the hero,
cast spells, or read hidden enemy information.

------------------------------------------------------------------------

## 2. High-Level Architecture

``` text
DOTA 2
  │
  │ Game State Integration (GSI)
  ▼
Flask server
  │
  ├── Reconstruct stable game state
  ├── Build normalized observation
  ├── Track current-visible enemy heroes
  ├── Derive lane-wave pressure
  ├── Derive strategic state
  ├── Track own structures from GSI
  ├── Merge manually entered enemy structures
  ├── Compare carry pace with benchmark
  ├── Retrieve relevant patch/meta knowledge
  └── Detect local events
       │
       ├───────────────────────────┐
       ▼                           ▼
Compact AI context             Local event coach
       │                           │
       ▼                           ▼
OpenAI                       Kokoro TTS
       │                           │
       ├── Recommendation           ├── Kill
       ├── Win Probability          ├── Death
       ├── Carry Plan               ├── Multi-kill
       └── Strategy Support         ├── Major item
                                    ├── Level spike
                                    └── Buyback / 50% crossing
       │
       ▼
Browser Dashboard
```

The key design principle is:

> **Use local deterministic processing for facts and telemetry; use the
> language model for strategic interpretation.**

Raw GSI is therefore not simply dumped into the model every cycle.

------------------------------------------------------------------------

## 3. Information Sources

### 3.1 DOTA 2 GSI

The configured GSI feeds include:

``` text
provider
map
player
hero
abilities
items
draft
minimap
buildings
neutralitems
roshan
events
```

The project receives GSI by HTTP POST at:

``` text
http://127.0.0.1:5050/
```

### 3.2 Local Player Information

GSI provides rich information for the local player, including where
available:

-   hero
-   level
-   XP
-   HP / mana
-   alive/dead state
-   status effects
-   buyback cooldown and cost
-   K/D/A
-   last hits / denies
-   gold
-   GPM
-   XPM
-   abilities and cooldowns
-   inventory
-   current hero coordinates

This information is central to carry progression and current-power
reasoning.

### 3.3 Minimap

The current GSI minimap packet is used for:

-   allied hero positions
-   currently visible enemy hero positions
-   lane creeps
-   wave-pressure derivation
-   map rendering

Enemy positions follow a strict rule:

> **An enemy position is AI-eligible only while that enemy appears with
> coordinates in the current GSI minimap packet.**

The assistant does not treat an old coordinate as the enemy's current
location after that hero disappears.

### 3.4 Structures

Structure information has two authoritative sources.

**Your team:** GSI.

The project reconstructs the own-team structure state from the exposed
`buildings` branch. Own structures can therefore show live health
information.

**Enemy team:** manual dashboard input.

Enemy towers and barracks are updated by clicking the Objectives
controls:

-   enemy T1/T2/T3: click to toggle alive/destroyed
-   enemy RAX: click to cycle `2/2 → 1/2 → 0/2 → 2/2`

Enemy structure status is deliberately **not inferred from minimap
structure presence**.

The merged structure state is supplied to the AI and affects:

-   Win Probability
-   Main Factors
-   Recommendation
-   In-game Carry Plan

### 3.5 Patch and Strategy Knowledge

The local knowledge base contains:

``` text
knowledge/
├── patch_7_41f.json
├── carry_macro.md
├── meta_carry_profiles.json
├── immortal_pos1_7_41f.json
├── heroes/
│   ├── juggernaut.md
│   ├── phantom_assassin.md
│   ├── sven.md
│   └── ursa.md
├── items/
└── matchups/
```

Relevant knowledge is retrieved locally before an AI call. The entire
knowledge base is not sent every cycle.

------------------------------------------------------------------------

## 4. Match Lifecycle

The system follows the match rather than treating every GSI packet
independently.

``` text
WAITING
   ↓
CONNECTED / DRAFT
   ↓
PRE_GAME
   ↓
LIVE
   ↓
POST-MATCH / FROZEN
   ↓
NEW MATCH
   ↓
RESET
```

### Waiting

The server is running but there is no usable match state.

### Pre-game

The opening AI carry plan is generated **only** when DOTA reports:

``` text
DOTA_GAMERULES_STATE_PRE_GAME
```

It is intentionally not generated during:

-   HERO_SELECTION
-   STRATEGY_TIME
-   TEAM_SHOWCASE

This prevents premature analysis from an incomplete draft.

### Live

When DOTA reports:

``` text
DOTA_GAMERULES_STATE_GAME_IN_PROGRESS
```

the live strategist becomes active.

### Post-match

A Performance Report is generated only after explicit terminal GSI
evidence:

``` text
DOTA_GAMERULES_STATE_POST_GAME
```

or:

``` text
DOTA_GAMERULES_STATE_DISCONNECT
```

A stale GSI connection may freeze the dashboard for review, but
staleness alone does **not** trigger the Performance Report.

------------------------------------------------------------------------

## 5. Pre-game Carry Plan

The initial pre-game analysis produces:

-   WIN CONDITION
-   LANE
-   ITEMS
-   FARM
-   TEAMFIGHT
-   TARGETS
-   THREATS

After the first successful pre-game analysis, the opening voice briefing
is spoken once:

``` text
Hi, I'm your DOTA 2 AI Assistant.
Your Winning Condition: ...
Laning: ...
Items: ...
Farming: ...
Good luck, have fun!
```

Later strategic updates do not replay the opening introduction.

------------------------------------------------------------------------

## 6. Live AI Analysis

During an in-progress game, the assistant periodically creates a compact
strategic snapshot and asks the AI for a high-level carry decision.

The response format is:

``` text
ACTION: <best next action>
WHY: <reason grounded in current observation>
WATCH: <most important thing to monitor>
CONFIDENCE: <LOW|MEDIUM|HIGH>
WIN_PROBABILITY: <0-100>
MAIN_FACTORS: <decisive observable factors>
VOICE: <short spoken instruction>
```

The spoken `VOICE` field is intentionally shorter than the full
dashboard analysis.

### Analysis interval

The active setting is:

``` text
AI_INTERVAL_PATTERN
```

If it is not configured, the current code defaults to:

``` text
60
```

meaning one live AI analysis approximately every 60 seconds.

A custom pattern can also be used, for example:

``` env
AI_INTERVAL_PATTERN=60
```

or:

``` env
AI_INTERVAL_PATTERN=45,60,60
```

The older environment variable `AI_INTERVAL_SECONDS` is not the active
scheduler setting in V44.4.

Manual analysis is available through the dashboard/API and can force a
new analysis while the game is live.

------------------------------------------------------------------------

## 7. What the AI Uses

The AI does not evaluate the game from kill score alone.

The compact context includes the observable information needed to
interpret the current game, including:

### Overall game state

-   game time
-   Radiant score
-   Dire score
-   player team
-   day/night state where relevant
-   current game phase

### Carry progression

-   hero and level
-   XP-related progression
-   K/D/A
-   last hits / denies
-   GPM
-   XPM
-   current gold
-   items
-   major item timings
-   HP / mana
-   ability readiness
-   buyback state

### Structures

-   own-team structures from GSI
-   enemy structures from manual dashboard input

### Draft

-   known allied heroes
-   known enemy heroes
-   manual draft additions where needed

### Map information

-   allied hero positions
-   currently visible enemy positions
-   current lane-wave pressure

### Strategic interpretation

-   economy status
-   current power
-   map context
-   priority
-   score context
-   winning condition
-   carry-specific patch/meta knowledge
-   relevant matchup/strategy knowledge

### Benchmark

-   Position 1 GPM trajectory
-   last-hit trajectory
-   XPM trajectory
-   percentile/band interpretation

### Recent events

Where GSI exposes relevant events, they can contribute additional
context.

------------------------------------------------------------------------

## 8. Win Probability

Win Probability is an **AI strategic estimate**, not Valve's official
probability and not a calibrated betting model.

It is recomputed from the current observable game context rather than
being mechanically anchored to the previous percentage.

Important inputs include:

-   score and game time
-   carry economic trajectory
-   level / XP progression
-   K/D/A and deaths
-   items and power spikes
-   HP / mana / readiness
-   buyback
-   structure state
-   draft
-   current visible enemy locations
-   allied positioning
-   lane-wave pressure
-   recent events
-   winning condition
-   patch/meta strategy
-   Immortal--Divine Position 1 benchmark

The relative importance of these signals changes with game phase.

For example, early in the game, farming pace and first-item timing can
be highly important. Late in the game, barracks, buyback, map pressure,
visible enemy positions, item completion, and high-ground conditions may
dominate.

### Important limitation

The assistant must not silently invent unavailable enemy information
such as:

-   hidden enemy positions
-   enemy net worth
-   enemy GPM/XPM
-   hidden enemy items
-   enemy cooldowns
-   enemy buybacks
-   enemy levels when not actually available

------------------------------------------------------------------------

## 9. Lane-Wave Pressure

V44.3 introduced locally derived lane-wave pressure. V44.4 displays it
directly on the map.

The calculation uses **current GSI minimap lane creeps only**.

It does not use stale accumulated creep coordinates.

Internally, each lane can be classified as:

``` text
PUSHED_DEEP_INTO_OUR_SIDE
PUSHED_INTO_OUR_SIDE
NEAR_CENTER
PUSHED_INTO_ENEMY_SIDE
PUSHED_DEEP_INTO_ENEMY_SIDE
```

The dashboard simplifies this to three visual states:

-   **Green dot** --- our wave is pushing into the enemy side
-   **Gray dot** --- roughly neutral
-   **Red dot** --- enemy wave is pushing into our side

There is one indicator for TOP, MID, and BOT.

The same derived wave state is supplied to the AI, so the dashboard
indicator and strategic reasoning use the same source.

Wave pressure can affect reasoning about:

-   available farming space
-   lane safety
-   map pressure
-   objective windows
-   high-ground opportunities
-   cross-map decisions
-   whether the team needs to address incoming lanes

It must **not** be used to claim a hidden enemy hero is at a particular
location.

------------------------------------------------------------------------

## 10. Strategic State

The dashboard derives a compact strategic state locally.

### Economy

Five bands:

``` text
VERY AHEAD
AHEAD
ON PACE
BEHIND
VERY BEHIND
```

Economy is primarily based on Position 1 farming progression relative to
the Immortal--Divine benchmark rather than simply using team kill score.

### Power

Five bands:

``` text
VERY STRONG
STRONG
PARTIAL
WEAK
VERY WEAK
```

Power is conceptually separate from economy.

A hero may be economically ahead but temporarily weak because a key
timing is incomplete, HP/mana is poor, or important abilities/items are
unavailable.

### Priority

Examples:

``` text
OBJECTIVE (PUSH TOWER)
OBJECTIVE (NEXT ITEM)
OBJECTIVE (ROSHAN)
OBJECTIVE (HIGH GROUND)
```

The system should not fabricate an objective such as Roshan without
adequate evidence.

### Score

Summarizes the current team score context.

### Map

Uses currently observable map evidence and locally derived signals.
Telemetry confidence should not be confused with perfect strategic
knowledge.

------------------------------------------------------------------------

## 11. Immortal--Divine Position 1 Benchmark

The benchmark file is:

``` text
knowledge/immortal_pos1_7_41f.json
```

The current validated dataset contains:

-   250 Position 1 player samples
-   125 matches
-   49 heroes

The collection methodology targets high-level lobbies and Position 1
candidates, with a maximum of one candidate per team.

The dashboard compares the user's trajectory with the high-level cohort
rather than judging a single GPM number in isolation.

### Dashboard benchmark charts

The dashboard includes trajectories such as:

-   GPM
-   Last Hits
-   XPM

The comparison is intended to answer:

> Is my carry progression on pace for a high-level Position 1 game at
> this minute?

It is not a claim that matching a percentile automatically means the
player is playing at that rank in every other dimension.

### Regenerating the benchmark

Use:

``` bash
python scripts/refresh_immortal_benchmarks.py
```

See the script options before running:

``` bash
python scripts/refresh_immortal_benchmarks.py --help
```

### Important backup rule

The benchmark is generated data and should be backed up separately
before replacing the project.

Do not casually overwrite:

``` text
knowledge/immortal_pos1_7_41f.json
```

------------------------------------------------------------------------

## 12. Map State

The map shows:

-   player
-   allied heroes
-   currently visible enemy heroes
-   team-side coloring
-   day/night indicator
-   TOP / BOT orientation
-   wave-pressure dots

Radiant and Dire remain absolute map teams:

-   Radiant territory: green
-   Dire territory: red

### Enemy visibility rule

Enemy markers are current-visible only.

``` text
Enemy appears in current GSI minimap packet
    → marker appears / updates

Enemy disappears from current GSI minimap packet
    → marker disappears
```

The registry may retain identity for draft-related purposes, but a
retained identity must not become a stale current position.

------------------------------------------------------------------------

## 13. Objectives / Structures

The Objectives panel is team-aware.

### Own team

Automatically maintained from GSI:

-   T1
-   T2
-   T3
-   RAX
-   structure health where available

Own structure health is shown visually.

### Opponent

Maintained manually:

-   click tower status to toggle alive/destroyed
-   click RAX to cycle remaining barracks

This is necessary because the chosen GSI structure pipeline does not
provide a sufficiently reliable symmetric enemy structure state.

Manual enemy structure changes are local and do not trigger an OpenAI
call by themselves. They are included in the next regular or manually
forced live analysis.

Manual enemy structure state resets for a new match.

------------------------------------------------------------------------

## 14. Zero-API Event Coach

Not every spoken message uses OpenAI.

The event coach reacts immediately to deterministic GSI changes and uses
local Kokoro TTS.

This means these reactions have:

``` text
OpenAI calls: 0
OpenAI token cost: $0
TTS API cost: $0
```

### Kill reactions

The current phrase pool is defined in `app.py`:

``` python
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
```

### Death reactions

``` python
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
```

The code selects a random phrase while avoiding immediate repetition.

To add more reactions, simply add more strings to these tuples and
restart the server.

### Other local event reactions

The local event system also supports reactions to events such as:

-   multi-kills
-   major item completion
-   level 6 / 12 / 18
-   buyback use
-   Win Probability crossing 50%

The 50% voice notification is based on a threshold crossing, not every
analysis.

------------------------------------------------------------------------

## 15. Kokoro Voice

Voice synthesis is local/offline.

Expected model files:

``` text
models/kokoro/kokoro-v1.0.onnx
models/kokoro/voices-v1.0.bin
```

Typical configuration:

``` env
VOICE_ENABLED=1
VOICE_ENGINE=kokoro
KOKORO_MODEL=models/kokoro/kokoro-v1.0.onnx
KOKORO_VOICES=models/kokoro/voices-v1.0.bin
KOKORO_VOICE=am_michael
VOICE_SPEED=1.25
VOICE_VOLUME=4.0
```

Relative model paths are resolved from the repository.

Verify the files:

``` bash
ls -lh models/kokoro/
```

Expected approximate sizes are about:

``` text
kokoro-v1.0.onnx    ~311 MB
voices-v1.0.bin      ~25 MB
```

The Kokoro executable must also be available to the application.

------------------------------------------------------------------------

## 16. Performance Report

After a confirmed match end, the assistant creates one post-match
report.

The report contains:

``` text
OVERALL
WHAT_WENT_WELL
MAIN_MISTAKE
KEY_LESSON
NEXT_MATCH_FOCUS
```

It uses accumulated match evidence and the carry trajectory.

The report is not voiced.

The dashboard visually distinguishes:

-   WIN
-   LOSS
-   EXITED / unknown terminal result

A temporary loss of GSI connectivity does not by itself generate this
report.

------------------------------------------------------------------------

## 17. Dashboard Layout

The major dashboard hierarchy is:

``` text
Recommendation
↓
Win Probability
↓
Performance Report (post-match only)
↓
In-game Carry Plan | Strategy Support
↓
Live match / strategic panels
↓
Map State
↓
Objectives / Structures
↓
Benchmark trajectories
↓
GSI Event Feed
↓
Live API Budget
```

The dashboard refreshes local state frequently without requiring an AI
call every refresh.

------------------------------------------------------------------------

## 18. API Efficiency

The project is intentionally designed to avoid unnecessary model usage.

### Raw GSI is processed locally

Instead of sending the full raw GSI stream to OpenAI:

``` text
Raw GSI
   ↓
Local reconstruction
   ↓
Normalized observation
   ↓
Derived strategic state
   ↓
Compact AI context
   ↓
OpenAI
```

### Patch knowledge is filtered locally

Only relevant patch/strategy facts are supplied to the model.

### Event reactions are local

Kills, deaths, and other fixed-rule notifications do not require OpenAI.

### TTS is local

Kokoro does not incur a cloud TTS API cost.

The dashboard tracks:

-   AI calls
-   input tokens
-   output tokens
-   estimated cost
-   projected match cost
-   local TTS plays

------------------------------------------------------------------------

## 19. Project Structure

``` text
dota-ai-assistant/
├── app.py
├── README.md
├── requirements.txt
├── .env
│
├── config/
│   └── gamestate_integration_dota_ai.cfg
│
├── knowledge/
│   ├── patch_7_41f.json
│   ├── carry_macro.md
│   ├── meta_carry_profiles.json
│   ├── immortal_pos1_7_41f.json
│   ├── heroes/
│   ├── items/
│   └── matchups/
│
├── models/
│   └── kokoro/
│       ├── kokoro-v1.0.onnx
│       └── voices-v1.0.bin
│
├── scripts/
│   ├── gsi_inspector.py
│   ├── refresh_immortal_benchmarks.py
│   └── gsi_inspector_output/
│
├── static/
│   ├── app.js
│   ├── style.css
│   └── img/
│
├── templates/
│   └── index.html
│
└── logs/
```

`logs/` contains generated diagnostic data and should not normally be
treated as source code.

------------------------------------------------------------------------

## 20. Installation

### Python dependencies

The core requirements are:

``` text
Flask>=3.0,<4
openai>=2.0
python-dotenv>=1.0
```

Install them in the desired environment:

``` bash
pip install -r requirements.txt
```

For the current development machine, the project is normally run from
the `indonesia-ai` Conda environment:

``` bash
conda activate indonesia-ai
```

### OpenAI key

Create or restore `.env` and configure:

``` env
OPENAI_API_KEY=YOUR_KEY
OPENAI_MODEL=gpt-5.6-luna
```

Never commit or distribute a real API key.

### Recommended scheduler setting

``` env
AI_INTERVAL_PATTERN=60
```

### Server

``` env
PORT=5050
```

------------------------------------------------------------------------

## 21. DOTA 2 GSI Configuration

The project includes:

``` text
config/gamestate_integration_dota_ai.cfg
```

The active DOTA configuration must be placed in DOTA's:

``` text
game/dota/cfg/gamestate_integration/
```

Example:

``` cfg
"Dota AI"
{
    "uri"           "http://127.0.0.1:5050"
    "timeout"       "5.0"
    "buffer"        "0.1"
    "throttle"      "0.1"
    "heartbeat"     "30.0"

    "data"
    {
        "provider"      "1"
        "map"           "1"
        "player"        "1"
        "hero"          "1"
        "abilities"     "1"
        "items"         "1"
        "draft"         "1"
        "minimap"       "1"
        "buildings"     "1"
        "neutralitems"  "1"
        "roshan"        "1"
        "events"        "1"
    }
}
```

The URI must point to the Flask root endpoint:

``` text
http://127.0.0.1:5050
```

not `/gsi`.

Restart DOTA after changing GSI configuration.

------------------------------------------------------------------------

## 22. Running the Assistant

From the project directory:

``` bash
conda activate indonesia-ai
cd ~/Desktop/Indonesia-AI/dota-ai-assistant
python app.py
```

Desktop dashboard:

``` text
http://127.0.0.1:5050
```

For another device on the same LAN, obtain the PC address:

``` bash
hostname -I
```

Then open:

``` text
http://<PC-LAN-IP>:5050
```

Do not expose the development Flask server directly to the public
Internet.

------------------------------------------------------------------------

## 23. Useful Endpoints

### Dashboard

``` text
GET /
```

### DOTA GSI receiver

``` text
POST /
```

### Current dashboard state

``` text
GET /api/state
```

### Health

``` text
GET /health
```

### Latest raw GSI + derived observation

``` text
GET /api/gsi-information
```

This endpoint is especially useful when investigating whether a problem
exists in:

``` text
DOTA GSI
→ state accumulation
→ normalized observation
→ strategic context
→ AI
```

### Manual enemy structure update

``` text
POST /api/manual-enemy-structure
```

Normally used by the dashboard controls.

### Manual/forced live analysis

``` text
POST /api/analyze
```

### Manual pre-game analysis

``` text
POST /api/pregame
```

### Draft helper

``` text
POST /api/draft
```

### Structure diagnostics

``` text
GET /api/debug/structure-transitions
GET /api/debug/structure-experiment
POST /api/debug/structure-experiment/reset
```

### Draft diagnostics

``` text
GET /api/debug/draft
```

------------------------------------------------------------------------

## 24. Debugging

### Server does not start

Check:

``` bash
python -m pip check
python app.py
```

Also make sure another process is not already using port 5050:

``` bash
ss -ltnp | grep ':5050'
```

### Multiple old Flask processes

After development/recovery, check:

``` bash
ps aux | grep '[p]ython.*app.py'
```

A stale server can make the browser appear to use old code or old
filesystem paths.

### No GSI connection

Check the GSI configuration and server:

``` bash
curl http://127.0.0.1:5050/health
```

Then inspect:

``` text
http://127.0.0.1:5050/api/gsi-information
```

### Kokoro model files missing

Check:

``` bash
ls -lh models/kokoro/
```

and:

``` bash
grep -n -i kokoro .env
```

For relative paths, verify resolution from the repository:

``` bash
python - <<'PY'
from pathlib import Path

for p in [
    "models/kokoro/kokoro-v1.0.onnx",
    "models/kokoro/voices-v1.0.bin",
]:
    x = Path(p)
    print(x.resolve(), x.exists())
PY
```

### Benchmark missing

Check:

``` bash
ls -lh knowledge/immortal_pos1_7_41f.json
```

Validate JSON:

``` bash
python -m json.tool knowledge/immortal_pos1_7_41f.json >/dev/null && echo "JSON OK"
```

### Wave indicator missing

First inspect:

``` text
/api/gsi-information
```

The derived observation should contain lane-wave pressure when current
lane-creep evidence is sufficient.

The browser uses that same state to draw the TOP/MID/BOT dots.

### Enemy marker remains after disappearing

That is a bug. Enemy map coordinates must be current-packet-only.
Historical registry information must not be rendered as a current enemy
position.

### Structures look wrong

Remember the authority policy:

``` text
Own structures  → GSI
Enemy structures → manual dashboard input
```

Do not diagnose enemy structures from the minimap.

------------------------------------------------------------------------

## 25. Safe Update / Backup Procedure

Some project assets should be treated as local persistent data rather
than disposable source files.

Before replacing the project, back up at least:

``` text
.env
knowledge/immortal_pos1_7_41f.json
models/kokoro/
```

A practical backup:

``` bash
mkdir -p ~/dota-ai-backup

cp .env ~/dota-ai-backup/
cp knowledge/immortal_pos1_7_41f.json ~/dota-ai-backup/
cp -a models/kokoro ~/dota-ai-backup/
```

When distributing code updates, avoid packaging:

``` text
.env
__pycache__/
*.pyc
generated logs/
```

The generated Immortal benchmark should also be protected from
accidental replacement.

After applying an update, verify:

``` bash
python -m py_compile app.py
node --check static/app.js
python -m json.tool knowledge/immortal_pos1_7_41f.json >/dev/null
```

Then start the app and inspect `/health`.

------------------------------------------------------------------------

## 26. Recommended Test Sequence

After any significant update:

### 1. Server

``` bash
python app.py
```

Open `/health`.

### 2. GSI

Start DOTA and verify the dashboard changes to GSI connected.

### 3. Raw telemetry

Open:

``` text
/api/gsi-information
```

Confirm current packet and derived observation update.

### 4. Map

Verify:

-   five allied markers when available
-   currently visible enemies appear
-   enemies disappear when no longer currently visible
-   day/night indicator updates

### 5. Wave pressure

Verify TOP/MID/BOT dots appear and change as lane waves move.

### 6. Own structures

Verify GSI structure health/status changes correctly.

### 7. Enemy structures

Click enemy towers/RAX and verify the UI changes immediately.

### 8. AI structure context

After changing enemy structures, force or wait for the next live
analysis and verify Win Probability/Main Factors do not contradict the
dashboard structure state.

### 9. Event coach

Test:

-   kill
-   death
-   level spike
-   major item
-   multi-kill where possible

Verify Kokoro speaks without creating an OpenAI call.

### 10. Live strategist

Verify Recommendation, Win Probability, Carry Plan, and Strategy Support
update.

### 11. Benchmark

Verify GPM/LH/XPM trajectory charts populate.

### 12. Post-match

After a real terminal GSI state, verify the frozen dashboard and
Performance Report.

------------------------------------------------------------------------

## 27. Scope and Limitations

This project is a strategic assistant, not an omniscient DOTA observer.

Its strongest information is the local player's GSI state.

It does not have symmetric rich progression data for all ten players.
Therefore it should not claim to know enemy net worth, hidden inventory,
hidden cooldowns, or current fog positions without evidence.

The Win Probability is an interpretation of observable evidence, not an
official Valve statistic.

Wave pressure is a coarse map-level signal derived from current creep
coordinates. It is not exact lane simulation.

The benchmark measures carry pace, not complete player skill.

The system is intentionally conservative about hidden information
because strategic usefulness is more valuable when the factual boundary
is clear.

------------------------------------------------------------------------

## 28. Current V44.4 Feature Summary

V44.4 currently combines:

-   DOTA 2 GSI integration
-   stable accumulated local game state
-   current-visible-only enemy position policy
-   desktop/mobile dashboard
-   day/night map indicator
-   lane-wave pressure derivation
-   three-color map wave indicators
-   own-team GSI structure tracking
-   manual enemy structure controls
-   authoritative merged structure context for AI
-   Position 1 strategic state
-   patch-aware knowledge retrieval
-   hero-specific carry knowledge
-   Immortal--Divine Position 1 benchmark
-   GPM/LH/XPM trajectory charts
-   live Recommendation
-   live Win Probability
-   dynamic In-game Carry Plan
-   Strategy Support
-   local Kokoro voice
-   zero-API kill/death/event coach
-   one-time pre-game opening briefing
-   post-match Performance Report
-   API/token/cost tracking
-   raw GSI diagnostic endpoint
-   structure diagnostic tools

------------------------------------------------------------------------

## 29. Quick Start

``` bash
# 1. Activate environment
conda activate indonesia-ai

# 2. Enter project
cd ~/Desktop/Indonesia-AI/dota-ai-assistant

# 3. Confirm important local assets
ls -lh knowledge/immortal_pos1_7_41f.json
ls -lh models/kokoro/
grep -E '^(OPENAI_MODEL|AI_INTERVAL_PATTERN|PORT|VOICE_)' .env

# 4. Start
python app.py
```

Open:

``` text
http://127.0.0.1:5050
```

Then start DOTA 2.

The intended runtime flow is:

``` text
DOTA GSI
→ local world-state reconstruction
→ strategic derivation
→ dashboard
→ periodic AI carry reasoning
→ concise Kokoro coaching
```

The assistant should help the carry make better high-level decisions
while remaining explicit about what the game telemetry actually reveals.
