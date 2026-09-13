"""SQLite database adapter used by the terminal browser."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence
from urllib.parse import quote

from .base import (
    AdapterError,
    DatabaseAdapter,
    QueryResult,
    ReadOnlyError,
    UnsupportedQueryError,
)
from ..query import (
    build_filter_clause,
    build_order_clause,
    build_primary_key_where,
    quote_identifier,
    validate_page_bounds,
)


_READ_QUERY_RE = re.compile(r"^(?:SELECT|WITH|EXPLAIN)\b", re.IGNORECASE)

# SQLite authorizer actions that can mutate a database.  The getattr calls
# keep this module compatible with Python builds exposing slightly different
# SQLite constants.
_WRITE_ACTIONS = {
    getattr(sqlite3, name)
    for name in (
        "SQLITE_INSERT",
        "SQLITE_UPDATE",
        "SQLITE_DELETE",
        "SQLITE_CREATE_INDEX",
        "SQLITE_CREATE_TABLE",
        "SQLITE_CREATE_TEMP_INDEX",
        "SQLITE_CREATE_TEMP_TABLE",
        "SQLITE_CREATE_TEMP_TRIGGER",
        "SQLITE_CREATE_TEMP_VIEW",
        "SQLITE_CREATE_TRIGGER",
        "SQLITE_CREATE_VIEW",
        "SQLITE_DROP_INDEX",
        "SQLITE_DROP_TABLE",
        "SQLITE_DROP_TEMP_INDEX",
        "SQLITE_DROP_TEMP_TABLE",
        "SQLITE_DROP_TEMP_TRIGGER",
        "SQLITE_DROP_TEMP_VIEW",
        "SQLITE_DROP_TRIGGER",
        "SQLITE_DROP_VIEW",
        "SQLITE_ALTER_TABLE",
        "SQLITE_REINDEX",
        "SQLITE_ANALYZE",
        "SQLITE_ATTACH",
        "SQLITE_DETACH",
    )
    if hasattr(sqlite3, name)
}


class SQLiteAdapter(DatabaseAdapter):
    """Short-lived-connection SQLite adapter.

    Each operation opens its own connection, enables foreign-key enforcement,
    and closes the connection before returning.  Writes use bound values and
    primary-key identities; tables without a primary key are deliberately
    refused for update/delete operations.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        read_only: bool = False,
        timeout: float = 5.0,
        check_same_thread: bool = True,
    ) -> None:
        self._path_value = str(path)
        self._read_only = bool(read_only)
        self.timeout = timeout
        self.check_same_thread = check_same_thread

    @property
    def engine(self) -> str:
        return "sqlite"

    @property
    def read_only(self) -> bool:
        return self._read_only

    def list_databases(self) -> list[str]:
        return [self._path_value]

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        try:
            if self._path_value == ":memory:":
                if self._read_only:
                    raise AdapterError("An in-memory SQLite database cannot be read-only")
                connection = sqlite3.connect(
                    ":memory:",
                    timeout=self.timeout,
                    check_same_thread=self.check_same_thread,
                )
            elif self._read_only:
                resolved_path = Path(self._path_value).expanduser().resolve()
                uri = f"file:{quote(str(resolved_path), safe='/')}?mode=ro"
                connection = sqlite3.connect(
                    uri,
                    uri=True,
                    timeout=self.timeout,
                    check_same_thread=self.check_same_thread,
                )
            else:
                connection = sqlite3.connect(
                    self._path_value,
                    timeout=self.timeout,
                    check_same_thread=self.check_same_thread,
                )
        except (sqlite3.Error, OSError) as exc:
            raise AdapterError(f"Unable to open SQLite database {self._path_value!r}: {exc}") from exc

        try:
            # SQLite requires this setting on every connection.  The previous
            # script only enabled foreign-key inspection, not enforcement.
            connection.execute("PRAGMA foreign_keys = ON")
            yield connection
        except AdapterError:
            raise
        except sqlite3.Error as exc:
            raise AdapterError(f"SQLite error: {exc}") from exc
        finally:
            connection.close()

    @staticmethod
    def _validate_table_name(table_name: str) -> None:
        # quote_identifier performs the actual escaping.  This small check
        # gives callers a useful error before opening a connection.
        quote_identifier(table_name, "sqlite")

    @staticmethod
    def _validate_page(limit: int, offset: int) -> tuple[int, int]:
        return validate_page_bounds(limit, offset)

    def _table_info(self, connection: sqlite3.Connection, table_name: str) -> list[tuple[Any, ...]]:
        self._validate_table_name(table_name)
        quoted_table = quote_identifier(table_name, "sqlite")
        rows = connection.execute(f"PRAGMA table_info({quoted_table})").fetchall()
        if not rows:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type IN ('table', 'view') AND name = ? LIMIT 1",
                (table_name,),
            ).fetchone()
            if not exists:
                raise AdapterError(f"Unknown SQLite table: {table_name}")
        return rows

    def _column_names(
        self, connection: sqlite3.Connection, table_name: str
    ) -> list[str]:
        return [str(row[1]) for row in self._table_info(connection, table_name)]

    def _primary_key_columns(
        self, connection: sqlite3.Connection, table_name: str
    ) -> list[str]:
        info = self._table_info(connection, table_name)
        # PRAGMA table_info column 5 is the 1-based PK position.  Sorting is
        # required for composite keys.
        primary_keys = [(int(row[5]), str(row[1])) for row in info if row[5]]
        return [column for _, column in sorted(primary_keys)]

    @staticmethod
    def _normalize_value(value: Any) -> Any:
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        return value

    def _ensure_writable(self) -> None:
        if self._read_only:
            raise ReadOnlyError("This SQLite connection is read-only")

    @staticmethod
    def _result(cursor: sqlite3.Cursor, rows: Sequence[Sequence[Any]]) -> QueryResult:
        headers = [description[0] for description in cursor.description or []]
        return QueryResult(headers, [tuple(row) for row in rows])

    def list_tables(self, include_views: bool = False) -> list[str]:
        if include_views:
            type_clause = "type IN ('table', 'view')"
        else:
            type_clause = "type = 'table'"
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT name FROM sqlite_master WHERE {type_clause} "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        return [str(row[0]) for row in rows]

    def _filter_and_order(
        self,
        connection: sqlite3.Connection,
        table_name: str,
        filter_query: str | None,
        order_by: tuple[str, str] | None,
    ) -> tuple[str, list[Any], str]:
        columns = self._column_names(connection, table_name)
        where_sql, params = build_filter_clause(
            filter_query, columns, dialect="sqlite"
        )
        order_sql = build_order_clause(order_by, columns, dialect="sqlite")
        return where_sql, params, order_sql

    def fetch_page(
        self,
        table_name: str,
        limit: int,
        offset: int,
        filter_query: str | None = None,
        order_by: tuple[str, str] | None = None,
    ) -> QueryResult:
        limit, offset = self._validate_page(limit, offset)
        with self._connection() as connection:
            where_sql, params, order_sql = self._filter_and_order(
                connection, table_name, filter_query, order_by
            )
            sql = (
                f"SELECT * FROM {quote_identifier(table_name, 'sqlite')} "
                f"{where_sql} {order_sql} LIMIT ? OFFSET ?"
            )
            cursor = connection.execute(sql, [*params, limit, offset])
            return self._result(cursor, cursor.fetchall())

    def count_rows(
        self, table_name: str, filter_query: str | None = None
    ) -> int:
        with self._connection() as connection:
            where_sql, params, _ = self._filter_and_order(
                connection, table_name, filter_query, None
            )
            cursor = connection.execute(
                f"SELECT COUNT(*) FROM {quote_identifier(table_name, 'sqlite')} {where_sql}",
                params,
            )
            return int(cursor.fetchone()[0])

    def schema_info(self, table_name: str) -> QueryResult:
        with self._connection() as connection:
            rows = self._table_info(connection, table_name)
        schema_rows = [
            (
                str(row[0]),
                str(row[1]),
                str(row[2] or "TEXT"),
                "YES" if row[3] else "NO",
                str(row[4]) if row[4] is not None else "NULL",
                "YES" if row[5] else "NO",
            )
            for row in rows
        ]
        return QueryResult(
            ["CID", "Column Name", "Type", "NotNull", "Default", "PK"],
            schema_rows,
        )

    def foreign_keys(self, table_name: str) -> list[dict[str, str]]:
        with self._connection() as connection:
            self._table_info(connection, table_name)
            rows = connection.execute(
                f"PRAGMA foreign_key_list({quote_identifier(table_name, 'sqlite')})"
            ).fetchall()
        return [
            {"from_col": str(row[3]), "to_table": str(row[2]), "to_col": str(row[4])}
            for row in rows
        ]

    def insert_row(self, table_name: str, row: Mapping[str, Any]) -> int:
        self._ensure_writable()
        if not isinstance(row, Mapping):
            raise TypeError("row must be a mapping")

        with self._connection() as connection:
            columns = self._column_names(connection, table_name)
            unknown_columns = set(row) - set(columns)
            if unknown_columns:
                raise AdapterError(
                    f"Unknown column(s) for {table_name}: {sorted(unknown_columns)!r}"
                )

            if row:
                column_names = list(row)
                quoted_columns = ", ".join(
                    quote_identifier(column, "sqlite") for column in column_names
                )
                placeholders = ", ".join("?" for _ in column_names)
                sql = (
                    f"INSERT INTO {quote_identifier(table_name, 'sqlite')} "
                    f"({quoted_columns}) VALUES ({placeholders})"
                )
                values = [self._normalize_value(row[column]) for column in column_names]
            else:
                sql = f"INSERT INTO {quote_identifier(table_name, 'sqlite')} DEFAULT VALUES"
                values = []

            try:
                cursor = connection.execute(sql, values)
                connection.commit()
                return max(cursor.rowcount, 0)
            except sqlite3.Error as exc:
                connection.rollback()
                raise AdapterError(f"SQLite insert failed: {exc}") from exc

    def update_row(
        self,
        table_name: str,
        original_row: Mapping[str, Any],
        updated_row: Mapping[str, Any],
    ) -> int:
        self._ensure_writable()
        if not isinstance(original_row, Mapping) or not isinstance(updated_row, Mapping):
            raise TypeError("original_row and updated_row must be mappings")

        with self._connection() as connection:
            columns = self._column_names(connection, table_name)
            primary_keys = self._primary_key_columns(connection, table_name)
            unknown_columns = set(updated_row) - set(columns)
            if unknown_columns:
                raise AdapterError(
                    f"Unknown column(s) for {table_name}: {sorted(unknown_columns)!r}"
                )
            where_sql, where_params = build_primary_key_where(
                original_row, primary_keys, dialect="sqlite"
            )
            changed = {
                column: value
                for column, value in updated_row.items()
                if original_row.get(column) != value
            }
            if not changed:
                return 0

            assignments = ", ".join(
                f"{quote_identifier(column, 'sqlite')} = ?" for column in changed
            )
            values = [self._normalize_value(value) for value in changed.values()]
            sql = (
                f"UPDATE {quote_identifier(table_name, 'sqlite')} SET {assignments} "
                f"WHERE {where_sql}"
            )
            try:
                cursor = connection.execute(sql, [*values, *where_params])
                connection.commit()
                return max(cursor.rowcount, 0)
            except sqlite3.Error as exc:
                connection.rollback()
                raise AdapterError(f"SQLite update failed: {exc}") from exc

    def delete_row(self, table_name: str, original_row: Mapping[str, Any]) -> int:
        self._ensure_writable()
        if not isinstance(original_row, Mapping):
            raise TypeError("original_row must be a mapping")

        with self._connection() as connection:
            primary_keys = self._primary_key_columns(connection, table_name)
            where_sql, where_params = build_primary_key_where(
                original_row, primary_keys, dialect="sqlite"
            )
            sql = (
                f"DELETE FROM {quote_identifier(table_name, 'sqlite')} "
                f"WHERE {where_sql}"
            )
            try:
                cursor = connection.execute(sql, where_params)
                connection.commit()
                return max(cursor.rowcount, 0)
            except sqlite3.Error as exc:
                connection.rollback()
                raise AdapterError(f"SQLite delete failed: {exc}") from exc

    @staticmethod
    def _readonly_authorizer(
        action: int,
        arg1: str | None,
        arg2: str | None,
        database: str | None,
        trigger: str | None,
    ) -> int:
        if action in _WRITE_ACTIONS:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def execute_readonly(
        self,
        sql: str,
        params: Sequence[Any] = (),
        limit: int = 50,
        offset: int = 0,
    ) -> QueryResult:
        limit, offset = self._validate_page(limit, offset)
        if not isinstance(sql, str) or not sql.strip():
            raise UnsupportedQueryError("A non-empty read-only SQL query is required")

        statement = sql.strip().rstrip(";").strip()
        if not _READ_QUERY_RE.match(statement):
            raise UnsupportedQueryError(
                "Only SELECT, WITH and EXPLAIN statements are allowed here"
            )

        with self._connection() as connection:
            connection.set_authorizer(self._readonly_authorizer)
            try:
                if statement.upper().startswith("EXPLAIN"):
                    cursor = connection.execute(statement, tuple(params))
                    rows = cursor.fetchall()[offset : offset + limit]
                    return self._result(cursor, rows)

                wrapped_sql = (
                    f"SELECT * FROM ({statement}) AS _dbcli_query "
                    "LIMIT ? OFFSET ?"
                )
                cursor = connection.execute(
                    wrapped_sql, [*params, limit, offset]
                )
                return self._result(cursor, cursor.fetchall())
            except sqlite3.Error as exc:
                raise AdapterError(f"SQLite read-only query failed: {exc}") from exc
            finally:
                connection.set_authorizer(None)
