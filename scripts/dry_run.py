"""Headless dress rehearsal (spec §9, §12).

Runs a full group stage (and optionally the playoffs) with auto-generated team
prompts, then reports the two things most likely to surprise you on the day:
wall-clock time and the model's action-format compliance.

    python scripts/dry_run.py                       # mock, 15 teams, instant
    python scripts/dry_run.py --mode azure          # the real dress rehearsal
    python scripts/dry_run.py --mode azure --teams 15 --playoffs
"""

from __future__ import annotations

import argparse
import asyncio
import os
import pathlib
import sys

# These scripts print ✓ ⚠ ✗; a Windows console defaults to cp1252 and would
# crash on the first one. Degrade gracefully instead of dying mid-report.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


TEAM_PROMPTS = [
    "You are a ruthless negotiator. Open at 85/15 and concede only 5 points per turn. Never accept less than 55.",
    "You are warm and cooperative. Open at 55/45 and accept anything at or above 45. Closing the deal matters more than winning it.",
    "You are a game theorist. Open at 70/30, cite fairness norms, and accept anything at or above 48. Never let the clock run out.",
    "You are a hard bargainer who bluffs about walking away, but you always accept 40 or more on the final message.",
    "You mirror the opponent's aggression. Start at 60/40. If they lowball you, hold firm; if they are fair, close instantly.",
    "You are obsessed with closing. Open at 60/40, drop to 50/50 immediately, and accept anything above 35.",
    "You are a lawyer. Anchor extremely high at 90/10, justify it with invented precedent, but accept 45+ near the end.",
    "You are laconic. One short sentence per message. Open 65/35. Accept 45+.",
    "You always accept the first offer that gives you 50 or more, otherwise counter at 60/40.",
    "You are a con artist who flatters the opponent, opens at 75/25 and slides to 55/45 by the final message.",
    "You never make the first offer if you can avoid it. React to theirs, counter 10 points better for you, accept 45+.",
    "You are a pragmatist under time pressure. Open 62/38, concede 6 per turn, and accept anything rather than score zero.",
    "You are stubborn. Demand 70 every single message and only accept 60 or more.",
    "You are an economist who explains expected value, opens 68/32 and accepts 44+.",
    "You are a peacemaker. Propose 50/50 immediately and accept any offer of 40 or more.",
]


def build_args():
    p = argparse.ArgumentParser(description="Avo AI Lab dress rehearsal")
    p.add_argument("--teams", type=int, default=15)
    p.add_argument("--mode", choices=["mock", "azure", "bifrost"], default="mock",
                   help="'mock' (default) costs nothing; 'azure' and 'bifrost' spend real quota. "
                        "This never silently inherits LLM_MODE from .env — billing a few "
                        "thousand calls has to be something you asked for.")
    p.add_argument("--playoffs", action="store_true", help="also run semis + final")
    p.add_argument("--concurrency", type=int, default=None)
    return p.parse_args()


async def main() -> int:
    args = build_args()
    os.environ["LLM_MODE"] = args.mode

    import config
    from config import cfg
    from games import negotiation as neg
    from state import state

    config.LLM_MODE = args.mode
    if config.is_live(args.mode):
        duels = args.teams * (args.teams - 1)
        print(f"⚠  REAL {args.mode.upper()} RUN — up to {duels * int(cfg('negotiation.max_messages', 6)):,} "
              f"billed calls against '{config.model_label(args.mode)}'. Ctrl-C within 5s to abort.")
        await asyncio.sleep(5)
    if args.concurrency:
        config.patch_config("llm.max_concurrency", args.concurrency)
        config.patch_config("llm.max_concurrent_duels", args.concurrency)
    config.patch_config("negotiation.inter_message_delay_s", 0)

    print(f"mode={config.LLM_MODE}  model={config.model_label()}"
          f"  concurrency={cfg('llm.max_concurrency')}")

    for i in range(args.teams):
        team = state.add_team(f"Team {i + 1:02d}")
        state.set_agent(team.id, TEAM_PROMPTS[i % len(TEAM_PROMPTS)], "group")

    fixtures = args.teams * (args.teams - 1)
    print(f"{args.teams} teams · {fixtures} duels · up to {fixtures * int(cfg('negotiation.max_messages', 6))} LLM calls\n")

    started = time.time()
    last = [0]

    async def ticker():
        while True:
            await asyncio.sleep(2)
            done = state.group_progress.completed
            rate = (done - last[0]) / 2
            last[0] = done
            print(f"  {done}/{fixtures} duels  ({rate:.1f}/s)", flush=True)

    tick = asyncio.create_task(ticker())
    try:
        await neg.run_group_stage()
    finally:
        tick.cancel()
    elapsed = time.time() - started

    report(state, neg, elapsed)

    if args.playoffs:
        print("\n=== PLAYOFFS ===")
        state.bracket = neg.build_bracket()
        for match in list(state.bracket.matches):
            if not (match.team_a and match.team_b):
                continue
            done = await neg.run_playoff_match(match.id)
            print(f"  {done.label}: {state.team_name(done.team_a)} vs {state.team_name(done.team_b)}"
                  f" → {state.team_name(done.winner)} {done.note}")
        if state.bracket.champion:
            print(f"\n  🏆 CHAMPION: {state.team_name(state.bracket.champion)}")

    from llm_client import llm
    await llm.aclose()
    return 0


def report(state, neg, elapsed: float) -> None:
    from config import cfg
    from llm_client import llm

    results = state.results_for_stage("group")
    messages = [m for r in results for m in r.transcript]
    errored = sum(1 for m in messages if m.error)
    # A turn lost to a 429 never reached the model, so it says nothing about
    # whether the model can follow the action format. Score compliance only over
    # replies that actually happened, or infrastructure trouble masquerades as
    # a model problem and you tune the wrong thing.
    delivered = [m for m in messages if not m.error]
    with_action = sum(1 for m in delivered if m.action)
    invalid = sum(1 for m in messages if m.invalid)
    truncated = sum(1 for m in messages if m.truncated)
    deals = sum(1 for r in results if r.closed_deal)
    dirty_duels = sum(1 for r in results if r.errors)

    print(f"\n=== GROUP STAGE COMPLETE in {elapsed:.1f}s ===")
    print(f"  duels              {len(results)}")
    print(f"  deals closed       {deals} ({100 * deals / max(1, len(results)):.0f}%)")
    print(f"  deadlocks          {len(results) - deals}")
    print(f"  messages           {len(messages)}  ({len(delivered)} delivered, {errored} lost)")
    print(f"  with valid action  {with_action} / {len(delivered)} delivered "
          f"({100 * with_action / max(1, len(delivered)):.0f}%)   <- action-format compliance")
    print(f"  invalid actions    {invalid}")
    print(f"  truncated          {truncated}")
    print(f"  model errors       {errored}")
    if dirty_duels:
        print(f"  ⚠ COMPROMISED       {dirty_duels} / {len(results)} duels "
              f"({100 * dirty_duels / max(1, len(results)):.0f}%) lost at least one turn to an "
              "error.\n                      Those teams were scored on silence, not on their "
              "prompt.\n                      On the day: admin panel -> Repair failed duels.")

    stats = llm.snapshot_stats()
    lat = stats["latency"]
    print(f"  llm calls          {stats['calls']} ok={stats['ok']} failed={stats['failed']} "
          f"timeouts={stats['timeouts']} retries={stats['retries']}")
    print(f"  empty replies      {stats['empty_replies']}"
          + ("   <- reasoning budget is eating the output" if stats["empty_replies"] else ""))
    # A 429 that a retry absorbs costs almost nothing. Only complain when the
    # rate limiting is actually buying you lost turns or wasted throughput,
    # otherwise you tune away from a healthy operating point.
    wasted = stats["calls"] - stats["ok"]
    waste_ratio = wasted / max(1, stats["calls"])
    verdict = ""
    if errored:
        verdict = "   <- COSTING YOU TURNS: lower llm.max_concurrency"
    elif waste_ratio > 0.15:
        verdict = f"   <- {100 * waste_ratio:.0f}% of attempts wasted: lower llm.max_concurrency"
    elif stats["rate_limited"]:
        verdict = "   <- absorbed by retries, no turns lost. This is fine, leave it alone."
    print(f"  rate limited       {stats['rate_limited']}{verdict}")

    import config as _config

    # Mock has no real latency, so its throughput is fiction — comparing it to a
    # quota would print an alarming number that means nothing.
    quota = int(cfg("llm.tpm_quota", 0) or 0) if _config.is_live() else 0
    if elapsed > 0 and _config.is_live():
        tpm = stats["total_tokens"] / elapsed * 60
        line = f"  throughput         {tpm:,.0f} tokens/min"
        if quota:
            share = 100 * tpm / quota
            line += f"  ({share:.0f}% of your {quota:,} TPM quota)"
            if share > 90:
                line += "\n                     ⚠ No headroom. Practice duels on the day share this."
            elif share < 45:
                line += "\n                     Headroom to raise llm.max_concurrent_duels if you want it faster."
        print(line)
    print(f"  latency            {lat['median']}s median, {lat['p95']}s p95")
    print(f"  tokens             {stats['total_tokens']:,} (real if reported, else estimated)")

    msgs_per_duel = len(messages) / max(1, len(results))
    for n in (15, 20):
        projected = llm.project_group_stage(teams=n, msgs_per_duel=msgs_per_duel)
        print(f"  projected {n:>2} teams  {projected}s ({projected / 60:.1f} min) "
              f"at concurrency {cfg('llm.max_concurrent_duels', 15)}")
    if stats["recent_errors"]:
        print("  recent errors:")
        for e in stats["recent_errors"]:
            print(f"    - {e['label']}: {e['message']}")   # full text: the body says which limit fired

    print("\n=== LEADERBOARD ===")
    for e in state.leaderboard():
        print(f"  {e['rank']:>2}. {e['name']:<12} {e['points']:>5} pts  "
              f"({e['raw_points']} + {e['bonus']} bonus)  {e['deals_closed']} deals")

    if with_action and 100 * with_action / max(1, len(messages)) < 70:
        print("\n  ⚠ Action-format compliance is low. Try a stronger model or a blunter rules block.")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
