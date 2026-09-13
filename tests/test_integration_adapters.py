"""Opt-in integration tests for native adapters.

These tests are skipped by default.  To run them against Docker-published
servers, install the optional drivers and set URLs, for example:

    DBCLI_RUN_INTEGRATION=1 \
    DBCLI_POSTGRES_URL=postgresql://postgres:postgres@localhost:5432/postgres \
    DBCLI_MYSQL_URL=mysql://root:root@localhost:3306/test \
    python -m unittest tests.test_integration_adapters
"""

import importlib.util
import os
import shutil
import subprocess
import unittest

from dbcli.adapters import MySQLAdapter, PostgresAdapter


def _docker_is_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(
            ["docker", "info"], capture_output=True, check=True, timeout=5
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


_RUN_INTEGRATION = os.environ.get("DBCLI_RUN_INTEGRATION") == "1"
_DOCKER_AVAILABLE = _docker_is_available() if _RUN_INTEGRATION else False


class NativeIntegrationMixin:
    table_name = f"dbcli_adapter_test_{os.getpid()}"

    @classmethod
    def setUpClass(cls):
        try:
            cls.adapter = cls.adapter_type(cls.connection_url)
            connection = cls.adapter._connection_factory()
            cursor = connection.cursor()
            cursor.execute(cls.create_table_sql)
            connection.commit()
            cursor.close()
            connection.close()
        except Exception as exc:
            raise unittest.SkipTest(f"database is not ready: {exc}") from exc

    @classmethod
    def tearDownClass(cls):
        try:
            connection = cls.adapter._connection_factory()
            cursor = connection.cursor()
            cursor.execute(cls.drop_table_sql)
            connection.commit()
            cursor.close()
            connection.close()
        except Exception:
            pass

    def test_native_adapter_crud_and_query(self):
        self.assertIn(self.table_name, self.adapter.list_tables())
        self.assertEqual(
            self.adapter.insert_row(self.table_name, {"id": 1, "name": "Ada"}), 1
        )
        result = self.adapter.fetch_page(
            self.table_name, 10, 0, filter_query="Ada"
        )
        self.assertEqual(result.rows[0][1], "Ada")
        self.assertEqual(self.adapter.count_rows(self.table_name, "Ada"), 1)
        self.assertEqual(
            self.adapter.update_row(
                self.table_name,
                {"id": 1, "name": "Ada"},
                {"id": 1, "name": "Grace"},
            ),
            1,
        )
        self.assertEqual(
            self.adapter.delete_row(self.table_name, {"id": 1, "name": "Grace"}),
            1,
        )


@unittest.skipUnless(
    _RUN_INTEGRATION
    and _DOCKER_AVAILABLE
    and os.environ.get("DBCLI_POSTGRES_URL")
    and importlib.util.find_spec("psycopg"),
    "set DBCLI_RUN_INTEGRATION=1, DBCLI_POSTGRES_URL and install psycopg",
)
class PostgresDockerIntegrationTests(NativeIntegrationMixin, unittest.TestCase):
    adapter_type = PostgresAdapter
    connection_url = os.environ.get("DBCLI_POSTGRES_URL", "")
    create_table_sql = (
        f'CREATE TABLE "{NativeIntegrationMixin.table_name}" '
        "(id INTEGER PRIMARY KEY, name TEXT NOT NULL)"
    )
    drop_table_sql = f'DROP TABLE IF EXISTS "{NativeIntegrationMixin.table_name}"'


@unittest.skipUnless(
    _RUN_INTEGRATION
    and _DOCKER_AVAILABLE
    and os.environ.get("DBCLI_MYSQL_URL")
    and importlib.util.find_spec("mysql.connector"),
    "set DBCLI_RUN_INTEGRATION=1, DBCLI_MYSQL_URL and install mysql-connector-python",
)
class MySQLDockerIntegrationTests(NativeIntegrationMixin, unittest.TestCase):
    adapter_type = MySQLAdapter
    connection_url = os.environ.get("DBCLI_MYSQL_URL", "")
    create_table_sql = (
        f"CREATE TABLE `{NativeIntegrationMixin.table_name}` "
        "(id INTEGER PRIMARY KEY, name VARCHAR(255) NOT NULL)"
    )
    drop_table_sql = f"DROP TABLE IF EXISTS `{NativeIntegrationMixin.table_name}`"


if __name__ == "__main__":
    unittest.main()
