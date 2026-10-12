"""End-to-end: launch the real server process on each transport (`python -m monzo_mcp`), talk
to it with a real MCP client, with MONZO_API_BASE pointed at a local fake Monzo.

The token file starts with an EXPIRED access token, so every run also proves that
the server refreshes and writes the rotated refresh token back to disk.

The server process runs on the real clock, so nothing here depends on the time of day:
the fake's transactions sit at London noon on fixed days relative to one captured date, the
window is passed explicitly, and every expected value is derived from that same date.
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest
from mcp import Client, StdioServerParameters

from fake_monzo import ACCOUNT_ID, LONDON, PER_DAY, FakeMonzo, free_port, make_transactions
from monzo_mcp import __version__

MCP_TOKEN = "test-only-mcp-bearer-token"
N = 150
DAYS = N // PER_DAY


@pytest.fixture
def first_day():
    return datetime.now(LONDON).date() - timedelta(days=20)


@pytest.fixture
def fake(first_day):
    f = FakeMonzo(make_transactions(first_day, N)).start()
    yield f
    f.stop()


@pytest.fixture
def server_env(fake, tmp_path):
    token_file = tmp_path / "tokens.json"
    token_file.write_text(
        json.dumps(
            {
                "access_token": "stale",
                "refresh_token": fake.refresh_token,
                "expires_at": time.time() - 3600,
                "authorized_at": time.time() - 3600,
            }
        )
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("MONZO_")}
    env.update(
        MONZO_CLIENT_ID="oauth2client_test",
        MONZO_CLIENT_SECRET="test-secret",
        MONZO_API_BASE=fake.base_url,
        MONZO_TOKEN_FILE=str(token_file),
    )
    return env, token_file


def payload(result):
    assert not result.is_error, result.content
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


async def exercise(client: Client, fake: FakeMonzo, token_file: Path, first_day) -> None:
    assert client.server_info.version == __version__
    tools = {t.name for t in (await client.list_tools()).tools}
    assert tools == {
        "get_connection_status",
        "list_accounts",
        "get_balance",
        "list_pots",
        "list_transactions",
        "get_transaction",
    }

    status = payload(await client.call_tool("get_connection_status", {}))
    assert status["connected"] is True and status["days_remaining"] == 89 and status["warning"] is None
    assert status["consent_expires_at"].endswith("Z") and "T" in status["consent_expires_at"]

    accounts = payload(await client.call_tool("list_accounts", {}))
    assert accounts["accounts"][0]["id"] == ACCOUNT_ID and accounts["accounts"][0]["name"] == "Test User"

    balance = payload(await client.call_tool("get_balance", {"account_id": ACCOUNT_ID}))
    assert balance["balance_minor"] == 5000 and balance["currency"] == "GBP"

    window = {
        "account_id": ACCOUNT_ID,
        "since": (first_day - timedelta(days=1)).isoformat(),
        "before": (first_day + timedelta(days=DAYS)).isoformat(),
    }
    txs = payload(await client.call_tool("list_transactions", window))
    # two pages (100 + 50) fetched; the one pending transaction is left out by default
    assert txs["count"] == N - 1 and txs["pending_excluded"] == 1 and txs["truncated"] is False
    assert (txs["since"], txs["before"]) == (window["since"], window["before"])
    last_day = (first_day + timedelta(days=DAYS - 1)).isoformat()
    assert txs["earliest_pending_date"] == last_day
    first = txs["transactions"][0]
    assert first["id"] == "tx_00000" and first["amount_minor"] == -100 and first["merchant"] == "Shop 0"
    assert first["source"] == "monzo" and first["pending"] is False and first["notes"] is None
    assert first["date"] == first_day.isoformat()
    assert all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", t["date"]) for t in txs["transactions"])
    per_day = {}
    for t in txs["transactions"]:
        per_day[t["date"]] = per_day.get(t["date"], 0) + 1
    expected = {(first_day + timedelta(days=d)).isoformat(): PER_DAY for d in range(DAYS)}
    expected[last_day] -= 1
    assert per_day == expected

    with_pending = payload(await client.call_tool("list_transactions", {**window, "include_pending": True}))
    assert with_pending["count"] == N and with_pending["transactions"][-1]["pending"] is True

    # a one-day window returns exactly that London day
    one_day = {**window, "since": first_day.isoformat(), "before": (first_day + timedelta(days=1)).isoformat()}
    assert payload(await client.call_tool("list_transactions", one_day))["count"] == PER_DAY

    bad = await client.call_tool("get_transaction", {"transaction_id": "../accounts"})
    assert bad.is_error and "[invalid_request]" in bad.content[0].text

    # The stale access token forced exactly one refresh, and the rotated token is on disk.
    assert fake.refresh_calls == 1
    saved = json.loads(token_file.read_text())
    assert saved["refresh_token"] == fake.refresh_token == "rt-1"
    assert saved["access_token"] == fake.access_token


async def test_stdio_transport(fake, server_env, first_day):
    env, token_file = server_env
    params = StdioServerParameters(command=sys.executable, args=["-m", "monzo_mcp"], env=env)
    async with Client(params) as client:
        await exercise(client, fake, token_file, first_day)


async def test_streamable_http_transport(fake, server_env, first_day):
    env, token_file = server_env
    port = free_port()
    env.update(MONZO_MCP_TRANSPORT="http", MONZO_MCP_PORT=str(port), MONZO_MCP_AUTH_TOKEN=f"  {MCP_TOKEN}  ")
    proc = subprocess.Popen(
        [sys.executable, "-m", "monzo_mcp"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    url = f"http://127.0.0.1:{port}/mcp"
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                unauth = httpx.post(url, json={}, timeout=2)
                break
            except httpx.TransportError:
                if proc.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError(f"server did not start: {proc.stderr.read().decode()[-2000:]}") from None
                time.sleep(0.2)
        assert unauth.status_code == 401  # no bearer token -> rejected before reaching MCP
        assert httpx.post(url, json={}, headers={"Authorization": "Bearer wrong"}, timeout=5).status_code == 401

        from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

        # lower-case scheme: matched case-insensitively
        async with create_mcp_http_client(headers={"Authorization": f"bearer {MCP_TOKEN}"}) as http:
            async with Client(streamable_http_client(url, http_client=http)) as client:
                await exercise(client, fake, token_file, first_day)
    finally:
        proc.terminate()  # only the process this test started
        proc.wait(timeout=10)
        logs = proc.stderr.read().decode()
    assert "rt-" not in logs and "at-" not in logs and "test-secret" not in logs  # nothing secret logged
    assert MCP_TOKEN not in logs


def test_module_entry_point_and_refusal_to_bind_publicly_without_a_token():
    env = {k: v for k, v in os.environ.items() if not k.startswith("MONZO_")}
    version = subprocess.run([sys.executable, "-m", "monzo_mcp", "--version"], env=env, capture_output=True, text=True)
    assert version.returncode == 0 and version.stdout.strip() == f"monzo-mcp {__version__}"
    refused = subprocess.run(
        [sys.executable, "-m", "monzo_mcp", "--transport", "http", "--host", "0.0.0.0", "--port", str(free_port())],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert refused.returncode != 0 and "MONZO_MCP_AUTH_TOKEN" in refused.stderr
