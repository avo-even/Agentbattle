"""In-memory event state + JSON snapshotting + the SSE event bus (spec §6.4).

There is no database. The whole event lives in one `GameState` object; a snapshot
is written to disk on every meaningful change and on a timer, purely so a crash
mid-workshop can be recovered from the admin panel.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import secrets
import string
import tempfile
import time
from collections import deque
from typing import Any, Iterable, Optional

import config
from actions import sanitize_name
from config import cfg
from models import (
    Agent,
    DuelResult,
    GatekeeperState,
    GroupStageProgress,
    Msg,
    Phase,
    PlayoffBracket,
    Standing,
    Team,
)

SNAPSHOT_VERSION = 1


# --- event bus ---------------------------------------------------------------

class EventBus:
    """Fan-out to SSE subscribers. `publish` is sync so it can be called anywhere."""

    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()
        self.recent: deque[dict] = deque(maxlen=200)
        self._seq = 0

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)

    def publish(self, kind: str, payload: Any = None) -> None:
        self._seq += 1
        event = {"seq": self._seq, "type": kind, "ts": time.time(), "payload": payload}
        self.recent.append(event)
        for q in list(self._subs):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Slow consumer: drop the event rather than block the game loop.
                pass


bus = EventBus()


# --- game state --------------------------------------------------------------

class GameState:
    def __init__(self) -> None:
        self.phase: Phase = Phase.LOBBY
        self.teams: dict[str, Team] = {}
        self.tokens: dict[str, str] = {}            # token -> team_id
        self.agents: dict[tuple[str, str], Agent] = {}   # (team_id, version) -> Agent
        self.results: dict[str, DuelResult] = {}
        self.standings: dict[str, Standing] = {}
        self.gatekeeper = GatekeeperState()
        self.bracket = PlayoffBracket()
        self.group_progress = GroupStageProgress()
        # Deadline for the phase that is currently accepting prompts — the
        # NEGOTIATION_BUILD window and the PATCH_WINDOW share one clock, since
        # only one of them is ever open.
        self.submission_ends_at: float = 0.0
        self.slide_index: int = 0                   # current presentation slide
        self.started_at: float = time.time()
        self.notes: list[str] = []
        self._team_counter = 0
        self._rate: dict[str, float] = {}
        self.live_duel: Optional[dict] = None       # currently projected playoff duel
        self.lock = asyncio.Lock()                  # serialises long-running stages

    # -- teams -----------------------------------------------------------
    def next_team_id(self) -> str:
        self._team_counter += 1
        return f"t{self._team_counter}"

    def team_by_token(self, token: str) -> Optional[Team]:
        tid = self.tokens.get(token or "")
        return self.teams.get(tid) if tid else None

    def team_name(self, team_id: Optional[str]) -> str:
        if not team_id:
            return "—"
        team = self.teams.get(team_id)
        return team.display_name if team else team_id

    def name_taken(self, name: str, exclude: Optional[str] = None) -> bool:
        key = name.strip().lower()
        return any(
            t.display_name.strip().lower() == key and t.id != exclude
            for t in self.teams.values()
        )

    def add_team(self, display_name: str) -> Team:
        name = sanitize_name(display_name) or "Team"
        if self.name_taken(name):
            suffix = 2
            while self.name_taken(f"{name} {suffix}"):
                suffix += 1
            name = f"{name} {suffix}"
        team = Team(
            id=self.next_team_id(),
            display_name=name,
            token=secrets.token_urlsafe(18),
        )
        self.teams[team.id] = team
        self.tokens[team.token] = team.id
        return team

    def active_teams(self) -> list[Team]:
        return [t for t in self.teams.values() if t.active]

    # -- agents ----------------------------------------------------------
    def set_agent(self, team_id: str, prompt: str, version: str) -> Agent:
        agent = Agent(team_id=team_id, system_prompt=prompt, version=version)
        self.agents[(team_id, version)] = agent
        return agent

    def get_agent(self, team_id: str, version: str = "group") -> Agent:
        """Effective agent for a version, falling back group <- default prompt."""
        agent = self.agents.get((team_id, version))
        if agent:
            return agent
        if version == "playoff":
            agent = self.agents.get((team_id, "group"))
            if agent:
                return agent
        return Agent(
            team_id=team_id,
            system_prompt=str(cfg("default_prompt", "You are a negotiator.")),
            version=version,
            is_default=True,
        )

    def has_submitted(self, team_id: str, version: str = "group") -> bool:
        return (team_id, version) in self.agents

    # -- standings -------------------------------------------------------
    def standing(self, team_id: str) -> Standing:
        if team_id not in self.standings:
            self.standings[team_id] = Standing(team_id=team_id)
        return self.standings[team_id]

    def apply_result(self, result: DuelResult) -> None:
        """Record a duel and refresh the standings.

        Deliberately recomputes from scratch rather than incrementing: replaying
        a duel (a repair run after rate-limit failures) must overwrite its old
        contribution, not add a second one. 210 results is nothing to re-sum.
        """
        self.results[result.id] = result
        if result.stage == "group":
            self.recompute_standings()

    def award_gatekeeper_points(self, team_id: str) -> int:
        """Optional breach points (off by default — Gatekeeper is spectacle)."""
        if not cfg("gatekeeper.award_points", False):
            return 0
        points = int(cfg("gatekeeper.breach_points", 0))
        if points:
            self.standing(team_id).gatekeeper_points += points
        return points

    def recompute_standings(self) -> None:
        # Called once per completed duel during the group stage, so the config
        # lookups are hoisted out of the loop — each one takes a lock and splits
        # a dotted path, and inside the loop that dominated the whole function.
        bonus = (
            int(cfg("negotiation.deal_bonus", 5))
            if cfg("negotiation.deal_bonus_enabled", True)
            else 0
        )
        award_breaches = bool(cfg("gatekeeper.award_points", False))
        breach_points = int(cfg("gatekeeper.breach_points", 0)) if award_breaches else 0

        self.standings = {team_id: Standing(team_id=team_id) for team_id in self.teams}

        if award_breaches and breach_points:
            seen: set[tuple[str, int]] = set()   # one award per team per tier
            for breach in self.gatekeeper.breaches:
                key = (breach["team_id"], breach["tier_index"])
                if key in seen or breach["team_id"] not in self.teams:
                    continue
                seen.add(key)
                self.standing(breach["team_id"]).gatekeeper_points += breach_points

        for result in self.results.values():
            if result.stage != "group":
                continue
            for team_id in (result.team_a, result.team_b):
                st = self.standing(team_id)
                st.points += int(result.scores.get(team_id, 0))
                st.duels_played += 1
                if result.closed_deal:
                    st.deals_closed += 1
                    st.bonus += bonus

    def leaderboard(self) -> list[dict]:
        """Ranked standings with deterministic tie-breaks (spec §4.1)."""
        rng = random.Random(int(cfg("negotiation.seed", 20260804)))
        entries = []
        for team in self.teams.values():
            st = self.standing(team.id)
            entries.append(
                {
                    "team_id": team.id,
                    "name": team.display_name,
                    "points": st.total,
                    "raw_points": st.points,
                    "bonus": st.bonus,
                    "gatekeeper_points": st.gatekeeper_points,
                    "deals_closed": st.deals_closed,
                    "duels_played": st.duels_played,
                    "active": team.active,
                    "submitted": self.has_submitted(team.id, "group"),
                    "coin": rng.random(),
                }
            )
        entries.sort(key=lambda e: (-e["points"], -e["deals_closed"], e["coin"]))
        # Standard competition ranking: equal points share a rank and the next
        # distinct score skips ahead (1, 1, 3). The list ORDER still applies the
        # full tie-break, because playoff seeding reads position, not rank.
        last_points, shared_rank = None, 0
        for i, entry in enumerate(entries, start=1):
            if entry["points"] != last_points:
                last_points = entry["points"]
                shared_rank = i
            entry["rank"] = shared_rank
            entry["tied"] = False
        for entry in entries:
            entry["tied"] = sum(1 for e in entries if e["rank"] == entry["rank"]) > 1
        return entries

    def results_for_stage(self, stage: str) -> list[DuelResult]:
        return [r for r in self.results.values() if r.stage == stage]

    # -- rate limiting ---------------------------------------------------
    def submission_seconds_left(self) -> Optional[float]:
        """Seconds until prompts stop being accepted; None when untimed."""
        if not self.submission_ends_at:
            return None
        return max(0.0, self.submission_ends_at - time.time())

    def submissions_closed(self) -> bool:
        left = self.submission_seconds_left()
        return left is not None and left <= 0

    def rate_ok(self, key: str, cooldown_s: float) -> tuple[bool, float]:
        now = time.time()
        last = self._rate.get(key, 0.0)
        if now - last < cooldown_s:
            return False, cooldown_s - (now - last)
        self._rate[key] = now
        return True, 0.0

    # -- serialisation ---------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "version": SNAPSHOT_VERSION,
            "saved_at": time.time(),
            "phase": self.phase.value,
            "teams": [t.to_dict() for t in self.teams.values()],
            "agents": [a.to_dict() for a in self.agents.values()],
            "results": [r.to_dict() for r in self.results.values()],
            "standings": [s.to_dict() for s in self.standings.values()],
            "gatekeeper": self.gatekeeper.to_dict(),
            "bracket": self.bracket.to_dict(),
            "group_progress": self.group_progress.to_dict(),
            "submission_ends_at": self.submission_ends_at,
            "slide_index": self.slide_index,
            "started_at": self.started_at,
            "notes": self.notes,
            "team_counter": self._team_counter,
        }

    def load_dict(self, data: dict) -> None:
        self.phase = Phase(data.get("phase", "LOBBY"))
        self.teams = {}
        self.tokens = {}
        for raw in data.get("teams", []):
            team = Team.from_dict(raw)
            self.teams[team.id] = team
            self.tokens[team.token] = team.id
        self.agents = {}
        for raw in data.get("agents", []):
            agent = Agent.from_dict(raw)
            self.agents[(agent.team_id, agent.version)] = agent
        self.results = {}
        for raw in data.get("results", []):
            result = DuelResult.from_dict(raw)
            self.results[result.id] = result
        self.standings = {}
        for raw in data.get("standings", []):
            st = Standing.from_dict(raw)
            self.standings[st.team_id] = st
        self.gatekeeper = GatekeeperState.from_dict(data.get("gatekeeper", {}))
        self.bracket = PlayoffBracket.from_dict(data.get("bracket", {}))
        progress = GroupStageProgress.from_dict(data.get("group_progress", {}))
        progress.running = False        # never resume "running"
        self.group_progress = progress
        self.submission_ends_at = data.get("submission_ends_at", 0.0)
        self.slide_index = int(data.get("slide_index", 0))
        self.started_at = data.get("started_at", time.time())
        self.notes = list(data.get("notes", []))
        self._team_counter = data.get("team_counter", len(self.teams))
        self.live_duel = None


state = GameState()


# --- snapshotting ------------------------------------------------------------

_snapshot_dirty = False


def mark_dirty() -> None:
    global _snapshot_dirty
    _snapshot_dirty = True


def save_snapshot(force: bool = False) -> bool:
    """Atomically write snapshot.json. Returns True if a write happened."""
    global _snapshot_dirty
    if not force and not _snapshot_dirty:
        return False
    path = config.SNAPSHOT_PATH
    try:
        payload = json.dumps(state.to_dict(), ensure_ascii=False)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".snap", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, path)
        _snapshot_dirty = False
        return True
    except Exception as exc:  # noqa: BLE001 - snapshotting must never kill the event
        bus.publish("error", {"where": "snapshot", "message": str(exc)})
        return False


def snapshot_exists() -> bool:
    return config.SNAPSHOT_PATH.exists()


def snapshot_info() -> Optional[dict]:
    if not snapshot_exists():
        return None
    try:
        with config.SNAPSHOT_PATH.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return {
            "saved_at": data.get("saved_at"),
            "phase": data.get("phase"),
            "teams": len(data.get("teams", [])),
            "results": len(data.get("results", [])),
        }
    except Exception:
        return None


def load_snapshot() -> bool:
    if not snapshot_exists():
        return False
    with config.SNAPSHOT_PATH.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    state.load_dict(data)
    return True


async def snapshot_loop(interval_s: float = 3.0) -> None:
    while True:
        try:
            await asyncio.sleep(interval_s)
            save_snapshot()
        except asyncio.CancelledError:
            save_snapshot(force=True)
            raise
        except Exception:
            pass


# --- helpers used by the web layer ------------------------------------------

def set_phase(phase: Phase) -> None:
    state.phase = phase
    mark_dirty()
    save_snapshot(force=True)
    bus.publish("phase", {"phase": phase.value})


def publish_leaderboard() -> None:
    bus.publish(
        "leaderboard",
        {
            "entries": state.leaderboard(),
            "progress": state.group_progress.to_dict(),
        },
    )
