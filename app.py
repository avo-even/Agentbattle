"""Avo AI Lab — Workshop Orchestrator.

One FastAPI process serves the team page, the projector, the admin panel, the
API and the SSE stream.

The workshop runs on the deployed Azure Container App (see REDEPLOY.md). Locally,
for development:

    uvicorn app:app --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import Any, Optional

from fastapi import Body, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import config
from actions import sanitize_name
from config import cfg
from games import gatekeeper as gk
from games import negotiation as neg
from llm_client import llm
from models import Phase, PHASE_ORDER
from state import (
    bus,
    load_snapshot,
    mark_dirty,
    publish_leaderboard,
    save_snapshot,
    set_phase,
    snapshot_info,
    snapshot_loop,
    state,
)

app = FastAPI(title="Avo AI Lab — Workshop Orchestrator", docs_url=None, redoc_url=None)

# Persona photos for the Gatekeeper vaults (web/img/bjorn.jpg etc.). The
# directory is created so a checkout without the photos still boots; the
# frontend falls back to initials for any file that is missing.
_IMG_DIR = config.WEB_DIR / "img"
_IMG_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/img", StaticFiles(directory=str(_IMG_DIR)), name="img")

_background: dict[str, asyncio.Task] = {}


# --- lifecycle ---------------------------------------------------------------

def _print_banner() -> None:
    port = int(config.env("PORT", "8000") or 8000)
    # Plain ASCII on purpose: this is read through `az containerapp logs` and
    # Windows consoles, where box-drawing characters turn to mojibake.
    rule = "=" * 68
    lines = [
        "",
        rule,
        "  AVO AI LAB - WORKSHOP ORCHESTRATOR",
        rule,
        f"  mode        {config.LLM_MODE}"
        + (f"  ({config.model_label()})" if config.is_live() else ""),
        f"  join code   {config.JOIN_CODE}",
    ]

    # Azure Container Apps injects these when running as the deployed app, which
    # is the only way the workshop is ever run. A local run is for development.
    app_name = config.env("CONTAINER_APP_NAME")
    dns_suffix = config.env("CONTAINER_APP_ENV_DNS_SUFFIX")

    if app_name and dns_suffix:
        base = f"https://{app_name}.{dns_suffix}"
        lines.append("")
        lines.append(f"  Deployed as '{app_name}'"
                     + (f" revision {config.env('CONTAINER_APP_REVISION')}"
                        if config.env("CONTAINER_APP_REVISION") else ""))
        lines.append(f"    teams        {base}/team")
        lines.append(f"    projector    {base}/projector")
        lines.append(f"    facilitator  {base}/admin")
    else:
        lines.append("")
        lines.append("  Local development run:")
        lines.append(f"    team         http://localhost:{port}/team")
        lines.append(f"    projector    http://localhost:{port}/projector")
        lines.append(f"    facilitator  http://localhost:{port}/admin")
        lines.append("")
        lines.append("  Run the workshop itself on the deployed app - see REDEPLOY.md.")
    if config.ADMIN_CODE == "changeme-admin":
        lines.append("")
        lines.append("  !! ADMIN_CODE is still the default. Change it in .env.")
    lines.append(rule)
    lines.append("")
    print("\n".join(lines), flush=True)


@app.on_event("startup")
async def _startup() -> None:
    _background["snapshot"] = asyncio.create_task(snapshot_loop(3.0))
    _print_banner()


@app.on_event("shutdown")
async def _shutdown() -> None:
    for task in _background.values():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    save_snapshot(force=True)
    await llm.aclose()


# --- auth helpers ------------------------------------------------------------

def require_team(token: Optional[str]):
    team = state.team_by_token(token or "")
    if team is None:
        raise HTTPException(status_code=401, detail="Ukjent lag-token. Bli med på nytt.")
    team.last_seen = time.time()
    return team


def require_admin(code: Optional[str]) -> None:
    if (code or "") != config.ADMIN_CODE:
        raise HTTPException(status_code=401, detail="Bad admin code")


def require_phase(*allowed: Phase) -> None:
    if state.phase not in allowed:
        names = " eller ".join(p.value for p in allowed)
        raise HTTPException(
            status_code=409,
            detail=f"Ikke tilgjengelig nå (dette skjer i {names}, vi er i {state.phase.value}).",
        )


# --- pages -------------------------------------------------------------------

def _page(name: str) -> FileResponse:
    path = config.WEB_DIR / name
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"{name} missing")
    return FileResponse(path, headers={"Cache-Control": "no-store"})


@app.get("/")
async def root():
    return RedirectResponse("/team")


@app.get("/team")
async def team_page():
    return _page("team.html")


@app.get("/projector")
async def projector_page():
    return _page("projector.html")


@app.get("/admin")
async def admin_page():
    return _page("admin.html")


@app.get("/healthz")
async def healthz():
    return {"ok": True, "phase": state.phase.value, "teams": len(state.teams)}


# --- SSE ---------------------------------------------------------------------

@app.get("/events")
async def events(request: Request):
    queue = bus.subscribe()

    async def stream():
        try:
            yield _sse("hello", {"phase": state.phase.value})
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield _sse(event["type"], event["payload"], event["seq"])
        finally:
            bus.unsubscribe(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


def _seconds_left(value: Optional[float]) -> Optional[int]:
    return None if value is None else round(value)


def _sse(kind: str, payload: Any, seq: int = 0) -> str:
    return f"id: {seq}\nevent: {kind}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


# --- team API ----------------------------------------------------------------

@app.post("/api/join")
async def api_join(body: dict = Body(...)):
    code = str(body.get("code", "")).strip()
    if code.upper() != config.JOIN_CODE.upper():
        raise HTTPException(status_code=403, detail="Feil kode")
    name = sanitize_name(body.get("name", ""))
    if not name:
        raise HTTPException(status_code=400, detail="Velg et lagnavn")
    team = state.add_team(name)
    mark_dirty()
    save_snapshot(force=True)
    bus.publish("roster", _roster_payload())
    return {"token": team.token, "team": team.to_dict()}


@app.get("/api/team/state")
async def api_team_state(x_team_token: Optional[str] = Header(default=None)):
    team = require_team(x_team_token)
    phase = state.phase
    prompt_version = "playoff" if phase == Phase.PATCH_WINDOW else "group"
    agent = state.agents.get((team.id, prompt_version))
    fallback = state.agents.get((team.id, "group"))

    payload: dict[str, Any] = {
        "phase": phase.value,
        "team": team.to_dict(),
        "join_code_ok": True,
        "event_title": cfg("event.title", "AVO AI LAB"),
        "char_limit": int(cfg("negotiation.team_prompt_char_limit", 4000)),
        "message_char_limit": int(cfg("negotiation.message_char_limit", 300)),
        "max_messages": int(cfg("negotiation.max_messages", 6)),
        "pot": int(cfg("negotiation.pot", 100)),
        "prompt_version": prompt_version,
        "prompt": (agent.system_prompt if agent else (fallback.system_prompt if fallback else "")),
        "submitted": agent is not None,
        "submitted_group": (team.id, "group") in state.agents,
        "submitted_playoff": (team.id, "playoff") in state.agents,
        "submission_ends_at": state.submission_ends_at,
        "submission_seconds_left": _seconds_left(state.submission_seconds_left()),
        "llm_paused": llm.paused,
    }
    if phase == Phase.GATEKEEPER:
        tiers = gk.tiers()
        my_tier = gk.team_tier_index(team.id)
        my_breaches = len({b["tier_index"] for b in state.gatekeeper.breaches_by(team.id)})
        left = gk.time_left()
        board = gk.gatekeeper_payload()["standings"]
        payload["gatekeeper"] = {
            "tier_index": my_tier,
            "tier_name": tiers[my_tier]["name"] if my_tier < len(tiers) else None,
            "tier_tagline": tiers[my_tier].get("tagline", "") if my_tier < len(tiers) else "",
            "tier_image": tiers[my_tier].get("image", "") if my_tier < len(tiers) else "",
            "tier_count": len(tiers),
            "breached": my_breaches,
            "points": my_breaches * int(cfg("gatekeeper.breach_points", 0) or 0),
            "breach_points": int(cfg("gatekeeper.breach_points", 0) or 0),
            "cleared_all": gk.team_finished(team.id),
            "my_attempts": sum(1 for a in state.gatekeeper.attempts if a.team_id == team.id),
            "ends_at": state.gatekeeper.ends_at,
            "seconds_left": None if left is None else round(left),
            "round_over": gk.round_over(),
            "my_rank": next((e["rank"] for e in board if e["team_id"] == team.id), None),
            "standings": board[:8],
        }
    if phase in (Phase.GROUP_STAGE, Phase.PATCH_WINDOW, Phase.PLAYOFFS, Phase.DONE):
        payload["leaderboard"] = state.leaderboard()[:10]
        payload["progress"] = state.group_progress.to_dict()
        payload["my_rank"] = next(
            (e["rank"] for e in state.leaderboard() if e["team_id"] == team.id), None
        )
    return payload


@app.post("/api/team/attack")
async def api_team_attack(
    body: dict = Body(...), x_team_token: Optional[str] = Header(default=None)
):
    team = require_team(x_team_token)
    require_phase(Phase.GATEKEEPER)
    if gk.round_over():
        raise HTTPException(status_code=409, detail="Tiden er ute. Hvelvene er stengt.")
    if gk.team_finished(team.id):
        raise HTTPException(status_code=409, detail="Dere har åpnet alle hvelvene. Ingenting igjen å knekke.")
    ok, wait = state.rate_ok(f"attack:{team.id}", float(cfg("gatekeeper.attack_cooldown_s", 3)))
    if not ok:
        raise HTTPException(status_code=429, detail=f"Ro ned. {wait:.1f} s igjen.")
    message = str(body.get("message", "")).strip()
    if not message:
        raise HTTPException(status_code=400, detail="Skriv et angrep først")
    try:
        attempt = await gk.run_attack(team.id, message)
    except gk.AllTiersCleared as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    tiers = gk.tiers()
    next_index = gk.team_tier_index(team.id)
    return {
        "reply": attempt.reply,
        "breached": attempt.breached,
        "tier_name": tiers[attempt.tier_index]["name"] if tiers else "",
        "points": int(cfg("gatekeeper.breach_points", 0) or 0) if attempt.breached else 0,
        "next_tier_index": next_index,
        "next_tier_name": tiers[next_index]["name"] if next_index < len(tiers) else None,
        "cleared_all": gk.team_finished(team.id),
        "filtered": attempt.filtered,
        "error": attempt.error,
    }


@app.post("/api/team/prompt")
async def api_team_prompt(
    body: dict = Body(...), x_team_token: Optional[str] = Header(default=None)
):
    team = require_team(x_team_token)
    require_phase(Phase.NEGOTIATION_BUILD, Phase.PATCH_WINDOW)
    if state.submissions_closed():
        raise HTTPException(
            status_code=409,
            detail="Innsendingsvinduet er stengt. Den sist lagrede prompten er den som kjører.",
        )
    prompt = str(body.get("prompt", "")).strip()
    limit = int(cfg("negotiation.team_prompt_char_limit", 4000))
    if not prompt:
        raise HTTPException(status_code=400, detail="Prompten er tom")
    if len(prompt) > limit:
        raise HTTPException(status_code=400, detail=f"Prompten er over grensen på {limit} tegn")
    version = "playoff" if state.phase == Phase.PATCH_WINDOW else "group"
    state.set_agent(team.id, prompt, version)
    mark_dirty()
    save_snapshot(force=True)
    bus.publish("roster", _roster_payload())
    return {"ok": True, "version": version, "submitted_at": time.time()}


@app.post("/api/team/test")
async def api_team_test(
    body: dict = Body(...), x_team_token: Optional[str] = Header(default=None)
):
    team = require_team(x_team_token)
    require_phase(Phase.NEGOTIATION_BUILD, Phase.PATCH_WINDOW)
    if state.submissions_closed():
        raise HTTPException(status_code=409, detail="Innsendingsvinduet er stengt.")
    cooldown = float(cfg("negotiation.test_duel_cooldown_s", 15))
    ok, wait = state.rate_ok(f"test:{team.id}", cooldown)
    if not ok:
        raise HTTPException(status_code=429, detail=f"Testduellene er begrenset. {wait:.0f} s igjen.")
    prompt = str(body.get("prompt", "")).strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Skriv en prompt først")
    limit = int(cfg("negotiation.team_prompt_char_limit", 4000))
    if len(prompt) > limit:
        raise HTTPException(status_code=400, detail=f"Prompten er over grensen på {limit} tegn")
    team_first = bool(body.get("team_first", True))
    result = await neg.run_practice_duel(team.id, prompt, team_first=team_first)
    team.requests += 1
    # Apply the same deal bonus the tournament uses, otherwise a practice score
    # of 60 and a tournament score of 65 for the identical duel looks like a bug.
    bonus_enabled = bool(cfg("negotiation.deal_bonus_enabled", True))
    bonus = int(cfg("negotiation.deal_bonus", 5)) if bonus_enabled else 0
    earned = int(result.scores.get(team.id, 0))
    deal_bonus = bonus if result.closed_deal else 0
    return {
        "transcript": [m.to_dict() for m in result.transcript],
        "closed_deal": result.closed_deal,
        "deadlocked": result.deadlocked,
        "scores": result.scores,
        "you": team.id,
        "your_points": earned,
        "deal_bonus": deal_bonus,
        "your_total": earned + deal_bonus,
        "bot_points": int(sum(result.scores.values()) - earned),
        "errors": result.errors,
    }


@app.get("/api/team/prompts")
async def api_team_prompts(x_team_token: Optional[str] = Header(default=None)):
    """Every team's group-stage prompt — the scouting report for the patch window.

    Only the group version is exposed: those prompts already played out in front
    of the room. Playoff prompts stay private while the window is open, or the
    last team to submit would just copy the best one.
    """
    team = require_team(x_team_token)
    require_phase(Phase.PATCH_WINDOW)
    ranked = {e["team_id"]: e for e in state.leaderboard()}
    entries = []
    for other in state.teams.values():
        agent = state.agents.get((other.id, "group"))
        board = ranked.get(other.id, {})
        entries.append(
            {
                "team_id": other.id,
                "name": other.display_name,
                "is_you": other.id == team.id,
                "submitted": agent is not None,
                "prompt": agent.system_prompt if agent else "",
                "rank": board.get("rank"),
                "points": board.get("points", 0),
                "deals_closed": board.get("deals_closed", 0),
            }
        )
    entries.sort(key=lambda e: (e["rank"] is None, e["rank"] or 0))
    return {"prompts": entries, "count": len(entries)}


def _roster_payload() -> dict:
    return {
        "teams": [
            {
                "team_id": t.id,
                "name": t.display_name,
                "connected": t.connected,
                "requests": t.requests,
                "active": t.active,
                "submitted": (t.id, "group") in state.agents,
                "submitted_playoff": (t.id, "playoff") in state.agents,
            }
            for t in state.teams.values()
        ],
        "count": len(state.teams),
    }


# --- projector API -----------------------------------------------------------

@app.get("/api/projector")
async def api_projector():
    """Full state for a freshly-loaded (or reconnected) projector."""
    return {
        "phase": state.phase.value,
        "event": {
            "title": cfg("event.title", "AVO AI LAB"),
            "subtitle": cfg("event.subtitle", ""),
        },
        "roster": _roster_payload(),
        "gatekeeper": gk.gatekeeper_payload(),
        "leaderboard": state.leaderboard(),
        "progress": state.group_progress.to_dict(),
        "bracket": neg.bracket_payload(),
        "highlights": _recent_highlights(12),
        "finalists": _finalist_prompts(),
        "submission_ends_at": state.submission_ends_at,
        "submission_seconds_left": _seconds_left(state.submission_seconds_left()),
        "slides": _slides(),
        "slide_index": _clamp_slide(state.slide_index),
        "closing_line": "Same model for everyone. Only the prompt differs. The prompt is everything.",
    }


def _slides() -> list[dict]:
    import config as _config

    return _config.slides()


def _clamp_slide(index: int) -> int:
    total = len(_slides())
    if total == 0:
        return 0
    return max(0, min(int(index), total - 1))


def _finalist_prompts() -> list[dict]:
    """Top seeds' GROUP-STAGE prompts, revealed on the projector for the patch window.

    Group-stage prompts only, never the playoff versions being written right now —
    those are still secret, and leaking them mid-window would be unfair.
    """
    if state.phase not in (Phase.PATCH_WINDOW, Phase.PLAYOFFS, Phase.DONE):
        return []
    size = int(cfg("negotiation.playoff_size", 4))
    board = [e for e in state.leaderboard() if e["active"]][:size]
    out = []
    for entry in board:
        agent = state.agents.get((entry["team_id"], "group"))
        out.append(
            {
                "rank": entry["rank"],
                "team_id": entry["team_id"],
                "name": entry["name"],
                "points": entry["points"],
                "deals_closed": entry["deals_closed"],
                "submitted": agent is not None,
                "prompt": agent.system_prompt if agent else "",
            }
        )
    return out


def _recent_highlights(limit: int) -> list[dict]:
    out = []
    for result in sorted(
        state.results_for_stage("group"), key=lambda r: r.ended_at, reverse=True
    )[: limit * 2]:
        highlight = neg.pick_highlight(result)
        if highlight:
            out.append(highlight)
        if len(out) >= limit:
            break
    return out


# --- admin API ---------------------------------------------------------------

@app.post("/api/admin/auth")
async def api_admin_auth(body: dict = Body(...)):
    if str(body.get("code", "")) != config.ADMIN_CODE:
        raise HTTPException(status_code=401, detail="Bad admin code")
    return {"ok": True}


@app.get("/api/admin/state")
async def api_admin_state(x_admin_code: Optional[str] = Header(default=None)):
    require_admin(x_admin_code)
    return {
        "phase": state.phase.value,
        "phases": [p.value for p in PHASE_ORDER],
        "join_code": config.JOIN_CODE,
        "roster": _roster_payload(),
        "gatekeeper": gk.gatekeeper_payload(),
        "leaderboard": state.leaderboard(),
        "progress": {
            **state.group_progress.to_dict(),
            # Duels where a turn was lost to a model error — those teams were
            # scored on silence, not on their prompt. Repairable.
            "duels_with_errors": sum(
                1 for r in state.results_for_stage("group") if r.errors
            ),
        },
        "bracket": neg.bracket_payload(),
        "llm": llm.snapshot_stats(),
        "snapshot": snapshot_info(),
        "submission_ends_at": state.submission_ends_at,
        "submission_seconds_left": _seconds_left(state.submission_seconds_left()),
        "config": {
            "negotiation": cfg("negotiation", {}),
            "llm": {k: v for k, v in (cfg("llm", {}) or {}).items()},
            # Facilitator-only. gk.gatekeeper_payload() deliberately carries no
            # secrets, because the projector and team pages consume it too.
            "gatekeeper_secret": gk.current_secret(),
            "gatekeeper_tier_secrets": [
                {"name": t["name"], "secret": t["secret"]} for t in gk.tiers()
            ],
        },
        "projector_clients": bus.subscriber_count,
        "running": {name: not t.done() for name, t in _background.items() if name != "snapshot"},
        "slide_index": _clamp_slide(state.slide_index),
        "slide_count": len(_slides()),
        "slide_titles": [s.get("title", "") for s in _slides()],
    }


@app.post("/api/admin/slide")
async def api_admin_slide(
    body: dict = Body(default={}), x_admin_code: Optional[str] = Header(default=None)
):
    """Advance the presentation. Driven by arrow keys on the projector or the panel."""
    require_admin(x_admin_code)
    total = len(_slides())
    action = str(body.get("action", "set")).lower()
    if action == "next":
        target = state.slide_index + 1
    elif action == "prev":
        target = state.slide_index - 1
    else:
        target = int(body.get("index", 0))
    state.slide_index = 0 if total == 0 else max(0, min(target, total - 1))
    mark_dirty()
    bus.publish("slide", {"index": state.slide_index, "count": total})
    return {"ok": True, "index": state.slide_index, "count": total}


@app.post("/api/admin/phase")
async def api_admin_phase(
    body: dict = Body(...), x_admin_code: Optional[str] = Header(default=None)
):
    require_admin(x_admin_code)
    try:
        phase = Phase(str(body.get("phase", "")).upper())
    except ValueError:
        raise HTTPException(status_code=400, detail="Unknown phase")
    # Both prompt-writing phases run on the same clock, started on phase entry.
    if phase in (Phase.NEGOTIATION_BUILD, Phase.PATCH_WINDOW):
        key = ("negotiation.build_window_minutes" if phase == Phase.NEGOTIATION_BUILD
               else "negotiation.patch_window_minutes")
        default = float(cfg(key, 15 if phase == Phase.NEGOTIATION_BUILD else 5)) * 60
        duration = float(body.get("duration_s", default) or 0)
        state.submission_ends_at = time.time() + duration if duration else 0.0
    set_phase(phase)
    if phase == Phase.GATEKEEPER and not state.gatekeeper.started_at:
        # Entering the phase starts the clock — one less thing to remember live.
        gk.start_round()
    if phase == Phase.PLAYOFFS and not state.bracket.matches:
        state.bracket = neg.build_bracket()
        bus.publish("bracket", neg.bracket_payload())
    return {"ok": True, "phase": phase.value}


@app.post("/api/admin/config")
async def api_admin_config(
    body: dict = Body(...), x_admin_code: Optional[str] = Header(default=None)
):
    require_admin(x_admin_code)
    path = str(body.get("path", ""))
    if not path:
        raise HTTPException(status_code=400, detail="Missing config path")
    config.patch_config(path, body.get("value"))
    if path.startswith("llm."):
        llm.reset_backend()
    if path.startswith("negotiation.deal_bonus"):
        state.recompute_standings()
        publish_leaderboard()
    return {"ok": True, "path": path, "value": cfg(path)}


@app.post("/api/admin/gatekeeper/tier")
async def api_admin_tier(
    body: dict = Body(...), x_admin_code: Optional[str] = Header(default=None)
):
    require_admin(x_admin_code)
    index = body.get("index")
    if index is None:
        index = gk.current_tier_index() + 1
    return {"ok": True, "tier_index": gk.set_tier(int(index))}


@app.post("/api/admin/gatekeeper/round")
async def api_admin_gk_round(
    body: dict = Body(default={}), x_admin_code: Optional[str] = Header(default=None)
):
    """Start, extend or stop the timed Gatekeeper round."""
    require_admin(x_admin_code)
    action = str(body.get("action", "start")).lower()
    if action == "start":
        minutes = body.get("minutes")
        duration = float(minutes) * 60 if minutes is not None else None
        gk.start_round(duration)
    elif action == "extend":
        gk.extend_round(float(body.get("minutes", 1)) * 60)
    elif action == "stop":
        gk.stop_round()
    else:
        raise HTTPException(status_code=400, detail="action must be start, extend or stop")
    left = gk.time_left()
    return {"ok": True, "ends_at": state.gatekeeper.ends_at,
            "seconds_left": None if left is None else round(left)}


@app.post("/api/admin/gatekeeper/team_tier")
async def api_admin_team_tier(
    body: dict = Body(...), x_admin_code: Optional[str] = Header(default=None)
):
    """Nudge one team up or down the ladder (stuck team, or an unfair loss)."""
    require_admin(x_admin_code)
    team_id = str(body.get("team_id", ""))
    if team_id not in state.teams:
        raise HTTPException(status_code=404, detail="Unknown team")
    index = gk.set_team_tier(team_id, int(body.get("index", 0)))
    save_snapshot(force=True)
    bus.publish("gatekeeper_state", gk.gatekeeper_payload())
    return {"ok": True, "team_id": team_id, "tier_index": index}


@app.post("/api/admin/gatekeeper/reset")
async def api_admin_gk_reset(x_admin_code: Optional[str] = Header(default=None)):
    require_admin(x_admin_code)
    gk.reset_gatekeeper()
    return {"ok": True}


@app.get("/api/admin/gatekeeper/attempts")
async def api_admin_gk_attempts(
    limit: int = 60, x_admin_code: Optional[str] = Header(default=None)
):
    require_admin(x_admin_code)
    return gk.gatekeeper_payload(include_attempts=max(1, min(limit, 300)))


@app.post("/api/admin/group_stage/run")
async def api_admin_group_run(
    body: dict = Body(default={}), x_admin_code: Optional[str] = Header(default=None)
):
    require_admin(x_admin_code)
    task = _background.get("group_stage")
    if task and not task.done():
        raise HTTPException(status_code=409, detail="Group stage is already running")
    reset = bool(body.get("reset", True))
    if state.phase != Phase.GROUP_STAGE:
        set_phase(Phase.GROUP_STAGE)

    async def runner():
        try:
            await neg.run_group_stage(reset=reset)
        except Exception as exc:  # noqa: BLE001
            state.group_progress.running = False
            state.group_progress.error = str(exc)
            bus.publish("error", {"where": "group_stage", "message": str(exc)})

    _background["group_stage"] = asyncio.create_task(runner())
    return {"ok": True}


@app.post("/api/admin/group_stage/cancel")
async def api_admin_group_cancel(x_admin_code: Optional[str] = Header(default=None)):
    require_admin(x_admin_code)
    task = _background.get("group_stage")
    if task and not task.done():
        task.cancel()
        state.group_progress.running = False
        state.group_progress.error = "cancelled by facilitator"
        return {"ok": True, "cancelled": True}
    return {"ok": True, "cancelled": False}


@app.post("/api/admin/bracket/build")
async def api_admin_bracket_build(
    body: dict = Body(default={}), x_admin_code: Optional[str] = Header(default=None)
):
    require_admin(x_admin_code)
    seeds = body.get("seeds")
    state.bracket = neg.build_bracket(seeds if seeds else None)
    mark_dirty()
    save_snapshot(force=True)
    payload = neg.bracket_payload()
    bus.publish("bracket", payload)
    return payload


@app.post("/api/admin/match/run")
async def api_admin_match_run(
    body: dict = Body(...), x_admin_code: Optional[str] = Header(default=None)
):
    require_admin(x_admin_code)
    task = _background.get("match")
    if task and not task.done():
        raise HTTPException(status_code=409, detail="A match is already running")
    match_id = str(body.get("match_id", ""))
    match = state.bracket.match(match_id)
    if not match:
        raise HTTPException(status_code=404, detail="Unknown match")
    if not match.team_a or not match.team_b:
        raise HTTPException(status_code=409, detail=f"{match.label} is missing a team")

    async def runner():
        try:
            await neg.run_playoff_match(match_id)
        except Exception as exc:  # noqa: BLE001
            bus.publish("error", {"where": f"match:{match_id}", "message": str(exc)})

    _background["match"] = asyncio.create_task(runner())
    return {"ok": True}


@app.post("/api/admin/llm/pause")
async def api_admin_pause(x_admin_code: Optional[str] = Header(default=None)):
    require_admin(x_admin_code)
    llm.pause()
    bus.publish("llm", llm.snapshot_stats())
    return {"ok": True, "paused": True}


@app.post("/api/admin/llm/resume")
async def api_admin_resume(x_admin_code: Optional[str] = Header(default=None)):
    require_admin(x_admin_code)
    llm.resume()
    bus.publish("llm", llm.snapshot_stats())
    return {"ok": True, "paused": False}


@app.post("/api/admin/snapshot/save")
async def api_admin_snapshot_save(x_admin_code: Optional[str] = Header(default=None)):
    require_admin(x_admin_code)
    save_snapshot(force=True)
    return {"ok": True, "snapshot": snapshot_info()}


@app.post("/api/admin/snapshot/load")
async def api_admin_snapshot_load(x_admin_code: Optional[str] = Header(default=None)):
    require_admin(x_admin_code)
    task = _background.get("group_stage")
    if task and not task.done():
        raise HTTPException(status_code=409, detail="Stop the group stage before restoring")
    if not load_snapshot():
        raise HTTPException(status_code=404, detail="No snapshot on disk")
    bus.publish("phase", {"phase": state.phase.value})
    bus.publish("roster", _roster_payload())
    bus.publish("bracket", neg.bracket_payload())
    bus.publish("gatekeeper_state", gk.gatekeeper_payload())
    publish_leaderboard()
    return {"ok": True, "phase": state.phase.value, "teams": len(state.teams)}


@app.post("/api/admin/team/update")
async def api_admin_team_update(
    body: dict = Body(...), x_admin_code: Optional[str] = Header(default=None)
):
    require_admin(x_admin_code)
    team = state.teams.get(str(body.get("team_id", "")))
    if not team:
        raise HTTPException(status_code=404, detail="Unknown team")
    if "name" in body:
        name = sanitize_name(body["name"])
        if name:
            team.display_name = name
    if "active" in body:
        team.active = bool(body["active"])
    if "prompt" in body:
        version = str(body.get("version", "group"))
        state.set_agent(team.id, str(body["prompt"]), version)
    if body.get("remove"):
        state.teams.pop(team.id, None)
        state.tokens.pop(team.token, None)
        for key in [k for k in state.agents if k[0] == team.id]:
            state.agents.pop(key, None)
        state.standings.pop(team.id, None)
    mark_dirty()
    save_snapshot(force=True)
    bus.publish("roster", _roster_payload())
    publish_leaderboard()
    return {"ok": True}


@app.get("/api/admin/team/prompt")
async def api_admin_team_prompt(
    team_id: str, version: str = "group", x_admin_code: Optional[str] = Header(default=None)
):
    require_admin(x_admin_code)
    agent = state.agents.get((team_id, version))
    return {
        "team_id": team_id,
        "version": version,
        "submitted": agent is not None,
        "prompt": agent.system_prompt if agent else state.get_agent(team_id, version).system_prompt,
    }


@app.get("/api/admin/duels")
async def api_admin_duels(
    stage: str = "group", limit: int = 50, x_admin_code: Optional[str] = Header(default=None)
):
    require_admin(x_admin_code)
    duels = sorted(state.results_for_stage(stage), key=lambda r: r.ended_at, reverse=True)[:limit]
    return {
        "duels": [
            {
                "id": d.id,
                "label": d.label or f"{state.team_name(d.team_a)} vs {state.team_name(d.team_b)}",
                "closed_deal": d.closed_deal,
                "split": d.split,
                "scores": d.scores,
                "errors": d.errors,
            }
            for d in duels
        ]
    }


@app.get("/api/admin/duel")
async def api_admin_duel(duel_id: str, x_admin_code: Optional[str] = Header(default=None)):
    require_admin(x_admin_code)
    result = state.results.get(duel_id)
    if not result:
        raise HTTPException(status_code=404, detail="Unknown duel")
    payload = result.to_dict()
    payload["name_a"] = state.team_name(result.team_a)
    payload["name_b"] = state.team_name(result.team_b)
    return payload


@app.post("/api/admin/window")
async def api_admin_window(
    body: dict = Body(default={}), x_admin_code: Optional[str] = Header(default=None)
):
    """Control the clock on whichever prompt-writing phase is open."""
    require_admin(x_admin_code)
    action = str(body.get("action", "set")).lower()
    if action == "close":
        state.submission_ends_at = time.time()
    elif action == "extend":
        base = max(state.submission_ends_at, time.time()) if state.submission_ends_at else time.time()
        state.submission_ends_at = base + float(body.get("minutes", 1)) * 60
    else:
        minutes = float(body.get("minutes", 15))
        state.submission_ends_at = time.time() + minutes * 60 if minutes else 0.0
    mark_dirty()
    left = state.submission_seconds_left()
    bus.publish("window", {"ends_at": state.submission_ends_at})
    return {"ok": True, "ends_at": state.submission_ends_at,
            "seconds_left": None if left is None else round(left)}


@app.post("/api/admin/reset_event")
async def api_admin_reset(
    body: dict = Body(default={}), x_admin_code: Optional[str] = Header(default=None)
):
    require_admin(x_admin_code)
    if str(body.get("confirm", "")).upper() != "RESET":
        raise HTTPException(status_code=400, detail='Send {"confirm": "RESET"} to wipe the event')
    keep_teams = bool(body.get("keep_teams", False))
    teams = dict(state.teams)
    tokens = dict(state.tokens)
    state.__init__()  # type: ignore[misc]
    if keep_teams:
        state.teams = teams
        state.tokens = tokens
        state._team_counter = len(teams)
    save_snapshot(force=True)
    bus.publish("phase", {"phase": state.phase.value})
    bus.publish("roster", _roster_payload())
    publish_leaderboard()
    return {"ok": True}


# --- error shaping -----------------------------------------------------------

@app.exception_handler(HTTPException)
async def _http_error(_: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
