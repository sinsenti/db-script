#!/usr/bin/env python3
"""JSONL bridge to the production Python SQLiteAdapter and UI renderer."""
from __future__ import annotations

import argparse
import base64
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from dbcli.adapters.sqlite import SQLiteAdapter  # noqa: E402
from dbcli.ui import build_ascii_table_lines, get_fitted_columns, strip_ansi  # noqa: E402

TRACE: list[dict[str, Any]] = []
_ORIGINAL_CONNECT = sqlite3.connect


def encode(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"$blob": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, dict):
        return {str(k): encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [encode(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


class TrackedConnection(sqlite3.Connection):
    def execute(self, sql: str, parameters=(), /):
        TRACE.append({"sql": sql, "params": encode(list(parameters))})
        return super().execute(sql, parameters)


def tracked_connect(*args, **kwargs):
    kwargs["factory"] = TrackedConnection
    return _ORIGINAL_CONNECT(*args, **kwargs)


def decode(value: Any) -> Any:
    if isinstance(value, dict):
        if set(value) == {"$blob"}:
            return base64.b64decode(value["$blob"], validate=True)
        return {k: decode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [decode(v) for v in value]
    return value


def result_obj(result) -> dict[str, Any]:
    return {"headers": list(result.headers), "rows": encode(result.rows)}


def run(adapter: SQLiteAdapter, request: dict[str, Any]) -> Any:
    op = request["op"]
    table = request.get("table")
    if op == "list_tables":
        return adapter.list_tables(include_views=bool(request.get("include_views", False)))
    if op == "schema":
        return result_obj(adapter.schema_info(table))
    if op == "foreign_keys":
        return adapter.foreign_keys(table)
    if op == "page":
        order = request.get("sort")
        return result_obj(adapter.fetch_page(table, request["limit"], request["offset"],
                                             request.get("filter"), tuple(order) if order else None))
    if op == "count":
        return adapter.count_rows(table, request.get("filter"))
    if op == "insert":
        return adapter.insert_row(table, decode(request["row"]))
    if op == "update":
        return adapter.update_row(table, decode(request["original"]), decode(request["updated"]))
    if op == "delete":
        return adapter.delete_row(table, decode(request["original"]))
    if op == "raw":
        result = adapter.execute_readonly(request["sql"], decode(request.get("params", [])),
                                          request.get("limit", 50), request.get("offset", 0))
        return result_obj(result)
    if op == "render":
        order = request.get("sort")
        order = tuple(order) if order else None
        page = adapter.fetch_page(table, request["limit"], request["offset"],
                                  request.get("filter"), order)
        count = adapter.count_rows(table, request.get("filter"))
        width, height = int(request["width"]), int(request["height"])
        c_start, c_end, widths = get_fitted_columns(page.headers, page.rows, 0, width)
        sliced_headers = page.headers[c_start:c_end]
        sliced_rows = [row[c_start:c_end] for row in page.rows]
        sort_col, sort_dir = order if order else (None, None)
        lines = [
            f" Data Browser: {table} [Rows {request['offset'] + 1}-{request['offset'] + len(page.rows)} of {count}] "
            f"[Cols {c_start + 1}-{c_end}/{len(page.headers)}]"
            + (f" [Sort: {sort_col} {sort_dir}]" if order else "")
            + (f" (Filter: '{request['filter']}')" if request.get("filter") else ""),
        ]
        lines.extend(build_ascii_table_lines(
            sliced_headers, sliced_rows, widths, cursor_idx=0,
            focused_col_idx=0, c_start=c_start, sort_col=sort_col, sort_dir=sort_dir))
        lines.append(" Vim Nav: j/k Up/Down | l/Enter Card | H/L Focus | gg/G Jump | a Add | e Edit | E Bulk Edit | dd Del | o/O Sort | v Visibility | SPACE Mark | f FK | S/I Schema/Indexes | : SQL/Jump | x/X Export | h/q Back")
        return {"screen": "\n".join(strip_ansi(line) for line in lines[:height])}
    raise ValueError(f"Unknown operation: {op}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--read-only", action="store_true")
    args = parser.parse_args()
    sqlite3.connect = tracked_connect
    adapter = SQLiteAdapter(args.db, read_only=args.read_only)
    for line in sys.stdin:
        try:
            request = json.loads(line)
            TRACE.clear()
            value = run(adapter, request)
            response = {"ok": True, "result": encode(value), "trace": list(TRACE)}
        except Exception as exc:
            response = {"ok": False,
                        "error": {"class": type(exc).__name__, "message": str(exc)},
                        "trace": list(TRACE)}
        sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
