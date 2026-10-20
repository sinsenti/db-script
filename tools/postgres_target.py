"""Resolve PostgreSQL credentials in memory and configure bounded sessions."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

DEFAULT_CONTAINER = "a1labs-postgres-1"
ENVIRONMENT_KEYS = ("DBCLI_POSTGRES_URL", "DBCLI_BENCH_POSTGRES_URL", "DBCLI_BENCH_DATABASE_URL")


class TargetError(RuntimeError):
    """A deliberately credential-free diagnostic."""


def bounded_timeout(value: str, cap: int) -> int:
    match = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*(ms|s|min|h|d)?\s*", value, re.IGNORECASE)
    if not match:
        return cap
    try:
        scale = {None: 1, "ms": 1, "s": 1000, "min": 60000, "h": 3600000, "d": 86400000}[(match[2] or "ms").lower()]
        duration = Decimal(match[1]) * scale
        return min(cap, max(1, int(duration))) if duration > 0 else cap
    except (InvalidOperation, ValueError, OverflowError):
        return cap


def configured_url(url: str, *, read_only: bool = True) -> str:
    """Set bounded session defaults. Raw-query callers must request read_only=True."""
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
            raise ValueError()
        params = dict(parse_qsl(parsed.query, keep_blank_values=True))
        previous = params.get("options", "")
        statement = bounded_timeout(params.pop("statement_timeout", ""), 3000)
        lock = bounded_timeout(params.pop("lock_timeout", ""), 1000)
        tokens = shlex.split(previous)
        for i, token in enumerate(tokens):
            setting = tokens[i + 1] if token == "-c" and i + 1 < len(tokens) else token[2:] if token.startswith("-c") else ""
            key, _, value = setting.partition("=")
            if key.lower() == "statement_timeout":
                statement = bounded_timeout(value, statement)
            elif key.lower() == "lock_timeout":
                lock = bounded_timeout(value, lock)
        try:
            connect = int(params.get("connect_timeout", "3"))
        except ValueError:
            connect = 3
        params["connect_timeout"] = str(min(connect, 3) if connect > 0 else 3)
        mode = "on" if read_only else "off"
        params["options"] = previous + f" -c default_transaction_read_only={mode} -c transaction_read_only={mode} -c statement_timeout={statement} -c lock_timeout={lock} -c timezone=UTC"
        params["application_name"] = "dbcli_postgres_read" if read_only else "dbcli_postgres_write"
        # libpq URI queries percent-decode; they do not use HTML form '+' spaces.
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(params, quote_via=quote), ""))
    except (ValueError, TypeError):
        raise TargetError("Expected a PostgreSQL connection URL in DBCLI_POSTGRES_URL") from None


def readonly_url(url: str) -> str:
    return configured_url(url, read_only=True)


def docker_target(container: str = DEFAULT_CONTAINER, *, read_only: bool = True) -> tuple[str, dict]:
    try:
        result = subprocess.run(["docker", "inspect", container], capture_output=True, text=True, timeout=10, check=True)
        info = json.loads(result.stdout)[0]
        if not info["State"]["Running"]:
            raise ValueError()
        bindings = info["NetworkSettings"]["Ports"]["5432/tcp"]
        binding = next(x for x in bindings if x["HostIp"] in {"127.0.0.1", "0.0.0.0", ""})
        env = dict(item.split("=", 1) for item in info["Config"]["Env"] if "=" in item)
        user = env.get("POSTGRES_USER", "postgres")
        database = env.get("POSTGRES_DB", user)
        password = env.get("POSTGRES_PASSWORD", "")
        # Password-file deployments are supported through the explicit environment URL.
        if not password:
            raise TargetError("Container password unavailable; set DBCLI_POSTGRES_URL in your environment")
        authority = quote(user, safe="") + ":" + quote(password, safe="") + "@127.0.0.1:" + binding["HostPort"]
        url = configured_url("postgresql://" + authority + "/" + quote(database, safe="") + "?sslmode=disable", read_only=read_only)
        safe = {"container_id": info["Id"], "container": info["Name"].lstrip("/"),
                "image": info["Config"]["Image"], "endpoint": "127.0.0.1:" + binding["HostPort"],
                "database_name_sha256": hashlib.sha256(database.encode()).hexdigest()}
        return url, safe
    except TargetError:
        raise
    except (OSError, ValueError, KeyError, IndexError, StopIteration, subprocess.SubprocessError):
        raise TargetError("Cannot resolve a running PostgreSQL container with a local published port") from None


def target_environment(url: str, *, read_only: bool = True) -> dict[str, str]:
    env = dict(os.environ)
    for key in ENVIRONMENT_KEYS:
        env[key] = configured_url(url, read_only=read_only)
    return env


def connection_factory(url: str, *, read_only: bool = True):
    import psycopg

    def connect():
        connection = None
        try:
            connection = psycopg.connect(configured_url(url, read_only=read_only))
            connection.read_only = read_only
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_setting('transaction_read_only'),current_setting('default_transaction_read_only')")
                mode = "on" if read_only else "off"
                if cursor.fetchone() != (mode, mode):
                    raise TargetError("PostgreSQL refused the requested session mode")
            return connection
        except Exception:
            if connection is not None:
                connection.close()
            raise TargetError("Could not establish a bounded PostgreSQL connection") from None
    return connect
