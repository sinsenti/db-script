"""Native MySQL adapter using mysql-connector-python."""

from __future__ import annotations

from typing import Any, Callable
from urllib.parse import unquote, urlparse

from .base import DriverUnavailable
from .dbapi import DBAPIAdapter


class MySQLAdapter(DBAPIAdapter):
    """MySQL/MariaDB adapter backed by ``mysql-connector-python``.

    Credentials are passed to the driver rather than put in a subprocess
    command line.  The driver is imported lazily for SQLite-only installs.
    """

    dialect = "mysql"
    placeholder = "%s"

    def __init__(
        self,
        connection_url: str,
        *,
        read_only: bool = False,
        connection_factory: Callable[[], Any] | None = None,
    ) -> None:
        parsed = urlparse(connection_url)
        database = unquote(parsed.path.lstrip("/"))
        if not database and connection_factory is None:
            raise ValueError("MySQL connection URL must include a database name")

        if connection_factory is None:
            try:
                import mysql.connector
            except ImportError as exc:
                raise DriverUnavailable(
                    "MySQL support requires mysql-connector-python; install the optional mysql dependency"
                ) from exc

            connection_options: dict[str, Any] = {
                "host": parsed.hostname or "localhost",
                "port": parsed.port or 3306,
                "user": unquote(parsed.username or ""),
                "password": unquote(parsed.password or ""),
                "database": database,
                "autocommit": False,
            }
            connection_factory = lambda: mysql.connector.connect(**connection_options)

        self.connection_url = connection_url
        self.database = database
        super().__init__(
            connection_factory,
            schema=database,
            read_only=read_only,
        )

    @property
    def engine(self) -> str:
        return "mysql"

    def _current_database_sql(self) -> str:
        return "SELECT DATABASE()"

    def foreign_keys(self, table_name: str) -> list[dict[str, str]]:
        with self._cursor() as (_, cursor):
            self._metadata_columns(cursor, table_name)
            cursor.execute(
                "SELECT column_name, referenced_table_name, referenced_column_name "
                "FROM information_schema.key_column_usage "
                f"WHERE table_schema = {self.placeholder} "
                f"AND table_name = {self.placeholder} "
                "AND referenced_table_name IS NOT NULL "
                "ORDER BY ordinal_position",
                (self.schema, table_name),
            )
            rows = cursor.fetchall()
        return [
            {"from_col": str(row[0]), "to_table": str(row[1]), "to_col": str(row[2])}
            for row in rows
        ]
