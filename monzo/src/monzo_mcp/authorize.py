"""One-off helper: run the Monzo OAuth flow on your own machine and save the tokens.

    monzo-mcp-authorize            link (or re-link) the account; overwrites the token store
    monzo-mcp-authorize --paste    same, but paste the redirected URL instead of listening on localhost
    monzo-mcp-authorize --reset    delete the stored tokens and exit

Requires MONZO_CLIENT_ID and MONZO_CLIENT_SECRET in the environment (you are
prompted, without echo, if they are missing). Opens the Monzo login page,
listens on a localhost redirect URL for the authorization code, exchanges it
for tokens, waits for you to approve access in the Monzo app, then writes the
tokens to the configured token store. Token values are never printed.
"""

from __future__ import annotations

import argparse
import getpass
import hmac
import html
import os
import secrets
import sys
import time
import unicodedata
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from . import USER_AGENT, clock
from .client import DEFAULT_API_BASE, is_loopback_host, validate_api_base
from .errors import MonzoError
from .tokens import TokenSet, load_token_store

AUTH_URL = "https://auth.monzo.com/"
DEFAULT_REDIRECT = "http://localhost:8765/callback"


class AuthorizeError(Exception):
    """The flow cannot continue; the message is safe to print."""


def clean(value: object, limit: int = 300) -> str:
    """Text that is safe to echo to a terminal: control characters (including escape
    sequences and newlines) removed, and capped in length."""
    text = "".join(ch for ch in str(value) if unicodedata.category(ch)[0] != "C")
    return text[:limit]


def build_auth_url(client_id: str, redirect_uri: str, state: str) -> str:
    return (
        AUTH_URL
        + "?"
        + urlencode({"client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code", "state": state})
    )


def _same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def extract_code(pasted: str, state: str) -> str:
    """Get the authorization code out of what the user pasted.

    A URL (or anything with a query string) must carry the `state` this run generated;
    without that check a link crafted by someone else could plant their authorization.
    A bare code is accepted as it is.
    """
    value = pasted.strip()
    if not value:
        raise AuthorizeError("Nothing was pasted.")
    looks_like_url = (
        "://" in value or "?" in value or "code=" in value or "state=" in value or value.startswith(("/", "localhost"))
    )
    if not looks_like_url:
        if any(ch.isspace() or unicodedata.category(ch)[0] == "C" for ch in value):
            raise AuthorizeError("That does not look like an authorization code.")
        return value
    query = parse_qs(urlsplit(value).query if ("?" in value or "://" in value) else value)
    supplied = query.get("state", [""])[0]
    if not supplied or not _same(supplied, state):
        raise AuthorizeError(
            "The pasted URL does not carry the `state` value of this run, so it was not accepted. "
            "Start again and paste the URL your browser was redirected to."
        )
    if query.get("error"):
        raise AuthorizeError(f"Monzo reported an error: {clean(query['error'][0], 100)}")
    code = query.get("code", [""])[0]
    if not code:
        raise AuthorizeError("The pasted URL has no `code` parameter.")
    return code


def wait_for_code(redirect_uri: str, state: str, timeout: float = 600.0) -> str:
    """Listen on the localhost redirect URL until a callback with the right `state` arrives.

    A request with a missing or wrong `state` is answered with 400 and ignored: the helper
    keeps listening until the timeout, so a stray or hostile request cannot abort the flow.
    """
    parsed = urlsplit(redirect_uri)
    if parsed.scheme != "http" or not is_loopback_host(parsed.hostname):
        raise AuthorizeError("The redirect URI must be an http://localhost (or 127.0.0.1) URL for this helper.")
    result: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        timeout = 5  # a stalled connection must not block the loop

        def _reply(self, status: int, message: str) -> None:
            body = f"<html><body><h3>{html.escape(message)}</h3></body></html>".encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            url = urlsplit(self.path)
            if url.path != (parsed.path or "/"):
                self._reply(404, "Not found.")
                return
            query = parse_qs(url.query)
            supplied = query.get("state", [""])[0]
            if result or not supplied or not _same(supplied, state):
                self._reply(400, "This request does not belong to the authorization in progress. Ignored.")
                return
            if query.get("error"):
                result["error"] = f"Monzo reported an error: {clean(query['error'][0], 100)}"
                self._reply(200, "Monzo reported an error. Return to the terminal.")
            elif query.get("code", [""])[0]:
                result["code"] = query["code"][0]
                self._reply(200, "Done. Now open the Monzo app and approve access, then return to the terminal.")
            else:
                self._reply(400, "No authorization code in this request. Ignored.")

        def log_message(self, *args: object) -> None:  # keep the code out of the console
            pass

    try:
        server = HTTPServer((parsed.hostname, parsed.port or 80), Handler)
    except OSError as exc:
        raise AuthorizeError(f"Cannot listen on {clean(redirect_uri)}: {clean(exc)}") from None
    server.timeout = 0.5
    deadline = time.monotonic() + timeout
    try:
        while not result and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if "error" in result:
        raise AuthorizeError(result["error"])
    if "code" not in result:
        raise AuthorizeError("Timed out waiting for the Monzo redirect.")
    return result["code"]


def exchange_code(
    http: httpx.Client, api_base: str, client_id: str, client_secret: str, redirect_uri: str, code: str
) -> dict[str, Any]:
    try:
        resp = http.post(
            f"{api_base}/oauth2/token",
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "code": code,
            },
        )
    except httpx.HTTPError as exc:
        raise AuthorizeError(f"Network error during the token exchange: {type(exc).__name__}") from None
    if resp.status_code >= 400:
        raise AuthorizeError(
            f"Token exchange failed (HTTP {resp.status_code}). Check the client id/secret and redirect URI."
        )
    try:
        body = resp.json()
    except ValueError:
        body = None
    if not isinstance(body, dict) or not isinstance(body.get("access_token"), str) or not body["access_token"]:
        raise AuthorizeError("Token exchange failed: Monzo's response did not contain an access token.")
    return body


def wait_for_app_approval(
    http: httpx.Client, api_base: str, access_token: str, timeout: float = 300.0, interval: float = 3.0
) -> bool:
    """Poll /accounts until Monzo stops answering 403 (i.e. the user approved in the app)."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            resp = http.get(f"{api_base}/accounts", headers={"Authorization": f"Bearer {access_token}"})
            if resp.status_code == 200:
                return True
            if resp.status_code not in (401, 403):
                print(f"  (Monzo answered HTTP {resp.status_code}; still waiting)")
        except httpx.HTTPError as exc:
            print(f"  (network error: {type(exc).__name__}; still waiting)")
        if time.monotonic() + interval >= deadline:
            return False
        time.sleep(interval)


def reset(store: Any) -> None:
    clear = getattr(store, "clear", None)
    if not callable(clear):
        raise AuthorizeError(
            f"The configured token store ({type(store).__name__}) has no clear() method. Delete its contents by hand."
        )
    clear()


def _seconds(value: object) -> float:
    try:
        return max(float(value or 0), 0.0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def run(args: argparse.Namespace) -> int:
    store = load_token_store()
    where = clean(getattr(store, "path", type(store).__name__))
    if args.reset:
        reset(store)
        print(f"Stored Monzo tokens deleted ({where}). Run this helper again without --reset to link the account.")
        return 0

    api_base = validate_api_base((os.environ.get("MONZO_API_BASE") or "").strip() or DEFAULT_API_BASE)
    client_id = (os.environ.get("MONZO_CLIENT_ID") or "").strip() or input("Monzo client id: ").strip()
    client_secret = (os.environ.get("MONZO_CLIENT_SECRET") or "").strip() or getpass.getpass(
        "Monzo client secret (hidden): "
    ).strip()
    if not client_id or not client_secret:
        raise AuthorizeError("A client id and client secret are required.")

    state = secrets.token_urlsafe(24)
    url = build_auth_url(client_id, args.redirect_uri, state)
    print("1. Log in to Monzo in your browser (Monzo emails you a login link - open it on THIS computer).")
    if args.no_browser or args.paste or not webbrowser.open(url):
        print(f"   Open this URL:\n   {clean(url, 2000)}")
    if args.paste:
        print("   After logging in, your browser is sent to the redirect URL (the page may fail to load).")
        code = extract_code(input("   Paste that full URL here: "), state)
    else:
        code = wait_for_code(args.redirect_uri, state, timeout=args.timeout)

    with httpx.Client(timeout=30.0, headers={"User-Agent": USER_AGENT}) as http:
        body = exchange_code(http, api_base, client_id, client_secret, args.redirect_uri, code)
        refresh_token = body.get("refresh_token")
        has_refresh = isinstance(refresh_token, str) and bool(refresh_token)
        user_id = body.get("user_id")
        tokens = TokenSet(
            access_token=body["access_token"],
            refresh_token=refresh_token if has_refresh else None,
            expires_at=clock.now() + _seconds(body.get("expires_in")),
            authorized_at=clock.now(),
            user_id=user_id if isinstance(user_id, str) else None,
        )
        store.save(tokens)  # save now so nothing is lost if the next step is interrupted
        print("2. Tokens received. Open the Monzo app and tap the notification to approve access...")
        approved = wait_for_app_approval(
            http, api_base, body["access_token"], timeout=args.approval_timeout, interval=args.poll_interval
        )
        if approved:
            tokens.authorized_at = clock.now()
            tokens.approved = True
            store.save(tokens)

    print(f"3. Tokens saved to: {where}")
    if not has_refresh:
        print(
            "WARNING: Monzo did not issue a refresh token. Your OAuth client is not 'Confidential', so the access\n"
            "token will stop working in a few hours. Create a Confidential client and run this again."
        )
    if not approved:
        print("WARNING: Access was not approved in the Monzo app in time. Approve it, or run this again.")
        return 1
    print("Approved. You can now start the server. Re-run this helper when the server asks for re-authorization.")
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="monzo-mcp-authorize",
        description="Link a Monzo account: run the OAuth flow locally and save the tokens (overwrites the store).",
    )
    parser.add_argument(
        "--redirect-uri",
        default=(os.environ.get("MONZO_REDIRECT_URI") or "").strip() or DEFAULT_REDIRECT,
        help=f"Must match a redirect URL registered on the OAuth client. Env: MONZO_REDIRECT_URI. "
        f"Default: {DEFAULT_REDIRECT}",
    )
    parser.add_argument("--no-browser", action="store_true", help="Print the URL instead of opening a browser")
    parser.add_argument(
        "--paste", action="store_true", help="Paste the redirected URL instead of listening on the redirect port"
    )
    parser.add_argument("--reset", action="store_true", help="Delete the stored tokens and exit")
    parser.add_argument("--timeout", type=float, default=600.0, help="Seconds to wait for the browser redirect")
    parser.add_argument("--approval-timeout", type=float, default=300.0, help="Seconds to wait for in-app approval")
    parser.add_argument("--poll-interval", type=float, default=3.0, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        code = run(args)
    except (AuthorizeError, MonzoError) as exc:
        sys.exit(f"monzo-mcp-authorize: {clean(exc, 600)}")
    except (KeyboardInterrupt, EOFError):
        sys.exit("monzo-mcp-authorize: cancelled.")
    if code:
        sys.exit(code)


if __name__ == "__main__":
    main()
