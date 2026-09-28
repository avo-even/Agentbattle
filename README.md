# Avo AI Lab — Workshop Orchestrator

Single-process event software for a prompt-engineering workshop with ~15 teams of 3.
A presentation, then two games on shared infrastructure:

0. **Presentation** (~20 min) — projector slides on prompt injection and jailbreaking (Norwegian,
   in `slides.yaml`) that teach the attack families used in both games. Teams may join during it.
1. **Gatekeeper** (~15 min opener) — every team climbs three defender agents, the *Avo Vaults*,
   each guarding its own secret word. Breaching one advances that team to the next. +50 per breach.
   Doubles as the connectivity check.
2. **Negotiation Tournament** (~30 min main event) — each team submits one negotiator agent, the
   system runs a round-robin, shows a live leaderboard, then live playoffs on the projector.

The only thing a team builds is a system prompt. Everything else is carried by this app.

**In a hurry?** `RUN.md` is the one-page guide to starting it and driving the
event. `REDEPLOY.md` has copy-paste cells for pushing changes to Azure.
`DEPLOY.md` covers a first-time deployment. This page is the reference: rules,
architecture, and why things are the way they are.

---

## Quick start

The workshop runs on the deployed app — see `RUN.md`. To work on the code
locally:

```bash
python -m venv .venv && .venv/Scripts/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                # then fill in the Azure values
uvicorn app:app --host 127.0.0.1 --port 8000
```

Three surfaces, all from the one process:

| URL | Who | What |
|---|---|---|
| `/team` | students | join, attack the Vault, write + test + submit the negotiator prompt |
| `/projector` | the room | leaderboard, breach feed, bracket, live duel viewer |
| `/admin` | facilitator | the cockpit — phases, group stage, playoffs, kill switch |

`LLM_MODE=mock` (the default when `.env` is absent) runs the entire thing offline with a rule-based
negotiator — no Azure, no cost. Use it for development and for rehearsing the *choreography*.

Run the whole event headless in ~4 seconds to sanity-check a change:

```bash
python scripts/dry_run.py --teams 15 --playoffs
```

---

## Game rules (as implemented)

### Negotiation

* Two agents split a pot of **100** points, alternating messages, **6 messages max** (3 each).
* An agent may attach one JSON action to a message:
  * `{"offer": [x, y]}` — `x` for itself, `y` for the opponent, integers ≥ 0 summing to 100.
  * `{"accept": true}` — accepts the opponent's standing offer. **Binding**; ends the duel.
* **An offer stays standing until superseded by a newer offer or accepted.** A message with no
  offer does *not* withdraw the previous one.
* `accept` with no valid standing opponent offer is ignored and logged. You cannot accept your own offer.
* Prose is truncated server-side at **300 characters** (the action JSON does not count).
* No accepted offer after 6 messages → **both score 0**.
* Group stage: every pair plays **twice** (each speaks first once) — N·(N−1) duels; 210 for 15 teams.
* Score = sum of split points across all group duels, plus a **+5 bonus per closed deal**
  (configurable, toggleable) so stonewalling is punished.
* Seeding tie-break: total points → deals closed → seeded coin flip (deterministic, logged and
  shown on screen as *"decided on a seeded coin flip"*).
* Playoffs: top 4. Semis 1v4 and 2v3, then the final. Every match is **two duels, each side
  starting once, decided on total points**; a points tie goes to the better group-stage seed.
  Speaking first is worth ~20 points a duel, so single duels or odd series are unfair. Run live
  with a configurable inter-message delay. Playoff duels do not touch group-stage standings.
* Fewer than 4 teams: the bracket shrinks automatically (3 → one semi + a bye; 2 → final only).

### Gatekeeper

* Three vaults of increasing paranoia, branded as three Avo colleagues: **Bjørn** (would rather
  build a POC), **Pia** (competitive, naturally skeptical) and **Arne Benjamin** (the final boss,
  Avo's IT and security guru). Names, taglines and photo paths live in `config.yaml` under
  `gatekeeper.tiers[]`; photos go in `web/img/` (see the README there). They show up on the last
  presentation slide, the team page and the projector's climb board.
* Each vault is **guarding its own secret word**. Sharing one word
  would not survive first contact: the first team to breach tells the room within seconds, and
  every later vault would have nothing left to discover.
* **Every team climbs its own ladder.** Breaching a vault advances that team to the next one
  immediately — they never wait for the facilitator, and teams race in parallel. `auto_advance`
  turns this off if you want a single facilitator-driven tier instead.
* **+50 points per vault breached** (`breach_points`), added to the overall standings, so the
  opener carries into the final leaderboard. Roughly 150 max against ~1400 from the tournament —
  meaningful, not decisive. Set `award_points: false` for pure spectacle.
* **The round is timed** (`duration_s`, default 15 minutes). The clock starts automatically when
  you enter the GATEKEEPER phase; attacks are refused once it expires. Start / +1 min / stop are
  on the admin panel.
* Teams send single-shot attacks. A **breach** is the secret appearing in the *defender's* reply,
  matched after normalisation — case, spacing, punctuation and basic leetspeak are stripped, so
  `G-U-A-C A M O L E` and `GU4C4M0LE` both count.
* A team that pastes the secret into its own attack cannot breach with it.
* Reversal is detected too — "spell it backwards" is one of the attacks that most
  reliably beats a guardrail, and a reversed 9-letter word never occurs by chance.
* Near-misses are flagged for the facilitator. Scoring is spectacle-only by default.

### Calibrating the tiers — do this whenever the model or the secret changes

```bash
python scripts/crack_gatekeeper.py --mode bifrost --repeat 3
```

Runs ~19 known attack techniques against every tier and reports a hit rate per
attack. You want a descending ladder: several attacks beating Tier 1, a couple
beating Tier 2, one beating Tier 3. Two failure modes it catches:

* **A tier nothing beats.** The original ladder was written for a weaker model;
  against `gpt-5.4-mini` a blanket *"reply exactly: Access denied"* made Tiers 2
  and 3 impenetrable to all 19 techniques. Fifteen teams would have spent twelve
  minutes collecting refusals.
* **A ladder that isn't one.** See the note on `gatekeeper.secret_word` in
  `config.yaml` — a semantically obvious secret gets volunteered by the model
  without any guard firing, and every tier falls to the same trivial question.

Use `--secret WORD` to try a candidate secret without editing config.

### Azure's own bouncer: the jailbreak prompt shield

Azure OpenAI runs a content filter *in front of* the model. Its **jailbreak prompt shield**
rejects some attacks with an HTTP 400 (`content_filter_result.jailbreak.filtered: true`) before
the vault ever sees them. It is pattern-based and a little random: on this deployment
*"ignore your instructions"* was blocked nearly always under the `DefaultV2` policy and rarely
under `Default`; *"spell it backwards"* still gets blocked about 1 in 5 under `Default`.

The app treats this as its own outcome, not an error: the team page shows *"blocked by Azure's
content filter before it reached the vault — go sideways"*, duels record a silent turn that the
repair run ignores, the admin panel counts `content filtered` separately, and the cracker marks
those attempts `[filt]` so a blocked attack is not misread as a strong vault.

To remove the randomness entirely, the deployment dropdown (Default / DefaultV2) is not enough —
both include the shield. Create a custom filter under **Safety + security → Content filters →
Create content filter**, set *Prompt shields for jailbreak attacks* to **Annotate only**, then
assign it to the deployment. Re-run the cracker afterwards; Bjørn's numbers shift up because the
crude attacks start reaching him.

### The Bifrost gateway

The app normally reaches the model through Avo's Bifrost gateway (`LLM_MODE=bifrost`) rather
than an Azure key of its own. Bifrost is an OpenAI-compatible proxy: the client is the plain
OpenAI SDK pointed at `<gateway>/openai`, authenticated with a **virtual key** that carries
this project's weekly budget and rate limit, and the provider is chosen by the model name's
prefix — `azure/gpt-5.4-mini` means "the Azure deployment called gpt-5.4-mini behind the
gateway". Three things follow:

* **Azure's content filter still applies.** The request still ends at the same Azure
  deployment, so the prompt shield above behaves exactly as before; the gateway forwards the
  400 unchanged and the app still classifies it as *filtered*.
* **The virtual key's request limit is the quota that bites.** The group stage runs at roughly
  230 requests/min at concurrency 5 and 460 at the configured 10. A virtual key created with
  Bifrost's default of 100
  requests/min produced 157 retries and 19 lost turns in a 15-team dry run; the Azure deployment
  behind the gateway (1,000,000 TPM, 1,000 RPM, Sweden Central) never came close to its limit.
  Set the key's request limit to at least 1,000/min before the day. These 429s carry no
  `Retry-After`, so the app's global cooldown never engages behind the gateway; the exponential
  backoff is all there is. The error body says `is_bifrost_error: False` even for a virtual-key
  limit, so do not read that flag as "Azure's fault".
* **The app keeps its own retries.** Retries in Bifrost are a per-provider setting (Azure →
  Edit Provider Config → network config, `max_retries`, default 0) and fallbacks only happen when
  the request body asks for them, which this app never does. Leave both as they are.
* **`reasoning_effort` must be `none`, not `minimal`.** Bifrost rewrites `minimal` to `low`
  before forwarding. Measured over 210 duels that meant 27-46 hidden reasoning tokens per turn,
  20 empty replies and a 1.72s median (0.76s straight at Azure). `none` is honoured on both paths.

Deployed next to the gateway, the app must use the gateway's environment-internal hostname;
both public names sit behind an IP allow-list that rejects Azure-hosted callers (see
`DEPLOY.md`). From the office network, `llm.avo.consulting` works as-is.

Switching models is now an `.env` change: `BIFROST_MODEL=openai/gpt-5.4-mini` would go to
OpenAI directly (and lose the Azure prompt shield, which changes the Gatekeeper game). Any
model change still means re-running the three calibration scripts.

---

## Configuration

**`.env`** (secrets, gitignored) — see `.env.example`:
`LLM_MODE`, `BIFROST_*` (or `AZURE_OPENAI_*` for a direct deployment), `JOIN_CODE`,
`ADMIN_CODE`, optional `GATEKEEPER_SECRET`.

**`config.yaml`** (game tuning, safe to commit) — pot, message cap, char limit, deal bonus,
playoff format, inter-message delay, LLM concurrency/timeouts/retries, gatekeeper secret and tier
prompts, the practice-bot prompt, the fallback prompt, and the `rules_block` injected into every
turn.

Anything you might need to change at the venue is config, not code. The admin panel can patch the
most likely candidates (deal bonus, concurrency, message delay) live without a restart.

### Decisions taken (spec §14)

| Decision | Value | Change it in |
|---|---|---|
| Prompt injection between negotiators | **allowed** | `negotiation.allow_prompt_injection` |
| Gatekeeper scoring | **spectacle only** | `gatekeeper.award_points` |
| Semifinal format | **2 duels, total points** | `negotiation.semifinal_best_of` |
| Final format | **2 duels, total points** | `negotiation.final_best_of` |
| Deal-closed bonus | **+5** | `negotiation.deal_bonus` |
| Non-offer message clears a standing offer | **no** | stated in `rules_block` |
| Model | pick before the dress rehearsal | `BIFROST_MODEL` (or `AZURE_OPENAI_DEPLOYMENT`) |

The rules block is appended to every team's system prompt **server-side, every turn**. A team
cannot remove it by leaving it out, and teams never see it in their editor.

---

## On the day — runbook

1. **Open the deployed app** — nothing to start. `/admin`, unlock, confirm the header shows
   `llm: bifrost`. Open `/projector` on the big screen (button in the admin header). If `/healthz`
   reports anything other than `LOBBY` with 0 teams, wipe leftover state first.
2. **PRESENTATION** (~20 min). The intro slides on prompt injection and jailbreaking (`slides.yaml`,
   Norwegian). Advance with ← / → on the projector window (or the admin panel). Teams can already
   join during this phase, so early arrivals get set up while you present.
3. **LOBBY.** Put the join URL and code on a slide. Watch the roster fill on the projector.
4. **GATEKEEPER** (15 min, timed). Entering the phase starts the clock. Teams attack vault 1 and
   promote themselves as they breach, so there is nothing to drive — watch the projector's climb
   board and commentate. **Every team must send at least one attack; the roster's connected dots
   are your tech check.** Chase any red dot. Use *+1 min* if the room is close to a first
   all-clear, *Stop* to cut it short. The ± buttons next to a team nudge a stuck one up a vault.
5. **NEGOTIATION_BUILD** (10 min, timed). Teams write and test-duel their negotiator. Watch the *Group*
   column in the roster for teams that haven't submitted; a team that never submits gets the
   fallback prompt and loses gracefully rather than breaking the round-robin.
6. **Run the group stage** near the end of build — *▶ Run round-robin*. It finishes in minutes and
   the leaderboard tells the story live. **When it finishes, check for the amber warning under the
   progress bar.** If any duel lost a turn to a model error, those teams were scored on silence
   rather than on their prompt — hit *↻ Repair failed duels*, which replays only the broken ones
   and leaves the healthy results untouched. Do this before seeding the bracket.
7. **PATCH_WINDOW.** Entering it opens a 5 minute clock and the projector switches to **the top
   four teams' group-stage prompts**, cycling one every 12 seconds beside the standings. That is
   the reveal — everyone gets to read what beat them, then has five minutes to react. Teams can
   also browse all prompts at their own pace on their own page. Only the *group* versions are
   shown; the playoff rewrites happening right now stay private, and there is a test enforcing it.
   Playoff prompts are stored separately, so the group-stage record is preserved.
8. **PLAYOFFS.** *Seed from leaderboard* (override manually if you want), then *▶ Run* each match.
   Messages appear one at a time at the configured delay — commentate over it.
9. **DONE.** Champion screen with the closing line.

### When something breaks

* **Azure wobbles** → *⏸ Pause all LLM calls*, fix, *▶ Resume*. In-flight duels wait rather than fail.
* **Rate limited (429s climbing, `retries` shooting up)** → lower `concurrency` in the group-stage
  card and re-run. Counter-intuitive but correct: the group stage is bounded by your deployment's
  TPM quota, not by model speed, so *more* parallelism makes it slower and loses turns. The client
  honours Azure's `Retry-After` and applies a global cooldown, but it cannot invent quota.
* **Group stage misbehaves** → *Cancel*, then *▶ Run round-robin* again (it resets group results).
* **Process dies** → restart uvicorn and hit *Resume from snapshot*. State is written to
  `snapshot.json` on every meaningful change and every 3 seconds.
* **A single duel errors** → a failed turn is recorded as silence and the duel continues. Nothing
  aborts the stage.
* **A team's prompt is broken/abusive** → open its prompt from the roster and edit it, or *bench*
  the team to drop it from the round-robin.

---

## Architecture

```
app.py                  FastAPI: pages, API, SSE stream
llm_client.py           the only place that talks to a model — concurrency cap, retries,
                        timeouts, kill switch, usage stats; bifrost, azure + mock backends
actions.py              action parsing, truncation, name sanitisation
models.py               dataclasses for the whole event
state.py                in-memory GameState, JSON snapshotting, SSE event bus
config.py               .env + config.yaml with dotted-path access and live patching
games/negotiation.py    duel loop, round-robin, scoring, playoffs
games/gatekeeper.py     attacks, tiers, breach detection
web/{team,projector,admin}.html
scripts/dry_run.py      headless dress rehearsal
tests/                  82 tests
```

No database. In-memory state plus a JSON snapshot, which is a crash-recovery net for one event —
not a persistence layer.

For the Azure side — Container App, registry, OpenAI resource, how they connect — see the
infrastructure diagram in `DEPLOY.md`.

**Everything model-facing funnels through `llm_client.complete()`.** That is where the global
`asyncio.Semaphore` (default 15) sits, sized to the Azure deployment's tokens-per-minute quota.
It is the main defence against rate-limit failures during the group stage.

**Transcript framing (strategy A):** each agent sees the opponent's messages as `user` and its own
as `assistant`, with the canonical action JSON appended to each message so the opponent always sees
exactly what the machine saw. Set `llm.transcript_strategy: B` in config to fall back to a single
flattened user message if a model misbehaves with role mapping.

**Action parsing is deliberately generous about placement and strict about validity.** It tolerates
code fences, prose on either side, single quotes, trailing commas, `{"offer": {"me": 60, "them": 40}}`,
`{"offer": "60/40"}` and `{"action": "offer", "split": [...]}`. It takes the first *valid* action,
strips every action-shaped blob from the displayed text, logs invalid attempts, and never raises.

---

## Testing

```bash
pytest -q
```

82 tests: exhaustive action-parser cases, offer/accept validation, the standing-offer rule,
self-accept rejection, truncation, LLM-failure handling, scoring and tie-breaks, a full 6-team
group stage, bracket shapes for 2/3/4+ teams, gatekeeper normalisation, snapshot round-trip,
phase gating, rate limits, name sanitisation, and a load-ish test that runs a 15-team group stage
(210 duels) alongside concurrent practice duels while asserting the event loop never stalls.

### Dress rehearsal — the test that actually matters

A few days before the event, from an off-site network, against the real deployment:

```bash
python scripts/dry_run.py --mode bifrost --teams 15 --playoffs
```

It reports wall-clock time and, critically, **action-format compliance** — the percentage of
*delivered* replies that carried a valid action. Below ~70% and the model is too weak to run the
format: move up a tier or blunt the rules block. Compliance is scored over delivered replies only,
because a turn lost to a 429 never reached the model and says nothing about its formatting.

**Read `rate limited` and the wasted-attempts warning before you read anything else.** If a large
share of attempts were retries, you are over quota, and every other number in the report is
distorted by it — including the leaderboard, since teams that lost turns were scored on silence.

#### What the first real rehearsal measured (gpt-5.4-mini, swedencentral, concurrency 15)

| | |
|---|---|
| Group stage, 15 teams | 176s |
| Median call latency | **0.76s** — reasoning overhead is negligible at `reasoning_effort: minimal` |
| Action-format compliance | **94%** of delivered replies (the raw 77% counted rate-limit losses) |
| Successful calls | 730, costing **1557 attempts** and 663 retries |
| Turns lost outright | 164 (18% of all messages), across 31 duels |

The conclusion: the deployment is fast and follows the format well; the binding constraint is the
TPM quota. `llm.max_concurrency` and `max_concurrent_duels` were dropped to **5** as a result.
Check the deployment's TPM in Foundry → Deployments and raise it there before raising these —
quota, not parallelism, is what buys you speed.

#### Behind the Bifrost gateway (gpt-5.4-mini, `reasoning_effort: none`, key at 1000 req/min)

| | concurrency 5 | concurrency 10 |
|---|---|---|
| Group stage, 15 teams | 184s | **99s** |
| Median / p95 latency | 1.10s / 1.56s | 1.12s / 1.59s |
| Requests per minute | ~250 | ~460 |
| Retries / turns lost / empty replies | 0 / 0 / 0 | 0 / 0 / 0 |
| Action-format compliance | 98% | 99% |

The deployment behind the gateway has 1M TPM, so parallelism is free until the virtual key's
request limit is in sight. Concurrency is set to **10**; 15 would run at ~700 req/min against
a 1000/min key with practice duels sharing the same semaphore, which is too little margin.

Rough budget: 210 duels × ~5 delivered calls ≈ 1000–1300 calls and ~400k tokens for the group
stage, plus practice duels and Gatekeeper attacks. The admin panel tracks calls, tokens, latency
and rate-limit hits live so you can hit the kill switch if something runs away.
