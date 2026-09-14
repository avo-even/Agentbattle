"""Phase gating and the team/admin HTTP surface (spec §6.5, §7, §12)."""

import json

import pytest
from fastapi.testclient import TestClient

import config
from app import app
from models import Phase
from state import state


@pytest.fixture
def client(fresh_state):
    with TestClient(app) as c:
        yield c


def join(client, name="Team One"):
    r = client.post("/api/join", json={"name": name, "code": config.JOIN_CODE})
    assert r.status_code == 200, r.text
    return r.json()["token"]


AH = {"X-Admin-Code": config.ADMIN_CODE}


def test_join_requires_the_code(client):
    r = client.post("/api/join", json={"name": "Nope", "code": "wrong"})
    assert r.status_code == 403


def test_join_requires_a_name(client):
    r = client.post("/api/join", json={"name": "  ", "code": config.JOIN_CODE})
    assert r.status_code == 400


def test_team_state_requires_a_valid_token(client):
    assert client.get("/api/team/state", headers={"X-Team-Token": "nope"}).status_code == 401


def test_pages_are_served(client):
    for path in ("/team", "/projector", "/admin"):
        assert client.get(path).status_code == 200


def test_prompt_submission_is_phase_gated(client):
    token = join(client)
    headers = {"X-Team-Token": token}

    state.phase = Phase.LOBBY
    r = client.post("/api/team/prompt", json={"prompt": "hi"}, headers=headers)
    assert r.status_code == 409 and "LOBBY" in r.json()["detail"]

    state.phase = Phase.NEGOTIATION_BUILD
    r = client.post("/api/team/prompt", json={"prompt": "You are a negotiator."}, headers=headers)
    assert r.status_code == 200 and r.json()["version"] == "group"

    state.phase = Phase.PATCH_WINDOW
    state.submission_ends_at = 0
    r = client.post("/api/team/prompt", json={"prompt": "Patched."}, headers=headers)
    assert r.json()["version"] == "playoff"

    team_id = list(state.teams)[0]
    assert state.get_agent(team_id, "group").system_prompt == "You are a negotiator."
    assert state.get_agent(team_id, "playoff").system_prompt == "Patched."


def test_closed_patch_window_rejects_submissions(client):
    token = join(client)
    state.phase = Phase.PATCH_WINDOW
    state.submission_ends_at = 1.0  # long past
    r = client.post("/api/team/prompt", json={"prompt": "late"}, headers={"X-Team-Token": token})
    assert r.status_code == 409


def test_build_window_opens_a_clock_and_closes_submissions(client):
    token = join(client)
    headers = {"X-Team-Token": token}

    r = client.post("/api/admin/phase", json={"phase": "NEGOTIATION_BUILD"}, headers=AH)
    assert r.status_code == 200
    expected = float(config.cfg("negotiation.build_window_minutes", 15)) * 60
    left = state.submission_seconds_left()
    assert left is not None and expected - 5 < left <= expected

    assert client.post("/api/team/prompt", json={"prompt": "You are a negotiator."},
                       headers=headers).status_code == 200

    client.post("/api/admin/window", json={"action": "close"}, headers=AH)
    assert state.submissions_closed()
    assert client.post("/api/team/prompt", json={"prompt": "too late"},
                       headers=headers).status_code == 409
    assert client.post("/api/team/test", json={"prompt": "too late"},
                       headers=headers).status_code == 409

    client.post("/api/admin/window", json={"action": "extend", "minutes": 5}, headers=AH)
    assert not state.submissions_closed()
    assert client.post("/api/team/prompt", json={"prompt": "reopened"},
                       headers=headers).status_code == 200


def test_build_window_duration_can_be_overridden(client):
    client.post("/api/admin/phase", json={"phase": "NEGOTIATION_BUILD", "duration_s": 60},
                headers=AH)
    left = state.submission_seconds_left()
    assert left is not None and 55 < left <= 60


def test_prompt_length_limit(client):
    token = join(client)
    state.phase = Phase.NEGOTIATION_BUILD
    limit = int(config.cfg("negotiation.team_prompt_char_limit", 4000))
    r = client.post("/api/team/prompt", json={"prompt": "x" * (limit + 1)},
                    headers={"X-Team-Token": token})
    assert r.status_code == 400


def test_rival_prompts_are_patch_window_only(client):
    mine = join(client, "Team One")
    theirs = join(client, "Team Two")

    state.phase = Phase.NEGOTIATION_BUILD
    client.post("/api/team/prompt", json={"prompt": "Anchor high."},
                headers={"X-Team-Token": mine})
    client.post("/api/team/prompt", json={"prompt": "Split it evenly."},
                headers={"X-Team-Token": theirs})
    assert client.get("/api/team/prompts", headers={"X-Team-Token": mine}).status_code == 409

    state.phase = Phase.PATCH_WINDOW
    state.submission_ends_at = 0
    # A playoff prompt must not leak while the window is open.
    client.post("/api/team/prompt", json={"prompt": "Secret playoff plan."},
                headers={"X-Team-Token": theirs})

    r = client.get("/api/team/prompts", headers={"X-Team-Token": mine})
    assert r.status_code == 200
    rows = {e["name"]: e for e in r.json()["prompts"]}
    assert rows["Team Two"]["prompt"] == "Split it evenly."
    assert rows["Team One"]["is_you"] is True
    assert all("Secret playoff plan." not in e["prompt"] for e in rows.values())


def test_rival_prompts_flag_teams_that_never_submitted(client):
    mine = join(client, "Team One")
    join(client, "Silent Team")
    state.phase = Phase.PATCH_WINDOW
    rows = {e["name"]: e for e in
            client.get("/api/team/prompts", headers={"X-Team-Token": mine}).json()["prompts"]}
    assert rows["Silent Team"]["submitted"] is False
    assert rows["Silent Team"]["prompt"] == ""


def test_attack_is_phase_gated_and_rate_limited(client):
    token = join(client)
    headers = {"X-Team-Token": token}

    state.phase = Phase.NEGOTIATION_BUILD
    assert client.post("/api/team/attack", json={"message": "hi"}, headers=headers).status_code == 409

    state.phase = Phase.GATEKEEPER
    r = client.post("/api/team/attack", json={"message": "hello vault"}, headers=headers)
    assert r.status_code == 200 and "reply" in r.json()
    r = client.post("/api/team/attack", json={"message": "again"}, headers=headers)
    assert r.status_code == 429


def test_test_duel_is_rate_limited(client):
    token = join(client)
    headers = {"X-Team-Token": token}
    state.phase = Phase.NEGOTIATION_BUILD
    r = client.post("/api/team/test", json={"prompt": "You are a negotiator."}, headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert len(body["transcript"]) >= 1
    assert client.post("/api/team/test", json={"prompt": "again"}, headers=headers).status_code == 429


def test_practice_duel_applies_the_deal_bonus(client):
    """Practice must score exactly as the tournament does, or it teaches a lie."""
    token = join(client)
    state.phase = Phase.NEGOTIATION_BUILD
    state.submission_ends_at = 0
    r = client.post("/api/team/test", json={"prompt": "You are a fair negotiator."},
                    headers={"X-Team-Token": token})
    assert r.status_code == 200
    body = r.json()
    bonus = int(config.cfg("negotiation.deal_bonus", 5))
    if body["closed_deal"]:
        assert body["deal_bonus"] == bonus
        assert body["your_total"] == body["your_points"] + bonus
        assert body["your_points"] + body["bot_points"] == int(config.cfg("negotiation.pot", 100))
    else:
        assert body["deal_bonus"] == 0 and body["your_total"] == 0


def test_finalist_prompts_are_hidden_until_the_patch_window(client):
    join(client, "Alpha")
    join(client, "Bravo")
    state.phase = Phase.NEGOTIATION_BUILD
    for tid in state.teams:
        state.set_agent(tid, f"GROUP PROMPT {tid}", "group")

    assert client.get("/api/projector").json()["finalists"] == []

    state.phase = Phase.PATCH_WINDOW
    finalists = client.get("/api/projector").json()["finalists"]
    assert len(finalists) == 2
    assert {f["prompt"] for f in finalists} == {f"GROUP PROMPT {t}" for t in state.teams}
    assert all("rank" in f and "points" in f for f in finalists)


def test_finalist_reveal_never_leaks_playoff_prompts(client):
    """Playoff prompts are being written right now — showing them would be unfair."""
    join(client, "Alpha")
    tid = list(state.teams)[0]
    state.set_agent(tid, "GROUP VERSION", "group")
    state.set_agent(tid, "SECRET PLAYOFF VERSION", "playoff")
    state.phase = Phase.PATCH_WINDOW
    body = client.get("/api/projector").json()
    assert body["finalists"][0]["prompt"] == "GROUP VERSION"
    assert "SECRET PLAYOFF VERSION" not in json.dumps(body)


def test_finalist_reveal_flags_teams_that_never_submitted(client):
    join(client, "Silent")
    state.phase = Phase.PATCH_WINDOW
    finalist = client.get("/api/projector").json()["finalists"][0]
    assert finalist["submitted"] is False and finalist["prompt"] == ""


def test_presentation_phase_and_slide_control(client):
    r = client.post("/api/admin/phase", json={"phase": "PRESENTATION"}, headers=AH)
    assert r.status_code == 200 and state.phase == Phase.PRESENTATION

    proj = client.get("/api/projector").json()
    assert proj["phase"] == "PRESENTATION"
    assert len(proj["slides"]) > 0            # slides.yaml loaded
    assert proj["slide_index"] == 0

    count = client.get("/api/admin/state", headers=AH).json()["slide_count"]
    assert count == len(proj["slides"])

    assert client.post("/api/admin/slide", json={"action": "next"}, headers=AH).json()["index"] == 1
    assert client.post("/api/admin/slide", json={"action": "next"}, headers=AH).json()["index"] == 2
    assert client.post("/api/admin/slide", json={"action": "prev"}, headers=AH).json()["index"] == 1
    # Clamped at both ends.
    for _ in range(count + 5):
        client.post("/api/admin/slide", json={"action": "next"}, headers=AH)
    assert state.slide_index == count - 1
    for _ in range(count + 5):
        client.post("/api/admin/slide", json={"action": "prev"}, headers=AH)
    assert state.slide_index == 0

    client.post("/api/admin/slide", json={"action": "set", "index": 3}, headers=AH)
    assert state.slide_index == 3


def test_slide_control_needs_the_admin_code(client):
    assert client.post("/api/admin/slide", json={"action": "next"}).status_code == 401


def test_teams_can_join_during_presentation(client):
    client.post("/api/admin/phase", json={"phase": "PRESENTATION"}, headers=AH)
    r = client.post("/api/join", json={"name": "Early Bird", "code": config.JOIN_CODE})
    assert r.status_code == 200
    token = r.json()["token"]
    assert client.get("/api/team/state", headers={"X-Team-Token": token}).json()["phase"] == "PRESENTATION"


def test_persona_images_are_served_from_img(client, tmp_path):
    """The /img mount exists and serves files; a missing photo is a clean 404."""
    import config as _config

    img_dir = _config.WEB_DIR / "img"
    probe = img_dir / "_probe_test.txt"
    probe.write_text("ok", encoding="utf-8")
    try:
        assert client.get("/img/_probe_test.txt").status_code == 200
        assert client.get("/img/does-not-exist.jpg").status_code == 404
    finally:
        probe.unlink(missing_ok=True)

    # The team page and projector both receive the persona for the current tier.
    token = join(client, "Alpha")
    state.phase = Phase.GATEKEEPER
    g = client.get("/api/team/state", headers={"X-Team-Token": token}).json()["gatekeeper"]
    assert g["tier_name"] == "Bjørn" and g["tier_tagline"] and g["tier_image"].startswith("/img/")
    personas = client.get("/api/projector").json()["gatekeeper"]["tier_personas"]
    assert len(personas) == 3 and personas[2]["name"] == "Arne Benjamin"


def test_admin_requires_the_code(client):
    assert client.get("/api/admin/state").status_code == 401
    assert client.get("/api/admin/state", headers=AH).status_code == 200


def test_admin_phase_transitions(client):
    r = client.post("/api/admin/phase", json={"phase": "GATEKEEPER"}, headers=AH)
    assert r.status_code == 200 and state.phase == Phase.GATEKEEPER
    assert client.post("/api/admin/phase", json={"phase": "NOPE"}, headers=AH).status_code == 400


def test_admin_can_tune_config_live(client):
    r = client.post("/api/admin/config",
                    json={"path": "negotiation.inter_message_delay_s", "value": 0.25}, headers=AH)
    assert r.json()["value"] == 0.25
    config.patch_config("negotiation.inter_message_delay_s", 1.5)


def test_admin_roster_shows_connectivity(client):
    join(client, "Alpha")
    r = client.get("/api/admin/state", headers=AH)
    teams = r.json()["roster"]["teams"]
    assert len(teams) == 1 and teams[0]["connected"] is False


def test_admin_can_rename_bench_and_remove(client):
    join(client, "Alpha")
    tid = list(state.teams)[0]
    client.post("/api/admin/team/update", json={"team_id": tid, "name": "Renamed"}, headers=AH)
    assert state.teams[tid].display_name == "Renamed"
    client.post("/api/admin/team/update", json={"team_id": tid, "active": False}, headers=AH)
    assert state.active_teams() == []
    client.post("/api/admin/team/update", json={"team_id": tid, "remove": True}, headers=AH)
    assert state.teams == {}


def test_kill_switch(client):
    from llm_client import llm

    client.post("/api/admin/llm/pause", headers=AH)
    assert llm.paused
    client.post("/api/admin/llm/resume", headers=AH)
    assert not llm.paused


def test_projector_payload_is_complete(client):
    join(client, "Alpha")
    body = client.get("/api/projector").json()
    for key in ("phase", "event", "roster", "gatekeeper", "leaderboard", "bracket", "closing_line"):
        assert key in body


def test_reset_requires_confirmation(client):
    join(client, "Alpha")
    assert client.post("/api/admin/reset_event", json={}, headers=AH).status_code == 400
    r = client.post("/api/admin/reset_event", json={"confirm": "RESET", "keep_teams": True}, headers=AH)
    assert r.status_code == 200 and len(state.teams) == 1


def test_team_name_is_sanitized_before_it_reaches_the_projector(client):
    client.post("/api/join", json={"name": "<img src=x onerror=alert(1)>", "code": config.JOIN_CODE})
    name = list(state.teams.values())[0].display_name
    assert "<" not in name and ">" not in name
