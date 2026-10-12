import logging

import httpx
import pytest
import respx

from monzo_mcp import USER_AGENT, __version__
from monzo_mcp.client import MonzoConfig, validate_api_base
from monzo_mcp.errors import (
    AccountNotFound,
    ConfigError,
    ConsentExpired,
    HistoryWindowExceeded,
    InvalidRequest,
    MonzoAPIError,
    RateLimited,
    ScaNotApproved,
)

API = "https://api.monzo.com"
ACC = "acc_1"
RECENT = "2026-10-04T12:00:00Z"  # a week before the frozen clock


def txs(start, n):
    return [{"id": f"tx_{i:05d}", "amount": -i, "created": "2026-10-01T00:00:00Z"} for i in range(start, start + n)]


@respx.mock
async def test_pagination_follows_id_cursor(client):
    route = respx.get(f"{API}/transactions").mock(
        side_effect=[
            httpx.Response(200, json={"transactions": txs(0, 100)}),
            httpx.Response(200, json={"transactions": txs(100, 100)}),
            httpx.Response(200, json={"transactions": txs(200, 37)}),
        ]
    )
    items, truncated = await client.list_transactions(ACC, RECENT, before="2026-10-10T00:00:00Z")
    assert len(items) == 237 and not truncated
    assert [i["id"] for i in items] == [f"tx_{i:05d}" for i in range(237)]
    q = [c.request.url.params for c in route.calls]
    assert [p["since"] for p in q] == [RECENT, "tx_00099", "tx_00199"]
    assert all(
        p["limit"] == "100"
        and p["account_id"] == ACC
        and p["expand[]"] == "merchant"
        and p["before"] == "2026-10-10T00:00:00Z"
        for p in q
    )
    assert all(c.request.headers["user-agent"] == USER_AGENT == f"monzo-mcp/{__version__}" for c in route.calls)


@respx.mock
async def test_pagination_stops_at_max_items_and_flags_truncation(client):
    route = respx.get(f"{API}/transactions").mock(
        side_effect=[
            httpx.Response(200, json={"transactions": txs(0, 100)}),
            httpx.Response(200, json={"transactions": txs(100, 100)}),
        ]
    )
    items, truncated = await client.list_transactions(ACC, RECENT, max_items=150)
    assert len(items) == 150 and truncated and route.call_count == 2


@respx.mock
async def test_cursor_resumes_after_a_transaction(client):
    route = respx.get(f"{API}/transactions").mock(return_value=httpx.Response(200, json={"transactions": []}))
    await client.list_transactions(ACC, RECENT, cursor="tx_00149")
    assert route.calls[0].request.url.params["since"] == "tx_00149"
    with pytest.raises(InvalidRequest):
        await client.list_transactions(ACC, RECENT, cursor="tx_1/../../accounts")
    assert route.call_count == 1


@respx.mock
async def test_pagination_does_not_loop_on_repeated_page(client):
    route = respx.get(f"{API}/transactions").mock(return_value=httpx.Response(200, json={"transactions": txs(0, 100)}))
    items, _ = await client.list_transactions(ACC, RECENT)
    assert len(items) == 100 and route.call_count == 2


@respx.mock
async def test_same_id_with_different_content_is_kept_not_dropped(client, caplog):
    a = {"id": "tx_1", "amount": -500, "created": "2026-10-01T10:00:00Z", "description": "COFFEE"}
    exact_repeat = dict(a)
    b = {"id": "tx_1", "amount": -700, "created": "2026-10-01T10:00:00Z", "description": "COFFEE"}
    c = {"id": "tx_1", "amount": -500, "created": "2026-10-02T10:00:00Z", "description": "COFFEE"}
    d = {"id": "tx_1", "amount": -500, "created": "2026-10-01T10:00:00Z", "description": "TEA"}
    no_id = {"amount": -1, "created": "2026-10-01T10:00:00Z", "description": "NO ID"}
    respx.get(f"{API}/transactions").mock(
        return_value=httpx.Response(200, json={"transactions": [a, exact_repeat, b, c, d, no_id]})
    )
    with caplog.at_level(logging.WARNING, logger="monzo_mcp"):
        items, _ = await client.list_transactions(ACC, RECENT)
    assert [(i["amount"], i["created"][:10], i["description"]) for i in items] == [
        (-500, "2026-10-01", "COFFEE"),
        (-700, "2026-10-01", "COFFEE"),
        (-500, "2026-10-02", "COFFEE"),
        (-500, "2026-10-01", "TEA"),
        (-1, "2026-10-01", "NO ID"),
    ]
    ids = [i["id"] for i in items]
    assert len(set(ids)) == 5 and ids[0] == "tx_1"
    assert ids[1].startswith("tx_1_dup1_") and ids[2].startswith("tx_1_dup2_") and ids[3].startswith("tx_1_dup3_")
    assert items[1]["_provider_id"] == "tx_1" and ids[4].startswith("monzo_noid_")
    assert "same" not in caplog.text and "two different transactions with the id tx_1" in caplog.text


@respx.mock
async def test_sca_not_approved(client):
    respx.get(f"{API}/accounts").mock(
        return_value=httpx.Response(403, json={"code": "forbidden.insufficient_permissions", "message": "forbidden"})
    )
    with pytest.raises(ScaNotApproved) as exc:
        await client.list_accounts()
    assert "Monzo app" in str(exc.value) and exc.value.kind == "sca_not_approved"


@respx.mock
async def test_forbidden_after_previous_success_is_consent_expired(client):
    route = respx.get(f"{API}/accounts").mock(
        side_effect=[
            httpx.Response(200, json={"accounts": []}),
            httpx.Response(403, json={"code": "forbidden.insufficient_permissions"}),
        ]
    )
    await client.list_accounts()
    with pytest.raises(ConsentExpired, match="90 days") as exc:
        await client.list_accounts()
    assert exc.value.kind == "consent_expired" and route.call_count == 2


@respx.mock
async def test_verification_required_on_balance_is_sca(client):
    respx.get(f"{API}/balance").mock(
        return_value=httpx.Response(403, json={"code": "forbidden.verification_required", "message": "no"})
    )
    with pytest.raises(ScaNotApproved):
        await client.get_balance(ACC)


async def test_90_day_window_rejected_locally_without_calling_monzo(client):
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(f"{API}/transactions")
        with pytest.raises(HistoryWindowExceeded, match="90 days") as exc:
            await client.list_transactions(ACC, "2026-06-01T00:00:00Z")
        assert route.call_count == 0 and exc.value.kind == "invalid_request"


@respx.mock
async def test_old_history_allowed_within_5_minutes_of_approval(client, store, frozen_clock):
    store.tokens.authorized_at = frozen_clock() - 60
    respx.get(f"{API}/transactions").mock(return_value=httpx.Response(200, json={"transactions": txs(0, 3)}))
    items, _ = await client.list_transactions(ACC, "2019-01-01T00:00:00Z")
    assert len(items) == 3


@respx.mock
async def test_90_day_window_rejected_by_monzo(client):
    respx.get(f"{API}/transactions").mock(
        return_value=httpx.Response(403, json={"code": "forbidden.verification_required", "message": "no"})
    )
    with pytest.raises(HistoryWindowExceeded, match="5 minutes"):
        await client.list_transactions(ACC, RECENT)


@respx.mock
async def test_429_is_never_retried(client):
    route = respx.get(f"{API}/accounts").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}, json={"code": "too_many_requests"}),
            httpx.Response(200, json={"accounts": [{"id": "acc_1"}]}),
        ]
    )
    with pytest.raises(RateLimited, match="rate limit") as exc:
        await client.list_accounts()
    assert route.call_count == 1 and exc.value.retry_after == 7 and exc.value.kind == "rate_limited"


@respx.mock
async def test_429_on_token_refresh_is_not_retried(client, store):
    store.tokens.expires_at = 0
    route = respx.post(f"{API}/oauth2/token").mock(return_value=httpx.Response(429, json={}))
    with pytest.raises(RateLimited):
        await client.list_accounts()
    assert route.call_count == 1


@pytest.mark.parametrize(
    "bad_id",
    [
        "../accounts",
        "tx_1/../../accounts",
        "tx_1?x=1",
        "tx_1#frag",
        "tx_",
        "",
        "tx_%2e%2e",
        "tx_١٢٣",  # non-ASCII digits pass str.isalnum() but must not pass here
        "tx_abc\n",
        "acc_1",
    ],
)
async def test_transaction_id_cannot_reach_other_endpoints(client, bad_id):
    with respx.mock(assert_all_called=False) as mock:
        everything = mock.route().mock(return_value=httpx.Response(200, json={"accounts": [{"id": "acc_secret"}]}))
        with pytest.raises(InvalidRequest) as exc:
            await client.get_transaction(bad_id)
        assert everything.call_count == 0 and exc.value.kind == "invalid_request"


@pytest.mark.parametrize("bad_id", ["", "acc 1", "acc_1&account_id=acc_2", "acc_١", "../x", None, 5])
async def test_account_id_is_validated_before_any_request(client, bad_id):
    with respx.mock(assert_all_called=False) as mock:
        everything = mock.route().mock(return_value=httpx.Response(200, json={}))
        for call in (client.get_balance, client.list_pots):
            with pytest.raises(InvalidRequest):
                await call(bad_id)
        with pytest.raises(InvalidRequest):
            await client.list_transactions(bad_id, RECENT)
        assert everything.call_count == 0


@respx.mock
async def test_unknown_account_is_reported_once_the_account_list_is_known(client):
    accounts = respx.get(f"{API}/accounts").mock(return_value=httpx.Response(200, json={"accounts": [{"id": ACC}]}))
    balance = respx.get(f"{API}/balance").mock(return_value=httpx.Response(200, json={"balance": 1}))
    await client.list_accounts()
    await client.get_balance(ACC)
    with pytest.raises(AccountNotFound) as exc:
        await client.get_balance("acc_other")
    assert exc.value.kind == "account_not_found"
    assert balance.call_count == 1 and accounts.call_count == 2  # the list was re-checked once, no balance call
    # an account opened since the list was fetched is found by that re-check
    accounts.mock(return_value=httpx.Response(200, json={"accounts": [{"id": ACC}, {"id": "acc_new"}]}))
    await client.get_balance("acc_new")


@respx.mock
async def test_404_for_an_account_is_account_not_found(client):
    respx.get(f"{API}/balance").mock(return_value=httpx.Response(404, json={"code": "not_found"}))
    with pytest.raises(AccountNotFound):
        await client.get_balance("acc_gone")


@respx.mock
async def test_not_found_and_no_token_leak(client):
    respx.get(f"{API}/transactions/tx_x").mock(return_value=httpx.Response(404, json={"code": "not_found"}))
    with pytest.raises(MonzoAPIError, match="could not find") as exc:
        await client.get_transaction("tx_x")
    assert "access-old" not in str(exc.value) and exc.value.kind == "provider_error"


@respx.mock
async def test_network_error_is_wrapped(client):
    respx.get(f"{API}/accounts").mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(MonzoAPIError, match="Network error"):
        await client.list_accounts()


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="<html>gateway</html>"),
        httpx.Response(200, json=["not", "an", "object"]),
        httpx.Response(200, json={"accounts": "nope"}),
        httpx.Response(200, json={"accounts": ["acc_1"]}),
        httpx.Response(502, text="bad gateway"),
    ],
)
@respx.mock
async def test_malformed_or_non_json_responses_are_provider_errors(client, response):
    respx.get(f"{API}/accounts").mock(return_value=response)
    with pytest.raises(MonzoAPIError) as exc:
        await client.list_accounts()
    assert exc.value.kind == "provider_error"


@respx.mock
async def test_only_get_requests_reach_the_api(client):
    respx.get(f"{API}/accounts").mock(return_value=httpx.Response(200, json={"accounts": [{"id": ACC}]}))
    respx.get(f"{API}/balance").mock(return_value=httpx.Response(200, json={}))
    pots = respx.get(f"{API}/pots").mock(return_value=httpx.Response(200, json={"pots": []}))
    respx.get(f"{API}/ping/whoami").mock(return_value=httpx.Response(200, json={"authenticated": True}))
    await client.list_accounts()
    await client.get_balance(ACC)
    await client.list_pots(ACC)
    await client.whoami()
    assert {c.request.method for c in respx.calls} == {"GET"}
    assert pots.calls[0].request.url.params["current_account_id"] == ACC


@pytest.mark.parametrize(
    "url",
    [
        "http://api.monzo.com",
        "http://evil.example",
        "http://127.0.0.1.evil.example",
        "http://localhost.evil.example:8080",
        "ftp://127.0.0.1",
        "https://user:pw@api.monzo.com",
        "https://api.monzo.com/?x=1",
        "api.monzo.com",
        "",
    ],
)
def test_api_base_must_be_https_unless_loopback(url):
    with pytest.raises(ConfigError) as exc:
        validate_api_base(url)
    assert exc.value.kind == "config_error"
    with pytest.raises(ConfigError):
        MonzoConfig(api_base=url)
    if url:
        with pytest.raises(ConfigError):
            MonzoConfig.from_env({"MONZO_API_BASE": url})


@pytest.mark.parametrize(
    "url",
    [
        "https://api.monzo.com",
        "https://example.test/base/",
        "http://127.0.0.1:8080",
        "http://localhost:1",
        "http://[::1]:9",
    ],
)
def test_api_base_accepts_https_and_loopback_http(url):
    assert validate_api_base(url) == url.rstrip("/")
    assert MonzoConfig.from_env({"MONZO_API_BASE": url}).api_base == url.rstrip("/")
    assert MonzoConfig.from_env({}).api_base == "https://api.monzo.com"
