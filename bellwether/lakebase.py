"""Lakebase connection layer. No DDL and no table-specific SQL — see schema.py.

Lakebase authenticates with a workspace OAuth token used as the Postgres
password. The token is short-lived (one hour), so it is minted at call time
rather than read from a durable secret, and any auth failure triggers one
fresh mint and retry.

Note on credentials: `databricks database generate-database-credential` does
not work against this deployment. Lakebase projects are a different resource
type from database instances, and list-database-instances returns empty even
on the correct workspace profile — so the plain workspace OAuth token is the
credential. Do not reintroduce generate-database-credential here.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterator

import psycopg

from . import config


class LakebaseAuthError(RuntimeError):
    """No usable credential could be obtained."""


# --- Token minting ---------------------------------------------------------

# Refresh this far ahead of stated expiry, so a token cannot lapse between
# the freshness check and the connection handshake.
_EXPIRY_SKEW_SECONDS = 300

_TOKEN_CLI_TIMEOUT = 60


@dataclass(frozen=True)
class _Token:
    value: str
    expires_at: float | None

    def is_fresh(self) -> bool:
        if self.expires_at is None:
            return False
        return time.time() < self.expires_at - _EXPIRY_SKEW_SECONDS


_cached_token: _Token | None = None


def _mint_via_sdk(profile: str | None) -> _Token | None:
    """Workspace OAuth via the Databricks SDK, if it is installed.

    Present in a deployed app, generally absent locally. With no profile the
    SDK falls back to its own resolution, which is how a deployed app picks up
    its injected service-principal credentials.
    """
    try:
        from databricks.sdk.core import Config  # type: ignore
    except ImportError:
        return None
    try:
        cfg = Config(profile=profile) if profile else Config()
        headers = cfg.authenticate()
        token = headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not token:
            return None
        # The SDK refreshes internally; treat as short-lived regardless.
        return _Token(token, time.time() + 3600)
    except Exception:
        return None


def _mint_via_cli(profile: str) -> _Token:
    """Workspace OAuth via `databricks auth token -p <profile>`."""
    try:
        completed = subprocess.run(
            ["databricks", "auth", "token", "-p", profile],
            capture_output=True,
            text=True,
            timeout=_TOKEN_CLI_TIMEOUT,
        )
    except FileNotFoundError as exc:
        raise LakebaseAuthError(
            "databricks CLI not found on PATH and the SDK is not installed, "
            "so no token can be minted"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise LakebaseAuthError(
            f"databricks auth token timed out after {_TOKEN_CLI_TIMEOUT}s"
        ) from exc

    if completed.returncode != 0:
        raise LakebaseAuthError(
            f"databricks auth token failed for profile {profile!r}: "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )

    try:
        payload = json.loads(completed.stdout)
        token = payload["access_token"]
    except (json.JSONDecodeError, KeyError) as exc:
        raise LakebaseAuthError(
            "could not parse an access_token out of the databricks CLI response"
        ) from exc

    return _Token(token, _parse_expiry(payload.get("expiry")))


def _parse_expiry(raw: str | None) -> float | None:
    """The CLI reports an ISO-8601 expiry; absent or odd values mean unknown."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw).timestamp()
    except ValueError:
        return None


def mint_token(*, force: bool = False) -> str:
    """Return a workspace OAuth token, reusing a cached one while it is fresh."""
    global _cached_token

    if not force and _cached_token is not None and _cached_token.is_fresh():
        return _cached_token.value

    profile = config.DATABRICKS_PROFILE
    token = _mint_via_sdk(profile) or (_mint_via_cli(profile) if profile else None)
    if token is None:
        raise LakebaseAuthError(
            "no DATABRICKS_PROFILE set and the SDK could not authenticate, "
            "so no token can be minted"
        )

    _cached_token = token
    return token.value


# --- Connection parameters -------------------------------------------------


@dataclass(frozen=True)
class Connection:
    """Resolved connection parameters, plus where the credential came from."""

    host: str
    port: int
    dbname: str
    user: str
    password: str
    sslmode: str
    source: str

    def conninfo(self) -> str:
        return (
            f"host={self.host} port={self.port} dbname={self.dbname} "
            f"user={self.user} password={self.password} sslmode={self.sslmode}"
        )

    def describe(self) -> str:
        """Loggable summary. Never includes the password."""
        return (
            f"{self.user}@{self.host}:{self.port}/{self.dbname} "
            f"sslmode={self.sslmode} credential={self.source}"
        )


def _databricks_injected() -> dict[str, str] | None:
    """Standard libpq vars injected when deployed with a Lakebase resource."""
    host = os.getenv("PGHOST")
    user = os.getenv("PGUSER")
    if not host or not user:
        return None
    return {
        "host": host,
        "port": os.getenv("PGPORT", "5432"),
        "dbname": os.getenv("PGDATABASE", "databricks_postgres"),
        "user": user,
        "sslmode": os.getenv("PGSSLMODE", "require"),
    }


def resolve(*, force_refresh: bool = False) -> Connection:
    """Resolve connection parameters.

    Priority: Databricks-injected env vars, then a freshly minted workspace
    token (SDK default auth, then DATABRICKS_PROFILE if set), then
    LAKEBASE_PASSWORD from .env as a local fallback.
    """
    injected = _databricks_injected()
    if injected is not None:
        # A deployed app gets PGPASSWORD only sometimes; otherwise its own
        # service principal mints the token.
        password = os.getenv("PGPASSWORD")
        if password and not force_refresh:
            source = "databricks-env"
        else:
            password = mint_token(force=force_refresh)
            source = "databricks-env+minted"
        return Connection(
            host=injected["host"],
            port=int(injected["port"]),
            dbname=injected["dbname"],
            user=injected["user"],
            password=password,
            sslmode=injected["sslmode"],
            source=source,
        )

    settings = config.lakebase_settings()
    missing = settings.missing()
    if missing:
        raise LakebaseAuthError(
            f"missing connection settings: {', '.join(missing)}"
        )

    # Minting is attempted unconditionally rather than only when
    # DATABRICKS_PROFILE is set. mint_token() already tries the SDK's default
    # auth chain first — which resolves to the app's own service principal
    # when deployed — and only uses the CLI profile when that env var is
    # present. Gating this branch on the profile meant an app deployed with
    # LAKEBASE_HOST/LAKEBASE_USER but no injected PGHOST could not
    # authenticate at all, despite having a perfectly good identity.
    try:
        password = mint_token(force=force_refresh)
        source = f"minted:{config.DATABRICKS_PROFILE or 'sdk-default'}"
    except LakebaseAuthError:
        if not settings.password:
            raise LakebaseAuthError(
                "no credential available: the SDK's default auth chain found "
                "no identity, DATABRICKS_PROFILE is unset, and there is no "
                "LAKEBASE_PASSWORD fallback"
            )
        password, source = settings.password, "env:LAKEBASE_PASSWORD"

    return Connection(
        host=settings.host,  # type: ignore[arg-type]
        port=settings.port,
        dbname=settings.dbname,
        user=settings.user,  # type: ignore[arg-type]
        password=password,
        sslmode=settings.sslmode,
        source=source,
    )


# --- Auth-aware connect ----------------------------------------------------

# Substrings that mark a failure as credential-related rather than a genuine
# fault. Token expiry surfaces as an ordinary auth rejection.
_AUTH_MARKERS = (
    "password authentication failed",
    "authentication failed",
    "invalid token",
    "token has expired",
    "expired",
    "jwt",
    "unauthorized",
    "permission denied for database",
)

CONNECT_TIMEOUT = 30


def _is_auth_failure(exc: Exception) -> bool:
    return any(marker in str(exc).lower() for marker in _AUTH_MARKERS)


def _open(force_refresh: bool = False) -> psycopg.Connection:
    resolved = resolve(force_refresh=force_refresh)
    return psycopg.connect(resolved.conninfo(), connect_timeout=CONNECT_TIMEOUT)


@contextmanager
def connect(*, autocommit: bool = False) -> Iterator[psycopg.Connection]:
    """Yield a Lakebase connection, closing it on exit.

    Retries once with a freshly minted token if the handshake fails on auth,
    which is what an expired token looks like.
    """
    try:
        conn = _open()
    except psycopg.Error as exc:
        if not _is_auth_failure(exc):
            raise
        conn = _open(force_refresh=True)

    conn.autocommit = autocommit
    try:
        yield conn
    finally:
        conn.close()


def execute(
    sql: str,
    params: Any = None,
    *,
    fetch: str | None = None,
    autocommit: bool = False,
) -> Any:
    """Run one statement on its own connection, retrying once on auth failure.

    `fetch` is None, "one", or "all". Covers the query-level auth failure that
    the context manager cannot retry, since it cannot re-run the caller's block.
    """
    if fetch not in (None, "one", "all"):
        raise ValueError(f"fetch must be None, 'one' or 'all', got {fetch!r}")

    for attempt in (0, 1):
        try:
            with connect(autocommit=autocommit) as conn:
                cur = conn.execute(sql, params)
                result = (
                    cur.fetchone() if fetch == "one"
                    else cur.fetchall() if fetch == "all"
                    else None
                )
                if not autocommit:
                    conn.commit()
                return result
        except psycopg.Error as exc:
            if attempt == 1 or not _is_auth_failure(exc):
                raise
            mint_token(force=True)

    raise AssertionError("unreachable")


def healthcheck() -> dict[str, Any]:
    """Connect and report what came back. Useful as a deployment smoke test."""
    resolved = resolve()
    with connect() as conn:
        version, user, db = conn.execute(
            "select version(), current_user, current_database()"
        ).fetchone()
    return {
        "target": resolved.describe(),
        "server_version": version,
        "current_user": user,
        "current_database": db,
    }
