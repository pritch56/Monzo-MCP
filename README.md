# monzo-mcp

[![CI](https://github.com/pritch56/Monzo-MCP/actions/workflows/ci.yml/badge.svg)](https://github.com/pritch56/Monzo-MCP/actions/workflows/ci.yml)
[![Licence: MIT](https://img.shields.io/badge/licence-MIT-blue.svg)](LICENSE)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)
![Status: alpha](https://img.shields.io/badge/status-alpha-orange.svg)

**Ask Claude (or any MCP client) about your own Monzo account: balances, pots and transactions, read-only.**

An **unofficial**, **read-only** [Model Context Protocol](https://modelcontextprotocol.io) server for your
own Monzo account. It exposes accounts, balances, pots and transactions as normalized JSON over **stdio**
or **streamable HTTP**. It cannot move money, edit pots, post feed items or register webhooks: the only
non-GET request it ever makes is the OAuth token refresh.

> **Status: alpha.** The author runs it against their own live Monzo account for a weekly read-only
> sync, and it has had no wider use. The automated tests use only mocks and a local fake of the API, and
> the items under [Unverified assumptions](#unverified-assumptions) have not been confirmed one by one.
> Check amounts against your Monzo app before relying on it.

**Before you use it:** this project is not affiliated with Monzo, it is for **your own accounts only**, and
you need your own OAuth client from Monzo's developer portal. Monzo's developer API is not meant for public
applications. The full terms are in the [Disclaimer](#disclaimer).

**Contents:** [What you can ask](#what-you-can-ask) · [Quick start](#quick-start) ·
[MCP client configuration](#mcp-client-configuration) · [Tools](#tools) · [Disclaimer](#disclaimer) ·
[Unverified assumptions](#unverified-assumptions) · [Limitations](#limitations) · [Security](#security) ·
[Error codes](#error-codes) · [Configuration](#configuration) · [Troubleshooting](#troubleshooting)

## What you can ask

Once the server is connected to your MCP client, questions like these are answered from your real data:

| Ask | Tools it uses |
|---|---|
| "What's my Monzo balance, and how much is in each pot?" | `list_accounts`, `get_balance`, `list_pots` |
| "List everything I spent on eating out in the last 30 days, biggest first." | `list_transactions` (each row has `category`, `merchant` and `amount_minor`) |
| "Which card payments are still pending?" | `list_transactions` with `include_pending=true` |
| "Were any payments declined this week, and why?" | `list_transactions` with `include_declined=true`, `get_transaction` |
| "Is my Monzo connection still working, and when do I have to re-approve it?" | `get_connection_status` |

The server only returns the rows; the sorting, totalling and explaining is done by the model, so check
anything that matters. Monzo serves only the last 90 days of transactions (see [Limitations](#limitations)),
and nothing here can move money or change your account.

## Quick start

The short version, once you have an OAuth client from Monzo (step 1 below):

```bash
git clone https://github.com/pritch56/Monzo-MCP.git && cd Monzo-MCP && uv sync
export MONZO_CLIENT_ID="oauth2client_..." MONZO_CLIENT_SECRET="..."
uv run monzo-mcp-authorize   # log in by e-mail link, then approve in the Monzo app
uv run monzo-mcp             # stdio server; or add it to your MCP client (below)
```

The commands take about a minute; registering the OAuth client and approving in the app are the slow
parts. Each step in full, with PowerShell equivalents:

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/). It is not published to PyPI: the PyPI
name `monzo-mcp` belongs to an unrelated project, so install this one **from source**:

```bash
git clone https://github.com/pritch56/Monzo-MCP.git
cd Monzo-MCP
uv sync
```

```powershell
git clone https://github.com/pritch56/Monzo-MCP.git
cd Monzo-MCP
uv sync
```

**1. Register an OAuth client.** At <https://developers.monzo.com> open **Clients → New OAuth Client**:

- Redirect URL: `http://localhost:8765/callback`
- Confidentiality: **Confidential** (required: other clients get no refresh token)

Note the client id (`oauth2client_...`) and the client secret.

**2. Put the credentials in your environment**, never in a file you commit:

```bash
export MONZO_CLIENT_ID="oauth2client_..."
export MONZO_CLIENT_SECRET="..."
```

```powershell
$env:MONZO_CLIENT_ID = "oauth2client_..."
$env:MONZO_CLIENT_SECRET = "..."
```

**3. Link the account** on a computer with a browser:

```bash
uv run monzo-mcp-authorize
```

```powershell
uv run monzo-mcp-authorize
```

Monzo e-mails you a login link; open it **on the same computer**. You are redirected to
`localhost:8765`, then you must **open the Monzo app and approve the request**. The helper waits for that
approval and writes the tokens to the token store. It never prints token values. Options:

| Option | Effect |
|---|---|
| `--no-browser` | Print the login URL instead of opening a browser |
| `--paste` | Do not listen on the redirect port; paste the URL you were redirected to instead. A pasted URL must carry this run's `state` |
| `--redirect-uri URL` | Must match a redirect URL registered on the client (also `MONZO_REDIRECT_URI`) |
| `--reset` | Delete the stored tokens and exit |
| `--timeout`, `--approval-timeout` | Seconds to wait for the redirect / for in-app approval |

**4. Run the server** (stdio):

```bash
uv run monzo-mcp
```

```powershell
uv run monzo-mcp
```

`uv run python -m monzo_mcp` is equivalent.

### MCP client configuration

Keep real values in your environment; the config only refers to them.

Claude Code (`.mcp.json`):

```json
{
  "mcpServers": {
    "monzo": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/Monzo-MCP", "monzo-mcp"],
      "env": {
        "MONZO_CLIENT_ID": "${MONZO_CLIENT_ID}",
        "MONZO_CLIENT_SECRET": "${MONZO_CLIENT_SECRET}"
      }
    }
  }
}
```

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "monzo": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/Monzo-MCP", "monzo-mcp"],
      "env": {
        "MONZO_CLIENT_ID": "${MONZO_CLIENT_ID}",
        "MONZO_CLIENT_SECRET": "${MONZO_CLIENT_SECRET}"
      }
    }
  }
}
```

If your client does not expand `${VAR}`, leave the `env` block out and set the two variables in your user
environment so the server inherits them. Do not paste secrets into the config file. On Windows use a path
such as `C:/path/to/Monzo-MCP`.

## Tools

All tools are read-only. Amounts are integers in minor units (pence); negative means money out.

| Tool | Returns |
|---|---|
| `get_connection_status()` | `{connected, consent_expires_at, days_remaining, warning, error, ...}` |
| `list_accounts(include_closed=false)` | `{accounts: [{id, name, source, description, type, currency, closed, created}], count}` |
| `get_balance(account_id)` | `{source, account_id, currency, balance_minor, total_balance_minor, spend_today_minor}` |
| `list_pots(account_id, include_deleted=false)` | `{pots: [{id, source, account_id, name, balance_minor, currency, deleted, created, updated}], count}` |
| `list_transactions(account_id, since?, before?, include_pending=false, include_declined=false, limit=1000, cursor?)` | `{transactions, count, since, before, pending_supported, pending_excluded, earliest_pending_date, declined_excluded, truncated, next_cursor}` |
| `get_transaction(transaction_id)` | one normalized transaction plus `decline_reason` |

### `get_connection_status`

Never fails for provider, authorization, configuration or rate-limit problems: it returns
`connected: false` with the bracketed error in `warning`.

- `connected` — bool.
- `consent_expires_at` — ISO 8601 UTC timestamp ending in `Z` (approval time + 90 days, an estimate), or
  `null` when the approval time is unknown (tokens seeded from an environment variable).
- `days_remaining` — int or `null`; negative when the estimate has passed.
- `warning` — string or `null`. Set when fewer than 14 days remain, or when something needs attention.
- `error` — the bare error code when not connected, else `null`.

### `list_accounts` and `get_balance`

Every account has `id` and `name`. Monzo has no separate account name, so `name` is its `description`
(falling back to the account type, then the id). `balance_minor` excludes pots; `total_balance_minor`
includes them.

### `list_transactions`

- **Window:** `since <= date < before`, both `YYYY-MM-DD` in Europe/London. Defaults: `before` = tomorrow
  (so today is included), `since` = `before` minus 30 days. The effective values are echoed back as
  `since` and `before`. An ISO 8601 datetime is also accepted and is reduced to its London calendar date.
- `since >= before`, a malformed date, or a range longer than `MONZO_MAX_RANGE_DAYS` (default 400) is
  rejected with `[invalid_request]` before anything is sent. So is a `since` more than 90 days ago,
  outside the 5 minutes after approval (see [Limitations](#limitations)).
- Results are oldest first; the server pages through Monzo 100 at a time. `limit` (1–5000) caps how many
  are fetched. If `truncated` is true, call again with the same `since`/`before` and `cursor` set to
  `next_cursor`. The pending and declined counts cover only the part that was fetched.
- `pending_supported` is always `true` for Monzo.

Each transaction has exactly this shape:

```json
{
  "id": "tx_00008zIcpb1TB4yeIFXMzx",
  "source": "monzo",
  "account_id": "acc_00009237aqC8c5umZmrRdh",
  "date": "2026-10-03",
  "amount_minor": -510,
  "currency": "GBP",
  "description": "THE DE BEAUVOIR DELI C LONDON GBR",
  "merchant": "The De Beauvoir Deli Co.",
  "category": "eating_out",
  "pending": false,
  "notes": "scheme=mastercard; note=Salmon sandwich"
}
```

- `date` is the Europe/London calendar date of Monzo's `created` time, converted from the timestamp (a
  payment at `2026-09-30T23:30Z` is dated `2026-10-01`).
- `merchant` is the merchant name or `null`.
- `pending` is true while a card payment is authorised but not settled.
- `notes` is `null` when empty, never `""`. Otherwise it holds, in this fixed order and separated by
  `"; "`, whichever of these apply:

  | Part | Meaning |
  |---|---|
  | `declined: <reason>` | Declined payment (always first) |
  | `declined_amount_minor=<n>` | The amount that was attempted |
  | `scheme=<scheme>` | Monzo's payment scheme, e.g. `mastercard`, `uk_retail_pot` |
  | `local_amount_minor=<n>`, `local_currency=<ccy>` | Foreign transactions only: the amount in the local currency |
  | `pot_transfer=true`, `pot_id=<id>` | A transfer to or from one of your pots, not spending |
  | `top_up=true` | A top-up |
  | `note=<text>` | Your own note from the Monzo app. Always last; runs to the end of the string |

- **Declined payments** are left out unless `include_declined=true`. When included, `amount_minor` is `0`
  (no money moved) and `notes` starts with `declined: <reason>`, so they cannot be summed as spending.
- **Pot transfers and top-ups** are ordinary rows with a real amount. Check `notes` for
  `pot_transfer=true` / `top_up=true` if you are totalling spending.
- If Monzo ever returns two different transactions with the same `id`, both are kept: the later one gets
  an id of the form `<id>_dup<n>_<hash>` and a warning is logged.

### `get_transaction`

`transaction_id` must look like `tx_` followed by letters and digits; anything else is rejected with
`[invalid_request]` before a request is made.

## Always re-fetch an overlapping window

**If you call `list_transactions` repeatedly, every call must overlap the previous one.**

Pending card payments are left out by default, because their amounts can change or disappear on
settlement. A payment keeps its original `date` when it settles, which can be days later. So a caller
that fetches consecutive, non-overlapping windows (1st–8th, then 8th–15th) **never sees a payment that
was still pending at the first fetch**: by the time it settles, its date is behind the next window.

Do this instead:

1. Each fetch reaches back at least to the `earliest_pending_date` returned by the previous fetch.
   If you do not track that, re-fetch the last **14 days** every time.
2. De-duplicate on `id` (insert or update by `id`; never append blindly).

Every result reports `pending_excluded` (how many pending payments were left out) and
`earliest_pending_date` (the date of the oldest one, or `null`), so the overlap can be exact. With
`include_pending=true` they are returned (`pending: true`) and both fields are `0` / `null`; the same
re-fetch rule applies, because a pending row can still change.

## Disclaimer

- This project is **not affiliated with, endorsed by or supported by Monzo Bank**. "Monzo" is a trademark
  of its owner and is used here only to say what the software talks to.
- It is provided **as is, without warranty of any kind**. It is not financial advice.
- You must register **your own** API client at <https://developers.monzo.com> and comply with Monzo's
  terms. Monzo's documentation says: "The Monzo Developer API is not suitable for building public
  applications." and "You may only connect to your own account or those of a small set of users you
  explicitly allow."
- Use it for **your own accounts only**. Do not host it for other people.

## Unverified assumptions

These are built in and exercised by the tests, but have not been confirmed against the real service:

- **The `403` body codes.** `forbidden.insufficient_permissions` and `forbidden.verification_required`
  are taken from Monzo's documentation and community reports. Using them to tell `[consent_expired]` from
  `[sca_not_approved]` (see [Error codes](#error-codes)) is an inference.
- **The "90-day re-approval".** Inferred from Monzo's wording about how long data can be synchronised;
  Monzo documents no exact lifetime and no way to read the expiry. `consent_expires_at` is an estimate.
- **Whether a pending transaction keeps its `id` when it settles.** The
  [overlapping-window rule](#always-re-fetch-an-overlapping-window) depends on it for de-duplication.
- **`expand[]=merchant` on the list endpoint.** Monzo documents it only for the single-transaction
  endpoint. If the list returns a bare merchant id or `null`, `merchant` is `null` rather than an error.
- **`/accounts` fields `closed` and `type`.** Treated as optional; a missing `closed` means open.
- **The sign of `spend_today`.** Passed through unchanged as `spend_today_minor`.
- **What Monzo answers for an unknown account id.** A `404` is reported as `[account_not_found]`; any
  other answer falls through to the generic mapping.
- **The pot-transfer markers** (`scheme` = `uk_retail_pot`, `metadata.pot_id`) and the top-up flag
  (`is_load`), used only to annotate `notes`.
- Access-token lifetime, refresh-token rotation and the 5-minute full-history window are as documented by
  Monzo at the time of writing (October 2026) and may change.

## Limitations

- **Consent lasts about 90 days.** After that, token refresh may still succeed while every call returns
  `403`. `get_connection_status` warns from 14 days before the estimated date. Re-run
  `monzo-mcp-authorize` to renew.
- **90 days of history.** Monzo serves the full history only for 5 minutes after you approve access in
  the app; after that, only the last 90 days.
- **Rate limits are unpublished.** A `429` is reported as `[rate_limited]` and never retried.
- **Rotating, single-use refresh tokens.** See [Token storage](#token-storage). One process per OAuth
  client.
- **A confidential client is required**; a non-confidential client's access token simply expires after a
  few hours.
- **Pending payments** change or vanish; see
  [Always re-fetch an overlapping window](#always-re-fetch-an-overlapping-window).
- Dates are Europe/London. Monzo's `created` time, not the settlement time, decides a transaction's date.

## Security

- **Everything textual is untrusted third-party input.** Transaction descriptions, merchant names, notes,
  account and pot names are written by merchants, payers and other parties. They can contain
  prompt-injection text aimed at the model reading them, or spreadsheet formulas. Do not pair this server
  with tools that can run shell commands or send e-mail without your confirmation. Anything that writes
  these values to a spreadsheet must neutralise cells starting with `=`, `+`, `-` or `@`.
- **The token file is plaintext.** Anyone who can read it can read your bank data until the consent
  lapses. It is written atomically and restricted to your user: mode `0600` on Linux and macOS; on
  Windows, inherited permissions are removed and only your account is granted access (`icacls`; a failure
  is logged as a warning, so check the log on first run). Prefer an encrypted disk or a secret manager.
- **`MONZO_TOKEN_STORE=module:factory` executes code** named in the environment. Anyone who can set that
  variable can run code as the server.
- **The seed variables are secrets**, and so is the client secret. Keep them out of shell history, config
  files and logs. `monzo-mcp-authorize` never prints token values.
- **The HTTP transport is plain HTTP with one static bearer token.** Put it behind TLS (a reverse proxy
  or tunnel), set `MONZO_MCP_AUTH_TOKEN` **even on loopback** (any local process, and possibly a web page in
  your browser, can otherwise reach it), and rotate the token if it leaks.
- **Proxy environment variables are honoured** (`HTTPS_PROXY` and friends). A proxy configured there
  sees the connection metadata; a TLS-intercepting one sees your tokens.
- **`MONZO_API_BASE` is a test hook.** It is refused unless it is `https://` or a loopback address, but
  pointing it anywhere else still sends your tokens there.
- The authorize helper ignores (HTTP 400) any callback without the right `state` and keeps waiting, so a
  stray or hostile local request can neither inject a code nor abort the flow.

To report a vulnerability see [SECURITY.md](SECURITY.md).

## Error codes

A failing tool returns an MCP tool error whose text contains a bracketed code. The SDK puts its own
prefix first (`Error executing tool <name>: [rate_limited] ...`), so **match the bracketed code as a
substring**, not at the start.

| Code | Meaning | What to do |
|---|---|---|
| `[not_connected]` | No tokens stored: the account has never been linked | Run `monzo-mcp-authorize` |
| `[token_expired]` | The access/refresh token is expired, revoked, evicted or already used | Run `monzo-mcp-authorize` |
| `[consent_expired]` | Access that used to work is now forbidden: the consent lapsed or the app was removed in Monzo | Run `monzo-mcp-authorize` and approve in the app |
| `[sca_not_approved]` | Tokens were issued but access was never approved in the Monzo app | Approve the notification in the app |
| `[rate_limited]` | Monzo answered 429. The request is **never retried** by the server | Wait, then call again |
| `[invalid_request]` | Bad arguments (dates, range, ids, limit, or history older than 90 days). Nothing was sent | Fix the arguments |
| `[account_not_found]` | The account id is not one of yours | Use an `id` from `list_accounts` |
| `[provider_error]` | Any other upstream failure: unexpected status, network error, malformed or non-JSON response | Retry later; see the message |
| `[config_error]` | Missing or invalid configuration | Fix the environment variable named in the message |
| `[token_store_error]` | The token store could not be read or written | Fix the store; see below |
| `[internal_error]` | A bug. Only the exception type is returned; details are in the server log | Report it |

Notes:

- Monzo answers a lapsed consent and a never-approved authorization with the same `403`. The server tells
  them apart by remembering (an `approved` flag in the token store) whether any call has ever succeeded:
  forbidden after a success is `[consent_expired]`, forbidden before one is `[sca_not_approved]`.
- A **failed token save does not fail a call that can otherwise be served.** After a refresh the new
  tokens are used from memory even if they could not be saved, and the save is retried on every call. The
  result then carries `token_store_warning` (and `get_connection_status` puts it in `warning`). If the
  call fails for another reason, the store problem is appended to that error. Fix the store promptly: if
  the process stops first, the stored refresh token is stale and you must re-authorize.
- Arguments of the wrong JSON type are rejected by the MCP SDK itself, before the tool runs, with its own
  validation message and no bracketed code.

## Configuration

Every environment variable the server and the helper read. [.env.example](.env.example) lists the same
set.

| Variable | Default | Purpose |
|---|---|---|
| `MONZO_CLIENT_ID` | — (required) | OAuth client id |
| `MONZO_CLIENT_SECRET` | — (required) | OAuth client secret |
| `MONZO_REDIRECT_URI` | `http://localhost:8765/callback` | Redirect URL used by `monzo-mcp-authorize`; must be registered on the client and be an `http://` loopback URL |
| `MONZO_TOKEN_FILE` | `~/.monzo-mcp/tokens.json` | Path of the token file |
| `MONZO_TOKEN_STORE` | — | `package.module:factory` for a custom token store; replaces the file store. **Imports and runs that code** |
| `MONZO_REFRESH_TOKEN` | — | Seed refresh token, used only while the token store is empty |
| `MONZO_ACCESS_TOKEN` | — | Seed access token, used only while the token store is empty |
| `MONZO_MAX_RANGE_DAYS` | `400` | Longest `since`..`before` range `list_transactions` accepts |
| `MONZO_MCP_TRANSPORT` | `stdio` | `stdio` or `http` (also `--transport`) |
| `MONZO_MCP_HOST` | `127.0.0.1` | HTTP bind address (also `--host`) |
| `MONZO_MCP_PORT` | `8000` | HTTP port (also `--port`) |
| `MONZO_MCP_AUTH_TOKEN` | — | Bearer token HTTP clients must send. Required for a non-loopback bind |
| `MONZO_MCP_ALLOW_NO_AUTH` | — | `1` allows a non-loopback bind without a token (only behind an authenticating gateway) |
| `MONZO_MCP_PATH` | `/mcp` | HTTP path of the MCP endpoint |
| `MONZO_MCP_ALLOWED_HOSTS` | — | Comma-separated public host names accepted in the `Host` header (behind a proxy or tunnel) |
| `MONZO_MCP_LOG_LEVEL` | `INFO` | Log level. Logs go to stderr and never contain tokens or secrets |
| `MONZO_API_BASE` | `https://api.monzo.com` | **Test hook; leave unset.** Must be `https://` unless the host is loopback, otherwise `[config_error]` |

The HTTP client also honours the standard proxy and certificate variables (`HTTP_PROXY`, `HTTPS_PROXY`,
`ALL_PROXY`, `NO_PROXY`, `SSL_CERT_FILE`, `SSL_CERT_DIR`). On Windows, `USERNAME` and `USERDOMAIN` are
read to restrict the token file to your account.

## Token storage

Monzo issues a **new refresh token on every refresh and the old one stops working**, so the server must be
able to write tokens back somewhere durable. A read-only secret is not enough.

**Precedence — one rule:** a token in the store always wins. The seed variables (`MONZO_REFRESH_TOKEN`,
`MONZO_ACCESS_TOKEN`) are used **only while the store is empty**; changing them later has no effect.

**To re-link or start again**, do one of:

- run `monzo-mcp-authorize` — it overwrites the store with the new authorization; or
- run `monzo-mcp-authorize --reset` (or delete the token file) to empty the store, after which a seed is
  read again.

Options for where tokens live:

1. **A file** (default). `~/.monzo-mcp/tokens.json`, or `MONZO_TOKEN_FILE`. On a server, point it at a
   persistent volume, never an ephemeral filesystem.
2. **Seed, then a persistent store.** Link on your own machine, then set `MONZO_REFRESH_TOKEN` to the
   `refresh_token` value from your token file. On first start the server copies it into the (empty) store;
   from then on the store is authoritative and the variable is stale.
3. **Your own store.** Implement two methods and set `MONZO_TOKEN_STORE=my_module:make_store`:

   ```python
   from monzo_mcp.tokens import TokenSet


   class MySecretStore:
       def load(self) -> TokenSet | None:
           raw = read_secret("monzo-tokens")  # your secret manager
           return TokenSet.from_json(raw) if raw else None

       def save(self, tokens: TokenSet) -> None:
           write_secret("monzo-tokens", tokens.to_json())  # MUST raise if the write fails

       def clear(self) -> None:  # optional; used by `monzo-mcp-authorize --reset`
           delete_secret("monzo-tokens")


   def make_store():
       return MySecretStore()
   ```

Only one refresh runs at a time per process, and a `401` triggers a refresh only if no other call has
already obtained a newer token. Running **two processes on the same OAuth client** (for example a laptop
and a server) is not supported: Monzo allows one live access token per client and user, so they evict
each other. Register a second client instead.

## Streamable HTTP

```bash
export MONZO_MCP_AUTH_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
uv run monzo-mcp --transport http --host 127.0.0.1 --port 8000
```

```powershell
$env:MONZO_MCP_AUTH_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
uv run monzo-mcp --transport http --host 127.0.0.1 --port 8000
```

The endpoint is `http://<host>:<port>/mcp` (stateless, JSON responses).

- With `MONZO_MCP_AUTH_TOKEN` set, every request must carry `Authorization: Bearer <token>`. The scheme is
  matched case-insensitively, surrounding whitespace is ignored, and the token is compared in constant
  time.
- The server **refuses to bind a non-loopback address without a token**, unless
  `MONZO_MCP_ALLOW_NO_AUTH=1` says an authenticating gateway sits in front of it.
- Behind a reverse proxy or tunnel, set `MONZO_MCP_ALLOWED_HOSTS=your.domain` so the SDK's DNS-rebinding
  protection accepts the forwarded `Host` header.
- This is **plain HTTP**. Put it behind TLS. See [Security](#security).

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `[not_connected]` | Nothing in the token store. Run `monzo-mcp-authorize`; check `MONZO_TOKEN_FILE` is the same for the helper and the server |
| `[sca_not_approved]` right after linking | You have not approved the request in the Monzo app yet |
| `[consent_expired]`, or `[token_expired]` after weeks of working | Re-run `monzo-mcp-authorize` |
| `[token_expired]` soon after linking | Another process or login is using the same OAuth client, or the client is not Confidential (the helper warns when no refresh token is issued) |
| Changed `MONZO_REFRESH_TOKEN` but nothing happened | By design: the store wins. Use `monzo-mcp-authorize --reset`, then restart |
| `[invalid_request]` mentioning 90 days | `since` is too old; use a later date, or fetch within 5 minutes of approving |
| A payment is missing from your records | Non-overlapping windows; see [above](#always-re-fetch-an-overlapping-window) |
| `[config_error]` about `MONZO_API_BASE` | Unset it |
| HTTP server exits with "Refusing to serve bank data" | Set `MONZO_MCP_AUTH_TOKEN`, or bind to `127.0.0.1` |
| `401` from the HTTP endpoint | Missing or wrong `Authorization: Bearer` header |
| `421` / "Invalid Host header" behind a proxy | Set `MONZO_MCP_ALLOWED_HOSTS` |
| `token_store_warning` in results | The token file or store is not writable; fix it before the process restarts |
| "Cannot listen on ..." from the helper | The redirect port is in use; register another loopback URL and pass `--redirect-uri`, or use `--paste` |

## Development

```bash
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

```powershell
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

The tests need no credentials and no network beyond loopback: unit tests mock HTTP, and the end-to-end
tests start the real server (`python -m monzo_mcp`) on both transports against a local fake of the API.
See [CONTRIBUTING.md](CONTRIBUTING.md) and [CHANGELOG.md](CHANGELOG.md).

```
src/monzo_mcp/
  server.py      MCP tools, stdio + streamable HTTP, bearer-auth middleware
  client.py      read-only Monzo client: refresh, pagination, error mapping
  tokens.py      TokenSet, TokenStore protocol, FileTokenStore, store loader
  normalize.py   Monzo JSON -> normalized shapes
  errors.py      error types and codes
  authorize.py   OAuth helper (localhost redirect or pasted URL)
  clock.py       the single time source
tests/           pytest suite and a local fake of the Monzo API
```

## Licence

MIT. See [LICENSE](LICENSE). Provided as-is, without warranty of any kind.
