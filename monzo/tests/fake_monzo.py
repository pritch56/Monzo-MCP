"""A tiny local stand-in for api.monzo.com, used by the transport and authorize tests.

It enforces single-use rotating refresh tokens and one valid access token at a
time, like the real service, so the tests prove the server persists rotation.
Transactions are built from an explicit start day, never from the wall clock.
"""

from __future__ import annotations

import socket
import threading
import time
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime
from zoneinfo import ZoneInfo

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

ACCOUNT_ID = "acc_00009237aqC8c5umZmrRdh"
LONDON = ZoneInfo("Europe/London")
PER_DAY = 30


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_transactions(first_day: date, n: int) -> list[dict]:
    """`n` settled card payments, PER_DAY a day starting at London noon on `first_day` (noon is
    never near a date boundary, in GMT or BST), one second apart. The last one is pending."""
    txs = []
    for i in range(n):
        day = first_day + timedelta(days=i // PER_DAY)
        created = datetime.combine(day, dtime(12, 0), tzinfo=LONDON).astimezone(timezone.utc)
        created += timedelta(seconds=i % PER_DAY)
        stamp = created.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        txs.append(
            {
                "id": f"tx_{i:05d}",
                "account_id": ACCOUNT_ID,
                "created": stamp,
                "amount": -(100 + i),
                "currency": "GBP",
                "description": f"SHOP {i}",
                "merchant": {"id": f"merch_{i}", "name": f"Shop {i}"},
                "category": "groceries",
                "settled": "" if i == n - 1 else stamp,
                "notes": "",
            }
        )
    return txs


class FakeMonzo:
    def __init__(self, transactions: list[dict] | None = None, *, auth_code: str | None = None):
        self.generation = 0
        self.transactions = transactions or []
        self.refresh_calls = 0
        self.exchange_calls = 0
        self.auth_code = auth_code
        self.approved = auth_code is None  # an authorization-code login needs in-app approval first
        self.approve_after_polls = 1
        self.account_polls = 0
        self.port = free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.app = Starlette(
            routes=[
                Route("/oauth2/token", self.token, methods=["POST"]),
                Route("/ping/whoami", self.whoami),
                Route("/accounts", self.accounts),
                Route("/balance", self.balance),
                Route("/pots", self.pots),
                Route("/transactions", self.list_tx),
            ]
        )
        self._server: uvicorn.Server | None = None

    @property
    def access_token(self) -> str:
        return f"at-{self.generation}"

    @property
    def refresh_token(self) -> str:
        return f"rt-{self.generation}"

    def _unauth(self, request: Request):
        if request.headers.get("authorization") != f"Bearer {self.access_token}":
            return JSONResponse({"code": "unauthorized.bad_access_token.expired", "message": "expired"}, 401)
        return None

    def _issue(self):
        self.generation += 1
        return JSONResponse(
            {
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "expires_in": 21600,
                "token_type": "Bearer",
                "user_id": "user_1",
            }
        )

    async def token(self, request: Request):
        form = await request.form()
        grant = form.get("grant_type")
        if grant == "authorization_code" and self.auth_code and form.get("code") == self.auth_code:
            self.exchange_calls += 1
            self.auth_code = None  # single use
            return self._issue()
        if grant == "refresh_token" and form.get("refresh_token") == self.refresh_token:
            self.refresh_calls += 1
            return self._issue()
        return JSONResponse({"code": "unauthorized.bad_refresh_token", "message": "bad token request"}, 401)

    async def whoami(self, request: Request):
        return self._unauth(request) or JSONResponse({"authenticated": True, "client_id": "c", "user_id": "user_1"})

    async def accounts(self, request: Request):
        if (resp := self._unauth(request)) is not None:
            return resp
        if not self.approved:
            self.account_polls += 1
            if self.account_polls <= self.approve_after_polls:
                return JSONResponse({"code": "forbidden.insufficient_permissions", "message": "forbidden"}, 403)
            self.approved = True
        return JSONResponse(
            {
                "accounts": [
                    {
                        "id": ACCOUNT_ID,
                        "description": "Test User",
                        "type": "uk_retail",
                        "currency": "GBP",
                        "closed": False,
                        "created": "2020-01-01T00:00:00Z",
                    }
                ]
            }
        )

    async def balance(self, request: Request):
        return self._unauth(request) or JSONResponse(
            {"balance": 5000, "total_balance": 6000, "currency": "GBP", "spend_today": -250}
        )

    async def pots(self, request: Request):
        return self._unauth(request) or JSONResponse(
            {
                "pots": [
                    {
                        "id": "pot_1",
                        "name": "Savings",
                        "balance": 1000,
                        "currency": "GBP",
                        "deleted": False,
                        "created": "2021-01-01T00:00:00Z",
                        "updated": "2021-01-02T00:00:00Z",
                    }
                ]
            }
        )

    async def list_tx(self, request: Request):
        if (resp := self._unauth(request)) is not None:
            return resp
        q = request.query_params
        since, limit = q.get("since", ""), int(q.get("limit", 30))
        txs = self.transactions
        if since.startswith("tx_"):
            txs = [t for t in txs if t["id"] > since]
        elif since:
            txs = [t for t in txs if t["created"] >= since]
        if q.get("before"):
            txs = [t for t in txs if t["created"] < q["before"]]
        return JSONResponse({"transactions": txs[:limit]})

    def start(self) -> FakeMonzo:
        self._server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="error"))
        threading.Thread(target=self._server.run, daemon=True).start()
        deadline = time.monotonic() + 10
        while not self._server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("fake Monzo did not start")
            time.sleep(0.05)
        return self

    def stop(self) -> None:
        if self._server:
            self._server.should_exit = True
