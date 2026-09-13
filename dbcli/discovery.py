"""Local SQLite and Docker database discovery."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Callable


DB_KEYWORDS = ("postgres", "mysql", "mariadb", "sqlite")
DB_PORTS = ("5432", "3306")


def find_sqlite_files(root: str | Path | None = None) -> list[Path]:
    """Find SQLite files directly under ``root``.

    Discovery intentionally remains shallow to avoid scanning large home
    directories.  Callers can pass a different root for testing or future
    recursive-discovery options.
    """
    search_root = Path.cwd() if root is None else Path(root)
    sqlite_dbs: list[Path] = []
    try:
        candidates = search_root.glob("*")
    except (OSError, PermissionError):
        return []

    for path in candidates:
        if not path.is_file():
            continue
        try:
            with path.open("rb") as database_file:
                if database_file.read(16) == b"SQLite format 3\x00":
                    sqlite_dbs.append(path)
        except (PermissionError, OSError):
            continue
    return sorted(sqlite_dbs, key=lambda item: item.name.lower())


def find_docker_databases(
    run: Callable[..., object] | None = None,
) -> list[dict[str, str]] | None:
    """Find running Docker containers that look like database containers.

    ``run`` is injectable so discovery can be tested without requiring Docker.
    ``None`` indicates that Docker is unavailable or returned invalid output;
    an empty list means Docker worked but no database containers were found.
    """
    runner = run or subprocess.run
    containers: list[dict[str, str]] = []
    try:
        result = runner(
            ["docker", "ps", "--format", "{{json .}}"],
            capture_output=True,
            text=True,
            check=True,
        )
        output = getattr(result, "stdout", "") or ""
        for line in output.strip().splitlines():
            if not line:
                continue
            data = json.loads(line)
            image = data.get("Image", "").lower()
            ports = data.get("Ports", "").lower()

            if not any(keyword in image for keyword in DB_KEYWORDS) and not any(
                port in ports for port in DB_PORTS
            ):
                continue

            engine = (
                "postgres"
                if "postgres" in image
                else ("mysql" if "mysql" in image or "mariadb" in image else "other")
            )
            containers.append(
                {
                    "id": str(data.get("ID", "")),
                    "name": str(data.get("Names", "")),
                    "image": str(data.get("Image", "")),
                    "engine": engine,
                }
            )
    except (OSError, subprocess.CalledProcessError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return containers
