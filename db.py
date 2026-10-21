"""Executable entry point for the terminal database browser."""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

from dbcli.adapters import DriverUnavailable, MySQLAdapter, PostgresAdapter, SQLiteAdapter
from dbcli.discovery import find_docker_databases, find_sqlite_files
from dbcli.legacy import (
    count_direct_mysql_rows,
    count_direct_postgres_rows,
    count_mysql_rows,
    count_postgres_rows,
    delete_direct_mysql_row,
    delete_direct_postgres_row,
    delete_mysql_row,
    delete_postgres_row,
    execute_direct_mysql_raw,
    execute_direct_postgres_raw,
    execute_mysql_raw,
    execute_postgres_raw,
    format_sql_value,
    build_where_clause,
    get_direct_mysql_foreign_keys,
    get_direct_mysql_schema_info,
    get_direct_mysql_tables,
    get_direct_postgres_foreign_keys,
    get_direct_postgres_schema_info,
    get_direct_postgres_tables,
    get_foreign_keys,
    get_index_and_constraint_info,
    get_mysql_schema_info,
    get_mysql_tables,
    get_postgres_schema_info,
    get_postgres_tables,
    insert_direct_mysql_row,
    insert_direct_postgres_row,
    insert_mysql_row,
    insert_postgres_row,
    prompt_select_database,
    query_direct_mysql_page,
    query_direct_postgres_page,
    query_mysql_page,
    query_postgres_page,
    update_direct_mysql_row,
    update_direct_postgres_row,
    update_mysql_row,
    update_postgres_row,
)
from dbcli.query import is_sql_where_expression, sanitize_where_clause
from dbcli.ui import (
    BOLD,
    CYAN,
    DIM,
    MAGENTA,
    RED,
    RESET,
    YELLOW,
    browse_table,
    enter_alt_screen,
    exit_cleanup,
    select_menu_item,
)

APP_VERSION = "0.1.0"


# -------------------------------------------------------------------
#  DISCOVERY & MAIN MENU
# -------------------------------------------------------------------


def create_native_adapter(db_type, target, read_only=False, use_cli=False):
    """Create a native adapter when its optional driver is installed.

    Docker connections continue using the existing docker-exec implementation;
    native adapters are used for direct URLs.  Returning ``None`` is an
    intentional CLI fallback when an optional driver is unavailable.
    """
    if use_cli:
        return None

    try:
        if db_type == "direct_postgres":
            return PostgresAdapter(target, read_only=read_only)
        if db_type == "direct_mysql":
            return MySQLAdapter(target, read_only=read_only)
    except DriverUnavailable:
        return None
    return None


def parse_cli_args(argv=None):
    """Parse command-line options while preserving the original positional target."""
    parser = argparse.ArgumentParser(
        prog="db.py",
        description="Interactive database picker; Rust browser for SQLite/PostgreSQL, Python for MySQL.",
    )
    parser.add_argument(
        "target",
        nargs="?",
        help="SQLite path or PostgreSQL/MySQL connection URL",
    )
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="Disable insert, update and delete actions for this session",
    )
    parser.add_argument(
        "--cli",
        action="store_true",
        help="Use the original Python interface with psql/mysql client fallback",
    )
    parser.add_argument(
        "--python",
        action="store_true",
        help="Use the original Python interface instead of the Rust browser",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"db.py {APP_VERSION}",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_cli_args(argv)
    if args.python or args.cli:
        return python_main(args)
    from dbcli.launcher import run
    return run(args, python_browser=python_main)


def python_main(args, initial_item=None):
    """Original reference browser, also used for interactive MySQL targets."""
    cli_item = initial_item
    if args.target and initial_item is None:
        raw_arg = args.target
        if raw_arg.startswith(("postgres://", "postgresql://")):
            cli_item = {
                "type": "direct_postgres",
                "target": raw_arg,
                "label": f"POSTGRES (Remote/CLI: {raw_arg.split('@')[-1] if '@' in raw_arg else raw_arg})",
            }
        elif raw_arg.startswith(("mysql://", "mariadb://")):
            cli_item = {
                "type": "direct_mysql",
                "target": raw_arg,
                "label": f"MYSQL (Remote/CLI: {raw_arg.split('@')[-1] if '@' in raw_arg else raw_arg})",
            }
        elif raw_arg.startswith("sqlite://"):
            p = Path(raw_arg.replace("sqlite:///", "/").replace("sqlite://", ""))
            if p.exists():
                cli_item = {"type": "sqlite", "target": p, "label": f"SQLite: {p.name}"}
        elif Path(raw_arg).exists():
            p = Path(raw_arg)
            cli_item = {"type": "sqlite", "target": p, "label": f"SQLite: {p.name}"}

    sqlite_dbs = [] if initial_item is not None else find_sqlite_files()
    docker_dbs = [] if initial_item is not None else (find_docker_databases() or [])

    main_items = []
    if cli_item:
        main_items.append(cli_item)
    if sqlite_dbs:
        for db in sqlite_dbs:
            try:
                size_kb = db.stat().st_size / 1024
                main_items.append(
                    {
                        "type": "sqlite",
                        "target": db,
                        "label": f"SQLite: {db.name:<25} ({size_kb:.1f} KB)",
                    }
                )
            except Exception:
                continue
    if docker_dbs:
        for db in docker_dbs:
            main_items.append(
                {
                    "type": "docker",
                    "target": db,
                    "label": f"{db['engine'].upper():<8} (Container: {db['name']})",
                }
            )

    if not main_items:
        exit_cleanup()
        print(
            f"\n{YELLOW}No databases detected in current directory ({Path.cwd()}) or running Docker containers.{RESET}"
        )
        return

    enter_alt_screen()
    db_idx = 0
    direct_launch = True if cli_item else False

    while True:
        if initial_item is not None and not direct_launch:
            break
        if not direct_launch:
            selected_db, db_idx = select_menu_item(
                "Interactive Database Browser & Explorer",
                main_items,
                display_key="label",
                subtext=f"Scanning: {Path.cwd()}",
                initial_idx=db_idx,
            )
        else:
            selected_db = cli_item
            direct_launch = False

        if not selected_db:
            break

        db_type = selected_db["type"]
        target = selected_db["target"]

        # -------------------------------------------------------------
        #  DIRECT POSTGRES FLOW
        # -------------------------------------------------------------
        if db_type == "direct_postgres":
            native_adapter = create_native_adapter(
                db_type, target, read_only=args.read_only, use_cli=args.cli
            )
            table_idx = 0
            while True:
                tables = (
                    native_adapter.list_tables()
                    if native_adapter
                    else get_direct_postgres_tables(target)
                )
                if not tables:
                    sys.stdout.write("\033[?25h")
                    sys.stdout.flush()
                    print(f" (No public tables found for Postgres connection)")
                    input(" Press Enter to return...")
                    sys.stdout.write("\033[?25l")
                    sys.stdout.flush()
                    break

                table_items = [{"name": t, "label": t} for t in tables]
                selected_table, table_idx = select_menu_item(
                    "Tables in Postgres (Remote)",
                    table_items,
                    display_key="label",
                    initial_idx=table_idx,
                )

                if not selected_table:
                    break

                t_name = selected_table["name"]

                def query_table_fn(
                    table_name, limit, offset, filter_query=None, order_by=None
                ):
                    if native_adapter:
                        try:
                            return native_adapter.fetch_page(
                                table_name, limit, offset, filter_query, order_by
                            ).as_tuple()
                        except Exception as e:
                            return ["SQL Error"], [[str(e)]]
                    return query_direct_postgres_page(
                        target,
                        table_name,
                        limit,
                        offset,
                        filter_query,
                        order_by=order_by,
                    )

                def q_fn(limit, offset, filter_query=None, order_by=None):
                    return query_table_fn(
                        t_name, limit, offset, filter_query, order_by
                    )

                def u_fn(headers, orig_row, updated_dict):
                    if native_adapter:
                        try:
                            original = dict(zip(headers, orig_row))
                            return native_adapter.update_row(
                                t_name, original, updated_dict
                            ) == 1
                        except Exception as e:
                            print(f"\\n{RED}Database Update Error: {e}{RESET}")
                            return False
                    return update_direct_postgres_row(
                        target, t_name, headers, orig_row, updated_dict
                    )

                def i_fn(row_dict):
                    if native_adapter:
                        try:
                            return native_adapter.insert_row(t_name, row_dict) == 1
                        except Exception as e:
                            print(f"\\n{RED}Database Insert Error: {e}{RESET}")
                            return False
                    return insert_direct_postgres_row(target, t_name, row_dict)

                def d_fn(headers, orig_row):
                    if native_adapter:
                        try:
                            original = dict(zip(headers, orig_row))
                            return native_adapter.delete_row(t_name, original) == 1
                        except Exception as e:
                            print(f"\\n{RED}Database Delete Error: {e}{RESET}")
                            return False
                    return delete_direct_postgres_row(target, t_name, headers, orig_row)

                def s_fn():
                    if native_adapter:
                        return native_adapter.schema_info(t_name).as_tuple()
                    return get_direct_postgres_schema_info(target, t_name)

                def fk_fn():
                    if native_adapter:
                        return native_adapter.foreign_keys(t_name)
                    return get_direct_postgres_foreign_keys(target, t_name)

                def count_table_fn(table_name, filter_query=None):
                    if native_adapter:
                        return native_adapter.count_rows(table_name, filter_query)
                    return count_direct_postgres_rows(target, table_name, filter_query)

                def count_fn_direct_pg(filter_query=None):
                    return count_table_fn(t_name, filter_query)

                def exec_raw_fn(raw_sql, limit, offset):
                    if native_adapter:
                        try:
                            return native_adapter.execute_readonly(
                                raw_sql, limit=limit, offset=offset
                            ).as_tuple()
                        except Exception as e:
                            return ["SQL Error"], [[str(e)]]
                    return execute_direct_postgres_raw(target, raw_sql, limit, offset)

                def table_callbacks_fn(table_name):
                    if native_adapter:
                        return {}
                    return {
                        "update_fn": lambda headers, row, updated: update_direct_postgres_row(
                            target, table_name, headers, row, updated
                        ),
                        "insert_fn": lambda row: insert_direct_postgres_row(
                            target, table_name, row
                        ),
                        "delete_fn": lambda headers, row: delete_direct_postgres_row(
                            target, table_name, headers, row
                        ),
                        "schema_fn": lambda: get_direct_postgres_schema_info(
                            target, table_name
                        ),
                        "fk_fn": lambda: get_direct_postgres_foreign_keys(
                            target, table_name
                        ),
                        "count_fn": lambda filter_query=None: count_direct_postgres_rows(
                            target, table_name, filter_query
                        ),
                        "exec_raw_fn": lambda raw_sql, limit, offset: execute_direct_postgres_raw(
                            target, raw_sql, limit, offset
                        ),
                    }

                browse_table(
                    q_fn,
                    update_fn=u_fn,
                    insert_fn=i_fn,
                    delete_fn=d_fn,
                    schema_fn=s_fn,
                    fk_fn=fk_fn,
                    count_fn=count_fn_direct_pg,
                    exec_raw_fn=exec_raw_fn,
                    engine="direct_postgres",
                    dbname_or_path=target,
                    all_tables=tables,
                    table_name=t_name,
                    read_only=args.read_only,
                    inspector_fn=lambda: get_index_and_constraint_info(
                        "direct_postgres", None, target, t_name
                    ),
                    query_table_fn=query_table_fn,
                    count_table_fn=count_table_fn,
                    table_callbacks_fn=table_callbacks_fn,
                    adapter=native_adapter,
                )

            if native_adapter:
                native_adapter.close()

        # -------------------------------------------------------------
        #  DIRECT MYSQL FLOW
        # -------------------------------------------------------------
        elif db_type == "direct_mysql":
            native_adapter = create_native_adapter(
                db_type, target, read_only=args.read_only, use_cli=args.cli
            )
            table_idx = 0
            while True:
                tables = (
                    native_adapter.list_tables()
                    if native_adapter
                    else get_direct_mysql_tables(target)
                )
                if not tables:
                    sys.stdout.write("\033[?25h")
                    sys.stdout.flush()
                    print(f" (No tables found for MySQL connection)")
                    input(" Press Enter to return...")
                    sys.stdout.write("\033[?25l")
                    sys.stdout.flush()
                    break

                table_items = [{"name": t, "label": t} for t in tables]
                selected_table, table_idx = select_menu_item(
                    "Tables in MySQL (Remote)",
                    table_items,
                    display_key="label",
                    initial_idx=table_idx,
                )

                if not selected_table:
                    break

                t_name = selected_table["name"]

                def query_table_fn(
                    table_name, limit, offset, filter_query=None, order_by=None
                ):
                    if native_adapter:
                        try:
                            return native_adapter.fetch_page(
                                table_name, limit, offset, filter_query, order_by
                            ).as_tuple()
                        except Exception as e:
                            return ["SQL Error"], [[str(e)]]
                    return query_direct_mysql_page(
                        target,
                        table_name,
                        limit,
                        offset,
                        filter_query,
                        order_by=order_by,
                    )

                def q_fn(limit, offset, filter_query=None, order_by=None):
                    return query_table_fn(
                        t_name, limit, offset, filter_query, order_by
                    )

                def u_fn(headers, orig_row, updated_dict):
                    if native_adapter:
                        try:
                            original = dict(zip(headers, orig_row))
                            return native_adapter.update_row(
                                t_name, original, updated_dict
                            ) == 1
                        except Exception as e:
                            print(f"\\n{RED}Database Update Error: {e}{RESET}")
                            return False
                    return update_direct_mysql_row(
                        target, t_name, headers, orig_row, updated_dict
                    )

                def i_fn(row_dict):
                    if native_adapter:
                        try:
                            return native_adapter.insert_row(t_name, row_dict) == 1
                        except Exception as e:
                            print(f"\\n{RED}Database Insert Error: {e}{RESET}")
                            return False
                    return insert_direct_mysql_row(target, t_name, row_dict)

                def d_fn(headers, orig_row):
                    if native_adapter:
                        try:
                            original = dict(zip(headers, orig_row))
                            return native_adapter.delete_row(t_name, original) == 1
                        except Exception as e:
                            print(f"\\n{RED}Database Delete Error: {e}{RESET}")
                            return False
                    return delete_direct_mysql_row(target, t_name, headers, orig_row)

                def s_fn():
                    if native_adapter:
                        return native_adapter.schema_info(t_name).as_tuple()
                    return get_direct_mysql_schema_info(target, t_name)

                def fk_fn():
                    if native_adapter:
                        return native_adapter.foreign_keys(t_name)
                    return get_direct_mysql_foreign_keys(target, t_name)

                def count_table_fn(table_name, filter_query=None):
                    if native_adapter:
                        return native_adapter.count_rows(table_name, filter_query)
                    return count_direct_mysql_rows(target, table_name, filter_query)

                def count_fn_direct_my(filter_query=None):
                    return count_table_fn(t_name, filter_query)

                def exec_raw_fn(raw_sql, limit, offset):
                    if native_adapter:
                        try:
                            return native_adapter.execute_readonly(
                                raw_sql, limit=limit, offset=offset
                            ).as_tuple()
                        except Exception as e:
                            return ["SQL Error"], [[str(e)]]
                    return execute_direct_mysql_raw(target, raw_sql, limit, offset)

                def table_callbacks_fn(table_name):
                    if native_adapter:
                        return {}
                    return {
                        "update_fn": lambda headers, row, updated: update_direct_mysql_row(
                            target, table_name, headers, row, updated
                        ),
                        "insert_fn": lambda row: insert_direct_mysql_row(
                            target, table_name, row
                        ),
                        "delete_fn": lambda headers, row: delete_direct_mysql_row(
                            target, table_name, headers, row
                        ),
                        "schema_fn": lambda: get_direct_mysql_schema_info(
                            target, table_name
                        ),
                        "fk_fn": lambda: get_direct_mysql_foreign_keys(
                            target, table_name
                        ),
                        "count_fn": lambda filter_query=None: count_direct_mysql_rows(
                            target, table_name, filter_query
                        ),
                        "exec_raw_fn": lambda raw_sql, limit, offset: execute_direct_mysql_raw(
                            target, raw_sql, limit, offset
                        ),
                    }

                browse_table(
                    q_fn,
                    update_fn=u_fn,
                    insert_fn=i_fn,
                    delete_fn=d_fn,
                    schema_fn=s_fn,
                    fk_fn=fk_fn,
                    count_fn=count_fn_direct_my,
                    exec_raw_fn=exec_raw_fn,
                    engine="direct_mysql",
                    dbname_or_path=target,
                    all_tables=tables,
                    table_name=t_name,
                    read_only=args.read_only,
                    inspector_fn=lambda: get_index_and_constraint_info(
                        "direct_mysql", None, target, t_name
                    ),
                    query_table_fn=query_table_fn,
                    count_table_fn=count_table_fn,
                    table_callbacks_fn=table_callbacks_fn,
                    adapter=native_adapter,
                )

            if native_adapter:
                native_adapter.close()

        # -------------------------------------------------------------
        #  SQLITE FLOW
        # -------------------------------------------------------------
        elif db_type == "sqlite":
            adapter = SQLiteAdapter(target, read_only=args.read_only)
            table_idx = 0
            while True:
                tables = adapter.list_tables()
                if not tables:
                    sys.stdout.write("\033[?25h")
                    sys.stdout.flush()
                    print(f" (Database '{target.name}' contains no tables)")
                    input(" Press Enter to return...")
                    sys.stdout.write("\033[?25l")
                    sys.stdout.flush()
                    break

                table_items = []
                for t in tables:
                    count = adapter.count_rows(t)
                    table_items.append({"name": t, "label": f"{t:<30} ({count} rows)"})

                selected_table, table_idx = select_menu_item(
                    f"Tables in SQLite: {target.name}",
                    table_items,
                    display_key="label",
                    initial_idx=table_idx,
                )

                if not selected_table:
                    break

                t_name = selected_table["name"]

                def query_table_fn(
                    table_name, limit, offset, filter_query=None, order_by=None
                ):
                    return adapter.fetch_page(
                        table_name, limit, offset, filter_query, order_by
                    ).as_tuple()

                def q_fn(limit, offset, filter_query=None, order_by=None):
                    return query_table_fn(
                        t_name, limit, offset, filter_query, order_by
                    )

                def u_fn(headers, orig_row, updated_dict):
                    try:
                        original = dict(zip(headers, orig_row))
                        return adapter.update_row(t_name, original, updated_dict) == 1
                    except Exception as e:
                        print(f"\\n{RED}Database Update Error: {e}{RESET}")
                        return False

                def i_fn(row_dict):
                    try:
                        return adapter.insert_row(t_name, row_dict) == 1
                    except Exception as e:
                        print(f"\\n{RED}Database Insert Error: {e}{RESET}")
                        return False

                def d_fn(headers, orig_row):
                    try:
                        original = dict(zip(headers, orig_row))
                        return adapter.delete_row(t_name, original) == 1
                    except Exception as e:
                        print(f"\\n{RED}Database Delete Error: {e}{RESET}")
                        return False

                def s_fn():
                    return adapter.schema_info(t_name).as_tuple()

                def fk_fn():
                    return adapter.foreign_keys(t_name)

                def count_table_fn(table_name, filter_query=None):
                    return adapter.count_rows(table_name, filter_query)

                def count_fn_sqlite(filter_query=None):
                    return count_table_fn(t_name, filter_query)

                def exec_raw_fn(raw_sql, limit, offset):
                    return adapter.execute_readonly(
                        raw_sql, limit=limit, offset=offset
                    ).as_tuple()

                browse_table(
                    q_fn,
                    update_fn=u_fn,
                    insert_fn=i_fn,
                    delete_fn=d_fn,
                    schema_fn=s_fn,
                    fk_fn=fk_fn,
                    count_fn=count_fn_sqlite,
                    exec_raw_fn=exec_raw_fn,
                    engine="sqlite",
                    dbname_or_path=target,
                    all_tables=tables,
                    table_name=t_name,
                    read_only=args.read_only,
                    inspector_fn=lambda: get_index_and_constraint_info(
                        "sqlite", None, target, t_name
                    ),
                    query_table_fn=query_table_fn,
                    count_table_fn=count_table_fn,
                    adapter=adapter,
                )

            adapter.close()

        # -------------------------------------------------------------
        #  DOCKER FLOW (Postgres / MySQL)
        # -------------------------------------------------------------
        elif db_type == "docker":
            c_name = target["name"]
            engine = target["engine"]

            db_prompt_idx = 0
            while True:
                dbname, db_prompt_idx = prompt_select_database(
                    c_name, engine, initial_idx=db_prompt_idx
                )
                if not dbname:
                    break

                table_idx = 0
                while True:
                    if engine == "postgres":
                        tables = get_postgres_tables(c_name, dbname)
                    elif engine == "mysql":
                        tables = get_mysql_tables(c_name, dbname)
                    else:
                        break

                    if not tables:
                        sys.stdout.write("\033[?25h")
                        sys.stdout.flush()
                        print(
                            f" (No tables found in {engine.upper()} database '{dbname}')"
                        )
                        input(" Press Enter to return...")
                        sys.stdout.write("\033[?25l")
                        sys.stdout.flush()
                        break

                    table_items = [{"name": t, "label": t} for t in tables]

                    selected_table, table_idx = select_menu_item(
                        f"Tables in {engine.upper()}: {dbname}",
                        table_items,
                        display_key="label",
                        initial_idx=table_idx,
                    )

                    if not selected_table:
                        break

                    t_name = selected_table["name"]

                    def query_table_fn(
                        table_name, limit, offset, filter_query=None, order_by=None
                    ):
                        if engine == "postgres":
                            return query_postgres_page(
                                c_name,
                                dbname,
                                table_name,
                                limit,
                                offset,
                                filter_query,
                                order_by=order_by,
                            )
                        return query_mysql_page(
                            c_name,
                            dbname,
                            table_name,
                            limit,
                            offset,
                            filter_query,
                            order_by=order_by,
                        )

                    def q_fn(limit, offset, filter_query=None, order_by=None):
                        return query_table_fn(
                            t_name, limit, offset, filter_query, order_by
                        )

                    def u_fn(headers, orig_row, updated_dict):
                        if engine == "postgres":
                            return update_postgres_row(
                                c_name, dbname, t_name, headers, orig_row, updated_dict
                            )
                        else:
                            return update_mysql_row(
                                c_name, dbname, t_name, headers, orig_row, updated_dict
                            )

                    def i_fn(row_dict):
                        if engine == "postgres":
                            return insert_postgres_row(c_name, dbname, t_name, row_dict)
                        else:
                            return insert_mysql_row(c_name, dbname, t_name, row_dict)

                    def d_fn(headers, orig_row):
                        if engine == "postgres":
                            return delete_postgres_row(
                                c_name, dbname, t_name, headers, orig_row
                            )
                        else:
                            return delete_mysql_row(
                                c_name, dbname, t_name, headers, orig_row
                            )

                    def s_fn():
                        if engine == "postgres":
                            return get_postgres_schema_info(c_name, dbname, t_name)
                        else:
                            return get_mysql_schema_info(c_name, dbname, t_name)

                    def fk_fn():
                        return get_foreign_keys(engine, c_name, dbname, t_name)

                    def count_table_fn(table_name, filter_query=None):
                        if engine == "postgres":
                            return count_postgres_rows(
                                c_name, dbname, table_name, filter_query
                            )
                        return count_mysql_rows(
                            c_name, dbname, table_name, filter_query
                        )

                    def count_fn_docker(filter_query=None):
                        return count_table_fn(t_name, filter_query)

                    def exec_raw_fn(raw_sql, limit, offset):
                        if engine == "postgres":
                            return execute_postgres_raw(
                                c_name, dbname, raw_sql, limit, offset
                            )
                        return execute_mysql_raw(
                            c_name, dbname, raw_sql, limit, offset
                        )

                    def table_callbacks_fn(table_name):
                        if engine == "postgres":
                            return {
                                "update_fn": lambda headers, row, updated: update_postgres_row(
                                    c_name, dbname, table_name, headers, row, updated
                                ),
                                "insert_fn": lambda row: insert_postgres_row(
                                    c_name, dbname, table_name, row
                                ),
                                "delete_fn": lambda headers, row: delete_postgres_row(
                                    c_name, dbname, table_name, headers, row
                                ),
                                "schema_fn": lambda: get_postgres_schema_info(
                                    c_name, dbname, table_name
                                ),
                                "fk_fn": lambda: get_foreign_keys(
                                    engine, c_name, dbname, table_name
                                ),
                                "count_fn": lambda filter_query=None: count_postgres_rows(
                                    c_name, dbname, table_name, filter_query
                                ),
                                "exec_raw_fn": lambda raw_sql, limit, offset: execute_postgres_raw(
                                    c_name, dbname, raw_sql, limit, offset
                                ),
                            }
                        return {
                            "update_fn": lambda headers, row, updated: update_mysql_row(
                                c_name, dbname, table_name, headers, row, updated
                            ),
                            "insert_fn": lambda row: insert_mysql_row(
                                c_name, dbname, table_name, row
                            ),
                            "delete_fn": lambda headers, row: delete_mysql_row(
                                c_name, dbname, table_name, headers, row
                            ),
                            "schema_fn": lambda: get_mysql_schema_info(
                                c_name, dbname, table_name
                            ),
                            "fk_fn": lambda: get_foreign_keys(
                                engine, c_name, dbname, table_name
                            ),
                            "count_fn": lambda filter_query=None: count_mysql_rows(
                                c_name, dbname, table_name, filter_query
                            ),
                            "exec_raw_fn": lambda raw_sql, limit, offset: execute_mysql_raw(
                                c_name, dbname, raw_sql, limit, offset
                            ),
                        }

                    browse_table(
                        q_fn,
                        update_fn=u_fn,
                        insert_fn=i_fn,
                        delete_fn=d_fn,
                        schema_fn=s_fn,
                        fk_fn=fk_fn,
                        count_fn=count_fn_docker,
                        exec_raw_fn=exec_raw_fn,
                        engine=engine,
                        container=c_name,
                        dbname_or_path=dbname,
                        all_tables=tables,
                        table_name=t_name,
                        read_only=args.read_only,
                        inspector_fn=lambda: get_index_and_constraint_info(
                            engine, c_name, dbname, t_name
                        ),
                        query_table_fn=query_table_fn,
                        count_table_fn=count_table_fn,
                        table_callbacks_fn=table_callbacks_fn,
                    )


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        exit_cleanup()
        sys.exit(130)
    except Exception as e:
        exit_cleanup()
        print(f"\n{RED}{BOLD}Startup Error:{RESET} {e}\n")
        traceback.print_exc()
