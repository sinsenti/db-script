"""Common adapter contracts and result/error types.

The terminal UI should talk to this interface instead of knowing how a
particular database is connected.  New database engines can be added without
changing the browser controller.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


class AdapterError(RuntimeError):
    """Base exception for expected database-adapter failures."""


class InvalidIdentifierError(AdapterError, ValueError):
    """Raised when a table or column identifier cannot be safely composed."""


class ReadOnlyError(AdapterError):
    """Raised when a write is attempted through a read-only adapter."""


class RowIdentityError(AdapterError):
    """Raised when a row cannot be addressed unambiguously."""


class UnsupportedQueryError(AdapterError, ValueError):
    """Raised when a query is not allowed by a read-only query API."""


@dataclass(frozen=True)
class QueryResult:
    """A database result in the shape consumed by the terminal renderer."""

    headers: list[str]
    rows: list[tuple[Any, ...]]

    def as_tuple(self) -> tuple[list[str], list[tuple[Any, ...]]]:
        """Return the legacy ``(headers, rows)`` shape used by ``db.py``."""
        return self.headers, self.rows


class DatabaseAdapter(ABC):
    """Backend contract used by the database browser.

    Mutating methods return the number of affected rows.  Returning zero is a
    normal result for a no-op; adapters should raise ``AdapterError`` for
    connection, validation or transaction failures.
    """

    @property
    @abstractmethod
    def engine(self) -> str:
        """Short engine name, for example ``sqlite`` or ``postgres``."""

    @property
    @abstractmethod
    def read_only(self) -> bool:
        """Whether the adapter refuses all writes."""

    @abstractmethod
    def list_databases(self) -> list[str]:
        """Return databases represented by this adapter."""

    @abstractmethod
    def list_tables(self, include_views: bool = False) -> list[str]:
        """Return visible table names."""

    @abstractmethod
    def fetch_page(
        self,
        table_name: str,
        limit: int,
        offset: int,
        filter_query: str | None = None,
        order_by: tuple[str, str] | None = None,
    ) -> QueryResult:
        """Fetch one page of rows."""

    @abstractmethod
    def count_rows(
        self, table_name: str, filter_query: str | None = None
    ) -> int:
        """Count rows using the same filter semantics as ``fetch_page``."""

    @abstractmethod
    def schema_info(self, table_name: str) -> QueryResult:
        """Return schema metadata as a tabular result."""

    @abstractmethod
    def foreign_keys(self, table_name: str) -> list[dict[str, str]]:
        """Return foreign-key relationships."""

    @abstractmethod
    def insert_row(self, table_name: str, row: Mapping[str, Any]) -> int:
        """Insert a row and return the affected-row count."""

    @abstractmethod
    def update_row(
        self,
        table_name: str,
        original_row: Mapping[str, Any],
        updated_row: Mapping[str, Any],
    ) -> int:
        """Update a row identified by its primary key."""

    @abstractmethod
    def delete_row(self, table_name: str, original_row: Mapping[str, Any]) -> int:
        """Delete a row identified by its primary key."""

    @abstractmethod
    def execute_readonly(
        self,
        sql: str,
        params: Sequence[Any] = (),
        limit: int = 50,
        offset: int = 0,
    ) -> QueryResult:
        """Execute a read-only query and return a bounded result."""

    def close(self) -> None:
        """Release adapter resources.

        The default implementation is suitable for adapters that open a
        short-lived connection per operation.
        """
