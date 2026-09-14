"""Safe, database-neutral SQL composition helpers.

Values are always returned separately as parameters.  Only identifiers and
small, validated SQL fragments are composed into query strings.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from .adapters.base import InvalidIdentifierError, RowIdentityError


_SQL_EXPRESSION_MARKERS = re.compile(
    r"(?:"
    r"(?:!=|<=|>=|<>|=|<|>)|"
    r"::|->>|->|"
    r"\b(?:AND|BETWEEN|ILIKE|IN|IS|LIKE|NOT|OR)\b"
    r")",
    re.IGNORECASE,
)


def sanitize_where_clause(text: str | None) -> str | None:
    """Normalize an accidental duplicate quote around a JSON key."""
    if not text:
        return text
    return re.sub(r"(->>|->)\s*''([^']+)''", r"\1'\2'", text)


def is_sql_where_expression(text: str | None) -> bool:
    """Return whether input looks like an explicit SQL filter expression.

    This function only classifies input; it is not a SQL parser.  Expressions
    are intentionally supported for the existing interactive browser, while
    ordinary text searches are parameterized by the adapter.
    """
    return bool(text and _SQL_EXPRESSION_MARKERS.search(text))


def quote_identifier(identifier: str, dialect: str = "sqlite") -> str:
    """Quote one SQL identifier for a supported dialect.

    Identifiers cannot be bound as SQL parameters, so they must be validated
    and quoted separately from values.  Empty identifiers and NUL bytes are
    rejected rather than silently producing invalid SQL.
    """
    if not isinstance(identifier, str) or not identifier or "\x00" in identifier:
        raise InvalidIdentifierError(f"Invalid {dialect} identifier: {identifier!r}")

    if dialect.lower() == "mysql":
        return f"`{identifier.replace('`', '``')}`"
    if dialect.lower() in {"sqlite", "postgres", "postgresql"}:
        return f'"{identifier.replace(chr(34), chr(34) * 2)}"'
    raise InvalidIdentifierError(f"Unsupported SQL dialect: {dialect}")


def validate_page_bounds(limit: int, offset: int) -> tuple[int, int]:
    """Validate and normalize pagination values."""
    if isinstance(limit, bool) or isinstance(offset, bool):
        raise ValueError("limit and offset must be integers")
    try:
        normalized_limit = int(limit)
        normalized_offset = int(offset)
    except (TypeError, ValueError) as exc:
        raise ValueError("limit and offset must be integers") from exc
    if normalized_limit < 1:
        raise ValueError("limit must be greater than zero")
    if normalized_offset < 0:
        raise ValueError("offset cannot be negative")
    return normalized_limit, normalized_offset


def build_order_clause(
    order_by: tuple[str, str] | None,
    columns: Sequence[str],
    dialect: str = "sqlite",
) -> str:
    """Build an ORDER BY clause using an allow-listed column and direction."""
    if not order_by:
        return ""
    if len(order_by) != 2:
        raise ValueError("order_by must contain (column, direction)")

    column, direction = order_by
    allowed_columns = set(columns)
    if column not in allowed_columns:
        raise InvalidIdentifierError(f"Unknown sort column: {column!r}")

    normalized_direction = str(direction).upper()
    if normalized_direction not in {"ASC", "DESC"}:
        raise ValueError("sort direction must be ASC or DESC")
    return f"ORDER BY {quote_identifier(column, dialect)} {normalized_direction}"


def build_filter_clause(
    filter_query: str | None,
    columns: Sequence[str],
    dialect: str = "sqlite",
) -> tuple[str, list[Any]]:
    """Build a filter and its parameters.

    Plain text is searched across every column using bound parameters.  An
    input containing SQL operators is treated as an explicit SQL expression
    for compatibility with the interactive SQL-filter feature.  Callers
    should expose that behavior as a clearly labelled raw-filter mode.
    """
    if not filter_query:
        return "", []

    clean_filter = sanitize_where_clause(filter_query) or ""
    if is_sql_where_expression(clean_filter):
        # Explicit SQL entered by the user; no generated values are merged.
        return f"WHERE ({clean_filter})", []

    if not columns:
        return "WHERE 1 = 0", []

    normalized_dialect = dialect.lower()
    if normalized_dialect == "mysql":
        cast_type = "CHAR"
    else:
        cast_type = "TEXT"
    placeholder = "?" if normalized_dialect == "sqlite" else "%s"

    conditions = [
        f"CAST({quote_identifier(column, dialect)} AS {cast_type}) LIKE {placeholder}"
        for column in columns
    ]
    return "WHERE " + " OR ".join(conditions), [f"%{clean_filter}%"] * len(
        conditions
    )


def format_sql_literal(value: Any) -> str:
    """Format a value for a generated, internal SQL relation predicate.

    New database writes must use driver parameters.  This helper is retained
    only for composing a foreign-key browser filter before it is handed to a
    read query; it is not a general-purpose query API.
    """
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def build_primary_key_where(
    original_row: Mapping[str, Any],
    primary_key_columns: Sequence[str],
    dialect: str = "sqlite",
    placeholder: str = "?",
) -> tuple[str, list[Any]]:
    """Build a parameterized WHERE clause for a primary-key identity."""
    if not primary_key_columns:
        raise RowIdentityError(
            "This table has no primary key; refusing an ambiguous row mutation"
        )

    clauses: list[str] = []
    params: list[Any] = []
    for column in primary_key_columns:
        if column not in original_row:
            raise RowIdentityError(
                f"Original row does not contain primary-key column {column!r}"
            )
        value = original_row[column]
        if value is None:
            clauses.append(f"{quote_identifier(column, dialect)} IS NULL")
        else:
            clauses.append(f"{quote_identifier(column, dialect)} = {placeholder}")
            params.append(value)
    return " AND ".join(clauses), params
