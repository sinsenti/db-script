#!/usr/bin/env python3
"""Manage only this comparison's disposable PostgreSQL fixture container."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
from urllib.parse import quote

from postgres_target import readonly_url

ROOT = Path(__file__).resolve().parents[1]
DESCRIPTOR = ROOT / ".local/postgres-write/fixture.json"
NAME = "dbcli-codex-postgres-write-fixture"
MARKER = "dbcli-postgres-write-disposable-v1"
DATABASE = "dbcli_postgres_write_fixture"


def docker(*args):
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError("Fixture Docker command failed (details suppressed)")
    return result.stdout


def verified_descriptor():
    if DESCRIPTOR.stat().st_mode & 0o077:
        raise RuntimeError("Fixture descriptor must be private")
    data = json.loads(DESCRIPTOR.read_text())
    info = json.loads(docker("inspect", NAME))[0]
    if (data.get("fixture_marker") != MARKER or data.get("container") != NAME
            or data.get("container_id") != info["Id"]
            or info["Config"].get("Labels", {}).get("dbcli.fixture") != "postgres-write"
            or data.get("database") != DATABASE):
        raise RuntimeError("Fixture identity mismatch; refusing to manage this container")
    return data


def start():
    if DESCRIPTOR.exists():
        return verified_descriptor()
    DESCRIPTOR.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(DESCRIPTOR.parent, 0o700)
    password = secrets.token_urlsafe(32)
    descriptor_fd, filename = tempfile.mkstemp(prefix="docker-env-", dir=DESCRIPTOR.parent)
    try:
        with os.fdopen(descriptor_fd, "w") as output:
            output.write("POSTGRES_USER=dbcli_fixture\nPOSTGRES_PASSWORD=" + password + "\nPOSTGRES_DB=" + DATABASE + "\n")
        docker("run", "-d", "--rm", "--name", NAME, "--label", "dbcli.fixture=postgres-write", "--env-file", filename,
               "-p", "127.0.0.1::5432", "postgres:16-alpine")
    finally:
        Path(filename).unlink(missing_ok=True)
    info = json.loads(docker("inspect", NAME))[0]
    port = int(info["NetworkSettings"]["Ports"]["5432/tcp"][0]["HostPort"])
    url = "postgresql://dbcli_fixture:" + quote(password, safe="") + "@127.0.0.1:" + str(port) + "/" + DATABASE + "?sslmode=disable&connect_timeout=3"
    data = {"fixture_marker": MARKER, "container": NAME, "container_id": info["Id"], "database": DATABASE,
            "admin_url": url, "url": url, "readonly_url": readonly_url(url), "port": port}
    fd = os.open(DESCRIPTOR, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as output:
        json.dump(data, output)
        output.write("\n")
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        probe = subprocess.run(["docker", "exec", NAME, "pg_isready", "-h", "127.0.0.1", "-U", "dbcli_fixture", "-d", DATABASE], capture_output=True, timeout=3)
        if probe.returncode == 0:
            return data
        time.sleep(0.2)
    raise RuntimeError("Fixture startup timed out; inspect its status before retrying")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "status", "stop"])
    args = parser.parse_args()
    try:
        data = start() if args.action == "start" else verified_descriptor()
        if args.action == "stop":
            docker("rm", "-f", NAME)
            DESCRIPTOR.unlink()
            print("Stopped the disposable PostgreSQL write fixture")
        else:
            print(json.dumps({"container": data["container"], "port": data["port"], "descriptor": str(DESCRIPTOR)}))
        return 0
    except (RuntimeError, OSError, ValueError, KeyError, subprocess.SubprocessError):
        print("Fixture operation failed; no connection details printed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
