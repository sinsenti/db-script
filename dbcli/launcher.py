"""Database picker for the Rust browser, with explicit Python/MySQL compatibility."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import termios
from urllib.parse import unquote, urlsplit

from .discovery import find_docker_databases, find_sqlite_files
from .postgres_target import TargetError, database_url, docker_target, target_environment
from .rust import FRONTEND, ROOT, WORKER, command
from .ui import CANCELLED, enter_alt_screen, exit_cleanup, prompt_with_autocomplete, select_menu_item

TITLE = "Interactive Database Browser & Explorer"
DATABASES_SQL = (
    "SELECT datname FROM pg_catalog.pg_database "
    "WHERE datallowconn AND NOT datistemplate "
    "AND pg_catalog.has_database_privilege(datname, 'CONNECT') ORDER BY datname"
)


class LaunchError(RuntimeError):
    """User-facing startup diagnostics containing no connection credentials."""


class Stopped(BaseException):
    def __init__(self, signum):
        self.signum = signum


@contextmanager
def stop_signals():
    def stop(signum, frame):
        raise Stopped(signum)

    previous = {}
    try:
        for sig in (signal.SIGTERM, signal.SIGHUP):
            previous[sig] = signal.signal(sig, stop)
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def connection_label(url, engine="PostgreSQL"):
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        if not host:
            raise ValueError()
        endpoint = host + (":" + str(parsed.port) if parsed.port else "")
        database = unquote(parsed.path.lstrip("/"))
        label = f"{engine}: {endpoint}/{database}"
        return "".join(c if c.isprintable() else "?" for c in label)
    except (TypeError, ValueError):
        return f"{engine} connection"


def sqlite_path(value):
    try:
        path = Path(value).expanduser().resolve()
        if not path.is_file():
            raise OSError()
        with path.open("rb") as source:
            signature = source.read(16)
        if signature not in (b"", b"SQLite format 3\x00"):
            raise OSError()
    except (OSError, ValueError, TypeError):
        raise LaunchError("Choose an existing, readable SQLite database file") from None
    return path


def explicit_target(value):
    if value.startswith(("postgres://", "postgresql://")):
        return {"type": "direct_postgres", "target": value, "label": connection_label(value)}
    if value.startswith(("mysql://", "mariadb://")):
        return {"type": "direct_mysql", "target": value, "label": connection_label(value, "MySQL")}
    if value.startswith("sqlite://"):
        parsed = urlsplit(value)
        # Keep the original sqlite://relative/path shorthand as well as
        # sqlite:///absolute/path and sqlite://localhost/absolute/path.
        value = unquote(parsed.path if parsed.netloc in ("", "localhost") else value[len("sqlite://"):])
    path = sqlite_path(value)
    return {"type": "sqlite", "target": path, "label": f"SQLite: {path.name}"}


def discover_items():
    items = []
    for path in find_sqlite_files():
        items.append({"type": "sqlite", "target": path, "label": f"SQLite: {path.name}"})
    for container in find_docker_databases() or []:
        if container["engine"] not in {"postgres", "mysql"}:
            continue
        backend = "Rust" if container["engine"] == "postgres" else "Python"
        label = f"{container['engine'].upper()}: {container['name']} (Container, {backend})"
        items.append({"type": "docker", "target": container, "label": label})
    url = os.environ.get("DBCLI_POSTGRES_URL")
    if url:
        items.append({"type": "direct_postgres", "target": url, "label": connection_label(url) + " (Environment, Rust)"})
    items.extend([
        {"type": "open_sqlite", "label": "Open a SQLite file…"},
        {"type": "refresh", "label": "Refresh database list"},
    ])
    return items


def require_rust():
    if not all(path.is_file() and os.access(path, os.X_OK) for path in (WORKER, FRONTEND)):
        raise LaunchError(f"Build Rust once: bash {ROOT / 'tools/build_rust.sh'} (or use --python)")


def postgres_databases(url):
    """Discover database names through the selected native backend, not a Python driver."""
    try:
        result = subprocess.run(
            [str(WORKER), "--postgres", "--read-only", "--query", DATABASES_SQL, "--limit", "100000"],
            env=target_environment(url, read_only=True), capture_output=True, text=True, timeout=10,
        )
        if result.returncode:
            raise ValueError()
        rows = json.loads(result.stdout)["rows"]
        if not isinstance(rows, list) or any(not isinstance(row, list) or len(row) != 1 or not isinstance(row[0], str) for row in rows):
            raise ValueError()
        return [row[0] for row in rows]
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        raise LaunchError("Could not list PostgreSQL databases; check the container's published port and credentials") from None


@contextmanager
def restored_terminal():
    settings = None
    flags = {}
    try:
        fd = sys.stdin.fileno()
        if os.isatty(fd):
            settings = (fd, termios.tcgetattr(fd))
        for stream in (sys.stdin, sys.stdout):
            fd = stream.fileno()
            flags[fd] = fcntl.fcntl(fd, fcntl.F_GETFL)
    except (OSError, ValueError):
        pass
    try:
        yield
    finally:
        if settings:
            try:
                termios.tcsetattr(settings[0], termios.TCSANOW, settings[1])
            except OSError:
                pass
        for fd, value in flags.items():
            try:
                fcntl.fcntl(fd, fcntl.F_SETFL, value)
            except OSError:
                pass
        if sys.stdout.isatty():
            sys.stdout.write("\033[?25h\033[?1049l")
            sys.stdout.flush()


def run_native(argv, env):
    """Return to the picker after the owned UI exits; reap it on interruption."""
    exit_cleanup()
    sys.stdout.flush()
    with restored_terminal():
        try:
            child = subprocess.Popen(argv, env=env)
        except OSError:
            raise LaunchError("Could not start the Rust browser; rebuild with tools/build_rust.sh") from None
        try:
            return child.wait()
        except BaseException:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
            raise


def launch_selected(item, args, python_browser):
    kind = item["type"]
    if kind == "direct_mysql" or (kind == "docker" and item["target"]["engine"] == "mysql"):
        # The original controller can start at this exact target and return here.
        python_browser(args, initial_item=item)
        exit_cleanup()
        return 0
    require_rust()
    if kind == "sqlite":
        argv = command(database=sqlite_path(item["target"]), read_only=args.read_only)
        return run_native(argv, dict(os.environ))
    if kind == "docker":
        url, _ = docker_target(item["target"]["name"], read_only=True)
        databases = postgres_databases(url)
        if not databases:
            raise LaunchError("No accessible PostgreSQL databases found")
        enter_alt_screen()
        selected, _ = select_menu_item(
            f"Databases in '{item['target']['name']}'", databases,
            subtext="Choose a database to open in Rust",
        )
        if selected is None:
            return 0
        url = database_url(url, selected, read_only=args.read_only)
    elif kind == "direct_postgres":
        url = item["target"]
    else:
        raise LaunchError("Unsupported database target")
    env = target_environment(url, read_only=args.read_only)
    return run_native(command(postgres=True, read_only=args.read_only), env)


def open_sqlite_item():
    try:
        from prompt_toolkit.completion import PathCompleter
        completer = PathCompleter(expanduser=True)
    except ImportError:
        completer = None
    value = prompt_with_autocomplete(" SQLite file: ", completer=completer)
    if value is CANCELLED or not value:
        return None
    path = sqlite_path(value)
    return {"type": "sqlite", "target": path, "label": f"SQLite: {path.name}"}


def run(args, python_browser):
    status = "Choose a database · Rust browser · SQLite files and running containers"
    index = 0
    try:
        with stop_signals():
            if args.target:
                return launch_selected(explicit_target(args.target), args, python_browser)
            while True:
                enter_alt_screen()
                item, index = select_menu_item(
                    TITLE, discover_items(), display_key="label", initial_idx=index,
                    subtext=f"Scanning: {Path.cwd()} | {status}",
                )
                if item is None:
                    return 0
                if item["type"] == "refresh":
                    status = "Database list refreshed"
                    continue
                try:
                    if item["type"] == "open_sqlite":
                        item = open_sqlite_item()
                        if item is None:
                            continue
                    result = launch_selected(item, args, python_browser)
                    status = "Choose another database" if not result else f"Rust browser exited with status {result}; check the connection"
                except (LaunchError, TargetError) as exc:
                    status = str(exc)
    except (LaunchError, TargetError) as exc:
        exit_cleanup()
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except Stopped as exc:
        return 128 + exc.signum
    finally:
        exit_cleanup()
