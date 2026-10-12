# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## 0.1.0 - unreleased

First public version. Tested only against mocks and a local fake of the Monzo API.

### Added

- Read-only tools: `get_connection_status`, `list_accounts`, `get_balance`, `list_pots`,
  `list_transactions`, `get_transaction`.
- stdio and streamable HTTP transports; `python -m monzo_mcp`.
- `monzo-mcp-authorize` helper with `--paste` and `--reset`.
- `list_transactions` reports `pending_excluded`, `earliest_pending_date` and `pending_supported`, echoes
  the effective `since`/`before`, and enforces a maximum range (`MONZO_MAX_RANGE_DAYS`).
- Accounts carry `name`.
- `notes` carries the payment scheme, foreign amount and currency, pot-transfer and top-up markers.
- Error codes `[not_connected]`, `[invalid_request]`, `[account_not_found]`, `[provider_error]` and
  `[internal_error]`.

### Changed

- `since` and `before` are Europe/London dates; the default window is tomorrow minus 30 days.
- A truncated listing continues with `cursor` / `next_cursor` (previously a transaction id in `since`).
- `[api_error]` is now `[provider_error]`; `[history_window_exceeded]` is now `[invalid_request]`; missing
  credentials are `[not_connected]` rather than `[token_expired]`.
- `notes` is `null` when empty; `consent_expires_at` is an ISO 8601 UTC timestamp.
- Declined payments, when included, have `amount_minor` 0 and `notes` starting `declined: <reason>`.
- A token in the store always wins over the environment seed, including a changed seed.
- 429 responses are never retried.
- The generic `PORT` variable is no longer read; use `MONZO_MCP_PORT`.

### Fixed

- A refresh whose result could not be saved no longer loses the new refresh token: it is kept in
  memory, used, and saved on a later call.
- Concurrent 401 responses cause a single token refresh.
- `get_transaction` validates the id before building the URL.
- Malformed or non-JSON responses are reported as `[provider_error]`.
- `get_connection_status` no longer fails on rate limits, provider errors or configuration problems.
- Two transactions sharing an id but differing in content are both kept.
- The OAuth helper ignores callbacks with a wrong or missing `state` instead of aborting.

### Security

- Token files are restricted to the current user on Windows as well as POSIX.
- `MONZO_API_BASE` must be `https://` unless it is a loopback address.
- The HTTP bearer scheme is matched case-insensitively; tokens are stripped and compared in constant time.
