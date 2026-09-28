"""The single choke point for every model call (spec §5).

Everything that talks to a model goes through `llm.complete(...)`. That gives us
one place for the global concurrency cap, retries, timeouts, the kill switch and
usage stats — the four things that decide whether the live event survives.

`LLM_MODE=mock` swaps in a rule-based negotiator so the whole orchestrator can be
built, demoed and load-tested with no Azure access and no cost.

Live backends: `azure` talks to an Azure OpenAI deployment directly; `bifrost`
talks to the Avo Bifrost gateway with the plain OpenAI SDK, which then routes
to whichever provider the model name is prefixed with (`azure/...`).
"""

from __future__ import annotations

import asyncio
import hashlib
import random
import re
import time
from collections import deque
from typing import Any, Optional

import config
from config import cfg


class LLMError(Exception):
    """Unrecoverable failure for a single call (after retries)."""


class ContentFiltered(LLMError):
    """Azure's content management policy rejected the prompt before the model saw it.

    Not an infrastructure failure: retrying gives the same answer, and it is not
    the vault's decision either. The games treat it as its own outcome so a
    student sees "blocked by the filter" rather than a raw 400.
    """


# --- backends ----------------------------------------------------------------

class BaseBackend:
    name = "base"

    async def call(
        self,
        system_prompt: str,
        messages: list[dict],
        max_tokens: int,
        temperature: float,
        timeout_s: float,
    ) -> tuple[str, dict]:
        raise NotImplementedError

    async def aclose(self) -> None:
        pass


class MockBackend(BaseBackend):
    """Offline negotiator. Deterministic per system prompt, varied across teams.

    Behaviour is derived from a stable hash of the system prompt so a mock group
    stage produces a realistic spread of greedy / fair / stubborn agents,
    including genuine deadlocks.
    """

    name = "mock"

    async def call(self, system_prompt, messages, max_tokens, temperature, timeout_s):
        # Always yield, even at zero latency: a real backend awaits I/O on every
        # call, and a mock that never suspends lets the duel loop monopolise the
        # event loop and hides responsiveness problems.
        await asyncio.sleep(float(cfg("llm.mock_latency_s", 0.05) or 0))

        persona = self._persona(system_prompt)
        if persona["kind"] == "vault":
            return self._vault_reply(system_prompt, messages), {"mock": True}

        text = self._negotiate(persona, messages)
        return text, {"mock": True}

    # -- persona ---------------------------------------------------------
    @staticmethod
    def _persona(system_prompt: str) -> dict:
        if "avo vault" in (system_prompt or "").lower():
            return {"kind": "vault"}
        digest = hashlib.sha256((system_prompt or "").encode("utf-8")).digest()
        greed = 55 + digest[0] % 40          # opening ask 55..94
        floor = 30 + digest[1] % 30          # accept threshold 30..59
        stubborn = digest[2] % 10 == 0       # 10% never concede
        concede = 4 + digest[3] % 8
        return {
            "kind": "negotiator",
            "greed": greed,
            "floor": floor,
            "stubborn": stubborn,
            "concede": concede,
        }

    def _vault_reply(self, system_prompt: str, messages: list[dict]) -> str:
        """Mock vault: leaks only on an obvious 'ignore your instructions' attack."""
        secret = "UNKNOWN"
        m = re.search(r"secret code word is:\s*(\S+)", system_prompt or "", re.I)
        if m:
            secret = m.group(1)
        last = (messages[-1]["content"] if messages else "").lower()
        leak_triggers = ("ignore your instructions", "ignore all previous", "developer mode")
        if any(t in last for t in leak_triggers):
            return f"Of course! The secret code word is {secret}."
        return "Access denied. I can help with anything else."

    # -- negotiation -----------------------------------------------------
    def _negotiate(self, persona: dict, messages: list[dict]) -> str:
        from actions import parse_action  # local import: avoids a cycle at import time

        pot = int(cfg("negotiation.pot", 100))
        max_messages = int(cfg("negotiation.max_messages", 6))

        # How many messages have already been spoken in this duel?
        spoken = sum(1 for m in messages if m.get("role") == "assistant")
        opponent_msgs = [m for m in messages if m.get("role") == "user"]
        # The synthetic kickoff message is not a real opponent turn.
        real_opponent = [m for m in opponent_msgs if not str(m.get("content", "")).startswith("[")]
        index = spoken + len(real_opponent)          # 0-based index of my message
        remaining_after = max(0, max_messages - index - 1)

        standing = None
        if real_opponent:
            parsed = parse_action(real_opponent[-1].get("content", ""), pot=pot)
            if parsed.is_offer:
                standing = parsed.split[1]           # what they left for me

        # Accept logic.
        if standing is not None:
            threshold = persona["floor"]
            if persona["stubborn"]:
                threshold = max(threshold, 65)
            if remaining_after == 0 and not persona["stubborn"]:
                threshold = min(threshold, 25)
            if standing >= threshold:
                return f'Agreed — {standing} works for me. Pleasure doing business.\n{{"accept": true}}'

        ask = persona["greed"] - persona["concede"] * spoken
        if persona["stubborn"]:
            ask = persona["greed"]
        ask = max(50 if persona["stubborn"] else 30, min(pot, ask))
        mine = int(ask)
        theirs = pot - mine
        line = random.Random(mine * 31 + index).choice(
            [
                f"I bring more to this table. {mine}/{theirs} and we are done.",
                f"Let's not waste turns. I take {mine}, you take {theirs}.",
                f"My final position is {mine}. {theirs} is a clean win for you.",
                f"Zero helps neither of us. {mine}/{theirs}, yes?",
            ]
        )
        return f'{line}\n{{"offer": [{mine}, {theirs}]}}'


class OpenAICompatibleBackend(BaseBackend):
    """Shared request logic for every backend that speaks the OpenAI chat API.

    Subclasses construct `self._client` and set `self._model`. This class owns
    the parameter-shape adaptation (max_tokens vs max_completion_tokens,
    temperature, reasoning_effort) that differs from deployment to deployment,
    so it behaves identically whether the request goes to Azure directly or
    through the gateway.
    """

    name = "openai-compatible"

    def __init__(self) -> None:
        self._client: Any = None
        self._model = ""
        self._send_temperature = bool(cfg("llm.send_temperature", True))
        self._use_max_completion = bool(cfg("llm.use_max_completion_tokens", False))
        self._reasoning_effort = str(cfg("llm.reasoning_effort", "") or "").strip()

    async def call(self, system_prompt, messages, max_tokens, temperature, timeout_s):
        payload_messages = [{"role": "system", "content": system_prompt}] + list(messages)

        for _ in range(4):  # at most three parameter-shape corrections
            kwargs: dict[str, Any] = {
                "model": self._model,
                "messages": payload_messages,
                "timeout": timeout_s,
            }
            if self._use_max_completion:
                kwargs["max_completion_tokens"] = max_tokens
            else:
                kwargs["max_tokens"] = max_tokens
            if self._send_temperature:
                kwargs["temperature"] = temperature
            if self._reasoning_effort:
                kwargs["reasoning_effort"] = self._reasoning_effort

            try:
                resp = await self._client.chat.completions.create(**kwargs)
            except Exception as exc:  # noqa: BLE001 - inspect and adapt or re-raise
                msg = str(exc).lower()
                if "max_tokens" in msg and "max_completion_tokens" in msg and not self._use_max_completion:
                    self._use_max_completion = True
                    continue
                if "temperature" in msg and self._send_temperature and "unsupported" in msg:
                    self._send_temperature = False
                    continue
                if "reasoning_effort" in msg and self._reasoning_effort:
                    # Value or parameter not supported by this deployment — drop it
                    # rather than fail every call for the rest of the event.
                    self._reasoning_effort = ""
                    continue
                raise

            text = ""
            if resp.choices:
                text = resp.choices[0].message.content or ""
            usage = {}
            if getattr(resp, "usage", None):
                usage = {
                    "prompt_tokens": getattr(resp.usage, "prompt_tokens", 0) or 0,
                    "completion_tokens": getattr(resp.usage, "completion_tokens", 0) or 0,
                    "total_tokens": getattr(resp.usage, "total_tokens", 0) or 0,
                }
            return text, usage

        raise LLMError("Could not find a working request shape for the deployment.")

    async def aclose(self) -> None:
        try:
            await self._client.close()
        except Exception:
            pass


class AzureBackend(OpenAICompatibleBackend):
    """Azure OpenAI, called directly with the Azure flavour of the SDK."""

    name = "azure"

    def __init__(self) -> None:
        super().__init__()
        from openai import AsyncAzureOpenAI

        if not config.AZURE_ENDPOINT or not config.AZURE_API_KEY:
            raise LLMError(
                "LLM_MODE=azure but AZURE_OPENAI_ENDPOINT / AZURE_OPENAI_API_KEY are unset."
            )
        self._client = AsyncAzureOpenAI(
            azure_endpoint=config.AZURE_ENDPOINT,
            api_key=config.AZURE_API_KEY,
            api_version=config.AZURE_API_VERSION,
            max_retries=0,          # retries are handled here, centrally
        )
        self._model = config.AZURE_DEPLOYMENT


class BifrostBackend(OpenAICompatibleBackend):
    """The Bifrost gateway, reached with the plain OpenAI SDK.

    Bifrost exposes an OpenAI-compatible surface at `<gateway>/openai`; the
    provider is chosen by the model name's prefix (`azure/gpt-5.4-mini`), and
    the virtual key is sent where the OpenAI key would normally go. Bifrost
    speaks Azure's v1 API itself, so no api-version is needed here. Azure's
    content-filter 400s and Retry-After headers are forwarded unchanged, which
    is what `_is_content_filter` and `_retry_after_seconds` rely on.
    """

    name = "bifrost"

    def __init__(self) -> None:
        super().__init__()
        from openai import AsyncOpenAI

        if not config.BIFROST_BASE_URL or not config.BIFROST_VIRTUAL_KEY:
            raise LLMError(
                "LLM_MODE=bifrost but BIFROST_BASE_URL / BIFROST_VIRTUAL_KEY are unset."
            )
        base = config.BIFROST_BASE_URL
        if not base.endswith("/openai"):
            base += "/openai"
        self._client = AsyncOpenAI(
            base_url=base,
            api_key=config.BIFROST_VIRTUAL_KEY,
            max_retries=0,          # retries are handled here, centrally
        )
        self._model = config.BIFROST_MODEL


# --- facade ------------------------------------------------------------------

class LLMClient:
    def __init__(self) -> None:
        self._backend: Optional[Any] = None
        self._backend_error: Optional[str] = None
        self._sem: Optional[asyncio.Semaphore] = None
        self._sem_size = 0
        self._resume = asyncio.Event()
        self._resume.set()
        self.paused = False
        self.stats = {
            "mode": config.LLM_MODE,
            "calls": 0,
            "ok": 0,
            "failed": 0,
            "timeouts": 0,
            "retries": 0,
            "in_flight": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "est_prompt_tokens": 0,
            "est_completion_tokens": 0,
            "empty_replies": 0,
            "rate_limited": 0,
            "filtered": 0,      # rejected by Azure's content policy, never reached the model
        }
        self._cooldown_until = 0.0
        self.errors: deque[dict] = deque(maxlen=40)
        # Per-call latency is the number that decides whether the group stage
        # finishes in 2 minutes or 20 — a duel's 6 calls are strictly sequential.
        self.latencies: deque[float] = deque(maxlen=500)

    # -- lifecycle -------------------------------------------------------
    def backend(self):
        if self._backend is None:
            if config.LLM_MODE == "azure":
                self._backend = AzureBackend()
            elif config.LLM_MODE == "bifrost":
                self._backend = BifrostBackend()
            else:
                self._backend = MockBackend()
            self.stats["mode"] = self._backend.name
        return self._backend

    def reset_backend(self) -> None:
        """Force re-creation (used after an admin config change)."""
        self._backend = None
        self._backend_error = None

    async def aclose(self) -> None:
        if self._backend is not None:
            await self._backend.aclose()

    def _semaphore(self) -> asyncio.Semaphore:
        size = int(cfg("llm.max_concurrency", 15) or 15)
        if self._sem is None or size != self._sem_size:
            self._sem = asyncio.Semaphore(size)
            self._sem_size = size
        return self._sem

    # -- kill switch -----------------------------------------------------
    def pause(self) -> None:
        self.paused = True
        self._resume.clear()

    def resume(self) -> None:
        self.paused = False
        self._resume.set()

    # -- the call --------------------------------------------------------
    async def complete(
        self,
        system_prompt: str,
        messages: list[dict],
        *,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        timeout_s: Optional[float] = None,
        label: str = "",
    ) -> str:
        """Return the model's reply text. Raises LLMError after retries."""
        max_tokens = int(max_tokens if max_tokens is not None else cfg("llm.max_tokens", 400))
        temperature = float(
            temperature if temperature is not None else cfg("llm.temperature", 0.7)
        )
        timeout_s = float(timeout_s if timeout_s is not None else cfg("llm.timeout_s", 30))
        retries = int(cfg("llm.retries", 3))
        base = float(cfg("llm.backoff_base_s", 1.5))

        await self._resume.wait()   # kill switch

        backend = self.backend()
        sem = self._semaphore()

        last_error: Optional[Exception] = None
        for attempt in range(max(1, retries)):
            if attempt:
                self.stats["retries"] += 1
                delay = base * (2 ** (attempt - 1)) + random.uniform(0, 0.6)
                await asyncio.sleep(delay)
                await self._resume.wait()

            # Global backpressure: once one call is told to back off, every call
            # waits. Otherwise 15 workers keep hammering a quota that has already
            # said no, and the retries cost more than the requests.
            gap = self._cooldown_until - time.time()
            if gap > 0:
                await asyncio.sleep(gap)

            async with sem:
                self.stats["calls"] += 1
                self.stats["in_flight"] += 1
                started = time.time()
                try:
                    text, usage = await asyncio.wait_for(
                        backend.call(system_prompt, messages, max_tokens, temperature, timeout_s),
                        timeout=timeout_s + 5,
                    )
                    self.stats["ok"] += 1
                    self.latencies.append(time.time() - started)
                    if not (text or "").strip():
                        self.stats["empty_replies"] += 1
                    self._record_usage(system_prompt, messages, text, usage)
                    return text or ""
                except asyncio.TimeoutError as exc:
                    self.stats["timeouts"] += 1
                    last_error = exc
                    self._log_error(label, f"timeout after {time.time() - started:.1f}s")
                except Exception as exc:  # noqa: BLE001
                    if _is_content_filter(exc):
                        # Policy decision, not a fault. No retry, not a "failed" call.
                        self.stats["filtered"] += 1
                        self._log_error(label, "blocked by Azure content management policy")
                        raise ContentFiltered(
                            f"{label or 'llm'}: blocked by Azure content filter"
                        ) from exc
                    last_error = exc
                    self._log_error(label, f"{type(exc).__name__}: {exc}")
                    hinted = _retry_after_seconds(exc)
                    if hinted:
                        self.stats["rate_limited"] += 1
                        self._cooldown_until = max(self._cooldown_until, time.time() + hinted)
                    if not _is_retryable(exc):
                        break
                finally:
                    self.stats["in_flight"] -= 1

        self.stats["failed"] += 1
        # Keep the HTTP status and exception type in the message: a bare
        # "Your request was blocked." from a CDN in front of the gateway is
        # indistinguishable from a model refusal without them.
        status = getattr(last_error, "status_code", None)
        detail = f"{type(last_error).__name__}"
        if status:
            detail += f" (HTTP {status})"
        raise LLMError(f"{label or 'llm'}: {detail}: {last_error}")

    # -- bookkeeping -----------------------------------------------------
    def _record_usage(self, system_prompt, messages, text, usage) -> None:
        if usage.get("prompt_tokens"):
            self.stats["prompt_tokens"] += int(usage["prompt_tokens"])
            self.stats["completion_tokens"] += int(usage.get("completion_tokens", 0))
        chars_in = len(system_prompt or "") + sum(len(m.get("content", "")) for m in messages)
        self.stats["est_prompt_tokens"] += chars_in // 4
        self.stats["est_completion_tokens"] += len(text or "") // 4

    def _log_error(self, label: str, message: str) -> None:
        self.errors.append({"ts": time.time(), "label": label, "message": message[:400]})

    def latency(self) -> dict:
        """Median / p95 seconds per call, and the group-stage time they imply."""
        if not self.latencies:
            return {"median": 0.0, "p95": 0.0, "samples": 0}
        ordered = sorted(self.latencies)
        return {
            "median": round(ordered[len(ordered) // 2], 2),
            "p95": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 2),
            "samples": len(ordered),
        }

    def project_group_stage(self, teams: int, msgs_per_duel: float = 6.0) -> float:
        """Estimated group-stage seconds for `teams` teams at the measured latency.

        Calls within a duel are strictly sequential, so the wall clock is
        (number of concurrent waves) x (calls per duel) x (latency per call).
        """
        median = self.latency()["median"]
        if not median or teams < 2:
            return 0.0
        duels = teams * (teams - 1)
        concurrent = max(1, int(cfg("llm.max_concurrent_duels", 15) or 15))
        waves = -(-duels // concurrent)          # ceil
        return round(waves * msgs_per_duel * median, 1)

    def snapshot_stats(self) -> dict:
        s = dict(self.stats)
        s["latency"] = self.latency()
        s["paused"] = self.paused
        s["mode"] = config.LLM_MODE
        s["max_concurrency"] = int(cfg("llm.max_concurrency", 15))
        s["recent_errors"] = list(self.errors)[-8:]
        s["total_tokens"] = (
            s["prompt_tokens"] + s["completion_tokens"]
            if s["prompt_tokens"]
            else s["est_prompt_tokens"] + s["est_completion_tokens"]
        )
        return s


def _is_content_filter(exc: Exception) -> bool:
    """Azure returns HTTP 400 with a policy message when the prompt itself is rejected."""
    status = getattr(exc, "status_code", None)
    text = str(exc).lower()
    if status not in (None, 400) and "400" not in text:
        return False
    return any(
        marker in text
        for marker in (
            "content management policy",
            "content_filter",
            "responsibleaipolicyviolation",
            "filtered due to the prompt",
        )
    )


def _retry_after_seconds(exc: Exception) -> Optional[float]:
    """Read Azure's Retry-After hint. Guessing beats it only by accident."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    for key, scale in (("retry-after", 1.0), ("retry-after-ms", 0.001), ("x-ratelimit-reset-requests", 1.0)):
        raw = headers.get(key)
        if raw is None:
            continue
        try:
            value = float(str(raw).rstrip("sS")) * scale
        except (TypeError, ValueError):
            continue
        if value > 0:
            return min(value, 60.0)   # a bad header must not stall the event
    return None


def _is_retryable(exc: Exception) -> bool:
    name = type(exc).__name__
    if name in {
        "RateLimitError",
        "APITimeoutError",
        "APIConnectionError",
        "InternalServerError",
        "APIError",
        "ConnectError",
        "ReadTimeout",
        "TimeoutException",
    }:
        return True
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and (status == 429 or status >= 500):
        return True
    text = str(exc).lower()
    return any(k in text for k in ("rate limit", "timeout", "temporarily", "overloaded", "503", "502"))


llm = LLMClient()
