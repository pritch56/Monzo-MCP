import asyncio
import json
import os
import stat
import subprocess
import sys

import httpx
import pytest
import respx

from conftest import FROZEN_NOW
from monzo_mcp import tokens as tokens_module
from monzo_mcp.client import MonzoClient, MonzoConfig
from monzo_mcp.errors import ConfigError, MonzoAPIError, NotConnected, ReauthRequired, TokenStoreError
from monzo_mcp.tokens import FileTokenStore, MemoryTokenStore, TokenSet, TokenStore, load_token_store

API = "https://api.monzo.com"
NOW = FROZEN_NOW.timestamp()
NEW = {"access_token": "access-new", "refresh_token": "refresh-new", "expires_in": 21600, "user_id": "user_1"}


# ------------------------------------------------------------------ file store


def test_file_store_round_trip(tmp_path):
    store = FileTokenStore(tmp_path / "sub" / "tokens.json")
    assert store.load() is None
    store.save(TokenSet(access_token="a", refresh_token="r", expires_at=5.0, authorized_at=1.0))
    loaded = store.load()
    assert (loaded.access_token, loaded.refresh_token, loaded.expires_at) == ("a", "r", 5.0)
    assert not list(tmp_path.glob("sub/*.tmp"))
    store.clear()
    assert store.load() is None
    store.clear()  # clearing an empty store is not an error


def test_token_store_interface_is_unchanged():
    # A private adapter depends on exactly this: two methods, and this stored shape.
    assert [f for f in TokenSet.__dataclass_fields__] == [
        "access_token",
        "refresh_token",
        "expires_at",
        "authorized_at",
        "approved",
        "user_id",
        "seed_hash",
    ]

    class Minimal:
        def load(self):
            return None

        def save(self, tokens):
            pass

    assert isinstance(Minimal(), TokenStore)
    assert TokenSet.from_json(TokenSet(access_token="a").to_json()).access_token == "a"
    assert TokenSet.from_json('{"access_token": "a", "some_future_field": 1}').access_token == "a"


def test_token_repr_hides_secrets():
    assert "SECRET" not in repr(TokenSet(access_token="SECRET-A", refresh_token="SECRET-R"))


def test_token_file_is_restricted_to_the_current_user(tmp_path):
    path = tmp_path / "tokens.json"
    FileTokenStore(path).save(TokenSet(access_token="a", refresh_token="r"))
    if sys.platform == "win32":
        out = subprocess.run(["icacls", str(path)], capture_output=True, text=True, check=True).stdout
        entries = [line.replace(str(path), "").strip() for line in out.splitlines() if ":(" in line]
        user = os.environ["USERNAME"].lower()
        assert len(entries) == 1, entries  # inheritance removed: no Users/Everyone/Administrators entries
        assert entries[0].lower().endswith(f"{user}:(f)"), entries
    else:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_windows_acl_command_and_non_fatal_failure(tmp_path, monkeypatch, caplog):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 5)  # icacls failing

    monkeypatch.setattr(tokens_module, "_is_windows", lambda: True)
    monkeypatch.setattr(tokens_module.subprocess, "run", fake_run)
    monkeypatch.setenv("USERNAME", "alice")
    monkeypatch.setenv("USERDOMAIN", "BOX")
    path = tmp_path / "tokens.json"
    with caplog.at_level("WARNING", logger="monzo_mcp"):
        FileTokenStore(path).save(TokenSet(access_token="a"))  # must not raise
    assert path.exists()
    assert len(calls) == 1
    assert calls[0][0] == "icacls" and calls[0][2:] == ["/inheritance:r", "/grant:r", "BOX\\alice:(F)"]
    assert calls[0][1].endswith(".tmp")  # locked down before it takes the final name
    assert "Could not restrict the token file" in caplog.text


def test_file_is_fsynced_before_it_replaces_the_old_one(tmp_path, monkeypatch):
    order = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(os, "fsync", lambda fd: (order.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(os, "replace", lambda a, b: (order.append("replace"), real_replace(a, b))[1])
    FileTokenStore(tmp_path / "tokens.json").save(TokenSet(access_token="a"))
    assert order == ["fsync", "replace"]


def test_os_errors_become_token_store_errors(tmp_path, monkeypatch):
    path = tmp_path / "tokens.json"
    store = FileTokenStore(path)
    store.save(TokenSet(access_token="a"))

    def boom(*args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(TokenStoreError) as exc:
        store.save(TokenSet(access_token="b"))
    assert exc.value.kind == "token_store_error"
    assert not list(tmp_path.glob("*.tmp"))  # the temporary file is cleaned up
    assert json.loads(path.read_text())["access_token"] == "a"  # the old file is intact

    path.write_text("[1, 2, 3]")
    with pytest.raises(TokenStoreError):
        store.load()
    path.write_text("{not json")
    with pytest.raises(TokenStoreError):
        store.load()
    path.unlink()
    path.mkdir()  # reading a directory is an OSError, not a missing file
    with pytest.raises(TokenStoreError):
        store.load()


def test_load_token_store_from_env(tmp_path):
    store = load_token_store({"MONZO_TOKEN_FILE": str(tmp_path / "t.json")})
    assert isinstance(store, FileTokenStore) and store.path == tmp_path / "t.json"
    custom = load_token_store({"MONZO_TOKEN_STORE": "monzo_mcp.tokens:MemoryTokenStore"})
    assert isinstance(custom, MemoryTokenStore)
    for bad in ("nope_not_a_module:thing", "no_colon", "monzo_mcp.tokens:nope", "monzo_mcp.tokens:TokenSet"):
        with pytest.raises(ConfigError):
            load_token_store({"MONZO_TOKEN_STORE": bad})


# ------------------------------------------------------------------ refresh


@respx.mock
async def test_expired_token_is_refreshed_and_rotation_persisted_to_disk(tmp_path, config):
    path = tmp_path / "tokens.json"
    store = FileTokenStore(path)
    store.save(
        TokenSet(access_token="access-old", refresh_token="refresh-old", expires_at=NOW - 10, authorized_at=123.0)
    )
    token_route = respx.post(f"{API}/oauth2/token").mock(return_value=httpx.Response(200, json=NEW))
    accounts = respx.get(f"{API}/accounts").mock(return_value=httpx.Response(200, json={"accounts": []}))

    async with httpx.AsyncClient() as http:
        client = MonzoClient(config, store, http=http)
        await client.list_accounts()
        await client.list_accounts()  # second call must reuse the new token, not refresh again

    assert token_route.call_count == 1
    sent = dict(httpx.QueryParams(token_route.calls[0].request.content.decode()))
    assert sent == {
        "grant_type": "refresh_token",
        "client_id": "oauth2client_test",
        "client_secret": "test-secret",
        "refresh_token": "refresh-old",
    }
    assert accounts.calls[0].request.headers["authorization"] == "Bearer access-new"
    on_disk = json.loads(path.read_text())
    assert on_disk["refresh_token"] == "refresh-new" and on_disk["access_token"] == "access-new"
    assert on_disk["authorized_at"] == 123.0  # preserved across refreshes
    assert on_disk["expires_at"] == NOW + 21600


@respx.mock
async def test_401_triggers_one_refresh_then_retry(client, store):
    respx.post(f"{API}/oauth2/token").mock(return_value=httpx.Response(200, json=NEW))
    route = respx.get(f"{API}/accounts").mock(
        side_effect=[
            httpx.Response(401, json={"code": "unauthorized.bad_access_token.expired"}),
            httpx.Response(200, json={"accounts": []}),
        ]
    )
    assert await client.list_accounts() == []
    assert route.call_count == 2
    # two saves: the rotated tokens, then the "approval confirmed" marker
    assert store.tokens.refresh_token == "refresh-new" and store.saves == 2 and store.tokens.approved


@respx.mock
async def test_refresh_token_rejected_means_reauth(config):
    store = MemoryTokenStore(TokenSet(access_token="a", refresh_token="dead", expires_at=0))
    respx.post(f"{API}/oauth2/token").mock(
        return_value=httpx.Response(401, json={"code": "unauthorized.bad_refresh_token"})
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(ReauthRequired) as exc:
            await MonzoClient(config, store, http=http).list_accounts()
    assert "monzo-mcp-authorize" in str(exc.value) and "dead" not in str(exc.value)
    assert exc.value.kind == "token_expired"
    assert store.tokens.refresh_token == "dead"  # nothing overwritten on failure


@respx.mock
async def test_refresh_500_reports_probable_revocation(config):
    store = MemoryTokenStore(TokenSet(access_token="a", refresh_token="r", expires_at=0))
    respx.post(f"{API}/oauth2/token").mock(return_value=httpx.Response(500, json={}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(MonzoAPIError, match="revoked"):
            await MonzoClient(config, store, http=http).list_accounts()


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="<html>Service temporarily unavailable</html>"),
        httpx.Response(200, json={"refresh_token": "r2", "expires_in": 60}),  # no access_token
        httpx.Response(200, json={"access_token": None}),
        httpx.Response(200, json={"access_token": 12345}),
        httpx.Response(200, json=["access_token"]),
        httpx.Response(200, content=b"\xff\xfe"),
    ],
)
@respx.mock
async def test_malformed_refresh_response_is_a_provider_error(config, response):
    store = MemoryTokenStore(TokenSet(access_token="a", refresh_token="r", expires_at=0))
    respx.post(f"{API}/oauth2/token").mock(return_value=response)
    async with httpx.AsyncClient() as http:
        with pytest.raises(MonzoAPIError) as exc:
            await MonzoClient(config, store, http=http).list_accounts()
    assert exc.value.kind == "provider_error"
    assert store.tokens.refresh_token == "r" and store.saves == 0  # nothing half-written


@respx.mock
async def test_access_token_still_401_after_refresh_means_reauth(client):
    respx.post(f"{API}/oauth2/token").mock(return_value=httpx.Response(200, json=NEW))
    respx.get(f"{API}/accounts").mock(
        return_value=httpx.Response(401, json={"code": "unauthorized.bad_access_token.evicted"})
    )
    with pytest.raises(ReauthRequired, match="evicted"):
        await client.list_accounts()


async def test_no_tokens_at_all_is_not_connected(config):
    async with httpx.AsyncClient() as http:
        for store in (MemoryTokenStore(), MemoryTokenStore(TokenSet())):
            with pytest.raises(NotConnected, match="No Monzo tokens") as exc:
                await MonzoClient(config, store, http=http).list_accounts()
            assert exc.value.kind == "not_connected"


@respx.mock
async def test_non_confidential_client_without_refresh_token(config):
    store = MemoryTokenStore(TokenSet(access_token="a", refresh_token=None, expires_at=0))
    respx.get(f"{API}/accounts").mock(return_value=httpx.Response(401, json={"error": "invalid_token"}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(ReauthRequired, match="Confidential"):
            await MonzoClient(config, store, http=http).list_accounts()


# ------------------------------------------------------------------ seed precedence


@respx.mock
async def test_seed_is_used_only_while_the_store_is_empty():
    cfg = MonzoConfig(client_id="c", client_secret="s", seed_refresh_token="seed-1")
    store = MemoryTokenStore()
    token_route = respx.post(f"{API}/oauth2/token").mock(
        side_effect=[
            httpx.Response(200, json={"access_token": "a1", "refresh_token": "rot-1", "expires_in": 3600}),
            httpx.Response(200, json={"access_token": "a2", "refresh_token": "rot-2", "expires_in": 3600}),
        ]
    )
    respx.get(f"{API}/accounts").mock(return_value=httpx.Response(200, json={"accounts": []}))
    async with httpx.AsyncClient() as http:
        await MonzoClient(cfg, store, http=http).list_accounts()
        assert store.tokens.refresh_token == "rot-1"
        assert httpx.QueryParams(token_route.calls[0].request.content.decode())["refresh_token"] == "seed-1"
        # Same (now stale) seed on the next run: the rotated token in the store wins.
        await MonzoClient(cfg, store, http=http).list_accounts()
        assert store.tokens.refresh_token == "rot-1" and token_route.call_count == 1
        # A CHANGED seed does not replace the store either: the store always wins.
        cfg2 = MonzoConfig(client_id="c", client_secret="s", seed_refresh_token="seed-2")
        await MonzoClient(cfg2, store, http=http).list_accounts()
        assert store.tokens.refresh_token == "rot-1" and token_route.call_count == 1
        # Clearing the store is how a new seed takes effect.
        store.clear()
        await MonzoClient(cfg2, store, http=http).list_accounts()
    assert token_route.call_count == 2
    assert httpx.QueryParams(token_route.calls[1].request.content.decode())["refresh_token"] == "seed-2"
    assert store.tokens.refresh_token == "rot-2"


@respx.mock
async def test_store_written_by_the_authorize_helper_beats_any_seed():
    cfg = MonzoConfig(client_id="c", client_secret="s", seed_refresh_token="seed-x", seed_access_token="seed-a")
    store = MemoryTokenStore(TokenSet(access_token="linked", refresh_token="linked-r", expires_at=NOW + 3600))
    accounts = respx.get(f"{API}/accounts").mock(return_value=httpx.Response(200, json={"accounts": []}))
    async with httpx.AsyncClient() as http:
        await MonzoClient(cfg, store, http=http).list_accounts()
    assert accounts.calls[0].request.headers["authorization"] == "Bearer linked"


# ------------------------------------------------------------------ save failure after rotation


class FlakyStore(MemoryTokenStore):
    """A store whose writes can be made to fail."""

    def __init__(self, tokens=None):
        super().__init__(tokens)
        self.broken = False
        self.attempts = 0

    def save(self, tokens):
        self.attempts += 1
        if self.broken:
            raise TokenStoreError("disk full")
        super().save(tokens)


class RotatingBank:
    """httpx transport standing in for Monzo: refresh tokens are single-use, and a refresh
    invalidates the previous access token. `hold` makes that many stale requests wait for
    each other, so their 401s are genuinely concurrent."""

    def __init__(self, hold: int = 0):
        self.generation = 0
        self.refreshes = 0
        self.rejected_refreshes = 0
        self.refresh_tokens_seen: list[str] = []
        self.hold = hold
        self._waiting = 0
        self._release = asyncio.Event()

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            sent = dict(httpx.QueryParams(request.content.decode()))["refresh_token"]
            self.refresh_tokens_seen.append(sent)
            await asyncio.sleep(0.01)  # give a second, racing refresh the chance to start
            if sent != f"rt-{self.generation}":
                self.rejected_refreshes += 1
                return httpx.Response(401, json={"code": "unauthorized.bad_refresh_token"})
            self.generation += 1
            self.refreshes += 1
            return httpx.Response(
                200,
                json={
                    "access_token": f"at-{self.generation}",
                    "refresh_token": f"rt-{self.generation}",
                    "expires_in": 3600,
                },
            )
        if request.headers["authorization"] != f"Bearer at-{self.generation}":
            if self.hold:
                self._waiting += 1
                if self._waiting >= self.hold:
                    self._release.set()
                await self._release.wait()
            return httpx.Response(401, json={"code": "unauthorized.bad_access_token"})
        return httpx.Response(200, json={"accounts": [{"id": "acc_1"}]})


async def test_save_failure_after_rotation_does_not_lose_the_new_refresh_token(config):
    bank = RotatingBank()
    store = FlakyStore(TokenSet(access_token="stale", refresh_token="rt-0", expires_at=NOW - 60, approved=True))
    store.broken = True
    async with httpx.AsyncClient(transport=httpx.MockTransport(bank)) as http:
        client = MonzoClient(config, store, http=http)
        # The refresh succeeded at Monzo, so rt-0 is dead. The save fails, yet the call is served.
        assert await client.list_accounts() == [{"id": "acc_1"}]
        assert store.tokens.refresh_token == "rt-0"  # the store is stale ...
        assert "could not be saved" in client.store_error and "disk full" in client.store_error
        # ... and later calls keep using the in-memory tokens: the dead rt-0 is never sent again.
        assert await client.list_accounts() == [{"id": "acc_1"}]
        assert bank.refresh_tokens_seen == ["rt-0"] and bank.rejected_refreshes == 0
        attempts_while_broken = store.attempts
        assert attempts_while_broken >= 2  # the save was retried on the second call

        # Even when the access token expires again, the refresh uses the in-memory rt-1.
        bank_generation_before = bank.generation
        client._tokens.expires_at = NOW - 1
        assert await client.list_accounts() == [{"id": "acc_1"}]
        assert bank.refresh_tokens_seen == ["rt-0", "rt-1"] and bank.generation == bank_generation_before + 1

        # Once the store works again the next call persists the current tokens.
        store.broken = False
        assert await client.list_accounts() == [{"id": "acc_1"}]
        assert store.tokens.refresh_token == "rt-2" and store.tokens.access_token == "at-2"
        assert client.store_error is None


async def test_relink_while_unsaved_tokens_are_held_wins(config):
    bank = RotatingBank()
    store = FlakyStore(TokenSet(access_token="stale", refresh_token="rt-0", expires_at=NOW - 60))
    store.broken = True
    async with httpx.AsyncClient(transport=httpx.MockTransport(bank)) as http:
        client = MonzoClient(config, store, http=http)
        await client.list_accounts()
        assert client.store_error
        # The user re-links: the authorize helper overwrites the store with a fresh authorization.
        bank.generation = 7
        store.tokens = TokenSet(access_token="at-7", refresh_token="rt-7", expires_at=NOW + 3600)
        store.broken = False
        await client.list_accounts()
    assert store.tokens.refresh_token == "rt-7" and client.store_error is None
    assert bank.refresh_tokens_seen == ["rt-0"]


async def test_store_handing_back_a_spent_refresh_token_is_not_trusted(config):
    bank = RotatingBank()
    original = TokenSet(access_token="stale", refresh_token="rt-0", expires_at=NOW - 60)
    store = MemoryTokenStore(original)
    async with httpx.AsyncClient(transport=httpx.MockTransport(bank)) as http:
        client = MonzoClient(config, store, http=http)
        await client.list_accounts()
        store.tokens = original  # e.g. an eventually-consistent secret store serving an old version
        await client.list_accounts()
    assert bank.refresh_tokens_seen == ["rt-0"] and store.tokens.refresh_token == "rt-1"


async def test_unreadable_store_does_not_stop_a_client_that_already_has_tokens(config):
    class Unreadable(MemoryTokenStore):
        fail = False

        def load(self):
            if self.fail:
                raise RuntimeError("secret backend down: token=hunter2")
            return super().load()

    store = Unreadable(TokenSet(access_token="at-0", refresh_token="rt-0", expires_at=NOW + 3600))
    async with httpx.AsyncClient(transport=httpx.MockTransport(RotatingBank())) as http:
        client = MonzoClient(config, store, http=http)
        await client.list_accounts()
        store.fail = True
        assert await client.list_accounts() == [{"id": "acc_1"}]
        assert "could not be read" in client.store_error and "hunter2" not in client.store_error
        # with no tokens in memory there is nothing to fall back on
        with pytest.raises(TokenStoreError) as exc:
            await MonzoClient(config, store, http=http).list_accounts()
        assert "hunter2" not in str(exc.value)


# ------------------------------------------------------------------ concurrency


async def test_concurrent_401s_cause_exactly_one_refresh(config):
    n = 5
    bank = RotatingBank(hold=n)
    # Not expired as far as the client can tell, but Monzo no longer accepts it.
    store = MemoryTokenStore(TokenSet(access_token="evicted", refresh_token="rt-0", expires_at=NOW + 3600))
    async with httpx.AsyncClient(transport=httpx.MockTransport(bank)) as http:
        client = MonzoClient(config, store, http=http)
        results = await asyncio.wait_for(
            asyncio.gather(*(client.list_accounts() for _ in range(n)), return_exceptions=True), timeout=10
        )
    assert results == [[{"id": "acc_1"}]] * n  # no spurious [token_expired]
    assert bank.refreshes == 1 and bank.rejected_refreshes == 0
    assert bank.refresh_tokens_seen == ["rt-0"]
    assert store.tokens.refresh_token == "rt-1" and store.tokens.access_token == "at-1"


async def test_concurrent_calls_with_an_expired_token_refresh_once(config):
    bank = RotatingBank()
    store = MemoryTokenStore(TokenSet(access_token="old", refresh_token="rt-0", expires_at=NOW - 60))
    async with httpx.AsyncClient(transport=httpx.MockTransport(bank)) as http:
        client = MonzoClient(config, store, http=http)
        results = await asyncio.gather(*(client.list_accounts() for _ in range(8)))
    assert len(results) == 8 and bank.refreshes == 1 and bank.rejected_refreshes == 0
