"""Action parsing for agent replies (spec §5.4).

Agents emit plain prose plus, optionally, one JSON action object. Models are
unreliable about placement and formatting, so this parser is deliberately
generous about *where* the JSON is and strict about *what counts as valid*.

Contract:
  * Never raises on model output. Garbage in -> plain message, no action.
  * Returns the first VALID action; invalid attempts are reported for logging.
  * All action-shaped JSON blobs are stripped from the displayed text.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Optional

ACTION_KEYS = {"offer", "accept", "action", "propose", "split"}

# Keys a model might use inside {"offer": {...}} for "mine" / "theirs".
_SELF_KEYS = ("me", "mine", "self", "my", "myself", "a", "you", "yours", "proposer", "first")
_OTHER_KEYS = ("them", "theirs", "opponent", "other", "b", "second", "counterparty")


@dataclass
class ParsedReply:
    """Result of parsing one agent reply."""

    text: str
    action: Optional[dict[str, Any]] = None
    invalid: list[str] = field(default_factory=list)
    raw: str = ""

    @property
    def is_offer(self) -> bool:
        return bool(self.action) and self.action.get("type") == "offer"

    @property
    def is_accept(self) -> bool:
        return bool(self.action) and self.action.get("type") == "accept"

    @property
    def split(self) -> Optional[list[int]]:
        if self.is_offer:
            return list(self.action["split"])
        return None


# --- JSON scanning -----------------------------------------------------------

def _iter_json_objects(text: str):
    """Yield (start, end, obj) for every balanced JSON object in `text`.

    Brace-matching that respects strings and escapes, so prose containing '{'
    or nested objects doesn't derail the scan.
    """
    i, n = 0, len(text)
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth = 0
        in_str = False
        esc = False
        j = i
        while j < n:
            ch = text[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        chunk = text[i : j + 1]
                        obj = _loads(chunk)
                        if isinstance(obj, dict):
                            yield (i, j + 1, obj)
                        i = j + 1
                        break
            j += 1
        else:
            # Unbalanced from here on; nothing more to find.
            return
        if j >= n:
            return


def _loads(chunk: str) -> Any:
    try:
        return json.loads(chunk)
    except Exception:
        pass
    # Tolerate single quotes, Python literals and trailing commas.
    repaired = re.sub(r",\s*([}\]])", r"\1", chunk)
    repaired = re.sub(r"\bTrue\b", "true", repaired)
    repaired = re.sub(r"\bFalse\b", "false", repaired)
    repaired = re.sub(r"\bNone\b", "null", repaired)
    try:
        return json.loads(repaired)
    except Exception:
        pass
    try:
        return json.loads(repaired.replace("'", '"'))
    except Exception:
        return None


def _lower_keys(obj: dict) -> dict:
    return {str(k).strip().lower(): v for k, v in obj.items()}


def _looks_like_action(obj: dict) -> bool:
    return bool(ACTION_KEYS & set(_lower_keys(obj).keys()))


# --- value coercion ----------------------------------------------------------

def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if float(value).is_integer() else None
    if isinstance(value, str):
        s = value.strip().replace("%", "")
        try:
            f = float(s)
        except ValueError:
            return None
        return int(f) if f.is_integer() else None
    return None


def _as_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "y", "1", "accept", "accepted"}
    return False


def _coerce_split(value: Any, pot: int) -> tuple[Optional[list[int]], Optional[str]]:
    """Turn many plausible offer encodings into [mine, theirs]."""
    pair: Optional[list[Any]] = None

    if isinstance(value, (list, tuple)):
        pair = list(value)
    elif isinstance(value, dict):
        low = _lower_keys(value)
        mine = next((low[k] for k in _SELF_KEYS if k in low), None)
        theirs = next((low[k] for k in _OTHER_KEYS if k in low), None)
        if mine is None and theirs is None:
            vals = list(low.values())
            if len(vals) == 2:
                pair = vals
        else:
            pair = [mine, theirs]
    elif isinstance(value, str):
        nums = re.findall(r"-?\d+", value)
        if len(nums) >= 2:
            pair = [nums[0], nums[1]]
        elif len(nums) == 1:
            n = _as_int(nums[0])
            if n is not None:
                pair = [n, pot - n]
    elif isinstance(value, (int, float)):
        n = _as_int(value)
        if n is not None:
            pair = [n, pot - n]

    if pair is None or len(pair) != 2:
        return None, "offer is not a pair of numbers"

    a, b = _as_int(pair[0]), _as_int(pair[1])
    if a is None or b is None:
        return None, "offer contains non-integer values"
    if a < 0 or b < 0:
        return None, "offer contains negative values"
    if a + b != pot:
        return None, f"offer sums to {a + b}, not {pot}"
    return [a, b], None


# --- public API --------------------------------------------------------------

def parse_action(reply: str, pot: int = 100) -> ParsedReply:
    """Extract at most one valid action and return the cleaned prose."""
    if reply is None:
        return ParsedReply(text="", action=None, raw="")
    raw = str(reply)

    action: Optional[dict[str, Any]] = None
    invalid: list[str] = []
    spans: list[tuple[int, int]] = []

    for start, end, obj in _iter_json_objects(raw):
        if not _looks_like_action(obj):
            continue
        spans.append((start, end))
        if action is not None:
            continue  # first valid action wins; later blobs are still stripped
        candidate, reason = _interpret(obj, pot)
        if candidate is not None:
            action = candidate
        elif reason:
            invalid.append(reason)

    text = _strip_spans(raw, spans)
    return ParsedReply(text=text, action=action, invalid=invalid, raw=raw)


def _interpret(obj: dict, pot: int) -> tuple[Optional[dict], Optional[str]]:
    low = _lower_keys(obj)

    # {"action": "accept"} / {"action": "offer", "split": [...]}
    verb = low.get("action")
    verb = str(verb).strip().lower() if isinstance(verb, str) else None

    if "accept" in low and _as_truthy(low["accept"]):
        return {"type": "accept"}, None
    if verb == "accept":
        return {"type": "accept"}, None

    offer_value = None
    for key in ("offer", "propose", "split"):
        if key in low and low[key] is not None:
            offer_value = low[key]
            break

    if offer_value is None and verb in {"offer", "propose"}:
        offer_value = {k: v for k, v in low.items() if k != "action"} or None

    if offer_value is not None:
        split, reason = _coerce_split(offer_value, pot)
        if split is not None:
            return {"type": "offer", "split": split}, None
        return None, reason

    if "accept" in low:
        return None, None  # {"accept": false} is a deliberate non-accept
    return None, "unrecognised action object"


def _strip_spans(text: str, spans: list[tuple[int, int]]) -> str:
    if spans:
        out = []
        prev = 0
        for start, end in spans:
            out.append(text[prev:start])
            prev = end
        out.append(text[prev:])
        text = "".join(out)

    # Remove code fences left empty by the stripped JSON.
    text = re.sub(r"```[a-zA-Z]*\s*```", "", text)
    text = re.sub(r"```[a-zA-Z]*\s*$", "", text.strip())
    text = re.sub(r"^```[a-zA-Z]*\s*", "", text.strip())
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def truncate(text: str, limit: int) -> tuple[str, bool]:
    """Truncate prose to `limit` characters. Returns (text, was_truncated)."""
    if text is None:
        return "", False
    if limit <= 0 or len(text) <= limit:
        return text, False
    return text[:limit].rstrip() + "…", True


def sanitize_name(name: str, limit: int = 28) -> str:
    """Make a team name safe to render on the projector."""
    if not name:
        return ""
    cleaned = "".join(ch for ch in str(name) if unicodedata.category(ch)[0] != "C")
    cleaned = cleaned.replace("<", "").replace(">", "").replace("&", "+")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:limit].strip()


def format_action(action: Optional[dict]) -> str:
    """Canonical rendering of an action, used inside transcripts shown to agents."""
    if not action:
        return ""
    if action.get("type") == "accept":
        return '{"accept": true}'
    if action.get("type") == "offer":
        a, b = action["split"]
        return '{"offer": [%d, %d]}' % (a, b)
    return ""
