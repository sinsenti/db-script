"""Low-level terminal rendering, input and clipboard helpers.

The browser controller can use these helpers without owning terminal state or
ANSI styling details.  Higher-level menus and screens remain in ``db.py`` for
now and can be moved here incrementally.
"""

from __future__ import annotations

import atexit
import re
import select
import shutil
import subprocess
import sys
import termios
import tty


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
    else:
        sys.stdout.write("\033[?25h")
        sys.stdout.flush()


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
