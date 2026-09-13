"""External editor integration for SQL text and JSON row editing."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import termios
from typing import Any, Sequence

from .ui import RED, RESET, enter_alt_screen


def _editor_command() -> list[str]:
    """Resolve ``$EDITOR`` while supporting commands with arguments."""
    configured_editor = os.environ.get("EDITOR")
    if configured_editor:
        try:
            command = shlex.split(configured_editor)
        except ValueError:
            command = []
        if command:
            return command

    for editor_name in ("nvim", "vim", "nano"):
        editor_path = shutil.which(editor_name)
        if editor_path:
            return [editor_path]
    return ["nano"]


def _run_editor(path: str) -> int:
    """Temporarily leave the alternate screen and run the configured editor."""
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        sys.stdout.write("\033[?25h\033[?1049l")
        sys.stdout.flush()
        termios.tcsetattr(fd, termios.TCSANOW, old_settings)
        return subprocess.call([*_editor_command(), path])
    finally:
        termios.tcsetattr(fd, termios.TCSANOW, old_settings)
        termios.tcflush(fd, termios.TCIFLUSH)
        enter_alt_screen()


def edit_text_in_nvim(initial_text: str | None, suffix: str = ".sql") -> str | None:
    """Open SQL/filter text in ``$EDITOR`` and return the edited text."""
    with tempfile.NamedTemporaryFile(mode="w+", suffix=suffix, delete=False) as text_file:
        text_file.write(initial_text or "")
        temp_path = text_file.name

    try:
        _run_editor(temp_path)
        with open(temp_path, "r", encoding="utf-8") as text_file:
            return text_file.read().strip()
    except Exception as exc:
        print(f"\n{RED}Error reading text from editor: {exc}{RESET}")
        return initial_text
    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass


def _json_object(value: Any) -> Any:
    if isinstance(value, str) and (
        (value.startswith("{") and value.endswith("}"))
        or (value.startswith("[") and value.endswith("]"))
    ):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            pass
    return value


def launch_nvim_editor(data_dict: dict[str, Any]) -> dict[str, Any] | None:
    """Open one database row as a JSON object in ``$EDITOR``."""
    edit_dict: dict[str, Any] = {}
    json_fields: set[str] = set()
    for key, value in data_dict.items():
        converted = _json_object(value)
        if converted is not value:
            json_fields.add(key)
        edit_dict[key] = converted

    with tempfile.NamedTemporaryFile(mode="w+", suffix=".json", delete=False) as json_file:
        json.dump(edit_dict, json_file, indent=2, ensure_ascii=False)
        temp_path = json_file.name

    try:
        _run_editor(temp_path)
        with open(temp_path, "r", encoding="utf-8") as json_file:
            updated_dict = json.load(json_file)
    except Exception as exc:
        print(f"\n{RED}Error reading edited JSON or syntax error: {exc}{RESET}")
        return None
    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass

    if not isinstance(updated_dict, dict):
        print(f"\n{RED}Error: Expected a JSON object for one row.{RESET}")
        return None

    return {
        key: json.dumps(value, ensure_ascii=False)
        if key in json_fields and isinstance(value, (dict, list))
        else value
        for key, value in updated_dict.items()
    }


def launch_nvim_insert_editor(headers: Sequence[str]) -> list[dict[str, Any]] | None:
    """Open a blank JSON row template for insertion."""
    if not headers:
        return None
    template = {header: None for header in headers}
    with tempfile.NamedTemporaryFile(mode="w+", suffix=".json", delete=False) as json_file:
        json.dump(template, json_file, indent=2, ensure_ascii=False)
        temp_path = json_file.name

    try:
        _run_editor(temp_path)
        with open(temp_path, "r", encoding="utf-8") as json_file:
            inserted_data = json.load(json_file)
    except Exception as exc:
        print(f"\n{RED}Error reading inserted JSON or syntax error: {exc}{RESET}")
        return None
    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass

    if isinstance(inserted_data, dict):
        return [inserted_data]
    if isinstance(inserted_data, list):
        return [row for row in inserted_data if isinstance(row, dict)]
    return None


def launch_nvim_table_editor(
    headers: Sequence[str], rows: Sequence[Sequence[Any]]
) -> list[dict[str, Any]] | None:
    """Open an entire table as a JSON array for bulk editing."""
    if not headers or not rows:
        return None

    json_fields: set[str] = set()
    table_data: list[dict[str, Any]] = []
    for row in rows:
        row_dict: dict[str, Any] = {}
        for key, value in zip(headers, row):
            converted = _json_object(value)
            if converted is not value:
                json_fields.add(key)
            row_dict[key] = converted
        table_data.append(row_dict)

    with tempfile.NamedTemporaryFile(mode="w+", suffix=".json", delete=False) as json_file:
        json.dump(table_data, json_file, indent=2, ensure_ascii=False)
        temp_path = json_file.name

    try:
        _run_editor(temp_path)
        with open(temp_path, "r", encoding="utf-8") as json_file:
            updated_table_data = json.load(json_file)
    except Exception as exc:
        print(f"\n{RED}Error reading edited JSON or syntax error: {exc}{RESET}")
        return None
    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass

    if not isinstance(updated_table_data, list):
        print(f"\n{RED}Error: Expected top-level JSON array of row objects.{RESET}")
        return None

    result: list[dict[str, Any]] = []
    for row in updated_table_data:
        if isinstance(row, dict):
            result.append(
                {
                    key: json.dumps(value, ensure_ascii=False)
                    if key in json_fields and isinstance(value, (dict, list))
                    else value
                    for key, value in row.items()
                }
            )
    return result
