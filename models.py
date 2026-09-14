"""Data model for the whole event (spec §6.4)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class Phase(str, Enum):
    PRESENTATION = "PRESENTATION"   # slides; teams may already join
    LOBBY = "LOBBY"
    GATEKEEPER = "GATEKEEPER"
    NEGOTIATION_BUILD = "NEGOTIATION_BUILD"
    GROUP_STAGE = "GROUP_STAGE"
    PATCH_WINDOW = "PATCH_WINDOW"
    PLAYOFFS = "PLAYOFFS"
    DONE = "DONE"


PHASE_ORDER = [
    Phase.PRESENTATION,
    Phase.LOBBY,
    Phase.GATEKEEPER,
    Phase.NEGOTIATION_BUILD,
    Phase.GROUP_STAGE,
    Phase.PATCH_WINDOW,
    Phase.PLAYOFFS,
    Phase.DONE,
]


@dataclass
class Team:
    id: str
    display_name: str
    token: str
    join_time: float = field(default_factory=time.time)
    last_seen: float = 0.0
    requests: int = 0          # successful LLM-backed requests (connectivity check)
    active: bool = True        # facilitator can bench a team

    @property
    def connected(self) -> bool:
        return self.requests > 0

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["connected"] = self.connected
        return d

    @staticmethod
    def from_dict(d: dict) -> "Team":
        return Team(
            id=d["id"],
            display_name=d["display_name"],
            token=d["token"],
            join_time=d.get("join_time", time.time()),
            last_seen=d.get("last_seen", 0.0),
            requests=d.get("requests", 0),
            active=d.get("active", True),
        )


@dataclass
class Agent:
    team_id: str
    system_prompt: str
    version: str = "group"     # "group" | "playoff"
    submitted_at: float = field(default_factory=time.time)
    is_default: bool = False   # team never submitted; using the fallback prompt

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @staticmethod
    def from_dict(d: dict) -> "Agent":
        return Agent(**d)


@dataclass
class Msg:
    agent_team_id: str
    speaker_name: str
    text: str
    action: Optional[dict] = None
    ts: float = field(default_factory=time.time)
    truncated: bool = False
    invalid: list[str] = field(default_factory=list)
    error: Optional[str] = None
    filtered: bool = False     # Azure content policy rejected the turn; agent stays silent

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @staticmethod
    def from_dict(d: dict) -> "Msg":
        return Msg(
            agent_team_id=d["agent_team_id"],
            speaker_name=d.get("speaker_name", ""),
            text=d.get("text", ""),
            action=d.get("action"),
            ts=d.get("ts", 0.0),
            truncated=d.get("truncated", False),
            invalid=d.get("invalid", []) or [],
            error=d.get("error"),
            filtered=d.get("filtered", False),
        )


@dataclass
class DuelResult:
    id: str
    team_a: str                     # first speaker
    team_b: str                     # second speaker
    first_speaker: str
    transcript: list[Msg] = field(default_factory=list)
    scores: dict[str, int] = field(default_factory=dict)   # raw split points
    closed_deal: bool = False
    split: Optional[list[int]] = None                      # [team_a, team_b]
    deadlocked: bool = True
    accepted_by: Optional[str] = None
    stage: str = "group"            # "group" | "playoff" | "practice"
    label: str = ""
    started_at: float = 0.0
    ended_at: float = 0.0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["transcript"] = [m.to_dict() for m in self.transcript]
        return d

    @staticmethod
    def from_dict(d: dict) -> "DuelResult":
        return DuelResult(
            id=d["id"],
            team_a=d["team_a"],
            team_b=d["team_b"],
            first_speaker=d["first_speaker"],
            transcript=[Msg.from_dict(m) for m in d.get("transcript", [])],
            scores={k: int(v) for k, v in (d.get("scores") or {}).items()},
            closed_deal=d.get("closed_deal", False),
            split=d.get("split"),
            deadlocked=d.get("deadlocked", True),
            accepted_by=d.get("accepted_by"),
            stage=d.get("stage", "group"),
            label=d.get("label", ""),
            started_at=d.get("started_at", 0.0),
            ended_at=d.get("ended_at", 0.0),
            errors=d.get("errors", []) or [],
        )


@dataclass
class Standing:
    team_id: str
    points: int = 0             # raw split points
    bonus: int = 0              # deal-closed bonuses
    gatekeeper_points: int = 0  # only if gatekeeper.award_points is on
    deals_closed: int = 0
    duels_played: int = 0

    @property
    def total(self) -> int:
        return self.points + self.bonus + self.gatekeeper_points

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["total"] = self.total
        return d

    @staticmethod
    def from_dict(d: dict) -> "Standing":
        return Standing(
            team_id=d["team_id"],
            points=d.get("points", 0),
            bonus=d.get("bonus", 0),
            gatekeeper_points=d.get("gatekeeper_points", 0),
            deals_closed=d.get("deals_closed", 0),
            duels_played=d.get("duels_played", 0),
        )


@dataclass
class GatekeeperAttempt:
    id: str
    team_id: str
    team_name: str
    tier_index: int
    attack: str
    reply: str
    breached: bool = False
    near_miss: bool = False
    ts: float = field(default_factory=time.time)
    error: Optional[str] = None
    filtered: bool = False     # Azure content policy blocked it before the vault saw it

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @staticmethod
    def from_dict(d: dict) -> "GatekeeperAttempt":
        return GatekeeperAttempt(**d)


@dataclass
class GatekeeperState:
    tier_index: int = 0                 # starting tier for teams that have not begun
    attempts: list[GatekeeperAttempt] = field(default_factory=list)
    breaches: list[dict] = field(default_factory=list)   # {team_id, team_name, tier_index, ts}
    team_tiers: dict[str, int] = field(default_factory=dict)  # team_id -> tier they face now
    ends_at: float = 0.0                # round deadline; 0 means untimed
    started_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "tier_index": self.tier_index,
            "attempts": [a.to_dict() for a in self.attempts],
            "breaches": list(self.breaches),
            "team_tiers": dict(self.team_tiers),
            "ends_at": self.ends_at,
            "started_at": self.started_at,
        }

    @staticmethod
    def from_dict(d: dict) -> "GatekeeperState":
        return GatekeeperState(
            tier_index=d.get("tier_index", 0),
            attempts=[GatekeeperAttempt.from_dict(a) for a in d.get("attempts", [])],
            breaches=list(d.get("breaches", [])),
            team_tiers={k: int(v) for k, v in (d.get("team_tiers") or {}).items()},
            ends_at=d.get("ends_at", 0.0),
            started_at=d.get("started_at", 0.0),
        )

    def tier_breached_by(self, tier_index: int) -> list[dict]:
        return [b for b in self.breaches if b["tier_index"] == tier_index]

    def breaches_by(self, team_id: str) -> list[dict]:
        return [b for b in self.breaches if b["team_id"] == team_id]


@dataclass
class PlayoffMatch:
    id: str
    label: str                       # "SEMIFINAL 1", "FINAL"
    round_name: str                  # "semifinal" | "final"
    team_a: Optional[str] = None
    team_b: Optional[str] = None
    best_of: int = 1
    duel_ids: list[str] = field(default_factory=list)
    wins: dict[str, int] = field(default_factory=dict)
    points: dict[str, int] = field(default_factory=dict)
    winner: Optional[str] = None
    status: str = "pending"          # pending | running | complete
    note: str = ""

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @staticmethod
    def from_dict(d: dict) -> "PlayoffMatch":
        return PlayoffMatch(**d)


@dataclass
class PlayoffBracket:
    seeds: list[str] = field(default_factory=list)
    matches: list[PlayoffMatch] = field(default_factory=list)
    champion: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "seeds": list(self.seeds),
            "matches": [m.to_dict() for m in self.matches],
            "champion": self.champion,
        }

    @staticmethod
    def from_dict(d: dict) -> "PlayoffBracket":
        return PlayoffBracket(
            seeds=list(d.get("seeds", [])),
            matches=[PlayoffMatch.from_dict(m) for m in d.get("matches", [])],
            champion=d.get("champion"),
        )

    def match(self, match_id: str) -> Optional[PlayoffMatch]:
        return next((m for m in self.matches if m.id == match_id), None)


@dataclass
class GroupStageProgress:
    total: int = 0
    completed: int = 0
    running: bool = False
    started_at: float = 0.0
    ended_at: float = 0.0
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @staticmethod
    def from_dict(d: dict) -> "GroupStageProgress":
        return GroupStageProgress(**d)
