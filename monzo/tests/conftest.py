import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).parent))

from monzo_mcp import clock  # noqa: E402
from monzo_mcp.client import MonzoClient, MonzoConfig  # noqa: E402
from monzo_mcp.tokens import MemoryTokenStore, TokenSet  # noqa: E402

API = "https://api.monzo.com"
# Every in-process test runs at this instant unless it moves the clock itself.
FROZEN_NOW = datetime(2026, 10, 11, 12, 0, 0, tzinfo=timezone.utc)


def pytest_configure(config):
    # Keep pytest's temporary files inside the project (git-ignored), wherever pytest is started from.
    if not config.option.basetemp:
        config.option.basetemp = str(ROOT / ".pytest_tmp")


class Clock:
    def __init__(self, at: datetime):
        self.at = at.timestamp()

    def __call__(self) -> float:
        return self.at

    def set(self, at: datetime) -> None:
        self.at = at.timestamp()

    def advance(self, seconds: float) -> None:
        self.at += seconds


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    """No test may pick up the developer's own Monzo settings."""
    for name in list(os.environ):
        if name.startswith("MONZO_"):
            monkeypatch.delenv(name)


@pytest.fixture(autouse=True)
def frozen_clock():
    frozen = Clock(FROZEN_NOW)
    clock.set_clock(frozen)
    yield frozen
    clock.set_clock(None)


@pytest.fixture
def config():
    return MonzoConfig(client_id="oauth2client_test", client_secret="test-secret")


@pytest.fixture
def valid_tokens():
    return TokenSet(
        access_token="access-old",
        refresh_token="refresh-old",
        # far enough ahead that tests which move the clock forward do not trigger a refresh
        expires_at=FROZEN_NOW.timestamp() + 3650 * 86400,
        authorized_at=FROZEN_NOW.timestamp() - 86400,
    )


@pytest.fixture
def store(valid_tokens):
    return MemoryTokenStore(valid_tokens)


@pytest.fixture
async def client(config, store):
    async with httpx.AsyncClient() as http:
        yield MonzoClient(config, store, http=http)
