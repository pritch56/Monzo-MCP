"""Token persistence.

Monzo returns a NEW refresh token on every refresh and refresh tokens are
single-use, so whatever store is used must durably save the token set after
each refresh. To plug in your own secret store, implement the two-method
`TokenStore` protocol below and point MONZO_TOKEN_STORE at a factory, e.g.

    MONZO_TOKEN_STORE=my_module:make_store

where `make_store()` takes no arguments and returns an object with
`load() -> TokenSet | None` and `save(tokens: TokenSet) -> None`. A store may
also offer `clear() -> None`, which `monzo-mcp-authorize --reset` uses.

MONZO_TOKEN_STORE names code that this process imports and runs: treat it like
any other executable configuration.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import logging
import os
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Protocol, runtime_checkable

from . import clock
from .errors import ConfigError, TokenStoreError

log = logging.getLogger("monzo_mcp")


@dataclass
class TokenSet:
    access_token: str | None = None
    refresh_token: str | None = None
    # Unix seconds. 0 = unknown (treated as expired when a refresh token exists).
    expires_at: float = 0.0
    # When the user last completed the browser + in-app approval (unix seconds).
    # Used only to estimate when the next in-app re-approval falls due.
    authorized_at: float | None = None
    # True once an API call has succeeded with these credentials, i.e. the in-app
    # approval definitely happened. Lets a later 403 be reported as lapsed consent.
    approved: bool = False
    user_id: str | None = None
    # Fingerprint of the env-var seed these tokens descend from (never the token itself).
    seed_hash: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_json(cls, raw: str) -> TokenSet:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("token data must be a JSON object")
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def __repr__(self) -> str:  # never leak secrets through logs/tracebacks
        return (
            f"TokenSet(access_token={'<set>' if self.access_token else None}, "
            f"refresh_token={'<set>' if self.refresh_token else None}, "
            f"expires_at={self.expires_at}, authorized_at={self.authorized_at})"
        )


@runtime_checkable
class TokenStore(Protocol):
    def load(self) -> TokenSet | None:
        """Return the most recently saved tokens, or None if nothing is stored."""

    def save(self, tokens: TokenSet) -> None:
        """Durably persist tokens. Must raise if the write did not succeed."""


def _is_windows() -> bool:
    return sys.platform == "win32"


def _windows_account() -> str:
    user = os.environ.get("USERNAME") or ""
    domain = os.environ.get("USERDOMAIN") or ""
    if not user:
        import getpass

        user = getpass.getuser()
    return f"{domain}\\{user}" if domain else user


def restrict_to_current_user(path: str | os.PathLike[str]) -> bool:
    """Make `path` readable and writable by the current user only.

    POSIX: mode 0600. Windows: drop inherited ACL entries and grant the current user full
    control (`icacls /inheritance:r /grant:r`). Failure is logged as a warning, never fatal.
    """
    try:
        if _is_windows():
            result = subprocess.run(
                ["icacls", str(path), "/inheritance:r", "/grant:r", f"{_windows_account()}:(F)"],
                capture_output=True,
                timeout=15,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode != 0:
                raise OSError(f"icacls exited with status {result.returncode}")
        else:
            os.chmod(path, 0o600)
        return True
    except (OSError, subprocess.SubprocessError) as exc:
        reason = type(exc).__name__ if isinstance(exc, subprocess.SubprocessError) else str(exc)
        log.warning(
            "Could not restrict the token file to the current user (%s). Check its permissions by hand.", reason
        )
        return False


class FileTokenStore:
    """Default store: a plaintext JSON file, written atomically and readable only by the
    current user (0600 on POSIX, a current-user-only ACL on Windows)."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path).expanduser()

    def load(self) -> TokenSet | None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError) as exc:
            raise TokenStoreError(f"Cannot read token file {self.path}: {type(exc).__name__}") from exc
        try:
            return TokenSet.from_json(raw)
        except (ValueError, TypeError) as exc:
            raise TokenStoreError(f"Token file {self.path} is not valid: {type(exc).__name__}") from exc

    def save(self, tokens: TokenSet) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".tokens-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(tokens.to_json())
                    fh.flush()
                    os.fsync(fh.fileno())
                # Lock the file down before it takes its final name, so the tokens are never
                # readable by anyone else under that name.
                restrict_to_current_user(tmp)
                os.replace(tmp, self.path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError as exc:
            raise TokenStoreError(f"Cannot write token file {self.path}: {exc}") from exc

    def clear(self) -> None:
        """Delete the stored tokens (used by `monzo-mcp-authorize --reset`)."""
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:
            raise TokenStoreError(f"Cannot delete token file {self.path}: {exc}") from exc


class MemoryTokenStore:
    """Non-persistent store for tests. Do NOT use in production: a rotated
    refresh token would be lost when the process exits."""

    def __init__(self, tokens: TokenSet | None = None):
        self.tokens = tokens
        self.saves = 0

    def load(self) -> TokenSet | None:
        return self.tokens

    def save(self, tokens: TokenSet) -> None:
        self.tokens = tokens
        self.saves += 1

    def clear(self) -> None:
        self.tokens = None


def seed_fingerprint(refresh_token: str | None, access_token: str | None) -> str | None:
    basis = refresh_token or access_token
    if not basis:
        return None
    return hashlib.sha256(basis.encode()).hexdigest()[:16]


def default_token_path() -> Path:
    return Path.home() / ".monzo-mcp" / "tokens.json"


def load_token_store(env: dict[str, str] | None = None) -> TokenStore:
    """Build the store from MONZO_TOKEN_STORE ("module:factory") or MONZO_TOKEN_FILE."""
    env = os.environ if env is None else env
    spec = (env.get("MONZO_TOKEN_STORE") or "").strip()
    if spec:
        module_name, _, attr = spec.partition(":")
        if not module_name or not attr:
            raise ConfigError("MONZO_TOKEN_STORE must look like 'package.module:factory'.")
        try:
            factory = getattr(importlib.import_module(module_name), attr)
        except (ImportError, AttributeError) as exc:
            raise ConfigError(f"Cannot load token store '{spec}': {exc}") from exc
        try:
            store = factory()
        except Exception as exc:  # third-party code: anything can go wrong
            raise ConfigError(f"Token store '{spec}' could not be created: {type(exc).__name__}") from exc
        if not isinstance(store, TokenStore):
            raise ConfigError(f"Token store '{spec}' must provide load() and save(tokens).")
        return store
    return FileTokenStore((env.get("MONZO_TOKEN_FILE") or "").strip() or default_token_path())


def now() -> float:
    return clock.now()
