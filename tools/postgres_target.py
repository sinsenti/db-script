"""Compatibility imports for shared PostgreSQL target helpers."""
from pathlib import Path
import sys

# Standalone tools also work when invoked outside the repository directory.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dbcli.postgres_target import *  # noqa: F401,F403
