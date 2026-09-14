import unittest

from dbcli.adapters import (
    MySQLAdapter,
    PostgresAdapter,
    ReadOnlyError,
    UnsupportedQueryError,
)


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.description = None
        self.rowcount = 0
        self._rows = []

    def execute(self, sql, params=()):
        self.connection.calls.append((sql, tuple(params)))
        normalized = " ".join(sql.split()).lower()
        self.description = None
        self.rowcount = 0

        if "select current_database()" in normalized or "select database()" in normalized:
            self._rows = [(self.connection.database,)]
        elif "select table_name from information_schema.tables" in normalized:
            self._rows = [("teams",), ("users",)]
        elif "select column_name from information_schema.columns" in normalized:
            self._rows = [("id",), ("name",)]
        elif "select column_name, data_type, is_nullable" in normalized:
            self._rows = [("id", "integer", "NO", None), ("name", "text", "YES", None)]
        elif "select kcu.column_name" in normalized and "primary key" in normalized:
            self._rows = [("id",)]
        elif "constraint_column_usage" in normalized or "referenced_table_name" in normalized:
            self._rows = [("team_id", "teams", "id")]
        elif normalized.startswith("select count(*)"):
            self._rows = [(2,)]
        elif normalized.startswith("select * from"):
            self.description = [("id",), ("name",)]
            self._rows = [(1, "Ada"), (2, "Grace")]
        elif normalized.startswith("insert "):
            self.rowcount = 1
            self._rows = []
        elif normalized.startswith("update ") or normalized.startswith("delete "):
            self.rowcount = 1
            self._rows = []
        else:
            raise AssertionError(f"Unhandled SQL in fake cursor: {sql}")
        return self

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        pass


class FakeConnection:
    def __init__(self, database):
        self.database = database
        self.calls = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


class NativeAdapterTests(unittest.TestCase):
    def _exercise_adapter(self, adapter, connection):
        self.assertEqual(adapter.list_databases(), [connection.database])
        self.assertEqual(adapter.list_tables(), ["teams", "users"])

        page = adapter.fetch_page(
            "users", 2, 0, filter_query="Ada", order_by=("name", "ASC")
        )
        self.assertEqual(page.headers, ["id", "name"])
        self.assertEqual(page.rows[0], (1, "Ada"))
        self.assertEqual(adapter.count_rows("users", "Ada"), 2)
        self.assertEqual(adapter.schema_info("users").rows[0][0], "id")
        self.assertEqual(
            adapter.foreign_keys("users"),
            [{"from_col": "team_id", "to_table": "teams", "to_col": "id"}],
        )

        self.assertEqual(adapter.insert_row("users", {"name": "O'Reilly"}), 1)
        original = {"id": 1, "name": "Ada"}
        self.assertEqual(adapter.update_row("users", original, {"id": 1, "name": "Updated"}), 1)
        self.assertEqual(adapter.delete_row("users", original), 1)
        raw = adapter.execute_readonly(
            "SELECT id, name FROM users WHERE name = %s", ("Ada",), limit=1
        )
        self.assertEqual(raw.rows[0], (1, "Ada"))
        quoted_identifier = adapter.execute_readonly(
            'SELECT "update", \'delete\' FROM users', limit=1
        )
        self.assertEqual(quoted_identifier.rows[0], (1, "Ada"))

        self.assertGreaterEqual(len(connection.calls), 10)
        update_call = next(call for call in connection.calls if call[0].lstrip().upper().startswith("UPDATE"))
        self.assertNotIn("Updated", update_call[0])
        self.assertIn("Updated", update_call[1])
        self.assertGreaterEqual(connection.commits, 3)

    def test_postgres_adapter_contract_with_injected_connection(self):
        connection = FakeConnection("appdb")
        adapter = PostgresAdapter(
            "postgresql://user@example/appdb",
            connection_factory=lambda: connection,
        )
        self._exercise_adapter(adapter, connection)

    def test_mysql_adapter_contract_with_injected_connection(self):
        connection = FakeConnection("appdb")
        adapter = MySQLAdapter(
            "mysql://user@example/appdb",
            connection_factory=lambda: connection,
        )
        self._exercise_adapter(adapter, connection)

    def test_native_read_only_contract(self):
        for adapter_type, url in (
            (PostgresAdapter, "postgresql://user@example/appdb"),
            (MySQLAdapter, "mysql://user@example/appdb"),
        ):
            connection = FakeConnection("appdb")
            adapter = adapter_type(url, read_only=True, connection_factory=lambda: connection)
            with self.subTest(adapter=adapter_type.__name__):
                with self.assertRaises(ReadOnlyError):
                    adapter.insert_row("users", {"name": "blocked"})
                with self.assertRaises(ReadOnlyError):
                    adapter.update_row("users", {"id": 1}, {"name": "blocked"})
                with self.assertRaises(ReadOnlyError):
                    adapter.delete_row("users", {"id": 1})
                with self.assertRaises(UnsupportedQueryError):
                    adapter.execute_readonly("UPDATE users SET name = %s", ("blocked",))
                with self.assertRaises(UnsupportedQueryError):
                    adapter.execute_readonly(
                        "WITH changed AS (UPDATE users SET name = %s RETURNING id) "
                        "SELECT * FROM changed",
                        ("blocked",),
                    )


if __name__ == "__main__":
    unittest.main()
