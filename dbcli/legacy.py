"""Subprocess-backed compatibility paths for PostgreSQL and MySQL.

Native adapters are preferred for direct URLs.  These functions remain
intentionally isolated here for Docker-backed databases and the explicit
``--cli`` fallback, so the executable entry point does not contain a second
copy of the browser's database implementation.
"""

from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
import subprocess
from urllib.parse import unquote, urlparse

from .adapters.sqlite import SQLiteAdapter
from .query import is_sql_where_expression, sanitize_where_clause
from .ui import BOLD, CYAN, DIM, MAGENTA, RED, RESET, YELLOW, select_menu_item


_FK_CACHE = {}


_POSTGRES_RESERVED_WORDS = {
    "all", "and", "array", "as", "authorization", "between", "both",
    "case", "cast", "check", "column", "constraint", "create", "current_user",
    "default", "deferrable", "delete", "distinct", "do", "else", "end",
    "except", "false", "fetch", "for", "foreign", "from", "grant", "group",
    "having", "in", "initially", "intersect", "into", "join", "limit",
    "not", "null", "offset", "on", "only", "or", "order", "primary",
    "references", "returning", "select", "table", "then", "to", "true",
    "union", "unique", "update", "user", "using", "values", "when", "where",
    "window", "with",
}


def _parse_postgres_table_name(table_name):
    """Parse a table name emitted by the PostgreSQL browser's schema-qualified list."""
    components = []
    index = 0
    text = str(table_name)
    while index < len(text):
        if text[index] == '"':
            index += 1
            chars = []
            while index < len(text):
                if text[index] == '"':
                    if index + 1 < len(text) and text[index + 1] == '"':
                        chars.append('"')
                        index += 2
                    else:
                        index += 1
                        break
                else:
                    chars.append(text[index])
                    index += 1
            else:
                raise ValueError(f"Unterminated quoted PostgreSQL identifier: {text}")
            component = "".join(chars)
        else:
            end = text.find(".", index)
            if end == -1:
                end = len(text)
            component = text[index:end]
            index = end
        if not component:
            raise ValueError(f"Invalid PostgreSQL table name: {text}")
        components.append(component)
        if index == len(text):
            break
        if text[index] != ".":
            raise ValueError(f"Invalid PostgreSQL table name: {text}")
        index += 1
    if len(components) == 1:
        return "public", components[0]
    if len(components) == 2:
        return components[0], components[1]
    raise ValueError(f"Invalid PostgreSQL table name: {text}")


def _quote_postgres_identifier(identifier):
    return '"' + str(identifier).replace('"', '""') + '"'


def _postgres_table_reference(table_name):
    schema, table = _parse_postgres_table_name(table_name)
    return f"{_quote_postgres_identifier(schema)}.{_quote_postgres_identifier(table)}"


def _postgres_display_table_name(schema, table):
    def display_part(value):
        if (
            re.fullmatch(r"[a-z_][a-z0-9_$]*", value)
            and value.lower() not in _POSTGRES_RESERVED_WORDS
        ):
            return value
        return _quote_postgres_identifier(value)

    return f"{display_part(schema)}.{display_part(table)}"


def _postgres_table_parts_sql(table_name):
    schema, table = _parse_postgres_table_name(table_name)
    return (
        schema.replace("'", "''"),
        table.replace("'", "''"),
    )


def _postgres_table_names_from_output(output):
    names = []
    for line in output.splitlines() if output else []:
        if line.strip():
            schema, table = json.loads(line)
            names.append(_postgres_display_table_name(schema, table))
    return sorted(names)


# -------------------------------------------------------------------
#  INDEX, CONSTRAINT & TRIGGER INSPECTOR
# -------------------------------------------------------------------


def get_index_and_constraint_info(engine, container, dbname_or_path, table_name):
    """Fetches Primary Keys, Foreign Keys, Indexes, and Active Triggers across all database types."""
    info_lines = []
    info_lines.append(
        f"{BOLD}{MAGENTA}=== INDEXES, CONSTRAINTS & TRIGGERS FOR '{table_name}' ==={RESET}\n"
    )

    try:
        if engine == "sqlite":
            conn = sqlite3.connect(dbname_or_path)
            cursor = conn.cursor()

            # Indexes
            cursor.execute(f'PRAGMA index_list("{table_name}");')
            indexes = cursor.fetchall()
            info_lines.append(f"{BOLD}{CYAN}[INDEXES]{RESET}")
            if not indexes:
                info_lines.append("   (No explicit indexes found)")
            else:
                for idx in indexes:
                    idx_name = idx[1]
                    unique_str = "UNIQUE" if idx[2] else "NON-UNIQUE"
                    cursor.execute(f'PRAGMA index_info("{idx_name}");')
                    cols = [c[2] for c in cursor.fetchall()]
                    info_lines.append(
                        f"   • {BOLD}{idx_name}{RESET} ({unique_str}) ➔ Columns: {', '.join(cols)}"
                    )
            info_lines.append("")

            # Foreign Keys
            cursor.execute(f'PRAGMA foreign_key_list("{table_name}");')
            fks = cursor.fetchall()
            info_lines.append(f"{BOLD}{CYAN}[FOREIGN KEYS]{RESET}")
            if not fks:
                info_lines.append("   (No foreign keys found)")
            else:
                for fk in fks:
                    info_lines.append(f"   • {fk[3]} ➔ {fk[2]}.{fk[4]}")
            info_lines.append("")

            # Triggers
            cursor.execute(
                f"SELECT name, sql FROM sqlite_master WHERE type='trigger' AND tbl_name='{table_name}';"
            )
            triggers = cursor.fetchall()
            info_lines.append(f"{BOLD}{CYAN}[TRIGGERS]{RESET}")
            if not triggers:
                info_lines.append("   (No triggers found)")
            else:
                for trg in triggers:
                    info_lines.append(
                        f"   • {BOLD}{trg[0]}{RESET}:\n    {DIM}{trg[1]}{RESET}"
                    )
            conn.close()

        elif engine in ("postgres", "direct_postgres"):
            exec_fn = (
                run_docker_cmd
                if engine == "postgres"
                else lambda cmd: run_docker_cmd_with_err(cmd)[0]
            )

            schema_name, relation_name = _postgres_table_parts_sql(table_name)
            table_ref_literal = _postgres_table_reference(table_name).replace("'", "''")

            # Indexes
            sql_idx = (
                "SELECT indexname, indexdef FROM pg_indexes "
                f"WHERE tablename = '{relation_name}' AND schemaname = '{schema_name}';"
            )
            cmd = (
                [
                    "docker",
                    "exec",
                    "-i",
                    container,
                    "psql",
                    "-U",
                    get_container_env_user(container, "postgres"),
                    "-d",
                    dbname_or_path,
                    "-At",
                    "-F",
                    "|",
                    "-c",
                    sql_idx,
                ]
                if engine == "postgres"
                else ["psql", dbname_or_path, "-At", "-F", "|", "-c", sql_idx]
            )
            out_idx = exec_fn(cmd)
            info_lines.append(f"{BOLD}{CYAN}[INDEXES]{RESET}")
            if not out_idx:
                info_lines.append("   (No indexes found)")
            else:
                for line in out_idx.split("\n"):
                    if line.strip():
                        p = line.split("|")
                        info_lines.append(
                            f"   • {BOLD}{p[0]}{RESET} ➔ {DIM}{p[1]}{RESET}"
                        )
            info_lines.append("")

            # Constraints
            sql_con = (
                f"SELECT conname, contype, pg_get_constraintdef(oid) "
                f"FROM pg_constraint WHERE conrelid = '{table_ref_literal}'::regclass;"
            )
            cmd_con = (
                [
                    "docker",
                    "exec",
                    "-i",
                    container,
                    "psql",
                    "-U",
                    get_container_env_user(container, "postgres"),
                    "-d",
                    dbname_or_path,
                    "-At",
                    "-F",
                    "|",
                    "-c",
                    sql_con,
                ]
                if engine == "postgres"
                else ["psql", dbname_or_path, "-At", "-F", "|", "-c", sql_con]
            )
            out_con = exec_fn(cmd_con)
            info_lines.append(f"{BOLD}{CYAN}[CONSTRAINTS]{RESET}")
            if not out_con:
                info_lines.append("   (No constraints found)")
            else:
                for line in out_con.split("\n"):
                    if line.strip():
                        p = line.split("|")
                        ctype = {
                            "p": "PRIMARY KEY",
                            "f": "FOREIGN KEY",
                            "u": "UNIQUE",
                            "c": "CHECK",
                        }.get(p[1], p[1])
                        info_lines.append(
                            f"   • {BOLD}{p[0]}{RESET} [{ctype}] ➔ {p[2]}"
                        )
            info_lines.append("")

            # Triggers
            sql_trg = (
                "SELECT trigger_name, event_manipulation, action_statement "
                "FROM information_schema.triggers "
                f"WHERE event_object_table = '{relation_name}' "
                f"AND event_object_schema = '{schema_name}';"
            )
            cmd_trg = (
                [
                    "docker",
                    "exec",
                    "-i",
                    container,
                    "psql",
                    "-U",
                    get_container_env_user(container, "postgres"),
                    "-d",
                    dbname_or_path,
                    "-At",
                    "-F",
                    "|",
                    "-c",
                    sql_trg,
                ]
                if engine == "postgres"
                else ["psql", dbname_or_path, "-At", "-F", "|", "-c", sql_trg]
            )
            out_trg = exec_fn(cmd_trg)
            info_lines.append(f"{BOLD}{CYAN}[TRIGGERS]{RESET}")
            if not out_trg:
                info_lines.append("   (No triggers found)")
            else:
                for line in out_trg.split("\n"):
                    if line.strip():
                        p = line.split("|")
                        info_lines.append(
                            f"   • {BOLD}{p[0]}{RESET} [{p[1]}] ➔ {DIM}{p[2]}{RESET}"
                        )

        elif engine in ("mysql", "direct_mysql"):
            exec_fn = (
                run_docker_cmd
                if engine == "mysql"
                else lambda cmd: run_docker_cmd_with_err(cmd)[0]
            )

            sql_idx = f"SHOW INDEX FROM `{table_name}`;"
            cmd = (
                [
                    "docker",
                    "exec",
                    "-i",
                    container,
                    "mysql",
                    "-u",
                    get_container_env_user(container, "mysql"),
                    "-D",
                    dbname_or_path,
                    "-B",
                    "-N",
                    "-e",
                    sql_idx,
                ]
                if engine == "mysql"
                else build_mysql_cli_cmd(dbname_or_path, bare=True) + ["-e", sql_idx]
            )
            out_idx = exec_fn(cmd)
            info_lines.append(f"{BOLD}{CYAN}[INDEXES]{RESET}")
            if not out_idx:
                info_lines.append("   (No indexes found)")
            else:
                for line in out_idx.split("\n"):
                    if line.strip():
                        p = line.split("\t")
                        if len(p) > 4:
                            info_lines.append(
                                f"   • {BOLD}{p[2]}{RESET} ({'NON-UNIQUE' if p[1] == '1' else 'UNIQUE'}) ➔ Column: {p[4]}"
                            )
            info_lines.append("")

            sql_trg = f"SHOW TRIGGERS LIKE '{table_name}';"
            cmd_trg = (
                [
                    "docker",
                    "exec",
                    "-i",
                    container,
                    "mysql",
                    "-u",
                    get_container_env_user(container, "mysql"),
                    "-D",
                    dbname_or_path,
                    "-B",
                    "-N",
                    "-e",
                    sql_trg,
                ]
                if engine == "mysql"
                else build_mysql_cli_cmd(dbname_or_path, bare=True) + ["-e", sql_trg]
            )
            out_trg = exec_fn(cmd_trg)
            info_lines.append(f"{BOLD}{CYAN}[TRIGGERS]{RESET}")
            if not out_trg:
                info_lines.append("   (No triggers found)")
            else:
                for line in out_trg.split("\n"):
                    if line.strip():
                        p = line.split("\t")
                        info_lines.append(
                            f"   • {BOLD}{p[0]}{RESET} [{p[1]} {p[2]}] ➔ {DIM}{p[3]}{RESET}"
                        )

    except Exception as e:
        info_lines.append(f"   {RED}Error reading inspector details: {e}{RESET}")

    return info_lines


# -------------------------------------------------------------------
#  CONTAINER ENVIRONMENT INSPECTION
# -------------------------------------------------------------------


def get_container_env_user(container, engine):
    try:
        cmd = ["docker", "inspect", container, "--format", "{{json .Config.Env}}"]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        env_vars = json.loads(res.stdout.strip()) if res.stdout else []

        target_key = "POSTGRES_USER=" if engine == "postgres" else "MYSQL_USER="
        for var in env_vars:
            if var.startswith(target_key):
                return var.split("=", 1)[1]
    except Exception:
        pass

    return "postgres" if engine == "postgres" else "root"


# -------------------------------------------------------------------
#  DIRECT DATABASE CONNECTION EXECUTION (PSQL / MYSQL / SQLITE)
# -------------------------------------------------------------------


def get_direct_postgres_tables(conn_url):
    cmd = [
        "psql",
        conn_url,
        "-At",
        "-c",
        "SELECT json_build_array(schemaname, tablename)::text FROM pg_tables "
        "WHERE schemaname !~ '^pg_' AND schemaname <> 'information_schema' "
        "ORDER BY schemaname, tablename;",
    ]
    out, _ = run_docker_cmd_with_err(cmd)
    return _postgres_table_names_from_output(out)


def query_direct_postgres_page(
    conn_url, table, limit, offset, filter_query=None, order_by=None
):
    if filter_query:
        clean_filter = sanitize_where_clause(filter_query)
        safe_filter = clean_filter.replace("'", "''")
        if is_sql_where_expression(clean_filter):
            where_sql = f"WHERE {clean_filter}"
        else:
            where_sql = f"WHERE CAST(row_to_json(t) AS text) ILIKE '%{safe_filter}%'"
    else:
        where_sql = ""

    order_sql = (
        f'ORDER BY t.{_quote_postgres_identifier(order_by[0])} {order_by[1]}'
        if order_by
        else ""
    )
    sql = f"COPY (SELECT * FROM {_postgres_table_reference(table)} t {where_sql} {order_sql} LIMIT {limit} OFFSET {offset}) TO STDOUT WITH CSV HEADER;"
    cmd = ["psql", conn_url, "-c", sql]
    output, err_msg = run_docker_cmd_with_err(cmd)

    if err_msg:
        return ["PostgreSQL Error"], [[err_msg]]
    if not output:
        return [], []

    reader = csv.reader(io.StringIO(output))
    data = list(reader)
    if not data:
        return [], []
    return data[0], data[1:]


def update_direct_postgres_row(conn_url, table_name, headers, orig_row, updated_dict):
    changed = {
        k: v
        for k, v in updated_dict.items()
        if dict(zip(headers, orig_row)).get(k) != v
    }
    if not changed:
        print(f"\n{YELLOW}No changes detected.{RESET}")
        return False

    set_clause = ", ".join(
        [f'"{k}" = {format_sql_value(v)}' for k, v in changed.items()]
    )
    where_sql = build_where_clause(headers, orig_row)
    sql = f'UPDATE {_postgres_table_reference(table_name)} SET {set_clause} WHERE {where_sql};'
    cmd = ["psql", conn_url, "-c", sql]
    _, err = run_docker_cmd_with_err(cmd)
    return err is None


def insert_direct_postgres_row(conn_url, table_name, row_dict):
    cols = list(row_dict.keys())
    vals = [
        format_sql_value(v if not isinstance(v, (dict, list)) else json.dumps(v))
        for v in row_dict.values()
    ]
    cols_sql = ", ".join([f'"{c}"' for c in cols])
    vals_sql = ", ".join(vals)
    sql = f'INSERT INTO {_postgres_table_reference(table_name)} ({cols_sql}) VALUES ({vals_sql});'
    cmd = ["psql", conn_url, "-c", sql]
    _, err = run_docker_cmd_with_err(cmd)
    return err is None


def delete_direct_postgres_row(conn_url, table_name, headers, orig_row):
    where_sql = build_where_clause(headers, orig_row)
    sql = f'DELETE FROM {_postgres_table_reference(table_name)} WHERE {where_sql};'
    cmd = ["psql", conn_url, "-c", sql]
    _, err = run_docker_cmd_with_err(cmd)
    return err is None


def count_direct_postgres_rows(conn_url, table_name, filter_query=None):
    if filter_query:
        clean_filter = sanitize_where_clause(filter_query)
        safe_filter = clean_filter.replace("'", "''")
        if is_sql_where_expression(clean_filter):
            where_sql = f"WHERE {clean_filter}"
        else:
            where_sql = f"WHERE CAST(row_to_json(t) AS text) ILIKE '%{safe_filter}%'"
    else:
        where_sql = ""
    sql = f'SELECT COUNT(*) FROM {_postgres_table_reference(table_name)} t {where_sql};'
    cmd = ["psql", conn_url, "-At", "-c", sql]
    out, _ = run_docker_cmd_with_err(cmd)
    try:
        return int(out.strip()) if out else 0
    except Exception:
        return 0


def get_direct_postgres_schema_info(conn_url, table_name):
    schema_name, relation_name = _postgres_table_parts_sql(table_name)
    sql = (
        "SELECT column_name, data_type, is_nullable, column_default "
        "FROM information_schema.columns "
        f"WHERE table_schema = '{schema_name}' AND table_name = '{relation_name}' "
        "ORDER BY ordinal_position;"
    )
    cmd = ["psql", conn_url, "-At", "-F", "|", "-c", sql]
    output, _ = run_docker_cmd_with_err(cmd)
    if not output:
        return ["Column", "Type", "Nullable", "Default"], [
            ["Error", "Unknown", "-", "-"]
        ]
    headers = ["Column Name", "Type", "Nullable", "Default"]
    rows = [line.split("|") for line in output.split("\n") if line.strip()]
    return headers, rows


def get_direct_postgres_foreign_keys(conn_url, table_name):
    cache_key = ("direct_postgres", conn_url, table_name)
    if cache_key in _FK_CACHE:
        return _FK_CACHE[cache_key]
    schema_name, relation_name = _postgres_table_parts_sql(table_name)
    sql = (
        "SELECT a1.attname AS from_col, n2.nspname AS to_schema, "
        "c2.relname AS to_table, a2.attname AS to_col "
        "FROM pg_constraint k "
        "JOIN pg_class c1 ON c1.oid = k.conrelid "
        "JOIN pg_namespace n1 ON n1.oid = c1.relnamespace "
        "JOIN pg_class c2 ON c2.oid = k.confrelid "
        "JOIN pg_namespace n2 ON n2.oid = c2.relnamespace "
        "JOIN pg_attribute a1 ON a1.attrelid = c1.oid AND a1.attnum = k.conkey[1] "
        "JOIN pg_attribute a2 ON a2.attrelid = c2.oid AND a2.attnum = k.confkey[1] "
        f"WHERE k.contype = 'f' AND c1.relname = '{relation_name}' "
        f"AND n1.nspname = '{schema_name}';"
    )
    cmd = ["psql", conn_url, "-At", "-F", "|", "-c", sql]
    output, _ = run_docker_cmd_with_err(cmd)
    fks = []
    if output:
        for line in output.split("\n"):
            if line.strip():
                p = line.split("|")
                if len(p) == 4:
                    fks.append(
                        {
                            "from_col": p[0],
                            "to_table": _postgres_display_table_name(p[1], p[2]),
                            "to_col": p[3],
                        }
                    )
    _FK_CACHE[cache_key] = fks
    return fks


def execute_direct_postgres_raw(conn_url, raw_sql, limit=50, offset=0):
    limited_sql = f"COPY (SELECT * FROM ({raw_sql.rstrip(';')}) AS _subq LIMIT {limit} OFFSET {offset}) TO STDOUT WITH CSV HEADER;"
    cmd = ["psql", conn_url, "-c", limited_sql]
    output, err_msg = run_docker_cmd_with_err(cmd)
    if err_msg:
        return ["PostgreSQL Error"], [[err_msg]]
    if not output:
        return [], []
    reader = csv.reader(io.StringIO(output))
    data = list(reader)
    return (data[0], data[1:]) if data else ([], [])


def build_mysql_cli_cmd(conn_url, bare=False):
    parsed = urlparse(conn_url)
    user = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    host = parsed.hostname or "localhost"
    port = parsed.port or 3306
    dbname = parsed.path.lstrip("/")

    cmd = ["mysql", f"--host={host}", f"--port={port}"]
    if user:
        cmd.append(f"--user={user}")
    if password:
        cmd.append(f"--password={password}")
    if dbname:
        cmd.extend(["-D", dbname])
    if bare:
        cmd.extend(["-B", "-N"])
    else:
        cmd.extend(["-B"])
    return cmd


def get_direct_mysql_tables(conn_url):
    cmd = build_mysql_cli_cmd(conn_url, bare=True) + ["-e", "SHOW TABLES;"]
    out, _ = run_docker_cmd_with_err(cmd)
    return sorted([t for t in out.split("\n") if t]) if out else []


def query_direct_mysql_page(
    conn_url, table, limit, offset, filter_query=None, order_by=None
):
    clean_filter = sanitize_where_clause(filter_query) if filter_query else None
    where_sql = (
        f"WHERE {clean_filter}"
        if clean_filter and is_sql_where_expression(clean_filter)
        else ""
    )
    order_sql = f"ORDER BY `{order_by[0]}` {order_by[1]}" if order_by else ""
    sql = f"SELECT * FROM `{table}` {where_sql} {order_sql} LIMIT {limit} OFFSET {offset};"
    cmd = build_mysql_cli_cmd(conn_url, bare=False) + ["-e", sql]
    output, err_msg = run_docker_cmd_with_err(cmd)

    if err_msg:
        return ["MySQL Error"], [[err_msg]]
    if not output:
        return [], []

    reader = csv.reader(io.StringIO(output), delimiter="\t")
    data = list(reader)
    if not data:
        return [], []

    headers, rows = data[0], data[1:]
    if clean_filter and not is_sql_where_expression(clean_filter):
        rows = [
            r
            for r in rows
            if any(clean_filter.lower() in str(cell).lower() for cell in r)
        ]
    return headers, rows


def update_direct_mysql_row(conn_url, table_name, headers, orig_row, updated_dict):
    changed = {
        k: v
        for k, v in updated_dict.items()
        if dict(zip(headers, orig_row)).get(k) != v
    }
    if not changed:
        print(f"\n{YELLOW}No changes detected.{RESET}")
        return False

    set_clause = ", ".join(
        [f"`{k}` = {format_sql_value(v)}" for k, v in changed.items()]
    )
    where_sql = build_where_clause(headers, orig_row).replace('"', "`")
    sql = f"UPDATE `{table_name}` SET {set_clause} WHERE {where_sql};"
    cmd = build_mysql_cli_cmd(conn_url) + ["-e", sql]
    _, err = run_docker_cmd_with_err(cmd)
    return err is None


def insert_direct_mysql_row(conn_url, table_name, row_dict):
    cols = list(row_dict.keys())
    vals = [
        format_sql_value(v if not isinstance(v, (dict, list)) else json.dumps(v))
        for v in row_dict.values()
    ]
    cols_sql = ", ".join([f"`{c}`" for c in cols])
    vals_sql = ", ".join(vals)
    sql = f"INSERT INTO `{table_name}` ({cols_sql}) VALUES ({vals_sql});"
    cmd = build_mysql_cli_cmd(conn_url) + ["-e", sql]
    _, err = run_docker_cmd_with_err(cmd)
    return err is None


def delete_direct_mysql_row(conn_url, table_name, headers, orig_row):
    where_sql = build_where_clause(headers, orig_row).replace('"', "`")
    sql = f"DELETE FROM `{table_name}` WHERE {where_sql};"
    cmd = build_mysql_cli_cmd(conn_url) + ["-e", sql]
    _, err = run_docker_cmd_with_err(cmd)
    return err is None


def count_direct_mysql_rows(conn_url, table_name, filter_query=None):
    clean_filter = sanitize_where_clause(filter_query) if filter_query else None
    where_sql = (
        f"WHERE {clean_filter}"
        if clean_filter and is_sql_where_expression(clean_filter)
        else ""
    )
    sql = f"SELECT COUNT(*) FROM `{table_name}` {where_sql};"
    cmd = build_mysql_cli_cmd(conn_url, bare=True) + ["-e", sql]
    out, _ = run_docker_cmd_with_err(cmd)
    try:
        return int(out.strip()) if out else 0
    except Exception:
        return 0


def get_direct_mysql_schema_info(conn_url, table_name):
    sql = f"DESCRIBE `{table_name}`;"
    cmd = build_mysql_cli_cmd(conn_url, bare=True) + ["-e", sql]
    output, _ = run_docker_cmd_with_err(cmd)
    if not output:
        return ["Field", "Type", "Null", "Key", "Default"], [
            ["Error", "-", "-", "-", "-"]
        ]
    headers = ["Field", "Type", "Null", "Key", "Default", "Extra"]
    rows = [line.split("\t") for line in output.split("\n") if line.strip()]
    return headers, rows


def get_direct_mysql_foreign_keys(conn_url, table_name):
    cache_key = ("direct_mysql", conn_url, table_name)
    if cache_key in _FK_CACHE:
        return _FK_CACHE[cache_key]
    parsed = urlparse(conn_url)
    dbname = parsed.path.lstrip("/")
    sql = (
        "SELECT COLUMN_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME "
        "FROM information_schema.KEY_COLUMN_USAGE "
        f"WHERE TABLE_SCHEMA = '{dbname}' AND TABLE_NAME = '{table_name}' AND REFERENCED_TABLE_NAME IS NOT NULL;"
    )
    cmd = build_mysql_cli_cmd(conn_url, bare=True) + ["-e", sql]
    output, _ = run_docker_cmd_with_err(cmd)
    fks = []
    if output:
        for line in output.split("\n"):
            if line.strip():
                p = line.split("\t")
                if len(p) == 3:
                    fks.append({"from_col": p[0], "to_table": p[1], "to_col": p[2]})
    _FK_CACHE[cache_key] = fks
    return fks


def execute_direct_mysql_raw(conn_url, raw_sql, limit=50, offset=0):
    limited_sql = (
        f"SELECT * FROM ({raw_sql.rstrip(';')}) AS _subq LIMIT {limit} OFFSET {offset};"
    )
    cmd = build_mysql_cli_cmd(conn_url, bare=False) + ["-e", limited_sql]
    output, err_msg = run_docker_cmd_with_err(cmd)
    if err_msg:
        return ["MySQL Error"], [[err_msg]]
    if not output:
        return [], []
    reader = csv.reader(io.StringIO(output), delimiter="\t")
    data = list(reader)
    return (data[0], data[1:]) if data else ([], [])


# -------------------------------------------------------------------
#  HIGH-PERFORMANCE FK INSPECTION & EXPORT ENGINES
# -------------------------------------------------------------------


def get_foreign_keys(engine, container, dbname_or_path, table_name):
    """Retrieves foreign key definitions using fast direct catalog queries and in-memory caching."""
    cache_key = (engine, container, str(dbname_or_path), table_name)
    if cache_key in _FK_CACHE:
        return _FK_CACHE[cache_key]

    fks = []
    try:
        if engine == "sqlite":
            fks = SQLiteAdapter(dbname_or_path).foreign_keys(table_name)
        elif engine == "postgres":
            user = get_container_env_user(container, "postgres")
            schema_name, relation_name = _postgres_table_parts_sql(table_name)
            sql = (
                "SELECT a1.attname AS from_col, n2.nspname AS to_schema, "
                "c2.relname AS to_table, a2.attname AS to_col "
                "FROM pg_constraint k "
                "JOIN pg_class c1 ON c1.oid = k.conrelid "
                "JOIN pg_namespace n1 ON n1.oid = c1.relnamespace "
                "JOIN pg_class c2 ON c2.oid = k.confrelid "
                "JOIN pg_namespace n2 ON n2.oid = c2.relnamespace "
                "JOIN pg_attribute a1 ON a1.attrelid = c1.oid AND a1.attnum = k.conkey[1] "
                "JOIN pg_attribute a2 ON a2.attrelid = c2.oid AND a2.attnum = k.confkey[1] "
                f"WHERE k.contype = 'f' AND c1.relname = '{relation_name}' "
                f"AND n1.nspname = '{schema_name}';"
            )
            cmd = [
                "docker",
                "exec",
                "-i",
                container,
                "psql",
                "-U",
                user,
                "-d",
                dbname_or_path,
                "-At",
                "-F",
                "|",
                "-c",
                sql,
            ]
            output = run_docker_cmd(cmd)
            if output:
                for line in output.split("\n"):
                    if line.strip():
                        p = line.split("|")
                        if len(p) == 4:
                            fks.append(
                                {
                                    "from_col": p[0],
                                    "to_table": _postgres_display_table_name(p[1], p[2]),
                                    "to_col": p[3],
                                }
                            )
        elif engine == "mysql":
            user = get_container_env_user(container, "mysql")
            sql = (
                "SELECT COLUMN_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME "
                "FROM information_schema.KEY_COLUMN_USAGE "
                f"WHERE TABLE_SCHEMA = '{dbname_or_path}' AND TABLE_NAME = '{table_name}' AND REFERENCED_TABLE_NAME IS NOT NULL;"
            )
            cmd = [
                "docker",
                "exec",
                "-i",
                container,
                "mysql",
                "-u",
                user,
                "-D",
                dbname_or_path,
                "-B",
                "-N",
                "-e",
                sql,
            ]
            output = run_docker_cmd(cmd)
            if output:
                for line in output.split("\n"):
                    if line.strip():
                        p = line.split("\t")
                        if len(p) == 3:
                            fks.append(
                                {"from_col": p[0], "to_table": p[1], "to_col": p[2]}
                            )
    except Exception:
        pass

    _FK_CACHE[cache_key] = fks
    return fks


def count_postgres_rows(container, dbname, table_name, filter_query=None):
    """Counts matching rows for Postgres."""
    user = get_container_env_user(container, "postgres")
    if filter_query:
        clean_filter = sanitize_where_clause(filter_query)
        safe_filter = clean_filter.replace("'", "''")
        if is_sql_where_expression(clean_filter):
            where_sql = f"WHERE {clean_filter}"
        else:
            where_sql = f"WHERE CAST(row_to_json(t) AS text) ILIKE '%{safe_filter}%'"
    else:
        where_sql = ""

    sql = f'SELECT COUNT(*) FROM {_postgres_table_reference(table_name)} t {where_sql};'
    cmd = [
        "docker",
        "exec",
        "-i",
        container,
        "psql",
        "-U",
        user,
        "-d",
        dbname,
        "-At",
        "-c",
        sql,
    ]
    output = run_docker_cmd(cmd)
    try:
        return int(output.strip()) if output else 0
    except Exception:
        return 0


def count_mysql_rows(container, dbname, table_name, filter_query=None):
    """Counts matching rows for MySQL."""
    user = get_container_env_user(container, "mysql")
    clean_filter = sanitize_where_clause(filter_query) if filter_query else None

    if clean_filter and is_sql_where_expression(clean_filter):
        where_sql = f"WHERE {clean_filter}"
    else:
        where_sql = ""

    sql = f"SELECT COUNT(*) FROM `{table_name}` {where_sql};"
    cmd = [
        "docker",
        "exec",
        "-i",
        container,
        "mysql",
        "-u",
        user,
        "-D",
        dbname,
        "-B",
        "-N",
        "-e",
        sql,
    ]
    output = run_docker_cmd(cmd)
    try:
        return int(output.strip()) if output else 0
    except Exception:
        return 0


def get_postgres_schema_info(container, dbname, table_name):
    """Fetches schema info for Postgres table."""
    user = get_container_env_user(container, "postgres")
    schema_name, relation_name = _postgres_table_parts_sql(table_name)
    sql = (
        "SELECT column_name, data_type, is_nullable, column_default "
        "FROM information_schema.columns "
        f"WHERE table_schema = '{schema_name}' AND table_name = '{relation_name}' "
        "ORDER BY ordinal_position;"
    )
    cmd = [
        "docker",
        "exec",
        "-i",
        container,
        "psql",
        "-U",
        user,
        "-d",
        dbname,
        "-At",
        "-F",
        "|",
        "-c",
        sql,
    ]
    output = run_docker_cmd(cmd)
    if not output:
        return ["Column", "Type", "Nullable", "Default"], [
            ["Error", "Unknown", "-", "-"]
        ]

    headers = ["Column Name", "Type", "Nullable", "Default"]
    rows = []
    for line in output.split("\n"):
        if line.strip():
            parts = line.split("|")
            rows.append(parts)
    return headers, rows


def get_mysql_schema_info(container, dbname, table_name):
    """Fetches schema info for MySQL table."""
    user = get_container_env_user(container, "mysql")
    sql = f"DESCRIBE `{table_name}`;"
    cmd = [
        "docker",
        "exec",
        "-i",
        container,
        "mysql",
        "-u",
        user,
        "-D",
        dbname,
        "-B",
        "-N",
        "-e",
        sql,
    ]
    output = run_docker_cmd(cmd)
    if not output:
        return ["Field", "Type", "Null", "Key", "Default"], [
            ["Error", "-", "-", "-", "-"]
        ]

    headers = ["Field", "Type", "Null", "Key", "Default", "Extra"]
    rows = []
    for line in output.split("\n"):
        if line.strip():
            rows.append(line.split("\t"))
    return headers, rows


# -------------------------------------------------------------------
#  SQL UPDATE, INSERT & DELETE ENGINES
# -------------------------------------------------------------------


def format_sql_value(val):
    if val is None:
        return "NULL"
    if isinstance(val, bool):
        return "TRUE" if val else "FALSE"
    if isinstance(val, (int, float)):
        return str(val)
    s_val = str(val).replace("'", "''")
    return f"'{s_val}'"


def build_where_clause(headers, orig_row):
    if "id" in headers:
        id_idx = headers.index("id")
        id_val = orig_row[id_idx]
        if id_val is not None:
            return f'"id" = {format_sql_value(id_val)}'

    clauses = []
    for h, v in zip(headers, orig_row):
        if v is None:
            clauses.append(f'"{h}" IS NULL')
        else:
            clauses.append(f'"{h}" = {format_sql_value(v)}')
    return " AND ".join(clauses)


def insert_postgres_row(container, dbname, table_name, row_dict):
    user = get_container_env_user(container, "postgres")
    cols = list(row_dict.keys())
    vals = []
    for v in row_dict.values():
        if isinstance(v, (dict, list)):
            vals.append(format_sql_value(json.dumps(v)))
        else:
            vals.append(format_sql_value(v))
    cols_sql = ", ".join([f'"{c}"' for c in cols])
    vals_sql = ", ".join(vals)
    sql = f'INSERT INTO {_postgres_table_reference(table_name)} ({cols_sql}) VALUES ({vals_sql});'
    cmd = [
        "docker",
        "exec",
        "-i",
        container,
        "psql",
        "-U",
        user,
        "-d",
        dbname,
        "-c",
        sql,
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode == 0:
        return True
    else:
        print(f"\n{RED}Postgres Insert Error: {res.stderr.strip()}{RESET}")
        return False


def insert_mysql_row(container, dbname, table_name, row_dict):
    user = get_container_env_user(container, "mysql")
    cols = list(row_dict.keys())
    vals = []
    for v in row_dict.values():
        if isinstance(v, (dict, list)):
            vals.append(format_sql_value(json.dumps(v)))
        else:
            vals.append(format_sql_value(v))
    cols_sql = ", ".join([f"`{c}`" for c in cols])
    vals_sql = ", ".join(vals)
    sql = f"INSERT INTO `{table_name}` ({cols_sql}) VALUES ({vals_sql});"
    cmd = [
        "docker",
        "exec",
        "-i",
        container,
        "mysql",
        "-u",
        user,
        "-D",
        dbname,
        "-e",
        sql,
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode == 0:
        return True
    else:
        print(f"\n{RED}MySQL Insert Error: {res.stderr.strip()}{RESET}")
        return False


def update_postgres_row(container, dbname, table_name, headers, orig_row, updated_dict):
    changed = {
        k: v
        for k, v in updated_dict.items()
        if dict(zip(headers, orig_row)).get(k) != v
    }
    if not changed:
        print(f"\n{YELLOW}No changes detected.{RESET}")
        return False

    user = get_container_env_user(container, "postgres")
    set_clause = ", ".join(
        [f'"{k}" = {format_sql_value(v)}' for k, v in changed.items()]
    )
    where_sql = build_where_clause(headers, orig_row)

    sql = f'UPDATE {_postgres_table_reference(table_name)} SET {set_clause} WHERE {where_sql};'
    cmd = [
        "docker",
        "exec",
        "-i",
        container,
        "psql",
        "-U",
        user,
        "-d",
        dbname,
        "-c",
        sql,
    ]

    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode == 0:
        return True
    else:
        print(f"\n{RED}Postgres Update Error: {res.stderr.strip()}{RESET}")
        return False


def delete_postgres_row(container, dbname, table_name, headers, orig_row):
    user = get_container_env_user(container, "postgres")
    where_sql = build_where_clause(headers, orig_row)

    sql = f'DELETE FROM {_postgres_table_reference(table_name)} WHERE {where_sql};'
    cmd = [
        "docker",
        "exec",
        "-i",
        container,
        "psql",
        "-U",
        user,
        "-d",
        dbname,
        "-c",
        sql,
    ]

    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode == 0:
        return True
    else:
        print(f"\n{RED}Postgres Delete Error: {res.stderr.strip()}{RESET}")
        return False


def update_mysql_row(container, dbname, table_name, headers, orig_row, updated_dict):
    changed = {
        k: v
        for k, v in updated_dict.items()
        if dict(zip(headers, orig_row)).get(k) != v
    }
    if not changed:
        print(f"\n{YELLOW}No changes detected.{RESET}")
        return False

    user = get_container_env_user(container, "mysql")
    set_clause = ", ".join(
        [f"`{k}` = {format_sql_value(v)}" for k, v in changed.items()]
    )
    where_sql = build_where_clause(headers, orig_row).replace('"', "`")

    sql = f"UPDATE `{table_name}` SET {set_clause} WHERE {where_sql};"
    cmd = [
        "docker",
        "exec",
        "-i",
        container,
        "mysql",
        "-u",
        user,
        "-D",
        dbname,
        "-e",
        sql,
    ]

    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode == 0:
        return True
    else:
        print(f"\n{RED}MySQL Update Error: {res.stderr.strip()}{RESET}")
        return False


def delete_mysql_row(container, dbname, table_name, headers, orig_row):
    user = get_container_env_user(container, "mysql")
    where_sql = build_where_clause(headers, orig_row).replace('"', "`")

    sql = f"DELETE FROM `{table_name}` WHERE {where_sql};"
    cmd = [
        "docker",
        "exec",
        "-i",
        container,
        "mysql",
        "-u",
        user,
        "-D",
        dbname,
        "-e",
        sql,
    ]

    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode == 0:
        return True
    else:
        print(f"\n{RED}MySQL Delete Error: {res.stderr.strip()}{RESET}")
        return False


# -------------------------------------------------------------------
#  DOCKER ENGINE & DATABASE DISCOVERY
# -------------------------------------------------------------------


def run_docker_cmd(cmd):
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return res.stdout.strip()
    except subprocess.CalledProcessError:
        return None


def run_docker_cmd_with_err(cmd):
    """Runs a command via subprocess and returns stdout and error message if any."""
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return res.stdout.strip(), None
    except subprocess.CalledProcessError as e:
        err_msg = e.stderr.strip() if e.stderr else str(e)
        return None, err_msg


def get_postgres_dbs(container):
    user = get_container_env_user(container, "postgres")
    cmd = [
        "docker",
        "exec",
        "-i",
        container,
        "psql",
        "-U",
        user,
        "-At",
        "-c",
        "SELECT datname FROM pg_database WHERE datistemplate = false;",
    ]
    output = run_docker_cmd(cmd)
    if not output:
        return [user]
    return [db.strip() for db in output.split("\n") if db.strip()]


def get_mysql_dbs(container):
    user = get_container_env_user(container, "mysql")
    query = (
        "SELECT schema_name FROM information_schema.schemata "
        "WHERE schema_name NOT IN ('information_schema', 'performance_schema', 'mysql', 'sys');"
    )
    cmd = [
        "docker",
        "exec",
        "-i",
        container,
        "mysql",
        "-u",
        user,
        "-B",
        "-N",
        "-e",
        query,
    ]
    output = run_docker_cmd(cmd)
    if not output:
        return []
    return [db.strip() for db in output.split("\n") if db.strip()]


def prompt_select_database(container, engine, initial_idx=0):
    if engine == "postgres":
        dbs = get_postgres_dbs(container)
    elif engine == "mysql":
        dbs = get_mysql_dbs(container)
    else:
        return None, 0

    if not dbs:
        return None, 0

    db_items = [{"name": db, "label": db} for db in dbs]
    selected, idx = select_menu_item(
        f"Databases in '{container}'",
        db_items,
        display_key="label",
        initial_idx=initial_idx,
    )
    return (selected["name"] if selected else None), idx


def get_postgres_tables(container, dbname):
    user = get_container_env_user(container, "postgres")
    cmd = [
        "docker",
        "exec",
        "-i",
        container,
        "psql",
        "-U",
        user,
        "-d",
        dbname,
        "-At",
        "-c",
        "SELECT json_build_array(schemaname, tablename)::text FROM pg_tables "
        "WHERE schemaname !~ '^pg_' AND schemaname <> 'information_schema' "
        "ORDER BY schemaname, tablename;",
    ]
    output = run_docker_cmd(cmd)
    return _postgres_table_names_from_output(output)


def query_postgres_page(
    container, dbname, table, limit, offset, filter_query=None, order_by=None
):
    user = get_container_env_user(container, "postgres")
    if filter_query:
        clean_filter = sanitize_where_clause(filter_query)
        safe_filter = clean_filter.replace("'", "''")
        if is_sql_where_expression(clean_filter):
            where_sql = f"WHERE {clean_filter}"
        else:
            where_sql = f"WHERE CAST(row_to_json(t) AS text) ILIKE '%{safe_filter}%'"
    else:
        where_sql = ""

    order_sql = (
        f'ORDER BY t.{_quote_postgres_identifier(order_by[0])} {order_by[1]}'
        if order_by
        else ""
    )

    sql = f"COPY (SELECT * FROM {_postgres_table_reference(table)} t {where_sql} {order_sql} LIMIT {limit} OFFSET {offset}) TO STDOUT WITH CSV HEADER;"
    cmd = [
        "docker",
        "exec",
        "-i",
        container,
        "psql",
        "-U",
        user,
        "-d",
        dbname,
        "-c",
        sql,
    ]
    output, err_msg = run_docker_cmd_with_err(cmd)

    if err_msg:
        return ["PostgreSQL Error"], [[err_msg]]
    if not output:
        return [], []

    reader = csv.reader(io.StringIO(output))
    data = list(reader)
    if not data:
        return [], []
    return data[0], data[1:]


def execute_postgres_raw(container, dbname, raw_sql, limit=50, offset=0):
    user = get_container_env_user(container, "postgres")
    limited_sql = f"COPY (SELECT * FROM ({raw_sql.rstrip(';')}) AS _subq LIMIT {limit} OFFSET {offset}) TO STDOUT WITH CSV HEADER;"
    cmd = [
        "docker",
        "exec",
        "-i",
        container,
        "psql",
        "-U",
        user,
        "-d",
        dbname,
        "-c",
        limited_sql,
    ]
    output, err_msg = run_docker_cmd_with_err(cmd)

    if err_msg:
        return ["PostgreSQL Error"], [[err_msg]]
    if not output:
        return [], []

    reader = csv.reader(io.StringIO(output))
    data = list(reader)
    if not data:
        return [], []
    return data[0], data[1:]


def get_mysql_tables(container, dbname):
    user = get_container_env_user(container, "mysql")
    cmd = [
        "docker",
        "exec",
        "-i",
        container,
        "mysql",
        "-u",
        user,
        "-D",
        dbname,
        "-B",
        "-N",
        "-e",
        "SHOW TABLES;",
    ]
    output = run_docker_cmd(cmd)
    return sorted([t for t in output.split("\n") if t]) if output else []


def query_mysql_page(
    container, dbname, table, limit, offset, filter_query=None, order_by=None
):
    user = get_container_env_user(container, "mysql")
    clean_filter = sanitize_where_clause(filter_query) if filter_query else None

    if clean_filter and is_sql_where_expression(clean_filter):
        where_sql = f"WHERE {clean_filter}"
    else:
        where_sql = ""

    order_sql = f"ORDER BY `{order_by[0]}` {order_by[1]}" if order_by else ""

    sql = f"SELECT * FROM `{table}` {where_sql} {order_sql} LIMIT {limit} OFFSET {offset};"
    cmd = [
        "docker",
        "exec",
        "-i",
        container,
        "mysql",
        "-u",
        user,
        "-D",
        dbname,
        "-B",
        "-e",
        sql,
    ]
    output, err_msg = run_docker_cmd_with_err(cmd)

    if err_msg:
        return ["MySQL Error"], [[err_msg]]
    if not output:
        return [], []

    reader = csv.reader(io.StringIO(output), delimiter="\t")
    data = list(reader)
    if not data:
        return [], []

    headers = data[0]
    rows = data[1:]

    if clean_filter and not is_sql_where_expression(clean_filter):
        rows = [
            r
            for r in rows
            if any(clean_filter.lower() in str(cell).lower() for cell in r)
        ]

    return headers, rows


def execute_mysql_raw(container, dbname, raw_sql, limit=50, offset=0):
    user = get_container_env_user(container, "mysql")
    limited_sql = (
        f"SELECT * FROM ({raw_sql.rstrip(';')}) AS _subq LIMIT {limit} OFFSET {offset};"
    )
    cmd = [
        "docker",
        "exec",
        "-i",
        container,
        "mysql",
        "-u",
        user,
        "-D",
        dbname,
        "-B",
        "-e",
        limited_sql,
    ]
    output, err_msg = run_docker_cmd_with_err(cmd)

    if err_msg:
        return ["MySQL Error"], [[err_msg]]
    if not output:
        return [], []

    reader = csv.reader(io.StringIO(output), delimiter="\t")
    data = list(reader)
    if not data:
        return [], []
    return data[0], data[1:]
