"""Shared implementation for native DB-API adapters."""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Mapping, Sequence

from ..query import (
    build_filter_clause,
    build_order_clause,
    build_primary_key_where,
    quote_identifier,
    validate_page_bounds,
)
from .base import (
    AdapterError,
    DatabaseAdapter,
    QueryResult,
    ReadOnlyError,
    UnsupportedQueryError,
)


_READ_QUERY_RE = re.compile(r"^(?:SELECT|WITH|EXPLAIN)\b", re.IGNORECASE)
_MUTATING_QUERY_RE = re.compile(
    r"\b(?:INSERT|UPDATE|DELETE|ALTER|DROP|CREATE|TRUNCATE|GRANT|REVOKE|CALL|DO)\b",
    re.IGNORECASE,
)
_SQL_LITERAL_RE = re.compile(
    r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`",
    re.DOTALL,
)
_SQL_COMMENT_RE = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)


def _contains_mutating_keyword(statement: str) -> bool:
    """Detect DML/DDL while ignoring quoted values, identifiers and comments."""
    without_comments = _SQL_COMMENT_RE.sub(" ", statement)
    without_literals = _SQL_LITERAL_RE.sub(" ", without_comments)
    return bool(_MUTATING_QUERY_RE.search(without_literals))


def _fetch_bounded(cursor: Any, offset: int, limit: int) -> list[tuple[Any, ...]]:
    """Read only the requested EXPLAIN window without storing all rows."""
    for _ in range(offset):
        if cursor.fetchone() is None:
            return []

    rows: list[tuple[Any, ...]] = []
    for _ in range(limit):
        row = cursor.fetchone()
        if row is None:
            break
        rows.append(tuple(row))
    return rows


class DBAPIAdapter(DatabaseAdapter):
    """Common CRUD, paging and metadata behavior for DB-API drivers.

    Subclasses provide the engine name, current-database query and foreign-key
    metadata query.  The connection factory is injectable, which makes the
    adapter testable without requiring a live server.
    """

    dialect = "sqlite"
    placeholder = "?"

    def __init__(
        self,
        connection_factory: Callable[[], Any],
        *,
        schema: str,
        read_only: bool = False,
    ) -> None:
        self._connection_factory = connection_factory
        self.schema = schema
        self._read_only = bool(read_only)

    @property
    def read_only(self) -> bool:
        return self._read_only

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        try:
            connection = self._connection_factory()
        except Exception as exc:
            raise AdapterError(f"Unable to connect to {self.engine}: {exc}") from exc

        try:
            yield connection
        except AdapterError:
            self._rollback(connection)
            raise
        except Exception as exc:
            self._rollback(connection)
            raise AdapterError(f"{self.engine} operation failed: {exc}") from exc
        finally:
            self._close(connection)

    @staticmethod
    def _rollback(connection: Any) -> None:
        rollback = getattr(connection, "rollback", None)
        if rollback:
            try:
                rollback()
            except Exception:
                pass

    @staticmethod
    def _commit(connection: Any) -> None:
        try:
            connection.commit()
        except Exception as exc:
            raise AdapterError(f"Database commit failed: {exc}") from exc

    @staticmethod
    def _close(connection: Any) -> None:
        close = getattr(connection, "close", None)
        if close:
            try:
                close()
            except Exception:
                pass

    @staticmethod
    def _close_cursor(cursor: Any) -> None:
        close = getattr(cursor, "close", None)
        if close:
            try:
                close()
            except Exception:
                pass

    @contextmanager
    def _cursor(self) -> Iterator[tuple[Any, Any]]:
        with self._connection() as connection:
            cursor = connection.cursor()
            try:
                yield connection, cursor
            finally:
                self._close_cursor(cursor)

    def _table_reference(self, table_name: str) -> str:
        return (
            f"{quote_identifier(self.schema, self.dialect)}."
            f"{quote_identifier(table_name, self.dialect)}"
        )

    def _metadata_columns(self, cursor: Any, table_name: str) -> list[str]:
        cursor.execute(
            "SELECT column_name FROM information_schema.columns "
            f"WHERE table_schema = {self.placeholder} "
            f"AND table_name = {self.placeholder} "
            "ORDER BY ordinal_position",
            (self.schema, table_name),
        )
        columns = [str(row[0]) for row in cursor.fetchall()]
        if not columns:
            raise AdapterError(f"Unknown {self.engine} table: {table_name}")
        return columns

    def list_databases(self) -> list[str]:
        with self._cursor() as (_, cursor):
            cursor.execute(self._current_database_sql())
            row = cursor.fetchone()
        return [str(row[0])] if row and row[0] else []

    def list_tables(self, include_views: bool = False) -> list[str]:
        table_types = ["BASE TABLE"]
        if include_views:
            table_types.append("VIEW")
        type_placeholders = ", ".join(self.placeholder for _ in table_types)
        params = [self.schema, *table_types]
        with self._cursor() as (_, cursor):
            cursor.execute(
                "SELECT table_name FROM information_schema.tables "
                f"WHERE table_schema = {self.placeholder} "
                f"AND table_type IN ({type_placeholders}) ORDER BY table_name",
                params,
            )
            rows = cursor.fetchall()
        return [str(row[0]) for row in rows]

    def _filter_and_order(
        self,
        cursor: Any,
        table_name: str,
        filter_query: str | None,
        order_by: tuple[str, str] | None,
    ) -> tuple[str, list[Any], str]:
        columns = self._metadata_columns(cursor, table_name)
        where_sql, params = build_filter_clause(
            filter_query, columns, dialect=self.dialect
        )
        # The shared helper uses DB-API placeholders for non-SQLite dialects.
        order_sql = build_order_clause(order_by, columns, dialect=self.dialect)
        return where_sql, params, order_sql

    @staticmethod
    def _headers(cursor: Any) -> list[str]:
        return [str(description[0]) for description in (cursor.description or [])]

    def fetch_page(
        self,
        table_name: str,
        limit: int,
        offset: int,
        filter_query: str | None = None,
        order_by: tuple[str, str] | None = None,
    ) -> QueryResult:
        limit, offset = validate_page_bounds(limit, offset)
        with self._cursor() as (_, cursor):
            where_sql, params, order_sql = self._filter_and_order(
                cursor, table_name, filter_query, order_by
            )
            sql = (
                f"SELECT * FROM {self._table_reference(table_name)} "
                f"{where_sql} {order_sql} "
                f"LIMIT {self.placeholder} OFFSET {self.placeholder}"
            )
            cursor.execute(sql, [*params, limit, offset])
            rows = [tuple(row) for row in cursor.fetchall()]
            return QueryResult(self._headers(cursor), rows)

    def count_rows(
        self, table_name: str, filter_query: str | None = None
    ) -> int:
        with self._cursor() as (_, cursor):
            where_sql, params, _ = self._filter_and_order(
                cursor, table_name, filter_query, None
            )
            cursor.execute(
                f"SELECT COUNT(*) FROM {self._table_reference(table_name)} {where_sql}",
                params,
            )
            row = cursor.fetchone()
        return int(row[0]) if row else 0

    def schema_info(self, table_name: str) -> QueryResult:
        with self._cursor() as (_, cursor):
            cursor.execute(
                "SELECT column_name, data_type, is_nullable, column_default "
                "FROM information_schema.columns "
                f"WHERE table_schema = {self.placeholder} "
                f"AND table_name = {self.placeholder} ORDER BY ordinal_position",
                (self.schema, table_name),
            )
            rows = [tuple(row) for row in cursor.fetchall()]
        if not rows:
            raise AdapterError(f"Unknown {self.engine} table: {table_name}")
        return QueryResult(
            ["Column Name", "Type", "Nullable", "Default"],
            rows,
        )

    def _primary_key_columns(self, cursor: Any, table_name: str) -> list[str]:
        cursor.execute(
            "SELECT kcu.column_name "
            "FROM information_schema.table_constraints tc "
            "JOIN information_schema.key_column_usage kcu "
            "ON kcu.constraint_schema = tc.constraint_schema "
            "AND kcu.constraint_name = tc.constraint_name "
            "AND kcu.table_schema = tc.table_schema "
            "AND kcu.table_name = tc.table_name "
            f"WHERE tc.constraint_schema = {self.placeholder} "
            f"AND tc.table_schema = {self.placeholder} "
            f"AND tc.table_name = {self.placeholder} "
            "AND tc.constraint_type = 'PRIMARY KEY' "
            "ORDER BY kcu.ordinal_position",
            (self.schema, self.schema, table_name),
        )
        return [str(row[0]) for row in cursor.fetchall()]

    def _normalize_value(self, value: Any) -> Any:
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        return value

    def _ensure_writable(self) -> None:
        if self.read_only:
            raise ReadOnlyError(f"This {self.engine} connection is read-only")

    def insert_row(self, table_name: str, row: Mapping[str, Any]) -> int:
        self._ensure_writable()
        if not isinstance(row, Mapping):
            raise TypeError("row must be a mapping")

        with self._cursor() as (connection, cursor):
            columns = self._metadata_columns(cursor, table_name)
            unknown_columns = set(row) - set(columns)
            if unknown_columns:
                raise AdapterError(
                    f"Unknown column(s) for {table_name}: {sorted(unknown_columns)!r}"
                )

            if row:
                names = list(row)
                quoted_columns = ", ".join(
                    quote_identifier(column, self.dialect) for column in names
                )
                placeholders = ", ".join(self.placeholder for _ in names)
                sql = (
                    f"INSERT INTO {self._table_reference(table_name)} "
                    f"({quoted_columns}) VALUES ({placeholders})"
                )
                values = [self._normalize_value(row[column]) for column in names]
            elif self.dialect == "mysql":
                sql = f"INSERT INTO {self._table_reference(table_name)} () VALUES ()"
                values = []
            else:
                sql = f"INSERT INTO {self._table_reference(table_name)} DEFAULT VALUES"
                values = []

            cursor.execute(sql, values)
            self._commit(connection)
            return max(int(getattr(cursor, "rowcount", 0)), 0)

    def update_row(
        self,
        table_name: str,
        original_row: Mapping[str, Any],
        updated_row: Mapping[str, Any],
    ) -> int:
        self._ensure_writable()
        if not isinstance(original_row, Mapping) or not isinstance(updated_row, Mapping):
            raise TypeError("original_row and updated_row must be mappings")

        with self._cursor() as (connection, cursor):
            columns = self._metadata_columns(cursor, table_name)
            primary_keys = self._primary_key_columns(cursor, table_name)
            unknown_columns = set(updated_row) - set(columns)
            if unknown_columns:
                raise AdapterError(
                    f"Unknown column(s) for {table_name}: {sorted(unknown_columns)!r}"
                )
            where_sql, where_params = build_primary_key_where(
                original_row,
                primary_keys,
                dialect=self.dialect,
                placeholder=self.placeholder,
            )
            changed = {
                column: value
                for column, value in updated_row.items()
                if original_row.get(column) != value
            }
            if not changed:
                return 0

            assignments = ", ".join(
                f"{quote_identifier(column, self.dialect)} = {self.placeholder}"
                for column in changed
            )
            values = [self._normalize_value(value) for value in changed.values()]
            cursor.execute(
                f"UPDATE {self._table_reference(table_name)} SET {assignments} "
                f"WHERE {where_sql}",
                [*values, *where_params],
            )
            self._commit(connection)
            return max(int(getattr(cursor, "rowcount", 0)), 0)

    def delete_row(self, table_name: str, original_row: Mapping[str, Any]) -> int:
        self._ensure_writable()
        if not isinstance(original_row, Mapping):
            raise TypeError("original_row must be a mapping")

        with self._cursor() as (connection, cursor):
            self._metadata_columns(cursor, table_name)
            primary_keys = self._primary_key_columns(cursor, table_name)
            where_sql, where_params = build_primary_key_where(
                original_row,
                primary_keys,
                dialect=self.dialect,
                placeholder=self.placeholder,
            )
            cursor.execute(
                f"DELETE FROM {self._table_reference(table_name)} WHERE {where_sql}",
                where_params,
            )
            self._commit(connection)
            return max(int(getattr(cursor, "rowcount", 0)), 0)

    def execute_readonly(
        self,
        sql: str,
        params: Sequence[Any] = (),
        limit: int = 50,
        offset: int = 0,
    ) -> QueryResult:
        limit, offset = validate_page_bounds(limit, offset)
        if not isinstance(sql, str) or not sql.strip():
            raise UnsupportedQueryError("A non-empty read-only SQL query is required")

        statement = sql.strip().rstrip(";").strip()
        if (
            not _READ_QUERY_RE.match(statement)
            or ";" in statement
            or _contains_mutating_keyword(statement)
        ):
            raise UnsupportedQueryError(
                "Only single-statement SELECT, read-only WITH and EXPLAIN queries are allowed here"
            )

        with self._cursor() as (_, cursor):
            if statement.upper().startswith("EXPLAIN"):
                cursor.execute(statement, tuple(params))
                return QueryResult(
                    self._headers(cursor), _fetch_bounded(cursor, offset, limit)
                )

            wrapped_sql = (
                f"SELECT * FROM ({statement}) AS _dbcli_query "
                f"LIMIT {self.placeholder} OFFSET {self.placeholder}"
            )
            cursor.execute(wrapped_sql, [*params, limit, offset])
            return QueryResult(
                self._headers(cursor),
                [tuple(row) for row in cursor.fetchall()],
            )

    def _current_database_sql(self) -> str:
        raise NotImplementedError

    def foreign_keys(self, table_name: str) -> list[dict[str, str]]:
        raise NotImplementedError
