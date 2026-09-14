"""Native PostgreSQL adapter using psycopg 3."""

from __future__ import annotations

from typing import Any, Callable

from .base import DriverUnavailable
from .dbapi import DBAPIAdapter


class PostgresAdapter(DBAPIAdapter):
    """PostgreSQL adapter backed by ``psycopg``.

    ``psycopg`` is imported lazily so SQLite-only users do not need to install
    a PostgreSQL driver.  Pass ``connection_factory`` in tests or when an
    application already owns a configured connection pool.
    """

    dialect = "postgres"
    placeholder = "%s"

    def __init__(
        self,
        dsn: str,
        *,
        schema: str = "public",
        read_only: bool = False,
        connection_factory: Callable[[], Any] | None = None,
    ) -> None:
        if connection_factory is None:
            try:
                import psycopg
            except ImportError as exc:
                raise DriverUnavailable(
                    "PostgreSQL support requires psycopg; install the optional postgres dependency"
                ) from exc
            connection_factory = lambda: psycopg.connect(dsn)

        self.dsn = dsn
        super().__init__(
            connection_factory,
            schema=schema,
            read_only=read_only,
        )

    @property
    def engine(self) -> str:
        return "postgres"

    def _current_database_sql(self) -> str:
        return "SELECT current_database()"

    def foreign_keys(self, table_name: str) -> list[dict[str, str]]:
        with self._cursor() as (_, cursor):
            self._metadata_columns(cursor, table_name)
            cursor.execute(
                "SELECT kcu.column_name, ccu.table_name, ccu.column_name "
                "FROM information_schema.table_constraints tc "
                "JOIN information_schema.key_column_usage kcu "
                "ON kcu.constraint_schema = tc.constraint_schema "
                "AND kcu.constraint_name = tc.constraint_name "
                "AND kcu.table_schema = tc.table_schema "
                "AND kcu.table_name = tc.table_name "
                "JOIN information_schema.constraint_column_usage ccu "
                "ON ccu.constraint_schema = tc.constraint_schema "
                "AND ccu.constraint_name = tc.constraint_name "
                "AND ccu.table_schema = tc.table_schema "
                f"WHERE tc.constraint_schema = {self.placeholder} "
                f"AND tc.table_schema = {self.placeholder} "
                f"AND tc.table_name = {self.placeholder} "
                "AND tc.constraint_type = 'FOREIGN KEY' "
                "ORDER BY kcu.ordinal_position",
                (self.schema, self.schema, table_name),
            )
            rows = cursor.fetchall()
        return [
            {"from_col": str(row[0]), "to_table": str(row[1]), "to_col": str(row[2])}
            for row in rows
        ]
