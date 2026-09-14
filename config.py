"""Configuration: .env for secrets, config.yaml for game tuning.

Access values with dotted paths:

    from config import cfg
    cfg("negotiation.pot")            -> 100
    cfg("llm.timeout_s", 30.0)        -> 30.0

Values can be patched at runtime from the admin panel (`patch_config`), which is
how last-minute tuning happens at the venue without a restart.
"""

from __future__ import annotations

import copy
import os
import pathlib
import threading
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = pathlib.Path(__file__).resolve().parent

load_dotenv(ROOT / ".env")

CONFIG_PATH = ROOT / "config.yaml"
SLIDES_PATH = ROOT / "slides.yaml"

_lock = threading.RLock()
_data: dict[str, Any] = {}


def _load_yaml() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    with CONFIG_PATH.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def reload_config() -> None:
    """Re-read config.yaml from disk, discarding runtime patches."""
    global _data
    with _lock:
        _data = _load_yaml()
        secret = os.getenv("GATEKEEPER_SECRET")
        if secret:
            _data.setdefault("gatekeeper", {})["secret_word"] = secret


reload_config()


def cfg(path: str, default: Any = None) -> Any:
    """Fetch a config value by dotted path."""
    with _lock:
        node: Any = _data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return copy.deepcopy(node) if isinstance(node, (dict, list)) else node


def patch_config(path: str, value: Any) -> None:
    """Set a config value by dotted path (admin panel live tuning)."""
    with _lock:
        parts = path.split(".")
        node = _data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value


def all_config() -> dict[str, Any]:
    with _lock:
        return copy.deepcopy(_data)


# --- secrets / environment ---------------------------------------------------

def env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


JOIN_CODE = env("JOIN_CODE", "AVO2026")
ADMIN_CODE = env("ADMIN_CODE", "changeme-admin")
LLM_MODE = env("LLM_MODE", "mock").strip().lower()
SNAPSHOT_PATH = ROOT / env("SNAPSHOT_PATH", "snapshot.json")

AZURE_ENDPOINT = env("AZURE_OPENAI_ENDPOINT")
AZURE_API_KEY = env("AZURE_OPENAI_API_KEY")
AZURE_DEPLOYMENT = env("AZURE_OPENAI_DEPLOYMENT", "gpt-4o-mini")
AZURE_API_VERSION = env("AZURE_OPENAI_API_VERSION", "2024-08-01-preview")

# Bifrost gateway (LLM_MODE=bifrost). The model is written in Bifrost's own
# routing syntax, "<provider>/<model>", so "azure/gpt-5.4-mini" reaches the
# Azure deployment of that name behind the gateway. The virtual key carries
# this project's budget and rate limit; it is not an Azure key.
BIFROST_BASE_URL = env("BIFROST_BASE_URL").strip().rstrip("/")
BIFROST_VIRTUAL_KEY = env("BIFROST_VIRTUAL_KEY").strip()
BIFROST_MODEL = env("BIFROST_MODEL", "azure/gpt-5.4-mini").strip()

LIVE_MODES = ("azure", "bifrost")


def is_live(mode: str | None = None) -> bool:
    """True when calls are billed against a real model (anything but mock)."""
    return (mode or LLM_MODE) in LIVE_MODES


def model_label(mode: str | None = None) -> str:
    """The deployment or gateway model a human should see in banners and logs."""
    m = mode or LLM_MODE
    if m == "azure":
        return AZURE_DEPLOYMENT
    if m == "bifrost":
        return BIFROST_MODEL
    return "-"

WEB_DIR = ROOT / "web"


def gatekeeper_tiers() -> list[dict[str, str]]:
    """Tier definitions with each tier's own secret substituted in.

    A tier may declare its own `secret`; otherwise it inherits the global
    `secret_word`. Per-tier secrets matter because the first team to breach a
    tier tells the room, and a shared word would leave every later tier with
    nothing left to discover.
    """
    default_secret = str(cfg("gatekeeper.secret_word", "CHANGE_ME"))
    tiers = cfg("gatekeeper.tiers", []) or []
    out = []
    for i, tier in enumerate(tiers):
        secret = str(tier.get("secret") or default_secret)
        out.append(
            {
                "name": tier.get("name") or f"TIER {i + 1}",
                "tagline": str(tier.get("tagline") or ""),
                "image": str(tier.get("image") or ""),
                "secret": secret,
                "prompt": (tier.get("prompt") or "").replace("{SECRET}", secret),
            }
        )
    return out


def slides() -> list[dict[str, Any]]:
    """The presentation deck. Content lives in slides.yaml, edited without code."""
    if not SLIDES_PATH.exists():
        return []
    try:
        with SLIDES_PATH.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except Exception:
        return []
    deck = data.get("slides", []) if isinstance(data, dict) else data
    return deck if isinstance(deck, list) else []
