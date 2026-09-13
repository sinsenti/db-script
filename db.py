import csv
import difflib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import termios
import tty
import traceback
from pathlib import Path
from urllib.parse import urlparse, unquote

from dbcli.adapters import SQLiteAdapter
from dbcli.discovery import find_docker_databases, find_sqlite_files
from dbcli.export import export_rows_to_csv, export_rows_to_json, export_rows_to_sql

# Optional prompt_toolkit import for interactive SQL autocompletion
try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.filters import completion_is_selected
    from prompt_toolkit.application import run_in_terminal

    PROMPT_TOOLKIT_AVAILABLE = True
except ImportError:
    PROMPT_TOOLKIT_AVAILABLE = False

# Configuration
DB_KEYWORDS = ["postgres", "mysql", "mariadb", "sqlite"]
DB_PORTS = ["5432", "3306"]
MAX_COL_WIDTH = 35

_FK_CACHE = {}
CANCELLED = object()

# Terminal rendering, input and styling live in dbcli.ui.  These imports keep
# the existing db.py screens compatible while they are migrated incrementally.
from dbcli.ui import (
    BLUE,
    BOLD,
    CYAN,
    DIM,
    GREEN,
    MAGENTA,
    RED,
    RESET,
    REVERSE,
    YELLOW,
    confirm_action,
    copy_to_clipboard,
    draw_frame,
    enter_alt_screen,
    exit_cleanup,
    get_key,
    get_next_key_timeout,
    get_terminal_size,
    strip_ansi,
    visible_len,
)


# -------------------------------------------------------------------
#  FAST LIGHTWEIGHT JSON METADATA ANALYZER & FUZZY SQL AUTOCOMPLETE
# -------------------------------------------------------------------


def is_fuzzy_match(query, target):
    """Subsequence fuzzy matcher: checks if query characters appear sequentially in target."""
    if not query:
        return True
    q, t = query.lower(), target.lower()
    it = iter(t)
    return all(c in it for c in q)


def analyze_table_sample(headers, sample_rows):
    """
    Fast, depth-limited inspector for JSON keys and values.
    Optimized to prevent CPU spikes on large datasets.
    """
    column_values = {}
    path_subkeys = {}
    path_values = {}

    def traverse_json(obj, current_path, depth=0):
        if depth > 3:
            return
        if isinstance(obj, dict):
            path_subkeys.setdefault(current_path, set())
            for k, v in obj.items():
                if len(path_subkeys[current_path]) < 50:
                    path_subkeys[current_path].add(str(k))
                next_single = f"{current_path}->'{k}'"
                traverse_json(v, next_single, depth + 1)
        elif isinstance(obj, list):
            for item in obj[:10]:
                traverse_json(item, current_path, depth + 1)
        elif obj is not None:
            v_str = str(obj)
            if len(v_str) > 100:
                return
            key_name = (
                current_path.split("->")[-1].replace("'", "").replace(">", "").strip()
            )
            path_values.setdefault(key_name, set()).add(v_str)
            path_values.setdefault(current_path, set()).add(v_str)

            if "->" in current_path:
                idx = current_path.rfind("->")
                if idx != -1 and not current_path[idx:].startswith("->>"):
                    double_path = current_path[:idx] + "->>" + current_path[idx + 2 :]
                    path_values.setdefault(double_path, set()).add(v_str)

    for col_idx, col_name in enumerate(headers):
        col_vals = set()
        for row in sample_rows[:10]:
            if col_idx < len(row):
                val = row[col_idx]
                if val is not None and val != "":
                    val_str = str(val).strip()
                    if len(val_str) < 100:
                        col_vals.add(val_str)

                    if (val_str.startswith("{") and val_str.endswith("}")) or (
                        val_str.startswith("[") and val_str.endswith("]")
                    ):
                        try:
                            parsed = json.loads(val_str)
                            traverse_json(parsed, col_name)
                        except Exception:
                            pass
        if col_vals:
            column_values[col_name] = list(col_vals)[:15]

    path_subkeys_clean = {k: sorted(list(v)) for k, v in path_subkeys.items()}
    path_values_clean = {k: sorted(list(v))[:15] for k, v in path_values.items()}

    return column_values, path_subkeys_clean, path_values_clean


if PROMPT_TOOLKIT_AVAILABLE:

    class CustomSqlCompleter(Completer):
        """Context-aware fuzzy SQL completer for columns, nested JSON paths, and values."""

        def __init__(
            self, columns=None, column_values=None, path_subkeys=None, path_values=None
        ):
            self.columns = columns or []
            self.column_values = column_values or {}
            self.path_subkeys = path_subkeys or {}
            self.path_values = path_values or {}
            self.sql_keywords = [
                "WHERE",
                "AND",
                "OR",
                "NOT",
                "IN",
                "LIKE",
                "ILIKE",
                "IS NULL",
                "IS NOT NULL",
                "ORDER BY",
                "ASC",
                "DESC",
                "LIMIT",
                "::numeric",
                "::int",
                "::text",
            ]

        def get_completions(self, document, complete_event):
            text = document.text_before_cursor
            word = document.get_word_before_cursor()

            recent_text = text[-120:] if len(text) > 120 else text

            # 1. Value Completion
            val_match = re.search(
                r"([a-zA-Z0-9_`\'\->>:\(\)]+)\s*(=|!=|LIKE|ILIKE|IN|<|>|<=|>=)\s*\'?([a-zA-Z0-9_\- ]*)$",
                recent_text,
                re.IGNORECASE,
            )
            if val_match:
                field_expr, op, typed_val = val_match.groups()
                clean_field = field_expr.replace("`", "").strip()

                short_key = clean_field
                if "->" in clean_field:
                    short_key = (
                        clean_field.split("->")[-1]
                        .replace("'", "")
                        .replace(">", "")
                        .replace(")", "")
                        .strip()
                    )

                candidate_values = set()
                if clean_field in self.path_values:
                    candidate_values.update(self.path_values[clean_field])
                if short_key in self.path_values:
                    candidate_values.update(self.path_values[short_key])
                if clean_field in self.column_values:
                    candidate_values.update(self.column_values[clean_field])

                if candidate_values:
                    for val_str in sorted(list(candidate_values)):
                        if is_fuzzy_match(typed_val, val_str):
                            completion_val = (
                                f"'{val_str}'"
                                if not val_str.replace(".", "", 1).isdigit()
                                else val_str
                            )
                            yield Completion(
                                completion_val,
                                start_position=-len(typed_val),
                                display=f"{completion_val} (Value)",
                            )
                    return

            # 2. JSON Key Completion (When user types -> or ->>)
            json_key_match = re.search(
                r"([a-zA-Z0-9_]+(?:->>?\'[^\']*\')*)(->>|->)\s*(\'?)([a-zA-Z0-9_]*)$",
                recent_text,
            )
            if json_key_match:
                prefix_path, op, has_leading_quote, typed_key = json_key_match.groups()
                clean_path = prefix_path.replace("`", "").strip()

                if clean_path in self.path_subkeys:
                    start_pos = -(len(has_leading_quote) + len(typed_key))
                    for subkey in self.path_subkeys[clean_path]:
                        if is_fuzzy_match(typed_key, subkey):
                            yield Completion(
                                f"'{subkey}'",
                                start_position=start_pos,
                                display=f"'{subkey}' (JSON Key)",
                            )
                    return

            # 3. Fuzzy Column Name & Pre-built JSON Path Completions
            if not re.search(
                r"(=|!=|LIKE|ILIKE|IN|<|>|<=|>=)\s*$", recent_text, re.IGNORECASE
            ):
                for col in self.columns:
                    if is_fuzzy_match(word, col):
                        yield Completion(
                            col, start_position=-len(word), display=f"{col} (Column)"
                        )

                    if col in self.path_subkeys:
                        for subkey in self.path_subkeys[col]:
                            json_text_path = f"{col}->>'{subkey}'"
                            json_num_path = f"({col}->>'{subkey}')::numeric"

                            if is_fuzzy_match(word, json_text_path) or is_fuzzy_match(
                                word, f"{col}{subkey}"
                            ):
                                yield Completion(
                                    json_text_path,
                                    start_position=-len(word),
                                    display=f"{json_text_path} (JSON Text)",
                                )
                            if is_fuzzy_match(word, json_num_path) or is_fuzzy_match(
                                word, f"{col}{subkey}num"
                            ):
                                yield Completion(
                                    json_num_path,
                                    start_position=-len(word),
                                    display=f"{json_num_path} (Numeric JSON)",
                                )

                for kw in self.sql_keywords:
                    if is_fuzzy_match(word, kw):
                        yield Completion(
                            kw, start_position=-len(word), display=f"{kw} (Keyword)"
                        )

    class FullSqlShellCompleter(Completer):
        """Advanced SQL shell completer with smart JOIN ON foreign-key suggestions, tables, and columns."""

        def __init__(
            self, current_table=None, all_tables=None, columns=None, foreign_keys=None
        ):
            self.current_table = current_table or ""
            self.all_tables = all_tables or []
            self.columns = columns or []
            self.foreign_keys = foreign_keys or []
            self.sql_keywords = [
                "SELECT",
                f"SELECT * FROM {self.current_table}",
                "JOIN",
                "INNER JOIN",
                "LEFT JOIN",
                "RIGHT JOIN",
                "ON",
                "WHERE",
                "GROUP BY",
                "ORDER BY",
                "HAVING",
                "LIMIT 50",
            ]

        def get_completions(self, document, complete_event):
            text = document.text_before_cursor
            word = document.get_word_before_cursor()
            recent_text = text[-120:] if len(text) > 120 else text

            join_match = re.search(
                r"(?:JOIN|FROM)\s+([a-zA-Z0-9_]+)\s+([a-zA-Z0-9_\.\=]*)$",
                recent_text,
                re.IGNORECASE,
            )
            if join_match:
                tbl_name, word_typed = join_match.groups()
                for fk in self.foreign_keys:
                    if fk["to_table"].lower() == tbl_name.lower():
                        on_clause = f"ON {self.current_table}.{fk['from_col']} = {fk['to_table']}.{fk['to_col']}"
                        if is_fuzzy_match(word_typed, on_clause):
                            yield Completion(
                                on_clause,
                                start_position=-len(word_typed),
                                display=f"{on_clause} (FK Condition)",
                            )

            if re.search(
                r"(?:FROM|JOIN|INTO)\s+[a-zA-Z0-9_]*$", recent_text, re.IGNORECASE
            ):
                for tbl in self.all_tables:
                    if is_fuzzy_match(word, tbl):
                        yield Completion(
                            tbl, start_position=-len(word), display=f"{tbl} (Table)"
                        )

                        for fk in self.foreign_keys:
                            if fk["to_table"].lower() == tbl.lower():
                                full_join = f"{tbl} ON {self.current_table}.{fk['from_col']} = {tbl}.{fk['to_col']}"
                                yield Completion(
                                    full_join,
                                    start_position=-len(word),
                                    display=f"{tbl} + FK JOIN",
                                )
                return

            for col in self.columns:
                if is_fuzzy_match(word, col):
                    yield Completion(
                        col, start_position=-len(word), display=f"{col} (Column)"
                    )
                if self.current_table:
                    tbl_col = f"{self.current_table}.{col}"
                    if is_fuzzy_match(word, tbl_col):
                        yield Completion(
                            tbl_col,
                            start_position=-len(word),
                            display=f"{tbl_col} (Table.Col)",
                        )

            for tbl in self.all_tables:
                if is_fuzzy_match(word, tbl):
                    yield Completion(
                        tbl, start_position=-len(word), display=f"{tbl} (Table)"
                    )

            for kw in self.sql_keywords:
                if is_fuzzy_match(word, kw):
                    yield Completion(
                        kw, start_position=-len(word), display=f"{kw} (Keyword)"
                    )


def get_prompt_key_bindings():
    """Custom KeyBindings allowing 'Enter' to accept autocompletion, 'Esc' to cancel, and 'Ctrl+G' to edit in $EDITOR."""
    kb = KeyBindings()

    @kb.add("enter", filter=completion_is_selected)
    def _(event):
        b = event.current_buffer
        if b.complete_state and b.complete_state.current_completion:
            b.apply_completion(b.complete_state.current_completion)

    @kb.add("escape")
    def _(event):
        event.app.exit(exception=KeyboardInterrupt)

    @kb.add("c-g")
    def _(event):
        def run_editor():
            b = event.current_buffer
            edited = edit_text_in_nvim(b.text, suffix=".sql")
            b.text = edited
            b.cursor_position = len(edited)

        if PROMPT_TOOLKIT_AVAILABLE:
            run_in_terminal(run_editor)

    return kb


def prompt_with_autocomplete(prompt_str, completer=None, default=""):
    """Executes prompt_toolkit session cleanly on a fresh line below frame. Returns CANCELLED on Esc/Ctrl+C."""
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)

    try:
        sys.stdout.write("\033[?25h\n")
        sys.stdout.flush()
        termios.tcsetattr(fd, termios.TCSANOW, old_settings)

        if PROMPT_TOOLKIT_AVAILABLE:
            session = PromptSession(
                completer=completer, key_bindings=get_prompt_key_bindings()
            )
            res = session.prompt(prompt_str, default=default or "")
            return res.strip()
        else:
            prompt_label = f"{prompt_str}[{default}]: " if default else prompt_str
            res = input(prompt_label).strip()
            return res if res else (default or "")
    except (KeyboardInterrupt, EOFError):
        return CANCELLED
    finally:
        termios.tcsetattr(fd, termios.TCSANOW, old_settings)
        sys.stdout.write("\033[?25l")
        sys.stdout.flush()


def sanitize_where_clause(text):
    """Sanitizes accidental duplicate quotes after JSON operators (e.g. ->>''key'' -> ->>'key')."""
    if not text:
        return text
    return re.sub(r"(->>|->)\s*\'\'([^\']+)\'\'", r"\1'\2'", text)


def is_sql_where_expression(text):
    """Detects if user input is an explicit SQL WHERE clause expression."""
    if not text:
        return False
    tokens = [
        "=",
        "!=",
        ">",
        "<",
        "LIKE",
        "ILIKE",
        "IS NULL",
        "IS NOT NULL",
        "IN ",
        "->",
        "->>",
        "::",
    ]
    return any(tok in text.upper() for tok in tokens)


# -------------------------------------------------------------------
#  NVIM / $EDITOR INTEGRATIONS
# -------------------------------------------------------------------


def edit_text_in_nvim(initial_text, suffix=".sql"):
    """Opens $EDITOR (nvim) to edit raw text (e.g. SQL filter queries)."""
    with tempfile.NamedTemporaryFile(mode="w+", suffix=suffix, delete=False) as tf:
        tf.write(initial_text or "")
        temp_path = tf.name

    editor = os.environ.get("EDITOR")
    if not editor:
        editor = (
            "nvim"
            if shutil.which("nvim")
            else ("vim" if shutil.which("vim") else "nano")
        )

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)

    try:
        sys.stdout.write("\033[?25h\033[?1049l")
        sys.stdout.flush()
        termios.tcsetattr(fd, termios.TCSANOW, old_settings)
        subprocess.call([editor, temp_path])
    finally:
        termios.tcsetattr(fd, termios.TCSANOW, old_settings)
        termios.tcflush(fd, termios.TCIFLUSH)
        enter_alt_screen()

    try:
        with open(temp_path, "r", encoding="utf-8") as f:
            updated_text = f.read().strip()
        os.remove(temp_path)
        return updated_text
    except Exception as e:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        print(f"\n{RED}Error reading text from editor: {e}{RESET}")
        return initial_text


def launch_nvim_editor(data_dict):
    """Opens Neovim with single row data formatted as JSON."""
    edit_dict = {}
    json_fields = set()

    for key, val in data_dict.items():
        if isinstance(val, str) and (
            (val.startswith("{") and val.endswith("}"))
            or (val.startswith("[") and val.endswith("]"))
        ):
            try:
                edit_dict[key] = json.loads(val)
                json_fields.add(key)
                continue
            except json.JSONDecodeError:
                pass
        edit_dict[key] = val

    with tempfile.NamedTemporaryFile(mode="w+", suffix=".json", delete=False) as tf:
        json.dump(edit_dict, tf, indent=2, ensure_ascii=False)
        temp_path = tf.name

    editor = os.environ.get("EDITOR")
    if not editor:
        editor = (
            "nvim"
            if shutil.which("nvim")
            else ("vim" if shutil.which("vim") else "nano")
        )

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)

    try:
        sys.stdout.write("\033[?25h\033[?1049l")
        sys.stdout.flush()
        termios.tcsetattr(fd, termios.TCSANOW, old_settings)
        subprocess.call([editor, temp_path])
    finally:
        termios.tcsetattr(fd, termios.TCSANOW, old_settings)
        termios.tcflush(fd, termios.TCIFLUSH)
        enter_alt_screen()

    try:
        with open(temp_path, "r", encoding="utf-8") as f:
            updated_dict = json.load(f)
        os.remove(temp_path)
    except Exception as e:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        print(f"\n{RED}Error reading edited JSON or syntax error: {e}{RESET}")
        return None

    final_dict = {}
    for k, v in updated_dict.items():
        if k in json_fields and isinstance(v, (dict, list)):
            final_dict[k] = json.dumps(v)
        else:
            final_dict[k] = v

    return final_dict


def launch_nvim_insert_editor(headers):
    """Opens Neovim with a blank JSON object template pre-populated with column headers for row insertion."""
    if not headers:
        return None
    template = {h: None for h in headers}
    with tempfile.NamedTemporaryFile(mode="w+", suffix=".json", delete=False) as tf:
        json.dump(template, tf, indent=2, ensure_ascii=False)
        temp_path = tf.name

    editor = os.environ.get("EDITOR")
    if not editor:
        editor = (
            "nvim"
            if shutil.which("nvim")
            else ("vim" if shutil.which("vim") else "nano")
        )

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)

    try:
        sys.stdout.write("\033[?25h\033[?1049l")
        sys.stdout.flush()
        termios.tcsetattr(fd, termios.TCSANOW, old_settings)
        subprocess.call([editor, temp_path])
    finally:
        termios.tcsetattr(fd, termios.TCSANOW, old_settings)
        termios.tcflush(fd, termios.TCIFLUSH)
        enter_alt_screen()

    try:
        with open(temp_path, "r", encoding="utf-8") as f:
            inserted_data = json.load(f)
        os.remove(temp_path)
    except Exception as e:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        print(f"\n{RED}Error reading inserted JSON or syntax error: {e}{RESET}")
        return None

    if isinstance(inserted_data, dict):
        return [inserted_data]
    elif isinstance(inserted_data, list):
        return [d for d in inserted_data if isinstance(d, dict)]
    return None


def launch_nvim_table_editor(headers, rows):
    """Opens Neovim with entire table data formatted as a JSON array of objects."""
    if not headers or not rows:
        return None

    json_fields = set()
    table_data = []

    for row in rows:
        row_dict = {}
        for k, val in zip(headers, row):
            if isinstance(val, str) and (
                (val.startswith("{") and val.endswith("}"))
                or (val.startswith("[") and val.endswith("]"))
            ):
                try:
                    row_dict[k] = json.loads(val)
                    json_fields.add(k)
                    continue
                except json.JSONDecodeError:
                    pass
            row_dict[k] = val
        table_data.append(row_dict)

    with tempfile.NamedTemporaryFile(mode="w+", suffix=".json", delete=False) as tf:
        json.dump(table_data, tf, indent=2, ensure_ascii=False)
        temp_path = tf.name

    editor = os.environ.get("EDITOR")
    if not editor:
        editor = (
            "nvim"
            if shutil.which("nvim")
            else ("vim" if shutil.which("vim") else "nano")
        )

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)

    try:
        sys.stdout.write("\033[?25h\033[?1049l")
        sys.stdout.flush()
        termios.tcsetattr(fd, termios.TCSANOW, old_settings)
        subprocess.call([editor, temp_path])
    finally:
        termios.tcsetattr(fd, termios.TCSANOW, old_settings)
        termios.tcflush(fd, termios.TCIFLUSH)
        enter_alt_screen()

    try:
        with open(temp_path, "r", encoding="utf-8") as f:
            updated_table_data = json.load(f)
        os.remove(temp_path)
    except Exception as e:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        print(f"\n{RED}Error reading edited JSON or syntax error: {e}{RESET}")
        return None

    if not isinstance(updated_table_data, list):
        print(f"\n{RED}Error: Expected top-level JSON array of row objects.{RESET}")
        return None

    final_table_data = []
    for row_dict in updated_table_data:
        if isinstance(row_dict, dict):
            final_row = {}
            for k, v in row_dict.items():
                if k in json_fields and isinstance(v, (dict, list)):
                    final_row[k] = json.dumps(v)
                else:
                    final_row[k] = v
            final_table_data.append(final_row)

    return final_table_data


# -------------------------------------------------------------------
#  GIT-STYLE UNIFIED DIFF PAGER
# -------------------------------------------------------------------


def generate_ansi_unified_diff(orig_data, new_data, file_name="table.json"):
    """Generates Git-style ANSI colorized unified diff lines for JSON object arrays."""
    orig_lines = json.dumps(orig_data, indent=2, ensure_ascii=False).splitlines(
        keepends=True
    )
    new_lines = json.dumps(new_data, indent=2, ensure_ascii=False).splitlines(
        keepends=True
    )

    raw_diff = list(
        difflib.unified_diff(
            orig_lines, new_lines, fromfile=f"a/{file_name}", tofile=f"b/{file_name}"
        )
    )

    if not raw_diff:
        return []

    colored_lines = []
    for line in raw_diff:
        l_str = line.rstrip("\r\n")
        if l_str.startswith("---") or l_str.startswith("+++"):
            colored_lines.append(f"{BOLD}{CYAN}{l_str}{RESET}")
        elif l_str.startswith("@@"):
            colored_lines.append(f"{BOLD}{MAGENTA}{l_str}{RESET}")
        elif l_str.startswith("-"):
            colored_lines.append(f"{RED}{l_str}{RESET}")
        elif l_str.startswith("+"):
            colored_lines.append(f"{GREEN}{l_str}{RESET}")
        else:
            colored_lines.append(f"{DIM}{l_str}{RESET}")

    return colored_lines


def display_diff_pager(diff_lines, summary_str):
    """Scrollable Vim pager displaying unified color diff before DB commit."""
    line_offset = 0
    while True:
        term_width, term_height = get_terminal_size()
        viewport_height = max(5, term_height - 6)

        max_offset = max(0, len(diff_lines) - viewport_height)
        line_offset = max(0, min(line_offset, max_offset))

        frame_lines = []
        pct = (
            int(((line_offset + viewport_height) / len(diff_lines)) * 100)
            if max_offset > 0
            else 100
        )
        pct = min(100, pct)

        frame_lines.append(
            f" {BOLD}{CYAN}Git-Style Unified Diff Preview{RESET} [{summary_str}] "
            f"[{line_offset + 1}-{min(len(diff_lines), line_offset + viewport_height)}/{len(diff_lines)} lines ({pct}%)]"
        )
        frame_lines.append(f"{DIM}{'-' * min(term_width - 2, 98)}{RESET}")

        frame_lines.extend(diff_lines[line_offset : line_offset + viewport_height])

        frame_lines.append("")
        frame_lines.append(
            f" {BOLD}Actions:{RESET} {GREEN}{BOLD}y{RESET} Apply Changes | "
            f"{RED}{BOLD}n/q/Esc{RESET} Discard | "
            f"{YELLOW}j/k{RESET} Scroll | {YELLOW}gg/G{RESET} Top/Bottom"
        )

        draw_frame(frame_lines)
        key = get_key()

        if key in ("j", "J"):
            line_offset += 1
        elif key in ("k", "K"):
            line_offset -= 1
        elif key in ("CTRL_D", "SPACE"):
            line_offset += viewport_height // 2
        elif key == "CTRL_U":
            line_offset -= viewport_height // 2
        elif key == "g":
            if get_next_key_timeout(0.3) == "g":
                line_offset = 0
        elif key == "G":
            line_offset = max_offset
        elif key.lower() == "y":
            return True
        elif key.lower() in ("n", "q", "b") or key == "q":
            return False


# -------------------------------------------------------------------
#  COLUMN VISIBILITY TOGGLE MENU
# -------------------------------------------------------------------


def toggle_column_visibility(headers, hidden_cols):
    """Interactive toggle menu allowing users to mark columns as visible [X] or hidden [ ]."""
    cursor_idx = 0
    while True:
        term_width, _ = get_terminal_size()
        card_width = min(term_width - 2, 98)
        lines = []

        lines.append(f"{CYAN}{'=' * card_width}{RESET}")
        lines.append(
            f"{BOLD}{YELLOW}Column Visibility Settings{RESET}".center(card_width + 8)
        )
        lines.append(f"{CYAN}{'=' * card_width}{RESET}")
        lines.append(
            f" {DIM}Select columns to show or hide in ASCII table view:{RESET}"
        )
        lines.append("")

        for idx, col in enumerate(headers):
            is_visible = col not in hidden_cols
            status_tag = f"{GREEN}[X]{RESET}" if is_visible else f"{DIM}[ ]{RESET}"
            is_selected = idx == cursor_idx

            line_text = f"   {status_tag} {col}"
            if is_selected:
                lines.append(f"{REVERSE}{BOLD}{line_text:<{card_width}}{RESET}")
            else:
                lines.append(line_text)

        lines.append("")
        lines.append(f"{CYAN}{'-' * card_width}{RESET}")
        lines.append(
            f" {BOLD}Controls:{RESET} {YELLOW}j/k{RESET} Up/Down | "
            f"{YELLOW}SPACE/Enter{RESET} Toggle | "
            f"{YELLOW}a{RESET} Toggle All | "
            f"{YELLOW}h/q/Esc{RESET} Return"
        )

        draw_frame(lines)
        key = get_key()

        if key in ("j", "J"):
            cursor_idx = min(cursor_idx + 1, len(headers) - 1)
        elif key in ("k", "K"):
            cursor_idx = max(0, cursor_idx - 1)
        elif key in ("SPACE", "ENTER"):
            target_col = headers[cursor_idx]
            if target_col in hidden_cols:
                hidden_cols.remove(target_col)
            else:
                if len(headers) - len(hidden_cols) > 1:
                    hidden_cols.add(target_col)
        elif key.lower() == "a":
            if len(hidden_cols) > 0:
                hidden_cols.clear()
            else:
                hidden_cols.update(headers[1:])
        elif key in ("h", "q", "b"):
            break


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

            # Indexes
            sql_idx = f"SELECT indexname, indexdef FROM pg_indexes WHERE tablename = '{table_name}' AND schemaname = 'public';"
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
                f"FROM pg_constraint WHERE conrelid = '\"{table_name}\"'::regclass;"
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
            sql_trg = f"SELECT trigger_name, event_manipulation, action_statement FROM information_schema.triggers WHERE event_object_table = '{table_name}';"
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
#  GENERIC VIM + NUMBER MENU SELECTOR WITH SEARCH / FILTER
# -------------------------------------------------------------------


def select_menu_item(title, items, display_key=None, subtext=None, initial_idx=0):
    if not items:
        return None, 0

    cursor_idx = min(max(0, initial_idx), len(items) - 1)
    filter_text = None

    while True:
        active_items = items
        if filter_text:
            active_items = [
                item
                for item in items
                if filter_text.lower()
                in (
                    item[display_key]
                    if display_key and isinstance(item, dict)
                    else str(item)
                ).lower()
            ]

        if not active_items:
            cursor_idx = 0
        else:
            cursor_idx = max(0, min(cursor_idx, len(active_items) - 1))

        term_width, _ = get_terminal_size()
        card_width = min(term_width - 2, 98)
        lines = []

        lines.append(f"{CYAN}{'=' * card_width}{RESET}")
        title_centered = title.center(card_width)
        lines.append(f"{BOLD}{YELLOW}{title_centered}{RESET}")
        lines.append(f"{CYAN}{'=' * card_width}{RESET}")

        current_subtext = subtext or ""
        if filter_text:
            current_subtext += (
                f" [Filter: '{filter_text}']"
                if current_subtext
                else f"[Filter: '{filter_text}']"
            )

        if current_subtext:
            lines.append(f" {DIM}{current_subtext}{RESET}")
            lines.append("")

        if not active_items:
            lines.append(f"   {RED}(No matching items for '{filter_text}'){RESET}")
        else:
            for idx, item in enumerate(active_items, start=1):
                label = (
                    item[display_key]
                    if display_key and isinstance(item, dict)
                    else str(item)
                )
                is_selected = idx - 1 == cursor_idx

                num_tag = f"[{idx}] "
                max_label_width = card_width - 6
                truncated_label = label[:max_label_width]

                if is_selected:
                    padded_line = f"   {num_tag}{truncated_label}".ljust(card_width)
                    lines.append(f"{REVERSE}{BOLD}{padded_line}{RESET}")
                else:
                    lines.append(f"   {BOLD}{num_tag}{RESET}{truncated_label}")

        lines.append("")
        lines.append(f"{CYAN}{'-' * card_width}{RESET}")
        lines.append(
            f" {BOLD}Vim Nav:{RESET} {YELLOW}j/k{RESET} Up/Down | "
            f"{YELLOW}gg/G{RESET} Jump | "
            f"{YELLOW}l/Enter{RESET} Select | "
            f"{YELLOW}h/q{RESET} Back | "
            f"{YELLOW}/s{RESET} Search | "
            f"{YELLOW}c{RESET} Clear | "
            f"{YELLOW}1-{min(9, len(active_items))}{RESET} Pick"
        )

        draw_frame(lines)
        key = get_key()

        if key in ("j", "J"):
            if active_items:
                cursor_idx = min(cursor_idx + 1, len(active_items) - 1)
        elif key in ("k", "K"):
            if active_items:
                cursor_idx = max(cursor_idx - 1, 0)
        elif key == "g":
            if get_next_key_timeout(0.3) == "g":
                cursor_idx = 0
        elif key == "G":
            if active_items:
                cursor_idx = len(active_items) - 1
        elif key in ("ENTER", "l"):
            if active_items:
                chosen = active_items[cursor_idx]
                real_idx = items.index(chosen) if chosen in items else 0
                return chosen, real_idx
        elif key in "123456789":
            num = int(key) - 1
            if 0 <= num < len(active_items):
                chosen = active_items[num]
                real_idx = items.index(chosen) if chosen in items else 0
                return chosen, real_idx
        elif key in ("/", "s", "S"):
            new_filter = prompt_with_autocomplete(
                " Search: ", default=filter_text or ""
            )
            if new_filter is not CANCELLED:
                filter_text = new_filter if new_filter else None
            cursor_idx = 0
        elif key.lower() == "c":
            filter_text = None
            cursor_idx = 0
        elif key in ("h", "q", "b"):
            return None, cursor_idx


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
        "SELECT tablename FROM pg_tables WHERE schemaname='public';",
    ]
    out, _ = run_docker_cmd_with_err(cmd)
    return sorted([t for t in out.split("\n") if t]) if out else []


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

    order_sql = f'ORDER BY t."{order_by[0]}" {order_by[1]}' if order_by else ""
    sql = f'COPY (SELECT * FROM "{table}" t {where_sql} {order_sql} LIMIT {limit} OFFSET {offset}) TO STDOUT WITH CSV HEADER;'
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
    sql = f'UPDATE "{table_name}" SET {set_clause} WHERE {where_sql};'
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
    sql = f'INSERT INTO "{table_name}" ({cols_sql}) VALUES ({vals_sql});'
    cmd = ["psql", conn_url, "-c", sql]
    _, err = run_docker_cmd_with_err(cmd)
    return err is None


def delete_direct_postgres_row(conn_url, table_name, headers, orig_row):
    where_sql = build_where_clause(headers, orig_row)
    sql = f'DELETE FROM "{table_name}" WHERE {where_sql};'
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
    sql = f'SELECT COUNT(*) FROM "{table_name}" t {where_sql};'
    cmd = ["psql", conn_url, "-At", "-c", sql]
    out, _ = run_docker_cmd_with_err(cmd)
    try:
        return int(out.strip()) if out else 0
    except Exception:
        return 0


def get_direct_postgres_schema_info(conn_url, table_name):
    sql = (
        f"SELECT column_name, data_type, is_nullable, column_default "
        f"FROM information_schema.columns WHERE table_name = '{table_name}' ORDER BY ordinal_position;"
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
    sql = (
        "SELECT a1.attname AS from_col, c2.relname AS to_table, a2.attname AS to_col "
        "FROM pg_constraint k "
        "JOIN pg_class c1 ON c1.oid = k.conrelid "
        "JOIN pg_namespace n1 ON n1.oid = c1.relnamespace "
        "JOIN pg_class c2 ON c2.oid = k.confrelid "
        "JOIN pg_attribute a1 ON a1.attrelid = c1.oid AND a1.attnum = k.conkey[1] "
        "JOIN pg_attribute a2 ON a2.attrelid = c2.oid AND a2.attnum = k.confkey[1] "
        f"WHERE k.contype = 'f' AND c1.relname = '{table_name}' AND n1.nspname = 'public';"
    )
    cmd = ["psql", conn_url, "-At", "-F", "|", "-c", sql]
    output, _ = run_docker_cmd_with_err(cmd)
    fks = []
    if output:
        for line in output.split("\n"):
            if line.strip():
                p = line.split("|")
                if len(p) == 3:
                    fks.append({"from_col": p[0], "to_table": p[1], "to_col": p[2]})
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
            sql = (
                "SELECT a1.attname AS from_col, c2.relname AS to_table, a2.attname AS to_col "
                "FROM pg_constraint k "
                "JOIN pg_class c1 ON c1.oid = k.conrelid "
                "JOIN pg_namespace n1 ON n1.oid = c1.relnamespace "
                "JOIN pg_class c2 ON c2.oid = k.confrelid "
                "JOIN pg_attribute a1 ON a1.attrelid = c1.oid AND a1.attnum = k.conkey[1] "
                "JOIN pg_attribute a2 ON a2.attrelid = c2.oid AND a2.attnum = k.confkey[1] "
                f"WHERE k.contype = 'f' AND c1.relname = '{table_name}' AND n1.nspname = 'public';"
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
                        if len(p) == 3:
                            fks.append(
                                {"from_col": p[0], "to_table": p[1], "to_col": p[2]}
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


def count_sqlite_rows(db_path, table_name, filter_query=None):
    """Compatibility wrapper around :class:`SQLiteAdapter`."""
    try:
        return SQLiteAdapter(db_path).count_rows(table_name, filter_query)
    except Exception:
        return 0


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

    sql = f'SELECT COUNT(*) FROM "{table_name}" t {where_sql};'
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


def get_sqlite_schema_info(db_path, table_name):
    """Compatibility wrapper around :class:`SQLiteAdapter`."""
    result = SQLiteAdapter(db_path).schema_info(table_name)
    return result.as_tuple()


def get_postgres_schema_info(container, dbname, table_name):
    """Fetches schema info for Postgres table."""
    user = get_container_env_user(container, "postgres")
    sql = (
        f"SELECT column_name, data_type, is_nullable, column_default "
        f"FROM information_schema.columns WHERE table_name = '{table_name}' ORDER BY ordinal_position;"
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
#  TERMINAL FORMATTING UTILITIES & COLUMN CALCULATOR
# -------------------------------------------------------------------


def compute_cell_visible_len(val, max_col_w=MAX_COL_WIDTH):
    if val is None or val == "":
        return 4
    s = str(val).replace("\n", " ").strip()
    if (s.startswith("{") and s.endswith("}")) or (
        s.startswith("[") and s.endswith("]")
    ):
        s_preview = f"{s[:14]}...{s[-3:]}" if len(s) > 20 else s
        return len(s_preview)
    return min(len(s), max_col_w)


def compute_column_widths(headers, rows, max_col_w=MAX_COL_WIDTH):
    """Pre-calculates exact column widths across ALL rows on the current page."""
    widths = []
    for c_idx, h in enumerate(headers):
        w = len(str(h))
        for r in rows:
            if c_idx < len(r):
                w = max(w, compute_cell_visible_len(r[c_idx], max_col_w))
        widths.append(min(w, max_col_w))
    return widths


def get_fitted_columns(headers, rows, col_start_idx, term_width):
    """Selects columns that fit strictly inside term_width - 2."""
    if not headers or not rows:
        return col_start_idx, col_start_idx + 1, []

    all_col_widths = compute_column_widths(headers, rows)
    target_width = max(20, term_width - 2)

    used_width = 1
    col_end_idx = col_start_idx

    while col_end_idx < len(headers):
        w = min(all_col_widths[col_end_idx], target_width - 3)
        needed = w + 3
        if used_width + needed > target_width and col_end_idx > col_start_idx:
            break
        used_width += needed
        col_end_idx += 1

    fitted_widths = [
        min(all_col_widths[i], target_width - 3)
        for i in range(col_start_idx, col_end_idx)
    ]
    return col_start_idx, col_end_idx, fitted_widths


def build_ascii_table_lines(
    headers,
    rows,
    col_widths,
    cursor_idx=0,
    marked_row_indices=None,
    focused_col_idx=None,
    c_start=0,
    sort_col=None,
    sort_dir=None,
):
    """Renders ASCII table with active multi-select marks via first-column color styling and column focus across all pages."""
    lines = []
    if not headers or not rows or not col_widths:
        lines.append(f"   {DIM}(No data found){RESET}")
        return lines

    marked_row_indices = marked_row_indices or set()

    def format_cell(val, col_w):
        if val is None or val == "":
            return f"{DIM}{'NULL':<{col_w}}{RESET}"

        s = str(val).replace("\n", " ").strip()

        if (s.startswith("{") and s.endswith("}")) or (
            s.startswith("[") and s.endswith("]")
        ):
            s_preview = f"{s[:14]}...{s[-3:]}" if len(s) > 20 else s
            if len(s_preview) > col_w:
                s_preview = (
                    s_preview[: col_w - 3] + "..." if col_w > 3 else s_preview[:col_w]
                )
            return f"{DIM}{s_preview:<{col_w}}{RESET}"

        if len(s) > col_w:
            s_trunc = s[: col_w - 3] + "..." if col_w > 3 else s[:col_w]
            return f"{CYAN}{s_trunc:<{col_w}}{RESET}"

        return f"{s:<{col_w}}"

    sep = "+" + "+".join("-" * (w + 2) for w in col_widths) + "+"
    lines.append(f"{DIM}{sep}{RESET}")

    hdr_cells = []
    for i in range(len(headers)):
        abs_c_idx = c_start + i
        h_name = str(headers[i])
        if sort_col and h_name == sort_col:
            h_name += " ▲" if sort_dir == "ASC" else " ▼"

        cell_str = f"{h_name:<{col_widths[i]}}"
        if focused_col_idx is not None and abs_c_idx == focused_col_idx:
            hdr_cells.append(f"{REVERSE}{BOLD}{cell_str}{RESET}")
        else:
            hdr_cells.append(f"{BOLD}{cell_str}{RESET}")

    header_str = "| " + " | ".join(hdr_cells) + " |"
    lines.append(header_str)
    lines.append(f"{DIM}{sep.replace('-', '=')}{RESET}")

    for r_idx, row in enumerate(rows):
        is_marked = r_idx in marked_row_indices

        row_cells = []
        for c_idx in range(len(headers)):
            cell_val = row[c_idx] if c_idx < len(row) else None
            cell_formatted = format_cell(cell_val, col_widths[c_idx])
            if is_marked and c_idx == 0:
                plain_s = strip_ansi(cell_formatted)
                cell_formatted = f"{GREEN}{BOLD}{plain_s}{RESET}"
            row_cells.append(cell_formatted)

        line = "| " + " | ".join(row_cells) + " |"

        if r_idx == cursor_idx:
            lines.append(f"{REVERSE}{BOLD}{line}{RESET}")
        else:
            lines.append(line)

    lines.append(f"{DIM}{sep}{RESET}")
    return lines


# -------------------------------------------------------------------
#  VIM-STYLE ROW DETAIL PAGER
# -------------------------------------------------------------------


def build_vertical_card_lines(headers, row, row_num):
    term_width, _ = get_terminal_size()
    card_width = min(term_width - 2, 98)
    lines = []

    lines.append(f"{CYAN}{'=' * card_width}{RESET}")
    title_str = f"ROW #{row_num} DETAIL VIEW".center(card_width)
    lines.append(f"{BOLD}{YELLOW}{title_str}{RESET}")
    lines.append(f"{CYAN}{'=' * card_width}{RESET}")

    max_hdr_len = max(len(str(h)) for h in headers) if headers else 15

    for header, val in zip(headers, row):
        hdr_str = f"{BOLD}{MAGENTA}{str(header):<{max_hdr_len}}{RESET}"

        if val is None or val == "":
            lines.append(f"   {hdr_str} : {DIM}NULL{RESET}")
            continue

        val_str = str(val).strip()

        if (val_str.startswith("{") and val_str.endswith("}")) or (
            val_str.startswith("[") and val_str.endswith("]")
        ):
            try:
                parsed = json.loads(val_str)
                pretty_json = json.dumps(parsed, indent=2)
                lines.append(f"   {hdr_str} : {GREEN}(JSON/Array){RESET}")
                for line in pretty_json.split("\n"):
                    lines.append(f"     {DIM}{line}{RESET}")
                continue
            except json.JSONDecodeError:
                pass

        if val_str in ["t", "f", "true", "false"]:
            colored_val = f"{BLUE}{val_str}{RESET}"
        elif any(k in header.lower() for k in ["status", "state"]):
            colored_val = f"{YELLOW}{val_str}{RESET}"
        elif any(k in header.lower() for k in ["created", "updated", "date", "at"]):
            colored_val = f"{CYAN}{val_str}{RESET}"
        else:
            colored_val = val_str

        lines.append(f"   {hdr_str} : {colored_val}")

    lines.append(f"{CYAN}{'=' * card_width}{RESET}")
    return lines


def browse_row_detail(headers, row, row_num, update_fn=None, delete_fn=None):
    card_lines = build_vertical_card_lines(headers, row, row_num)
    line_offset = 0

    while True:
        term_width, term_height = get_terminal_size()
        viewport_height = max(5, term_height - 5)

        max_offset = max(0, len(card_lines) - viewport_height)
        line_offset = max(0, min(line_offset, max_offset))

        frame_lines = []
        pct = (
            int(((line_offset + viewport_height) / len(card_lines)) * 100)
            if max_offset > 0
            else 100
        )
        pct = min(100, pct)
        frame_lines.append(
            f" {BOLD}{CYAN}Row #{row_num} Detail Pager{RESET} "
            f"[{line_offset + 1}-{min(len(card_lines), line_offset + viewport_height)}/{len(card_lines)} lines ({pct}%)]"
        )
        frame_lines.append(f"{DIM}{'-' * min(term_width - 2, 98)}{RESET}")

        frame_lines.extend(card_lines[line_offset : line_offset + viewport_height])

        frame_lines.append("")
        frame_lines.append(
            f" {BOLD}Vim Nav:{RESET} {YELLOW}j/k{RESET} Line Up/Down | "
            f"{YELLOW}e{RESET} Edit (nvim) | "
            f"{YELLOW}dd{RESET} Delete | "
            f"{YELLOW}gg/G{RESET} Top/Bottom | "
            f"{YELLOW}h/q/Esc/Enter{RESET} Return"
        )

        draw_frame(frame_lines)
        key = get_key()

        if key in ("j", "J"):
            line_offset += 1
        elif key in ("k", "K"):
            line_offset -= 1
        elif key in ("CTRL_D", "SPACE"):
            line_offset += viewport_height // 2
        elif key == "CTRL_U":
            line_offset -= viewport_height // 2
        elif key == "g":
            if get_next_key_timeout(0.3) == "g":
                line_offset = 0
        elif key == "G":
            line_offset = max_offset
        elif key.lower() == "e":
            orig_dict = dict(zip(headers, row))
            updated_dict = launch_nvim_editor(orig_dict)
            if updated_dict and update_fn:
                success = update_fn(headers, row, updated_dict)
                if success:
                    sys.stdout.write("\033[?25h")
                    sys.stdout.flush()
                    print(f"\n{GREEN}Success! Row updated in database.{RESET}")
                    print(f" {DIM}Press any key to return...{RESET}")
                    get_key()
                    sys.stdout.write("\033[?25l")
                    sys.stdout.flush()
                    break
        elif key == "d":
            if get_next_key_timeout(0.3) == "d":
                if delete_fn:
                    if confirm_action(
                        f"Are you sure you want to delete row #{row_num}?"
                    ):
                        success = delete_fn(headers, row)
                        if success:
                            sys.stdout.write("\033[?25h")
                            sys.stdout.flush()
                            print(
                                f"\n{GREEN}Success! Row deleted from database.{RESET}"
                            )
                            print(f" {DIM}Press any key to return...{RESET}")
                            get_key()
                            sys.stdout.write("\033[?25l")
                            sys.stdout.flush()
                            return True
        elif key in ("h", "q", "b", "ENTER"):
            break

    return False


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


def insert_sqlite_row(db_path, table_name, row_dict):
    """Compatibility wrapper around :class:`SQLiteAdapter`."""
    try:
        return SQLiteAdapter(db_path).insert_row(table_name, row_dict) == 1
    except Exception as e:
        print(f"\n{RED}Database Insert Error: {e}{RESET}")
        return False


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
    sql = f'INSERT INTO "{table_name}" ({cols_sql}) VALUES ({vals_sql});'
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


def update_sqlite_row(db_path, table_name, headers, orig_row, updated_dict):
    """Compatibility wrapper around :class:`SQLiteAdapter`."""
    try:
        original = dict(zip(headers, orig_row))
        return SQLiteAdapter(db_path).update_row(table_name, original, updated_dict) == 1
    except Exception as e:
        print(f"\n{RED}Database Update Error: {e}{RESET}")
        return False


def delete_sqlite_row(db_path, table_name, headers, orig_row):
    """Compatibility wrapper around :class:`SQLiteAdapter`."""
    try:
        original = dict(zip(headers, orig_row))
        return SQLiteAdapter(db_path).delete_row(table_name, original) == 1
    except Exception as e:
        print(f"\n{RED}Database Delete Error: {e}{RESET}")
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

    sql = f'UPDATE "{table_name}" SET {set_clause} WHERE {where_sql};'
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

    sql = f'DELETE FROM "{table_name}" WHERE {where_sql};'
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
#  SQLITE ENGINE
# -------------------------------------------------------------------


def get_sqlite_tables(db_path):
    """Compatibility wrapper around :class:`SQLiteAdapter`."""
    return SQLiteAdapter(db_path).list_tables()


def get_sqlite_row_count(db_path, table_name):
    try:
        return SQLiteAdapter(db_path).count_rows(table_name)
    except Exception:
        return 0


def query_sqlite_page(
    db_path, table_name, limit, offset, filter_query=None, order_by=None
):
    """Compatibility wrapper around :class:`SQLiteAdapter`."""
    try:
        return SQLiteAdapter(db_path).fetch_page(
            table_name, limit, offset, filter_query, order_by
        ).as_tuple()
    except Exception as e:
        return ["SQL Error"], [[str(e)]]


def execute_sqlite_raw(db_path, sql_query, limit=50, offset=0):
    """Compatibility wrapper around the bounded read-only query API."""
    try:
        return SQLiteAdapter(db_path).execute_readonly(sql_query, limit=limit, offset=offset).as_tuple()
    except Exception as e:
        return ["SQL Error"], [[str(e)]]


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
        "SELECT tablename FROM pg_tables WHERE schemaname='public';",
    ]
    output = run_docker_cmd(cmd)
    return sorted([t for t in output.split("\n") if t]) if output else []


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

    order_sql = f'ORDER BY t."{order_by[0]}" {order_by[1]}' if order_by else ""

    sql = f'COPY (SELECT * FROM "{table}" t {where_sql} {order_sql} LIMIT {limit} OFFSET {offset}) TO STDOUT WITH CSV HEADER;'
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


# -------------------------------------------------------------------
#  INTERACTIVE DATA BROWSER CONTROLLER
# -------------------------------------------------------------------


def browse_table(
    query_fn,
    update_fn=None,
    insert_fn=None,
    delete_fn=None,
    schema_fn=None,
    fk_fn=None,
    count_fn=None,
    exec_raw_fn=None,
    engine=None,
    container=None,
    dbname_or_path=None,
    all_tables=None,
    table_name="table",
    total_rows=None,
    **kwargs,
):
    """Interactive table browser supporting visual multi-select, FK nav, raw SQL shell with JOIN completions, exports & jump shortcuts."""
    # Optional adapter bridge used during the incremental Python refactor.
    # Legacy callback arguments remain supported for the other backends.
    backend_adapter = kwargs.get("adapter")
    offset = 0
    col_offset = 0
    cursor_idx = 0
    focused_col_idx = 0
    filter_text = None
    sort_col = None
    sort_dir = None

    marked_rows = set()
    hidden_cols = set()
    cached_total_count = None

    cached_headers, cached_values, cached_subkeys, cached_path_vals = (
        None,
        None,
        None,
        None,
    )

    while True:
        term_width, term_height = get_terminal_size()
        page_size = max(3, term_height - 7)

        if cached_total_count is None:
            if count_fn:
                cached_total_count = count_fn(filter_query=filter_text)
            else:
                cached_total_count = total_rows

        order_by = (sort_col, sort_dir) if sort_col and sort_dir else None
        headers, rows = query_fn(
            limit=page_size, offset=offset, filter_query=filter_text, order_by=order_by
        )

        if rows:
            cursor_idx = max(0, min(cursor_idx, len(rows) - 1))
        else:
            cursor_idx = 0

        # Apply Column Visibility filtering if user toggled columns
        disp_headers = headers
        disp_rows = rows
        if headers and hidden_cols:
            visible_indices = [i for i, h in enumerate(headers) if h not in hidden_cols]
            if visible_indices:
                disp_headers = [headers[i] for i in visible_indices]
                disp_rows = [
                    [r[i] for i in visible_indices if i < len(r)] for r in rows
                ]

        if disp_headers and disp_rows:
            focused_col_idx = max(0, min(focused_col_idx, len(disp_headers) - 1))

            if focused_col_idx < col_offset:
                col_offset = focused_col_idx

            c_start, c_end, fitted_widths = get_fitted_columns(
                disp_headers, disp_rows, col_offset, term_width
            )

            while focused_col_idx >= c_end and col_offset < len(disp_headers) - 1:
                col_offset += 1
                c_start, c_end, fitted_widths = get_fitted_columns(
                    disp_headers, disp_rows, col_offset, term_width
                )

            sliced_headers = disp_headers[c_start:c_end]
            sliced_rows = [r[c_start:c_end] for r in disp_rows]
            col_status = f"Cols {c_start + 1}-{c_end}/{len(disp_headers)}"
        else:
            c_start = 0
            sliced_headers, sliced_rows, fitted_widths = disp_headers, disp_rows, []
            col_status = "Cols 0-0"

        frame_lines = []
        status = f"Rows {offset + 1}-{offset + len(rows)}" if rows else "Rows 0-0"
        if cached_total_count is not None:
            status += f" of {cached_total_count}"
        if marked_rows:
            status += f" [{len(marked_rows)} marked]"
        if sort_col and sort_dir:
            status += f" [Sort: {sort_col} {sort_dir}]"
        if filter_text:
            status += f" (Filter: '{filter_text}')"

        frame_lines.append(
            f" {BOLD}{CYAN}Data Browser: {table_name}{RESET} [{status}] [{col_status}]  "
            f"{DIM}(Press 'h' to back, '/' for SQL filter search){RESET}"
        )

        table_lines = build_ascii_table_lines(
            sliced_headers,
            sliced_rows,
            fitted_widths,
            cursor_idx=cursor_idx,
            marked_row_indices=marked_rows,
            focused_col_idx=focused_col_idx,
            c_start=c_start,
            sort_col=sort_col,
            sort_dir=sort_dir,
        )
        frame_lines.extend(table_lines)

        frame_lines.append(
            f" {BOLD}Vim Nav:{RESET} {YELLOW}j/k{RESET} Up/Down | "
            f"{YELLOW}l/Enter{RESET} Card | "
            f"{YELLOW}H/L{RESET} Focus | "
            f"{YELLOW}gg/G{RESET} Jump | "
            f"{YELLOW}a{RESET} Add | "
            f"{YELLOW}o/O{RESET} Sort | "
            f"{YELLOW}v{RESET} Visibility | "
            f"{YELLOW}SPACE{RESET} Mark | "
            f"{YELLOW}f{RESET} FK | "
            f"{YELLOW}S/I{RESET} Schema/Indexes | "
            f"{YELLOW}dd{RESET} Del | "
            f"{YELLOW}:{RESET} SQL/Jump | "
            f"{YELLOW}x/X{RESET} Export | "
            f"{YELLOW}h/q{RESET} Back"
        )

        draw_frame(frame_lines)
        key = get_key()

        # Navigation
        if key == "j":
            if rows and cursor_idx < len(rows) - 1:
                cursor_idx += 1
            elif rows and len(rows) == page_size:
                offset += page_size
                cursor_idx = 0
                marked_rows.clear()
        elif key == "k":
            if cursor_idx > 0:
                cursor_idx -= 1
            elif offset >= page_size:
                offset -= page_size
                cursor_idx = page_size - 1
                marked_rows.clear()
        elif key == "L":
            if disp_headers and focused_col_idx < len(disp_headers) - 1:
                focused_col_idx += 1
        elif key == "H":
            if focused_col_idx > 0:
                focused_col_idx -= 1
        elif key in ("CTRL_D",):
            if rows and len(rows) == page_size:
                offset += page_size
                cursor_idx = 0
                marked_rows.clear()
        elif key == "CTRL_U":
            if offset >= page_size:
                offset -= page_size
                cursor_idx = 0
                marked_rows.clear()

        # Vim Jump Shortcuts: gg (top) and G (bottom)
        elif key == "g":
            if get_next_key_timeout(0.3) == "g":
                offset, cursor_idx = 0, 0
                marked_rows.clear()
        elif key == "G":
            if count_fn:
                cached_total_count = count_fn(filter_query=filter_text)
            if cached_total_count and cached_total_count > 0:
                offset = max(0, ((cached_total_count - 1) // page_size) * page_size)
                cursor_idx = (cached_total_count - 1) % page_size
                marked_rows.clear()

        # Insert Row(s) (a)
        elif key == "a":
            if headers:
                inserted_rows = launch_nvim_insert_editor(headers)
                if inserted_rows and insert_fn:
                    inserted_count = 0
                    for row_dict in inserted_rows:
                        if insert_fn(row_dict):
                            inserted_count += 1
                    sys.stdout.write("\033[?25h")
                    sys.stdout.flush()
                    if inserted_count > 0:
                        cached_total_count = None
                        print(
                            f"\n{GREEN}Success! Inserted {inserted_count} record(s) into database.{RESET}"
                        )
                    else:
                        print(f"\n{YELLOW}No records were inserted.{RESET}")
                    print(f" {DIM}Press any key to continue...{RESET}")
                    get_key()
                    sys.stdout.write("\033[?25l")
                    sys.stdout.flush()

        # Foreign Key Navigation (f)
        elif key == "f":
            if fk_fn and rows and 0 <= cursor_idx < len(rows):
                fks = fk_fn()
                if not fks:
                    sys.stdout.write("\033[?25h")
                    sys.stdout.flush()
                    print(
                        f"\n{YELLOW}No foreign key relations found on table '{table_name}'.{RESET}"
                    )
                    print(f" {DIM}Press any key to continue...{RESET}")
                    get_key()
                    sys.stdout.write("\033[?25l")
                    sys.stdout.flush()
                else:
                    curr_row = rows[cursor_idx]
                    row_dict = dict(zip(headers, curr_row))
                    fk_items = []
                    for fk in fks:
                        val = row_dict.get(fk["from_col"])
                        if val is not None:
                            fk_items.append(
                                {
                                    "fk": fk,
                                    "val": val,
                                    "label": f"Target Table: {BOLD}{CYAN}{fk['to_table']:<20}{RESET}  ({fk['from_col']} = '{val}' ➔ {fk['to_table']}.{fk['to_col']})",
                                }
                            )

                    if not fk_items:
                        sys.stdout.write("\033[?25h")
                        sys.stdout.flush()
                        print(
                            f"\n{YELLOW}Foreign key columns contain NULL values on row #{offset + cursor_idx + 1}.{RESET}"
                        )
                        print(f" {DIM}Press any key to continue...{RESET}")
                        get_key()
                        sys.stdout.write("\033[?25l")
                        sys.stdout.flush()
                    else:
                        chosen_fk, _ = select_menu_item(
                            f"Foreign Keys in '{table_name}'",
                            fk_items,
                            display_key="label",
                        )
                        if chosen_fk:
                            fk = chosen_fk["fk"]
                            fk_val = chosen_fk["val"]
                            target_tbl = fk["to_table"]
                            target_col = fk["to_col"]

                            def target_q_fn(
                                limit, offset, filter_query=None, order_by=None
                            ):
                                sub_filter = (
                                    f'"{target_col}" = {format_sql_value(fk_val)}'
                                )
                                if filter_query:
                                    sub_filter += f" AND ({filter_query})"

                                if engine == "sqlite" and backend_adapter:
                                    return backend_adapter.fetch_page(
                                        target_tbl,
                                        limit,
                                        offset,
                                        sub_filter,
                                        order_by,
                                    ).as_tuple()
                                elif engine == "sqlite":
                                    return query_sqlite_page(
                                        dbname_or_path,
                                        target_tbl,
                                        limit,
                                        offset,
                                        sub_filter,
                                        order_by,
                                    )
                                elif engine == "postgres":
                                    return query_postgres_page(
                                        container,
                                        dbname_or_path,
                                        target_tbl,
                                        limit,
                                        offset,
                                        sub_filter,
                                        order_by,
                                    )
                                elif engine == "mysql":
                                    return query_mysql_page(
                                        container,
                                        dbname_or_path,
                                        target_tbl,
                                        limit,
                                        offset,
                                        sub_filter,
                                        order_by,
                                    )
                                elif engine == "direct_postgres":
                                    return query_direct_postgres_page(
                                        dbname_or_path,
                                        target_tbl,
                                        limit,
                                        offset,
                                        sub_filter,
                                        order_by,
                                    )
                                elif engine == "direct_mysql":
                                    return query_direct_mysql_page(
                                        dbname_or_path,
                                        target_tbl,
                                        limit,
                                        offset,
                                        sub_filter,
                                        order_by,
                                    )
                                else:
                                    return query_fn(limit, offset, sub_filter, order_by)

                            browse_table(
                                target_q_fn,
                                update_fn=update_fn,
                                insert_fn=insert_fn,
                                delete_fn=delete_fn,
                                schema_fn=schema_fn,
                                fk_fn=fk_fn,
                                count_fn=count_fn,
                                exec_raw_fn=exec_raw_fn,
                                engine=engine,
                                container=container,
                                dbname_or_path=dbname_or_path,
                                all_tables=all_tables,
                                table_name=target_tbl,
                                adapter=backend_adapter,
                            )

        # Free-form SQL Shell & Jump to Row Index (:) with JOIN Autocomplete
        elif key == ":":
            shell_completer = None
            if PROMPT_TOOLKIT_AVAILABLE:
                fks_for_shell = fk_fn() if fk_fn else []
                shell_completer = FullSqlShellCompleter(
                    current_table=table_name,
                    all_tables=all_tables or [],
                    columns=headers or [],
                    foreign_keys=fks_for_shell,
                )

            input_val = prompt_with_autocomplete(" : ", completer=shell_completer)
            if input_val and input_val is not CANCELLED:
                if input_val.isdigit():
                    row_num = int(input_val)
                    if row_num > 0:
                        offset = max(0, ((row_num - 1) // page_size) * page_size)
                        cursor_idx = (row_num - 1) % page_size
                        marked_rows.clear()
                elif (
                    exec_raw_fn
                    and is_sql_where_expression(input_val)
                    and not any(
                        k in input_val.upper() for k in ["SELECT", "JOIN", "GROUP BY"]
                    )
                ):
                    filter_text = input_val
                    offset, cursor_idx = 0, 0
                    marked_rows.clear()
                    cached_total_count = None
                elif exec_raw_fn:

                    def raw_q_fn(limit, offset, filter_query=None, order_by=None):
                        return exec_raw_fn(input_val, limit, offset)

                    browse_table(
                        raw_q_fn,
                        engine=engine,
                        container=container,
                        dbname_or_path=dbname_or_path,
                        all_tables=all_tables,
                        table_name="Custom Query",
                    )

        # Visual Multi-select Toggle
        elif key == "SPACE":
            if rows and 0 <= cursor_idx < len(rows):
                if cursor_idx in marked_rows:
                    marked_rows.remove(cursor_idx)
                else:
                    marked_rows.add(cursor_idx)
                if cursor_idx < len(rows) - 1:
                    cursor_idx += 1

        # Toggle Column Visibility Settings (v)
        elif key == "v":
            if headers:
                toggle_column_visibility(headers, hidden_cols)

        # Interactive Column Sorting (o = ASC, O = DESC)
        elif key == "o":
            if disp_headers and 0 <= focused_col_idx < len(disp_headers):
                target_col = disp_headers[focused_col_idx]
                if sort_col == target_col and sort_dir == "ASC":
                    sort_col, sort_dir = None, None
                else:
                    sort_col, sort_dir = target_col, "ASC"
                offset, cursor_idx = 0, 0
                marked_rows.clear()
        elif key == "O":
            if disp_headers and 0 <= focused_col_idx < len(disp_headers):
                target_col = disp_headers[focused_col_idx]
                if sort_col == target_col and sort_dir == "DESC":
                    sort_col, sort_dir = None, None
                else:
                    sort_col, sort_dir = target_col, "DESC"
                offset, cursor_idx = 0, 0
                marked_rows.clear()

        # Row Card Detail
        elif key in ("ENTER", "l") or key.lower() == "v":
            if rows and 0 <= cursor_idx < len(rows):
                deleted = browse_row_detail(
                    headers,
                    rows[cursor_idx],
                    offset + cursor_idx + 1,
                    update_fn=update_fn,
                    delete_fn=delete_fn,
                )
                if deleted:
                    cached_total_count = None

        # Edit Single Row
        elif key == "e":
            if rows and 0 <= cursor_idx < len(rows):
                orig_dict = dict(zip(headers, rows[cursor_idx]))
                updated_dict = launch_nvim_editor(orig_dict)
                if updated_dict and update_fn:
                    success = update_fn(headers, rows[cursor_idx], updated_dict)
                    if success:
                        sys.stdout.write("\033[?25h")
                        sys.stdout.flush()
                        print(f"\n{GREEN}Success! Row updated in database.{RESET}")
                        print(f" {DIM}Press any key to continue...{RESET}")
                        get_key()
                        sys.stdout.write("\033[?25l")
                        sys.stdout.flush()

        # Edit Table JSON with Git-Style Unified Diff Preview (E)
        elif key == "E":
            all_headers, all_rows = query_fn(
                limit=100000, offset=0, filter_query=filter_text, order_by=order_by
            )
            if all_rows:
                orig_table_data = []
                json_fields = set()
                for r in all_rows:
                    row_dict = {}
                    for k, val in zip(all_headers, r):
                        if isinstance(val, str) and (
                            (val.startswith("{") and val.endswith("}"))
                            or (val.startswith("[") and val.endswith("]"))
                        ):
                            try:
                                row_dict[k] = json.loads(val)
                                json_fields.add(k)
                                continue
                            except json.JSONDecodeError:
                                pass
                        row_dict[k] = val
                    orig_table_data.append(row_dict)

                updated_table_data = launch_nvim_table_editor(all_headers, all_rows)
                if updated_table_data is not None:
                    diff_lines = generate_ansi_unified_diff(
                        orig_table_data,
                        updated_table_data,
                        file_name=f"{table_name}.json",
                    )
                    if not diff_lines:
                        sys.stdout.write("\033[?25h")
                        sys.stdout.flush()
                        print(f"\n{YELLOW}No changes detected.{RESET}")
                        print(f" {DIM}Press any key to continue...{RESET}")
                        get_key()
                        sys.stdout.write("\033[?25l")
                        sys.stdout.flush()
                    else:
                        rows_to_update = []
                        rows_to_delete = []

                        if "id" in all_headers:
                            id_idx = all_headers.index("id")
                            updated_by_id = {}
                            for u_dict in updated_table_data:
                                if "id" in u_dict and u_dict["id"] is not None:
                                    updated_by_id[str(u_dict["id"])] = u_dict

                            for orig_row in all_rows:
                                orig_id = (
                                    str(orig_row[id_idx])
                                    if orig_row[id_idx] is not None
                                    else None
                                )
                                if orig_id in updated_by_id:
                                    u_dict = updated_by_id[orig_id]
                                    orig_dict = dict(zip(all_headers, orig_row))
                                    if any(
                                        orig_dict.get(k) != v for k, v in u_dict.items()
                                    ):
                                        rows_to_update.append(
                                            (all_headers, orig_row, u_dict)
                                        )
                                else:
                                    rows_to_delete.append((all_headers, orig_row))
                        else:
                            remaining_updated = list(updated_table_data)
                            for orig_row in all_rows:
                                orig_dict = dict(zip(all_headers, orig_row))
                                match_idx = -1
                                for idx, u_dict in enumerate(remaining_updated):
                                    if all(
                                        orig_dict.get(k) == v for k, v in u_dict.items()
                                    ):
                                        match_idx = idx
                                        break
                                if match_idx != -1:
                                    remaining_updated.pop(match_idx)
                                else:
                                    best_idx = -1
                                    max_matches = -1
                                    for idx, u_dict in enumerate(remaining_updated):
                                        common_matches = sum(
                                            1
                                            for k, v in u_dict.items()
                                            if orig_dict.get(k) == v
                                        )
                                        if common_matches > max_matches:
                                            max_matches = common_matches
                                            best_idx = idx

                                    if best_idx != -1 and max_matches > 0:
                                        u_dict = remaining_updated.pop(best_idx)
                                        rows_to_update.append(
                                            (all_headers, orig_row, u_dict)
                                        )
                                    else:
                                        rows_to_delete.append((all_headers, orig_row))

                        summary_str = f"{len(rows_to_update)} updated, {len(rows_to_delete)} deleted"
                        confirmed = display_diff_pager(diff_lines, summary_str)

                        if confirmed:
                            deleted_count = 0
                            if rows_to_delete and delete_fn:
                                for h, orig_row in rows_to_delete:
                                    if delete_fn(h, orig_row):
                                        deleted_count += 1
                                        cached_total_count = None

                            updated_count = 0
                            if update_fn:
                                for h, orig_row, u_dict in rows_to_update:
                                    if update_fn(h, orig_row, u_dict):
                                        updated_count += 1

                            sys.stdout.write("\033[?25h")
                            sys.stdout.flush()
                            msg_parts = []
                            if updated_count > 0:
                                msg_parts.append(f"{updated_count} row(s) updated")
                            if deleted_count > 0:
                                msg_parts.append(f"{deleted_count} row(s) deleted")
                            print(
                                f"\n{GREEN}Success! Committed {', '.join(msg_parts)} to database.{RESET}"
                            )
                            print(f" {DIM}Press any key to continue...{RESET}")
                            get_key()
                            sys.stdout.write("\033[?25l")
                            sys.stdout.flush()
                        else:
                            sys.stdout.write("\033[?25h")
                            sys.stdout.flush()
                            print(
                                f"\n{YELLOW}Changes discarded. No modifications were committed.{RESET}"
                            )
                            print(f" {DIM}Press any key to continue...{RESET}")
                            get_key()
                            sys.stdout.write("\033[?25l")
                            sys.stdout.flush()

        # Delete Single or Marked Rows via Double 'dd' Sequence
        elif key == "d":
            if get_next_key_timeout(0.3) == "d":
                if delete_fn:
                    if marked_rows:
                        target_indices = sorted(list(marked_rows))
                        if confirm_action(
                            f"Are you sure you want to delete {len(target_indices)} marked row(s)?"
                        ):
                            deleted_count = 0
                            for r_i in target_indices:
                                if r_i < len(rows):
                                    if delete_fn(headers, rows[r_i]):
                                        deleted_count += 1
                            marked_rows.clear()
                            cached_total_count = None
                            sys.stdout.write("\033[?25h")
                            sys.stdout.flush()
                            print(
                                f"\n{GREEN}Success! {deleted_count} row(s) deleted from database.{RESET}"
                            )
                            print(f" {DIM}Press any key to continue...{RESET}")
                            get_key()
                            sys.stdout.write("\033[?25l")
                            sys.stdout.flush()
                    elif rows and 0 <= cursor_idx < len(rows):
                        if confirm_action(
                            f"Are you sure you want to delete row #{offset + cursor_idx + 1}?"
                        ):
                            success = delete_fn(headers, rows[cursor_idx])
                            if success:
                                cached_total_count = None
                                sys.stdout.write("\033[?25h")
                                sys.stdout.flush()
                                print(
                                    f"\n{GREEN}Success! Row deleted from database.{RESET}"
                                )
                                print(f" {DIM}Press any key to continue...{RESET}")
                                get_key()
                                sys.stdout.write("\033[?25l")
                                sys.stdout.flush()
                                if cursor_idx >= len(rows) - 1 and cursor_idx > 0:
                                    cursor_idx -= 1

        # Export JSON (x) vs Export Menu (X: JSON, CSV, SQL Dumps)
        elif key == "x":
            if marked_rows:
                export_data = [
                    rows[i] for i in sorted(list(marked_rows)) if i < len(rows)
                ]
                prefix = f"{table_name}_marked"
            else:
                _, export_data = query_fn(
                    limit=100000, offset=0, filter_query=filter_text, order_by=order_by
                )
                prefix = f"{table_name}_export"

            if export_data:
                out_path = export_rows_to_json(
                    headers, export_data, filename_prefix=prefix
                )
                sys.stdout.write("\033[?25h")
                sys.stdout.flush()
                print(
                    f"\n{GREEN}Success! Exported {len(export_data)} row(s) to '{out_path.name}'.{RESET}"
                )
                print(f" {DIM}Press any key to continue...{RESET}")
                get_key()
                sys.stdout.write("\033[?25l")
                sys.stdout.flush()

        elif key == "X":
            export_options = [
                {"type": "json", "label": "JSON Format (.json)"},
                {"type": "csv", "label": "CSV Format (.csv)"},
                {"type": "sql", "label": "SQL Insert Statements (.sql)"},
            ]
            chosen_opt, _ = select_menu_item(
                f"Export Options: {table_name}", export_options, display_key="label"
            )
            if chosen_opt:
                fmt = chosen_opt["type"]
                if marked_rows:
                    export_data = [
                        rows[i] for i in sorted(list(marked_rows)) if i < len(rows)
                    ]
                    prefix = f"{table_name}_marked"
                else:
                    _, export_data = query_fn(
                        limit=100000,
                        offset=0,
                        filter_query=filter_text,
                        order_by=order_by,
                    )
                    prefix = f"{table_name}_export"

                if export_data:
                    if fmt == "json":
                        out_path = export_rows_to_json(
                            headers, export_data, filename_prefix=prefix
                        )
                    elif fmt == "csv":
                        out_path = export_rows_to_csv(
                            headers, export_data, filename_prefix=prefix
                        )
                    elif fmt == "sql":
                        out_path = export_rows_to_sql(
                            headers, export_data, table_name, filename_prefix=prefix
                        )

                    sys.stdout.write("\033[?25h")
                    sys.stdout.flush()
                    print(
                        f"\n{GREEN}Success! Exported {len(export_data)} row(s) to '{out_path.name}'.{RESET}"
                    )
                    print(f" {DIM}Press any key to continue...{RESET}")
                    get_key()
                    sys.stdout.write("\033[?25l")
                    sys.stdout.flush()

        # Schema Inspector (S)
        elif key == "S":
            if schema_fn:
                s_headers, s_rows = schema_fn()
                if s_headers and s_rows:
                    disp_s_rows = [[str(cell) for cell in row] for row in s_rows]
                    browse_row_detail(
                        s_headers,
                        disp_s_rows[0] if disp_s_rows else [],
                        1,
                    )

        # Index, Constraint & Trigger Inspector (I)
        elif key == "I":
            if engine and dbname_or_path:
                info_lines = get_index_and_constraint_info(
                    engine, container, dbname_or_path, table_name
                )
                browse_row_detail(
                    ["Inspector Output"],
                    ["\n".join(info_lines)],
                    1,
                )

        # SQL Filter Search
        elif key == "/" or key.lower() == "s":
            if cached_headers is None:
                sample_headers, sample_rows = query_fn(limit=10, offset=0)
                if sample_headers and sample_rows:
                    cached_headers = sample_headers
                    cached_values, cached_subkeys, cached_path_vals = (
                        analyze_table_sample(sample_headers, sample_rows)
                    )

            completer = None
            if PROMPT_TOOLKIT_AVAILABLE and cached_headers:
                completer = CustomSqlCompleter(
                    columns=cached_headers,
                    column_values=cached_values,
                    path_subkeys=cached_subkeys,
                    path_values=cached_path_vals,
                )

            new_filter = prompt_with_autocomplete(
                " Filter: ", completer=completer, default=filter_text or ""
            )
            if new_filter is not CANCELLED:
                filter_text = new_filter if new_filter else None
                offset, cursor_idx = 0, 0
                marked_rows.clear()
                cached_total_count = None

        # Edit Filter in $EDITOR (Ctrl+G)
        elif key == "CTRL_G":
            filter_text = edit_text_in_nvim(filter_text or "", suffix=".sql")
            if not filter_text:
                filter_text = None
            offset, cursor_idx = 0, 0
            marked_rows.clear()
            cached_total_count = None

        elif key.lower() == "c":
            filter_text = None
            sort_col, sort_dir = None, None
            offset, cursor_idx = 0, 0
            marked_rows.clear()
            cached_total_count = None

        elif key in ("h", "q", "b"):
            break


# -------------------------------------------------------------------
#  DISCOVERY & MAIN MENU
# -------------------------------------------------------------------


def main():
    cli_item = None
    if len(sys.argv) > 1:
        raw_arg = sys.argv[1]
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

    sqlite_dbs = find_sqlite_files()
    docker_dbs = find_docker_databases() or []

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
            table_idx = 0
            while True:
                tables = get_direct_postgres_tables(target)
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

                def q_fn(limit, offset, filter_query=None, order_by=None):
                    return query_direct_postgres_page(
                        target, t_name, limit, offset, filter_query, order_by=order_by
                    )

                def u_fn(headers, orig_row, updated_dict):
                    return update_direct_postgres_row(
                        target, t_name, headers, orig_row, updated_dict
                    )

                def i_fn(row_dict):
                    return insert_direct_postgres_row(target, t_name, row_dict)

                def d_fn(headers, orig_row):
                    return delete_direct_postgres_row(target, t_name, headers, orig_row)

                def s_fn():
                    return get_direct_postgres_schema_info(target, t_name)

                def fk_fn():
                    return get_direct_postgres_foreign_keys(target, t_name)

                def count_fn_direct_pg(filter_query=None):
                    return count_direct_postgres_rows(target, t_name, filter_query)

                def exec_raw_fn(raw_sql, limit, offset):
                    return execute_direct_postgres_raw(target, raw_sql, limit, offset)

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
                )

        # -------------------------------------------------------------
        #  DIRECT MYSQL FLOW
        # -------------------------------------------------------------
        elif db_type == "direct_mysql":
            table_idx = 0
            while True:
                tables = get_direct_mysql_tables(target)
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

                def q_fn(limit, offset, filter_query=None, order_by=None):
                    return query_direct_mysql_page(
                        target, t_name, limit, offset, filter_query, order_by=order_by
                    )

                def u_fn(headers, orig_row, updated_dict):
                    return update_direct_mysql_row(
                        target, t_name, headers, orig_row, updated_dict
                    )

                def i_fn(row_dict):
                    return insert_direct_mysql_row(target, t_name, row_dict)

                def d_fn(headers, orig_row):
                    return delete_direct_mysql_row(target, t_name, headers, orig_row)

                def s_fn():
                    return get_direct_mysql_schema_info(target, t_name)

                def fk_fn():
                    return get_direct_mysql_foreign_keys(target, t_name)

                def count_fn_direct_my(filter_query=None):
                    return count_direct_mysql_rows(target, t_name, filter_query)

                def exec_raw_fn(raw_sql, limit, offset):
                    return execute_direct_mysql_raw(target, raw_sql, limit, offset)

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
                )

        # -------------------------------------------------------------
        #  SQLITE FLOW
        # -------------------------------------------------------------
        elif db_type == "sqlite":
            adapter = SQLiteAdapter(target)
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

                def q_fn(limit, offset, filter_query=None, order_by=None):
                    return adapter.fetch_page(
                        t_name, limit, offset, filter_query, order_by
                    ).as_tuple()

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

                def count_fn_sqlite(filter_query=None):
                    return adapter.count_rows(t_name, filter_query)

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
                            f" (No public tables found in {engine.upper()} database '{dbname}')"
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

                    def q_fn(limit, offset, filter_query=None, order_by=None):
                        if engine == "postgres":
                            return query_postgres_page(
                                c_name,
                                dbname,
                                t_name,
                                limit,
                                offset,
                                filter_query,
                                order_by=order_by,
                            )
                        else:
                            return query_mysql_page(
                                c_name,
                                dbname,
                                t_name,
                                limit,
                                offset,
                                filter_query,
                                order_by=order_by,
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

                    def count_fn_docker(filter_query=None):
                        if engine == "postgres":
                            return count_postgres_rows(
                                c_name, dbname, t_name, filter_query
                            )
                        else:
                            return count_mysql_rows(
                                c_name, dbname, t_name, filter_query
                            )

                    def exec_raw_fn(raw_sql, limit, offset):
                        if engine == "postgres":
                            return execute_postgres_raw(
                                c_name, dbname, raw_sql, limit, offset
                            )
                        else:
                            return execute_mysql_raw(
                                c_name, dbname, raw_sql, limit, offset
                            )

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
                    )


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        exit_cleanup()
        print(f"\n{RED}{BOLD}Startup Error:{RESET} {e}\n")
        traceback.print_exc()
