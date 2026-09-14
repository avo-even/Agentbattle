"""Preflight: prove the model config works before you trust it with a workshop.

    python scripts/check_llm.py

Makes two calls — a trivial one to prove connectivity/auth, then one real
negotiation turn to prove the deployment can follow the action format. Failures
are translated into the thing you actually need to fix.
"""

from __future__ import annotations

import asyncio
import pathlib
import sys

# These scripts print ✓ ⚠ ✗; a Windows console defaults to cp1252 and would
# crash on the first one. Degrade gracefully instead of dying mid-report.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from actions import parse_action  # noqa: E402
from config import cfg  # noqa: E402
from llm_client import LLMError, llm  # noqa: E402


def mask(secret: str) -> str:
    if not secret:
        return "(EMPTY)"
    return f"{secret[:4]}…{secret[-4:]} ({len(secret)} chars)"


def diagnose(exc: Exception) -> str:
    """Map the failure onto the setting that is actually wrong."""
    text = str(exc).lower()
    status = getattr(exc, "status_code", None)
    via_gateway = config.LLM_MODE == "bifrost"
    endpoint_var = "BIFROST_BASE_URL" if via_gateway else "AZURE_OPENAI_ENDPOINT"

    if "getaddrinfo" in text or "name or service not known" in text or "nodename" in text:
        return f"{endpoint_var} hostname does not resolve — check for a typo."
    if "apiconnectionerror" in text or "connection error" in text or "connecterror" in text:
        return (f"Could not open a connection to {endpoint_var}. Usually a typo in the "
                "hostname; otherwise a VPN, corporate proxy or firewall is blocking it.")
    if "connect" in text and "timeout" in text:
        return "Cannot reach the endpoint — check the URL, a VPN, or a corporate proxy."
    if status == 401 or "401" in text or "invalid subscription key" in text or "access denied" in text:
        if via_gateway:
            return ("BIFROST_VIRTUAL_KEY was rejected — wrong key, or the virtual key is "
                    "disabled in Bifrost → Virtual Keys.")
        return ("AZURE_OPENAI_API_KEY is wrong, or the key belongs to a different resource "
                "than AZURE_OPENAI_ENDPOINT. Portal → your resource → Keys and Endpoint.")
    if status == 403 or "403" in text:
        if via_gateway:
            return ("The virtual key is not allowed to use this provider/model. In Bifrost, "
                    "check the key's provider and model allow-lists.")
        return ("Key rejected by policy — local/key auth may be disabled on the resource, "
                "or a network rule is blocking you.")
    if status == 404 or "404" in text or "deploymentnotfound" in text or "resource not found" in text:
        if via_gateway:
            if "cognitiveservices.azure.com" in config.BIFROST_BASE_URL or "openai.azure.com" in config.BIFROST_BASE_URL:
                return ("BIFROST_BASE_URL points at an Azure resource, not at the gateway. That "
                        "endpoint belongs in Bifrost's Azure provider config; here you want the "
                        "origin of the Bifrost server itself (the host of its web UI).")
            return ("A 404 with the gateway URL set usually means BIFROST_MODEL is not routable: "
                    "it must be '<provider>/<model>' (e.g. azure/gpt-5.4-mini), the provider "
                    "must be configured in Bifrost, and for Azure the part after the slash must "
                    "be the deployment name (or an alias on the Azure key). If the 404 body looks "
                    "like Azure's own ('Resource not found'), the base URL is a provider "
                    "endpoint rather than the gateway.")
        return ("AZURE_OPENAI_DEPLOYMENT does not exist on this resource, OR the endpoint "
                "includes a path (it must be just https://<resource>.openai.azure.com), OR "
                "AZURE_OPENAI_API_VERSION is not supported. Check Foundry → Deployments for "
                "the exact deployment name.")
    if "budget" in text or "402" in text:
        return ("The virtual key's budget in Bifrost is exhausted or misconfigured. "
                "Bifrost → Virtual Keys → this key → budget.")
    if status == 429 or "429" in text or "rate limit" in text:
        if via_gateway:
            return ("Rate limited on the very first call — either the virtual key's rate limit "
                    "in Bifrost or the Azure deployment quota behind it. Check both, or lower "
                    "llm.max_concurrency in config.yaml.")
        return ("Rate limited on the very first call — the deployment's quota is tiny. "
                "Raise TPM in Foundry, or lower llm.max_concurrency in config.yaml.")
    if "max_tokens" in text or "temperature" in text:
        return ("The deployment rejects a standard parameter. Set llm.use_max_completion_tokens: "
                "true and/or llm.send_temperature: false in config.yaml.")
    if status and int(status) >= 500:
        return "Azure-side error. Retry; if it persists the deployment may be unhealthy."
    return "Unrecognised failure — the raw error above is your best clue."


async def main() -> int:
    print("=== configuration ===")
    print(f"  LLM_MODE                  {config.LLM_MODE}")
    if not config.is_live():
        print("\n  Running in mock mode — nothing to check against a real model.")
        print("  Set LLM_MODE=bifrost (or azure) in .env to test the real backend.")
        return 0

    if config.LLM_MODE == "bifrost":
        print(f"  BIFROST_BASE_URL          {config.BIFROST_BASE_URL or '(EMPTY)'}")
        print(f"  BIFROST_MODEL             {config.BIFROST_MODEL or '(EMPTY)'}")
        print(f"  BIFROST_VIRTUAL_KEY       {mask(config.BIFROST_VIRTUAL_KEY)}")
        required = (
            ("BIFROST_BASE_URL", config.BIFROST_BASE_URL),
            ("BIFROST_VIRTUAL_KEY", config.BIFROST_VIRTUAL_KEY),
            ("BIFROST_MODEL", config.BIFROST_MODEL),
        )
    else:
        print(f"  AZURE_OPENAI_ENDPOINT     {config.AZURE_ENDPOINT or '(EMPTY)'}")
        print(f"  AZURE_OPENAI_DEPLOYMENT   {config.AZURE_DEPLOYMENT or '(EMPTY)'}")
        print(f"  AZURE_OPENAI_API_VERSION  {config.AZURE_API_VERSION or '(EMPTY)'}")
        print(f"  AZURE_OPENAI_API_KEY      {mask(config.AZURE_API_KEY)}")
        required = (
            ("AZURE_OPENAI_ENDPOINT", config.AZURE_ENDPOINT),
            ("AZURE_OPENAI_API_KEY", config.AZURE_API_KEY),
            ("AZURE_OPENAI_DEPLOYMENT", config.AZURE_DEPLOYMENT),
            ("AZURE_OPENAI_API_VERSION", config.AZURE_API_VERSION),
        )

    missing = [name for name, value in required if not value]
    if missing:
        print(f"\n  ✗ Missing from .env: {', '.join(missing)}")
        return 1
    if config.LLM_MODE == "bifrost":
        if "/" not in config.BIFROST_MODEL:
            print("\n  ⚠ BIFROST_MODEL has no provider prefix. Bifrost will guess the provider "
                  "from its catalog; write it as azure/<deployment> to make routing explicit.")
        if "/v1" in config.BIFROST_BASE_URL:
            print("\n  ⚠ BIFROST_BASE_URL should be the gateway origin only (the client appends "
                  "/openai itself).")
    elif "/openai/" in config.AZURE_ENDPOINT:
        print("\n  ⚠ Your endpoint contains a path. It should be just "
              "https://<resource>.openai.azure.com")

    # --- 1. connectivity + auth ---------------------------------------
    print("\n=== 1/2 connectivity and auth ===")
    try:
        reply = await llm.complete(
            "You are a test harness. Reply with exactly: OK",
            [{"role": "user", "content": "Say OK."}],
            max_tokens=10,
            label="preflight",
        )
    except LLMError as exc:
        cause = llm.errors[-1]["message"] if llm.errors else str(exc)
        print(f"  ✗ FAILED\n    {cause}\n\n  → {diagnose(exc)}")
        return 1

    if not reply.strip():
        used = llm.stats.get("completion_tokens", 0)
        print(f"  ⚠ The call SUCCEEDED but returned empty text (completion_tokens={used}).")
        print("    On reasoning models the token budget is spent on hidden reasoning before any")
        print("    visible text is produced. Raise llm.max_tokens in config.yaml (try 2000) and")
        print("    re-run. Left unfixed, every agent 'says nothing' and every duel deadlocks.")
    else:
        print(f"  ✓ reachable — model replied {reply.strip()[:60]!r}")

    # --- 2. action-format compliance ----------------------------------
    print("\n=== 2/2 action format ===")
    from games.negotiation import KICKOFF, DuelAgent, build_system_prompt

    agent = DuelAgent("preflight", "Preflight", "You are a firm negotiator. Open around 60/40.")
    try:
        reply = await llm.complete(
            build_system_prompt(agent, 0, int(cfg("negotiation.max_messages", 6))),
            [{"role": "user", "content": KICKOFF}],
            label="preflight-action",
        )
    except LLMError as exc:
        print(f"  ✗ FAILED — {diagnose(exc)}")
        return 1

    parsed = parse_action(reply, pot=int(cfg("negotiation.pot", 100)))
    print(f"  raw reply: {reply.strip()[:200]!r}")
    backend = llm.backend()
    # Only worth saying if the client had to discover this itself; if config.yaml
    # already pins the same values there was no wasted call to save.
    adapted = (
        getattr(backend, "_use_max_completion", False) != bool(cfg("llm.use_max_completion_tokens", False))
        or getattr(backend, "_send_temperature", True) != bool(cfg("llm.send_temperature", True))
    )
    if adapted:
        print("  ℹ This deployment rejected standard parameters and the client adapted. Pin it in")
        print("    config.yaml to save two wasted calls per restart:")
        print(f"      llm.use_max_completion_tokens: {getattr(backend, '_use_max_completion', False)}")
        print(f"      llm.send_temperature: {getattr(backend, '_send_temperature', True)}")
    if parsed.action:
        print(f"  ✓ emitted a valid action: {parsed.action}")
        print(f"    displayed text: {parsed.text[:120]!r}")
    else:
        print("  ✗ no valid action in the reply.")
        if parsed.invalid:
            print(f"    rejected because: {'; '.join(parsed.invalid)}")
        print("    One miss is not fatal — models are stochastic. Run "
              f"scripts/dry_run.py --mode {config.LLM_MODE} --teams 4 to measure the real rate.")

    stats = llm.snapshot_stats()
    print(f"\n  calls={stats['calls']} ok={stats['ok']} failed={stats['failed']} "
          f"retries={stats['retries']} tokens={stats['total_tokens']:,}")

    # --- speed: the constraint that decides whether the format works live ---
    lat = stats["latency"]
    projected = llm.project_group_stage(teams=15)
    print("\n=== speed ===")
    print(f"  median latency     {lat['median']}s per call  ({lat['samples']} samples)")
    print(f"  15-team group stage ≈ {projected}s ({projected / 60:.1f} min) at "
          f"concurrency {cfg('llm.max_concurrent_duels', 15)}")
    if projected > 420:
        print("  ✗ Too slow. A duel's 6 calls are sequential, so latency multiplies out.")
        print("    Fix by, in order of preference: a faster (non-reasoning) deployment;")
        print("    reasoning effort set to none (not minimal: Bifrost rewrites that to low);")
        print("    or a higher llm.max_concurrent_duels")
        print("    if your TPM quota can take it.")
    elif projected > 240:
        print("  ⚠ Workable but slow enough to be noticed. Consider raising concurrency.")
    else:
        print("  ✓ Comfortable — this finishes while you are still talking.")

    print(f"\n  Next: python scripts/dry_run.py --mode {config.LLM_MODE} --teams 15 --playoffs")
    await llm.aclose()
    return 0 if parsed.action else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
