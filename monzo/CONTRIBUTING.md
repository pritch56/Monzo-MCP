# Contributing

Thanks for helping. This is a small, unofficial project; please keep changes focused.

## Setup

Python 3.11+ and [uv](https://docs.astral.sh/uv/):

```bash
uv sync
```

## Before you open a pull request

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

(`uv run ruff format .` fixes formatting.) The same commands work in PowerShell.

- **Every behavioural change needs a test** that fails without it.
- The tests must stay offline: no sign-ups, no credentials, no calls to the real Monzo API. Mock HTTP
  with `respx` or `httpx.MockTransport`, or use the local fake in `tests/fake_monzo.py`.
- Do not read the wall clock in code or tests. Use `monzo_mcp.clock`; the test suite freezes it.
- The server is read-only. A change that adds any non-GET request other than the OAuth token calls will
  not be accepted.
- Keep the token-store interface (`TokenStore.load` / `save`, the `TokenSet` fields and JSON shape, and
  the `MONZO_TOKEN_STORE` loader) backward compatible. Adding optional fields is fine.
- New environment variables go in the README configuration table and in `.env.example`; a test checks
  that both list every variable the code reads.

## Never share real data

Do not put real tokens, client secrets, account ids, transaction ids, merchant data or balances in
tests, issues, pull requests or logs. Use obviously fake values (`acc_1`, `tx_00001`, `test-secret`).
If you post a log, read it first.

Security problems go through the private channel described in [SECURITY.md](SECURITY.md), not a public
issue.
