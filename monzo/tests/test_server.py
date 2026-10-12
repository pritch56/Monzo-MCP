"""MCP-level tests: real MCP client <-> server in-process, Monzo HTTP mocked with respx.

The clock is frozen (see conftest), so every date below is a literal.
"""

import json
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
import pytest
import respx
from mcp import Client

from conftest import FROZEN_NOW
from monzo_mcp import __version__, server
from monzo_mcp.client import MonzoClient
from monzo_mcp.errors import ConfigError, TokenStoreError
from monzo_mcp.tokens import MemoryTokenStore, TokenSet

API = "https://api.monzo.com"
NOW = FROZEN_NOW.timestamp()  # 2026-10-11T12:00:00Z
EXPECTED_TOOLS = {
    "get_connection_status",
    "list_accounts",
    "get_balance",
    "list_pots",
    "list_transactions",
    "get_transaction",
}
SHAPE = ["id", "source", "account_id", "date", "amount_minor", "currency", "description", "merchant", "category"]
SHAPE += ["pending", "notes"]

CREATED_TS = "2026-10-06T12:00:00Z"
SETTLED = {
    "id": "tx_1",
    "account_id": "acc_1",
    "created": CREATED_TS,
    "amount": -510,
    "currency": "GBP",
    "description": "DELI",
    "merchant": {"id": "merch_1", "name": "Deli Co"},
    "category": "eating_out",
    "settled": CREATED_TS,
    "notes": "",
}
PENDING = {**SETTLED, "id": "tx_2", "settled": ""}
DECLINED = {**SETTLED, "id": "tx_3", "settled": "", "decline_reason": "INSUFFICIENT_FUNDS"}
FORBIDDEN = httpx.Response(403, json={"code": "forbidden.insufficient_permissions"})


@asynccontextmanager
async def connect(client):
    server.set_client(client)
    try:
        async with Client(server.mcp) as c:
            yield c
    finally:
        server.set_client(None)


def payload(result):
    assert not result.is_error, result.content
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


def error_text(result) -> str:
    assert result.is_error, result
    return result.content[0].text


def tx(tx_id, created, **extra):
    return {**SETTLED, "id": tx_id, "created": created, "settled": created, **extra}


def mock_transactions(*transactions):
    return respx.get(f"{API}/transactions").mock(
        return_value=httpx.Response(200, json={"transactions": list(transactions)})
    )


async def test_tools_are_exactly_the_read_only_set(client):
    async with connect(client) as mcp_client:
        tools = (await mcp_client.list_tools()).tools
        assert {t.name for t in tools} == EXPECTED_TOOLS
        assert all(t.annotations and t.annotations.read_only_hint for t in tools)
        forbidden = ("deposit", "withdraw", "feed", "webhook", "transfer", "update", "annotate", "upload", "receipt")
        assert not [t.name for t in tools if any(word in t.name for word in forbidden)]
        listing = next(t for t in tools if t.name == "list_transactions")
        assert {"account_id", "since", "before", "include_pending"} <= set(listing.input_schema["properties"])
        # the overlap requirement is in the text every client model reads
        assert "overlapping" in listing.description and "earliest_pending_date" in listing.description
        assert "de-duplicated on `id`" in listing.description


async def test_server_info_reports_the_package_version(client):
    async with connect(client) as mcp_client:
        info = mcp_client.server_info
    assert info.name == "monzo" and info.version == __version__ and __version__


@respx.mock
async def test_list_transactions_excludes_pending_and_declined_by_default(client):
    route = mock_transactions(SETTLED, PENDING, DECLINED)
    async with connect(client) as mcp_client:
        args = {"account_id": "acc_1", "since": "2026-10-02"}
        out = payload(await mcp_client.call_tool("list_transactions", args))
        assert out["count"] == 1 and out["pending_excluded"] == 1 and out["declined_excluded"] == 1
        assert out["earliest_pending_date"] == "2026-10-06" and out["pending_supported"] is True
        assert out["truncated"] is False and out["next_cursor"] is None
        assert out["since"] == "2026-10-02" and out["before"] == "2026-10-12"
        assert out["transactions"][0] == {
            "id": "tx_1",
            "source": "monzo",
            "account_id": "acc_1",
            "date": "2026-10-06",
            "amount_minor": -510,
            "currency": "GBP",
            "description": "DELI",
            "merchant": "Deli Co",
            "category": "eating_out",
            "pending": False,
            "notes": None,
        }
        assert list(out["transactions"][0]) == SHAPE
        # YYYY-MM-DD means UK local midnight (BST here); an upper bound in the future is not sent
        sent = route.calls[0].request.url.params
        assert sent["since"] == "2026-10-01T23:00:00Z" and "before" not in sent

        out = payload(await mcp_client.call_tool("list_transactions", {**args, "include_pending": True}))
        assert [t["id"] for t in out["transactions"]] == ["tx_1", "tx_2"]
        assert out["pending_excluded"] == 0 and out["earliest_pending_date"] is None
        assert out["transactions"][1]["pending"] is True

        out = payload(
            await mcp_client.call_tool("list_transactions", {**args, "include_pending": True, "include_declined": True})
        )
        assert out["count"] == 3 and out["declined_excluded"] == 0


@respx.mock
async def test_declined_rows_cannot_be_mistaken_for_spending(client):
    mock_transactions(SETTLED, {**DECLINED, "notes": "tried twice"})
    async with connect(client) as mcp_client:
        out = payload(
            await mcp_client.call_tool(
                "list_transactions", {"account_id": "acc_1", "since": "2026-10-02", "include_declined": True}
            )
        )
    declined = next(t for t in out["transactions"] if t["id"] == "tx_3")
    assert declined["amount_minor"] == 0 and declined["pending"] is False
    assert declined["notes"] == "declined: INSUFFICIENT_FUNDS; declined_amount_minor=-510; note=tried twice"
    assert sum(t["amount_minor"] for t in out["transactions"]) == -510  # only the real payment counts


@respx.mock
async def test_merchant_as_bare_id_or_null_on_the_list_endpoint(client):
    # expand[]=merchant is only documented for the single-transaction endpoint.
    mock_transactions(
        tx("tx_a", CREATED_TS, merchant="merch_00009abc"),
        tx("tx_b", CREATED_TS, merchant=None),
        tx("tx_c", CREATED_TS, merchant={"id": "merch_1"}),
        {k: v for k, v in tx("tx_d", CREATED_TS).items() if k != "merchant"},
    )
    async with connect(client) as mcp_client:
        out = payload(await mcp_client.call_tool("list_transactions", {"account_id": "acc_1", "since": "2026-10-02"}))
    assert [t["merchant"] for t in out["transactions"]] == [None, None, None, None]
    assert [t["description"] for t in out["transactions"]] == ["DELI"] * 4


@respx.mock
async def test_window_is_since_inclusive_before_exclusive_on_uk_dates(client):
    route = mock_transactions(
        tx("tx_before_start", "2026-10-04T22:59:59Z"),  # 23:59:59 BST on the 4th
        tx("tx_at_start", "2026-10-04T23:00:00Z"),  # 00:00:00 BST on the 5th
        tx("tx_last_second", "2026-10-06T22:59:59Z"),
        tx("tx_at_end", "2026-10-06T23:00:00Z"),
    )
    async with connect(client) as mcp_client:
        out = payload(
            await mcp_client.call_tool(
                "list_transactions", {"account_id": "acc_1", "since": "2026-10-05", "before": "2026-10-07"}
            )
        )
    assert [t["id"] for t in out["transactions"]] == ["tx_at_start", "tx_last_second"]
    assert [t["date"] for t in out["transactions"]] == ["2026-10-05", "2026-10-06"]
    assert (out["since"], out["before"]) == ("2026-10-05", "2026-10-07")
    sent = route.calls[0].request.url.params
    assert (sent["since"], sent["before"]) == ("2026-10-04T23:00:00Z", "2026-10-06T23:00:00Z")


@respx.mock
async def test_bst_month_boundary_2330z_on_30_september_is_1_october(client):
    mock_transactions(tx("tx_edge", "2026-09-30T23:30:00Z"))
    async with connect(client) as mcp_client:
        october = payload(
            await mcp_client.call_tool(
                "list_transactions", {"account_id": "acc_1", "since": "2026-10-01", "before": "2026-10-02"}
            )
        )
        september = payload(
            await mcp_client.call_tool(
                "list_transactions", {"account_id": "acc_1", "since": "2026-09-30", "before": "2026-10-01"}
            )
        )
        as_datetime = payload(
            await mcp_client.call_tool(
                "list_transactions", {"account_id": "acc_1", "since": "2026-09-30T23:30:00Z", "before": "2026-10-02"}
            )
        )
    assert [(t["id"], t["date"]) for t in october["transactions"]] == [("tx_edge", "2026-10-01")]
    assert september["transactions"] == []
    # a datetime argument is reduced to its London date, and the echo says which
    assert as_datetime["since"] == "2026-10-01" and as_datetime["count"] == 1


@respx.mock
async def test_autumn_clock_change_day_is_25_hours_long(client, frozen_clock):
    frozen_clock.set(datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc))
    route = mock_transactions(
        tx("tx_sat_late", "2026-10-24T22:59:59Z"),  # 23:59:59 BST Saturday
        tx("tx_sun_first", "2026-10-24T23:00:00Z"),  # 00:00 BST Sunday
        tx("tx_sun_0130_bst", "2026-10-25T00:30:00Z"),
        tx("tx_sun_0130_gmt", "2026-10-25T01:30:00Z"),
        tx("tx_sun_last", "2026-10-25T23:59:59Z"),  # 23:59:59 GMT Sunday
        tx("tx_mon_first", "2026-10-26T00:00:00Z"),
    )
    async with connect(client) as mcp_client:
        out = payload(
            await mcp_client.call_tool(
                "list_transactions", {"account_id": "acc_1", "since": "2026-10-25", "before": "2026-10-26"}
            )
        )
    assert [t["id"] for t in out["transactions"]] == [
        "tx_sun_first",
        "tx_sun_0130_bst",
        "tx_sun_0130_gmt",
        "tx_sun_last",
    ]
    assert {t["date"] for t in out["transactions"]} == {"2026-10-25"}
    sent = route.calls[0].request.url.params
    assert (sent["since"], sent["before"]) == ("2026-10-24T23:00:00Z", "2026-10-26T00:00:00Z")


@pytest.mark.parametrize(
    "now,since,before",
    [
        (datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc), "2026-09-12", "2026-10-12"),
        # 23:30Z in BST is already the next day in London, so "tomorrow" is two UTC dates ahead
        (datetime(2026, 9, 30, 23, 30, tzinfo=timezone.utc), "2026-09-02", "2026-10-02"),
        (datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc), "2026-09-26", "2026-10-26"),  # clock-change night
        (datetime(2026, 12, 31, 23, 59, tzinfo=timezone.utc), "2026-12-02", "2027-01-01"),
    ],
)
@respx.mock
async def test_default_window_is_tomorrow_minus_30_days_in_london(client, frozen_clock, now, since, before):
    frozen_clock.set(now)
    route = mock_transactions()
    async with connect(client) as mcp_client:
        out = payload(await mcp_client.call_tool("list_transactions", {"account_id": "acc_1"}))
    assert (out["since"], out["before"], out["count"]) == (since, before, 0)
    assert "before" not in route.calls[0].request.url.params


async def test_invalid_ranges_are_rejected_before_any_request(client, monkeypatch):
    cases = [
        ({"since": "2026-10-05", "before": "2026-10-05"}, "must be earlier"),
        ({"since": "2026-10-06", "before": "2026-10-05"}, "must be earlier"),
        ({"since": "2026-10-13"}, "must be earlier"),  # after the default `before`
        ({"since": "2025-01-01", "before": "2026-10-05"}, "longer than 400 days"),
        ({"since": "yesterday"}, "YYYY-MM-DD"),
        ({"before": "05/10/2026"}, "YYYY-MM-DD"),
        ({"since": "tx_00099"}, "cursor"),  # the old cursor-in-`since` form
        ({"since": "2026-10-05", "limit": 0}, "limit"),
        ({"since": "2026-10-05", "limit": 5001}, "limit"),
        ({"since": "2026-10-05", "cursor": "../accounts"}, "cursor"),
        ({"since": "2026-06-01"}, "90 days"),  # Monzo's own history guard
    ]
    with respx.mock(assert_all_called=False) as mock:
        everything = mock.route().mock(return_value=httpx.Response(200, json={"transactions": []}))
        async with connect(client) as mcp_client:
            for args, expected in cases:
                text = error_text(await mcp_client.call_tool("list_transactions", {"account_id": "acc_1", **args}))
                assert "[invalid_request]" in text and expected in text, (args, text)
            text = error_text(await mcp_client.call_tool("list_transactions", {"account_id": "../accounts"}))
            assert "[invalid_request]" in text

            monkeypatch.setenv("MONZO_MAX_RANGE_DAYS", "7")
            args = {"account_id": "acc_1", "since": "2026-10-01", "before": "2026-10-09"}
            text = error_text(await mcp_client.call_tool("list_transactions", args))
            assert "[invalid_request]" in text and "longer than 7 days" in text
            monkeypatch.setenv("MONZO_MAX_RANGE_DAYS", "a week")
            assert "[config_error]" in error_text(await mcp_client.call_tool("list_transactions", args))
        assert everything.call_count == 0


@respx.mock
async def test_max_range_allows_exactly_the_limit(client, monkeypatch, store):
    monkeypatch.setenv("MONZO_MAX_RANGE_DAYS", "7")
    mock_transactions()
    async with connect(client) as mcp_client:
        args = {"account_id": "acc_1", "since": "2026-10-01", "before": "2026-10-08"}
        assert payload(await mcp_client.call_tool("list_transactions", args))["count"] == 0


@respx.mock
async def test_pending_payment_that_settles_later_is_found_by_an_overlapping_refetch(client, frozen_clock):
    """The pending-loss hazard: a payment pending at the first fetch keeps its original date when
    it settles, so only a window reaching back to `earliest_pending_date` ever returns it."""
    card = {**SETTLED, "id": "tx_late", "created": "2026-10-08T12:00:00Z", "settled": ""}
    other = tx("tx_other", "2026-10-07T12:00:00Z")
    bank = {"transactions": [other, card]}

    def monzo(request):
        q = request.url.params
        rows = [t for t in bank["transactions"] if t["created"] >= q["since"]]
        if "before" in q:
            rows = [t for t in rows if t["created"] < q["before"]]
        return httpx.Response(200, json={"transactions": rows})

    respx.get(f"{API}/transactions").mock(side_effect=monzo)
    async with connect(client) as mcp_client:
        # Fetch 1, on the 11th: the card payment is still pending and is left out - but reported.
        first = payload(
            await mcp_client.call_tool(
                "list_transactions", {"account_id": "acc_1", "since": "2026-10-05", "before": "2026-10-10"}
            )
        )
        assert [t["id"] for t in first["transactions"]] == ["tx_other"]
        assert first["pending_excluded"] == 1 and first["earliest_pending_date"] == "2026-10-08"

        # It settles on the 12th. Its `created` time, and so its date, does not change.
        frozen_clock.set(datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc))
        bank["transactions"] = [other, {**card, "settled": "2026-10-12T02:00:00Z"}]

        # A window that merely continues where the first one stopped can never contain it.
        consecutive = payload(
            await mcp_client.call_tool(
                "list_transactions", {"account_id": "acc_1", "since": "2026-10-10", "before": "2026-10-15"}
            )
        )
        assert consecutive["transactions"] == [] and consecutive["pending_excluded"] == 0

        # Reaching back to `earliest_pending_date` returns it, under the same id, now settled.
        overlapping = payload(
            await mcp_client.call_tool(
                "list_transactions",
                {"account_id": "acc_1", "since": first["earliest_pending_date"], "before": "2026-10-15"},
            )
        )
        assert [(t["id"], t["date"], t["pending"]) for t in overlapping["transactions"]] == [
            ("tx_late", "2026-10-08", False)
        ]
        assert overlapping["pending_excluded"] == 0 and overlapping["earliest_pending_date"] is None


@respx.mock
async def test_earliest_pending_date_is_the_oldest_excluded_pending(client):
    mock_transactions(
        {**PENDING, "id": "tx_p2", "created": "2026-10-08T12:00:00Z"},
        {**PENDING, "id": "tx_p1", "created": "2026-10-03T23:30:00Z"},  # 4 October in London
        {**DECLINED, "created": "2026-10-02T12:00:00Z"},  # declined is not pending
        tx("tx_s", "2026-10-02T12:00:00Z"),
    )
    async with connect(client) as mcp_client:
        out = payload(await mcp_client.call_tool("list_transactions", {"account_id": "acc_1", "since": "2026-10-01"}))
    assert out["pending_excluded"] == 2 and out["earliest_pending_date"] == "2026-10-04" and out["count"] == 1


@respx.mock
async def test_truncated_result_continues_with_cursor(client):
    page = [tx(f"tx_{i:05d}", CREATED_TS) for i in range(100)]
    route = respx.get(f"{API}/transactions").mock(return_value=httpx.Response(200, json={"transactions": page}))
    async with connect(client) as mcp_client:
        args = {"account_id": "acc_1", "since": "2026-10-02", "before": "2026-10-10"}
        out = payload(await mcp_client.call_tool("list_transactions", {**args, "limit": 40}))
        assert out["count"] == 40 and out["truncated"] is True and out["next_cursor"] == "tx_00039"
        route.mock(return_value=httpx.Response(200, json={"transactions": page[40:]}))
        rest = payload(await mcp_client.call_tool("list_transactions", {**args, "cursor": out["next_cursor"]}))
    assert rest["count"] == 60 and rest["truncated"] is False
    assert (rest["since"], rest["before"]) == ("2026-10-02", "2026-10-10")
    assert route.calls[-1].request.url.params["since"] == "tx_00039"


@respx.mock
async def test_balance_pots_and_accounts_tools(client):
    respx.get(f"{API}/accounts").mock(
        return_value=httpx.Response(
            200,
            json={
                "accounts": [
                    {"id": "acc_1", "description": "Me", "type": "uk_retail", "currency": "GBP", "closed": False},
                    {"id": "acc_0", "description": "Old", "type": "uk_prepaid", "closed": True},
                ]
            },
        )
    )
    respx.get(f"{API}/balance").mock(
        return_value=httpx.Response(
            200, json={"balance": 5000, "total_balance": 6000, "currency": "GBP", "spend_today": 0}
        )
    )
    respx.get(f"{API}/pots").mock(
        return_value=httpx.Response(
            200,
            json={
                "pots": [
                    {"id": "pot_1", "name": "Savings", "balance": 1000, "currency": "GBP", "deleted": False},
                    {"id": "pot_2", "name": "Gone", "balance": 0, "currency": "GBP", "deleted": True},
                ]
            },
        )
    )
    async with connect(client) as mcp_client:
        accounts = payload(await mcp_client.call_tool("list_accounts", {}))
        assert [(a["id"], a["name"], a["description"]) for a in accounts["accounts"]] == [("acc_1", "Me", "Me")]
        everything = payload(await mcp_client.call_tool("list_accounts", {"include_closed": True}))
        assert [a["name"] for a in everything["accounts"]] == ["Me", "Old"]
        balance = payload(await mcp_client.call_tool("get_balance", {"account_id": "acc_1"}))
        assert balance["balance_minor"] == 5000 and balance["total_balance_minor"] == 6000
        assert balance["currency"] == "GBP"
        pots = payload(await mcp_client.call_tool("list_pots", {"account_id": "acc_1"}))
        assert [p["id"] for p in pots["pots"]] == ["pot_1"]
        text = error_text(await mcp_client.call_tool("get_balance", {"account_id": "acc_nope"}))
        assert "[account_not_found]" in text


# ------------------------------------------------------------------ connection status


def mock_healthy():
    respx.get(f"{API}/ping/whoami").mock(return_value=httpx.Response(200, json={"authenticated": True}))
    return respx.get(f"{API}/accounts").mock(return_value=httpx.Response(200, json={"accounts": []}))


async def status_of(client):
    async with connect(client) as mcp_client:
        return payload(await mcp_client.call_tool("get_connection_status", {}))


@respx.mock
async def test_connection_status_healthy(client):
    mock_healthy()
    status = await status_of(client)
    assert status["connected"] is True and status["warning"] is None and status["error"] is None
    assert status["days_remaining"] == 89  # fixture: approved exactly one day before the frozen clock
    # an ISO 8601 UTC timestamp, not a bare date: approval (10 Oct 12:00Z) + 90 days
    assert status["consent_expires_at"] == "2027-01-08T12:00:00Z"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z", status["consent_expires_at"])
    # extra fields kept for existing callers
    assert status["authenticated"] is True and status["days_until_reauthorization"] == 89
    assert status["reauthorize_by"] == status["consent_expires_at"]


@respx.mock
async def test_connection_status_warns_under_14_days(client, store):
    mock_healthy()
    store.tokens.authorized_at = NOW - 80 * 86400 - 3600
    status = await status_of(client)
    assert status["connected"] is True and status["days_remaining"] == 9
    assert "re-approval is due in about 9 day" in status["warning"]
    store.tokens.authorized_at = NOW - 95 * 86400
    status = await status_of(client)
    assert status["days_remaining"] == -5 and "overdue" in status["warning"]


@respx.mock
async def test_connection_status_unknown_expiry_for_seeded_tokens(client, store):
    mock_healthy()
    store.tokens.authorized_at = None
    status = await status_of(client)
    assert status["connected"] is True
    assert status["consent_expires_at"] is None and status["days_remaining"] is None and status["warning"] is None


@respx.mock
async def test_connection_status_reports_lapsed_consent_without_raising(client, store):
    store.tokens.approved = True
    respx.get(f"{API}/ping/whoami").mock(return_value=httpx.Response(200, json={"authenticated": True}))
    respx.get(f"{API}/accounts").mock(return_value=FORBIDDEN)
    status = await status_of(client)
    assert status["connected"] is False and status["error"] == "consent_expired"
    assert status["warning"].startswith("[consent_expired]")


@respx.mock
async def test_connection_status_reports_dead_refresh_token(client, store):
    store.tokens.expires_at = 0
    respx.post(f"{API}/oauth2/token").mock(
        return_value=httpx.Response(401, json={"code": "unauthorized.bad_refresh_token"})
    )
    status = await status_of(client)
    assert status["connected"] is False and status["warning"].startswith("[token_expired]")


@pytest.mark.parametrize(
    "response,code",
    [
        (httpx.Response(429, headers={"Retry-After": "30"}, json={"code": "too_many_requests"}), "rate_limited"),
        (httpx.Response(500, json={"code": "internal_service"}), "provider_error"),
        (httpx.Response(200, text="<html>maintenance</html>"), "provider_error"),
        (httpx.Response(403, json={"code": "forbidden.insufficient_permissions"}), "sca_not_approved"),
        (httpx.Response(404, json={}), "provider_error"),
    ],
)
@respx.mock
async def test_connection_status_never_raises_for_provider_problems(client, response, code):
    whoami = respx.get(f"{API}/ping/whoami").mock(return_value=response)
    status = await status_of(client)
    assert status["connected"] is False and status["error"] == code
    assert status["warning"].startswith(f"[{code}]")
    assert whoami.call_count == 1  # and a 429 is not retried
    assert status["days_remaining"] == 89  # what is known locally is still reported


@respx.mock
async def test_connection_status_never_raises_for_network_or_unexpected_errors(client):
    respx.get(f"{API}/ping/whoami").mock(side_effect=httpx.ConnectError("boom"))
    status = await status_of(client)
    assert status["connected"] is False and status["warning"].startswith("[provider_error]")
    respx.get(f"{API}/ping/whoami").mock(side_effect=RuntimeError("secret-detail"))
    status = await status_of(client)
    assert status["connected"] is False and status["warning"].startswith("[internal_error]")
    assert "secret-detail" not in json.dumps(status)


async def test_connection_status_when_never_linked_or_misconfigured(config, monkeypatch, tmp_path):
    async with httpx.AsyncClient() as http:
        status = await status_of(MonzoClient(config, MemoryTokenStore(), http=http))
    assert status["connected"] is False and status["warning"].startswith("[not_connected]")
    assert status["consent_expires_at"] is None and status["days_remaining"] is None

    # Configuration problems surface the same way, even though the client cannot be built.
    monkeypatch.setenv("MONZO_TOKEN_FILE", str(tmp_path / "tokens.json"))
    for name, value in [("MONZO_API_BASE", "http://evil.example"), ("MONZO_TOKEN_STORE", "nope_no_module:x")]:
        monkeypatch.setenv(name, value)
        status = await status_of(None)
        assert status["connected"] is False and status["warning"].startswith("[config_error]"), status
        monkeypatch.delenv(name)

    class BrokenStore:
        def load(self):
            raise TokenStoreError("vault sealed")

        def save(self, tokens):
            raise TokenStoreError("vault sealed")

    async with httpx.AsyncClient() as http:
        status = await status_of(MonzoClient(config, BrokenStore(), http=http))
    assert status["connected"] is False and status["warning"].startswith("[token_store_error]")


# ------------------------------------------------------------------ errors


@respx.mock
async def test_get_transaction_tool(client):
    route = respx.get(f"{API}/transactions/tx_3").mock(return_value=httpx.Response(200, json={"transaction": DECLINED}))
    async with connect(client) as mcp_client:
        out = payload(await mcp_client.call_tool("get_transaction", {"transaction_id": "tx_3"}))
        assert out["decline_reason"] == "INSUFFICIENT_FUNDS" and out["merchant"] == "Deli Co"
        assert out["date"] == "2026-10-06" and out["amount_minor"] == 0
        assert route.calls[0].request.url.params["expand[]"] == "merchant"
        # path injection: nothing but a tx_ id ever reaches the URL
        for bad in ("../accounts", "tx_1/../../balance?account_id=acc_1", "tx_١"):
            text = error_text(await mcp_client.call_tool("get_transaction", {"transaction_id": bad}))
            assert "[invalid_request]" in text
        assert route.call_count == 1 and len(respx.calls) == 1


@respx.mock
async def test_error_prefixes_reach_the_mcp_caller(client, store):
    accounts = respx.get(f"{API}/accounts").mock(return_value=FORBIDDEN)
    async with connect(client) as mcp_client:
        # never approved in the app
        text = error_text(await mcp_client.call_tool("list_accounts", {}))
        assert "[sca_not_approved]" in text and "Monzo app" in text
        assert "access-old" not in text
        # the SDK puts its own text first, so the code is matched as a substring
        assert text.startswith("Error executing tool list_accounts: [sca_not_approved]")

        # worked before, now forbidden -> the consent lapsed
        store.tokens.approved = True
        assert "[consent_expired]" in error_text(await mcp_client.call_tool("list_accounts", {}))

        # refresh token dead
        accounts.mock(return_value=httpx.Response(401, json={"code": "unauthorized.bad_access_token"}))
        respx.post(f"{API}/oauth2/token").mock(
            return_value=httpx.Response(401, json={"code": "unauthorized.bad_refresh_token"})
        )
        assert "[token_expired]" in error_text(await mcp_client.call_tool("list_accounts", {}))

        accounts.mock(return_value=httpx.Response(429, headers={"Retry-After": "0"}, json={}))
        before = accounts.call_count
        assert "[rate_limited]" in error_text(await mcp_client.call_tool("list_accounts", {}))
        assert accounts.call_count == before + 1

        accounts.mock(return_value=httpx.Response(500, json={"code": "internal_service"}))
        assert "[provider_error]" in error_text(await mcp_client.call_tool("list_accounts", {}))

        accounts.mock(return_value=httpx.Response(200, text="not json"))
        assert "[provider_error]" in error_text(await mcp_client.call_tool("list_accounts", {}))


async def test_missing_credentials_are_not_connected_not_token_expired(config):
    async with httpx.AsyncClient() as http:
        async with connect(MonzoClient(config, MemoryTokenStore(), http=http)) as mcp_client:
            for name, args in [
                ("list_accounts", {}),
                ("get_balance", {"account_id": "acc_1"}),
                ("list_pots", {"account_id": "acc_1"}),
                ("list_transactions", {"account_id": "acc_1"}),
                ("get_transaction", {"transaction_id": "tx_1"}),
            ]:
                text = error_text(await mcp_client.call_tool(name, args))
                assert "[not_connected]" in text and "[token_expired]" not in text, (name, text)


@respx.mock
async def test_no_tool_leaks_an_unprefixed_exception(client, monkeypatch, caplog):
    respx.get(f"{API}/accounts").mock(side_effect=RuntimeError("at-secret-token-value"))
    respx.get(f"{API}/balance").mock(return_value=httpx.Response(200, json={"balance": "12.50"}))
    respx.get(f"{API}/pots").mock(return_value=httpx.Response(200, json={"pots": [{"id": "pot_1", "balance": {}}]}))
    mock_transactions(tx("tx_bad", "not a timestamp"))
    respx.get(f"{API}/transactions/tx_9").mock(return_value=httpx.Response(200, json={"transaction": "nope"}))
    bracketed = re.compile(r"^Error executing tool \w+: \[[a-z_]+\] ")
    async with connect(client) as mcp_client:
        text = error_text(await mcp_client.call_tool("list_accounts", {}))
        assert "[internal_error]" in text and "RuntimeError" in text and "at-secret-token-value" not in text
        for name, args in [
            ("get_balance", {"account_id": "acc_1"}),
            ("list_pots", {"account_id": "acc_1"}),
            ("list_transactions", {"account_id": "acc_1"}),
            ("get_transaction", {"transaction_id": "tx_9"}),
        ]:
            text = error_text(await mcp_client.call_tool(name, args))
            assert bracketed.match(text) and "[provider_error]" in text, (name, text)

        def explode(*a, **k):
            raise KeyError("boom")

        monkeypatch.setattr(server, "normalize_transaction", explode)
        text = error_text(await mcp_client.call_tool("list_transactions", {"account_id": "acc_1"}))
        assert bracketed.match(text) and "[internal_error]" in text and "KeyError" in text


@respx.mock
async def test_token_store_failure_is_reported_without_failing_the_served_call(config):
    class WriteFails(MemoryTokenStore):
        def save(self, tokens):
            raise TokenStoreError("read-only filesystem")

    store = WriteFails(TokenSet(access_token="old", refresh_token="refresh-old", expires_at=NOW - 60, approved=True))
    respx.post(f"{API}/oauth2/token").mock(
        return_value=httpx.Response(
            200, json={"access_token": "new", "refresh_token": "refresh-new", "expires_in": 3600}
        )
    )
    accounts = respx.get(f"{API}/accounts").mock(return_value=httpx.Response(200, json={"accounts": [{"id": "acc_1"}]}))
    respx.get(f"{API}/ping/whoami").mock(return_value=httpx.Response(200, json={"authenticated": True}))
    async with httpx.AsyncClient() as http:
        async with connect(MonzoClient(config, store, http=http)) as mcp_client:
            out = payload(await mcp_client.call_tool("list_accounts", {}))
            assert out["count"] == 1  # the data call is served with the new, unsaved token
            assert out["token_store_warning"].startswith("[token_store_error]")
            assert "read-only filesystem" in out["token_store_warning"]
            status = payload(await mcp_client.call_tool("get_connection_status", {}))
            assert status["connected"] is True and status["warning"].startswith("[token_store_error]")
            # when the call cannot be served either, the store problem is still named
            accounts.mock(return_value=httpx.Response(500, json={}))
            text = error_text(await mcp_client.call_tool("list_accounts", {}))
            assert "[provider_error]" in text and "[token_store_error]" in text


@respx.mock
async def test_first_successful_call_records_approval(client, store):
    respx.get(f"{API}/accounts").mock(return_value=httpx.Response(200, json={"accounts": []}))
    assert store.tokens.approved is False
    async with connect(client) as mcp_client:
        payload(await mcp_client.call_tool("list_accounts", {}))
        payload(await mcp_client.call_tool("list_accounts", {}))
    assert store.tokens.approved is True and store.saves == 1


# ------------------------------------------------------------------ HTTP transport


def test_http_app_refuses_public_bind_without_auth():
    for host in ("0.0.0.0", "::", "192.168.1.10", "example.test", ""):
        with pytest.raises(ConfigError, match="MONZO_MCP_AUTH_TOKEN"):
            server.build_http_app({}, host=host)
        with pytest.raises(ConfigError):
            server.build_http_app({"MONZO_MCP_AUTH_TOKEN": "   "}, host=host)  # whitespace is not a token
    for host in ("127.0.0.1", "localhost", "::1", "127.0.0.2"):
        assert server.build_http_app({}, host=host) is not None
    assert isinstance(
        server.build_http_app({"MONZO_MCP_AUTH_TOKEN": "x" * 32}, host="0.0.0.0"), server.BearerAuthMiddleware
    )
    # the documented override, for use behind an authenticating gateway
    assert server.build_http_app({"MONZO_MCP_ALLOW_NO_AUTH": "1"}, host="0.0.0.0") is not None
    with pytest.raises(ConfigError):
        server.build_http_app({"MONZO_MCP_ALLOW_NO_AUTH": "yes"}, host="0.0.0.0")
    # a token is enforced on loopback too when one is set
    assert isinstance(
        server.build_http_app({"MONZO_MCP_AUTH_TOKEN": "t"}, host="127.0.0.1"), server.BearerAuthMiddleware
    )


@pytest.mark.parametrize(
    "header,ok",
    [
        ("Bearer s3cret-token", True),
        ("bearer s3cret-token", True),
        ("BEARER s3cret-token", True),
        ("BeArEr   s3cret-token  ", True),
        ("  Bearer s3cret-token", True),
        ("Bearer s3cret-toke", False),
        ("Bearer s3cret-token-and-more", False),
        ("Bearer", False),
        ("Bearer ", False),
        ("Basic s3cret-token", False),
        ("s3cret-token", False),
        ("Bearer S3CRET-TOKEN", False),  # the token itself stays case-sensitive
        (None, False),
    ],
)
async def test_bearer_auth_middleware(header, ok):
    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    # the configured token is stripped of surrounding whitespace
    app = server.BearerAuthMiddleware(inner, "  s3cret-token\n")
    headers = {} if header is None else {"Authorization": header}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as http:
        resp = await http.get("/mcp", headers=headers)
    assert resp.status_code == (204 if ok else 401)
    if not ok:
        assert resp.headers["www-authenticate"] == "Bearer" and "s3cret" not in resp.text


def test_main_rejects_bad_transport_settings(monkeypatch, capsys):
    monkeypatch.setenv("MONZO_MCP_PORT", "eighty")
    with pytest.raises(SystemExit):
        server.main(["--transport", "http"])
    assert "MONZO_MCP_PORT" in capsys.readouterr().err
    monkeypatch.delenv("MONZO_MCP_PORT")
    monkeypatch.delenv("MONZO_MCP_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("MONZO_MCP_ALLOW_NO_AUTH", raising=False)
    with pytest.raises(SystemExit) as exc:
        server.main(["--transport", "http", "--host", "0.0.0.0"])  # refused before anything is bound
    assert "MONZO_MCP_AUTH_TOKEN" in str(exc.value)
    with pytest.raises(SystemExit):
        server.main(["--version"])
    assert __version__ in capsys.readouterr().out
