"""Export query results to common interchange formats."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Sequence


def _output_path(filename_prefix: str, suffix: str, output_dir: str | Path | None) -> Path:
    directory = Path.cwd() if output_dir is None else Path(output_dir)
    return directory / f"{filename_prefix}.{suffix}"


def _format_sql_value(value: Any) -> str:
    """Format a value for a portable SQL INSERT export.

    This is for a generated dump file, not for executing user-controlled SQL.
    Database-specific importers should still validate the target schema.
    """
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False)
    return "'" + str(value).replace("'", "''") + "'"


def _decode_json_value(value: Any) -> Any:
    if isinstance(value, str) and (
        (value.startswith("{") and value.endswith("}"))
        or (value.startswith("[") and value.endswith("]"))
    ):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            pass
    return value


def export_rows_to_json(
    headers: Sequence[str],
    rows: Sequence[Sequence[Any]],
    filename_prefix: str = "export",
    output_dir: str | Path | None = None,
) -> Path:
    """Export rows as formatted JSON objects."""
    export_data = [
        {header: _decode_json_value(value) for header, value in zip(headers, row)}
        for row in rows
    ]
    export_path = _output_path(filename_prefix, "json", output_dir)
    with export_path.open("w", encoding="utf-8") as output_file:
        json.dump(export_data, output_file, indent=2, ensure_ascii=False)
    return export_path


def export_rows_to_csv(
    headers: Sequence[str],
    rows: Sequence[Sequence[Any]],
    filename_prefix: str = "export",
    output_dir: str | Path | None = None,
) -> Path:
    """Export rows as CSV."""
    export_path = _output_path(filename_prefix, "csv", output_dir)
    with export_path.open("w", newline="", encoding="utf-8") as output_file:
        writer = csv.writer(output_file)
        writer.writerow(headers)
        writer.writerows(rows)
    return export_path


def export_rows_to_sql(
    headers: Sequence[str],
    rows: Sequence[Sequence[Any]],
    table_name: str,
    filename_prefix: str = "export",
    output_dir: str | Path | None = None,
) -> Path:
    """Export rows as SQL INSERT statements."""
    export_path = _output_path(filename_prefix, "sql", output_dir)
    quoted_columns = ", ".join(f'"{header.replace(chr(34), chr(34) * 2)}"' for header in headers)
    quoted_table = '"' + table_name.replace('"', '""') + '"'

    with export_path.open("w", encoding="utf-8") as output_file:
        for row in rows:
            values = ", ".join(_format_sql_value(value) for value in row)
            output_file.write(
                f"INSERT INTO {quoted_table} ({quoted_columns}) VALUES ({values});\n"
            )
    return export_path
