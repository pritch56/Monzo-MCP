"""Read-only Monzo API client with automatic, persisted token refresh.

The only non-GET request this module ever makes is POST /oauth2/token (to
refresh the access token). Nothing here can move money or change the account.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

import httpx

from . import USER_AGENT, clock
from .errors import (
    REAUTH_HINT,
    AccountNotFound,
    ConfigError,
    ConsentExpired,
    HistoryWindowExceeded,
    InvalidRequest,
    MonzoAPIError,
    MonzoError,
    NotConnected,
    RateLimited,
    ReauthRequired,
    ScaNotApproved,
    TokenStoreError,
)
from .normalize import parse_rfc3339, to_rfc3339
from .tokens import TokenSet, TokenStore, seed_fingerprint

log = logging.getLogger("monzo_mcp")

DEFAULT_API_BASE = "https://api.monzo.com"
PAGE_SIZE = 100  # Monzo maximum
HISTORY_WINDOW_DAYS = 90
FULL_HISTORY_GRACE_SECONDS = 5 * 60
CONSENT_DAYS = 90
EXPIRY_SKEW_SECONDS = 60

ID_RE = re.compile(r"[A-Za-z0-9_-]+")
TRANSACTION_ID_RE = re.compile(r"tx_[A-Za-z0-9]+")

_UNKNOWN: Any = object()


def is_loopback_host(host: str | None) -> bool:
    if not host:
        return False
    host = host.strip("[]").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_api_base(url: str) -> str:
    """The API base must be https, except for a loopback host (which is how the tests reach a
    local fake). Anything else would send bank tokens somewhere in clear text."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        parts, host = None, None
    if parts is None or parts.scheme not in ("http", "https") or not host:
        raise ConfigError("MONZO_API_BASE must be an http(s) URL. It is a test hook: leave it unset in normal use.")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ConfigError("MONZO_API_BASE must not contain credentials, a query string or a fragment.")
    if parts.scheme != "https" and not is_loopback_host(host):
        raise ConfigError(
            "MONZO_API_BASE must use https:// unless it points at a loopback address. It is a test hook: "
            "leave it unset in normal use."
        )
    return url.rstrip("/")


def validate_account_id(account_id: Any) -> str:
    if not isinstance(account_id, str) or not ID_RE.fullmatch(account_id):
        raise InvalidRequest("`account_id` is not a valid Monzo account id. Use an `id` returned by list_accounts.")
    return account_id


def validate_transaction_id(transaction_id: Any, name: str = "transaction_id") -> str:
    if not isinstance(transaction_id, str) or not TRANSACTION_ID_RE.fullmatch(transaction_id):
        raise InvalidRequest(f"`{name}` must look like `tx_` followed by letters and digits.")
    return transaction_id


@dataclass
class MonzoConfig:
    client_id: str | None = None
    client_secret: str | None = field(default=None, repr=False)
    api_base: str = DEFAULT_API_BASE
    # Optional seed, used only while the token store is empty.
    seed_access_token: str | None = field(default=None, repr=False)
    seed_refresh_token: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.api_base = validate_api_base(self.api_base)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> MonzoConfig:
        env = os.environ if env is None else env

        def get(name: str) -> str | None:
            return (env.get(name) or "").strip() or None

        return cls(
            client_id=get("MONZO_CLIENT_ID"),
            client_secret=get("MONZO_CLIENT_SECRET"),
            api_base=get("MONZO_API_BASE") or DEFAULT_API_BASE,
            seed_access_token=get("MONZO_ACCESS_TOKEN"),
            seed_refresh_token=get("MONZO_REFRESH_TOKEN"),
        )


def _error_details(resp: httpx.Response) -> tuple[str, str]:
    """Return (code, message) from a Monzo error body without echoing anything secret."""
    try:
        body = resp.json()
    except ValueError:
        return "", ""
    if not isinstance(body, dict):
        return "", ""
    code = str(body.get("code") or body.get("error") or "")[:100]
    message = str(body.get("message") or body.get("error_description") or "")
    return code, message[:300]


def _fingerprint(tokens: TokenSet | None) -> tuple[str | None, str | None] | None:
    return None if tokens is None else (tokens.access_token, tokens.refresh_token)


def _has_tokens(tokens: TokenSet | None) -> bool:
    return tokens is not None and bool(tokens.access_token or tokens.refresh_token)


def _content_key(tx: dict[str, Any]) -> str:
    basis = [tx.get("id"), tx.get("created"), tx.get("amount"), tx.get("description")]
    return json.dumps(basis, sort_keys=True, default=str)


class MonzoClient:
    def __init__(self, config: MonzoConfig, store: TokenStore, *, http: httpx.AsyncClient | None = None):
        self.config = config
        self.store = store
        self._http = http or httpx.AsyncClient(timeout=30.0)
        self._owns_http = http is None
        self._lock = asyncio.Lock()
        # The newest token set this process knows about. After a refresh it is used even if
        # the store could not be written, because the previous refresh token is already dead.
        self._tokens: TokenSet | None = None
        self._unsaved = False
        self._store_view: Any = _UNKNOWN  # what the store held when it was last read or written
        self._spent_refresh_tokens: set[str] = set()
        self._known_accounts: set[str] | None = None
        # Message of the last failed store operation, or None once the store is in step again.
        self.store_error: str | None = None

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    # ------------------------------------------------------------------ tokens

    def _store_load(self) -> TokenSet | None:
        try:
            tokens = self.store.load()
        except MonzoError:
            raise
        except Exception as exc:  # a third-party store can raise anything
            raise TokenStoreError(f"The token store could not be read ({type(exc).__name__}).") from None
        if tokens is not None and not isinstance(tokens, TokenSet):
            raise TokenStoreError("The token store returned something that is not a TokenSet.")
        return tokens

    def _persist(self, tokens: TokenSet) -> None:
        """Adopt `tokens` and try to save them. Never raises: a failed save leaves the tokens
        in memory, is recorded in `store_error`, and is retried on the next call."""
        self._tokens = tokens
        try:
            self.store.save(tokens)
        except Exception as exc:
            reason = str(exc) if isinstance(exc, MonzoError) else type(exc).__name__
            self._unsaved = True
            self.store_error = (
                f"The current Monzo tokens could not be saved ({reason}). They are held in memory and the "
                "save is retried on every call. If this process stops before a save succeeds, the stored "
                "refresh token will be stale and you will have to re-authorize. Fix the token store."
            )
            log.error("Saving the Monzo tokens failed (%s); keeping them in memory and retrying.", reason)
        else:
            self._unsaved = False
            self._store_view = _fingerprint(tokens)
            self.store_error = None

    def _current_tokens(self) -> TokenSet:
        """The token set to use now.

        Order of precedence: tokens this process refreshed but has not managed to save yet;
        then whatever the store holds; then, only while the store is empty, the env-var seed.
        """
        if self._unsaved and self._tokens is not None:
            try:
                stored = self._store_load()
            except TokenStoreError:
                stored = _UNKNOWN
            if stored is _UNKNOWN or _fingerprint(stored) in (self._store_view, _fingerprint(self._tokens)):
                self._persist(self._tokens)  # retry the save
                return self._tokens
            # The store was rewritten by something else (a re-link or a reset): that wins.
            log.warning("The token store changed while unsaved tokens were held in memory; using the store.")
            self._unsaved, self.store_error, self._tokens = False, None, None
        else:
            try:
                stored = self._store_load()
            except TokenStoreError as exc:
                if _has_tokens(self._tokens):
                    self.store_error = str(exc)
                    return self._tokens  # type: ignore[return-value]
                raise
            if self.store_error and not self._unsaved:
                self.store_error = None

        self._store_view = _fingerprint(stored)
        if _has_tokens(stored):
            assert stored is not None
            if stored.refresh_token in self._spent_refresh_tokens and _has_tokens(self._tokens):
                # The store handed back a refresh token this process has already used up.
                log.warning("The token store returned an already-used refresh token; re-saving the current one.")
                self._persist(self._tokens)  # type: ignore[arg-type]
                return self._tokens  # type: ignore[return-value]
            self._tokens = stored
            return stored

        cfg = self.config
        if cfg.seed_refresh_token or cfg.seed_access_token:
            seeded = TokenSet(
                access_token=cfg.seed_access_token,
                refresh_token=cfg.seed_refresh_token,
                expires_at=0.0,
                authorized_at=None,
                seed_hash=seed_fingerprint(cfg.seed_refresh_token, cfg.seed_access_token),
            )
            log.info("Token store is empty; initialising it from the environment seed.")
            self._persist(seeded)
            return seeded
        self._tokens = None
        raise NotConnected("No Monzo tokens are stored: this account has not been linked yet. " + REAUTH_HINT)

    def _needs_refresh(self, t: TokenSet) -> bool:
        if not t.access_token:
            return True
        if not t.refresh_token:
            return False  # nothing we can do; let the API tell us if it has expired
        return clock.now() >= (t.expires_at - EXPIRY_SKEW_SECONDS)

    async def _refresh(self, t: TokenSet) -> TokenSet:
        cfg = self.config
        if not t.refresh_token:
            raise ReauthRequired(
                "The Monzo access token has expired and there is no refresh token. "
                "Refresh tokens are only issued to OAuth clients registered as 'Confidential'. " + REAUTH_HINT
            )
        if not (cfg.client_id and cfg.client_secret):
            raise ConfigError("MONZO_CLIENT_ID and MONZO_CLIENT_SECRET must be set to refresh the Monzo token.")
        try:
            resp = await self._http.post(
                f"{cfg.api_base}/oauth2/token",
                data={
                    "grant_type": "refresh_token",
                    "client_id": cfg.client_id,
                    "client_secret": cfg.client_secret,
                    "refresh_token": t.refresh_token,
                },
                headers={"User-Agent": USER_AGENT},
            )
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise MonzoAPIError(f"Network error while refreshing the Monzo token: {type(exc).__name__}") from None
        if resp.status_code == 429:
            raise RateLimited("Monzo rate-limited the token refresh. Wait a minute and retry.", status=429)
        if resp.status_code in (400, 401, 403):
            code, _ = _error_details(resp)
            raise ReauthRequired(
                "Monzo rejected the refresh token (expired, revoked, or already used - refresh tokens are "
                f"single-use and rotate on every refresh) [HTTP {resp.status_code} {code}]. " + REAUTH_HINT,
                status=resp.status_code,
                code=code,
            )
        if resp.status_code >= 400:
            code, _ = _error_details(resp)
            raise MonzoAPIError(
                f"Monzo token refresh failed with HTTP {resp.status_code} {code}. If this persists the refresh "
                "token has probably been revoked (Monzo is reported to answer 500 for revoked tokens). " + REAUTH_HINT,
                status=resp.status_code,
                code=code,
            )
        malformed = (
            f"Monzo answered the token refresh with HTTP {resp.status_code} but the body was not a usable token "
            "response. If the next call reports [token_expired], the refresh token was consumed and you must "
            "re-authorize."
        )
        try:
            body = resp.json()
        except ValueError:
            raise MonzoAPIError(malformed, status=resp.status_code) from None
        access_token = body.get("access_token") if isinstance(body, dict) else None
        if not isinstance(access_token, str) or not access_token:
            raise MonzoAPIError(malformed, status=resp.status_code)
        new_refresh = body.get("refresh_token")
        try:
            expires_in = float(body.get("expires_in") or 0)
        except (TypeError, ValueError):
            expires_in = 0.0
        user_id = body.get("user_id")
        new = replace(
            t,
            access_token=access_token,
            # Monzo rotates the refresh token; keep the old one only if none came back.
            refresh_token=new_refresh if isinstance(new_refresh, str) and new_refresh else t.refresh_token,
            expires_at=clock.now() + expires_in,
            user_id=user_id if isinstance(user_id, str) and user_id else t.user_id,
        )
        if new.refresh_token != t.refresh_token:
            self._spent_refresh_tokens.add(t.refresh_token)
        # From here on the new tokens are used whether or not the save works.
        self._persist(new)
        if not self._unsaved:
            log.info("Monzo access token refreshed and persisted.")
        return new

    async def _access_token(self, *, failed: str | None = None) -> str:
        """Return a usable access token. One refresh at a time per process.

        `failed` is an access token Monzo has just answered 401 to: refresh only if it is still
        the current one, otherwise another call already refreshed and its token is reused.
        """
        async with self._lock:
            t = self._current_tokens()
            if (failed is not None and t.access_token == failed) or self._needs_refresh(t):
                t = await self._refresh(t)
            if not t.access_token:
                raise ReauthRequired("No usable Monzo access token. " + REAUTH_HINT)
            return t.access_token

    # ---------------------------------------------------------------- requests

    async def _get(self, path: str, params: list[tuple[str, str]] | None = None, *, context: str = "") -> Any:
        token = await self._access_token()
        for attempt in (0, 1):
            try:
                resp = await self._http.get(
                    f"{self.config.api_base}{path}",
                    params=params,
                    headers={"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT},
                )
            except (httpx.HTTPError, httpx.InvalidURL) as exc:
                raise MonzoAPIError(f"Network error calling Monzo {path}: {type(exc).__name__}") from None
            if resp.status_code == 401 and attempt == 0:
                # Token expired early or was evicted by another login: refresh once and retry.
                token = await self._access_token(failed=token)
                continue
            break
        # 429 is never retried here: the caller decides when to come back.
        if resp.status_code >= 400:
            self._raise(resp, path, context)
        try:
            body = resp.json()
        except ValueError:
            raise MonzoAPIError(f"Monzo returned a non-JSON response for {path} [HTTP {resp.status_code}].") from None
        if not isinstance(body, dict):
            raise MonzoAPIError(f"Monzo returned an unexpected JSON shape for {path}.")
        await self._mark_approved()
        return body

    @staticmethod
    def _items(body: dict[str, Any], key: str, path: str) -> list[dict[str, Any]]:
        items = body.get(key)
        if items is None:
            return []
        if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
            raise MonzoAPIError(f"Monzo returned an unexpected `{key}` value for {path}.")
        return items

    async def _mark_approved(self) -> None:
        """Record (once) that the API has answered successfully, so a later 403 can be
        reported as lapsed consent rather than a never-approved authorization."""
        if self._tokens is None or self._tokens.approved:
            return
        async with self._lock:
            if self._tokens is not None and not self._tokens.approved:
                self._persist(replace(self._tokens, approved=True))

    @staticmethod
    def _retry_after(resp: httpx.Response) -> float | None:
        try:
            value = float(resp.headers.get("Retry-After", ""))
        except ValueError:
            return None
        return value if 0 <= value < 86400 else None

    def _raise(self, resp: httpx.Response, path: str, context: str) -> None:
        status = resp.status_code
        code, message = _error_details(resp)
        detail = f"[HTTP {status} {code}]".replace(" ]", "]")
        if status == 401:
            raise ReauthRequired(
                f"Monzo rejected the access token even after a refresh {detail}. It has expired, been revoked "
                "(app removed in Monzo > Settings > Manage apps), or been evicted by a newer login using the "
                "same OAuth client. " + REAUTH_HINT,
                status=status,
                code=code,
            )
        if status == 403:
            if "verification_required" in code and context == "transactions":
                raise HistoryWindowExceeded(
                    f"Monzo refused this transaction range {detail}. Only the last {HISTORY_WINDOW_DAYS} days of "
                    "transactions are readable once 5 minutes have passed since access was approved in the Monzo "
                    "app. Use a `since` within the last 90 days. To read older history, re-authorize and fetch "
                    "it within 5 minutes of approving.",
                    status=status,
                    code=code,
                )
            if "insufficient_permissions" in code or "verification_required" in code:
                if self._tokens is not None and self._tokens.approved:
                    raise ConsentExpired(
                        f"Monzo returned 'forbidden' {detail} for access that used to work: the consent has "
                        "lapsed (Monzo asks for re-approval about every 90 days) or the app was removed in "
                        "Monzo > Settings > Manage apps. The user must re-approve. " + REAUTH_HINT,
                        status=status,
                        code=code,
                    )
                raise ScaNotApproved(
                    f"Monzo returned 'forbidden' {detail}: access has not been approved in the Monzo app. Open "
                    "the Monzo app and approve the notification from the authorization, then retry. If this "
                    "connection used to work, the consent has lapsed and you must re-authorize. " + REAUTH_HINT,
                    status=status,
                    code=code,
                )
            raise MonzoAPIError(f"Monzo denied access to {path} {detail}: {message}", status=status, code=code)
        if status == 429:
            retry_after = self._retry_after(resp)
            wait = f" Retry after {retry_after:g}s." if retry_after else " Wait a minute before retrying."
            raise RateLimited(
                f"Monzo rate limit exceeded on {path} {detail}. The request was not retried." + wait,
                retry_after=retry_after,
                status=status,
                code=code,
            )
        if status == 404:
            if context in ("account", "transactions"):
                raise AccountNotFound(
                    f"Monzo does not know this account {detail}. Use an `id` returned by list_accounts.",
                    status=status,
                    code=code,
                )
            raise MonzoAPIError(f"Monzo could not find {path} {detail}. Check the id.", status=status, code=code)
        raise MonzoAPIError(f"Monzo API error on {path} {detail}: {message}", status=status, code=code)

    # --------------------------------------------------------------- endpoints

    async def whoami(self) -> dict[str, Any]:
        return await self._get("/ping/whoami")

    async def list_accounts(self) -> list[dict[str, Any]]:
        accounts = self._items(await self._get("/accounts"), "accounts", "/accounts")
        self._known_accounts = {a["id"] for a in accounts if isinstance(a.get("id"), str)}
        return accounts

    async def require_account(self, account_id: str) -> str:
        """Reject a malformed id outright, and an unknown one once the account list is known.

        The list is only consulted if an earlier call in this process fetched it; it is
        re-fetched once before an id is declared unknown, in case the account is new.
        """
        validate_account_id(account_id)
        if self._known_accounts is not None and account_id not in self._known_accounts:
            await self.list_accounts()
            if account_id not in (self._known_accounts or set()):
                raise AccountNotFound(
                    f"No Monzo account with id `{account_id}`. Use an `id` returned by list_accounts."
                )
        return account_id

    async def get_balance(self, account_id: str) -> dict[str, Any]:
        await self.require_account(account_id)
        return await self._get("/balance", [("account_id", account_id)], context="account")

    async def list_pots(self, account_id: str) -> list[dict[str, Any]]:
        await self.require_account(account_id)
        body = await self._get("/pots", [("current_account_id", account_id)], context="account")
        return self._items(body, "pots", "/pots")

    async def get_transaction(self, transaction_id: str) -> dict[str, Any]:
        # The id becomes part of the URL path, so it is checked before anything is sent.
        validate_transaction_id(transaction_id)
        data = await self._get(f"/transactions/{transaction_id}", [("expand[]", "merchant")], context="transaction")
        tx = data.get("transaction", data)
        if not isinstance(tx, dict):
            raise MonzoAPIError("Monzo returned an unexpected `transaction` value.")
        return tx

    def check_history_window(self, since: str) -> None:
        """Fail fast, with a clear message, instead of sending a request Monzo will refuse."""
        cutoff = clock.utcnow() - timedelta(days=HISTORY_WINDOW_DAYS)
        if parse_rfc3339(since) >= cutoff:
            return
        authorized_at = self._tokens.authorized_at if self._tokens else None
        if authorized_at and clock.now() - authorized_at < FULL_HISTORY_GRACE_SECONDS:
            return  # inside the 5 minute full-history window
        earliest = (cutoff + timedelta(days=1)).astimezone(clock.LONDON).date().isoformat()
        raise HistoryWindowExceeded(
            f"`since` is more than {HISTORY_WINDOW_DAYS} days ago. Monzo only allows the last "
            f"{HISTORY_WINDOW_DAYS} days of transactions once 5 minutes have passed since access was approved in "
            f"the app. Use a `since` on or after {earliest}, or re-authorize and fetch older history within 5 "
            "minutes of approving."
        )

    async def list_transactions(
        self,
        account_id: str,
        since: str,
        before: str | None = None,
        max_items: int = 1000,
        cursor: str | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Return (transactions oldest-first, truncated).

        Pages forward using the id of the last transaction as the next `since`
        cursor, PAGE_SIZE at a time, until a short page or max_items is reached.
        `cursor` (a transaction id) resumes a truncated listing after that transaction.

        An exact repeat of a transaction already seen is dropped. Two transactions that share
        an id but differ in created time, amount or description are both kept: the later one
        gets a new id built from its content and a sequence number, and its original id is
        kept under `_provider_id`.
        """
        await self.require_account(account_id)
        if cursor is not None:
            validate_transaction_id(cursor, "cursor")
        async with self._lock:
            # Load the tokens (so the 5 minute grace period is known) before the range check.
            self._current_tokens()
        self.check_history_window(since)

        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        ids: dict[str, int] = {}
        position = cursor or since
        while len(items) < max_items:
            params = [
                ("account_id", account_id),
                ("expand[]", "merchant"),
                ("limit", str(PAGE_SIZE)),
                ("since", position),
            ]
            if before:
                params.append(("before", before))
            body = await self._get("/transactions", params, context="transactions")
            page = self._items(body, "transactions", "/transactions")
            fresh = 0
            for tx in page:
                key = _content_key(tx)
                if key in seen:
                    continue
                seen.add(key)
                fresh += 1
                items.append(self._with_unique_id(tx, key, ids))
            if len(page) < PAGE_SIZE or not fresh:
                return items[:max_items], len(items) > max_items
            last_id = page[-1].get("id")
            if not isinstance(last_id, str) or not TRANSACTION_ID_RE.fullmatch(last_id):
                raise MonzoAPIError("Monzo returned a transaction without a usable id, so paging cannot continue.")
            position = last_id
        return items[:max_items], True

    @staticmethod
    def _with_unique_id(tx: dict[str, Any], key: str, ids: dict[str, int]) -> dict[str, Any]:
        provider_id = tx.get("id")
        digest = hashlib.sha256(key.encode()).hexdigest()[:10]
        if not isinstance(provider_id, str) or not provider_id:
            ids["<missing>"] = ids.get("<missing>", 0) + 1
            log.warning("Monzo returned a transaction without an id; assigned a content-based one.")
            return {**tx, "id": f"monzo_noid_{digest}_{ids['<missing>']}", "_provider_id": None}
        count = ids.get(provider_id, 0)
        ids[provider_id] = count + 1
        if count == 0:
            return tx
        log.warning(
            "Monzo returned two different transactions with the id %s; keeping both under distinct ids.", provider_id
        )
        return {**tx, "id": f"{provider_id}_dup{count}_{digest}", "_provider_id": provider_id}

    # ------------------------------------------------------------------ status

    def consent_status(self) -> dict[str, Any]:
        """Estimate when the next in-app re-approval falls due (Monzo has no API for this)."""
        tokens = self._tokens
        if tokens is None:
            try:
                tokens = self._store_load()
            except MonzoError:
                tokens = None
        authorized_at = tokens.authorized_at if tokens else None
        if not isinstance(authorized_at, (int, float)) or not authorized_at:
            return {"authorized_at": None, "reauthorize_by": None, "days_until_reauthorization": None}
        due = authorized_at + CONSENT_DAYS * 86400
        return {
            "authorized_at": to_rfc3339(datetime.fromtimestamp(authorized_at, timezone.utc)),
            "reauthorize_by": to_rfc3339(datetime.fromtimestamp(due, timezone.utc)),
            "days_until_reauthorization": int((due - clock.now()) // 86400),
        }
