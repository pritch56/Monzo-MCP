"""Error types.

Every message is written to be shown directly to whoever called the tool, so it says what
happened and what to do next. Messages never contain token values. `kind` becomes the
bracketed code at the start of the tool error, e.g. `[rate_limited] ...`.
"""

from __future__ import annotations

REAUTH_HINT = (
    "Run `monzo-mcp-authorize` on a machine with a browser, approve the request in the Monzo app, and make "
    "sure the server uses the token store it wrote."
)


class MonzoError(Exception):
    """Base class for all errors raised by this package."""

    kind = "internal_error"

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


class ConfigError(MonzoError):
    kind = "config_error"


class TokenStoreError(MonzoError):
    kind = "token_store_error"


class InvalidRequest(MonzoError):
    """The caller's arguments are wrong; nothing was sent to Monzo."""

    kind = "invalid_request"


class HistoryWindowExceeded(InvalidRequest):
    """Transactions older than 90 days were requested outside the 5 minute post-approval window."""


class AccountNotFound(MonzoError):
    kind = "account_not_found"


class NotConnected(MonzoError):
    """No tokens at all: the account has never been linked (or the store was cleared)."""

    kind = "not_connected"


class ReauthRequired(MonzoError):
    """The access/refresh token is expired, revoked or already used."""

    kind = "token_expired"


class ScaNotApproved(MonzoError):
    """Tokens are valid but access has never been approved in the Monzo app."""

    kind = "sca_not_approved"


class ConsentExpired(MonzoError):
    """Access worked before and is now forbidden: the consent lapsed (or the app was removed
    in Monzo). The tokens still refresh, but grant nothing."""

    kind = "consent_expired"


class RateLimited(MonzoError):
    kind = "rate_limited"

    def __init__(self, message: str, *, retry_after: float | None = None, **kw):
        super().__init__(message, **kw)
        self.retry_after = retry_after


class MonzoAPIError(MonzoError):
    """Any other upstream failure: unexpected status, network error, malformed or non-JSON body."""

    kind = "provider_error"


ProviderError = MonzoAPIError
