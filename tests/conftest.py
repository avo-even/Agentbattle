import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from llm_client import llm  # noqa: E402


@pytest.fixture(autouse=True)
def force_mock_backend(tmp_path, monkeypatch):
    """Tests never touch Azure and never overwrite the real snapshot."""
    monkeypatch.setattr(config, "LLM_MODE", "mock")
    monkeypatch.setattr(config, "SNAPSHOT_PATH", tmp_path / "snapshot.json")
    config.patch_config("llm.mock_latency_s", 0)
    llm.reset_backend()
    llm.resume()
    yield


@pytest.fixture
def fresh_state():
    from state import state

    state.__init__()  # type: ignore[misc]
    return state
