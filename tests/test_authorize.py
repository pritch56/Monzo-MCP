"""The authorize helper, driven against a local fake token endpoint. No browser, no Monzo."""

import json
import threading
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from conftest import FROZEN_NOW
from fake_monzo import FakeMonzo, free_port
from monzo_mcp import authorize
from monzo_mcp.authorize import AuthorizeError, clean, exchange_code, extract_code, wait_for_code
from monzo_mcp.tokens import FileTokenStore, TokenSet

STATE = "state-for-this-run"


def get(url: str) -> httpx.Response:
    deadline = time.monotonic() + 10
    while True:
        try:
            return httpx.get(url, timeout=5)
        except httpx.TransportError:  # the listener is not up yet, or is between requests
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)


def run_callbacks(urls: list[str]) -> tuple[threading.Thread, list[int]]:
    statuses: list[int] = []

    def worker():
        for url in urls:
            statuses.append(get(url).status_code)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return thread, statuses


def test_wrong_or_missing_state_gets_400_and_the_helper_keeps_listening():
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    thread, statuses = run_callbacks(
        [
            f"{base}/callback?code=attacker-code&state=wrong",  # wrong state
            f"{base}/callback?code=attacker-code",  # no state at all
            f"{base}/callback?state=",  # empty state
            f"{base}/favicon.ico",  # a stray request
            f"{base}/callback?state={STATE}",  # right state but no code: also ignored
            f"{base}/callback?code=real-code&state={STATE}",
        ]
    )
    code = wait_for_code(f"{base}/callback", STATE, timeout=20)
    thread.join(timeout=10)
    assert code == "real-code"  # the attacker's code was never accepted and the flow was not aborted
    assert statuses == [400, 400, 400, 404, 400, 200]


def test_wait_for_code_times_out_when_only_bad_requests_arrive():
    port = free_port()
    thread, statuses = run_callbacks([f"http://127.0.0.1:{port}/callback?code=x&state=wrong"])
    with pytest.raises(AuthorizeError, match="Timed out"):
        wait_for_code(f"http://127.0.0.1:{port}/callback", STATE, timeout=1.5)
    thread.join(timeout=10)
    assert statuses == [400]


def test_error_from_monzo_with_the_right_state_stops_the_flow_and_is_sanitised():
    port = free_port()
    thread, _ = run_callbacks([f"http://127.0.0.1:{port}/callback?state={STATE}&error=access_denied%1b%5b31m%0aEVIL"])
    with pytest.raises(AuthorizeError) as exc:
        wait_for_code(f"http://127.0.0.1:{port}/callback", STATE, timeout=20)
    thread.join(timeout=10)
    assert "access_denied" in str(exc.value) and "\x1b" not in str(exc.value) and "\n" not in str(exc.value)


@pytest.mark.parametrize(
    "uri", ["http://example.test/callback", "https://localhost:8765/callback", "http://10.0.0.5:1/cb"]
)
def test_redirect_uri_must_be_plain_http_on_loopback(uri):
    with pytest.raises(AuthorizeError, match="redirect URI"):
        wait_for_code(uri, STATE, timeout=0.1)


def test_extract_code_requires_state_whenever_a_url_is_pasted():
    assert extract_code(f"http://localhost:8765/callback?code=abc&state={STATE}", STATE) == "abc"
    assert extract_code(f"  http://localhost:8765/callback?state={STATE}&code=abc  ", STATE) == "abc"
    assert extract_code(f"code=abc&state={STATE}", STATE) == "abc"
    assert extract_code("eyJhbGciOi.bare-code_123", STATE) == "eyJhbGciOi.bare-code_123"  # a bare code
    for pasted in (
        "http://localhost:8765/callback?code=abc",  # URL without state
        "http://localhost:8765/callback?code=abc&state=other",
        "localhost:8765/callback?code=abc",
        "/callback?code=abc",
        "code=abc",
        "?code=abc&state=",
    ):
        with pytest.raises(AuthorizeError, match="state"):
            extract_code(pasted, STATE)
    with pytest.raises(AuthorizeError, match="no `code`"):
        extract_code(f"http://localhost:8765/callback?state={STATE}", STATE)
    with pytest.raises(AuthorizeError, match="access_denied"):
        extract_code(f"http://localhost:8765/callback?state={STATE}&error=access_denied", STATE)
    for junk in ("", "   ", "two words", "abc\x1b[2J"):
        with pytest.raises(AuthorizeError):
            extract_code(junk, STATE)


def test_clean_strips_control_characters():
    assert clean("ok\x1b[31m red\r\n\x07\x00 end") == "ok[31m red end"
    assert clean("‮gnp.exe") == "gnp.exe"  # bidi override is a format character
    assert len(clean("x" * 1000)) == 300 and clean(404) == "404"


@pytest.fixture
def fake():
    f = FakeMonzo(auth_code="good-code").start()
    yield f
    f.stop()


def test_exchange_code_against_the_fake_token_endpoint(fake):
    with httpx.Client() as http:
        with pytest.raises(AuthorizeError, match="HTTP 401") as exc:
            exchange_code(http, fake.base_url, "client", "s3cret-value", "http://localhost:1/cb", "bad-code")
        assert "s3cret-value" not in str(exc.value) and "bad-code" not in str(exc.value)
        body = exchange_code(http, fake.base_url, "client", "s3cret-value", "http://localhost:1/cb", "good-code")
        assert body["access_token"] == "at-1" and body["refresh_token"] == "rt-1"
        with pytest.raises(AuthorizeError):  # authorization codes are single-use
            exchange_code(http, fake.base_url, "client", "s3cret-value", "http://localhost:1/cb", "good-code")
        with pytest.raises(AuthorizeError, match="Network error"):
            exchange_code(http, f"http://127.0.0.1:{free_port()}", "c", "s", "http://localhost:1/cb", "x")


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="<html>"),
        httpx.Response(200, json={"token_type": "Bearer"}),
        httpx.Response(200, json=[]),
    ],
)
def test_exchange_code_rejects_a_malformed_200(response):
    with httpx.Client(transport=httpx.MockTransport(lambda request: response)) as http:
        with pytest.raises(AuthorizeError, match="access token"):
            exchange_code(http, "https://api.monzo.com", "c", "s", "http://localhost:1/cb", "x")


@pytest.fixture
def helper_env(monkeypatch, tmp_path, fake):
    token_file = tmp_path / "store" / "tokens.json"
    monkeypatch.setenv("MONZO_CLIENT_ID", "oauth2client_test")
    monkeypatch.setenv("MONZO_CLIENT_SECRET", "test-secret")
    monkeypatch.setenv("MONZO_API_BASE", fake.base_url)
    monkeypatch.setenv("MONZO_TOKEN_FILE", str(token_file))
    monkeypatch.setattr(authorize.secrets, "token_urlsafe", lambda n=32: STATE)
    return token_file


def test_full_flow_survives_a_stray_wrong_state_request(helper_env, fake, capsys):
    token_file = helper_env
    # An earlier authorization is on disk: linking again must overwrite it.
    FileTokenStore(token_file).save(TokenSet(access_token="previous", refresh_token="previous-r", approved=True))
    port = free_port()
    redirect = f"http://127.0.0.1:{port}/callback"
    thread, statuses = run_callbacks(
        [f"{redirect}?code=attacker&state=nope", f"{redirect}?code=good-code&state={STATE}"]
    )
    authorize.main(["--no-browser", "--redirect-uri", redirect, "--poll-interval", "0.05", "--timeout", "30"])
    thread.join(timeout=10)
    assert statuses == [400, 200]

    out = capsys.readouterr().out
    shown = next(line.strip() for line in out.splitlines() if line.strip().startswith("https://auth.monzo.com/"))
    query = parse_qs(urlsplit(shown).query)
    assert query == {
        "client_id": ["oauth2client_test"],
        "redirect_uri": [redirect],
        "response_type": ["code"],
        "state": [STATE],
    }
    assert "Approved." in out
    # neither tokens nor the client secret are ever printed
    for secret in ("at-1", "rt-1", "test-secret", "good-code"):
        assert secret not in out

    saved = json.loads(token_file.read_text())
    assert saved["access_token"] == "at-1" and saved["refresh_token"] == "rt-1"
    assert saved["approved"] is True and saved["user_id"] == "user_1"
    assert saved["authorized_at"] == FROZEN_NOW.timestamp() and saved["expires_at"] == FROZEN_NOW.timestamp() + 21600
    assert fake.exchange_calls == 1 and fake.account_polls == 2  # one 403 before the in-app approval


def test_paste_mode_rejects_a_url_without_the_right_state(helper_env, fake, monkeypatch):
    token_file = helper_env
    monkeypatch.setattr(
        "builtins.input", lambda prompt="": "http://localhost:8765/callback?code=good-code&state=forged"
    )
    with pytest.raises(SystemExit) as exc:
        authorize.main(["--paste"])
    assert "state" in str(exc.value)
    assert fake.exchange_calls == 0 and not token_file.exists()

    monkeypatch.setattr(
        "builtins.input", lambda prompt="": f"http://localhost:8765/callback?code=good-code&state={STATE}"
    )
    authorize.main(["--paste", "--poll-interval", "0.05"])
    assert json.loads(token_file.read_text())["refresh_token"] == "rt-1"


def test_unapproved_access_is_saved_but_reported(helper_env, fake, monkeypatch, capsys):
    token_file = helper_env
    fake.approve_after_polls = 10_000
    monkeypatch.setattr("builtins.input", lambda prompt="": f"code=good-code&state={STATE}")
    with pytest.raises(SystemExit) as exc:
        authorize.main(["--paste", "--poll-interval", "0.05", "--approval-timeout", "0.3"])
    assert exc.value.code == 1 and "not approved" in capsys.readouterr().out
    saved = json.loads(token_file.read_text())
    assert saved["refresh_token"] == "rt-1" and saved["approved"] is False  # kept, so approving late still works


def test_reset_clears_the_store(helper_env, capsys):
    token_file = helper_env
    FileTokenStore(token_file).save(TokenSet(access_token="a", refresh_token="r"))
    authorize.main(["--reset"])
    assert not token_file.exists() and "deleted" in capsys.readouterr().out
    authorize.main(["--reset"])  # nothing to delete is fine


def test_helper_refuses_a_plain_http_api_base(helper_env, monkeypatch):
    monkeypatch.setenv("MONZO_API_BASE", "http://evil.example")
    with pytest.raises(SystemExit) as exc:
        authorize.main(["--paste"])
    assert "MONZO_API_BASE" in str(exc.value)
