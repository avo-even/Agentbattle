# Run it

The workshop runs on the **deployed app**. There is nothing to start on the day —
you open two browser tabs and drive it from the admin panel.

`README.md` has the game rules and the reasoning. `REDEPLOY.md` covers pushing
changes. This page is what you need in the room.

---

## The three URLs

Base: **https://avo-lab.lemonfield-d248f0b0.swedencentral.azurecontainerapps.io**

| | URL | Who |
|---|---|---|
| **teams** | `…/team` | the students — this is what goes on the slide and the cards |
| **projector** | `…/projector` | the big screen |
| **facilitator** | `…/admin` | you |

Join code and admin code are the `JOIN_CODE` / `ADMIN_CODE` secrets on the
Container App. Students need only the team URL and the join code.

Confirm it is awake before the session starts:

```bash
curl https://avo-lab.lemonfield-d248f0b0.swedencentral.azurecontainerapps.io/healthz
```

Expect `{"ok":true,"phase":"LOBBY","teams":0}`. If `phase` is anything else, or
`teams` is non-zero, wipe leftover state: `/admin` → type `RESET` → *Wipe event*.

---

## Setting up the room

1. Open `/admin` on your laptop, unlock with the admin code.
2. Click **Open projector** — put that window on the big screen, full screen.
3. Check the header: it should read `llm: bifrost` and `projector: 1`.
4. Set the phase to **PRESENTATION** to open with the intro slides. Advancing them
   with the arrow keys only works from a projector window opened on a browser that
   has logged into `/admin` (same machine) — the admin code lives in that browser.
   The Presentation card in `/admin` has ◀ ▶ as a backup.

The slide deck is `slides.yaml` — plain text, edit freely, but a change needs a
redeploy to reach the live app (see `REDEPLOY.md`).

---

## Driving the event

You only ever click phase buttons. Everything else follows.

| Phase | What happens | Your job |
|---|---|---|
| **PRESENTATION** | the intro slides (`slides.yaml`) show on the projector; teams may already join | present. Advance with **← / →** on the projector window, or the Presentation card in `/admin` |
| **LOBBY** | teams join | wait for the roster |
| **GATEKEEPER** | 15 min clock starts automatically; teams climb the three vaults independently | commentate the climb board; chase red dots on the roster — a team that has not sent one attack has not connected |
| **NEGOTIATION_BUILD** | 15 min clock starts automatically; teams write and test-duel | watch the *Group* column for teams that have not submitted |
| **GROUP_STAGE** | click **▶ Run round-robin** | commentate the leaderboard. If the amber warning appears, hit **↻ Repair failed duels** before seeding |
| **PATCH_WINDOW** | 5 min clock; projector reveals the top four prompts, cycling | let them read, then react |
| **PLAYOFFS** | **Seed from leaderboard**, then **▶ Run** each match | commentate |
| **DONE** | champion screen and closing line | — |

### If something goes wrong

| Symptom | Fix |
|---|---|
| Azure wobbling, errors climbing | **⏸ Pause all LLM calls**, wait, **▶ Resume** |
| 429s / retries climbing | lower `concurrency` in the Group stage card, re-run |
| Group stage misbehaved | **Cancel**, then **▶ Run round-robin** again |
| Some duels lost a turn to an error | **↻ Repair failed duels** — replays only the broken ones |
| A team's prompt is broken or abusive | open it from the roster and edit, or **bench** the team |
| Everything is wedged | `/admin` → `RESET` → *Wipe event* |
| The app itself died | it restarts on its own; then **Resume from snapshot** |

**Never redeploy during the workshop** — it restarts the container and wipes all
event state. See `REDEPLOY.md`.

---

## Running it locally (development only)

Not for the workshop. Use this to try changes before pushing them.

```bash
.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000
```

Then http://localhost:8000/admin. To run free, with no Azure calls at all:

```bash
$env:LLM_MODE='mock'; .venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000
```

`mock` swaps in a rule-based negotiator, so the full event works offline — good
for rehearsing the phase choreography without spending anything.

### First time on a new machine

```bash
python -m venv .venv
```

```bash
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

```bash
copy .env.example .env
```

Fill in `.env` (`LLM_MODE=bifrost`, the gateway URL, the virtual key, the
model, `JOIN_CODE`, `ADMIN_CODE`), then check the model config:

```bash
.venv\Scripts\python.exe scripts\check_llm.py
```

Two calls. It names which value is wrong rather than leaving you to guess, and
reports latency and action-format compliance.

---

## Before the day

Full 15-team event, headless, free, ~13 seconds:

```bash
.venv\Scripts\python.exe scripts\dry_run.py --teams 15 --playoffs
```

Add `--mode bifrost` for the real rehearsal (~1300 billed calls; it prints a
5-second abort warning first).

Re-check the vaults whenever you change a tier prompt, a secret word, or the
model — a vault nobody can breach kills the opener:

```bash
.venv\Scripts\python.exe scripts\crack_gatekeeper.py --mode bifrost --repeat 3
```

Tests, ~5 seconds, no network:

```bash
.venv\Scripts\python.exe -m pytest -q
```
