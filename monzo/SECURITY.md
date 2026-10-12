# Security policy

## Reporting a vulnerability

Please report security problems **privately**, using GitHub's private vulnerability reporting on this
repository (**Security → Report a vulnerability**). Do not open a public issue, and do not include real
tokens, client secrets, account ids or transaction data in a report: a description and a reproduction
against the local fake (`tests/fake_monzo.py`) is enough.

There is no bounty and no guaranteed response time; this is a volunteer project provided as is.

## Scope

In scope:

- Anything that lets the server do more than read (it must never move money or change an account).
- Leaks of tokens, the client secret or the HTTP bearer token through logs, errors or tool results.
- Bypasses of the HTTP bearer-token check, or of the refusal to bind a non-loopback address without one.
- Request forgery through tool arguments (for example an id that reaches another API path).
- Weaknesses in the OAuth helper (`monzo-mcp-authorize`), such as accepting a callback without the
  right `state`.
- Token-file handling: permissions, atomicity, loss of a rotated refresh token.

Out of scope, because they are documented properties rather than bugs (see the README's Security
section):

- The token file is plaintext on disk.
- The HTTP transport is plain HTTP with a static bearer token; TLS is the deployer's job.
- `MONZO_TOKEN_STORE` runs code named in the environment.
- Text returned by the tools (descriptions, merchant names, notes) is untrusted third-party input and may
  contain prompt-injection or spreadsheet-formula content.
- Problems in Monzo's own API or apps: report those to Monzo.

This project is unofficial and is not affiliated with Monzo Bank.
