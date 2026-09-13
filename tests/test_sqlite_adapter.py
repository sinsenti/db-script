import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dbcli.adapters import (
    AdapterError,
    ReadOnlyError,
    RowIdentityError,
    SQLiteAdapter,
    UnsupportedQueryError,
)


class SQLiteAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "fixture.sqlite"
        connection = sqlite3.connect(self.db_path)
        connection.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE departments (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE
            );
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                department_id INTEGER,
                name TEXT NOT NULL,
                metadata TEXT,
                FOREIGN KEY (department_id) REFERENCES departments(id)
            );
            CREATE TABLE duplicate_values (
                name TEXT NOT NULL,
                value TEXT
            );
            INSERT INTO departments (id, name) VALUES (1, 'Engineering');
            INSERT INTO users (id, department_id, name, metadata) VALUES
                (1, 1, 'Ada', '{"role":"admin"}'),
                (2, 1, 'O''Reilly', '{"role":"author"}');
            INSERT INTO duplicate_values (name, value) VALUES
                ('same', 'one'), ('same', 'one');
            """
        )
        connection.commit()
        connection.close()
        self.adapter = SQLiteAdapter(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_lists_tables_and_databases(self):
        self.assertEqual(
            self.adapter.list_tables(),
            ["departments", "duplicate_values", "users"],
        )
        self.assertEqual(self.adapter.list_databases(), [str(self.db_path)])

    def test_fetch_and_count_use_same_plain_text_filter(self):
        result = self.adapter.fetch_page(
            "users", limit=1, offset=0, filter_query="O'Reilly"
        )
        self.assertEqual(result.headers, ["id", "department_id", "name", "metadata"])
        self.assertEqual(len(result.rows), 1)
        self.assertEqual(result.rows[0][2], "O'Reilly")
        self.assertEqual(self.adapter.count_rows("users", "O'Reilly"), 1)

    def test_explicit_filter_and_safe_sorting(self):
        result = self.adapter.fetch_page(
            "users",
            limit=10,
            offset=0,
            filter_query="department_id = 1",
            order_by=("name", "DESC"),
        )
        self.assertEqual([row[2] for row in result.rows], ["O'Reilly", "Ada"])
        with self.assertRaises(AdapterError):
            self.adapter.fetch_page(
                "users", 10, 0, order_by=("name; DROP TABLE users", "ASC")
            )

    def test_schema_and_foreign_keys(self):
        schema = self.adapter.schema_info("users")
        self.assertEqual(schema.headers[-1], "PK")
        self.assertEqual(schema.rows[0][-1], "YES")
        self.assertEqual(
            self.adapter.foreign_keys("users"),
            [{"from_col": "department_id", "to_table": "departments", "to_col": "id"}],
        )

    def test_crud_binds_values_and_serializes_json(self):
        dangerous_name = "Robert'); DROP TABLE users; --"
        self.assertEqual(
            self.adapter.insert_row(
                "users",
                {
                    "department_id": 1,
                    "name": dangerous_name,
                    "metadata": {"tags": ["one", "two"]},
                },
            ),
            1,
        )
        self.assertEqual(self.adapter.count_rows("users"), 3)

        result = self.adapter.fetch_page(
            "users", 10, 0, filter_query=dangerous_name
        )
        self.assertEqual(len(result.rows), 1)
        row = dict(zip(result.headers, result.rows[0]))
        self.assertEqual(
            self.adapter.update_row(
                "users",
                row,
                {**row, "name": "Updated O'Reilly", "metadata": {"active": True}},
            ),
            1,
        )
        updated = self.adapter.fetch_page("users", 10, 0, filter_query="Updated O'Reilly")
        self.assertEqual(len(updated.rows), 1)
        self.assertEqual(json.loads(updated.rows[0][3]), {"active": True})

        updated_row = dict(zip(updated.headers, updated.rows[0]))
        self.assertEqual(self.adapter.delete_row("users", updated_row), 1)
        self.assertEqual(self.adapter.count_rows("users"), 2)
        # The quote-heavy value did not become executable SQL.
        self.assertEqual(self.adapter.list_tables()[-1], "users")

    def test_update_noop_and_no_primary_key_are_safe(self):
        original = {"id": 1, "department_id": 1, "name": "Ada", "metadata": '{"role":"admin"}'}
        self.assertEqual(self.adapter.update_row("users", original, original), 0)
        with self.assertRaises(RowIdentityError):
            self.adapter.delete_row("duplicate_values", {"name": "same", "value": "one"})
        with self.assertRaises(RowIdentityError):
            self.adapter.update_row(
                "duplicate_values",
                {"name": "same", "value": "one"},
                {"name": "changed", "value": "one"},
            )

    def test_readonly_adapter_and_readonly_query_guard(self):
        read_only = SQLiteAdapter(self.db_path, read_only=True)
        result = read_only.execute_readonly(
            "SELECT name FROM users WHERE id = ?", (1,), limit=10
        )
        self.assertEqual(result.rows, [("Ada",)])
        with self.assertRaises(ReadOnlyError):
            read_only.insert_row("users", {"name": "blocked"})
        with self.assertRaises(UnsupportedQueryError):
            read_only.execute_readonly("DELETE FROM users WHERE id = 1")
        read_only.close()

    def test_readonly_query_is_bounded_and_rejects_multiple_statements(self):
        result = self.adapter.execute_readonly(
            "SELECT id, name FROM users ORDER BY id", limit=1, offset=1
        )
        self.assertEqual(result.rows, [(2, "O'Reilly")])
        with self.assertRaises(AdapterError):
            self.adapter.execute_readonly("SELECT 1; SELECT 2")


if __name__ == "__main__":
    unittest.main()
