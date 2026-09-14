"""Terminal rendering, input and browser screens.

Database adapters and legacy command-line fallbacks are supplied through
callbacks so this module does not own database connections.
"""

from __future__ import annotations

import atexit
import difflib
import json
import re
import select
import shutil
import subprocess
import sys
import termios
import tty

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.application import run_in_terminal
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.filters import completion_is_selected
    from prompt_toolkit.key_binding import KeyBindings

    PROMPT_TOOLKIT_AVAILABLE = True
except ImportError:
    PROMPT_TOOLKIT_AVAILABLE = False

from .editor import (
    edit_text_in_nvim,
    launch_nvim_editor,
    launch_nvim_insert_editor,
    launch_nvim_table_editor,
)
from .export import export_rows_to_csv, export_rows_to_json, export_rows_to_sql
from .query import (
    format_sql_literal,
    is_sql_where_expression,
    sanitize_where_clause,
)


# ANSI Colors & Styling
CYAN = "\033[96m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
MAGENTA = "\033[95m"
RED = "\033[91m"
DIM = "\033[2m"
BOLD = "\033[1m"
REVERSE = "\033[7m"
RESET = "\033[0m"

_ALT_SCREEN_ACTIVE = False
MAX_COL_WIDTH = 35
CANCELLED = object()


def strip_ansi(text):
    """Strip ANSI color codes to calculate visible string length."""
    return re.sub(r"\x1b\[[0-9;]*m", "", str(text))


def visible_len(text):
    """Return the visible length of a possibly-colored string."""
    return len(strip_ansi(text))


def enter_alt_screen():
    """Switch to the alternate terminal buffer and hide the cursor."""
    global _ALT_SCREEN_ACTIVE
    if not _ALT_SCREEN_ACTIVE:
        sys.stdout.write("\033[?1049h\033[?25l")
        sys.stdout.flush()
        _ALT_SCREEN_ACTIVE = True


def exit_cleanup():
    """Restore the main terminal buffer and cursor on exit."""
    global _ALT_SCREEN_ACTIVE
    if _ALT_SCREEN_ACTIVE:
        sys.stdout.write("\033[?25h\033[?1049l")
        sys.stdout.flush()
        _ALT_SCREEN_ACTIVE = False


atexit.register(exit_cleanup)


def get_terminal_size():
    """Return terminal width and height, with a safe non-TTY fallback."""
    try:
        size = shutil.get_terminal_size()
        return size.columns, size.lines
    except Exception:
        return 100, 24


def draw_frame(lines):
    """Render a frame in place without allowing it to exceed terminal height."""
    _, term_height = get_terminal_size()
    flat_lines = []
    for item in lines:
        flat_lines.extend(str(item).split("\n"))

    flat_lines = flat_lines[:term_height]
    formatted = [line + "\033[K" for line in flat_lines]
    output = "\033[?25l\033[H" + "\n".join(formatted) + "\033[J"
    sys.stdout.write(output)
    sys.stdout.flush()


def get_key():
    """Capture one keypress and normalize common terminal escape sequences."""
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)

        if ch == "\x1b":
            readable, _, _ = select.select([sys.stdin], [], [], 0.05)
            if readable:
                ch2 = sys.stdin.read(1)
                if ch2 == "[":
                    readable2, _, _ = select.select([sys.stdin], [], [], 0.05)
                    if readable2:
                        ch3 = sys.stdin.read(1)
                        if ch3 == "A":
                            return "k"
                        if ch3 == "B":
                            return "j"
                        if ch3 == "C":
                            return "L"
                        if ch3 == "D":
                            return "H"
            return "q"

        if ch in ("\r", "\n"):
            return "ENTER"
        if ch == " ":
            return "SPACE"
        if ch == "\x04":
            return "CTRL_D"
        if ch == "\x15":
            return "CTRL_U"
        if ch == "\x07":
            return "CTRL_G"
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSANOW, old_settings)


def get_next_key_timeout(timeout=0.3):
    """Read a possible second key for commands such as ``gg`` or ``dd``."""
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        readable, _, _ = select.select([sys.stdin], [], [], timeout)
        if readable:
            return sys.stdin.read(1)
        return None
    finally:
        termios.tcsetattr(fd, termios.TCSANOW, old_settings)


def confirm_action(prompt_text):
    """Display a confirmation prompt and wait for ``y``/``N``."""
    sys.stdout.write("\033[?25h")
    sys.stdout.flush()
    try:
        answer = input(f"\n {RED}{BOLD}{prompt_text} (y/N): {RESET}").strip().lower()
    except Exception:
        answer = "n"
    sys.stdout.write("\033[?25l")
    sys.stdout.flush()
    return answer in ("y", "yes")


def copy_to_clipboard(text):
    """Copy text using wl-copy, xclip or pbcopy when available."""
    text_str = str(text) if text is not None else ""
    if shutil.which("wl-copy"):
        try:
            proc = subprocess.Popen(["wl-copy"], stdin=subprocess.PIPE, text=True)
            proc.communicate(input=text_str, timeout=2)
            return True
        except Exception:
            pass
    if shutil.which("xclip"):
        try:
            proc = subprocess.Popen(
                ["xclip", "-selection", "clipboard"],
                stdin=subprocess.PIPE,
                text=True,
            )
            proc.communicate(input=text_str, timeout=2)
            return True
        except Exception:
            pass
    if shutil.which("pbcopy"):
        try:
            proc = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE, text=True)
            proc.communicate(input=text_str, timeout=2)
            return True
        except Exception:
            pass
    return False

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
        mutation_help = ""
        if update_fn:
            mutation_help += f"{YELLOW}e{RESET} Edit (nvim) | "
        if delete_fn:
            mutation_help += f"{YELLOW}dd{RESET} Delete | "
        frame_lines.append(
            f" {BOLD}Vim Nav:{RESET} {YELLOW}j/k{RESET} Line Up/Down | "
            f"{mutation_help}"
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
        elif key.lower() == "e" and update_fn:
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
        elif key == "d" and delete_fn:
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
    inspector_fn=None,
    exec_raw_fn=None,
    query_table_fn=None,
    count_table_fn=None,
    table_callbacks_fn=None,
    engine=None,
    container=None,
    dbname_or_path=None,
    all_tables=None,
    table_name="table",
    total_rows=None,
    read_only=False,
    **kwargs,
):
    """Interactive table browser supporting visual multi-select, FK nav, raw SQL shell with JOIN completions, exports & jump shortcuts."""
    # Optional adapter bridge used during the incremental Python refactor.
    # Legacy callback arguments remain supported for the other backends.
    backend_adapter = kwargs.get("adapter")
    if read_only:
        # A read-only session must not expose mutation actions, even for a
        # legacy backend whose callback implementation predates adapters.
        update_fn = insert_fn = delete_fn = None
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
        if read_only:
            status += " [READ ONLY]"

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

        mutation_help = "" if read_only else (
            f"{YELLOW}a{RESET} Add | "
            f"{YELLOW}e{RESET} Edit | "
            f"{YELLOW}E{RESET} Bulk Edit | "
            f"{YELLOW}dd{RESET} Del | "
        )
        frame_lines.append(
            f" {BOLD}Vim Nav:{RESET} {YELLOW}j/k{RESET} Up/Down | "
            f"{YELLOW}l/Enter{RESET} Card | "
            f"{YELLOW}H/L{RESET} Focus | "
            f"{YELLOW}gg/G{RESET} Jump | "
            f"{mutation_help}"
            f"{YELLOW}o/O{RESET} Sort | "
            f"{YELLOW}v{RESET} Visibility | "
            f"{YELLOW}SPACE{RESET} Mark | "
            f"{YELLOW}f{RESET} FK | "
            f"{YELLOW}S/I{RESET} Schema/Indexes | "
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
        elif key == "a" and not read_only:
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
                                dialect = (
                                    "mysql"
                                    if engine in ("mysql", "direct_mysql")
                                    else "sqlite"
                                    if engine == "sqlite"
                                    else "postgres"
                                )
                                identifier_quote = "`" if dialect == "mysql" else '"'
                                escaped_target_col = target_col.replace(
                                    identifier_quote, identifier_quote * 2
                                )
                                quoted_target_col = (
                                    f"{identifier_quote}{escaped_target_col}{identifier_quote}"
                                )
                                sub_filter = (
                                    f"{quoted_target_col} = {format_sql_literal(fk_val)}"
                                )
                                if filter_query:
                                    sub_filter += f" AND ({filter_query})"

                                if query_table_fn:
                                    return query_table_fn(
                                        target_tbl,
                                        limit,
                                        offset,
                                        sub_filter,
                                        order_by,
                                    )
                                if backend_adapter:
                                    return backend_adapter.fetch_page(
                                        target_tbl,
                                        limit,
                                        offset,
                                        sub_filter,
                                        order_by,
                                    ).as_tuple()
                                return query_fn(limit, offset, sub_filter, order_by)

                            target_callbacks = (
                                table_callbacks_fn(target_tbl)
                                if table_callbacks_fn
                                else {}
                            )
                            target_update_fn = target_callbacks.get("update_fn")
                            target_insert_fn = target_callbacks.get("insert_fn")
                            target_delete_fn = target_callbacks.get("delete_fn")
                            target_schema_fn = target_callbacks.get("schema_fn")
                            target_fk_fn = target_callbacks.get("fk_fn")
                            target_count_fn = target_callbacks.get("count_fn")
                            target_exec_raw_fn = target_callbacks.get("exec_raw_fn")
                            if backend_adapter:
                                def target_update_fn(headers, orig_row, updated_dict):
                                    try:
                                        return (
                                            backend_adapter.update_row(
                                                target_tbl,
                                                dict(zip(headers, orig_row)),
                                                updated_dict,
                                            )
                                            == 1
                                        )
                                    except Exception as exc:
                                        print(
                                            f"\n{RED}Database Update Error: {exc}{RESET}"
                                        )
                                        return False

                                def target_insert_fn(row_dict):
                                    try:
                                        return backend_adapter.insert_row(target_tbl, row_dict) == 1
                                    except Exception as exc:
                                        print(
                                            f"\n{RED}Database Insert Error: {exc}{RESET}"
                                        )
                                        return False

                                def target_delete_fn(headers, orig_row):
                                    try:
                                        return (
                                            backend_adapter.delete_row(
                                                target_tbl, dict(zip(headers, orig_row))
                                            )
                                            == 1
                                        )
                                    except Exception as exc:
                                        print(
                                            f"\n{RED}Database Delete Error: {exc}{RESET}"
                                        )
                                        return False

                                target_schema_fn = lambda: backend_adapter.schema_info(
                                    target_tbl
                                ).as_tuple()
                                target_fk_fn = lambda: backend_adapter.foreign_keys(target_tbl)
                                target_count_fn = lambda filter_query=None: backend_adapter.count_rows(
                                    target_tbl, filter_query
                                )
                                target_exec_raw_fn = (
                                    lambda raw_sql, limit, offset: backend_adapter.execute_readonly(
                                        raw_sql, limit=limit, offset=offset
                                    ).as_tuple()
                                )
                            elif count_table_fn:
                                target_count_fn = lambda filter_query=None: count_table_fn(
                                    target_tbl, filter_query
                                )

                            browse_table(
                                target_q_fn,
                                update_fn=target_update_fn,
                                insert_fn=target_insert_fn,
                                delete_fn=target_delete_fn,
                                schema_fn=target_schema_fn,
                                fk_fn=target_fk_fn,
                                count_fn=target_count_fn,
                                exec_raw_fn=target_exec_raw_fn,
                                engine=engine,
                                container=container,
                                dbname_or_path=dbname_or_path,
                                all_tables=all_tables,
                                table_name=target_tbl,
                                read_only=read_only,
                                adapter=backend_adapter,
                                query_table_fn=query_table_fn,
                                count_table_fn=count_table_fn,
                                table_callbacks_fn=table_callbacks_fn,
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
                        read_only=read_only,
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
        elif key == "e" and not read_only:
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
        elif key == "E" and not read_only:
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
        elif key == "d" and not read_only:
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
            if inspector_fn:
                info_lines = inspector_fn()
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
