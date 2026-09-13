import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from dbcli.discovery import find_docker_databases, find_sqlite_files


class DiscoveryTests(unittest.TestCase):
    def test_find_sqlite_files_is_shallow_and_ignores_non_sqlite_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database_path = root / "app.db"
            connection = sqlite3.connect(database_path)
            connection.execute("CREATE TABLE users (id INTEGER PRIMARY KEY)")
            connection.close()
            (root / "notes.txt").write_text("not a database", encoding="utf-8")
            nested = root / "nested"
            nested.mkdir()
            nested_database = nested / "nested.db"
            sqlite3.connect(nested_database).close()

            self.assertEqual(find_sqlite_files(root), [database_path])

    def test_find_docker_databases_supports_injected_runner(self):
        docker_output = "\n".join(
            [
                json.dumps(
                    {
                        "ID": "abc",
                        "Names": "postgres-db",
                        "Image": "postgres:16",
                        "Ports": "5432/tcp",
                    }
                ),
                json.dumps(
                    {
                        "ID": "def",
                        "Names": "web",
                        "Image": "nginx:latest",
                        "Ports": "",
                    }
                ),
            ]
        )

        def fake_run(*args, **kwargs):
            return SimpleNamespace(stdout=docker_output)

        self.assertEqual(
            find_docker_databases(run=fake_run),
            [
                {
                    "id": "abc",
                    "name": "postgres-db",
                    "image": "postgres:16",
                    "engine": "postgres",
                }
            ],
        )

    def test_find_docker_databases_returns_none_when_docker_is_unavailable(self):
        def unavailable(*args, **kwargs):
            raise FileNotFoundError("docker")

        self.assertIsNone(find_docker_databases(run=unavailable))


if __name__ == "__main__":
    unittest.main()
