from datetime import date

import pytest

from monzo_mcp.errors import MonzoAPIError
from monzo_mcp.normalize import (
    day_start,
    normalize_account,
    normalize_balance,
    normalize_pot,
    normalize_transaction,
    parse_day,
    to_rfc3339,
)

KEYS = [
    "id",
    "source",
    "account_id",
    "date",
    "amount_minor",
    "currency",
    "description",
    "merchant",
    "category",
    "pending",
    "notes",
]

CARD = {
    "id": "tx_00008zIcpb1TB4yeIFXMzx",
    "account_id": "acc_1",
    "created": "2026-10-03T13:22:45.123Z",
    "amount": -510,
    "currency": "GBP",
    "description": "THE DE BEAUVOIR DELI C LONDON  GBR",
    "merchant": {"id": "merch_1", "name": "The De Beauvoir Deli Co.", "category": "eating_out"},
    "category": "eating_out",
    "settled": "2026-10-04T01:00:00Z",
    "notes": "Salmon sandwich",
    "metadata": {},
    "is_load": False,
}


def test_settled_card_payment_exact_shape():
    out = normalize_transaction(CARD)
    assert list(out) == KEYS
    assert out == {
        "id": "tx_00008zIcpb1TB4yeIFXMzx",
        "source": "monzo",
        "account_id": "acc_1",
        "date": "2026-10-03",
        "amount_minor": -510,
        "currency": "GBP",
        "description": "THE DE BEAUVOIR DELI C LONDON  GBR",
        "merchant": "The De Beauvoir Deli Co.",
        "category": "eating_out",
        "pending": False,
        "notes": "note=Salmon sandwich",
    }
    assert isinstance(out["amount_minor"], int)


def test_notes_is_null_when_there_is_nothing_to_say():
    assert normalize_transaction({**CARD, "notes": ""})["notes"] is None
    assert normalize_transaction({**CARD, "notes": "   "})["notes"] is None
    assert normalize_transaction({k: v for k, v in CARD.items() if k != "notes"})["notes"] is None


def test_pending_when_not_settled():
    assert normalize_transaction({**CARD, "settled": ""})["pending"] is True
    assert normalize_transaction({k: v for k, v in CARD.items() if k != "settled"})["pending"] is True


def test_declined_row_moves_no_money_and_says_so_in_notes():
    out = normalize_transaction({**CARD, "settled": "", "decline_reason": "INSUFFICIENT_FUNDS", "scheme": "mastercard"})
    assert out["pending"] is False
    assert out["amount_minor"] == 0  # cannot be mistaken for spending
    assert out["notes"] == (
        "declined: INSUFFICIENT_FUNDS; declined_amount_minor=-510; scheme=mastercard; note=Salmon sandwich"
    )
    assert list(out) == KEYS


def test_scheme_and_foreign_amount_go_into_notes_in_a_stable_order():
    foreign = {**CARD, "notes": "", "scheme": "mastercard", "local_amount": -600, "local_currency": "EUR"}
    assert normalize_transaction(foreign)["notes"] == "scheme=mastercard; local_amount_minor=-600; local_currency=EUR"
    # a domestic payment repeats the same amount and currency: nothing to add
    domestic = {**CARD, "notes": "", "scheme": "mastercard", "local_amount": -510, "local_currency": "GBP"}
    assert normalize_transaction(domestic)["notes"] == "scheme=mastercard"


def test_pot_transfers_and_top_ups_are_marked():
    pot = {
        **CARD,
        "notes": "",
        "merchant": None,
        "scheme": "uk_retail_pot",
        "metadata": {"pot_id": "pot_0000A1"},
        "description": "pot_0000A1",
    }
    assert normalize_transaction(pot)["notes"] == "scheme=uk_retail_pot; pot_transfer=true; pot_id=pot_0000A1"
    top_up = {**CARD, "notes": "", "amount": 10000, "is_load": True, "scheme": "payport_faster_payments"}
    assert normalize_transaction(top_up)["notes"] == "scheme=payport_faster_payments; top_up=true"


def test_structured_note_values_cannot_inject_separators_or_control_characters():
    out = normalize_transaction({**CARD, "notes": "", "scheme": "evil; pot_transfer=true\n\x1b[31m"})
    assert out["notes"] == "scheme=evil, pot_transfer-true[31m"


def test_incoming_transfer_without_merchant():
    out = normalize_transaction(
        {
            "id": "tx_2",
            "created": "2026-10-01T09:00:00Z",
            "amount": 250000,
            "currency": "GBP",
            "description": "ACME LTD SALARY",
            "merchant": None,
            "category": "income",
            "settled": "2026-10-01T09:00:00Z",
        },
        account_id="acc_9",
    )
    assert out["amount_minor"] == 250000 and out["merchant"] is None
    assert out["account_id"] == "acc_9" and out["notes"] is None and out["date"] == "2026-10-01"


@pytest.mark.parametrize("merchant", ["merch_00008zIcpbAKe8shBxXUtl", None, "", 7, [], {"id": "merch_1"}])
def test_merchant_that_is_not_an_expanded_object_becomes_null(merchant):
    assert normalize_transaction({**CARD, "merchant": merchant})["merchant"] is None


@pytest.mark.parametrize("amount", ["abc", 1.5, True, [1]])
def test_malformed_amount_is_a_provider_error(amount):
    with pytest.raises(MonzoAPIError) as exc:
        normalize_transaction({**CARD, "amount": amount})
    assert exc.value.kind == "provider_error"


def test_malformed_created_is_a_provider_error():
    with pytest.raises(MonzoAPIError, match="created"):
        normalize_transaction({**CARD, "created": "yesterday"})


@pytest.mark.parametrize(
    "given,expected",
    [
        ("2026-10-03", "2026-10-02T23:00:00Z"),  # date-only = UK local midnight (BST)
        ("2026-12-01", "2026-12-01T00:00:00Z"),  # GMT
        ("2026-10-03T12:00:00", "2026-10-03T12:00:00Z"),
        ("2026-10-03T12:00:00+01:00", "2026-10-03T11:00:00Z"),
        ("2026-10-03T12:00:00.5Z", "2026-10-03T12:00:00.500Z"),
        (None, None),
    ],
)
def test_to_rfc3339(given, expected):
    assert to_rfc3339(given) == expected


@pytest.mark.parametrize(
    "created,expected",
    [
        ("2026-07-01T22:59:59Z", "2026-07-01"),
        ("2026-07-01T23:00:00Z", "2026-07-02"),  # BST: already tomorrow in the UK
        ("2026-09-30T23:30:00Z", "2026-10-01"),  # BST month boundary
        ("2026-12-01T23:30:00Z", "2026-12-01"),  # GMT
        ("2026-12-31T23:59:59.999Z", "2026-12-31"),
        # Autumn clock change, Sunday 25 October 2026: 02:00 BST becomes 01:00 GMT.
        ("2026-10-24T22:59:59Z", "2026-10-24"),
        ("2026-10-24T23:00:00Z", "2026-10-25"),  # 00:00 BST
        ("2026-10-25T00:30:00Z", "2026-10-25"),  # 01:30 BST (first time round)
        ("2026-10-25T01:30:00Z", "2026-10-25"),  # 01:30 GMT (second time round)
        ("2026-10-25T23:30:00Z", "2026-10-25"),  # GMT again: no longer tomorrow
        ("2026-10-26T00:00:00Z", "2026-10-26"),
        ("2026-09-30T23:30:00+00:00", "2026-10-01"),
        ("2026-10-01T00:30:00+01:00", "2026-10-01"),
    ],
)
def test_date_is_uk_local_calendar_date(created, expected):
    assert normalize_transaction({**CARD, "created": created})["date"] == expected


def test_day_boundaries_across_the_autumn_clock_change():
    # The London day of 25 October 2026 is 25 hours long.
    assert day_start(date(2026, 10, 25)) == "2026-10-24T23:00:00Z"
    assert day_start(date(2026, 10, 26)) == "2026-10-26T00:00:00Z"
    # ... and the spring one (29 March 2026) is 23 hours long.
    assert day_start(date(2026, 3, 29)) == "2026-03-29T00:00:00Z"
    assert day_start(date(2026, 3, 30)) == "2026-03-29T23:00:00Z"


def test_parse_day_accepts_dates_and_reduces_datetimes_to_the_london_date():
    assert parse_day("2026-10-03") == date(2026, 10, 3)
    assert parse_day(" 2026-10-03 ") == date(2026, 10, 3)
    assert parse_day("2026-09-30T23:30:00Z") == date(2026, 10, 1)
    assert parse_day("2026-12-01T23:30:00Z") == date(2026, 12, 1)
    for bad in ("last tuesday", "2026-13-01", "03/10/2026", "tx_00099"):
        with pytest.raises(ValueError):
            parse_day(bad)


def test_to_rfc3339_rejects_garbage():
    with pytest.raises(ValueError, match="ISO 8601"):
        to_rfc3339("last tuesday")


def test_account_has_id_and_name():
    acc = normalize_account(
        {"id": "acc_1", "description": "Joe Bloggs", "type": "uk_retail", "currency": "GBP", "created": "2020-01-01"}
    )
    assert acc["id"] == "acc_1" and acc["name"] == "Joe Bloggs" and acc["description"] == "Joe Bloggs"
    assert acc["closed"] is False
    # fields this server cannot verify against the live API are optional
    bare = normalize_account({"id": "acc_2"})
    assert bare["name"] == "acc_2" and bare["description"] is None and bare["type"] is None and bare["closed"] is False
    assert normalize_account({"id": "acc_3", "type": "uk_retail_joint"})["name"] == "uk_retail_joint"


def test_balance_and_pot():
    assert normalize_balance(
        {"balance": 5000, "total_balance": 6000, "currency": "GBP", "spend_today": -250}, "acc_1"
    ) == {
        "source": "monzo",
        "account_id": "acc_1",
        "currency": "GBP",
        "balance_minor": 5000,
        "total_balance_minor": 6000,
        "spend_today_minor": -250,
    }
    pot = normalize_pot(
        {
            "id": "pot_1",
            "name": "Savings",
            "balance": 133700,
            "currency": "GBP",
            "deleted": False,
            "created": "2017-11-09T12:30:53.695Z",
        },
        "acc_1",
    )
    assert pot["balance_minor"] == 133700 and pot["updated"] is None
