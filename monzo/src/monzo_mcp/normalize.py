"""Convert Monzo API objects into this server's normalized shapes."""

from __future__ import annotations

import unicodedata
from datetime import date, datetime, time, timezone
from typing import Any

from .clock import LONDON
from .errors import MonzoAPIError

SOURCE = "monzo"
POT_SCHEME = "uk_retail_pot"


def to_rfc3339(value: str | datetime | date | None) -> str | None:
    """Accept an ISO 8601 date or datetime and return RFC 3339 UTC ("...Z").

    Date-only values mean midnight UK local time (Europe/London), matching the
    `date` field of normalized transactions. Naive datetimes are assumed to be UTC.
    """
    if value is None or value == "":
        return None
    if isinstance(value, str):
        text = value.strip()
        try:
            if len(text) == 10:
                parsed: datetime | date = date.fromisoformat(text)
            else:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
        except ValueError as exc:
            raise ValueError(
                f"'{value}' is not an ISO 8601 date/datetime (e.g. 2026-10-03 or 2026-10-03T00:00:00Z)."
            ) from exc
    else:
        parsed = value
    if not isinstance(parsed, datetime):
        parsed = datetime.combine(parsed, time.min, tzinfo=LONDON)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    spec = "milliseconds" if parsed.microsecond else "seconds"
    return parsed.isoformat(timespec=spec).replace("+00:00", "Z")


def parse_rfc3339(value: str) -> datetime:
    return datetime.fromisoformat(to_rfc3339(value).replace("Z", "+00:00"))  # type: ignore[union-attr]


def london_date(created: str | None) -> str | None:
    """Calendar date (YYYY-MM-DD) of a Monzo timestamp in UK local time."""
    if not created:
        return None
    return parse_rfc3339(created).astimezone(LONDON).date().isoformat()


def parse_day(value: str) -> date:
    """A `since`/`before` argument as a Europe/London calendar date.

    Accepts YYYY-MM-DD, or an ISO 8601 datetime (UTC if it has no offset), which is reduced
    to the London calendar date it falls on. Raises ValueError otherwise.
    """
    text = value.strip()
    if len(text) == 10:
        try:
            return date.fromisoformat(text)
        except ValueError:
            pass
    return parse_rfc3339(text).astimezone(LONDON).date()


def day_start(day: date) -> str:
    """RFC 3339 UTC instant at which `day` begins in Europe/London."""
    return to_rfc3339(day)  # type: ignore[return-value]


def _minor(value: Any, field: str) -> int:
    """An amount in minor units. Monzo documents these as integers; anything else is a malformed response."""
    if value is None or value == "":
        return 0
    if isinstance(value, bool):
        raise MonzoAPIError(f"Monzo returned a non-numeric `{field}`.")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            pass
    raise MonzoAPIError(f"Monzo returned a non-integer `{field}`.")


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _token(value: Any) -> str:
    """A value safe to embed in the `key=value; ` part of notes: no control characters, `;` or `=`."""
    text = "".join(ch for ch in str(value) if unicodedata.category(ch)[0] != "C")
    return text.replace(";", ",").replace("=", "-").strip()[:80]


def _merchant_name(merchant: Any) -> str | None:
    # With expand[]=merchant this is an object; otherwise a bare merchant id string, or null.
    if isinstance(merchant, dict):
        return _text(merchant.get("name")) or None
    return None


def is_declined(tx: dict[str, Any]) -> bool:
    return bool(tx.get("decline_reason"))


def is_pending(tx: dict[str, Any]) -> bool:
    # `settled` is empty while a card payment is authorised but not yet presented.
    return not tx.get("settled") and not is_declined(tx)


def build_notes(tx: dict[str, Any]) -> str | None:
    """Extra facts that have no field of their own, in a stable order:

        declined: <reason>; declined_amount_minor=<n>; scheme=<s>; local_amount_minor=<n>;
        local_currency=<ccy>; pot_transfer=true; pot_id=<id>; top_up=true; note=<Monzo's own notes>

    Parts that do not apply are left out. `note=` is always last and runs to the end of the
    string, so the user's own text may contain anything. Returns None when there is nothing.
    """
    parts: list[str] = []
    if is_declined(tx):
        parts.append(f"declined: {_token(tx.get('decline_reason'))}")
        parts.append(f"declined_amount_minor={_minor(tx.get('amount'), 'amount')}")
    scheme = _text(tx.get("scheme"))
    if scheme:
        parts.append(f"scheme={_token(scheme)}")
    local_currency = _text(tx.get("local_currency"))
    if local_currency and local_currency != _text(tx.get("currency")) and tx.get("local_amount") is not None:
        parts.append(f"local_amount_minor={_minor(tx.get('local_amount'), 'local_amount')}")
        parts.append(f"local_currency={_token(local_currency)}")
    metadata = tx.get("metadata") if isinstance(tx.get("metadata"), dict) else {}
    pot_id = _text(metadata.get("pot_id"))
    if scheme == POT_SCHEME or pot_id:
        parts.append("pot_transfer=true")
        if pot_id:
            parts.append(f"pot_id={_token(pot_id)}")
    if tx.get("is_load") is True:
        parts.append("top_up=true")
    note = _text(tx.get("notes"))
    if note:
        parts.append(f"note={note}")
    return "; ".join(parts) or None


def normalize_transaction(tx: dict[str, Any], account_id: str | None = None) -> dict[str, Any]:
    """Shape: {id, source, account_id, date, amount_minor, currency, description, merchant,
    category, pending, notes}.

    `date` is the Europe/London calendar date of Monzo's `created` time. `amount_minor` is
    signed (negative = money out) and is 0 for a declined payment, because no money moved;
    the attempted amount is kept in `notes`. `notes` is null when there is nothing to say.
    """
    if not isinstance(tx, dict):
        raise MonzoAPIError("Monzo returned a transaction that is not a JSON object.")
    try:
        day = london_date(tx.get("created") if isinstance(tx.get("created"), str) else None)
    except ValueError:
        raise MonzoAPIError("Monzo returned a transaction with an unreadable `created` timestamp.") from None
    declined = is_declined(tx)
    return {
        "id": tx.get("id"),
        "source": SOURCE,
        "account_id": tx.get("account_id") or account_id,
        "date": day,
        "amount_minor": 0 if declined else _minor(tx.get("amount"), "amount"),
        "currency": tx.get("currency"),
        "description": _text(tx.get("description")),
        "merchant": _merchant_name(tx.get("merchant")),
        "category": _text(tx.get("category")) or None,
        "pending": is_pending(tx),
        "notes": build_notes(tx),
    }


def _timestamp(value: Any) -> str | None:
    try:
        return to_rfc3339(value) if isinstance(value, str) else None
    except ValueError:
        return None


def normalize_account(acc: dict[str, Any]) -> dict[str, Any]:
    description = _text(acc.get("description")) or None
    return {
        "id": acc.get("id"),
        "source": SOURCE,
        # Monzo has no separate account name; `description` is the closest thing it returns.
        "name": description or _text(acc.get("type")) or acc.get("id"),
        "description": description,
        "type": acc.get("type"),
        "currency": acc.get("currency"),
        "closed": bool(acc.get("closed", False)),
        "created": _timestamp(acc.get("created")),
    }


def normalize_balance(bal: dict[str, Any], account_id: str) -> dict[str, Any]:
    return {
        "source": SOURCE,
        "account_id": account_id,
        "currency": bal.get("currency"),
        # Spendable balance of the account itself, excluding pots.
        "balance_minor": _minor(bal.get("balance"), "balance"),
        # Account balance plus all pots.
        "total_balance_minor": _minor(bal.get("total_balance"), "total_balance"),
        # Passed through with Monzo's own sign.
        "spend_today_minor": _minor(bal.get("spend_today"), "spend_today"),
    }


def normalize_pot(pot: dict[str, Any], account_id: str) -> dict[str, Any]:
    return {
        "id": pot.get("id"),
        "source": SOURCE,
        "account_id": account_id,
        "name": pot.get("name"),
        "balance_minor": _minor(pot.get("balance"), "balance"),
        "currency": pot.get("currency"),
        "deleted": bool(pot.get("deleted", False)),
        "created": _timestamp(pot.get("created")),
        "updated": _timestamp(pot.get("updated")),
    }
